"""Cảnh minh hoạ (cutaway, không có người nói) theo style "QuantVN Research Lab".

Ý tưởng chính:
- STYLE là cố định (màu, font, bố cục, nhịp chuyển động) theo HUONG-DAN-STYLE-VIDEO-MINH-HOA.md.
- NỘI DUNG lấy hoàn toàn từ kịch bản của người dùng. Không có con số / câu chữ nào của
  video mẫu (phí 0,15%, 0,4%, 100 vòng...) được gắn cứng trong template.

Mỗi cảnh là một "card":
    {
      "template": "calculator" | "receipt" | "dots" | "result" | "chart" |
                  "compare" | "bignumber" | "statement" | "climax",
      "headline": "dòng tiêu đề 1",
      "highlight_text": "dòng tiêu đề 2 (nhấn)",
      "sub_text": "phụ đề = lời đang đọc",
      "props": {... dữ liệu riêng của template, xem TEMPLATE_SPEC ...},
      "badge": "QuantVN · Research Lab", "tag": "Ví dụ minh hoạ"
    }

Mọi chuyển động là hàm thuần của thời gian t (giây) -> frame chụp chính xác, render qua pipe FFmpeg.
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import shutil
import subprocess
import zlib
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from sfx import SfxRecorder, cue, set_recorder


def get_ffmpeg_bin(name: str = "ffmpeg") -> str:
    """Tìm binary ffmpeg/ffprobe từ PATH hoặc C:\\ffmpeg."""
    p = shutil.which(name)
    if p:
        return p
    for candidate in [Path(f"C:/ffmpeg/{name}.exe"), Path(f"C:/ffmpeg/bin/{name}.exe")]:
        if candidate.exists():
            return str(candidate)
    return name


# =============================================================================
# DESIGN TOKENS (mục 3 của file hướng dẫn)
# =============================================================================
C = {
    "bg": (236, 243, 236),          # #ECF3EC
    "bg_glow": (242, 249, 244),     # #F2F9F4
    "card": (255, 255, 255),
    "green900": (11, 56, 41),       # #0B3829
    "green_key": (20, 80, 59),      # #14503B phím số
    "green600": (34, 168, 85),      # #22A855
    "green400": (76, 217, 100),     # #4CD964
    "red": (220, 38, 38),           # #DC2626
    "red_soft": (253, 232, 232),    # #FDE8E8
    "dot_off": (227, 232, 228),     # #E3E8E4
    "end_top": (11, 72, 83),        # #0B4853
    "end_bottom": (9, 42, 31),      # #092A1F
    "muted": (107, 122, 114),       # #6B7A72
    "grid": (226, 232, 228),
    "line_soft": (210, 218, 212),
    "shadow": (11, 56, 41),
}
SUB_BG_LIGHT = (40, 40, 40, 199)    # rgba(40,40,40,.78)
SUB_BG_DARK = (0, 0, 0, 217)        # rgba(0,0,0,.85)

DESIGN_W, DESIGN_H = 1080, 1920
TEMPLATES = ["calculator", "receipt", "dots", "result", "chart", "compare", "bignumber", "statement", "climax"]


# =============================================================================
# FONT
# =============================================================================
_FONT_FILES = {
    "black": ["SVN-Gilroy Black.otf", "SVN-Gilroy Heavy.otf", "SVN-Gilroy XBold.otf"],
    "heavy": ["SVN-Gilroy Heavy.otf", "SVN-Gilroy XBold.otf", "SVN-Gilroy Bold.otf"],
    "xbold": ["SVN-Gilroy XBold.otf", "SVN-Gilroy Bold.otf"],
    "bold": ["SVN-Gilroy Bold.otf", "SVN-Gilroy SemiBold.otf"],
    "semibold": ["SVN-Gilroy SemiBold.otf", "SVN-Gilroy Bold.otf", "SVN-Gilroy Medium.otf"],
    "medium": ["SVN-Gilroy Medium.otf", "SVN-Gilroy Regular.otf"],
    "regular": ["SVN-Gilroy Regular.otf", "SVN-Gilroy Medium.otf"],
}
_FONT_DIRS = [
    Path(os.environ.get("QUANTVN_FONT_DIR", "")) if os.environ.get("QUANTVN_FONT_DIR") else None,
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Windows/Fonts" if os.environ.get("LOCALAPPDATA") else None,
    Path("C:/Windows/Fonts"),
    Path(__file__).resolve().parent / "fonts",
]


@lru_cache(maxsize=None)
def get_windows_font(bold: bool = True, weight: str = "bold") -> str:
    """Đường dẫn font SVN-Gilroy theo weight (fallback font Windows có dấu tiếng Việt)."""
    weight = (weight or ("bold" if bold else "regular")).lower()
    for name in _FONT_FILES.get(weight, _FONT_FILES["bold"]):
        for d in _FONT_DIRS:
            if d and (d / name).exists():
                return str(d / name)
    heavy = weight in ("black", "heavy", "xbold", "bold", "semibold")
    for c in (["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"] if heavy
              else ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf"]):
        if Path(c).exists():
            return c
    return "arial.ttf"


@lru_cache(maxsize=1024)
def _font(weight: str, px: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(get_windows_font(weight=weight), max(8, int(px)))
    except Exception:
        return ImageFont.load_default()


def get_quantvn_font(weight: str = "bold", size: int = 40) -> ImageFont.FreeTypeFont:
    return _font(weight, size)


# =============================================================================
# EASING & TIỆN ÍCH
# =============================================================================
def _clamp(x: float, a: float = 0.0, b: float = 1.0) -> float:
    return max(a, min(b, x))


def _ease_out_cubic(x: float) -> float:
    x = _clamp(x)
    return 1.0 - (1.0 - x) ** 3


def _ease_out_back(x: float) -> float:
    x = _clamp(x)
    c1 = 1.70158
    return 1.0 + (c1 + 1.0) * (x - 1.0) ** 3 + c1 * (x - 1.0) ** 2


def _ease_in_out_quad(x: float) -> float:
    x = _clamp(x)
    return 2 * x * x if x < 0.5 else 1 - (-2 * x + 2) ** 2 / 2


def _ease_out_quint(x: float) -> float:
    x = _clamp(x)
    return 1.0 - (1.0 - x) ** 5


def _spring(x: float, bounce: float = 0.35, freq: float = 1.6) -> float:
    """Lò xo: 0 -> 1 có vượt nhẹ rồi lắng (x = 0..1 trên thời lượng animation)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    damp = 4.5 + (1 - bounce) * 4
    return 1 - math.exp(-damp * x) * math.cos(2 * math.pi * freq * x) * (1 - x) ** 0.5


def _inv_in_out_quad(y: float) -> float:
    y = _clamp(y)
    return math.sqrt(y / 2) if y < 0.5 else 1 - math.sqrt((1 - y) / 2)


def _prog(t: float, start: float, dur: float) -> float:
    return _clamp((t - start) / max(1e-6, dur))


def _mix(c: tuple, under: tuple, a: float) -> tuple:
    a = _clamp(a)
    return tuple(int(round(u + (v - u) * a)) for v, u in zip(c[:3], under[:3]))


def _schedule(n: int, duration: float, start: float = 0.35, end_frac: float = 0.72) -> list[float]:
    """Rải n mốc xuất hiện đều trong clip (progressive build theo lời đọc)."""
    if n <= 0:
        return []
    end = max(start + 0.25 * (n - 1), duration * end_frac)
    if n == 1:
        return [start]
    step = (end - start) / (n - 1)
    return [start + i * step for i in range(n)]


_NUM_RE = re.compile(r"^(?P<pre>[^\d]*?)(?P<sign>[+\-−]?)(?P<int>\d{1,3}(?:\.\d{3})+|\d+)(?:,(?P<dec>\d+))?(?P<suf>.*)$")


def _count_up_text(value: str, frac: float) -> str:
    """'40%' -> '17%' ở frac=0.43 (giữ dấu phẩy thập phân kiểu Việt, dấu chấm hàng nghìn)."""
    m = _NUM_RE.match(value.strip())
    if not m or frac >= 1.0:
        return value
    int_part = m.group("int")
    dec = m.group("dec") or ""
    target = float(int_part.replace(".", "") + ("." + dec if dec else ""))
    cur = target * _clamp(frac)
    if dec:
        s = f"{cur:.{len(dec)}f}"
        ip, dp = s.split(".")
    else:
        ip, dp = str(int(round(cur))), ""
    if "." in int_part:
        ip = f"{int(ip):,}".replace(",", ".")
    out = ip + ("," + dp if dp else "")
    return f"{m.group('pre')}{m.group('sign')}{out}{m.group('suf')}"


# =============================================================================
# KHUNG VẼ (thiết kế trên lưới 1080x1920, tự scale & căn giữa vào mọi khung hình)
# =============================================================================
_SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


def _clean(txt: str) -> str:
    """Đổi ký tự font Gilroy không có (chữ số mũ, mũi tên lạ) sang dạng an toàn."""
    txt = re.sub(r"([⁰¹²³⁴⁵⁶⁷⁸⁹]+)", lambda m: "^" + m.group(1).translate(_SUPERSCRIPT), txt)
    return txt.replace("⟶", "→").replace("➜", "→").replace(" ", " ")


