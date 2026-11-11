"""Agent sản xuất video: Opus 5.5 điều phối, Haiku/Sonnet thực thi, Higgsfield sinh video.

Chạy:
    python agent.py "Làm video quảng cáo 15 giây cho trà thảo mộc, 9:16" --budget 5
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from avatar_director import build_cutaway_avatar_video
from higgsfield_client import GenerationFailed, HiggsfieldClient, HiggsfieldError
from voice_client import DEFAULT_VOICE_CODE, VbeeVoiceClient, mux_voice_into_video

BRAIN_MODEL = os.getenv("BRAIN_MODEL", "claude-opus-5-5")
WORKER_MODELS = {
    "haiku": os.getenv("HAIKU_MODEL", "claude-haiku-5-5"),
    "sonnet": os.getenv("SONNET_MODEL", "claude-sonnet-5-5"),
    "opus": os.getenv("BRAIN_MODEL", "claude-opus-5-5"),
}
HF_VIDEO_MODEL = os.getenv("HF_VIDEO_MODEL", "higgsfield/cinema-studio/4.0")

SYSTEM_PROMPT = """\
You are the director and orchestrator of an AI video production pipeline.

Tools:
- delegate_task: hand a self-contained job to a cheaper worker model
  (haiku = simple/high-volume, sonnet = needs better writing or reasoning).
- estimate_cost: price a shot before generating it.
- generate_video: render ONE shot with Higgsfield (costs real money, takes minutes).
- review_shot: look at 3 frames of a rendered shot to check quality.
- generate_voice: synthesize voiceover audio (.mp3) using Bee Voice AI (Vbee TTS).
- mux_voice_video: merge a generated voiceover audio file into a rendered video shot using FFmpeg.

Workflow:
1. Plan the film: shot list with duration, aspect ratio, consistent visual style, and voiceover script (if voiceover is requested).
2. Delegate prompt-writing for shots to workers IN PARALLEL (several delegate_task
   calls in one turn). Each brief must be fully self-contained: the worker sees nothing
   of this conversation. State goal, style bible, the exact shot, constraints, and demand
   machine-readable output (JSON).
3. Read every worker result critically. Fix or redo bad output; never pass it on blindly.
4. Generate shots (several generate_video calls in one turn when independent) and generate voiceover clips (generate_voice) if needed.
5. review_shot each result. Regenerate only shots that clearly fail, with an improved
   prompt. Attempts per shot and total budget are capped by the system.
6. If voiceover audio was generated, use mux_voice_video to combine the audio and video into final voiced MP4 files.
7. Finish with a short summary: the list of files and any shot you are unhappy with.

Rules:
- Stay within the budget; the system refuses over-budget calls. Prefer fewer, shorter
  shots and 720p unless told otherwise.
