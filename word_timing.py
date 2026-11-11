"""Mốc thời gian từng từ của giọng đọc, để phụ đề nhảy từng từ khớp voice.

Vbee không trả timestamp từng từ, nên ta suy ra từ chính file audio (chỉ cần ffmpeg + numpy):
  1. Giải mã audio, tính đường bao âm lượng; tìm KHOẢNG LẶNG (chỗ ngắt hơi) và ĐỈNH ÂM TIẾT
     (tiếng Việt đọc theo nhịp âm tiết: mỗi âm tiết ≈ một đỉnh âm lượng).
  2. Ước số âm tiết của từng từ trong kịch bản (từ tiếng Việt = 1; từ tiếng Anh đếm cụm nguyên âm;
     số/viết tắt đếm theo cách đọc: "5.5" = năm chấm năm, "MP4" = em pê bốn).
  3. Ghép khoảng lặng với dấu câu bằng quy hoạch động (tối ưu toàn cục, đúng thứ tự,
     cho phép bỏ qua chỗ ngắt hơi giữa câu và dấu phẩy không ngắt) theo vị trí ÂM TIẾT, không theo thời gian.
  4. Trong mỗi cụm giữa hai chỗ ngắt, âm tiết thứ k của kịch bản được gán vào đỉnh âm tiết tương ứng
     trong audio -> đoạn đọc nhanh/chậm đều bám đúng.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

import numpy as np

SR = 16000
HOP = 0.005  # 5ms: đủ mịn cho âm tiết ~90ms khi đọc nhanh
_PUNCT_END = re.compile(r"[,.;:!?…]+[\"'”’)]*$")
_SENT_END = re.compile(r"[.!?…]+[\"'”’)]*$")
_VI_CHARS = re.compile(r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]", re.I)
_DIGIT_SYL = {",": 1, ".": 1, "%": 2, "/": 1, ":": 1, "-": 0, "+": 1}


# =============================================================================
# ƯỚC SỐ ÂM TIẾT
# =============================================================================
def syllables(word: str) -> float:
    w = re.sub(r"^[\"'“‘(\[]+|[\"'”’)\],.;:!?…]+$", "", word)
    if not w:
        return 0.5
    if re.search(r"\d", w):  # số: mỗi chữ số ~1.3 âm tiết ("mười hai"), dấu ',' '.' '%' đọc ra tiếng
        n = sum(1.3 for ch in w if ch.isdigit()) + sum(_DIGIT_SYL.get(ch, 0) for ch in w if not ch.isdigit()
                                                       and not ch.isalpha())
        n += sum(1 for ch in w if ch.isalpha())  # "MP4", "5x"
        return max(1.0, n)
    if _VI_CHARS.search(w):
        return 1.0
    if w.isupper() and 1 < len(w) <= 5:  # viết tắt đánh vần: AI, API, MP
        return float(len(w))
    groups = re.findall(r"[aeiouy]+", w.lower())
    n = len(groups)
    if w.lower().endswith("e") and n > 1 and not w.lower().endswith(("le", "ee")):
        n -= 1  # "make", "code" (e câm)
    if n <= 1:
        return 1.0
    return float(n)  # "executor" 4, "GitHub" 2, "agent" 2


# =============================================================================
# PHÂN TÍCH AUDIO
# =============================================================================
def _load_pcm(audio: Path, ffmpeg_bin: str) -> np.ndarray:
    r = subprocess.run([ffmpeg_bin, "-v", "error", "-i", str(audio), "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                       capture_output=True, check=True)
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _envelope(x: np.ndarray, win_s: float = 0.02) -> np.ndarray:
    hop, win = int(SR * HOP), max(32, int(SR * win_s))
    n = max(1, (len(x) - win) // hop + 1)
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    frames = x[np.clip(idx, 0, len(x) - 1)]
    return np.sqrt((frames ** 2).mean(axis=1) + 1e-12)


def _smooth(v: np.ndarray, sec: float) -> np.ndarray:
    k = max(1, int(round(sec / HOP)))
    if k <= 1:
        return v
    return np.convolve(v, np.ones(k) / k, mode="same")


def find_silences(env: np.ndarray, noise_db: float = -35.0, min_dur: float = 0.16) -> list[tuple[float, float]]:
    ref = np.percentile(env, 95) + 1e-9
    quiet = 20 * np.log10(env / ref + 1e-9) < noise_db
    out, start = [], None
    for i, q in enumerate(quiet):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if (i - start) * HOP >= min_dur:
                out.append((start * HOP, i * HOP))
            start = None
    if start is not None and (len(quiet) - start) * HOP >= min_dur:
        out.append((start * HOP, len(quiet) * HOP))
    return out


def find_syllable_peaks(env: np.ndarray, min_gap: float = 0.09, smooth_s: float = 0.05,
                        valley_ratio: float = 0.82) -> np.ndarray:
    """Đỉnh âm tiết: cực đại cục bộ đủ cao, cách nhau ≥ min_gap, có 'thung lũng' rõ giữa hai đỉnh."""
    e = _smooth(env, smooth_s)
    ref = np.percentile(e, 95) + 1e-9
    floor = 0.12 * ref
    cand = np.where((e[1:-1] >= e[:-2]) & (e[1:-1] > e[2:]) & (e[1:-1] > floor))[0] + 1
    gap = max(1, int(min_gap / HOP))
    peaks: list[int] = []
    for i in cand:
        if peaks and i - peaks[-1] < gap:
            if e[i] > e[peaks[-1]]:
                peaks[-1] = i
            continue
        if peaks:
            valley = e[peaks[-1]:i + 1].min()
            if valley > valley_ratio * min(e[peaks[-1]], e[i]):  # không có thung lũng -> cùng 1 âm tiết
                if e[i] > e[peaks[-1]]:
                    peaks[-1] = i
                continue
        peaks.append(int(i))
    return np.array(peaks, dtype=float) * HOP


def syllable_onsets(env: np.ndarray, peaks: np.ndarray, smooth_s: float = 0.05,
                    max_link: float = 0.6, back_s: float = 0.2) -> np.ndarray:
    """Điểm BẮT ĐẦU mỗi âm tiết: chỗ trũng nhất giữa đỉnh trước và đỉnh này
    (âm tiết đầu cụm: lùi lại tới khi âm lượng tụt dưới 25% đỉnh, tối đa back_s)."""
    e = _smooth(env, smooth_s)
    idx = (peaks / HOP).astype(int)
    out = []
    for k, i in enumerate(idx):
        # Giới hạn lùi: chỗ trũng giữa đỉnh trước và đỉnh này (hoặc back_s nếu đỉnh trước ở xa)
        if k > 0 and (i - idx[k - 1]) * HOP < max_link:
            lim = idx[k - 1] + int(np.argmin(e[idx[k - 1]:i + 1]))
        else:
            lim = max(0, i - int(back_s / HOP))
        # Onset = lúc âm lượng dâng lên trước đỉnh (lùi tới khi tụt dưới 10% đỉnh), không lùi quá chỗ trũng.
        # Nếu chỗ trũng là một khoảng lặng, onset dừng ở mép khoảng lặng chứ không rơi vào giữa nó.
        j = i
        while j > lim and e[j] > 0.1 * e[i]:
            j -= 1
        out.append(j * HOP)
    return np.array(out, dtype=float)


REF_RATE = 5.8  # âm tiết/giây của giọng Vbee ở tốc độ 1.0 (đo trên giọng thật)


def analyze_speech(x: np.ndarray, expected_syllables: float) -> tuple:
    """Dò khoảng lặng + điểm bắt đầu âm tiết, TỰ THÍCH ỨNG TỐC ĐỘ ĐỌC.

    Tốc độ thật = số âm tiết kịch bản / thời gian có tiếng. Mọi ngưỡng thời gian co giãn theo tỉ lệ
    f = REF_RATE / tốc độ (đọc 1.5x -> f ≈ 0.67), rồi chọn độ nhạy cho số âm tiết dò được gần
    số âm tiết kịch bản nhất. -> (silences, onsets, f)
    """
    env0 = _envelope(x, 0.02)
    rough = find_silences(env0, min_dur=0.1)
    total = len(env0) * HOP
    speech = sum(y - x_ for x_, y in _speech_spans(rough, 0.0, total)) or total
    rate = expected_syllables / speech if expected_syllables > 0 else REF_RATE
    f = float(np.clip(REF_RATE / rate, 0.4, 1.4))
    env = _envelope(x, 0.02 * max(0.6, f))
    silences = find_silences(env, min_dur=max(0.07, 0.16 * f))
    best, best_err = None, 1e9
    for sm in (0.05 * f, 0.035 * f):
        for vr in (0.82, 0.9, 0.75):
            pk = find_syllable_peaks(env, min_gap=0.09 * f, smooth_s=max(0.01, sm), valley_ratio=vr)
            err = abs(len(pk) - expected_syllables)
            if err < best_err:
                best, best_err, best_sm = pk, err, max(0.01, sm)
    onsets = syllable_onsets(env, best, best_sm, max_link=0.6 * f, back_s=0.2 * f)
    return silences, onsets, f


# =============================================================================
# GHÉP CHỖ NGẮT <-> DẤU CÂU (quy hoạch động)
# =============================================================================
def _align_pauses(sil_pos: list[float], sil_dur: list[float], bounds: list[tuple[float, int, float]]) -> list[tuple[int, int]]:
    """sil_pos/bounds[.][0]: vị trí theo tỉ lệ âm tiết (0..1). -> [(chỉ số khoảng lặng, chỉ số dấu câu)]."""
    n, m = len(sil_pos), len(bounds)
    INF = 1e18
    dp = np.full((n + 1, m + 1), INF)
    bk = np.zeros((n + 1, m + 1), dtype=np.int8)  # 0 match, 1 bỏ khoảng lặng, 2 bỏ dấu câu
    dp[0, 0] = 0.0

    def skip_sil(i):  # ngắt hơi giữa câu là bình thường, nhưng khoảng lặng dài mà không khớp dấu câu thì đáng ngờ
        return 0.2 + 1.2 * min(sil_dur[i], 1.0)

    def skip_b(j):  # dấu phẩy không ngắt là bình thường; dấu chấm không ngắt hiếm hơn
        return 0.3 if bounds[j][2] < 2 else 0.9

    for i in range(n + 1):
        for j in range(m + 1):
            if dp[i, j] >= INF:
                continue
            if i < n and dp[i, j] + skip_sil(i) < dp[i + 1, j]:
                dp[i + 1, j], bk[i + 1, j] = dp[i, j] + skip_sil(i), 1
            if j < m and dp[i, j] + skip_b(j) < dp[i, j + 1]:
                dp[i, j + 1], bk[i, j + 1] = dp[i, j] + skip_b(j), 2
            if i < n and j < m:
                d = abs(sil_pos[i] - bounds[j][0])
                if d < 0.15:  # khớp gần -> rẻ hơn hẳn việc bỏ qua cả hai; DP chọn dấu câu gần nhất
                    c = dp[i, j] + (d / 0.08) ** 2 - (0.6 + 0.3 * bounds[j][2])
                    if c < dp[i + 1, j + 1]:
                        dp[i + 1, j + 1], bk[i + 1, j + 1] = c, 0
    pairs, i, j = [], n, m
    while i > 0 or j > 0:
        b = bk[i, j]
        if b == 0:
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif b == 1:
            i -= 1
        else:
            j -= 1
    return pairs[::-1]


# =============================================================================
# GÁN TỪ VÀO ĐỈNH ÂM TIẾT
# =============================================================================
def _speech_spans(silences: list[tuple[float, float]], a: float, b: float) -> list[tuple[float, float]]:
    spans, cur = [], a
    for s, e in silences:
        if e <= cur or s >= b:
            continue
        if s > cur:
            spans.append((cur, min(s, b)))
        cur = max(cur, e)
    if cur < b:
        spans.append((cur, b))
    return [(x, y) for x, y in spans if y - x > 1e-3] or [(a, b)]


def _place_by_time(weights: list[float], spans: list[tuple[float, float]]) -> list[float]:
    total_t = sum(y - x for x, y in spans)
    total_w = sum(weights) or 1.0

    def at(u: float) -> float:
        for x, y in spans:
            if u <= (y - x) + 1e-9:
                return x + u
            u -= (y - x)
        return spans[-1][1]

    out, acc = [], 0.0
    for wgt in weights:
        out.append(at(acc / total_w * total_t))
        acc += wgt
    return out


def _place_by_peaks(syl: list[float], peaks: np.ndarray, a: float, b: float,
                    silences: list[tuple[float, float]]) -> list[float]:
    """Thời điểm bắt đầu mỗi từ = điểm bắt đầu âm tiết đầu tiên của từ. `peaks` ở đây là ONSET âm tiết."""
    pk = peaks[(peaks >= a - 0.12) & (peaks < b)]
    M = sum(syl)
    if len(pk) < 2 or M <= 0 or not (0.45 <= len(pk) / M <= 2.2):
        return _place_by_time(syl, _speech_spans(silences, a, b))  # đếm đỉnh không tin cậy -> chia theo thời gian
    K = len(pk)
    starts, acc = [], 0.0
    for s in syl:
        u = acc / M * K  # vị trí âm tiết -> chỉ số đỉnh (số thực)
        k0 = int(np.floor(u))
        if k0 >= K - 1:
            t = pk[-1] + (u - (K - 1)) * (b - pk[-1])
        else:
            t = pk[k0] + (u - k0) * (pk[k0 + 1] - pk[k0])
        starts.append(max(a, t - 0.02))
        acc += s
    return starts


def build_word_timeline(speech_text: str, audio: Optional[Path], total_dur: float,
                        ffmpeg_bin: str = "ffmpeg") -> list[dict]:
    """-> [{"w": từ, "s": giây bắt đầu, "e": giây kết thúc}, ...] theo thời gian tuyệt đối của audio."""
    words = speech_text.split()
    if not words or total_dur <= 0:
        return []
    syl = [syllables(w) for w in words]
    silences: list[tuple[float, float]] = []
    peaks = np.array([])
    if audio and Path(audio).exists():
        try:
            sil, peaks, _f = analyze_speech(_load_pcm(Path(audio), ffmpeg_bin), sum(syl))
            silences = [(s, min(e, total_dur)) for s, e in sil if s < total_dur]
        except Exception:
            silences, peaks = [], np.array([])

    t0, t1 = 0.0, total_dur
    if silences and silences[0][0] <= 0.05:
        t0 = silences[0][1]
        silences = silences[1:]
    if silences and silences[-1][1] >= total_dur - 0.05:
        t1 = silences[-1][0]
        silences = silences[:-1]
    if t1 - t0 < 0.5:
        t0, t1, silences = 0.0, total_dur, []
    peaks = peaks[(peaks >= t0) & (peaks <= t1)] if len(peaks) else peaks

    # Vị trí theo tỉ lệ ÂM TIẾT: dấu câu (kịch bản) và khoảng lặng (audio, đếm đỉnh trước nó)
    M = sum(syl)
    cum, bounds = 0.0, []
    for i, (w, s) in enumerate(zip(words[:-1], syl[:-1])):
        cum += s
        if _PUNCT_END.search(w):
            bounds.append((cum / M, i, 2.0 if _SENT_END.search(w) else 1.0))
    use_peaks = len(peaks) >= 0.45 * M
    if use_peaks:
        sil_pos = [float((peaks < s).sum()) / len(peaks) for s, _ in silences]
    else:
        sp_total = sum(y - x for x, y in _speech_spans(silences, t0, t1)) or 1.0
        sil_pos = [sum(y - x for x, y in _speech_spans(silences, t0, s)) / sp_total if s > t0 else 0.0
                   for s, _ in silences]
    pairs = _align_pauses(sil_pos, [e - s for s, e in silences], bounds)

    # Ghép các cụm giữa hai neo
    anchors = [(bounds[j][1], silences[i][0], silences[i][1]) for i, j in pairs]
    starts: list[float] = []
    start_word, start_t = 0, t0
    for wi, s, e in anchors + [(len(words) - 1, t1, t1)]:
        idx = list(range(start_word, wi + 1))
        if idx:
            end_t = max(s, start_t + 0.05 * len(idx))
            seg_syl = [syl[k] for k in idx]
            if use_peaks:
                starts += _place_by_peaks(seg_syl, peaks, start_t, end_t, silences)
            else:
                starts += _place_by_time(seg_syl, _speech_spans(silences, start_t, end_t))
        start_word, start_t = wi + 1, e
    # Kết thúc từ = lúc bắt đầu từ sau, nhưng không kéo qua khoảng lặng
    ends = []
    for k, st in enumerate(starts):
        nxt = starts[k + 1] if k + 1 < len(starts) else t1
        sil_cut = next((s for s, e in silences if st < s < nxt), None)
        ends.append(sil_cut if sil_cut is not None else nxt)
    # đảm bảo tăng dần
    for k in range(1, len(starts)):
        if starts[k] < starts[k - 1] + 0.04:
            starts[k] = starts[k - 1] + 0.04
    return [{"w": w, "s": round(s, 3), "e": round(max(e, s + 0.05), 3)} for w, s, e in zip(words, starts, ends)]


def words_in_window(timeline: list[dict], a: float, b: float) -> list[dict]:
    """Từ có thời điểm bắt đầu trong [a, b), đổi về giây tương đối so với a."""
    return [{"w": x["w"], "s": round(x["s"] - a, 3), "e": round(x["e"] - a, 3)}
            for x in timeline if a - 1e-6 <= x["s"] < b]


def excerpt(timeline: list[dict], a: float, b: float) -> str:
    return " ".join(x["w"] for x in timeline if a - 1e-6 <= x["s"] < b)