class _Frame:
    def __init__(self, img: Image.Image, S: float, ox: float, oy: float):
        self.img = img
        self.d = ImageDraw.Draw(img)
        self.S, self.ox, self.oy = S, ox, oy

    def x(self, v: float) -> int:
        return int(round(self.ox + v * self.S))

    def y(self, v: float) -> int:
        return int(round(self.oy + v * self.S))

    def s(self, v: float) -> int:
        return max(1, int(round(v * self.S)))

    def box(self, l: float, t: float, r: float, b: float) -> list[int]:
        return [self.x(l), self.y(t), self.x(r), self.y(b)]

    def font(self, weight: str, px: float) -> ImageFont.FreeTypeFont:
        return _font(weight, max(8, int(round(px * self.S))))

    def text_w(self, txt: str, weight: str, px: float) -> float:
        """Độ rộng chữ theo đơn vị thiết kế."""
        return self.font(weight, px).getlength(_clean(txt)) / self.S

    def fit_px(self, txt: str, weight: str, px: float, max_w: float, min_px: float = 20) -> float:
        while px > min_px and self.text_w(txt, weight, px) > max_w:
            px *= 0.94
        return px

    def text(self, x: float, y: float, txt: str, fill: tuple, weight: str, px: float,
             anchor: str = "mm", alpha: float = 1.0, under: tuple = C["bg"]) -> None:
        if not txt or alpha <= 0.02:
            return
        txt = _clean(txt)
        col = fill if alpha >= 0.999 else _mix(fill, under, alpha)
        self.d.text((self.x(x), self.y(y)), txt, fill=col, font=self.font(weight, px), anchor=anchor)

    def rrect(self, l, t, r, b, radius, fill=None, outline=None, width=0) -> None:
        self.d.rounded_rectangle(self.box(l, t, r, b), radius=self.s(radius), fill=fill,
                                 outline=outline, width=self.s(width) if width else 0)

    def shadow(self, l, t, r, b, radius, strength: float = 0.16, blur: float = 22, dy: float = 14) -> None:
        sh = _shadow_img(self.s(r - l), self.s(b - t), self.s(radius), self.s(blur), int(255 * strength))
        pad = self.s(blur) * 2
        self.img.alpha_composite(sh, (self.x(l) - pad, self.y(t + dy) - pad))


@lru_cache(maxsize=64)
def _shadow_img(w: int, h: int, r: int, blur: int, alpha: int) -> Image.Image:
    pad = blur * 2
    im = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle([pad, pad, pad + w, pad + h], radius=r, fill=C["shadow"] + (alpha,))
    return im.filter(ImageFilter.GaussianBlur(blur))


@lru_cache(maxsize=8)
def _background(w: int, h: int, dark: bool) -> Image.Image:
    """Nền sáng: radial gradient nhẹ (sáng giữa, tối dần ra mép). Nền tối: gradient dọc."""
    if dark:
        mask = Image.linear_gradient("L").resize((w, h))
        top = Image.new("RGBA", (w, h), C["end_top"] + (255,))
        bot = Image.new("RGBA", (w, h), C["end_bottom"] + (255,))
        return Image.composite(bot, top, mask)
    mask = Image.radial_gradient("L").resize((w, int(h * 1.1))).crop((0, int(h * 0.05), w, int(h * 0.05) + h))
    glow = Image.new("RGBA", (w, h), C["bg_glow"] + (255,))
    edge = Image.new("RGBA", (w, h), _mix(C["bg"], (200, 214, 204), 0.35) + (255,))
    base = Image.composite(edge, glow, mask)
    return base


@lru_cache(maxsize=8)
def _blob_sprite(d: int, color: tuple, alpha: int) -> Image.Image:
    im = Image.new("RGBA", (d, d), color + (0,))
    m = Image.new("L", (d, d), 0)
    ImageDraw.Draw(m).ellipse([d * 0.2, d * 0.2, d * 0.8, d * 0.8], fill=alpha)
    im.putalpha(m.filter(ImageFilter.GaussianBlur(d * 0.12)))
    return im


def _draw_ambient(F: "_Frame", t: float, dark: bool, seed: int = 0) -> None:
    """Nền 'thở': vài đốm sáng mờ trôi chậm (+ hạt bay lên ở cảnh tối)."""
    col = C["green400"] if dark else C["green600"]
    sprite = _blob_sprite(F.s(720), col, 34 if dark else 26)
    for k in range(3):
        ph = seed * 1.7 + k * 2.1
        x = 540 + math.sin(t * 0.35 + ph) * 300 + (k - 1) * 260
        y = 900 + math.cos(t * 0.28 + ph * 1.3) * 380 + (k - 1) * 300
        F.img.paste(sprite, (F.x(x - 360), F.y(y - 360)), sprite)
    if dark:
        for k in range(18):
            sx = (k * 137.5 + seed * 53) % 1000 + 40
            speed = 40 + (k * 29) % 50
            sy = 1900 - ((t * speed + k * 211) % 1800)
            rr = 3 + (k % 3)
            a = 0.25 + 0.2 * math.sin(t * 2 + k)
            F.d.ellipse(F.box(sx - rr, sy - rr, sx + rr, sy + rr), fill=_mix(C["green400"], C["end_bottom"], a))


def _burst(F: "_Frame", cx: float, cy: float, t: float, t0: float, color: tuple, n: int = 10,
           radius: float = 150, under: tuple = C["bg"]) -> None:
    """Hạt bung ra khi một phần tử 'pop' (0.6s)."""
    k = _prog(t, t0, 0.6)
    if k <= 0 or k >= 1:
        return
    e = _ease_out_cubic(k)
    for i in range(n):
        ang = i / n * 2 * math.pi + 0.3
        dist = radius * (0.55 + 0.45 * ((i * 7) % 3) / 2) * e
        rr = (9 if i % 2 else 6) * (1 - k)
        x, y = cx + math.cos(ang) * dist, cy + math.sin(ang) * dist
        F.d.ellipse(F.box(x - rr, y - rr, x + rr, y + rr), fill=_mix(color, under, 1 - k * 0.6))


ENTRANCES = ["rise", "zoom", "slide_left", "drop", "swing", "slide_right", "unfold"]


def _entrance(style: str, t: float) -> tuple:
    """(dx, dy, sx, sy, rot_deg, alpha) cho lớp nội dung minh hoạ lúc xuất hiện (đơn vị thiết kế)."""
    q = _ease_out_quint(t / 0.65)
    sp = _spring(t / 0.9)
    a = _ease_out_cubic(t / 0.25)
    if style == "zoom":
        s = 0.8 + 0.2 * sp
        return 0, 0, s, s, 0, a
    if style == "slide_left":
        return (1 - q) * 300, 0, 1, 1, 0, a
    if style == "slide_right":
        return -(1 - q) * 300, 0, 1, 1, 0, a
    if style == "drop":
        return 0, -(1 - sp) * 260, 1, 1, 0, a
    if style == "swing":
        return 0, (1 - q) * 70, 1, 1, (1 - sp) * 7, a
    if style == "unfold":
        return 0, 0, 1, max(0.02, sp), 0, a
    return 0, (1 - q) * 150, 1, 1, 0, a  # rise


def _apply_layer(dst: Image.Image, layer: Image.Image, xf: tuple, pivot: tuple) -> None:
    """Chồng layer lên dst với dịch/scale/xoay quanh pivot (pixel)."""
    dx, dy, sx, sy, rot, alpha = xf
    if alpha < 0.999:
        layer.putalpha(layer.getchannel("A").point(lambda v: int(v * alpha)))
    if abs(sx - 1) > 1e-3 or abs(sy - 1) > 1e-3 or abs(rot) > 1e-2:
        px, py = pivot
        th = math.radians(rot)
        c, s_ = math.cos(th), math.sin(th)
        a_, b_ = c / sx, s_ / sx
        d_, e_ = -s_ / sy, c / sy
        cx_, cy_ = px + dx, py + dy
        coeffs = (a_, b_, px - a_ * cx_ - b_ * cy_, d_, e_, py - d_ * cx_ - e_ * cy_)
        layer = layer.transform(layer.size, Image.AFFINE, coeffs, resample=Image.BICUBIC)
        dst.alpha_composite(layer)
    else:
        _composite_at(dst, layer, int(round(dx)), int(round(dy)))


def _composite_at(dst: Image.Image, src: Image.Image, x: int, y: int) -> None:
    """alpha_composite đúng chuẩn 'over' kể cả khi toạ độ âm (paste làm tối màu trên nền trong suốt)."""
    sx, sy = max(0, -x), max(0, -y)
    if sx >= src.width or sy >= src.height or x >= dst.width or y >= dst.height:
        return
    w = min(src.width - sx, dst.width - max(0, x))
    h = min(src.height - sy, dst.height - max(0, y))
    if w > 0 and h > 0:
        dst.alpha_composite(src, (max(0, x), max(0, y)), (sx, sy, sx + w, sy + h))


@lru_cache(maxsize=4)
def _progress_strip(w: int, h: int) -> Image.Image:
    strip = Image.new("RGBA", (w, h))
    px = strip.load()
    stops = [(0.0, C["end_top"]), (0.6, C["green600"]), (1.0, C["green400"])]
    for x in range(w):
        f = x / max(1, w - 1)
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            if p0 <= f <= p1:
                col = _mix(c1, c0, (f - p0) / (p1 - p0))
                break
        for y in range(h):
            px[x, y] = col + (255,)
    return strip


# =============================================================================
# KHUNG CỐ ĐỊNH: thanh tiến trình, badge, nhãn, tiêu đề, phụ đề
# =============================================================================
def _draw_chrome(F: _Frame, card: dict, t: float, progress: float) -> None:
    # Thanh tiến trình gradient chạy theo tiến độ CẢ video
    full_w = F.s(DESIGN_W)
    pw = int(full_w * _clamp(progress))
    if pw > 0:
        F.img.alpha_composite(_progress_strip(full_w, F.s(10)).crop((0, 0, pw, F.s(10))), (F.x(0), F.y(0)))

    badge = card.get("badge") or "QuantVN · Research Lab"
    bh, by = 62, 199
    bw = F.text_w(badge, "bold", 26) + 82
    F.rrect(60, by, 60 + bw, by + bh, bh / 2, fill=C["green900"])
    F.d.ellipse(F.box(84, by + bh / 2 - 7, 98, by + bh / 2 + 7), fill=C["green400"])
    F.text(112, by + bh / 2, badge, (255, 255, 255), "bold", 26, anchor="lm")
    tag = card.get("tag", "Ví dụ minh hoạ")
    if tag:
        F.text(DESIGN_W - 60, by + bh / 2, tag, C["muted"], "semibold", 24, anchor="rm")