- Higgsfield outputs expire after 7 days; files are already downloaded for you.
- Reply to the user in the language they wrote in.
"""

WORKER_SYSTEM = (
    "You are a precise worker model. Do exactly the task you are given, using only the "
    "context provided. Follow the requested output format strictly. No preamble, no "
    "commentary outside the requested format."
)

_VIDEO_PROPS: dict[str, Any] = {
    "prompt": {"type": "string", "description": "Detailed shot description: subject, action, camera, lighting, style."},
    "duration": {"type": "integer", "minimum": 4, "maximum": 30, "description": "Seconds."},
    "aspect_ratio": {"type": "string", "enum": ["16:9", "4:3", "1:1", "3:4", "9:16", "21:9"]},
    "resolution": {"type": "string", "enum": ["480p", "720p"]},
    "generate_audio": {"type": "boolean"},
    "image_urls": {"type": "array", "items": {"type": "string"}, "maxItems": 30,
                   "description": "Optional public reference image URLs."},
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "delegate_task",
        "description": "Run a self-contained task on a worker model and return its text output.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tier": {"type": "string", "enum": ["haiku", "sonnet"]},
                "task": {"type": "string"},
                "context": {"type": "string", "description": "Everything the worker needs; it sees nothing else."},
                "output_format": {"type": "string", "description": "Exact format required, e.g. a JSON schema."},
            },
            "required": ["tier", "task", "context", "output_format"],
        },
    },
    {
        "name": "estimate_cost",
        "description": "Estimate the USD cost of a video shot without generating it.",
        "input_schema": {"type": "object", "properties": _VIDEO_PROPS, "required": ["prompt"]},
    },
    {
        "name": "generate_video",
        "description": "Generate one video shot with Higgsfield and download it. Costs money.",
        "input_schema": {
            "type": "object",
            "properties": {
                "shot_id": {"type": "string", "pattern": "^[a-z0-9_-]{1,40}$",
                            "description": "Stable id for the shot, e.g. s01."},
                **_VIDEO_PROPS,
            },
            "required": ["shot_id", "prompt"],
        },
    },
    {
        "name": "review_shot",
        "description": "Return 3 frames (10%, 50%, 90%) of a generated shot for visual QA.",
        "input_schema": {
            "type": "object",
            "properties": {"file": {"type": "string", "description": "File name returned by generate_video."}},
            "required": ["file"],
        },
    },
    {
        "name": "generate_voice",
        "description": "Synthesize voiceover audio (.mp3) from text using Bee Voice AI (Vbee TTS).",
        "input_schema": {
            "type": "object",
            "properties": {
                "voice_id": {"type": "string", "pattern": "^[a-z0-9_-]{1,40}$",
                             "description": "Stable id for the voice clip, e.g. vo_s01 or vo_full."},
                "text": {"type": "string", "description": "Script text to speak in the voiceover."},
                "voice_code": {"type": "string", "description": "Vbee voice code, e.g. hn_female_ngochuyen_full_48k-fhg."},
                "speed_rate": {"type": "number", "minimum": 0.5, "maximum": 1.9, "description": "Speech speed (default 1.0)."},
            },
            "required": ["voice_id", "text"],
        },
    },
    {
        "name": "mux_voice_video",
        "description": "Combine a generated voiceover audio file (.mp3) with a video file (.mp4) using FFmpeg.",
        "input_schema": {
            "type": "object",
            "properties": {
                "video_file": {"type": "string", "description": "Video file name in output folder, e.g. s01_a1.mp4."},
                "audio_file": {"type": "string", "description": "Audio file name in output folder, e.g. vo_s01.mp3."},
                "output_file": {"type": "string", "description": "Output voiced video file name, e.g. s01_voiced.mp4."},
            },
            "required": ["video_file", "audio_file", "output_file"],
        },
    },
    {
        "name": "create_cutaway_avatar",
        "description": "Create an ultra-low-cost Talking Avatar video with auto B-roll cutaway interleaving (Ken Burns motion, continuous voiceover audio).",
        "input_schema": {
            "type": "object",
            "properties": {
                "avatar_image": {"type": "string", "description": "Avatar character portrait image file name or path."},
                "broll_images": {"type": "array", "items": {"type": "string"}, "description": "Optional list of B-roll image paths to interleave."},
                "audio_file": {"type": "string", "description": "Generated voiceover audio file name (.mp3) in output dir."},
                "output_file": {"type": "string", "description": "Output video file name (.mp4)."},
                "aspect_ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1", "4:3", "21:9"], "description": "Aspect ratio (default 9:16)."},
                "interval": {"type": "number", "description": "Cutaway rhythm interval in seconds (default 5.0)."},
            },
            "required": ["avatar_image", "audio_file", "output_file"],
        },
    },
]


class BudgetRefused(Exception):
    pass


@dataclass
class Budget:
    max_usd: Decimal
    max_attempts_per_shot: int = 2
    max_shots: int = 12
    fallback_cost: Decimal = Decimal("1.00")  # dùng khi không ước tính được giá
    spent: Decimal = Decimal("0")
    attempts: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reserve(self, shot_id: str, cost: Decimal) -> int:
        with self._lock:
            used = self.attempts.get(shot_id, 0)
            if used >= self.max_attempts_per_shot:
                raise BudgetRefused(f"Shot {shot_id} đã dùng hết {self.max_attempts_per_shot} lần thử.")
            if shot_id not in self.attempts and len(self.attempts) >= self.max_shots:
                raise BudgetRefused(f"Đã đạt giới hạn {self.max_shots} shot.")
            if self.spent + cost > self.max_usd:
                raise BudgetRefused(
                    f"Vượt ngân sách: còn ${self.max_usd - self.spent:.3f}, shot cần ~${cost:.3f}.")
            self.spent += cost
            self.attempts[shot_id] = used + 1
            return used + 1

    def refund(self, cost: Decimal, *, shot_id: str | None = None) -> None:
        """Hoàn tiền (request failed/nsfw không bị tính phí). Truyền shot_id để trả cả lượt thử."""
        with self._lock:
            self.spent -= cost
            if shot_id is not None:
                self.attempts[shot_id] = max(0, self.attempts.get(shot_id, 1) - 1)

    @property
    def left(self) -> Decimal:
        return self.max_usd - self.spent


def _text(blocks: Any) -> str:
    return "".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text")


def _ok(tool_id: str, content: Any) -> dict[str, Any]:
    if not isinstance(content, (str, list)):
        content = json.dumps(content, ensure_ascii=False)
    return {"type": "tool_result", "tool_use_id": tool_id, "content": content}


def _err(tool_id: str, message: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": message, "is_error": True}


def extract_frames(path: Path) -> list[str]:
    """3 khung hình (base64 JPEG) ở 10%, 50%, 90% thời lượng. Cần ffmpeg/ffprobe."""
    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.strip())
    frames = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, frac in enumerate((0.1, 0.5, 0.9)):
            out = Path(tmp) / f"f{i}.jpg"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-ss", f"{dur * frac:.2f}", "-i", str(path),
                 "-frames:v", "1", "-vf", "scale=640:-2", str(out)],
                check=True, capture_output=True)
            frames.append(base64.b64encode(out.read_bytes()).decode())
    return frames


class VideoAgent:
    def __init__(self, llm: Any, hf: HiggsfieldClient, budget: Budget, out_dir: Path, *,
                 voice: VbeeVoiceClient | None = None,
                 auto_approve: bool = False, ask: Callable[[str], str] = input,
                 out: Callable[[str], None] = print, hf_concurrency: int = 2,
                 brain: str = BRAIN_MODEL):
        self.llm, self.hf, self.budget = llm, hf, budget
        self.voice = voice
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.auto_approve, self.ask, self.out, self.brain = auto_approve, ask, out, brain
        self._hf_slots = threading.Semaphore(hf_concurrency)
        self.worker_calls = 0

    # ---- công cụ ------------------------------------------------------
    @staticmethod
    def _video_params(inp: dict[str, Any]) -> dict[str, Any]:
        keys = ("prompt", "duration", "aspect_ratio", "resolution", "generate_audio", "image_urls")
        return {k: inp[k] for k in keys if k in inp}

    def _delegate(self, inp: dict[str, Any]) -> str:
        model = WORKER_MODELS[inp["tier"]]
        self.worker_calls += 1
        resp = self.llm.messages.create(
            model=model, max_tokens=4000, system=WORKER_SYSTEM,
            messages=[{"role": "user", "content": (
                f"TASK:\n{inp['task']}\n\nCONTEXT:\n{inp['context']}\n\n"
                f"OUTPUT FORMAT:\n{inp['output_format']}")}])
        return _text(resp.content)

    def _estimate(self, inp: dict[str, Any]) -> dict[str, Any]:
        est = self.hf.estimate(HF_VIDEO_MODEL, self._video_params(inp))
        return {"usd": None if est is None else str(est),
                "budget_left_usd": str(self.budget.left)}

    def _generate(self, inp: dict[str, Any], cost: Decimal, attempt: int) -> dict[str, Any]:
        shot = inp["shot_id"]
        try:
            with self._hf_slots:  # giới hạn số request chạy song song theo concurrency của tài khoản
                res = self.hf.generate(HF_VIDEO_MODEL, self._video_params(inp), self.out_dir,
                                       stem=f"{shot}_a{attempt}",
                                       on_status=lambda s: self.out(f"  [{shot}] {s}"))
        except GenerationFailed:
            self.budget.refund(cost)  # failed/nsfw không tính phí; vẫn giữ lượt thử để chặn vòng lặp
            raise
        except HiggsfieldError:
            self.budget.refund(cost)
            raise
        return {"shot_id": shot, "attempt": attempt, "file": Path(res["files"][0]).name,
                "expires_at": res["expires_at"], "est_cost_usd": str(cost),
                "budget_left_usd": str(self.budget.left)}

    def _review(self, inp: dict[str, Any]) -> list[dict[str, Any]]:
        path = (self.out_dir / Path(inp["file"]).name).resolve()
        if path.parent != self.out_dir.resolve() or not path.is_file():
            raise FileNotFoundError(f"Không thấy file {inp['file']} trong thư mục output")
        try:
            frames = extract_frames(path)
        except FileNotFoundError:
            raise RuntimeError("Chưa cài ffmpeg/ffprobe nên không xem được khung hình")
        blocks: list[dict[str, Any]] = [{"type": "text", "text": f"3 khung hình của {path.name} (10%, 50%, 90%)."}]
        blocks += [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": f}}
                   for f in frames]
        return blocks

    def _generate_voice(self, inp: dict[str, Any]) -> dict[str, Any]:
        if self.voice is None:
            raise RuntimeError("Chưa cấu hình VbeeVoiceClient (VBEE_APP_ID / VBEE_ACCESS_TOKEN).")
        voice_id = inp["voice_id"]
        if not re.fullmatch(r"[a-z0-9_-]{1,40}", voice_id):
            raise ValueError("voice_id chỉ gồm a-z 0-9 _ - (tối đa 40 ký tự)")
        text = inp["text"]
        voice_code = inp.get("voice_code")
        speed_rate = float(inp.get("speed_rate", 1.0))
        res = self.voice.synthesize(
            text,
            self.out_dir,
            stem=voice_id,
            voice_code=voice_code,
            speed_rate=speed_rate,
            on_status=lambda s: self.out(f"  [voice:{voice_id}] {s}"),
        )
        return {
            "voice_id": voice_id,
            "file": res["file"],
            "voice_code": res["voice_code"],
            "characters": res["characters"],
        }

    def _mux_voice_video(self, inp: dict[str, Any]) -> dict[str, Any]:
        video_path = (self.out_dir / Path(inp["video_file"]).name).resolve()
        audio_path = (self.out_dir / Path(inp["audio_file"]).name).resolve()
        out_name = Path(inp["output_file"]).name
        if not out_name.endswith(".mp4"):
            out_name += ".mp4"
        out_path = (self.out_dir / out_name).resolve()
        if (video_path.parent != self.out_dir.resolve()
                or audio_path.parent != self.out_dir.resolve()
                or out_path.parent != self.out_dir.resolve()):
            raise ValueError("Đường dẫn file không hợp lệ (chỉ cho phép trong thư mục output).")
        mux_voice_into_video(video_path, audio_path, out_path)
        self.out(f"  [mux] Đã ghép giọng đọc {audio_path.name} + {video_path.name} -> {out_path.name}")
        return {"output_file": out_path.name, "video_file": video_path.name, "audio_file": audio_path.name}

    def _create_cutaway_avatar(self, inp: dict[str, Any]) -> dict[str, Any]:
        avatar_img = Path(inp["avatar_image"]).resolve()
        audio_path = (self.out_dir / Path(inp["audio_file"]).name).resolve()
        out_name = Path(inp["output_file"]).name
        if not out_name.endswith(".mp4"):
            out_name += ".mp4"
        out_path = (self.out_dir / out_name).resolve()
        brolls = [Path(p).resolve() for p in inp.get("broll_images", []) if Path(p).exists()]
        aspect_ratio = inp.get("aspect_ratio", "9:16")
        interval = float(inp.get("interval", 5.0))

        build_cutaway_avatar_video(
            avatar_img=avatar_img,
            broll_images=brolls,
            audio_file=audio_path,
            out_path=out_path,
            aspect_ratio=aspect_ratio,
            interval=interval,
            on_status=lambda s: self.out(f"  [avatar-cutaway] {s}"),
        )
        return {"output_file": out_path.name, "audio_file": audio_path.name, "segments_interleaved": True}

    # ---- điều phối tool call -------------------------------------------
    def _approve(self, gen_blocks: list[Any], results: dict[str, dict]) -> list[tuple[Any, Decimal, int]]:
        pending: list[tuple[Any, Decimal, int, bool]] = []
        for b in gen_blocks:
            try:
                shot = b.input["shot_id"]
                if not re.fullmatch(r"[a-z0-9_-]{1,40}", shot):
                    raise ValueError("shot_id chỉ gồm a-z 0-9 _ - (tối đa 40 ký tự)")
                est = self.hf.estimate(HF_VIDEO_MODEL, self._video_params(b.input))
                cost = est if est is not None else self.budget.fallback_cost
                attempt = self.budget.reserve(shot, cost)
                pending.append((b, cost, attempt, est is None))
            except (BudgetRefused, KeyError, ValueError) as exc:
                results[b.id] = _err(b.id, f"Bị từ chối: {exc}")
        if pending and not self.auto_approve:
            lines = [f"  - {b.input['shot_id']}: ~${cost:.3f}{' (giá giả định, không ước tính được)' if guess else ''}"
                     for b, cost, _, guess in pending]
            total = sum(c for _, c, _, _ in pending)
            answer = self.ask("\nAgent muốn render:\n" + "\n".join(lines) +
                              f"\nTổng ~${total:.3f}, ngân sách còn ${self.budget.left:.3f}. Chạy? [y/N] ")
            if answer.strip().lower() not in ("y", "yes", "c", "co", "có"):
                for b, cost, _, _ in pending:
                    self.budget.refund(cost, shot_id=b.input["shot_id"])
                    results[b.id] = _err(b.id, "Người dùng từ chối render các shot này. Hỏi họ muốn chỉnh gì.")
                pending = []
        elif pending and self.auto_approve:
            total = sum(c for _, c, _, _ in pending)
            self.out(f"⚡ [Auto-Approve] Bật Tự Động Duyệt: Tự động phê duyệt render {len(pending)} shot (Tổng ~${total:.3f}). Đang bắt đầu render...")
        return [(b, cost, attempt) for b, cost, attempt, _ in pending]

    def dispatch(self, blocks: list[Any]) -> list[dict[str, Any]]:
        results: dict[str, dict] = {}
        approved = self._approve([b for b in blocks if b.name == "generate_video"], results)
        jobs: list[tuple[Any, Callable[[], Any]]] = []
        handlers = {
            "delegate_task": self._delegate,
            "estimate_cost": self._estimate,
            "review_shot": self._review,
            "generate_voice": self._generate_voice,
            "mux_voice_video": self._mux_voice_video,
            "create_cutaway_avatar": self._create_cutaway_avatar,
        }
        for b in blocks:
            if b.name == "generate_video":
                continue
            if b.name in handlers:
                jobs.append((b, lambda b=b: handlers[b.name](b.input)))
            else:
                results[b.id] = _err(b.id, f"Tool không tồn tại: {b.name}")
        for b, cost, attempt in approved:
            jobs.append((b, lambda b=b, c=cost, a=attempt: self._generate(b.input, c, a)))
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [(b, pool.submit(fn)) for b, fn in jobs]
            for b, fut in futures:
                try:
                    results[b.id] = _ok(b.id, fut.result())
                except Exception as exc:  # trả lỗi cho Opus để nó tự xử lý
                    results[b.id] = _err(b.id, f"{type(exc).__name__}: {exc}")
        return [results[b.id] for b in blocks]

    # ---- vòng lặp chính -------------------------------------------------
    def run(self, goal: str, max_turns: int = 30) -> str:
        messages: list[dict[str, Any]] = [{"role": "user", "content": goal}]
        for turn in range(1, max_turns + 1):
            resp = self.llm.messages.create(model=self.brain, max_tokens=8000,
                                            system=SYSTEM_PROMPT, tools=TOOLS, messages=messages)
            messages.append({"role": "assistant", "content": resp.content})
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason != "tool_use" or not tool_uses:
                return _text(resp.content)
            self.out(f"[vòng {turn}] " + ", ".join(b.name for b in tool_uses))
            messages.append({"role": "user", "content": self.dispatch(tool_uses)})
        return f"Dừng: đã đạt tối đa {max_turns} vòng. Đã chi ~${self.budget.spent:.3f}."


def main() -> None:
    import anthropic
    from dotenv import load_dotenv

    load_dotenv()
    ap = argparse.ArgumentParser(description="Agent sản xuất video (Opus điều phối + worker + Higgsfield)")
    ap.add_argument("goal", help="Mục tiêu, ví dụ: 'Video quảng cáo 15s cho trà thảo mộc, 9:16'")
    ap.add_argument("--budget", type=Decimal, default=Decimal("5"), help="Ngân sách tối đa (USD)")
    ap.add_argument("--out", type=Path, default=Path("output"))
    ap.add_argument("--max-shots", type=int, default=12)
    ap.add_argument("--max-attempts", type=int, default=2, help="Số lần thử tối đa mỗi shot")
    ap.add_argument("--yes", action="store_true", help="Không hỏi xác nhận trước khi render")
    args = ap.parse_args()

    hf = HiggsfieldClient(os.environ.get("HF_API_KEY_ID", ""), os.environ.get("HF_API_KEY_SECRET", ""))
    vbee_app_id = os.environ.get("VBEE_APP_ID", "")
    vbee_token = os.environ.get("VBEE_ACCESS_TOKEN", "")
    voice = (
        VbeeVoiceClient(
            vbee_app_id,
            vbee_token,
            default_voice=os.environ.get("VBEE_DEFAULT_VOICE", DEFAULT_VOICE_CODE),
        )
        if vbee_app_id and vbee_token
        else None
    )
    agent = VideoAgent(anthropic.Anthropic(), hf,
                       Budget(args.budget, args.max_attempts, args.max_shots),
                       args.out, voice=voice, auto_approve=args.yes)
    try:
        print(agent.run(args.goal))
    finally:
        hf.close()
        if voice:
            voice.close()
        print(f"\nĐã chi ước tính ${agent.budget.spent:.3f}/{agent.budget.max_usd}; "
              f"{agent.worker_calls} lượt gọi worker.")


if __name__ == "__main__":
    main()
