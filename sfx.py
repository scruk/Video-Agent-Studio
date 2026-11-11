"""Sound effect tổng hợp bằng code (không cần file âm thanh, không vướng bản quyền).

Luồng:
1. Lúc render frame, các template gọi `cue("pop", at=1.2)` đúng mốc animation bắt đầu.
   `SfxRecorder` chỉ ghi nhận cue khi frame đã chạy tới mốc đó -> âm thanh khớp hình tuyệt đối.
2. avatar_director cộng offset của từng shot, thêm whoosh ở mỗi lần chuyển cảnh.
3. `render_sfx_track()` trộn tất cả thành 1 file WAV, rồi FFmpeg trộn nhỏ dưới giọng đọc.
"""
from __future__ import annotations

import math
import os
import wave
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

SR = 48000


# =============================================================================
# GHI CUE TRONG LÚC RENDER
# =============================================================================
class SfxRecorder:
    def __init__(self) -> None:
        self.t = 0.0
        self.events: dict[tuple[str, float], float] = {}

    def add(self, name: str, at: float, gain: float = 1.0) -> None:
        if at <= self.t + 1e-6 and at >= -0.5:
            key = (name, round(at, 3))
            self.events.setdefault(key, gain)

    def list(self, offset: float = 0.0) -> list[tuple[float, str, float]]:
        return sorted((at + offset, name, g) for (name, at), g in self.events.items())


_ACTIVE: Optional[SfxRecorder] = None


def set_recorder(rec: Optional[SfxRecorder]) -> None:
    global _ACTIVE
    _ACTIVE = rec


def cue(name: str, at: float, gain: float = 1.0) -> None:
    """Gọi trong hàm vẽ: 'tại giây `at` của clip có hiệu ứng `name`'."""
    if _ACTIVE is not None:
        _ACTIVE.add(name, at, gain)


# =============================================================================
# TỔNG HỢP ÂM THANH
# =============================================================================
def _t(dur: float) -> np.ndarray:
    return np.arange(int(SR * dur)) / SR


def _env(n: int, attack: float, decay_tau: float) -> np.ndarray:
    t = np.arange(n) / SR
    a = np.clip(t / max(1e-4, attack), 0, 1)
    return a * np.exp(-np.maximum(0, t - attack) / decay_tau)


def _lowpass(x: np.ndarray, cutoff) -> np.ndarray:
    """One-pole lowpass, cutoff có thể thay đổi theo thời gian (mảng Hz)."""
    cut = np.broadcast_to(np.asarray(cutoff, dtype=float), x.shape)
    alpha = 1 - np.exp(-2 * np.pi * cut / SR)
    y = np.empty_like(x)
    acc = 0.0
    for i in range(len(x)):
        acc += alpha[i] * (x[i] - acc)
        y[i] = acc
    return y