def _tone_color(tone: str, default: tuple) -> tuple:
    return {"bad": C["red"], "red": C["red"], "good": C["green600"], "green": C["green600"],
            "muted": C["muted"], "dark": C["green900"]}.get((tone or "").lower(), default)


def _draw_title(F: _Frame, card: dict, t: float) -> None:
    l1 = (card.get("headline") or "").strip()
    l2 = (card.get("highlight_text") or "").strip()
    if not l1 and not l2:
        return
    mode = ["rise", "words", "pop"][int(card.get("_style", 0)) % 3]
    px1 = F.fit_px(l1, "heavy", 60, 960) if l1 else 60
    if l1:
        cue("swish", 0.0, 0.5)
        if mode == "words":
            # từng chữ trượt lên lần lượt
            x = 540 - F.text_w(l1, "heavy", px1) / 2
            for i, wd in enumerate(l1.split()):
                a = _ease_out_quint(_prog(t, i * 0.07, 0.45))
                F.text(x, 345 + (1 - a) * 30, wd, C["green900"], "heavy", px1, anchor="lm", alpha=a)
                x += F.text_w(wd + " ", "heavy", px1)
        elif mode == "pop":
            a = _spring(_prog(t, 0.0, 0.6))
            F.text(540, 345, l1, C["green900"], "heavy", px1 * (0.7 + 0.3 * a), alpha=_clamp(a * 2))
        else:
            a = _ease_out_quint(_prog(t, 0.0, 0.5))
            F.text(540, 345 + (1 - a) * 26, l1, C["green900"], "heavy", px1, alpha=a)
    if l2:
        # Dòng nhấn "pop" vào sau một nhịp (giống "Thật không?" ở video mẫu)
        cue("pop", 0.45, 0.8)
        q = _prog(t, 0.45, 0.55)
        sc = 0.6 + 0.4 * _spring(q)
        col = _tone_color(card.get("props", {}).get("highlight_tone", ""), C["red"])
        px2 = F.fit_px(l2, "heavy", 60, 960)
        F.text(540, (420 if l1 else 345), l2, col, "heavy", px2 * sc, alpha=_clamp(q * 4))


def _split_subtitle(text: str) -> list[str]:
    """Cắt phụ đề theo cụm ý 4–9 từ."""
    words = text.split()
    if not words:
        return []
    chunks, cur = [], []
    for wd in words:
        cur.append(wd)
        if (len(cur) >= 4 and re.search(r"[,.;:!?…]$", wd)) or len(cur) >= 9:
            chunks.append(" ".join(cur))
            cur = []
    if cur:
        if chunks and len(cur) < 3:
            chunks[-1] += " " + " ".join(cur)
        else:
            chunks.append(" ".join(cur))
    return chunks


def _wrap(F: _Frame, text: str, weight: str, px: float, max_w: float, max_lines: int = 2) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for wd in words:
        trial = (cur + " " + wd).strip()
        if F.text_w(trial, weight, px) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    return lines


SUB_ACCENT = (255, 205, 40)  # vàng: số liệu / % nổi trên nền tối


def _even_words(text: str, duration: float) -> list[dict]:
    """Không có mốc thời gian thật -> rải đều theo độ dài từ trên thời lượng cảnh."""
    ws = text.split()
    if not ws:
        return []
    wts = [1.0 + 0.09 * len(w) for w in ws]
    tot, acc, out = sum(wts), 0.0, []
    for w, k in zip(ws, wts):
        s = acc / tot * duration
        acc += k
        out.append({"w": w, "s": s, "e": acc / tot * duration})
    return out


