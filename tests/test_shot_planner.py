import shot_planner as sp


def _tl(text, rate=0.3, pause=0.5, start=0.2):
    """Timeline giả: mỗi từ 0.3s, ngắt 0.5s sau dấu câu."""
    out, t = [], start
    for w in text.split():
        out.append({"w": w, "s": round(t, 3), "e": round(t + rate * 0.9, 3)})
        t += rate + (pause if w[-1] in ",.?!" else 0)
    return out, round(t + 0.2, 3)


SCRIPT = ("Xin chào các bạn, hôm nay mình kể một chuyện. "
          "Mỗi lệnh giao dịch tốn 0,15% phí. "
          "Một năm bạn vào 100 lệnh thì mất 15% vốn. "
          "Vậy thì làm sao để tránh? "
          "Hãy giao dịch ít mà chắc. "
          "Nhớ follow kênh nhé.")


def test_beats_cover_audio_and_cut_inside_pauses():
    tl, dur = _tl(SCRIPT)
    beats = sp.split_beats(tl, dur)
    assert beats[0]["start"] == 0 and beats[-1]["end"] == dur
    for a, b in zip(beats, beats[1:]):
        assert a["end"] == b["start"]
        assert tl[a["w1"]]["e"] <= a["end"] <= tl[b["w0"]]["s"]  # điểm cắt nằm trong chỗ ngắt hơi
    assert " ".join(b["text"] for b in beats) == " ".join(x["w"] for x in tl)


def test_heuristic_puts_numbers_on_visual_and_greeting_on_speaker():
    tl, dur = _tl(SCRIPT)
    beats = sp.split_beats(tl, dur)
    lab = dict(zip([b["text"] for b in beats], sp.heuristic_labels(beats)))
    first = next(t for t in lab if t.startswith("Xin chào"))
    assert lab[first] == "speaker"
    assert lab[next(t for t in lab if "0,15%" in t)] == "visual"
    assert lab[next(t for t in lab if "follow" in t)] == "speaker"


def test_plan_follows_labels_merges_and_respects_min_len(monkeypatch):
    tl, dur = _tl(SCRIPT)
    beats = sp.split_beats(tl, dur)
    want = {"Xin": "speaker", "hôm": "speaker", "Mỗi": "visual", "Một": "visual", "Vậy": "speaker",
            "Hãy": "visual", "Nhớ": "speaker"}
    monkeypatch.setattr(sp, "label_beats",
                        lambda text, bs, key, goal, log: ([want[b["text"].split()[0]] for b in bs], "opus"))
    shots = sp.plan_shots(SCRIPT, tl, dur, interval=5, anthropic_key="k")
    assert [s["type"] for s in shots][:3] == ["speaker", "visual", "speaker"]
    assert "0,15%" in shots[1]["text"] and "15% vốn" in shots[1]["text"]   # 2 ý số liệu liền nhau -> 1 shot
    assert shots[0]["start"] == 0 and shots[-1]["end"] == dur
    assert all(a["end"] == b["start"] for a, b in zip(shots, shots[1:]))
    assert all(s["end"] - s["start"] >= sp.MIN_SHOT for s in shots)
    assert all(a["type"] != b["type"] or b["split"] for a, b in zip(shots, shots[1:]))


def test_long_speaker_run_is_split_at_beat_boundary():
    long = " ".join(f"Đây là câu dẫn chuyện số {k} khá dài để kể." for k in range(8))
    tl, dur = _tl(long)
    beats = sp.split_beats(tl, dur)
    shots = sp.plan_shots(long, tl, dur, interval=5, demo_mode=True)
    sp_runs = [s for s in shots if s["type"] == "speaker"]
    assert all(s["end"] - s["start"] <= 10.5 for s in sp_runs)
    cut_points = {b["start"] for b in beats}
    assert all(s["start"] in cut_points for s in shots)  # chỉ cắt ở ranh giới ý


def test_presenter_always_appears_at_least_once(monkeypatch):
    tl, dur = _tl("Xin chào. Mỗi lệnh tốn 0,15% phí và một năm mất 15% vốn.")
    monkeypatch.setattr(sp, "label_beats", lambda text, bs, key, goal, log: (["visual"] * len(bs), "opus"))
    shots = sp.plan_shots("x", tl, dur, interval=5, anthropic_key="k")
    assert shots[0]["type"] == "speaker"


def test_short_shot_is_absorbed():
    shots = [{"type": "speaker", "start": 0, "end": 4, "beats": [{"start": 0, "end": 4}]},
             {"type": "visual", "start": 4, "end": 4.9, "beats": [{"start": 4, "end": 4.9}]},
             {"type": "speaker", "start": 4.9, "end": 9, "beats": [{"start": 4.9, "end": 9}]}]
    out = sp._absorb_short(shots)
    assert len(out) == 1 and out[0]["type"] == "speaker" and out[0]["end"] == 9


def test_slots_from_shots_keep_shot_index():
    shots = [{"type": "speaker", "start": 0, "end": 3, "text": "a"}, {"type": "visual", "start": 3, "end": 7, "text": "b"},
             {"type": "speaker", "start": 7, "end": 9, "text": "c"}]
    v = sp.slots_from_shots(shots, "visual")
    s = sp.slots_from_shots(shots, "speaker")
    assert [x["segment_index"] for x in v] == [1] and v[0]["spoken_excerpt"] == "b" and v[0]["duration"] == 4
    assert [x["segment_index"] for x in s] == [0, 2] and s[1]["is_last_segment"]
