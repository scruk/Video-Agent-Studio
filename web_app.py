"""Web Application & Dashboard for AI Video Agent (Opus + Workers + Higgsfield).
Runs with Tornado, provides WebSocket live streaming, approval modals, and video gallery.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import tornado.ioloop
import tornado.web
import tornado.websocket
from dotenv import dotenv_values, set_key

# Thêm đường dẫn thư mục hiện tại vào sys.path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import anthropic
from agent import BRAIN_MODEL, Budget, VideoAgent
from avatar_director import build_cutaway_avatar_video, calculate_dimensions
from stock_footage import find_stock_media, write_credits
from word_timing import build_word_timeline
from shot_planner import plan_shots, slots_from_shots
from higgsfield_client import HiggsfieldClient
from speaker_overlay import build_speaker_slots, generate_speaker_overlays
from text_effects import build_timed_cutaway_slots, generate_text_cards_with_claude
from voice_client import (
    DEFAULT_VOICE_CODE,
    POPULAR_VOICES,
    VbeeVoiceClient,
    VoiceError,
    mux_voice_into_video,
)

OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
ENV_FILE = BASE_DIR / ".env"


def get_bin(name: str) -> str:
    """Tìm binary ffmpeg/ffprobe từ PATH hoặc C:\\ffmpeg."""
    p = shutil.which(name)
    if p:
        return p
    for candidate in [
        Path(f"C:/ffmpeg/{name}.exe"),
        Path(f"C:/ffmpeg/bin/{name}.exe"),
    ]:
        if candidate.exists():
            return str(candidate)
    return name


def upload_image_to_public_url(local_path: Path) -> str | None:
    """Tự động đồng bộ ảnh từ máy cục bộ lên CDN công khai (Catbox/Tmpfiles) để Higgsfield AI có thể đọc được."""
    if not local_path.exists():
        return None

    # 1. Thử Catbox.moe
    try:
        import httpx
        with open(local_path, "rb") as fh:
            resp = httpx.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": fh},
                timeout=25.0
            )
            if resp.status_code == 200 and resp.text.startswith("https://"):
                return resp.text.strip()
    except Exception:
        pass

    # 2. Thử Tmpfiles.org
    try:
        import httpx
        with open(local_path, "rb") as fh:
            resp = httpx.post(
                "https://tmpfiles.org/api/v1/upload",
                files={"file": fh},
                timeout=25.0
            )
            if resp.status_code == 200:
                data = resp.json()
                raw_url = data.get("data", {}).get("url", "")
                if raw_url:
                    return raw_url.replace("tmpfiles.org/", "tmpfiles.org/dl/")
    except Exception:
        pass

    return None


class AgentRunner:
    """Quản lý tiến trình chạy Agent trong background thread."""

    def __init__(self):
        self.thread: Optional[threading.Thread] = None
        self.is_running: bool = False
        self.current_agent: Optional[VideoAgent] = None
        self.pending_approval: Optional[threading.Event] = None
        self.approval_result: str = "n"
        self.approval_prompt: str = ""
        self.last_result: str = ""
        self.subscribers: set[LiveWebSocketHandler] = set()
        self.loop: Optional[tornado.ioloop.IOLoop] = None
        self.log_history: list[dict[str, Any]] = []

    def broadcast(self, data: dict[str, Any]):
        """Gửi thông điệp tới tất cả các WebSocket client đang kết nối."""
        if not self.loop:
            return
        msg = json.dumps(data, ensure_ascii=False)

        def _send():
            dead = set()
            for ws in self.subscribers:
                try:
                    ws.write_message(msg)
                except Exception:
                    dead.add(ws)
            self.subscribers.difference_update(dead)

        self.loop.add_callback(_send)

    def log(self, text: str):
        entry = {"type": "log", "text": text, "timestamp": time.time()}
        self.log_history.append(entry)
        if len(self.log_history) > 300:
            self.log_history.pop(0)
        self.broadcast(entry)

    def ask_user(self, question: str) -> str:
        """Được gọi bởi VideoAgent khi cần hỏi [y/N]. Chờ người dùng bấm trên Web UI."""
        self.pending_approval = threading.Event()
        self.approval_prompt = question
        self.broadcast(
            {
                "type": "approval_required",
                "question": question,
                "timestamp": time.time(),
            }
        )
        # Chờ tối đa 15 phút
        finished = self.pending_approval.wait(timeout=900)
        self.pending_approval = None
        self.approval_prompt = ""
        if not finished:
            self.log("⚠️ Hết thời gian chờ duyệt (Timeout), tự động hủy lượt render.")
            return "n"
        return self.approval_result

    def resolve_approval(self, answer: str):
        """Web UI gọi API này để gửi kết quả duyệt."""
        if self.pending_approval:
            self.approval_result = "y" if answer.lower() in ("y", "yes", "true", "ok", "co", "có") else "n"
            self.pending_approval.set()
            self.broadcast(
                {
                    "type": "approval_resolved",
                    "answer": self.approval_result,
                    "timestamp": time.time(),
                }
            )

    def start(self, goal: str, budget_usd: float, max_shots: int = 12,
              max_attempts: int = 2, auto_approve: bool = False,
              aspect_ratio: str = "9:16", resolution: str = "720p",
              demo_mode: bool = False,
              ref_images: list[dict[str, Any]] | None = None,
              image_urls: list[str] | None = None,
              enable_voice: bool = False,
              voice_code: str = DEFAULT_VOICE_CODE,
              voice_speed: float = 1.0,
              voice_script: str = "",
              production_mode: str = "cinema",
              auto_broll: bool = True,
              broll_interval: float = 5.0,
              avatar_ai_motion: bool = True,
              avatar_ai_model: str = "kling-video/v3.0-turbo/image-to-video",
              prefer_text_effects: bool = True,
              speaker_overlays: bool = True,
              sfx_enabled: bool = True,
              sfx_volume: float = 0.5,
              transitions: bool = True,
              stock_footage: bool = True,
              subtitle_mode: str = "word"):
        if self.is_running:
            raise RuntimeError("Agent đang trong quá trình sản xuất video khác!")

        ref_images = ref_images or []
        image_urls = image_urls or []
        voice_code = (voice_code or DEFAULT_VOICE_CODE).strip()
        is_avatar_mode = (production_mode == "avatar")

        # Đọc env mới nhất
        env = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
        anthropic_key = env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
        hf_key_id = env.get("HF_API_KEY_ID") or os.environ.get("HF_API_KEY_ID", "")
        hf_secret = env.get("HF_API_KEY_SECRET") or os.environ.get("HF_API_KEY_SECRET", "")
        vbee_app_id = env.get("VBEE_APP_ID") or os.environ.get("VBEE_APP_ID", "")
        vbee_token = env.get("VBEE_ACCESS_TOKEN") or os.environ.get("VBEE_ACCESS_TOKEN", "")
        pixabay_key = env.get("PIXABAY_API_KEY") or os.environ.get("PIXABAY_API_KEY", "")

        if not demo_mode:
            if not anthropic_key:
                raise ValueError("Chưa có ANTHROPIC_API_KEY trong file .env! (Hoặc bật 'Chế độ chạy thử nghiệm/Demo' để test trước)")
            if not hf_key_id or not hf_secret:
                raise ValueError("Chưa có HF_API_KEY_ID hoặc HF_API_KEY_SECRET trong .env! (Hoặc bật 'Chế độ chạy thử nghiệm/Demo' để test trước)")
            if enable_voice and (not vbee_app_id or not vbee_token):
                raise ValueError("Bạn đang bật Lồng tiếng AI nhưng chưa điền VBEE_APP_ID và VBEE_ACCESS_TOKEN trong mục API Keys (.env)!")
            os.environ["ANTHROPIC_API_KEY"] = anthropic_key
            os.environ["HF_API_KEY_ID"] = hf_key_id
            os.environ["HF_API_KEY_SECRET"] = hf_secret
            if vbee_app_id and vbee_token:
                os.environ["VBEE_APP_ID"] = vbee_app_id
                os.environ["VBEE_ACCESS_TOKEN"] = vbee_token

        # Lưu các ảnh tham chiếu tải lên vào thư mục uploads/ và chuẩn bị multimodal blocks
        saved_ref_paths: list[Path] = []
        image_blocks: list[dict[str, Any]] = []
        for idx, img in enumerate(ref_images):
            data_url = img.get("data_url", "")
            media_type = img.get("media_type", "image/jpeg")
            if "," in data_url:
                header, b64_data = data_url.split(",", 1)
                if "image/" in header:
                    media_type = header.split(";")[0].replace("data:", "")
                ext = media_type.split("/")[-1]
                if ext not in ("jpeg", "jpg", "png", "webp", "gif"):
                    ext = "jpg"
                save_path = UPLOADS_DIR / f"ref_{int(time.time())}_{idx}.{ext}"
                try:
                    save_path.write_bytes(base64.b64decode(b64_data))
                    saved_ref_paths.append(save_path)
                except Exception:
                    pass
                image_blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type if media_type != "image/jpg" else "image/jpeg",
                        "data": b64_data,
                    },
                })

        # Định dạng prompt kèm yêu cầu aspect ratio / resolution, ảnh tham chiếu và lồng tiếng
        extra_notes = [f"Yêu cầu kỹ thuật: Tỷ lệ {aspect_ratio}, độ phân giải {resolution}."]
        active_image_urls: list[str] = list(image_urls)
        if image_blocks:
            extra_notes.append(
                f"Người dùng đã đính kèm {len(image_blocks)} hình ảnh tham chiếu. "
                "Hãy quan sát kỹ hình ảnh (sản phẩm, nhân vật, màu sắc, bố cục) để đưa mô tả chính xác vào style bible và từng shot."
            )
        if active_image_urls:
            extra_notes.append(
                f"Danh sách URL ảnh tham chiếu cần truyền vào tham số image_urls của generate_video/estimate_cost: {json.dumps(active_image_urls)}"
            )
        if enable_voice:
            script_note = (
                f"Lời bình người dùng yêu cầu đọc: \"{voice_script.strip()}\""
                if voice_script.strip()
                else "Hãy tự sáng tác lời bình thuyết minh (voiceover) tiếng Việt ngắn gọn, cuốn hút khớp với thời lượng từng shot."
            )
            extra_notes.append(
                f"YÊU CẦU LỒNG TIẾNG (VOICEOVER): Sử dụng công cụ generate_voice với voice_code='{voice_code}', speed_rate={voice_speed}. "
                f"{script_note} Sau khi có file video và file voice (.mp3), hãy dùng công cụ mux_voice_video để ghép giọng đọc vào từng shot video!"
            )
        formatted_text = goal + "\n" + "\n".join(extra_notes)
        formatted_goal: Any = (
            [*image_blocks, {"type": "text", "text": formatted_text}]
            if image_blocks
            else formatted_text
        )

        def worker():
            nonlocal active_image_urls
            self.is_running = True
            self.broadcast({"type": "status", "status": "running", "goal": goal})
            self.log(f"🎬 Bắt đầu sản xuất: '{goal}'")
            self.log(f"⚙️ Thiết lập: Ngân sách ${budget_usd:.2f} | Tỷ lệ {aspect_ratio} | {resolution}")
            if saved_ref_paths or active_image_urls:
                self.log(f"🖼️ Đã nhận {len(saved_ref_paths)} ảnh tải lên và {len(active_image_urls)} link ảnh URL tham chiếu.")
            if enable_voice:
                self.log(f"🎙️ Lồng tiếng Bee Voice AI: BẬT (Giọng: {voice_code} | Tốc độ: {voice_speed}x)")

            if is_avatar_mode:
                self.log("🗣️ [TALKING AVATAR MODE] Kích hoạt quy trình nhân vật thuyết trình (Tiết kiệm >80% chi phí)...")
                ffmpeg_bin = get_bin("ffmpeg")
                ffprobe_bin = get_bin("ffprobe")

                avatar_img_path = saved_ref_paths[0] if saved_ref_paths else None
                avatar_url = active_image_urls[0] if active_image_urls else None
                if not avatar_img_path and not avatar_url:
                    err = "Chế độ Nhân Vật Thuyết Trình yêu cầu bạn tải lên 1 ảnh chân dung nhân vật!"
                    self.log(f"❌ {err}")
                    self.broadcast({"type": "finished", "status": "error", "error": err})
                    self.is_running = False
                    self.broadcast({"type": "status", "status": "idle"})
                    return

                try:
                    # BƯỚC 1: Soạn lời thoại
                    speech_text = voice_script.strip()
                    if not speech_text:
                        if demo_mode:
                            speech_text = (
                                f"Xin chào tất cả các bạn! Hôm nay tôi rất vui được chia sẻ cùng các bạn về: {goal}. "
                                "Hãy cùng theo dõi những thông tin hữu ích ngay sau đây nhé!"
                            )
                        else:
                            self.log("✍️ [Claude Sonnet] Đang soạn lời thoại thuyết trình cuốn hút & chuẩn điện ảnh...")
                            client_anthropic = anthropic.Anthropic(api_key=anthropic_key)
                            prompt_msg = (
                                f"Bạn là chuyên gia biên kịch video thuyết trình triệu view. "
                                f"Hãy viết một bài phát biểu/thuyết trình bằng tiếng Việt cực kỳ tự nhiên, gãy gọn, truyền cảm hứng "
                                f"(không kèm tiêu đề, không để dấu ngoặc hay chú thích camera) cho chủ đề sau: {goal}"
                            )
                            writer_models = [
                                os.getenv("BRAIN_MODEL", "claude-opus-5-5"),
                                "claude-opus-5-5",
                                "claude-opus-5",
                                "claude-sonnet-5-5",
                                "claude-sonnet-5",
                            ]
                            speech_text = ""
                            for m in writer_models:
                                try:
                                    res_msg = client_anthropic.messages.create(
                                        model=m,
                                        max_tokens=1500,
                                        messages=[{"role": "user", "content": prompt_msg}],
                                    )
                                    speech_text = "".join(getattr(b, "text", "") for b in res_msg.content if getattr(b, "type", "") == "text").strip()
                                    if speech_text:
                                        break
                                except Exception:
                                    continue
                            if not speech_text:
                                speech_text = f"Xin chào các bạn! Hôm nay tôi rất vui được chia sẻ cùng các bạn về: {goal}. Hãy cùng theo dõi nội dung chi tiết ngay sau đây."

                    self.log(f"📝 Lời thoại ({len(speech_text)} ký tự): \"{speech_text[:90]}...\"")

                    # BƯỚC 2: Sinh file âm thanh Bee Voice AI
                    stem_audio = f"vo_avatar_{int(time.time())}"
                    audio_file = OUTPUT_DIR / f"{stem_audio}.mp3"
                    if demo_mode:
                        self.log(f"🎙️ [Demo Voice] Tạo file giọng nói mẫu ({voice_code})...")
                        time.sleep(1.0)
                        subprocess.run(
                            [ffmpeg_bin, "-y", "-f", "lavfi", "-i", "sine=frequency=480:duration=6",
                             "-c:a", "libmp3lame", "-q:a", "4", str(audio_file)],
                            capture_output=True, check=False,
                        )
                        audio_dur = 6.0
                    else:
                        self.log(f"🎙️ [Bee Voice AI] Đang tạo giọng đọc chuẩn tiếng Việt ({voice_code})...")
                        v_client = VbeeVoiceClient(vbee_app_id, vbee_token, default_voice=voice_code)
                        try:
                            res_voice = v_client.synthesize(
                                speech_text, OUTPUT_DIR, stem=stem_audio,
                                voice_code=voice_code, speed_rate=voice_speed,
                                on_status=lambda s: self.log(f"  [vbee] {s}"),
                            )
                            audio_file = Path(res_voice["path"])
                        finally:
                            v_client.close()
                        # Đo thời lượng thực tế
                        dur_res = subprocess.run(
                            [ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(audio_file)],
                            capture_output=True, text=True, check=True,
                        )
                        audio_dur = float(dur_res.stdout.strip())

                    self.log(f"⏱️ Thời lượng phát biểu: {audio_dur:.1f} giây")

                    # BƯỚC 3: Ước tính chi phí & Hỏi duyệt
                    selected_model = avatar_ai_model or env.get("HF_VIDEO_MODEL", "kling-video/v3.0-turbo/image-to-video")
                    is_i2v_model = "image-to-video" in selected_model.lower()

                    base_voice_cost = Decimal(str(round(max(0.05, audio_dur * 0.0006), 3)))
                    if "kling" in selected_model.lower():
                        hf_shot_cost = Decimal("0.25") if avatar_ai_motion else Decimal("0.0")
                        model_label = "Kling 3.0 Turbo (~$0.25)"
                    elif "seedance" in selected_model.lower():
                        hf_shot_cost = Decimal("0.30") if avatar_ai_motion else Decimal("0.0")
                        model_label = "ByteDance Seedance 2.5 (~$0.30)"
                    else:
                        hf_shot_cost = Decimal("2.31") if avatar_ai_motion else Decimal("0.0")
                        model_label = "Cinema Studio 4.0 (~$2.31)"

                    if avatar_ai_motion and not demo_mode and hf_key_id and hf_secret:
                        try:
                            temp_hf = HiggsfieldClient(hf_key_id, hf_secret)
                            est_params: dict[str, Any] = {"prompt": "speaking", "duration": 5, "resolution": resolution}
                            if not is_i2v_model:
                                est_params["aspect_ratio"] = aspect_ratio
                            est_val = temp_hf.estimate(selected_model, est_params)
                            if est_val is not None:
                                hf_shot_cost = est_val
                            temp_hf.close()
                        except Exception:
                            pass

                    est_cost = Decimal("0.38") if demo_mode else (hf_shot_cost + base_voice_cost)
                    broll_paths = saved_ref_paths[1:] if len(saved_ref_paths) > 1 else []
                    cutaway_info = f"xen kẽ {len(broll_paths)} cảnh B-Roll" if broll_paths else "cắt cảnh đa góc quay camera"
                    mode_info = f"Phương án B (1 shot AI cử động 5s [{model_label}] + Cắt cảnh)" if avatar_ai_motion else "Phương án A (Ảnh tĩnh + Ken Burns 0đ)"

                    if not auto_approve:
                        q = (
                            f"\nAgent muốn render Nhân Vật Thuyết Trình [{mode_info}]:\n"
                            f"  - 1 Nhân vật nói chuyện ({audio_dur:.1f}s | Giọng: {voice_code})\n"
                            f"  - Cử động AI: {'BẬT (1 shot 5s ' + model_label + ')' if avatar_ai_motion else 'TẮT (0đ)'}\n"
                            f"  - Cắt cảnh thông minh ({cutaway_info}): {'BẬT' if auto_broll else 'TẮT'}\n"
                            f"Tổng chi phí ước tính ~${est_cost:.3f} (Tiết kiệm >95% so với multi-shot full video).\n"
                            f"Ngân sách còn ${budget_usd - float(est_cost):.2f}. Duyệt chạy? [y/N] "
                        )
                        ans = self.ask_user(q)
                        if ans.lower() not in ("y", "yes", "co", "có", "ok"):
                            self.log("⚠️ Người dùng từ chối duyệt render. Dừng tác vụ.")
                            self.broadcast({"type": "finished", "status": "canceled", "result": "Đã hủy theo yêu cầu."})
                            return
                    else:
                        self.log(f"⚡ [Auto-Approve] Bật Tự Động Duyệt: Tự động phê duyệt chi phí ~${est_cost:.3f} [{mode_info}].")
                        self.log("🚀 Không cần chờ xác nhận, hệ thống bắt đầu ngay tiến trình Render & Dựng phim...")

                    # BƯỚC 4: Tạo video nhân vật chuyển động & đồng bộ âm thanh
                    avatar_source_path = avatar_img_path or Path(avatar_url)

                    # PHƯƠNG ÁN B: Render 1 shot 5s cử động AI thật từ Higgsfield
                    if avatar_ai_motion:
                        self.log("✨ [Phương án B] Đang render 1 shot 5s cử động AI thật cho nhân vật từ Higgsfield (~$0.30)...")
                        stem_hf = f"avatar_motion_{int(time.time())}"
                        if demo_mode:
                            time.sleep(1.2)
                            self.log("  [Demo Higgsfield] Giả lập hoàn thành shot 5s cử động khuôn mặt tự nhiên!")
                            demo_clip = OUTPUT_DIR / f"{stem_hf}.mp4"
                            scale_w, scale_h = ("720", "1280") if aspect_ratio == "9:16" else ("1280", "720")
                            if aspect_ratio == "1:1":
                                scale_w, scale_h = ("720", "720")
                            vf_demo = f"scale={scale_w}:{scale_h}:force_original_aspect_ratio=decrease,pad={scale_w}:{scale_h}:(ow-iw)/2:(oh-ih)/2,zoompan=z='min(zoom+0.0008,1.08)':d=125:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={scale_w}x{scale_h}:fps=25,format=yuv420p"
                            subprocess.run([
                                ffmpeg_bin, "-y", "-loop", "1", "-i", str(avatar_img_path or avatar_url),
                                "-t", "5.0", "-vf", vf_demo,
                                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "25",
                                str(demo_clip)
                            ], capture_output=True, check=True)
                            avatar_source_path = demo_clip
                        else:
                            hf_client = HiggsfieldClient(hf_key_id, hf_secret)
                            try:
                                # Tự động đồng bộ ảnh chân dung lên hạ tầng lưu trữ Higgsfield
                                if avatar_img_path and not active_image_urls:
                                    self.log("🌐 [CDN Sync] Đang đồng bộ ảnh chân dung lên kho lưu trữ Higgsfield...")
                                    try:
                                        pub_url = hf_client.upload_file(avatar_img_path)
                                        if pub_url:
                                            active_image_urls = [pub_url]
                                            self.log(f"  [CDN] ✅ Đã tải ảnh lên Higgsfield Storage: {pub_url}")
                                    except Exception as exc:
                                        self.log(f"  [CDN] ⚠️ Tải lên Higgsfield Storage ({exc}), thử CDN dự phòng...")
                                        pub_url = upload_image_to_public_url(avatar_img_path)
                                        if pub_url:
                                            active_image_urls = [pub_url]
                                            self.log(f"  [CDN] ✅ Đã lấy được link ảnh CDN dự phòng: {pub_url}")

                                hf_model = selected_model
                                prompt_motion = (
                                    f"Character consistency: Exact likeness, face structure, facial features, hair, and clothing of the person in the reference image. "
                                    f"Medium close-up, speaking naturally, expressive facial movements, subtle natural head nod and blink, talking gestures, photorealistic 8k, studio lighting. "
                                    f"Context: {goal[:100]}"
                                )
                                hf_params: dict[str, Any] = {
                                    "prompt": prompt_motion,
                                    "duration": 5,
                                    "resolution": resolution,
                                }
                                if not is_i2v_model:
                                    hf_params["aspect_ratio"] = aspect_ratio
                                    hf_params["generate_audio"] = False

                                if active_image_urls:
                                    if is_i2v_model:
                                        hf_params["image_url"] = active_image_urls[0]
                                    else:
                                        hf_params["image_urls"] = active_image_urls[:1]

                                self.log(f"  [Higgsfield] Gửi yêu cầu render 5s nhân vật (model: {hf_model})...")
                                try:
                                    res_hf = hf_client.generate(
                                        model=hf_model,
                                        params=hf_params,
                                        dest_dir=OUTPUT_DIR,
                                        stem=stem_hf,
                                        on_status=lambda s: self.log(f"  [Higgsfield] Trạng thái: {s}")
                                    )
                                    avatar_source_path = Path(res_hf["files"][0])
                                    self.log(f"  [Higgsfield] ✅ Render thành công video nhân vật 5s: {avatar_source_path.name}")
                                except Exception as hf_err:
                                    self.log(f"⚠️ [Higgsfield] Máy chủ Higgsfield tạm thời lỗi ({hf_err}). Tự động kích hoạt cơ chế Dynamic Ken Burns Motion dự phòng...")
                                    fallback_clip = OUTPUT_DIR / f"{stem_hf}_kb.mp4"
                                    scale_w, scale_h = ("720", "1280") if aspect_ratio == "9:16" else ("1280", "720")
                                    if aspect_ratio == "1:1":
                                        scale_w, scale_h = ("720", "720")
                                    vf_kb = f"scale={scale_w}:{scale_h}:force_original_aspect_ratio=decrease,pad={scale_w}:{scale_h}:(ow-iw)/2:(oh-ih)/2,zoompan=z='min(zoom+0.0008,1.08)':d=125:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={scale_w}x{scale_h}:fps=25,format=yuv420p"
                                    subprocess.run([
                                        ffmpeg_bin, "-y", "-loop", "1", "-i", str(avatar_img_path or avatar_url),
                                        "-t", "5.0", "-vf", vf_kb,
                                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "25",
                                        str(fallback_clip)
                                    ], capture_output=True, check=True)
                                    avatar_source_path = fallback_clip
                                    self.log(f"  [Fallback Motion] ✅ Đã tạo chuyển động camera mượt mà cho nhân vật: {fallback_clip.name}")
                            finally:
                                hf_client.close()

                    self.log("🎬 [Video Render] Đang xử lý dựng video và đồng bộ âm thanh...")
                    out_avatar_video = OUTPUT_DIR / f"avatar_{int(time.time())}.mp4"

                    # Mốc thời gian từng từ (dò khoảng lặng trong audio) -> phụ đề nhảy từng từ khớp giọng
                    word_timeline = build_word_timeline(speech_text, audio_file, audio_dur, ffmpeg_bin=ffmpeg_bin)
                    if word_timeline:
                        self.log(f"⏱️ [Phụ đề] Canh giờ {len(word_timeline)} từ theo khoảng ngắt hơi thật trong giọng đọc")

                    # Opus xem kịch bản: ý dẫn chuyện -> người nói, ý chính -> cảnh minh hoạ
                    shots = None
                    if auto_broll and word_timeline:
                        self.log("🎬 [Opus] Đang đọc kịch bản để chọn đoạn nào để người nói, đoạn nào cho minh hoạ...")
                        shots = plan_shots(speech_text, word_timeline, audio_dur, interval=broll_interval,
                                           anthropic_key=anthropic_key, goal=goal, demo_mode=demo_mode,
                                           log=lambda s: self.log(s))

                    text_cards = None
                    if auto_broll and (prefer_text_effects or not broll_paths):
                        timed_slots = (slots_from_shots(shots, "visual", word_timeline) if shots else
                                       build_timed_cutaway_slots(
                                           speech_text=speech_text,
                                           total_dur=audio_dur,
                                           interval=broll_interval,
                                           timeline=word_timeline,
                                       ))
                        num_cutaways = len(timed_slots)
                        self.log(f"✍️ [Claude Opus 5.5] Đang phân tích kịch bản bằng mô hình cao cấp nhất (Claude Opus 5.5) thành {num_cutaways} mốc cắt cảnh đồng bộ...")
                        for s in timed_slots:
                            excerpt_preview = s['spoken_excerpt'][:50] + ("..." if len(s['spoken_excerpt']) > 50 else "")
                            self.log(f"   ⏱️ Slot #{s['slot_index']+1} [{s['time_range']}]: \"{excerpt_preview}\"")
                        text_cards = generate_text_cards_with_claude(
                            speech_text=speech_text,
                            num_cards=num_cutaways,
                            timed_slots=timed_slots,
                            anthropic_key=anthropic_key,
                            demo_mode=demo_mode,
                            goal=goal,
                        )
                        self.log(f"  [Opus 5.5] ✅ Đã sáng tác {len(text_cards)} thẻ Text Effect kinetic đồ họa đỉnh cao khớp chuẩn giọng đọc!")

                    stock_clips: dict = {}
                    if auto_broll and text_cards and stock_footage and not demo_mode:
                        if not pixabay_key:
                            self.log("ℹ️ [Pixabay] Chưa có PIXABAY_API_KEY trong .env, bỏ qua cảnh quay thật.")
                        else:
                            sw, sh = calculate_dimensions(aspect_ratio, resolution)
                            self.log("🎥 [Pixabay] Opus đọc kịch bản → xuất từ khoá từng cảnh → tìm video/ảnh → xem thumbnail để chọn...")
                            stock_clips = find_stock_media(
                                speech_text=speech_text,
                                cards=text_cards,
                                slots=timed_slots,
                                w=sw, h=sh,
                                seg_dur=(max((s_["duration"] for s_ in timed_slots), default=broll_interval) + 0.4),
                                pixabay_key=pixabay_key,
                                anthropic_key=anthropic_key,
                                cache_dir=OUTPUT_DIR / "_stock_cache",
                                goal=goal,
                                on_status=lambda s: self.log(s),
                            )
                            self.log(f"  [Pixabay] ✅ Chèn {len(stock_clips)} hình thật đã được Opus duyệt"
                                     + ("" if stock_clips else " (các cảnh còn lại giữ đồ hoạ)"))

                    overlay_list = None
                    if auto_broll and speaker_overlays:
                        sp_slots = (slots_from_shots(shots, "speaker", word_timeline) if shots else
                                    build_speaker_slots(speech_text=speech_text, total_dur=audio_dur,
                                                        interval=broll_interval, timeline=word_timeline))
                        self.log(f"💬 [Claude] Đang soạn phụ đề + thẻ text/icon động cho {len(sp_slots)} cảnh người nói...")
                        overlay_list = generate_speaker_overlays(
                            speech_text=speech_text,
                            slots=sp_slots,
                            anthropic_key=anthropic_key,
                            demo_mode=demo_mode,
                            goal=goal,
                        )

                    if auto_broll:
                        self.log(f"✂️ [Auto B-Roll Cutaway] Kích hoạt cắt cảnh xen kẽ thông minh (nhịp ~{broll_interval:.1f}s/cảnh)...")
                        build_cutaway_avatar_video(
                            avatar_img=avatar_source_path,
                            broll_images=broll_paths,
                            audio_file=audio_file,
                            out_path=out_avatar_video,
                            total_dur=audio_dur,
                            aspect_ratio=aspect_ratio,
                            resolution=resolution,
                            interval=broll_interval,
                            text_cards=text_cards,
                            prefer_text_effects=prefer_text_effects,
                            speaker_overlays=overlay_list,
                            transitions=transitions,
                            sfx=sfx_enabled,
                            sfx_volume=sfx_volume,
                            stock_clips=stock_clips,
                            word_timeline=word_timeline,
                            subtitle_mode=subtitle_mode,
                            shots=shots,
                            on_status=lambda s: self.log(s),
                        )
                        try:  # lưu lời thoại + mốc từng từ cạnh video để tra lại khi phụ đề lệch
                            out_avatar_video.with_suffix(".words.json").write_text(
                                json.dumps({"script": speech_text, "words": word_timeline,
                                            "shots": [{k: v for k, v in s_.items() if k != "beats"} for s_ in (shots or [])]},
                                           ensure_ascii=False, indent=1),
                                "utf-8")
                        except Exception:
                            pass
                        if write_credits(stock_clips, out_avatar_video):
                            self.log(f"  📄 Nguồn clip Pixabay: {out_avatar_video.with_suffix('.credits.txt').name}")
                    else:
                        scale_w, scale_h = ("720", "1280") if aspect_ratio == "9:16" else ("1280", "720")
                        if aspect_ratio == "1:1":
                            scale_w, scale_h = ("720", "720")
                        is_vid = str(avatar_source_path).lower().endswith((".mp4", ".mov", ".mkv", ".webm"))
                        in_args = ["-stream_loop", "-1", "-i", str(avatar_source_path)] if is_vid else ["-loop", "1", "-i", str(avatar_source_path)]
                        cmd = [
                            ffmpeg_bin, "-y",
                            *in_args,
                            "-i", str(audio_file),
                            "-vf", f"scale={scale_w}:{scale_h}:force_original_aspect_ratio=decrease,pad={scale_w}:{scale_h}:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
                            "-c:v", "libx264",
                            "-c:a", "aac", "-b:a", "192k",
                            "-shortest",
                            str(out_avatar_video),
                        ]
                        subprocess.run(cmd, capture_output=True, check=True)

                    res_msg = f"Hoàn thành video Nhân Vật Thuyết Trình ({audio_dur:.1f}s)! File đã lưu tại: {out_avatar_video.name}"
                    self.last_result = res_msg
                    self.log(f"✅ {res_msg}")
                    self.broadcast({
                        "type": "finished",
                        "status": "completed",
                        "result": res_msg,
                        "spent": float(est_cost),
                        "budget": budget_usd,
                    })
                except Exception as exc:
                    self.last_result = f"Lỗi: {exc}"
                    self.log(f"❌ Xảy ra lỗi: {exc}")
                    self.broadcast({"type": "finished", "status": "error", "error": str(exc)})
                finally:
                    self.is_running = False
                    self.broadcast({"type": "status", "status": "idle"})
                return

            if demo_mode:
                self.log("🧪 [DEMO MODE] Đang chạy chế độ giả lập (Không tốn tiền API thật)...")
                try:
                    time.sleep(1.2)
                    if saved_ref_paths or active_image_urls:
                        self.log("  [Opus Vision] Đang phân tích hình ảnh tham chiếu (màu sắc, chủ thể, ánh sáng)...")
                        time.sleep(1.0)
                    self.log("[vòng 1] delegate_task, delegate_task, estimate_cost, estimate_cost")
                    time.sleep(1.5)
                    self.log("  [Opus 5.5] Hoàn thành prompt shot 1 (bám sát hình ảnh tham chiếu)")
                    self.log("  [Opus 5.5] Hoàn thành prompt shot 2 (chuyển động camera mượt mà)")
                    time.sleep(1.0)
                    
                    question = (
                        "\nAgent muốn render:\n"
                        "  - s01: ~$0.50 (giá ước tính)\n"
                        "  - s02: ~$0.50 (giá ước tính)\n"
                        f"Tổng ~$1.000, ngân sách còn ${budget_usd - 1.0:.3f}. Chạy? [y/N] "
                    )
                    ans = "y" if auto_approve else self.ask_user(question)
                    if ans.lower() not in ("y", "yes", "co", "có", "ok"):
                        self.log("⚠️ Người dùng từ chối duyệt render. Dừng tác vụ.")
                        self.broadcast({"type": "finished", "status": "canceled", "result": "Đã hủy theo yêu cầu."})
                        return

                    self.log("[vòng 2] generate_video, generate_video" + (", generate_voice, generate_voice" if enable_voice else ""))
                    ffmpeg_bin = get_bin("ffmpeg")
                    for idx, (s_id, color) in enumerate([("s01", "darkblue"), ("s02", "darkgreen")]):
                        self.log(f"  [{s_id}] Đang render trên Higgsfield AI... (processing)")
                        time.sleep(1.5)
                        out_file = OUTPUT_DIR / f"{s_id}_a1.mp4"
                        # Nếu có ảnh tham chiếu từ máy, tạo video từ ảnh tham chiếu đó
                        if saved_ref_paths:
                            ref_img = saved_ref_paths[idx % len(saved_ref_paths)]
                            cmd = [
                                ffmpeg_bin, "-y", "-loop", "1", "-i", str(ref_img),
                                "-t", "3",
                                "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
                                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_file)
                            ]
                        else:
                            cmd = [
                                ffmpeg_bin, "-y", "-f", "lavfi",
                                "-i", f"color=c={color}:s=720x1280:d=3",
                                "-vf", f"drawtext=text='SHOT {s_id.upper()} (DEMO)':fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2",
                                "-pix_fmt", "yuv420p", str(out_file)
                            ]
                        subprocess.run(cmd, capture_output=True, check=False)
                        self.log(f"  [{s_id}] Render hoàn tất! Đã tải về: {out_file.name}")

                        if enable_voice:
                            vo_id = f"vo_{s_id}"
                            vo_file = OUTPUT_DIR / f"{vo_id}.mp3"
                            freq = 440 if idx == 0 else 528
                            subprocess.run(
                                [ffmpeg_bin, "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=3",
                                 "-c:a", "libmp3lame", "-q:a", "4", str(vo_file)],
                                capture_output=True, check=False
                            )
                            self.log(f"  [voice:{vo_id}] Đã tạo giọng đọc mẫu ({voice_code}) -> {vo_file.name}")
                            voiced_out = OUTPUT_DIR / f"{s_id}_voiced.mp4"
                            try:
                                mux_voice_into_video(out_file, vo_file, voiced_out)
                                self.log(f"  [mux] Đã lồng tiếng {vo_file.name} + {out_file.name} -> {voiced_out.name}")
                            except Exception as mux_err:
                                self.log(f"  ⚠️ Lỗi ghép tiếng demo: {mux_err}")

                    self.log("[vòng 3] review_shot, review_shot")
                    time.sleep(1.5)
                    self.log("  [Opus QA] Đã kiểm tra 3 frames của s01: Ánh sáng đạt, chuyển động mượt mà.")
                    self.log("  [Opus QA] Đã kiểm tra 3 frames của s02: Khói trà đạt độ chân thực cao.")
                    time.sleep(1.0)
                    res_text = "Hoàn thành 2 shot mẫu" + (" (đã lồng tiếng thuyết minh)" if enable_voice else "") + ". Các file đã sẵn sàng trong thư mục output!"
                    self.last_result = res_text
                    self.log(f"✅ {res_text}")
                    self.broadcast({
                        "type": "finished",
                        "status": "completed",
                        "result": res_text,
                        "spent": 1.0,
                        "budget": budget_usd,
                    })
                except Exception as e:
                    self.log(f"❌ Lỗi demo: {e}")
                finally:
                    self.is_running = False
                    self.broadcast({"type": "status", "status": "idle"})
                return

            # Chế độ chạy thật
            hf = HiggsfieldClient(hf_key_id, hf_secret)
            voice_client = (
                VbeeVoiceClient(vbee_app_id, vbee_token, default_voice=voice_code)
                if (vbee_app_id and vbee_token)
                else None
            )
            budget = Budget(Decimal(str(budget_usd)), max_attempts, max_shots)
            agent = VideoAgent(
                anthropic.Anthropic(api_key=anthropic_key),
                hf,
                budget,
                OUTPUT_DIR,
                voice=voice_client,
                auto_approve=auto_approve,
                ask=self.ask_user,
                out=self.log,
            )
            self.current_agent = agent

            try:
                result = agent.run(formatted_goal)
                self.last_result = result
                self.log(f"✅ Hoàn thành: {result}")
                self.broadcast({
                    "type": "finished",
                    "status": "completed",
                    "result": result,
                    "spent": float(agent.budget.spent),
                    "budget": float(agent.budget.max_usd),
                })
            except Exception as e:
                self.last_result = f"Lỗi: {e}"
                self.log(f"❌ Xảy ra lỗi: {e}")
                self.broadcast({
                    "type": "finished",
                    "status": "error",
                    "error": str(e),
                })
            finally:
                hf.close()
                if voice_client:
                    voice_client.close()
                self.is_running = False
                self.current_agent = None
                self.broadcast({"type": "status", "status": "idle"})

        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()


runner = AgentRunner()


# ---- CÁC HANDLER API ------------------------------------------------
class BaseHandler(tornado.web.RequestHandler):
    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Headers", "Content-Type")
        self.set_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, DELETE")

    def options(self, *args, **kwargs):
        self.set_status(204)
        self.finish()

    def write_json(self, data: Any, status: int = 200):
        self.set_status(status)
        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.write(json.dumps(data, ensure_ascii=False))


class IndexHandler(tornado.web.RequestHandler):
    def get(self):
        self.render("templates/index.html")


class EnvHandler(BaseHandler):
    def get(self):
        env = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}

        def mask(val: str | None) -> str:
            if not val:
                return ""
            if len(val) <= 8:
                return "••••••••"
            return f"{val[:4]}••••••••{val[-4:]}"

        self.write_json({
            "has_anthropic": bool(env.get("ANTHROPIC_API_KEY")),
            "anthropic_masked": mask(env.get("ANTHROPIC_API_KEY")),
            "has_hf_id": bool(env.get("HF_API_KEY_ID")),
            "hf_id_masked": mask(env.get("HF_API_KEY_ID")),
            "has_hf_secret": bool(env.get("HF_API_KEY_SECRET")),
            "hf_secret_masked": mask(env.get("HF_API_KEY_SECRET")),
            "has_vbee_app_id": bool(env.get("VBEE_APP_ID")),
            "vbee_app_id_masked": mask(env.get("VBEE_APP_ID")),
            "has_vbee_token": bool(env.get("VBEE_ACCESS_TOKEN")),
            "vbee_token_masked": mask(env.get("VBEE_ACCESS_TOKEN")),
            "has_pixabay": bool(env.get("PIXABAY_API_KEY")),
            "pixabay_masked": mask(env.get("PIXABAY_API_KEY")),
            "vbee_default_voice": env.get("VBEE_DEFAULT_VOICE", DEFAULT_VOICE_CODE),
            "brain_model": env.get("BRAIN_MODEL", "claude-opus-5-5"),
            "video_model": env.get("HF_VIDEO_MODEL", "higgsfield/cinema-studio/4.0"),
        })

    def post(self):
        try:
            body = json.loads(self.request.body.decode("utf-8"))
        except Exception:
            return self.write_json({"error": "Dữ liệu JSON không hợp lệ"}, 400)

        if not ENV_FILE.exists():
            ENV_FILE.touch()

        for key in ("ANTHROPIC_API_KEY", "HF_API_KEY_ID", "HF_API_KEY_SECRET",
                    "VBEE_APP_ID", "VBEE_ACCESS_TOKEN", "VBEE_DEFAULT_VOICE",
                    "BRAIN_MODEL", "HF_VIDEO_MODEL", "PIXABAY_API_KEY"):
            if key in body and body[key].strip() != "":
                set_key(str(ENV_FILE), key, body[key].strip())

        self.write_json({"status": "saved", "message": "Đã lưu cấu hình .env thành công!"})


class VoicesHandler(BaseHandler):
    """Lấy danh sách giọng đọc, danh sách file audio đã tạo và tạo voice trực tiếp."""
    def get(self):
        audios = []
        for f in sorted(OUTPUT_DIR.glob("*.mp3"), key=os.path.getmtime, reverse=True):
            size_kb = round(f.stat().st_size / 1024, 1)
            mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f.stat().st_mtime))
            audios.append({
                "name": f.name,
                "url": f"/output/{f.name}",
                "size_kb": size_kb,
                "mtime": mtime,
            })
        self.write_json({
            "voices": POPULAR_VOICES,
            "audios": audios,
        })

    def post(self):
        """Tạo một file giọng đọc (.mp3) ngay lập tức từ văn bản."""
        try:
            body = json.loads(self.request.body.decode("utf-8"))
            text = body.get("text", "").strip()
            voice_code = body.get("voice_code", DEFAULT_VOICE_CODE).strip()
            speed_rate = float(body.get("speed_rate", 1.0))
            demo_mode = bool(body.get("demo_mode", False))
            stem = re.sub(r"[^a-z0-9_-]", "", body.get("name", "").lower()) or f"voice_{int(time.time())}"
        except Exception as e:
            return self.write_json({"error": f"Dữ liệu không hợp lệ: {e}"}, 400)

        if not text:
            return self.write_json({"error": "Vui lòng nhập nội dung văn bản cần tạo giọng đọc!"}, 400)

        if demo_mode:
            ffmpeg_bin = get_bin("ffmpeg")
            out_file = OUTPUT_DIR / f"{stem}.mp3"
            subprocess.run(
                [ffmpeg_bin, "-y", "-f", "lavfi", "-i", "sine=frequency=480:duration=3",
                 "-c:a", "libmp3lame", "-q:a", "4", str(out_file)],
                capture_output=True, check=False
            )
            runner.log(f"🎙️ [Demo Voice] Đã tạo file giọng đọc mẫu: {out_file.name}")
            return self.write_json({
                "status": "success",
                "file": out_file.name,
                "url": f"/output/{out_file.name}",
                "message": f"Đã tạo giọng đọc mẫu {out_file.name} (Chế độ Demo)!",
            })

        env = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
        vbee_app_id = env.get("VBEE_APP_ID") or os.environ.get("VBEE_APP_ID", "")
        vbee_token = env.get("VBEE_ACCESS_TOKEN") or os.environ.get("VBEE_ACCESS_TOKEN", "")
        if not vbee_app_id or not vbee_token:
            return self.write_json({
                "error": "Chưa cấu hình VBEE_APP_ID hoặc VBEE_ACCESS_TOKEN trong mục API Keys! (Hoặc bật Chế độ Demo để thử)"
            }, 400)

        client = VbeeVoiceClient(vbee_app_id, vbee_token, default_voice=voice_code)
        try:
            runner.log(f"🎙️ Đang tạo giọng đọc Bee Voice AI ({voice_code})...")
            res = client.synthesize(
                text,
                OUTPUT_DIR,
                stem=stem,
                voice_code=voice_code,
                speed_rate=speed_rate,
                on_status=lambda s: runner.log(f"  [voice] {s}"),
            )
            runner.log(f"✅ Đã tạo xong giọng đọc: {res['file']}")
            self.write_json({
                "status": "success",
                "file": res["file"],
                "url": f"/output/{res['file']}",
                "message": f"Đã tạo thành công giọng đọc {res['file']}!",
            })
        except Exception as exc:
            runner.log(f"❌ Lỗi tạo giọng đọc: {exc}")
            self.write_json({"error": str(exc)}, 500)
        finally:
            client.close()


class MuxVoiceHandler(BaseHandler):
    """Ghép một file âm thanh (.mp3) vào một file video (.mp4) trong thư mục output."""
    def post(self):
        try:
            body = json.loads(self.request.body.decode("utf-8"))
            video_name = Path(body.get("video_file", "")).name
            audio_name = Path(body.get("audio_file", "")).name
            output_name = Path(body.get("output_file", "")).name or f"{Path(video_name).stem}_voiced.mp4"
            if not output_name.endswith(".mp4"):
                output_name += ".mp4"
        except Exception as e:
            return self.write_json({"error": f"Dữ liệu không hợp lệ: {e}"}, 400)

        video_path = (OUTPUT_DIR / video_name).resolve()
        audio_path = (OUTPUT_DIR / audio_name).resolve()
        out_path = (OUTPUT_DIR / output_name).resolve()

        try:
            mux_voice_into_video(video_path, audio_path, out_path)
            runner.log(f"🔊 Đã lồng tiếng {audio_name} vào {video_name} -> {out_path.name}")
            self.write_json({
                "status": "success",
                "filename": out_path.name,
                "url": f"/output/{out_path.name}",
                "message": f"Đã lồng tiếng thành công: {out_path.name}!",
            })
        except Exception as exc:
            self.write_json({"error": str(exc)}, 500)


class VideosHandler(BaseHandler):
    def get(self):
        files = []
        ffmpeg_bin = get_bin("ffmpeg")
        ffprobe_bin = get_bin("ffprobe")

        for f in sorted(OUTPUT_DIR.glob("*.mp4"), key=os.path.getmtime, reverse=True):
            size_mb = round(f.stat().st_size / (1024 * 1024), 2)
            mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f.stat().st_mtime))
            thumb_name = f"{f.stem}_thumb.jpg"
            thumb_path = OUTPUT_DIR / thumb_name

            # Tự động trích thumbnail nếu chưa có
            if not thumb_path.exists():
                try:
                    subprocess.run(
                        [ffmpeg_bin, "-y", "-ss", "00:00:01", "-i", str(f),
                         "-frames:v", "1", "-vf", "scale=480:-2", str(thumb_path)],
                        capture_output=True, timeout=10, check=False
                    )
                except Exception:
                    pass

            files.append({
                "name": f.name,
                "url": f"/output/{f.name}",
                "thumb_url": f"/output/{thumb_name}" if thumb_path.exists() else None,
                "size_mb": size_mb,
                "mtime": mtime,
            })

        self.write_json({"videos": files})

    def delete(self):
        filename = self.get_argument("name", "")
        if not filename or ".." in filename:
            return self.write_json({"error": "Tên file không hợp lệ"}, 400)
        target = OUTPUT_DIR / filename
        if target.exists() and target.is_file():
            target.unlink()
            # Xóa thumbnail nếu có
            thumb = OUTPUT_DIR / f"{target.stem}_thumb.jpg"
            if thumb.exists():
                thumb.unlink()
            self.write_json({"status": "deleted", "name": filename})
        else:
            self.write_json({"error": "Không tìm thấy file"}, 404)


class ConcatHandler(BaseHandler):
    """Ghép các shot thành video hoàn chỉnh bằng ffmpeg."""
    def post(self):
        try:
            body = json.loads(self.request.body.decode("utf-8"))
            shot_files: list[str] = body.get("shots", [])
            output_name: str = body.get("output_name", f"final_{int(time.time())}.mp4")
        except Exception:
            return self.write_json({"error": "Dữ liệu không hợp lệ"}, 400)

        if not shot_files or len(shot_files) < 2:
            return self.write_json({"error": "Vui lòng chọn ít nhất 2 video shot để ghép!"}, 400)

        ffmpeg_bin = get_bin("ffmpeg")
        concat_list_file = OUTPUT_DIR / f"concat_list_{int(time.time())}.txt"
        out_path = OUTPUT_DIR / output_name

        try:
            with open(concat_list_file, "w", encoding="utf-8") as f:
                for fname in shot_files:
                    fpath = (OUTPUT_DIR / fname).resolve()
                    if not fpath.exists():
                        return self.write_json({"error": f"Không tìm thấy file {fname}"}, 404)
                    f.write(f"file '{fpath.as_posix()}'\n")

            cmd = [
                ffmpeg_bin, "-y", "-f", "concat", "-safe", "0",
                "-i", str(concat_list_file), "-c", "copy", str(out_path)
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            concat_list_file.unlink(missing_ok=True)
            self.write_json({
                "status": "success",
                "filename": out_path.name,
                "url": f"/output/{out_path.name}",
                "message": f"Đã ghép thành công video {out_path.name}!"
            })
        except subprocess.CalledProcessError as exc:
            concat_list_file.unlink(missing_ok=True)
            return self.write_json({"error": f"Lỗi ffmpeg: {exc.stderr}"}, 500)
        except Exception as exc:
            concat_list_file.unlink(missing_ok=True)
            return self.write_json({"error": str(exc)}, 500)


class RunHandler(BaseHandler):
    def post(self):
        if runner.is_running:
            return self.write_json({"error": "Agent đang chạy một tác vụ khác. Vui lòng chờ!"}, 409)

        try:
            body = json.loads(self.request.body.decode("utf-8"))
            goal = body.get("goal", "").strip()
            budget = float(body.get("budget", 5.0))
            max_shots = int(body.get("max_shots", 12))
            max_attempts = int(body.get("max_attempts", 2))
            auto_approve = bool(body.get("auto_approve", False))
            aspect_ratio = body.get("aspect_ratio", "9:16")
            resolution = body.get("resolution", "720p")
            demo_mode = bool(body.get("demo_mode", False))
            ref_images = body.get("ref_images", [])
            image_urls = body.get("image_urls", [])
            enable_voice = bool(body.get("enable_voice", False))
            voice_code = body.get("voice_code", DEFAULT_VOICE_CODE)
            voice_speed = float(body.get("voice_speed", 1.0))
            voice_script = body.get("voice_script", "")
            production_mode = body.get("production_mode", "cinema")
            auto_broll = bool(body.get("auto_broll", True))
            broll_interval = float(body.get("broll_interval", 5.0))
            avatar_ai_motion = bool(body.get("avatar_ai_motion", True))
            avatar_ai_model = body.get("avatar_ai_model", "kling-video/v3.0-turbo/image-to-video")
            prefer_text_effects = bool(body.get("prefer_text_effects", True))
            speaker_overlays = bool(body.get("speaker_overlays", True))
            sfx_enabled = bool(body.get("sfx_enabled", True))
            sfx_volume = max(0.0, min(1.5, float(body.get("sfx_volume", 0.5))))
            transitions = bool(body.get("transitions", True))
            stock_footage = bool(body.get("stock_footage", True))
            subtitle_mode = "word" if body.get("subtitle_mode", "word") == "word" else "phrase"
        except Exception as e:
            return self.write_json({"error": f"Dữ liệu gửi lên sai định dạng: {e}"}, 400)

        if not goal:
            return self.write_json({"error": "Vui lòng nhập mô tả / kịch bản mục tiêu video!"}, 400)

        try:
            runner.start(
                goal=goal,
                budget_usd=budget,
                max_shots=max_shots,
                max_attempts=max_attempts,
                auto_approve=auto_approve,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                demo_mode=demo_mode,
                ref_images=ref_images,
                image_urls=image_urls,
                enable_voice=enable_voice,
                voice_code=voice_code,
                voice_speed=voice_speed,
                voice_script=voice_script,
                production_mode=production_mode,
                auto_broll=auto_broll,
                broll_interval=broll_interval,
                avatar_ai_motion=avatar_ai_motion,
                avatar_ai_model=avatar_ai_model,
                prefer_text_effects=prefer_text_effects,
                speaker_overlays=speaker_overlays,
                sfx_enabled=sfx_enabled,
                sfx_volume=sfx_volume,
                transitions=transitions,
                stock_footage=stock_footage,
                subtitle_mode=subtitle_mode,
            )
            self.write_json({"status": "started", "message": "Đã khởi động Agent thành công!"})
        except Exception as e:
            self.write_json({"error": str(e)}, 500)


class ApproveHandler(BaseHandler):
    def post(self):
        try:
            body = json.loads(self.request.body.decode("utf-8"))
            answer = body.get("answer", "n")
        except Exception:
            answer = "n"
        runner.resolve_approval(answer)
        self.write_json({"status": "ok", "answer": runner.approval_result})


class StatusHandler(BaseHandler):
    def get(self):
        spent = 0.0
        budget = 0.0
        if runner.current_agent:
            spent = float(runner.current_agent.budget.spent)
            budget = float(runner.current_agent.budget.max_usd)

        self.write_json({
            "is_running": runner.is_running,
            "has_pending_approval": runner.pending_approval is not None,
            "approval_prompt": runner.approval_prompt,
            "spent": spent,
            "budget": budget,
        })


class LogsHandler(BaseHandler):
    def get(self):
        self.write_json({
            "is_running": runner.is_running,
            "has_pending_approval": runner.pending_approval is not None,
            "approval_prompt": runner.approval_prompt,
            "logs": runner.log_history,
        })


class LiveWebSocketHandler(tornado.websocket.WebSocketHandler):
    def check_origin(self, origin):
        return True

    def open(self):
        runner.subscribers.add(self)
        # Gửi trạng thái ban đầu kèm lịch sử log gần nhất
        self.write_message(json.dumps({
            "type": "init",
            "is_running": runner.is_running,
            "has_pending_approval": runner.pending_approval is not None,
            "approval_prompt": runner.approval_prompt,
            "logs": runner.log_history,
        }))

    def on_close(self):
        runner.subscribers.discard(self)


def make_app():
    return tornado.web.Application(
        [
            (r"/", IndexHandler),
            (r"/api/env", EnvHandler),
            (r"/api/videos", VideosHandler),
            (r"/api/voices", VoicesHandler),
            (r"/api/voice/mux", MuxVoiceHandler),
            (r"/api/concat", ConcatHandler),
            (r"/api/run", RunHandler),
            (r"/api/approve", ApproveHandler),
            (r"/api/status", StatusHandler),
            (r"/api/logs", LogsHandler),
            (r"/ws", LiveWebSocketHandler),
            (
                r"/output/(.*)",
                tornado.web.StaticFileHandler,
                {"path": str(OUTPUT_DIR)},
            ),
        ],
        template_path=str(BASE_DIR),
        compiled_template_cache=False,
        debug=False,
    )


def main():
    port = int(os.environ.get("PORT", 8080))
    app = make_app()
    app.listen(port)
    runner.loop = tornado.ioloop.IOLoop.current()
    print("=" * 65)
    print(f"🚀 AI VIDEO AGENT DASHBOARD ĐANG CHẠY TẠI:")
    print(f"👉 http://localhost:{port}")
    print(f"👉 http://127.0.0.1:{port}")
    print("=" * 65)
    tornado.ioloop.IOLoop.current().start()


if __name__ == "__main__":
    main()

