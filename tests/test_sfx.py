"""Test sound effect: tổng hợp, ghi cue khớp frame, trộn track."""
import wave

import numpy as np

from sfx import SOUNDS, SfxRecorder, render_sfx_track, set_recorder, sound
from text_effects import normalize_card, render_frame


def test_every_sound_is_short_and_not_clipping():
    for name in SOUNDS:
        s = sound(name)
        assert 0 < len(s) / 48000 <= 1.5, name
        assert np.max(np.abs(s)) <= 1.0, name


def test_cues_are_recorded_only_when_frame_reaches_them():
    card = normalize_card({"template": "bignumber", "sub_text": "Vốn tăng 11 lần.", "props": {"number": "11", "unit": "lần"}})
    rec = SfxRecorder()
    set_recorder(rec)
    try:
        rec.t = 0.5
        render_frame(card, 0.5, 4.0, 360, 640)
        early = {n for _, n, _ in rec.list()}
        for i in range(0, 120):
            rec.t = i / 30
            render_frame(card, rec.t, 4.0, 360, 640)
    finally:
        set_recorder(None)
    names = [n for _, n, _ in rec.list()]
    assert "ding" not in early            # tiếng "ding" kết thúc đếm chưa tới lúc
    assert "pop_big" in names and "ding" in names and names.count("tick") >= 5
    assert all(0 <= at <= 4.0 for at, _, _ in rec.list())


def test_track_length_and_peaks(tmp_path):
    out = render_sfx_track([(0.5, "pop", 1.0), (1.0, "whoosh", 1.0), (9.9, "ding", 1.0)], 2.0, tmp_path / "s.wav")
    with wave.open(str(out)) as wf:
        assert wf.getframerate() == 48000
        assert abs(wf.getnframes() / 48000 - 2.0) < 1e-3
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    assert np.abs(pcm[:int(0.45 * 48000)]).max() == 0       # im lặng trước cue đầu tiên
    assert np.abs(pcm[int(0.5 * 48000):int(0.6 * 48000)]).max() > 1000
