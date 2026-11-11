"""Lớp phủ cho cảnh CÓ người nói: text + icon chuyển động ở trên, phụ đề ở dưới.

- Cùng ngôn ngữ thiết kế với cảnh minh hoạ (text_effects.py): thẻ trắng, chữ xanh đậm,
  từ khoá đỏ (rủi ro/chi phí) hoặc xanh (tốt/lãi), icon vector phẳng trong vòng tròn màu.
- Overlay được vẽ thành frame RGBA trong suốt rồi FFmpeg chồng lên video người nói.
- Nội dung (câu ngắn, từ khoá, icon) do Claude rút ra từ đoạn lời đang đọc; không có API thì
  dùng bộ quy tắc từ khoá tiếng Việt.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from PIL import Image

from sfx import SfxRecorder, cue, set_recorder
from text_effects import (
    C,
    DESIGN_H,
    DESIGN_W,
    _apply_layer,
    _burst,
    _clamp,
    _draw_subtitle,
    _ease_out_quint,
    _spring,
    _ease_out_back,
    _ease_out_cubic,
    _extract_json_array,
    _Frame,
    _mix,
    _prog,
    _progress_strip,
    _wrap,
    build_segment_slots,
    get_ffmpeg_bin,
)

ICONS = ["trend_up", "trend_down", "coin", "cash", "clock", "calendar", "warning", "check", "cross",
         "bulb", "target", "percent", "bars", "shield", "question", "user", "lock", "spark"]


# =============================================================================
# ICON VECTOR (vẽ trắng trên nền tròn màu; k = thời gian từ lúc icon xuất hiện)
# =============================================================================
def _icon(F: _Frame, name: str, cx: float, cy: float, r: float, k: float) -> None:
    W = (255, 255, 255)
    lw = max(6, r * 0.13)
    d = F.d

    def P(x: float, y: float) -> tuple[int, int]:
        return F.x(cx + x * r), F.y(cy + y * r)

    def line(pts, width=lw):
        d.line([P(*p) for p in pts], fill=W, width=F.s(width), joint="curve")

    def partial(pts, f):
        """Polyline vẽ dần theo f (0..1)."""
        segs = list(zip(pts, pts[1:]))
        lens = [math.dist(a, b) for a, b in segs]
        total, out, acc = sum(lens), [pts[0]], 0.0
        for (a, b), ln in zip(segs, lens):
            if acc + ln <= f * total:
                out.append(b)
                acc += ln
            else:
                q = (f * total - acc) / ln if ln else 0
                out.append((a[0] + (b[0] - a[0]) * q, a[1] + (b[1] - a[1]) * q))
                break
        return out

    draw_f = _ease_out_cubic(k / 0.6)
    bob = math.sin(k * 3.0) * 0.04

    if name in ("trend_up", "trend_down"):
        sgn = 1 if name == "trend_up" else -1
        pts = [(-0.52, 0.3 * sgn), (-0.16, -0.04 * sgn), (0.06, 0.14 * sgn), (0.42, -0.28 * sgn)]
        pts = [(x, y + bob) for x, y in pts]
        vis = partial(pts, draw_f)
        line(vis)
        if draw_f > 0.95:
            ex, ey = pts[-1]
            px, py = pts[-2]
            ang = math.atan2(ey - py, ex - px)
            h = 0.26
            a1 = (ex - h * math.cos(ang - 0.6), ey - h * math.sin(ang - 0.6))
            a2 = (ex - h * math.cos(ang + 0.6), ey - h * math.sin(ang + 0.6))
            d.polygon([P(ex + 0.06 * math.cos(ang), ey + 0.06 * math.sin(ang)), P(*a1), P(*a2)], fill=W)
    elif name == "coin":
        sx = max(0.15, abs(math.cos(k * 2.2)))  # đồng xu xoay
        d.ellipse([P(-0.5 * sx, -0.5), P(0.5 * sx, 0.5)], outline=W, width=F.s(lw))
        if sx > 0.45:
            F.text(cx, cy + r * 0.02, "đ", W, "black", r * 0.75 * sx, under=C["green600"])
    elif name == "cash":
        y = bob
        d.rounded_rectangle([P(-0.6, -0.36 + y), P(0.6, 0.36 + y)], radius=F.s(r * 0.1), outline=W, width=F.s(lw * 0.8))
        d.ellipse([P(-0.17, -0.17 + y), P(0.17, 0.17 + y)], outline=W, width=F.s(lw * 0.7))
        for sx in (-0.42, 0.42):
            d.ellipse([P(sx - 0.05, -0.05 + y), P(sx + 0.05, 0.05 + y)], fill=W)
    elif name == "clock":
        d.ellipse([P(-0.55, -0.55), P(0.55, 0.55)], outline=W, width=F.s(lw))
        a = k * 4.0 - math.pi / 2
        line([(0, 0), (0.36 * math.cos(a), 0.36 * math.sin(a))])
        line([(0, 0), (0, -0.25)])
    elif name == "calendar":
        d.rounded_rectangle([P(-0.52, -0.42), P(0.52, 0.5)], radius=F.s(r * 0.1), outline=W, width=F.s(lw * 0.8))
        d.rectangle([P(-0.52, -0.42), P(0.52, -0.18)], fill=W)
        for sx in (-0.26, 0.26):
            line([(sx, -0.58), (sx, -0.34)], lw * 0.8)
        n_lit = int(_clamp(k / 0.8) * 6)
        for i in range(6):
            gx, gy = -0.3 + (i % 3) * 0.3, 0.02 + (i // 3) * 0.24
            rr = 0.07 if i < n_lit else 0.04
            d.ellipse([P(gx - rr, gy - rr), P(gx + rr, gy + rr)], fill=W)
    elif name == "warning":
        s = 1 + 0.06 * math.sin(k * 6) if k > 0.5 else 1
        tri = [(0, -0.55 * s), (0.58 * s, 0.45 * s), (-0.58 * s, 0.45 * s), (0, -0.55 * s)]
        line(tri, lw * 0.85)
        line([(0, -0.18), (0, 0.13)], lw)
        d.ellipse([P(-0.07, 0.24), P(0.07, 0.38)], fill=W)
    elif name == "check":
        line(partial([(-0.4, 0.02), (-0.1, 0.32), (0.45, -0.3)], draw_f), lw * 1.15)
    elif name == "cross":
        f = draw_f
        line(partial([(-0.35, -0.35), (0.35, 0.35)], _clamp(f * 2)), lw * 1.1)
        if f > 0.5:
            line(partial([(0.35, -0.35), (-0.35, 0.35)], _clamp(f * 2 - 1)), lw * 1.1)
    elif name == "bulb":
        d.ellipse([P(-0.34, -0.5), P(0.34, 0.18)], outline=W, width=F.s(lw * 0.85))
        line([(-0.16, 0.3), (0.16, 0.3)], lw * 0.8)
        line([(-0.1, 0.44), (0.1, 0.44)], lw * 0.8)
        glow = 0.5 + 0.5 * math.sin(k * 5)
        for ang in (-150, -110, -70, -30):
            a = math.radians(ang)
            r1, r2 = 0.5, 0.6 + 0.08 * glow
            line([(r1 * math.cos(a), -0.16 + r1 * math.sin(a)), (r2 * math.cos(a), -0.16 + r2 * math.sin(a))], lw * 0.6)
    elif name == "target":
        pulse = 0.04 * math.sin(k * 4)
        for rr in (0.55 + pulse, 0.33):
            d.ellipse([P(-rr, -rr), P(rr, rr)], outline=W, width=F.s(lw * 0.75))
        d.ellipse([P(-0.11, -0.11), P(0.11, 0.11)], fill=W)
    elif name == "percent":
        F.text(cx, cy + r * 0.03 + bob * r, "%", W, "black", r * 1.15, under=C["green600"])
    elif name == "bars":
        for i, (bx, hgt) in enumerate([(-0.36, 0.45), (0, 0.72), (0.36, 1.0)]):
            g = _ease_out_back(_clamp((k - i * 0.12) / 0.45))
            top = 0.48 - 0.95 * hgt * g
            d.rectangle([P(bx - 0.12, top), P(bx + 0.12, 0.48)], fill=W)
    elif name == "shield":
        sh = [(0, -0.56), (0.46, -0.38), (0.4, 0.12), (0, 0.56), (-0.4, 0.12), (-0.46, -0.38), (0, -0.56)]
        line(sh, lw * 0.8)
        line(partial([(-0.2, 0.0), (-0.04, 0.17), (0.24, -0.16)], _clamp((k - 0.3) / 0.5)), lw * 0.85)
    elif name == "question":
        F.text(cx, cy + r * 0.05 + bob * r, "?", W, "black", r * 1.2, under=C["green900"])
    elif name == "user":
        d.ellipse([P(-0.2, -0.5 + bob), P(0.2, -0.1 + bob)], fill=W)
        d.pieslice([P(-0.45, 0.0), P(0.45, 0.9)], 180, 360, fill=W)
    elif name == "lock":
        d.arc([P(-0.27, -0.6), P(0.27, -0.06)], 180, 360, fill=W, width=F.s(lw * 0.85))
        line([(-0.27, -0.33), (-0.27, -0.12)], lw * 0.85)
        line([(0.27, -0.33), (0.27, -0.12)], lw * 0.85)
        d.rounded_rectangle([P(-0.42, -0.12), P(0.42, 0.5)], radius=F.s(r * 0.08), fill=W)
    else:  # spark
        rot = k * 1.5
        s = 0.9 + 0.1 * math.sin(k * 5)
        pts = []
        for i in range(8):
            a = rot + i * math.pi / 4
            rr = (0.58 if i % 2 == 0 else 0.18) * s
            pts.append(P(rr * math.cos(a), rr * math.sin(a)))
        d.polygon(pts, fill=W)


# =============================================================================
# VẼ 1 FRAME OVERLAY (RGBA trong suốt)
# =============================================================================
def _tone(tone: str) -> tuple:
    return {"good": C["green600"], "bad": C["red"]}.get((tone or "").lower(), C["green900"])


def render_overlay_frame(ov: dict, t: float, duration: float, w: int, h: int,
                         progress: float = 0.0, show_progress: bool = True) -> Image.Image:
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    S = min(w / DESIGN_W, h / DESIGN_H)
    F = _Frame(img, S, (w - DESIGN_W * S) / 2, (h - DESIGN_H * S) / 2)

    if show_progress:
        full_w = F.s(DESIGN_W)
        pw = int(full_w * _clamp(progress))
        if pw > 0:
            img.alpha_composite(_progress_strip(full_w, F.s(10)).crop((0, 0, pw, F.s(10))), (F.x(0), F.y(0)))

    text = (ov.get("text") or "").strip()
    icon = ov.get("icon") if ov.get("icon") in ICONS else "spark"
    accent = _tone(ov.get("tone", ""))
    if text:
        style = int(ov.get("_style", 0))
        entr = ov.get("_entrance") or ["drop", "slide", "pop", "expand"][style % 4]
        hl = ["marker", "underline", "box"][style % 3]
        out = 1.0 - _ease_out_cubic(_prog(t, duration - 0.3, 0.3))  # rút lên khi sắp cắt cảnh
        px = 56
        lines = _wrap(F, text, "heavy", px, 690, max_lines=2)
        while (len(lines) > 2 or any(F.text_w(ln, "heavy", px) > 690 for ln in lines)) and px > 34:
            px *= 0.93
            lines = _wrap(F, text, "heavy", px, 690, max_lines=2)
        lh = px * 1.22
        card_h = max(190, lh * len(lines) + 80)
        l, r, top = 60, 1020, 120
        icx, icy = l + 110, top + card_h / 2

        # --- kiểu xuất hiện của thẻ
        dx = dy = 0.0
        sc, alpha = 1.0, _clamp(_prog(t, 0.05, 0.2))
        t_words, t_icon = 0.35, 0.2
        if entr == "drop":
            dy = -(1 - _spring(_prog(t, 0.05, 0.8))) * 220
            cue("swish", 0.05, 0.5)
            cue("thud", 0.3, 0.45)
        elif entr == "slide":
            dx = -(1 - _ease_out_quint(_prog(t, 0.05, 0.6))) * 520
            cue("whoosh_soft", 0.02, 0.7)
        elif entr == "pop":
            sc = 0.55 + 0.45 * _spring(_prog(t, 0.05, 0.7))
            cue("pop", 0.05, 0.7)
        else:  # expand: icon xuất hiện trước, thẻ kéo dài sang phải
            t_icon, t_words = 0.05, 0.75
            g = _ease_out_quint(_prog(t, 0.3, 0.5))
            r = l + 230 + (r - l - 230) * g
            cue("swish", 0.3, 0.55)
        dy -= (1 - out) * 50
        alpha *= out
        cue("pop_soft" if entr == "pop" else "pop", t_icon, 0.7)

        if alpha > 0.02:
            layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            Lf = _Frame(layer, S, F.ox, F.oy)
            Lf.shadow(l, top, r, top + card_h, 30, strength=0.18)
            Lf.rrect(l, top, r, top + card_h, 30, fill=(255, 255, 255, 240))
            Lf.rrect(l, top + 26, l + 10, top + card_h - 26, 5, fill=accent)

            # Icon: pop lò xo + nảy nhẹ liên tục + hạt bung
            k = t - t_icon
            ip = _spring(_clamp(k / 0.6))
            iy = icy + math.sin(t * 2.4) * 4
            ir = 64 * ip
            _burst(Lf, icx, iy, t, t_icon + 0.12, accent, n=10, radius=120, under=(255, 255, 255))
            if ir > 2:
                Lf.d.ellipse(Lf.box(icx - ir, iy - ir, icx + ir, iy + ir), fill=accent)
                if ip > 0.6:
                    _icon(Lf, icon, icx, iy, ir * 0.78, max(0.0, k - 0.2))

            # Chữ hiện từng từ; từ khoá đổi màu + hiệu ứng nhấn (bút dạ / gạch chân / khung)
            kw = (ov.get("keyword") or "").strip().lower()
            kw_words = set(kw.split())
            all_words = [wd for ln in lines for wd in ln.split()]
            per = min(0.12, max(0.05, (duration * 0.35) / max(1, len(all_words))))
            t_hl = t_words + len(all_words) * per + 0.1
            if kw:
                cue("ding_soft" if hl == "box" else "marker", t_hl, 0.6 if hl == "box" else 0.8)
            # Bố cục trước (vị trí từng từ), rồi vẽ hiệu ứng nhấn, cuối cùng vẽ chữ
            clip_r = r - 30
            placed = []
            idx = 0
            for li, ln in enumerate(lines):
                y = top + card_h / 2 + (li - (len(lines) - 1) / 2) * lh - px * 0.04
                x = l + 210
                for wd in ln.split():
                    wpx = Lf.text_w(wd, "heavy", px)
                    if x + wpx > clip_r:
                        break
                    bare = re.sub(r"[^\w%,.+\-−]", "", wd.lower()).strip(",.")
                    is_kw = bool(kw) and (bare in kw_words or bare == kw)
                    placed.append((li, x, y, wd, wpx, is_kw, idx))
                    x += Lf.text_w(wd + " ", "heavy", px)
                    idx += 1
            kw_col = accent if accent != C["green900"] else C["green600"]
            g = _ease_out_cubic(_prog(t, t_hl, 0.35))
            if kw and g > 0:
                # gộp các từ khoá liền nhau trên cùng dòng thành 1 cụm
                runs = []
                for li, x, y, wd, wpx, is_kw, _ in placed:
                    if not is_kw:
                        continue
                    if runs and runs[-1][0] == li and abs(runs[-1][2] - x) < px * 0.6:
                        runs[-1][2] = x + wpx
                    else:
                        runs.append([li, x, x + wpx, y])
                for li, x0, x1, y in runs:
                    if hl == "marker":
                        soft = _mix(kw_col, (255, 255, 255), 0.18)
                        Lf.rrect(x0 - 8, y + px * 0.06, x0 - 8 + (x1 - x0 + 16) * g, y + px * 0.52, 8, fill=soft + (255,))
                    elif hl == "underline":
                        Lf.rrect(x0, y + px * 0.52, x0 + (x1 - x0) * g, y + px * 0.52 + 8, 4, fill=kw_col + (255,))
                    else:
                        soft = _mix(kw_col, (255, 255, 255), 0.12 * g)
                        Lf.rrect(x0 - 7, y - px * 0.58, x1 + 7, y + px * 0.62, 12, fill=soft + (255,),
                                 outline=_mix(kw_col, (255, 255, 255), g) + (255,), width=4)
            for li, x, y, wd, wpx, is_kw, i_w in placed:
                wa = _ease_out_quint(_prog(t, t_words + i_w * per, 0.35))
                col = kw_col if is_kw else C["green900"]
                bump = 1.0 + (0.08 * math.sin(math.pi * _prog(t, t_hl, 0.3)) if is_kw else 0)
                Lf.text(x, y + (1 - wa) * 18, wd, col, "heavy", px * bump, anchor="lm", alpha=wa,
                        under=(255, 255, 255))

            _apply_layer(img, layer, (dx * S, dy * S, sc, sc, 0, alpha), (F.x(icx), F.y(icy)))

    # Phụ đề dưới
    _draw_subtitle(F, ov.get("sub_text", ""), t, duration, dark=False,
                   words=ov.get("_words"), mode=ov.get("_sub_mode", "word"))
    return img


def apply_speaker_overlay(in_video: Path, out_video: Path, ov: dict, w: int, h: int, fps: int,
                          duration: float, progress_start: float = 0.0, progress_end: float = 1.0,
                          sfx_events: Optional[list] = None) -> Path:
    """Chồng overlay (text/icon trên + phụ đề dưới) lên clip người nói."""
    total = max(1, int(round(duration * fps)))
    cmd = [
        get_ffmpeg_bin("ffmpeg"), "-y", "-loglevel", "error",
        "-i", str(in_video),
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
        "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto:shortest=1,format=yuv420p[v]",
        "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-r", str(fps),
        str(out_video),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    rec = SfxRecorder()
    set_recorder(rec)
    try:
        for i in range(total):
            t = i / fps
            rec.t = t
            prog = progress_start + (progress_end - progress_start) * t / max(1e-6, duration)
            proc.stdin.write(render_overlay_frame(ov, t, duration, w, h, prog).tobytes())
        proc.stdin.close()
        if sfx_events is not None:
            sfx_events.extend(rec.list())
        err = proc.stderr.read().decode("utf-8", "ignore")
        if proc.wait() != 0:
            raise RuntimeError(f"ffmpeg lỗi khi chồng overlay: {err[-500:]}")
        return out_video
    except Exception:
        if proc.poll() is None:
            proc.kill()
        raise
    finally:
        set_recorder(None)


# =============================================================================
# NỘI DUNG OVERLAY
# =============================================================================
_ICON_RULES = [
    (("lỗ", "giảm", "mất", "tụt", "sập", "âm"), "trend_down", "bad"),
    (("rủi ro", "cẩn thận", "nguy", "sai lầm", "bẫy", "cảnh báo"), "warning", "bad"),
    (("phí", "chi phí", "thuế"), "cash", "bad"),
    (("lãi", "tăng", "lợi nhuận", "tăng trưởng", "sinh lời"), "trend_up", "good"),
    (("%", "phần trăm", "tỷ lệ", "tỉ lệ"), "percent", "neutral"),
    (("thời gian", "lâu dài", "kiên nhẫn", "năm", "tháng", "ngày"), "clock", "neutral"),
    (("tiền", "vốn", "triệu", "tỷ", "nghìn đồng"), "coin", "neutral"),
    (("mục tiêu", "chiến lược", "kế hoạch"), "target", "neutral"),
    (("dữ liệu", "biểu đồ", "thống kê", "backtest", "số liệu"), "bars", "neutral"),
    (("an toàn", "bảo vệ", "quản trị", "phòng thủ"), "shield", "good"),
    (("ý tưởng", "bí quyết", "mẹo", "hiểu", "nhớ"), "bulb", "neutral"),
    (("đúng", "nên", "hãy"), "check", "good"),
    (("không nên", "đừng", "tránh"), "cross", "bad"),
    (("?", "tại sao", "vì sao", "bao nhiêu", "liệu"), "question", "neutral"),
    (("bạn", "nhà đầu tư", "trader", "người"), "user", "neutral"),
]


def _fallback_overlay(slot: dict, speech_text: str) -> dict:
    ex = (slot.get("spoken_excerpt") or "").strip()
    sentences = [x.strip() for x in re.split(r"(?<=[.!?…])\s+", speech_text or ex) if x.strip()]
    ex_words = ex.lower().split()
    core = max(sentences or [ex], key=lambda s: sum(1 for wd in s.lower().split() if wd in ex_words))
    low = core.lower()
    icon, tone = "spark", "neutral"
    for keys, ic, tn in _ICON_RULES:
        if any(k in low for k in keys):
            icon, tone = ic, tn
            break
    # Câu ngắn dùng nguyên; câu dài -> chọn mệnh đề (tách theo dấu phẩy) chứa con số, hoặc mệnh đề đầu
    clean = core.rstrip(".!?…")
    clauses = [c.strip() for c in re.split(r"[,;:]", clean) if c.strip()]
    num = re.search(r"[+\-−]?\d+(?:[.,]\d+)*\s?%?", clean)
    if len(clean.split()) <= 11:
        text = clean
    else:
        pick = next((c for c in clauses if num and num.group(0).strip() in c), clauses[0] if clauses else clean)
        text = " ".join(pick.split()[:10])
    keyword = num.group(0).strip() if num and num.group(0).strip() in text else ""
    return {"icon": icon, "tone": tone, "text": text, "keyword": keyword, "sub_text": ex,
            "segment_index": slot.get("segment_index")}


def _overlay_prompt(speech_text: str, slots: list[dict], goal: str) -> str:
    rows = "\n".join(f"- Cảnh #{s['slot_index'] + 1} [{s['time_range']}]: \"{s.get('spoken_excerpt', '')}\"" for s in slots)
    return f"""Bạn là biên tập viên đồ hoạ của kênh QuantVN · Research Lab.