def _noise(n: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(n)


def _norm(x: np.ndarray, peak: float) -> np.ndarray:
    m = np.max(np.abs(x)) or 1.0
    return x / m * peak


def _sweep(f0: float, f1: float, dur: float, curve: float = 1.0) -> np.ndarray:
    t = _t(dur)
    f = f0 + (f1 - f0) * (t / dur) ** curve
    return np.sin(2 * np.pi * np.cumsum(f) / SR)


@lru_cache(maxsize=None)
def sound(name: str) -> np.ndarray:
    if name == "pop":  # bong bóng nảy
        s = _sweep(900, 260, 0.09, 0.5) * _env(int(SR * 0.09), 0.002, 0.03)
        return _norm(s, 0.8)
    if name == "pop_soft":
        s = _sweep(700, 380, 0.07, 0.6) * _env(int(SR * 0.07), 0.003, 0.025)
        return _norm(s, 0.45)
    if name == "pop_big":  # số khổng lồ xuất hiện
        n = int(SR * 0.35)
        body = _sweep(220, 70, 0.35, 0.4) * _env(n, 0.003, 0.09)
        click = _lowpass(_noise(n, 3), 3000) * _env(n, 0.001, 0.008)
        return _norm(body + 0.6 * click, 0.95)
    if name in ("whoosh", "whoosh_soft", "swish"):
        dur = {"whoosh": 0.42, "whoosh_soft": 0.5, "swish": 0.22}[name]
        n = int(SR * dur)
        x = np.linspace(0, 1, n)
        cutoff = 300 + 4200 * np.sin(np.pi * x) ** 2
        s = _lowpass(_noise(n, 7 if name != "swish" else 11), cutoff)
        s = s - _lowpass(s, 180)
        env = np.sin(np.pi * x) ** (1.6 if name != "swish" else 1.0)
        peak = {"whoosh": 0.55, "whoosh_soft": 0.3, "swish": 0.4}[name]
        return _norm(s * env, peak)
    if name == "tick":
        n = int(SR * 0.03)
        s = np.sin(2 * np.pi * 3200 * _t(0.03)) * _env(n, 0.0005, 0.006)
        return _norm(s, 0.35)
    if name == "key":  # phím máy tính
        n = int(SR * 0.05)
        t = _t(0.05)
        s = (np.sin(2 * np.pi * 1850 * t) + 0.5 * np.sin(2 * np.pi * 3300 * t)) * _env(n, 0.0005, 0.012)
        s += 0.4 * _lowpass(_noise(n, 5), 5000) * _env(n, 0.0003, 0.004)
        return _norm(s, 0.5)
    if name in ("ding", "ding_soft"):
        dur = 1.1
        t = _t(dur)
        base = 1318.5 if name == "ding" else 987.8
        s = sum(a * np.sin(2 * np.pi * base * m * t) * np.exp(-t / tau)
                for a, m, tau in [(1, 1, 0.45), (0.5, 2.0, 0.25), (0.25, 2.76, 0.15), (0.15, 5.4, 0.08)])
        s *= np.clip(t / 0.002, 0, 1)
        return _norm(s, 0.5 if name == "ding" else 0.32)
    if name == "chime":  # 2 nốt đi lên - thành công
        out = np.zeros(int(SR * 1.3))
        for k, (f, st) in enumerate([(1046.5, 0.0), (1568.0, 0.13)]):
            t = _t(1.1)
            s = (np.sin(2 * np.pi * f * t) + 0.35 * np.sin(2 * np.pi * f * 2 * t)) * np.exp(-t / 0.35)
            s *= np.clip(t / 0.003, 0, 1)
            i = int(st * SR)
            out[i:i + len(s)] += s[: len(out) - i]
        return _norm(out, 0.5)
    if name == "thud":
        n = int(SR * 0.16)
        s = _sweep(140, 55, 0.16, 0.5) * _env(n, 0.002, 0.05)
        return _norm(s, 0.6)
    if name == "draw":  # bút vẽ đường biểu đồ (~1s, lặp được)
        n = int(SR * 1.0)
        s = _noise(n, 13)
        s = s - _lowpass(s, 2500)
        s = _lowpass(s, 7000)
        wob = 0.6 + 0.4 * np.sin(2 * np.pi * 9 * _t(1.0)) ** 2
        env = np.clip(_t(1.0) / 0.05, 0, 1) * np.clip((1.0 - _t(1.0)) / 0.15, 0, 1)
        return _norm(s * wob * env, 0.16)
    if name == "marker":  # vạch highlight
        n = int(SR * 0.3)
        s = _noise(n, 17)
        s = _lowpass(s - _lowpass(s, 1200), 6000)
        env = np.sin(np.pi * np.linspace(0, 1, n)) ** 0.7
        return _norm(s * env, 0.22)
    if name == "sparkle":
        out = np.zeros(int(SR * 0.6))
        rng = np.random.default_rng(23)
        for k in range(6):
            f = rng.uniform(2600, 5200)
            t = _t(0.25)
            s = np.sin(2 * np.pi * f * t) * np.exp(-t / 0.05)
            i = int((k * 0.06 + rng.uniform(0, 0.02)) * SR)
            out[i:i + len(s)] += s[: len(out) - i] * (1 - k * 0.12)
        return _norm(out, 0.28)
    if name == "riser":  # dồn nén trước khi lộ kết quả
        n = int(SR * 0.6)
        x = np.linspace(0, 1, n)
        s = _lowpass(_noise(n, 29), 400 + 6000 * x ** 2)
        s = s - _lowpass(s, 250)
        return _norm(s * x ** 2.2, 0.35)
    raise KeyError(name)


SOUNDS = ["pop", "pop_soft", "pop_big", "whoosh", "whoosh_soft", "swish", "tick", "key", "ding",
          "ding_soft", "chime", "thud", "draw", "marker", "sparkle", "riser"]


def render_sfx_track(events: Iterable[tuple[float, str, float]], total_dur: float, out_wav: Path,
                     master: float = 0.5) -> Path:
    """Trộn các cue thành 1 file WAV mono 48kHz (có limiter mềm)."""
    n = int(SR * (total_dur + 1.5))
    mix = np.zeros(n)
    for at, name, gain in events:
        try:
            s = sound(name)
        except KeyError:
            continue
        i = int(max(0.0, at) * SR)
        if i >= n:
            continue
        j = min(n, i + len(s))
        mix[i:j] += s[: j - i] * gain
    mix *= master
    mix = np.tanh(mix * 1.2) / np.tanh(1.2)  # chống clip khi nhiều cue chồng nhau
    mix = mix[: int(SR * total_dur)]
    pcm = (np.clip(mix, -1, 1) * 32767).astype("<i2")
    out_wav = Path(out_wav)
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm.tobytes())
    return out_wav


def default_master() -> float:
    try:
        return float(os.getenv("SFX_VOLUME", "0.5"))
    except ValueError:
        return 0.5
