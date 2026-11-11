import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

import word_timing as wt

has_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") or Path("C:/ffmpeg/ffmpeg.exe").exists()), reason="cần ffmpeg")


def _ff():
    from text_effects import get_ffmpeg_bin
    return get_ffmpeg_bin("ffmpeg")


def _voice(path: Path, pattern):
    """pattern: [(giây có tiếng | âm = giây lặng)] -> wav giả lập giọng đọc."""
    ff = _ff()
    parts = []
    for i, d in enumerate(pattern):
        src = f"sine=frequency=300:duration={d}" if d > 0 else f"anullsrc=r=44100:cl=mono:d={-d}"
        p = path.parent / f"p{i}.wav"
        subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", src, "-ar", "44100", "-ac", "1", str(p)], check=True)
        parts.append(p)
    lst = path.parent / "l.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), str(path)], check=True)


@has_ffmpeg
def test_words_snap_to_real_pauses(tmp_path):
    a = tmp_path / "v.wav"
    # lặng đầu 0.3 | nói 1.2 | lặng 0.4 | nói 1.2 | lặng 0.7 | nói 1.2 | lặng cuối 0.3
    _voice(a, [-0.3, 1.2, -0.4, 1.2, -0.7, 1.2, -0.3])
    text = "Một hai ba bốn, năm sáu bảy tám. Chín mười mười một mười hai."
    tl = wt.build_word_timeline(text, a, 5.3, _ff())
    by = {x["w"]: x for x in tl}
    assert len(tl) == len(text.split())
    assert abs(tl[0]["s"] - 0.3) < 0.1                 # bỏ lặng đầu
    assert abs(by["bốn,"]["e"] - 1.5) < 0.1            # kết thúc cụm đúng lúc ngắt hơi
    assert abs(by["năm"]["s"] - 1.9) < 0.1             # từ sau dấu phẩy bắt đầu khi giọng nói lại
    assert abs(by["Chín"]["s"] - 3.8) < 0.1            # câu mới sau dấu chấm
    assert all(tl[i]["s"] <= tl[i + 1]["s"] for i in range(len(tl) - 1))
    assert tl[-1]["e"] <= 5.05


def _syllable_voice(path: Path, plan, sr=16000):
    """plan: list of ('syl', dur) | ('pause', dur). Âm tiết = burst sin có bao hình chuông, nối bằng khe tiếng rất nhỏ."""
    import numpy as np, wave
    out = []
    for kind, d in plan:
        n = int(sr * d)
        if kind == "syl":
            t = np.arange(n) / sr
            out.append(0.5 * np.sin(2 * np.pi * 220 * t) * np.sin(np.pi * t / d) ** 2)
        else:
            out.append(np.zeros(n))
    x = (np.concatenate(out) * 32767).astype("int16")
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(sr); f.writeframes(x.tobytes())


@has_ffmpeg
def test_words_follow_syllables_when_speed_changes(tmp_path):
    a = tmp_path / "s.wav"
    # Cụm 1: 6 âm tiết NHANH (0.14s); ngắt 0.5s; cụm 2: 6 âm tiết CHẬM (0.40s): executor (4) + là + bảy
    plan = [("pause", 0.3)] + [("syl", 0.14)] * 6 + [("pause", 0.5)] + [("syl", 0.40)] * 6 + [("pause", 0.3)]
    _syllable_voice(a, plan)
    text = "một hai ba bốn năm sáu, executor là bảy"
    tl = wt.build_word_timeline(text, a, sum(d for _, d in plan), _ff())
    by = {x["w"]: x["s"] for x in tl}
    assert abs(by["bốn"] - (0.30 + 3 * 0.14)) < 0.1          # cụm nhanh
    assert abs(by["executor"] - 1.64) < 0.12                  # sau chỗ ngắt
    assert abs(by["là"] - (1.64 + 4 * 0.40)) < 0.12           # "executor" chiếm đủ 4 âm tiết chậm
    assert abs(by["bảy"] - (1.64 + 5 * 0.40)) < 0.12


@has_ffmpeg
@pytest.mark.parametrize("speed", [1.0, 1.5, 1.8])
def test_timing_holds_when_voice_is_sped_up(tmp_path, speed):
    """Giọng đọc nhanh hơn (Vbee speed_rate): âm tiết + chỗ ngắt ngắn lại, phụ đề vẫn phải bám đúng."""
    syl, pause, lead = 0.17 / speed, 0.35 / speed, 0.3
    plan = [("pause", lead)] + [("syl", syl)] * 8 + [("pause", pause)] + [("syl", syl)] * 8 + [("pause", 0.3)]
    a = tmp_path / "f.wav"
    _syllable_voice(a, plan)
    text = "một hai ba bốn năm sáu bảy tám, chín mười một hai ba bốn năm sáu"
    tl = wt.build_word_timeline(text, a, sum(d for _, d in plan), _ff())
    starts = [x["s"] for x in tl]
    second = lead + 8 * syl + pause
    expect = [lead + k * syl for k in range(8)] + [second + k * syl for k in range(8)]
    err = max(abs(s - e) for s, e in zip(starts, expect))
    assert err < 0.4 * syl, f"lệch tối đa {err * 1000:.0f}ms ở tốc độ {speed}x"


def test_syllable_estimates():
    assert wt.syllables("nghĩ") == 1 and wt.syllables("executor") == 4
    assert wt.syllables("MP4") >= 3 and wt.syllables("5,5") >= 3 and wt.syllables("Claude") == 1


def test_fallback_without_audio_is_monotonic_and_covers_duration():
    tl = wt.build_word_timeline("a bb ccc dddd", None, 4.0)
    assert [x["w"] for x in tl] == ["a", "bb", "ccc", "dddd"]
    assert tl[0]["s"] == 0 and abs(tl[-1]["e"] - 4.0) < 0.06


def test_words_in_window_relative():
    tl = [{"w": "x", "s": 1.0, "e": 1.3}, {"w": "y", "s": 2.0, "e": 2.4}, {"w": "z", "s": 3.0, "e": 3.2}]
    assert wt.words_in_window(tl, 1.5, 3.0) == [{"w": "y", "s": 0.5, "e": 0.9}]
    assert wt.excerpt(tl, 0, 2.5) == "x y"


def test_word_pop_shows_one_word_and_animates():
    from text_effects import render_frame
    words = [{"w": "Phí", "s": 0.2, "e": 0.5}, {"w": "12%", "s": 0.6, "e": 1.0}]
    card = {"template": "statement", "props": {"text": "Phí giao dịch"}, "sub_text": "Phí 12%", "_words": words}

    def sub_region(t):
        return render_frame(card, t, 3.0, 540, 960).crop((0, 600, 540, 800))

    def pill_px(img):  # điểm ảnh tối của ô nền phụ đề (nền cảnh là màu kem sáng)
        return sum(1 for r, g, b in img.getdata() if r + g + b < 300)

    assert pill_px(sub_region(0.1)) == 0                       # chưa tới từ đầu tiên
    assert 0 < pill_px(sub_region(0.267)) < pill_px(sub_region(0.45))  # đang bật ra thì nhỏ hơn lúc ổn định
    num = sub_region(0.9)
    assert any(r > 230 and g > 180 and b < 90 for r, g, b in num.getdata())  # số liệu tô vàng
    assert pill_px(sub_region(2.5)) == 0                       # ngắt hơi dài -> từ mờ hẳn