def _draw_word_pop(F: _Frame, words: list[dict], t: float, dark: bool) -> None:
    """Phụ đề kiểu TikTok: mỗi lần chỉ 1 từ, bật ra đúng lúc giọng đọc tới từ đó."""
    cur = None
    for i, x in enumerate(words):
        if x["s"] <= t:
            cur = i
        else:
            break
    if cur is None:
        return
    x = words[cur]
    nxt = words[cur + 1]["s"] if cur + 1 < len(words) else None
    # Ngắt hơi dài: giữ từ thêm 0.35s rồi mờ dần, không để chữ đứng im giữa khoảng lặng
    hold_end = x["e"] + 0.35
    fade = 1.0
    if t > hold_end and (nxt is None or t < nxt):
        fade = 1.0 - _prog(t, hold_end, 0.15)
        if fade <= 0.02:
            return
    word = re.sub(r"^[\"'“‘(]+|[,.;:…\"'”’)]+$", "", x["w"]) or x["w"]
    k = _clamp((t - x["s"]) / 0.30)
    sc = 0.5 + 0.5 * _spring(k, bounce=0.55, freq=0.9)       # phóng to có nảy (~4 khung hình ở 30fps)
    rise = (1 - _ease_out_cubic(_clamp(k * 2.0))) * 30         # trồi lên nhẹ
    a = _clamp(k * 6) * fade
    accent = bool(re.search(r"\d|%", word))
    px = F.fit_px(word, "black", 112, DESIGN_W * 0.82) * sc
    tw = F.text_w(word, "black", px)
    cy = 1520 + rise  # dưới vùng nội dung cảnh (kết thúc ~1460), trên vùng nút của TikTok/Reels
    pad_x, pad_y = 30 * sc, 18 * sc
    bg = SUB_BG_DARK if dark else SUB_BG_LIGHT
    # Nền + chữ vẽ chung một lớp rồi nhân alpha: mờ dần đúng cả trên khung đặc lẫn overlay trong suốt
    layer = Image.new("RGBA", (F.s(tw + pad_x * 2), F.s(px + pad_y * 2)), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle([0, 0, layer.width - 1, layer.height - 1], radius=F.s(18 * sc), fill=bg)
    col = SUB_ACCENT if accent else (255, 255, 255)
    d.text((layer.width / 2, layer.height / 2), _clean(word), fill=col + (255,),
           font=F.font("black", px), anchor="mm")
    if a < 0.999:
        layer.putalpha(layer.getchannel("A").point(lambda v: int(v * a)))
    F.img.alpha_composite(layer, (F.x(540 - tw / 2 - pad_x), F.y(cy - px / 2 - pad_y)))


def _draw_subtitle(F: _Frame, text: str, t: float, duration: float, dark: bool,
                   words: Optional[list] = None, mode: str = "word") -> None:
    if mode == "word":
        ws = words if words else _even_words(text, duration)
        if ws:
            _draw_word_pop(F, ws, t, dark)
        return
    chunks = _split_subtitle(text)
    if not chunks:
        return
    weights = [len(c.split()) for c in chunks]
    total = sum(weights)
    acc, cur = 0.0, chunks[-1]
    for c, wgt in zip(chunks, weights):
        acc += wgt / total * duration
        if t < acc:
            cur = c
            break
    px = 44
    max_w = DESIGN_W * 0.78 - 32
    lines = _wrap(F, cur, "xbold", px, max_w)
    while len(lines) > 2 or any(F.text_w(ln, "xbold", px) > max_w for ln in lines):
        px *= 0.92
        lines = _wrap(F, cur, "xbold", px, max_w)
        if px < 26:
            break
    lh = px * 1.18
    box_w = max(F.text_w(ln, "xbold", px) for ln in lines) + 36
    box_h = lh * len(lines) + 20
    bottom = 1462
    top = bottom - box_h
    layer = Image.new("RGBA", (F.s(box_w), F.s(box_h)), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle([0, 0, F.s(box_w) - 1, F.s(box_h) - 1], radius=F.s(10),
                                            fill=SUB_BG_DARK if dark else SUB_BG_LIGHT)
    F.img.alpha_composite(layer, (F.x(540 - box_w / 2), F.y(top)))
    for i, ln in enumerate(lines):
        F.text(540, top + 10 + lh * (i + 0.5), ln, (255, 255, 255), "xbold", px)


# =============================================================================
# 6.1 MÁY TÍNH — số trên màn hình được gõ từng ký tự
# =============================================================================
_CALC_KEYS = [["7", "8", "9", "%"], ["4", "5", "6", "×"], ["1", "2", "3", "−"], ["0", ",", "=", "+"]]


def _tpl_calculator(F: _Frame, card: dict, t: float, D: float) -> None:
    p = card.get("props", {})
    display = str(p.get("display") or _first_number(card.get("sub_text", "")) or "0")
    cw, ch = 540, 720
    pad = 40
    # Layer riêng để nghiêng -2°
    layer = Image.new("RGBA", (F.s(cw + 2 * pad), F.s(ch + 2 * pad)), (0, 0, 0, 0))
    L = _Frame(layer, F.S, F.s(pad), F.s(pad))
    L.rrect(0, 0, cw, ch, 40, fill=C["green900"])
    L.rrect(36, 40, cw - 36, 230, 26, fill=C["bg_glow"])

    # Gõ từng ký tự
    t0, per = 0.3, min(0.22, max(0.08, (D * 0.45) / max(1, len(display))))
    n_chars = int(_clamp((t - t0) / per, 0, len(display))) if t >= t0 else 0
    for k in range(len(display)):
        cue("key", t0 + k * per, 0.85)
    shown = display[:n_chars] or "0"
    px = L.fit_px(display, "black", 150, cw - 120)
    L.text(cw - 64, 140, shown, C["green900"], "black", px, anchor="rm", under=C["bg_glow"])

    pressed = None
    if 0 < n_chars <= len(display):
        since = (t - t0) - (n_chars - 1) * per
        if since < 0.14:
            pressed = display[n_chars - 1]
    bw, bh, gx, gy = 99, 84, 20, 20
    kx0, ky0 = 46, 268
    for r, row in enumerate(_CALC_KEYS):
        for c, key in enumerate(row):
            x, y = kx0 + c * (bw + gx), ky0 + r * (bh + gy)
            op = key in ("%", "×", "−", "+", "=")
            col = C["green600"] if op else C["green_key"]
            if pressed is not None and (key == pressed or (key == "−" and pressed == "-")):
                col = _mix((255, 255, 255), col, 0.45)
            L.rrect(x, y, x + bw, y + bh, 18, fill=col)
            L.text(x + bw / 2, y + bh / 2, key, (255, 255, 255), "bold", 40, under=col)
    rot = layer.rotate(2, resample=Image.BICUBIC, expand=True)

    a = _ease_out_back(_prog(t, 0.0, 0.55))
    cy = 905 + (1 - a) * 160
    F.shadow(540 - cw / 2 + 8, cy - ch / 2 + 10, 540 + cw / 2 + 8, cy + ch / 2 + 10, 40, strength=0.22)
    F.img.alpha_composite(rot, (F.x(540) - rot.width // 2, F.y(cy) - rot.height // 2))


# =============================================================================
# 6.2 HOÁ ĐƠN — từng dòng fade + trượt lên, dòng tổng "pop"
# =============================================================================
def _row_layout(F: "_Frame", lbl: str, val: str, inner_w: float, lbl_wt: str, lbl_px: float,
                val_wt: str, val_px: float, gap: float = 28, min_px: float = 30) -> tuple:
    """Dòng 'nhãn ......... giá trị' không bao giờ chồng chữ.
    Giá trị tối đa 45% bề ngang; nhãn co chữ cho vừa phần còn lại, vẫn dài thì xuống 2 dòng.
    -> (các dòng nhãn, cỡ chữ nhãn, cỡ chữ giá trị, độ rộng giá trị)"""
    vpx = F.fit_px(val, val_wt, val_px, inner_w * 0.45, min_px)
    vw = F.text_w(val, val_wt, vpx) if val else 0.0
    room = max(80.0, inner_w - vw - gap)
    lpx = lbl_px
    while lpx > min_px and F.text_w(lbl, lbl_wt, lpx) > room:
        lpx *= 0.94
    lines = [lbl]
    if F.text_w(lbl, lbl_wt, lpx) > room:
        lpx = max(min_px, lbl_px * 0.8)
        lines = _wrap(F, lbl, lbl_wt, lpx, room, max_lines=2)
        while lpx > 22 and any(F.text_w(ln, lbl_wt, lpx) > room for ln in lines):
            lpx *= 0.94
            lines = _wrap(F, lbl, lbl_wt, lpx, room, max_lines=2)
    return lines, lpx, vpx, vw


def _draw_label_lines(F: "_Frame", x: float, cy: float, lines: list, col: tuple, wt: str, px: float,
                      alpha: float, under: tuple) -> None:
    lh = px * 1.12
    y0 = cy - lh * (len(lines) - 1) / 2
    for k, ln in enumerate(lines):
        F.text(x, y0 + k * lh, ln, col, wt, px, anchor="lm", alpha=alpha, under=under)


def _tpl_receipt(F: _Frame, card: dict, t: float, D: float) -> None:
    p = card.get("props", {})
    items = [(str(a), str(b)) for a, b in (p.get("items") or [])][:5]
    total_label = str(p.get("total_label") or "")
    total_value = str(p.get("total_value") or "")
    formula = str(p.get("formula") or "")
    note = str(p.get("note") or "")
    title = str(p.get("title") or "HOÁ ĐƠN").upper()

    events = len(items) + (1 if total_value else 0) + (1 if formula else 0) + (1 if note else 0)
    times = _schedule(events, D)
    l, r, top = 190, 890, 500
    row_h = 84
    inner = (r - 44) - (l + 44)
    lay = [_row_layout(F, lbl, val, inner, "bold", 46, "heavy", 46) for lbl, val in items]
    rows_h = [row_h if len(x[0]) == 1 else row_h + x[1] * 1.12 for x in lay]
    h = 120 + sum(rows_h) + (150 if total_value else 20) + (60 if note else 0) + 40
    bottom = top + h
    a_card = _ease_out_cubic(_prog(t, 0.0, 0.3))
    if a_card <= 0.02:
        return
    F.shadow(l, top, r, bottom, 24, strength=0.12 * a_card)
    # Thẻ trắng + mép răng cưa ở đáy
    F.rrect(l, top, r, bottom - 20, 24, fill=C["card"])
    teeth, tooth = 18, 18
    step = (r - l) / teeth
    pts = [(F.x(l), F.y(bottom - 40))]
    for i in range(teeth):
        pts.append((F.x(l + (i + 0.5) * step), F.y(bottom)))
        pts.append((F.x(l + (i + 1) * step), F.y(bottom - tooth)))
    pts.append((F.x(r), F.y(bottom - 40)))
    F.d.polygon(pts, fill=C["card"])

    F.text(540, top + 52, title, C["muted"], "bold", 26, under=C["card"])
    for x in range(int(l + 40), int(r - 40), 18):
        F.d.line([(F.x(x), F.y(top + 88)), (F.x(x + 9), F.y(top + 88))], fill=C["line_soft"], width=F.s(2))

    for k in range(len(items)):
        cue("pop_soft", times[k], 0.8)
    if total_value:
        cue("riser", times[len(items)] - 0.55, 0.5)
        cue("pop_big", times[len(items)], 0.8)
        cue("ding_soft", times[len(items)] + 0.04, 0.7)
    if formula:
        cue("swish", times[len(items) + (1 if total_value else 0)], 0.6)
    if note:
        cue("pop", times[-1], 0.5)
    y = top + 140
    ev = 0
    for (lbl, val), (lines, lpx, vpx, _), rh_k in zip(items, lay, rows_h):
        a = _ease_out_cubic(_prog(t, times[ev], 0.35))
        dy = (1 - a) * 12
        cy = y + (rh_k - row_h) / 2 + dy
        _draw_label_lines(F, l + 44, cy, lines, C["green900"], "bold", lpx, a, C["card"])
        F.text(r - 44, cy, val, C["red"], "heavy", vpx, anchor="rm", alpha=a, under=C["card"])
        y += rh_k
        ev += 1
    if items and total_value:
        F.d.line([(F.x(l + 36), F.y(y - 34)), (F.x(r - 36), F.y(y - 34))], fill=C["green900"], width=F.s(3))
    if total_value:
        tp = _prog(t, times[ev], 0.45)
        if tp > 0:
            sc = 0.9 + 0.1 * _ease_out_back(tp) if tp < 1 else 1.0
            a = _ease_out_cubic(tp * 2)
            F.rrect(l + 22, y - 18, r - 22, y + 116, 16, fill=_mix(C["red_soft"], C["card"], a))
            px = F.fit_px(total_value, "black", 104 * sc, 380)
            t_lines, t_px, _, _ = _row_layout(F, total_label, "", (r - 46) - (l + 46) - F.text_w(total_value, "black", px),
                                             "heavy", 46, "black", 10, gap=24)
            _draw_label_lines(F, l + 46, y + 49, t_lines, C["green900"], "heavy", t_px, a, C["red_soft"])
            _burst(F, r - 46 - F.text_w(total_value, "black", px) / 2, y + 49, t, times[ev], C["red"],
                   radius=170, under=C["card"])
            F.text(r - 46, y + 49, total_value, C["red"], "black", px, anchor="rm", alpha=a, under=C["red_soft"])
        ev += 1
    if formula:
        a = _ease_out_cubic(_prog(t, times[ev], 0.35))
        F.text(540, bottom + 70 + (1 - a) * 12, formula, C["green600"], "bold",
               F.fit_px(formula, "bold", 42, 960), alpha=a)
        ev += 1
    if note:
        a = _ease_out_back(_prog(t, times[ev], 0.35))
        if a > 0.02:
            nw = F.text_w(note, "semibold", 24) + 36
            ny = y + (138 if total_value else 10)
            F.rrect(r - 40 - nw, ny - 22, r - 40, ny + 22, 22, fill=_mix(C["dot_off"], C["card"], _clamp(a)))
            F.text(r - 40 - nw / 2, ny, note, C["muted"], "semibold", 24, alpha=_clamp(a), under=C["dot_off"])


# =============================================================================
# 6.3 LƯỚI 100 CHẤM — số đếm tăng đồng bộ với số chấm đỏ
# =============================================================================
def _parse_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(str(v).replace(".", "").replace(",", ".")))
    except Exception:
        return default


def _tpl_dots(F: _Frame, card: dict, t: float, D: float) -> None:
    p = card.get("props", {})
    count_txt = str(p.get("count") or _first_number(card.get("sub_text", "")) or "100")
    unit = str(p.get("unit") or "")
    filled = max(0, min(100, _parse_int(p.get("filled", count_txt), 100)))
    tone = _tone_color(p.get("tone", "bad"), C["red"])

    fill_dur = max(0.6, D * 0.7 - 0.3)
    f = _ease_in_out_quad(_prog(t, 0.3, fill_dur))
    step_n = max(1, filled // 18)
    for j in range(step_n, filled + 1, step_n):
        cue("tick", 0.3 + _inv_in_out_quad(j / max(1, filled)) * fill_dur, 0.7)
    cue("ding", 0.3 + fill_dur, 0.8)
    lit = filled * f
    n_lit = int(lit)
    num = _count_up_text(count_txt, f)

    # Số khổng lồ + đơn vị (thay cho tiêu đề)
    px = F.fit_px(count_txt, "black", 210, 560 if unit else 960)
    nw = F.text_w(count_txt, "black", px)
    uw = F.text_w(unit, "bold", 50) if unit else 0
    x0 = 540 - (nw + (24 + uw if unit else 0)) / 2
    F.text(x0 + nw, 400, num, tone, "black", px, anchor="rm")
    if unit:
        F.text(x0 + nw + 24, 420, unit, C["green900"], "bold", 50, anchor="lm")

    step, r0 = 64, 23
    gx = 540 - step * 4.5
    gy = 620
    for i in range(100):
        row, col = divmod(i, 10)
        cx, cy = gx + col * step, gy + row * step
        if i < n_lit:
            color, rr = tone, r0
            age = (lit - i - 1)  # chấm vừa thêm to hơn rồi về 1
            if age < 1.5:
                rr = r0 * (1 + 0.3 * (1 - _clamp(age / 1.5)))
        else:
            color, rr = C["dot_off"], r0
        F.d.ellipse(F.box(cx - rr, cy - rr, cx + rr, cy + rr), fill=color)


# =============================================================================
# 6.4 PHÉP TÍNH + KẾT QUẢ — "khoảnh khắc đập vào mắt"
# =============================================================================
def _tpl_result(F: _Frame, card: dict, t: float, D: float) -> None:
    p = card.get("props", {})
    expr = str(p.get("expression") or "")
    result = str(p.get("result") or _first_number(card.get("sub_text", "")) or "")
    caption = str(p.get("caption") or "")
    top_note = str(p.get("top_note") or "")
    tone = _tone_color(p.get("tone", "bad"), C["red"])

    # Lưới chấm mờ làm nền
    step, r0 = 64, 23
    gx, gy = 540 - step * 4.5, 620
    faded = _mix(tone, C["bg"], 0.18)
    for i in range(100):
        row, col = divmod(i, 10)
        cx, cy = gx + col * step, gy + row * step
        F.d.ellipse(F.box(cx - r0, cy - r0, cx + r0, cy + r0), fill=faded)

    if top_note:
        F.text(540, 400, top_note, tone, "black", F.fit_px(top_note, "black", 90, 960))
    t_expr, t_res, t_cap = _schedule(3, D, 0.25, 0.6)
    cue("swish", t_expr, 0.6)
    cue("riser", t_res - 0.6, 0.7)
    cue("pop_big", t_res, 1.0)
    cue("sparkle", t_res + 0.05, 0.8)
    if caption:
        cue("pop_soft", t_cap, 0.6)
    _burst(F, 540, 1060, t, t_res, tone, n=14, radius=330)
    if expr:
        a = _ease_out_cubic(_prog(t, t_expr, 0.35))
        F.text(540, 820 + (1 - a) * 12, expr, C["green900"], "heavy", F.fit_px(expr, "heavy", 76, 960), alpha=a)
    if result:
        q = _prog(t, t_res, 0.55)
        if q > 0:
            sc = 0.6 + 0.4 * _spring(q)  # 0.6 -> vượt ~1.08 -> 1
            px = F.fit_px(result, "black", 270, 820) * sc
            eq_px = 120 * sc
            rw = F.text_w(result, "black", px)
            ew = F.text_w("=", "heavy", eq_px) + 24
            x0 = 540 - (rw + ew) / 2
            F.text(x0, 1060, "=", C["green900"], "heavy", eq_px, anchor="lm", alpha=_clamp(q * 3))
            F.text(x0 + ew, 1060, result, tone, "black", px, anchor="lm", alpha=_clamp(q * 3))
    if caption:
        a = _ease_out_cubic(_prog(t, t_cap, 0.35))
        F.text(540, 1250, caption, C["green900"], "semibold", F.fit_px(caption, "semibold", 42, 960), alpha=a)


# =============================================================================
# 6.5 BIỂU ĐỒ ĐƯỜNG — đường vẽ dần, nhãn pill hiện sau
# =============================================================================
def _walk(seed_text: str, end: float, n: int = 48) -> list[float]:
    rnd = random.Random(zlib.crc32(seed_text.encode("utf-8")))
    acc, w = 0.0, [0.0]
    for _ in range(n):
        acc += rnd.gauss(0, 1)
        w.append(acc)
    amp = max(4.0, abs(end) * 0.12)
    span = max(1e-6, max(w) - min(w))
    return [end * i / n + (w[i] - w[n] * i / n) / span * amp for i in range(n + 1)]


def _tpl_chart(F: _Frame, card: dict, t: float, D: float) -> None:
    p = card.get("props", {})
    lines = p.get("lines") or []
    lines = [ln for ln in lines if isinstance(ln, dict)][:2]
    if not lines:
        v = _first_number(card.get("sub_text", ""))
        lines = [{"label": v or "", "end_value": _parse_signed(v) if v else 20, "tone": "good"}]
    xl = p.get("x_labels") or ["Đầu kỳ", "", "Cuối kỳ"]
    unit = str(p.get("unit", "%"))

    vals = [float(_parse_signed(ln.get("end_value", 0))) for ln in lines]
    lo = min(-20.0, min(vals) - 10)
    hi = max(40.0, max(vals) + 10)
    stepv = _nice_step((hi - lo) / 4)
    lo, hi = math.floor(lo / stepv) * stepv, math.ceil(hi / stepv) * stepv

    l, r, top, bot = 100, 980, 520, 1300
    a_card = _ease_out_cubic(_prog(t, 0.0, 0.3))
    F.shadow(l, top, r, bot, 28, strength=0.12 * a_card)
    F.rrect(l, top, r, bot, 28, fill=C["card"])
    pl, pr, pt, pb = l + 150, r - 50, top + 60, bot - 110

    def vy(v: float) -> float:
        return pb - (v - lo) / (hi - lo) * (pb - pt)

    v = lo
    while v <= hi + 1e-6:
        yy = vy(v)
        vs = f"{int(v):,}".replace(",", ".") if abs(v) >= 1000 else f"{v:g}".replace(".", ",")
        lbl = ("+" + vs if v > 0 else vs) + unit
        F.text(pl - 24, yy, lbl, C["muted"], "semibold", 26, anchor="rm", under=C["card"])
        if abs(v) < 1e-6:
            for x in range(int(pl), int(pr), 22):
                F.d.line([(F.x(x), F.y(yy)), (F.x(x + 12), F.y(yy))], fill=C["muted"], width=F.s(3))
        else:
            F.d.line([(F.x(pl), F.y(yy)), (F.x(pr), F.y(yy))], fill=C["grid"], width=F.s(2))
        v += stepv
    for i, txt in enumerate(xl[:3]):
        if txt:
            xx = [pl, (pl + pr) / 2, pr][i]
            F.text(xx, bot - 55, str(txt), C["muted"], "semibold", 26, under=C["card"],
                   anchor=["lm", "mm", "rm"][i])

    # Vẽ đường (đường "xấu" vẽ trước để nằm dưới)
    order = sorted(range(len(lines)), key=lambda k: 0 if _tone_color(lines[k].get("tone"), C["green600"]) == C["red"] else 1)
    starts = _schedule(len(lines), D, 0.4, 0.45)
    draw_dur = max(0.8, D * 0.3)
    for st in starts:
        cue("draw", st, 0.9)
        cue("pop", st + draw_dur, 0.7)
    pills = []
    for slot, k in enumerate(order):
        ln = lines[k]
        col = _tone_color(ln.get("tone", "good" if vals[k] >= 0 else "bad"), C["green600"])
        pts_v = _walk(str(ln.get("label", k)), vals[k])
        n = len(pts_v) - 1
        prog = _ease_in_out_quad(_prog(t, starts[slot], draw_dur))
        upto = prog * n
        pts = []
        for i in range(int(upto) + 1):
            pts.append((pl + (pr - pl) * i / n, vy(pts_v[i])))
        if upto < n and int(upto) < n:
            i = int(upto)
            fr = upto - i
            pts.append((pl + (pr - pl) * (i + fr) / n, vy(pts_v[i] + (pts_v[i + 1] - pts_v[i]) * fr)))
        if len(pts) >= 2:
            F.d.line([(F.x(x), F.y(y)) for x, y in pts], fill=col, width=F.s(7), joint="curve")
            ex, ey = pts[-1]
            F.d.ellipse(F.box(ex - 9, ey - 9, ex + 9, ey + 9), fill=col)
        pills.append((ln, col, vy(vals[k]), starts[slot] + draw_dur))

    # Nhãn pill sau khi đường vẽ xong
    pills.sort(key=lambda z: z[2])
    for idx, (ln, col, ey, t_done) in enumerate(pills):
        label = str(ln.get("label", ""))
        if not label:
            continue
        a = _ease_out_back(_prog(t, t_done, 0.35))
        if a <= 0.02:
            continue
        pw = F.text_w(label, "bold", 28) + 40
        py = ey + (-54 if idx == 0 else 54)
        py = min(pb - 10, max(pt + 10, py))
        F.rrect(pr - pw, py - 26, pr, py + 26, 26, fill=_mix(col, C["card"], _clamp(a)))
        F.text(pr - pw / 2, py, label, (255, 255, 255), "bold", 28 * _clamp(0.85 + 0.15 * a, 0.5, 1.1),
               alpha=_clamp(a), under=col)


def _nice_step(raw: float) -> float:
    if raw <= 20:
        return 20.0
    k = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * k:
            return m * k
    return 10 * k


def _parse_signed(v: Any) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"([+\-−]?)\s*(\d+(?:[.,]\d+)?)", str(v))
    if not m:
        return 0.0
    num = float(m.group(2).replace(",", "."))
    return -num if m.group(1) in ("-", "−") else num


# =============================================================================
# 6.6 BẢNG SO SÁNH — các dòng hiện lần lượt, dòng cuối viền đậm mời người xem
# =============================================================================
def _tpl_compare(F: _Frame, card: dict, t: float, D: float) -> None:
    p = card.get("props", {})
    rows = [(str(a), str(b)) for a, b in (p.get("rows") or [])][:5]
    if not rows:
        return
    hl_last = bool(p.get("highlight_last", False))
    tone = _tone_color(p.get("tone", "bad"), C["red"])
    rh, gap = 104, 22
    l, r = 120, 960
    lay = [_row_layout(F, lbl, val, (r - 70) - (l + 70), "bold", 44, "black", 50) for lbl, val in rows]
    rhs = [rh if len(x[0]) == 1 else rh + x[1] * 1.12 for x in lay]
    total_h = sum(rhs) + (len(rows) - 1) * gap + 80
    top = 520 + max(0, (760 - total_h) / 2)
    a0 = _ease_out_cubic(_prog(t, 0.0, 0.3))
    F.shadow(l, top, r, top + total_h, 28, strength=0.12 * a0)
    F.rrect(l, top, r, top + total_h, 28, fill=C["card"])
    times = _schedule(len(rows), D, 0.35, 0.75)
    for i in range(len(rows)):
        cue("ding_soft" if (hl_last and i == len(rows) - 1) else "pop_soft", times[i], 0.8)
    y = top + 40
    for i, ((lbl, val), (lines, lpx, vpx, _), rh_i) in enumerate(zip(rows, lay, rhs)):
        a = _ease_out_cubic(_prog(t, times[i], 0.35))
        if a > 0.02:
            last = hl_last and i == len(rows) - 1
            border = C["green900"] if last else C["line_soft"]
            F.rrect(l + 36, y + (1 - a) * 12, r - 36, y + rh_i + (1 - a) * 12, 18,
                    outline=_mix(border, C["card"], a), width=4 if last else 2)
            cy = y + rh_i / 2 + (1 - a) * 12
            _draw_label_lines(F, l + 70, cy, lines, C["green900"], "bold", lpx, a, C["card"])
            F.text(r - 70, cy, val, C["green900"] if last else tone, "black", vpx, anchor="rm", alpha=a, under=C["card"])
        y += rh_i + gap


# =============================================================================
# CON SỐ KHỔNG LỒ (khi kịch bản có 1 con số quan trọng mà không cần đồ vật khác)
# =============================================================================
def _tpl_bignumber(F: _Frame, card: dict, t: float, D: float) -> None:
    p = card.get("props", {})
    number = str(p.get("number") or _first_number(card.get("sub_text", "")) or "")
    unit = str(p.get("unit") or "")
    caption = str(p.get("caption") or "")
    tone = _tone_color(p.get("tone", "bad"), C["red"])
    if not number:
        return _tpl_statement(F, card, t, D)
    q = _prog(t, 0.3, 0.7)
    sc = 0.6 + 0.4 * _spring(q)
    count_dur = max(0.6, D * 0.35)
    count = _ease_in_out_quad(_prog(t, 0.3, count_dur))
    px = F.fit_px(number, "black", 300, 960)
    cue("pop_big", 0.3, 0.9)
    if _NUM_RE.match(number.strip()):
        for j in range(1, 10):
            cue("tick", 0.3 + _inv_in_out_quad(j / 10) * count_dur, 0.55)
    cue("ding", 0.3 + count_dur, 0.8)
    _burst(F, 540, 860, t, 0.3 + count_dur, tone, n=14, radius=340)
    F.text(540, 860, _count_up_text(number, count), tone, "black", px * sc, alpha=_clamp(q * 3))
    t_u, t_c = _schedule(2, D, 0.9, 0.6)
    if unit:
        a = _ease_out_cubic(_prog(t, t_u, 0.35))
        F.text(540, 1060 + (1 - a) * 12, unit, C["green900"], "bold", F.fit_px(unit, "bold", 54, 960), alpha=a)
    if caption:
        a = _ease_out_cubic(_prog(t, t_c, 0.35))
        F.text(540, 1160 + (1 - a) * 12, caption, C["muted"], "semibold", F.fit_px(caption, "semibold", 40, 960), alpha=a)


# =============================================================================
# CÂU NHẤN (cảnh không có số liệu): thẻ trắng + vạch nhấn chạy dưới từ khoá
# =============================================================================
def _tpl_statement(F: _Frame, card: dict, t: float, D: float) -> None:
    p = card.get("props", {})
    text = str(p.get("text") or card.get("sub_text") or "").strip()
    keyword = str(p.get("keyword") or "").strip()
    tone = _tone_color(p.get("tone", "bad"), C["red"])
    if not text:
        return
    px = 66
    lines = _wrap(F, text, "heavy", px, 780, max_lines=4)
    while len(lines) > 4 and px > 40:
        px *= 0.92
        lines = _wrap(F, text, "heavy", px, 780, max_lines=4)
    lh = px * 1.3
    h = lh * len(lines) + 140
    top = 900 - h / 2
    a0 = _ease_out_cubic(_prog(t, 0.0, 0.3))
    F.shadow(110, top, 970, top + h, 28, strength=0.12 * a0)
    F.rrect(110, top, 970, top + h, 28, fill=C["card"])
    F.rrect(110, top, 126, top + h, 8, fill=tone)
    times = _schedule(len(lines), D, 0.3, 0.55)
    kw_low = keyword.lower()
    for i, ln in enumerate(lines):
        cue("swish", times[i], 0.45)
        if kw_low and kw_low in ln.lower():
            cue("marker", times[i] + 0.4, 0.9)
    for i, ln in enumerate(lines):
        a = _ease_out_cubic(_prog(t, times[i], 0.35))
        y = top + 70 + lh * (i + 0.5) + (1 - a) * 12
        F.text(170, y, ln, C["green900"], "heavy", px, anchor="lm", alpha=a, under=C["card"])
        if kw_low and kw_low in ln.lower() and a > 0.5:
            pre = ln[: ln.lower().index(kw_low)]
            x1 = 170 + F.text_w(pre, "heavy", px)
            kw_w = F.text_w(ln[len(pre): len(pre) + len(keyword)], "heavy", px)
            g = _ease_out_cubic(_prog(t, times[i] + 0.4, 0.4))
            F.rrect(x1, y + px * 0.52, x1 + kw_w * g, y + px * 0.52 + 9, 4, fill=tone)


# =============================================================================
# 6.7 CẢNH KẾT NỀN TỐI — 2–3 dòng, dòng cuối xanh, tick vẽ nét, thương hiệu
# =============================================================================
def _tpl_climax(F: _Frame, card: dict, t: float, D: float) -> None:
    p = card.get("props", {})
    lines = [str(x) for x in (p.get("lines") or []) if str(x).strip()][:3]
    if not lines:
        lines = [x for x in [card.get("headline", ""), card.get("highlight_text", "")] if x] or \
                _wrap(F, card.get("sub_text", ""), "heavy", 90, 900, 3)
    under = _mix(C["end_top"], C["end_bottom"], 0.5)
    times = _schedule(len(lines), D, 0.2, 0.35)
    y0 = 760 - (len(lines) - 1) * 60
    for i, ln in enumerate(lines):
        a = _ease_out_cubic(_prog(t, times[i], 0.4))
        col = C["green400"] if i == len(lines) - 1 and len(lines) > 1 else (255, 255, 255)
        F.text(540, y0 + i * 120 + (1 - a) * 14, ln, col, "heavy", F.fit_px(ln, "heavy", 92, 960), alpha=a, under=under)

    # Tick vẽ nét
    tt = times[-1] + 0.45
    for st in times:
        cue("swish", st, 0.5)
    cue("chime", tt + 0.2, 0.9)
    cue("pop_soft", tt + 0.5, 0.5)
    _burst(F, 540, 1130, t, tt + 0.2, C["green400"], n=12, radius=150, under=under)
    a = _spring(_prog(t, tt, 0.6))
    if a > 0.02:
        cx, cy, rr = 540, 1130, 62 * _clamp(a, 0, 1.1)
        F.d.ellipse(F.box(cx - rr, cy - rr, cx + rr, cy + rr), fill=C["green600"])
        k = _ease_out_cubic(_prog(t, tt + 0.25, 0.4))
        p1, p2, p3 = (cx - 26, cy + 2), (cx - 6, cy + 22), (cx + 28, cy - 20)
        pts = [p1]
        if k < 0.4:
            f = k / 0.4
            pts.append((p1[0] + (p2[0] - p1[0]) * f, p1[1] + (p2[1] - p1[1]) * f))
        else:
            f = (k - 0.4) / 0.6
            pts += [p2, (p2[0] + (p3[0] - p2[0]) * f, p2[1] + (p3[1] - p2[1]) * f)]
        if k > 0:
            F.d.line([(F.x(x), F.y(y)) for x, y in pts], fill=(255, 255, 255), width=F.s(11), joint="curve")

    badge = card.get("badge") or "QuantVN · Research Lab"
    brand, _, tagline = badge.partition("·")
    brand = str(p.get("brand") or brand.strip() or "QuantVN")
    tagline = str(p.get("tagline") or tagline.strip())
    disclaimer = str(p.get("disclaimer", "Số liệu là ví dụ minh hoạ"))
    a = _ease_out_cubic(_prog(t, tt + 0.5, 0.4))
    F.text(540, 1280, brand, (255, 255, 255), "heavy", 56, alpha=a, under=under)
    F.text(540, 1336, tagline, _mix(C["green400"], under, 0.75), "semibold", 30, alpha=a, under=under)
    if disclaimer:
        F.text(540, 1790, disclaimer, (120, 140, 132), "medium", 24, alpha=a * 0.8, under=C["end_bottom"])


_DRAWERS = {
    "calculator": _tpl_calculator, "receipt": _tpl_receipt, "dots": _tpl_dots, "result": _tpl_result,
    "chart": _tpl_chart, "compare": _tpl_compare, "bignumber": _tpl_bignumber,
    "statement": _tpl_statement, "climax": _tpl_climax,
}
_NO_TITLE = {"dots", "result", "climax"}


# =============================================================================
# RENDER
# =============================================================================
def render_frame(card: dict, t: float, duration: float, w: int, h: int,
                 progress: float = 0.0, is_final: bool = False) -> Image.Image:
    tpl = card.get("template", "statement")
    dark = tpl == "climax"
    img = _background(w, h, dark).copy()
    S = min(w / DESIGN_W, h / DESIGN_H)
    F = _Frame(img, S, (w - DESIGN_W * S) / 2, (h - DESIGN_H * S) / 2)
    style_i = int(card.get("_style", 0))
    _draw_ambient(F, t, dark, style_i)
    if not dark:
        _draw_chrome(F, card, t, progress)
        if tpl not in _NO_TITLE:
            _draw_title(F, card, t)

    # Vật thể minh hoạ vẽ trên lớp riêng để mỗi cảnh có một kiểu xuất hiện khác nhau
    entrance = card.get("_entrance") or ENTRANCES[style_i % len(ENTRANCES)]
    if dark:
        cue("whoosh_soft", 0.0, 0.6)
        _DRAWERS.get(tpl, _tpl_statement)(F, card, t, duration)
    else:
        cue("thud" if entrance == "drop" else "whoosh_soft", 0.28 if entrance == "drop" else 0.0, 0.7)
        if t < 1.0:
            layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
            L = _Frame(layer, S, F.ox, F.oy)
            _DRAWERS.get(tpl, _tpl_statement)(L, card, t, duration)
            dx, dy, sx, sy, rot, a = _entrance(entrance, t)
            _apply_layer(img, layer, (dx * S, dy * S, sx, sy, rot, a), (F.x(540), F.y(900)))
        else:
            _DRAWERS.get(tpl, _tpl_statement)(F, card, t, duration)
    _draw_subtitle(F, card.get("sub_text", ""), t, duration, dark,
                   words=card.get("_words"), mode=card.get("_sub_mode", "word"))

    out = img.convert("RGB")
    fade = 1.0
    if dark:
        fade = _ease_out_cubic(_prog(t, 0.0, 0.5))           # cross-fade vào cảnh kết
        if is_final:
            fade *= 1.0 - _prog(t, duration - 1.0, 1.0)      # fade to black ~1s
    if fade < 0.999:
        out = Image.eval(out, lambda v: int(v * fade))
    return out


def normalize_card(card: dict, slot_index: int = 0, excerpt: str = "") -> dict:
    """Chuẩn hoá card: template hợp lệ, có props, có phụ đề. Thiếu dữ liệu -> 'statement' (không bịa số)."""
    c = dict(card or {})
    tpl = str(c.get("template", "")).lower().strip()
    if tpl not in TEMPLATES:
        tpl = "statement"
    props = c.get("props") if isinstance(c.get("props"), dict) else {}
    # tương thích kiểu cũ: items / metric_number / metric_label ở cấp ngoài
    if c.get("items") and "items" not in props:
        props["items"] = c["items"]
    if c.get("metric_number") and "count" not in props:
        props["count"] = c["metric_number"]
    if c.get("metric_label") and "unit" not in props:
        props["unit"] = c["metric_label"]
    c["sub_text"] = str(c.get("sub_text") or excerpt or "")
    required = {"receipt": "items", "compare": "rows", "result": "result", "chart": "lines"}
    if tpl in required and not props.get(required[tpl]):
        tpl = "bignumber" if _first_number(c["sub_text"]) else "statement"
    if tpl == "statement" and not props.get("text"):
        props["text"] = c.get("headline") or c["sub_text"]
        if tpl == "statement" and c.get("headline"):
            # headline đã dùng làm nội dung thẻ, không lặp lại ở tiêu đề
            c["headline"], c["highlight_text"] = "", c.get("highlight_text", "")
    c["template"] = tpl
    c["props"] = props
    c.setdefault("badge", "QuantVN · Research Lab")
    c.setdefault("tag", "Ví dụ minh hoạ")
    return c


def render_text_effect_clip(
    badge: str = "",
    headline: str = "",
    sub_text: str = "",
    theme: str = "emerald",
    w: int = 1080,
    h: int = 1920,
    duration: float = 4.0,
    out_clip_path: Optional[Path] = None,
    fps: int = 30,
    icon: str = "",
    scene_style: int = 0,
    template: str = "",
    highlight_text: str = "",
    tag: str = "Ví dụ minh hoạ",
    items: Optional[list] = None,
    metric_number: Optional[str] = None,
    metric_label: Optional[str] = None,
    card: Optional[dict] = None,
    props: Optional[dict] = None,
    progress_start: float = 0.0,
    progress_end: float = 1.0,
    is_final: bool = False,
    sfx_events: Optional[list] = None,
) -> Path:
    """Render 1 cảnh minh hoạ thành MP4 (không audio). Ưu tiên tham số `card`.
    Nếu truyền `sfx_events` (list), các cue âm thanh (giây trong clip, tên, gain) được thêm vào đó."""
    if out_clip_path is None:
        raise ValueError("out_clip_path is required")
    base = dict(card or {})
    for k, v in (("badge", badge), ("headline", headline), ("sub_text", sub_text), ("template", template),
                 ("highlight_text", highlight_text), ("items", items), ("metric_number", metric_number),
                 ("metric_label", metric_label)):
        if v and not base.get(k):
            base[k] = v
    if props:
        base["props"] = {**(base.get("props") or {}), **props}
    base.setdefault("tag", tag)
    if not base.get("template"):
        base["template"] = TEMPLATES[scene_style % (len(TEMPLATES) - 1)]
    c = normalize_card(base, scene_style)
    c.setdefault("_style", scene_style)

    out_clip_path = Path(out_clip_path)
    out_clip_path.parent.mkdir(parents=True, exist_ok=True)
    w, h = (w // 2) * 2, (h // 2) * 2
    total_frames = max(1, int(round(duration * fps)))
    cmd = [
        get_ffmpeg_bin("ffmpeg"), "-y", "-loglevel", "error",
        "-f", "rawvideo", "-vcodec", "rawvideo", "-s", f"{w}x{h}", "-pix_fmt", "rgb24", "-r", str(fps), "-i", "-",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(fps),
        str(out_clip_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    rec = SfxRecorder()
    set_recorder(rec)
    try:
        for i in range(total_frames):
            t = i / fps
            rec.t = t
            prog = progress_start + (progress_end - progress_start) * (t / max(1e-6, duration))
            frame = render_frame(c, t, duration, w, h, prog, is_final)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        if sfx_events is not None:
            sfx_events.extend(rec.list())
        err = proc.stderr.read().decode("utf-8", "ignore")
        if proc.wait() != 0:
            raise RuntimeError(f"ffmpeg lỗi khi render cảnh minh hoạ: {err[-500:]}")
        return out_clip_path
    except Exception:
        if proc.poll() is None:
            proc.kill()
        raise
    finally:
        set_recorder(None)


# =============================================================================
# PHÂN ĐOẠN THỜI GIAN THEO LỜI THOẠI
# =============================================================================
def build_segment_slots(speech_text: str, total_dur: float, interval: float = 5.0,
                        parity: Optional[int] = None, timeline: Optional[list] = None) -> list[dict[str, Any]]:
    """Chia video thành các shot đều nhau (giống avatar_director). parity=1: shot lẻ (cảnh minh hoạ),
    parity=0: shot chẵn (cảnh người nói), None: tất cả. Mỗi slot kèm đoạn lời thoại đang đọc."""
    seg_interval = max(2.5, float(interval))
    num_segments = max(1, int(round(total_dur / seg_interval)))
    seg = total_dur / num_segments
    words = speech_text.strip().split()
    n = len(words)
    slots: list[dict[str, Any]] = []
    for i in range(num_segments):
        if parity is not None and i % 2 != parity:
            continue
        st, et = i * seg, (i + 1) * seg
        ws = max(0, int(st / total_dur * n))
        we = min(n, max(ws + 1, int(round(et / total_dur * n))))
        slots.append({
            "slot_index": len(slots),
            "segment_index": i,
            "start_sec": st,
            "end_sec": et,
            "duration": seg,
            "time_range": f"{int(st // 60):02d}:{int(st % 60):02d} - {int(et // 60):02d}:{int(et % 60):02d}",
            # Có mốc thời gian thật từ audio -> lấy đúng các từ được đọc trong shot
            "spoken_excerpt": (" ".join(x["w"] for x in timeline if st - 1e-6 <= x["s"] < et)
                               if timeline else " ".join(words[ws:we])),
            "is_last_segment": i == num_segments - 1,
        })
    return slots


def build_timed_cutaway_slots(speech_text: str, total_dur: float, interval: float = 5.0,
                              timeline: Optional[list] = None) -> list[dict[str, Any]]:
    """Mốc (start_sec, end_sec) của các cảnh cắt (shot lẻ) + đoạn lời thoại đang đọc lúc đó."""
    return build_segment_slots(speech_text, total_dur, interval, parity=1, timeline=timeline)


# =============================================================================
# CLAUDE: biến kịch bản của người dùng thành các card minh hoạ
# =============================================================================
TEMPLATE_SPEC = """\
Các template (STYLE cố định, bạn chỉ cung cấp NỘI DUNG qua "props"):
1. "calculator" – máy tính, số trên màn hình được gõ từng ký tự. Dùng cho cảnh mở đầu nêu 1 con số.
   props: {"display": "<số hiển thị, vd '1%'>"}
2. "receipt" – hoá đơn răng cưa bóc tách các khoản cộng dồn thành 1 tổng.
   props: {"title": "<TIÊU ĐỀ NHỎ IN HOA>", "items": [["<nhãn>", "<giá trị>"], ...2-4 dòng],
           "total_label": "<nhãn dòng tổng>", "total_value": "<giá trị tổng>",
           "formula": "<công thức xanh, vd 'a + b = c'>", "note": "<tag nhỏ tuỳ chọn>"}
3. "dots" – lưới 100 chấm tô dần + con số khổng lồ đếm tăng (tần suất, số lần, tỉ lệ x/100).
   props: {"count": "<số hiển thị>", "unit": "<đơn vị, vd 'lần / năm'>", "filled": <số chấm tô 0-100>, "tone": "bad|good"}
4. "result" – phép tính + kết quả pop lớn (khoảnh khắc 'đập vào mắt').
   props: {"top_note": "<tuỳ chọn>", "expression": "<vd 'a × b'>", "result": "<kết quả>", "caption": "<1 câu ngắn giải thích>", "tone": "bad|good"}
5. "chart" – biểu đồ đường so sánh, vẽ dần, nhãn pill ở cuối đường.
   props: {"lines": [{"label": "<nhãn pill>", "end_value": <số, đơn vị theo unit>, "tone": "good|bad"}, ...tối đa 2],
           "unit": "%", "x_labels": ["<đầu>", "<giữa>", "<cuối>"]}
6. "compare" – bảng so sánh các mức, dòng hiện lần lượt; dòng cuối có thể viền đậm mời người xem tự điền.
   props: {"rows": [["<nhãn>", "<giá trị>"], ...2-4], "highlight_last": true|false, "tone": "bad|good"}
7. "bignumber" – một con số khổng lồ đếm tăng + đơn vị + chú thích.
   props: {"number": "<số>", "unit": "<đơn vị>", "caption": "<chú thích ngắn>", "tone": "bad|good"}
8. "statement" – thẻ câu nhấn khi đoạn thoại KHÔNG có số liệu.
   props: {"text": "<câu ngắn ≤ 14 từ>", "keyword": "<cụm từ trong text được gạch chân>", "tone": "bad|good"}
9. "climax" – cảnh kết nền tối, 2-3 dòng chữ lớn (dòng cuối xanh) + dấu tick + thương hiệu. Chỉ dùng cho câu chốt cuối.
   props: {"lines": ["<dòng 1>", "<dòng 2>", "<dòng 3>"], "disclaimer": "<vd 'Số liệu là ví dụ minh hoạ' hoặc ''>"}
"""


def _first_number(text: str) -> str:
    m = re.search(r"[+\-−]?\d+(?:[.,]\d+)*\s?%?", text or "")
    return m.group(0).replace(" ", "") if m else ""


def _extract_json_array(raw: str) -> list:
    raw = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
    if m:
        raw = m.group(1)
    s, e = raw.find("["), raw.rfind("]")
    if s == -1 or e == -1:
        raise ValueError("Không tìm thấy JSON array")
    return json.loads(raw[s: e + 1])


def _build_prompt(speech_text: str, slots: list[dict], goal: str) -> str:
    slot_lines = "\n".join(
        f"- Cảnh #{s['slot_index'] + 1} [{s['time_range']}, dài {s.get('duration', s['end_sec'] - s['start_sec']):.1f}s]"
        f"{' (cảnh CUỐI video)' if s.get('is_last_segment') else ''}: lời đang đọc: \"{s.get('spoken_excerpt', '')}\""
        for s in slots
    )
    return f"""Bạn là đạo diễn hình ảnh của kênh QuantVN · Research Lab.
Video gồm người dẫn nói, xen kẽ các CẢNH MINH HOẠ không có người. Nhiệm vụ của bạn: thiết kế NỘI DUNG
cho từng cảnh minh hoạ, bám sát lời thoại của CHÍNH kịch bản dưới đây.

Phong cách hình ảnh đã cố định sẵn trong code (infographic phẳng, nền kem xanh, chữ xanh đậm, số đỏ lớn).
Bạn KHÔNG cần mô tả style; bạn chỉ chọn template phù hợp và điền dữ liệu.

QUAN TRỌNG:
- Mọi con số, nhãn, câu chữ phải lấy từ kịch bản này. KHÔNG dùng lại nội dung ví dụ nào khác
  (vd đừng tự đưa vào "phí 0,15%", "100 vòng/năm" nếu kịch bản không nói vậy).
- Không bịa số liệu. Đoạn không có số thì dùng "statement" (hoặc "chart" định tính nếu nói về xu hướng).
- Mỗi cảnh chỉ 1 vật thể minh hoạ + 1 con số/ý trọng tâm. Đỏ = chi phí/rủi ro/mất; xanh = tốt/đúng/lãi.
- Số viết kiểu Việt: dấu phẩy thập phân (0,4%), dấu chấm hàng nghìn (1.000.000).
- Các cảnh nên nối nhau thành một mạch: mở vấn đề → phân rã → nhân quy mô → chứng minh → chốt.
  Tránh dùng cùng một template cho 2 cảnh liền nhau nếu có lựa chọn khác hợp lý.
- "climax" chỉ dùng cho cảnh cuối cùng (nếu đó là câu chốt).

{TEMPLATE_SPEC}
Trường chung của mỗi cảnh:
- "template": một trong các tên ở trên
- "headline": tiêu đề cảnh, 3-7 từ (bỏ trống với dots/result/climax)
- "highlight_text": dòng nhấn thứ 2 (tuỳ chọn, ngắn)
- "props": theo template
- "sub_text": phụ đề = nguyên văn đoạn lời đang đọc (sửa nhẹ cho đúng câu, không thêm ý)

{"Mục tiêu video: " + goal if goal else ""}
TOÀN BỘ KỊCH BẢN:
\"\"\"{speech_text}\"\"\"

CÁC CẢNH MINH HOẠ CẦN THIẾT KẾ ({len(slots)} cảnh):
{slot_lines}

Chỉ trả về một JSON array gồm đúng {len(slots)} object theo thứ tự trên, không viết gì thêm."""


def _fallback_card(slot: dict, is_last: bool, speech_text: str = "") -> dict:
    """Không có API: dựng card từ CHÍNH đoạn lời thoại (không dùng nội dung video mẫu)."""
    ex = (slot.get("spoken_excerpt") or "").strip()
    # Đoạn cắt theo thời gian thường hụt đầu/cuối câu -> lấy lại câu đầy đủ trong kịch bản
    # có nhiều từ chung với đoạn đang đọc nhất (ưu tiên câu nằm gọn trong đoạn).
    sentences = [x.strip() for x in re.split(r"(?<=[.!?…])\s+", speech_text or ex) if x.strip()]
    ex_words = ex.lower().split()

    def overlap(sent: str) -> float:
        sw = sent.lower().split()
        common = sum(1 for wd in sw if wd in ex_words)
        return common + (0.5 if " ".join(sw[:3]) in " ".join(ex_words) else 0)

    core = max(sentences or [ex], key=overlap)
    nums = [n.replace(" ", "") for n in re.findall(r"[+\-−]?\d+(?:[.,]\d+)*\s?%?", core)]
    if is_last:
        words = core.rstrip(".!?…").split()
        k = max(1, math.ceil(len(words) / 3))
        lines = [" ".join(words[i:i + k]) for i in range(0, len(words), k)][:3]
        return {"template": "climax", "props": {"lines": lines, "disclaimer": ""}, "sub_text": ex}
    if nums:
        # Câu có nhiều số: số cuối thường là kết quả, câu gốc làm chú thích
        num = nums[-1]
        props = {"number": num, "unit": _unit_after(core, num)}
        if len(nums) > 1:
            props["caption"] = core.rstrip(".")
        return {"template": "bignumber", "props": props, "sub_text": ex}
    return {"template": "statement", "props": {"text": " ".join(core.split()[:14])}, "sub_text": ex}


def _unit_after(text: str, num: str) -> str:
    m = re.search(re.escape(num.rstrip("%")) + r"%?\s+([^\s,.!?]+)", text)
    return m.group(1) if m and "%" not in num else ""


def generate_text_cards_with_claude(
    speech_text: str,
    num_cards: int,
    timed_slots: Optional[list[dict[str, Any]]] = None,
    anthropic_key: Optional[str] = None,
    demo_mode: bool = False,
    goal: str = "",
) -> list[dict[str, Any]]:
    """Sinh card minh hoạ cho từng cảnh cắt, nội dung lấy từ kịch bản của người dùng."""
    num_cards = max(1, num_cards)
    if not timed_slots:
        words = speech_text.strip().split()
        n = len(words)
        timed_slots = [{
            "slot_index": i, "segment_index": i * 2 + 1, "start_sec": i * 5.0, "end_sec": (i + 1) * 5.0,
            "duration": 5.0, "time_range": f"Cảnh #{i + 1}",
            "spoken_excerpt": " ".join(words[int(i / num_cards * n): int((i + 1) / num_cards * n)]),
            "is_last_segment": i == num_cards - 1,
        } for i in range(num_cards)]
    slots = timed_slots[:num_cards]

    if not demo_mode and anthropic_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=anthropic_key)
            prompt = _build_prompt(speech_text, slots, goal)
            preferred = os.getenv("TEXT_EFFECT_MODEL") or os.getenv("BRAIN_MODEL") or "claude-opus-5-5"
            for model_name in dict.fromkeys([preferred, "claude-opus-5-5", "claude-sonnet-5-5"]):
                try:
                    res = client.messages.create(
                        model=model_name, max_tokens=6000,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    raw = "".join(getattr(b, "text", "") for b in res.content if getattr(b, "type", "") == "text")
                    cards = _extract_json_array(raw)
                    if isinstance(cards, list) and cards:
                        out = []
                        for i, s in enumerate(slots):
                            c = cards[i] if i < len(cards) and isinstance(cards[i], dict) else _fallback_card(
                                s, bool(s.get("is_last_segment")), speech_text)
                            if c.get("template") == "climax" and not s.get("is_last_segment"):
                                c["template"] = "statement"
                                c.setdefault("props", {})["text"] = " ".join(
                                    (c.get("props", {}).get("lines") or [c.get("sub_text", "")]))
                            out.append(normalize_card(c, i, s.get("spoken_excerpt", "")))
                        return out
                except Exception:
                    continue
        except Exception:
            pass

    return [normalize_card(_fallback_card(s, bool(s.get("is_last_segment")), speech_text), i,
                           s.get("spoken_excerpt", ""))
            for i, s in enumerate(slots)]