Trong các cảnh người dẫn đang nói, phía trên màn hình có một thẻ nhỏ: 1 icon + 1 câu ngắn tóm ý đang nói.
Hãy thiết kế thẻ cho từng cảnh dưới đây, dựa đúng vào lời đang đọc trong cảnh đó.

Quy tắc:
- "text": 3-8 từ tiếng Việt, tóm ý chính đang nói (không chép nguyên câu dài, không thêm ý mới, không bịa số).
- "keyword": 1-2 từ trong "text" cần nhấn (thường là con số hoặc khái niệm then chốt).
- "tone": "bad" (chi phí, rủi ro, lỗ, sai lầm), "good" (lãi, an toàn, đúng), "neutral" (còn lại).
- "icon": chọn 1 trong {ICONS}.
- Các cảnh liên tiếp nên dùng icon khác nhau nếu hợp lý.
{"Mục tiêu video: " + goal if goal else ""}
TOÀN BỘ KỊCH BẢN:
\"\"\"{speech_text}\"\"\"

CÁC CẢNH NGƯỜI NÓI ({len(slots)} cảnh):
{rows}

Chỉ trả về JSON array gồm đúng {len(slots)} object dạng {{"text": "...", "keyword": "...", "tone": "...", "icon": "..."}}."""


def generate_speaker_overlays(speech_text: str, slots: list[dict], anthropic_key: Optional[str] = None,
                              demo_mode: bool = False, goal: str = "") -> list[dict[str, Any]]:
    """Mỗi slot người nói -> {icon, tone, text, keyword, sub_text}."""
    if not slots:
        return []
    if not demo_mode and anthropic_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=anthropic_key)
            prompt = _overlay_prompt(speech_text, slots, goal)
            preferred = os.getenv("TEXT_EFFECT_MODEL") or os.getenv("BRAIN_MODEL") or "claude-opus-5-5"
            for model_name in dict.fromkeys([preferred, "claude-opus-5-5", "claude-sonnet-5-5"]):
                try:
                    res = client.messages.create(model=model_name, max_tokens=3000,
                                                 messages=[{"role": "user", "content": prompt}])
                    raw = "".join(getattr(b, "text", "") for b in res.content if getattr(b, "type", "") == "text")
                    items = _extract_json_array(raw)
                    out = []
                    for i, s in enumerate(slots):
                        it = items[i] if i < len(items) and isinstance(items[i], dict) else {}
                        fb = _fallback_overlay(s, speech_text)
                        out.append({
                            "icon": it.get("icon") if it.get("icon") in ICONS else fb["icon"],
                            "tone": str(it.get("tone") or fb["tone"]),
                            "text": str(it.get("text") or fb["text"]),
                            "keyword": str(it.get("keyword") or ""),
                            "sub_text": s.get("spoken_excerpt", ""),
                            "segment_index": s.get("segment_index"),
                        })
                    return out
                except Exception:
                    continue
        except Exception:
            pass
    return [_fallback_overlay(s, speech_text) for s in slots]


def build_speaker_slots(speech_text: str, total_dur: float, interval: float = 5.0,
                        timeline: Optional[list] = None) -> list[dict[str, Any]]:
    """Các shot chẵn (người nói) với cùng cách chia như avatar_director."""
    return build_segment_slots(speech_text, total_dur, interval, parity=0, timeline=timeline)
