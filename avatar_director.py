"""Module đạo diễn và dựng phim Talking Avatar & Auto B-Roll Cutaway (Xen kẽ cắt cảnh thông minh).
Tối ưu chi phí sản xuất video dài (1-2 phút) giảm hơn 90% chi phí:
- Giữ âm thanh thuyết minh (Bee Voice AI) liền mạch liên tục (continuous audio).
- Xen kẽ hình ảnh người nói (Talking Head) đa góc quay (Toàn cảnh / Cận cảnh) với các cảnh B-Roll minh hoạ (zoom/pan điện ảnh Ken Burns).
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from sfx import default_master, render_sfx_track
from speaker_overlay import _fallback_overlay, apply_speaker_overlay
from stock_footage import render_stock_clip
from text_effects import render_text_effect_clip
from word_timing import words_in_window
from shot_planner import uniform_shots

# Chuyển cảnh: người nói -> minh hoạ và minh hoạ -> người nói (xoay vòng cho đa dạng)
XFADE_DUR = 0.35
_XF_TO_CUTAWAY = ["smoothup", "circleopen", "slideleft", "zoomin", "radial", "smoothleft"]
_XF_TO_SPEAKER = ["smoothdown", "fade", "slideright", "circleclose", "hblur", "smoothright"]
_XF_VISUAL = ["slideleft", "smoothleft", "slideup"]


def get_ffmpeg_bin(name: str = "ffmpeg") -> str:
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


def get_media_duration(file_path: Path) -> float:
    """Lấy thời lượng chính xác của file media (audio/video) bằng ffprobe."""
    ffprobe_bin = get_ffmpeg_bin("ffprobe")
    try:
        cmd = [
            ffprobe_bin,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(file_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(res.stdout.strip())
    except Exception:
        return 6.0


def calculate_dimensions(aspect_ratio: str, resolution: str) -> tuple[int, int]:
    """Tính toán chiều rộng và chiều cao phù hợp (luôn chẵn)."""
    is_480p = (resolution == "480p")
    if aspect_ratio == "9:16":
        w, h = (480, 854) if is_480p else (720, 1280)
    elif aspect_ratio == "1:1":
        w, h = (480, 480) if is_480p else (720, 720)
    elif aspect_ratio == "4:3":
        w, h = (640, 480) if is_480p else (960, 720)
    elif aspect_ratio == "21:9":
        w, h = (854, 360) if is_480p else (1280, 548)
    else:  # 16:9 mặc định
        w, h = (854, 480) if is_480p else (1280, 720)

    # Đảm bảo chia hết cho 2 cho chuẩn codec H.264
    return (w // 2) * 2, (h // 2) * 2


def build_cutaway_avatar_video(
    avatar_img: Path,
    broll_images: list[Path] | None,
    audio_file: Path,
    out_path: Path,
    total_dur: Optional[float] = None,
    aspect_ratio: str = "9:16",
    resolution: str = "720p",
    interval: float = 5.0,
    text_cards: list[dict[str, str]] | None = None,
    prefer_text_effects: bool = True,
    speaker_overlays: list[dict] | None = None,
    transitions: bool = True,
    sfx: bool = True,
    sfx_volume: Optional[float] = None,
    stock_clips: dict[int, dict] | None = None,
    word_timeline: list[dict] | None = None,
    subtitle_mode: str = "word",
    shots: list[dict] | None = None,
    on_status: Optional[Callable[[str], None]] = None,
) -> Path:
    """Tự động dựng video xen kẽ nhân vật chính, B-roll và Text Effect kinetic typography đồng bộ giọng đọc."""
    ffmpeg_bin = get_ffmpeg_bin("ffmpeg")
    brolls = [p for p in (broll_images or []) if p.exists()]

    if total_dur is None or total_dur <= 0:
        total_dur = get_media_duration(audio_file)

    w, h = calculate_dimensions(aspect_ratio, resolution)
    fps = 30
    # Danh sách shot: theo nội dung (shot_planner) hoặc chia đều xen kẽ như cũ
    if not shots:
        shots = uniform_shots(total_dur, interval)
    num_segments = len(shots)
    n_vis = sum(1 for s_ in shots if s_["type"] == "visual")

    if on_status:
        broll_info = []
        if brolls:
            broll_info.append(f"{len(brolls)} ảnh B-Roll")
        if text_cards:
            broll_info.append(f"{len(text_cards) - len(stock_clips or {})} Text Effect kinetic")
        if stock_clips:
            broll_info.append(f"{len(stock_clips)} cảnh quay Pixabay")
        if not broll_info:
            broll_info.append("Đổi góc quay đa camera nhân vật")
        on_status(
            f"🎬 Cắt cảnh theo nội dung: {num_segments} shot ({num_segments - n_vis} người nói, {n_vis} minh hoạ) | "
            f"Tỷ lệ {aspect_ratio} ({w}x{h}) | {' + '.join(broll_info)}"
        )

    temp_dir = out_path.parent / f"_cutaway_tmp_{int(time.time() * 1000)}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Overlay (text/icon trên + phụ đề dưới) cho cảnh người nói, tra theo số thứ tự shot
    overlay_map = {o.get("segment_index"): o for o in (speaker_overlays or []) if o.get("segment_index") is not None}

    use_xf = transitions and num_segments > 1
    # Thời lượng chuyển cảnh vào shot k: người nói -> người nói (đổi góc máy) là cắt nhanh, còn lại xfade bình thường
    xd = [0.0] + [(0.12 if shots[k - 1]["type"] == shots[k]["type"] == "speaker" else XFADE_DUR)
                  for k in range(1, num_segments)]
    sfx_events: list[tuple[float, str, float]] = []

    try:
        seg_files: list[Path] = []
        broll_cursor = 0
        text_card_cursor = 0
        speaker_shot_count = 0

        vis_idx = -1
        for i in range(num_segments):
            seg_file = temp_dir / f"seg_{i:03d}.mp4"
            seg_start = shots[i]["start"]
            shot_dur = shots[i]["end"] - shots[i]["start"]
            # Mỗi shot (trừ shot cuối) dài thêm đúng thời lượng chuyển cảnh để chồng lên shot sau
            seg_d = shot_dur + (xd[i + 1] if use_xf and i < num_segments - 1 else 0.0)
            frames_per_seg = max(30, int(seg_d * fps))
            clip_events: list = []
            # Phụ đề: các từ giọng đọc nói trong đúng khung thời gian của shot này (giây tương đối)
            seg_words = words_in_window(word_timeline, seg_start, seg_start + seg_d) if word_timeline else None
            sub_extra = {"_words": seg_words, "_sub_mode": subtitle_mode}

            is_visual = shots[i]["type"] == "visual"
            if is_visual:
                vis_idx += 1
                # Không có gì để minh hoạ -> giữ người nói
                is_visual = bool(text_cards) or bool(brolls) or vis_idx in (stock_clips or {})
            # CẢNH MINH HOẠ (ý chính) - KHÔNG CÓ NGƯỜI NÓI
            if is_visual:
                # Cảnh quay thật từ Pixabay (nếu đã tải cho cảnh này) + thẻ chữ & phụ đề chồng lên
                stock = (stock_clips or {}).get(vis_idx)
                if stock and Path(stock["path"]).exists():
                    card = text_cards[vis_idx] if text_cards and vis_idx < len(text_cards) else {}
                    if on_status:
                        kind_vi = "ảnh" if stock.get("kind") == "image" else "cảnh quay"
                        on_status(f"  🎥 [{i+1}/{num_segments}] Render {kind_vi} Pixabay \"{stock.get('query', '')}\" ({shot_dur:.1f}s)...")
                    raw_file = temp_dir / f"seg_{i:03d}_stock.mp4"
                    try:
                        render_stock_clip(Path(stock["path"]), raw_file, w, h, seg_d, fps,
                                          ffmpeg_bin=ffmpeg_bin, ffprobe_bin=get_ffmpeg_bin("ffprobe"), variant=vis_idx,
                                          kind=stock.get("kind"))
                        sub = str(card.get("sub_text") or "")
                        ov = _fallback_overlay({"spoken_excerpt": sub, "segment_index": i}, sub)
                        props = card.get("props") or {}
                        head = (card.get("headline") or props.get("text") or "").strip()
                        if head:
                            ov["text"] = " ".join(head.split()[:10])
                            ov["keyword"] = card.get("highlight_text", "") if card.get("highlight_text", "") in head else ""
                        ov["_style"] = vis_idx
                        ov.update(sub_extra)
                        apply_speaker_overlay(
                            raw_file, seg_file, ov, w, h, fps, seg_d,
                            progress_start=seg_start / total_dur,
                            progress_end=(seg_start + seg_d) / total_dur,
                            sfx_events=clip_events,
                        )
                        sfx_events.extend((at + seg_start, n, g) for at, n, g in clip_events)
                        seg_files.append(seg_file)
                        text_card_cursor += 1
                        continue
                    except Exception as e:
                        clip_events.clear()
                        if on_status:
                            on_status(f"     ⚠️ Clip Pixabay lỗi ({str(e)[:120]}), dùng cảnh đồ hoạ thay thế")

                # Ưu tiên tạo Video dạng Text Effect nếu có text_cards
                use_text_card = bool(text_cards) and (prefer_text_effects or not brolls or (broll_cursor % 2 == 1))
                if use_text_card and text_cards:
                    cutaway_slot_idx = vis_idx
                    if cutaway_slot_idx < len(text_cards):
                        card = text_cards[cutaway_slot_idx]
                    else:
                        card = text_cards[text_card_cursor % len(text_cards)]
                    text_card_cursor += 1
                    card = {**card, **sub_extra}
                    head_clean = (card.get("headline") or card.get("sub_text", "")[:40]).replace("\n", " ")
                    card_template = card.get("template", "")
                    tpl_display_names = {
                        "calculator": "Máy tính",
                        "receipt": "Hoá đơn",
                        "dots": "Lưới 100 chấm",
                        "result": "Phép tính + kết quả",
                        "chart": "Biểu đồ đường",
                        "compare": "Bảng so sánh",
                        "bignumber": "Con số lớn",
                        "statement": "Câu nhấn",
                        "climax": "Cảnh kết nền tối",
                    }
                    display_tpl = tpl_display_names.get(card_template, f"Template #{cutaway_slot_idx + 1}")
                    if on_status:
                        on_status(f"  ✨ [{i+1}/{num_segments}] Render cảnh minh hoạ [{display_tpl}]: \"{head_clean}\" ({shot_dur:.1f}s)...")
                    render_text_effect_clip(
                        card=card,
                        w=w,
                        h=h,
                        duration=seg_d,
                        out_clip_path=seg_file,
                        fps=fps,
                        scene_style=cutaway_slot_idx,
                        progress_start=seg_start / total_dur,
                        progress_end=(seg_start + seg_d) / total_dur,
                        is_final=(i == num_segments - 1),
                        sfx_events=clip_events,
                    )
                    sfx_events.extend((at + seg_start, n, g) for at, n, g in clip_events)
                    seg_files.append(seg_file)
                    continue

                if brolls:
                    curr_img = brolls[broll_cursor % len(brolls)]
                    broll_num = (broll_cursor % len(brolls)) + 1
                    broll_cursor += 1

                    # Chuyển động B-Roll luân phiên: Zoom In / Zoom Out
                    mode_broll = (broll_num - 1) % 2
                    if mode_broll == 0:
                        vf = (
                            f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                            f"zoompan=z='min(zoom+0.0014,1.22)':d={frames_per_seg}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps},"
                            f"format=yuv420p"
                        )
                        shot_title = f"B-Roll {broll_num} (Slow Zoom In)"
                    else:
                        vf = (
                            f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                            f"zoompan=z='if(lte(zoom,1.0),1.20,max(1.0,zoom-0.0014))':d={frames_per_seg}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps},"
                            f"format=yuv420p"
                        )
                        shot_title = f"B-Roll {broll_num} (Slow Zoom Out)"

                    if on_status:
                        on_status(f"  🎞️ [{i+1}/{num_segments}] Render {shot_title} ({shot_dur:.1f}s)...")

                    cmd = [
                        ffmpeg_bin, "-y",
                        "-loop", "1", "-i", str(curr_img),
                        "-t", f"{seg_d:.3f}",
                        "-vf", vf,
                        "-an",  # Triệt tiêu audio
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps),
                        str(seg_file),
                    ]
                    subprocess.run(cmd, capture_output=True, check=True)
                    seg_files.append(seg_file)
                    continue
            # NGƯỜI NÓI (hoặc shot minh hoạ không dựng được -> quay về người nói)
            if True:
                curr_img = avatar_img
                is_video_source = str(curr_img).lower().endswith((".mp4", ".mov", ".mkv", ".webm"))
                # Góc quay nhân vật chính: Camera 1 (Toàn cảnh/vừa) vs Camera 2 (Cận cảnh/focus mặt)
                speaker_angle = speaker_shot_count % 2
                speaker_shot_count += 1

                if is_video_source:
                    # Nguồn là video AI cử động thật từ Higgsfield
                    if speaker_angle == 0:
                        vf = (
                            f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                            f"format=yuv420p"
                        )
                        shot_title = "Nhân vật AI cử động (Camera 1 - Toàn cảnh vừa)"
                    else:
                        vf = (
                            f"scale={int(w*1.25)}:{int(h*1.25)}:force_original_aspect_ratio=increase,crop={w}:{h},"
                            f"format=yuv420p"
                        )
                        shot_title = "Nhân vật AI cử động (Camera 2 - Cận cảnh)"
                else:
                    # Nguồn là ảnh tĩnh -> áp dụng hiệu ứng camera thở & zoom điện ảnh
                    if speaker_angle == 0:
                        vf = (
                            f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                            f"zoompan=z='min(zoom+0.0006,1.06)':d={frames_per_seg}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps},"
                            f"format=yuv420p"
                        )
                        shot_title = "Nhân vật (Camera 1 - Toàn cảnh vừa)"
                    else:
                        vf = (
                            f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                            f"zoompan=z='min(zoom+0.0008,1.20)':d={frames_per_seg}:x='iw/2-(iw/zoom/2)':y='(ih/2-(ih/zoom/2))*0.8':s={w}x{h}:fps={fps},"
                            f"format=yuv420p"
                        )
                        shot_title = "Nhân vật (Camera 2 - Cận cảnh)"

            if on_status:
                on_status(f"  🎞️ [{i+1}/{num_segments}] Render {shot_title} ({shot_dur:.1f}s)..."
                          + (f" \"{shots[i]['text'][:50]}\"" if shots[i].get("text") else ""))

            is_video = str(curr_img).lower().endswith((".mp4", ".mov", ".mkv", ".webm"))
            if is_video:
                # Đổi mốc bắt đầu giữa các lần xuất hiện để nhân vật không lặp lại đúng 1 cử chỉ
                seek_offset = ((speaker_shot_count - 1) * 1.6) % 4.5
                input_args = ["-ss", f"{seek_offset:.2f}", "-stream_loop", "-1", "-i", str(curr_img)]
            else:
                input_args = ["-loop", "1", "-i", str(curr_img)]

            cmd = [
                ffmpeg_bin, "-y",
                *input_args,
                "-t", f"{seg_d:.3f}",
                "-vf", vf,
                "-an",  # BỎ HOÀN TOÀN AUDIO CỦA VIDEO 5S ĐỂ TRÁNH GHI ĐÈ VOICE CHÍNH
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps),
                str(seg_file),
            ]
            subprocess.run(cmd, capture_output=True, check=True)

            ov = overlay_map.get(i)
            if ov:
                if on_status:
                    on_status(f"     💬 Phụ đề + thẻ [{ov.get('icon')}]: \"{ov.get('text', '')}\"")
                ov_file = temp_dir / f"seg_{i:03d}_ov.mp4"
                ov = {**ov, "_style": ov.get("_style", speaker_shot_count), **sub_extra}
                apply_speaker_overlay(
                    seg_file, ov_file, ov, w, h, fps, seg_d,
                    progress_start=seg_start / total_dur,
                    progress_end=(seg_start + seg_d) / total_dur,
                    sfx_events=clip_events,
                )
                sfx_events.extend((at + seg_start, n, g) for at, n, g in clip_events)
                seg_file = ov_file
            seg_files.append(seg_file)

        if on_status:
            extra = []
            if use_xf:
                extra.append(f"{num_segments - 1} chuyển cảnh")
            if sfx:
                extra.append(f"{len(sfx_events)} sound effect")
            on_status("🔊 Hoàn thiện video: ghép shot + giọng đọc" + (f" ({', '.join(extra)})" if extra else "") + "...")

        # Track sound effect: cue của từng cảnh + whoosh ở mỗi lần chuyển cảnh
        sfx_wav = None
        if sfx:
            for k in range(1, num_segments):
                if shots[k - 1]["type"] == shots[k]["type"] == "speaker":
                    continue  # đổi góc máy: không whoosh
                sfx_events.append((shots[k]["start"] - 0.12, "whoosh", 0.55 if use_xf else 0.4))
            sfx_wav = render_sfx_track(sfx_events, total_dur, temp_dir / "sfx.wav",
                                       master=default_master() if sfx_volume is None else sfx_volume)

        cmd = [ffmpeg_bin, "-y"]
        for f in seg_files:
            cmd += ["-i", str(f)]
        a_idx = len(seg_files)
        cmd += ["-i", str(audio_file)]
        if sfx_wav:
            cmd += ["-i", str(sfx_wav)]

        parts = []
        for k in range(len(seg_files)):
            parts.append(f"[{k}:v]settb=AVTB,fps={fps},format=yuv420p[v{k}]")
        last = "v0"
        for k in range(1, len(seg_files)):
            if use_xf:
                a_t, b_t = shots[k - 1]["type"], shots[k]["type"]
                if a_t == b_t == "speaker":
                    tr = "fade"  # đổi góc máy: gần như cắt thẳng
                elif a_t == b_t == "visual":
                    tr = _XF_VISUAL[k % len(_XF_VISUAL)]  # thẻ minh hoạ này sang thẻ khác
                else:
                    names = _XF_TO_CUTAWAY if b_t == "visual" else _XF_TO_SPEAKER
                    tr = names[k % len(names)]
                parts.append(f"[{last}][v{k}]xfade=transition={tr}:duration={xd[k]:.3f}:"
                             f"offset={shots[k]['start']:.4f}[x{k}]")
                last = f"x{k}"
        if not use_xf and len(seg_files) > 1:
            parts.append("".join(f"[v{k}]" for k in range(len(seg_files))) + f"concat=n={len(seg_files)}:v=1:a=0[xc]")
            last = "xc"
        if sfx_wav:
            parts.append(f"[{a_idx}:a]aformat=sample_rates=48000:channel_layouts=stereo[va]")
            parts.append(f"[{a_idx + 1}:a]aformat=sample_rates=48000:channel_layouts=stereo[sa]")
            parts.append("[va][sa]amix=inputs=2:duration=first:normalize=0[aout]")
            a_map = "[aout]"
        else:
            a_map = f"{a_idx}:a:0"
        cmd += [
            "-filter_complex", ";".join(parts),
            "-map", f"[{last}]", "-map", a_map,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{total_dur:.3f}",
            str(out_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg lỗi khi ghép video cuối: {res.stderr[-800:]}")
        return out_path
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

