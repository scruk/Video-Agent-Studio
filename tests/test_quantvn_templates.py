"""Test cảnh minh hoạ QuantVN: style cố định, nội dung lấy từ kịch bản người dùng."""
import shutil
import subprocess

import pytest

from text_effects import (
    TEMPLATES,
    build_timed_cutaway_slots,
    generate_text_cards_with_claude,
    normalize_card,
    render_frame,
    render_text_effect_clip,
)

SAMPLE_ONLY = ["0,15%", "0,4%", "100 vòng", "Tính phí", "Thật không?", "HOÁ ĐƠN PHÍ"]

CARDS = {
    "calculator": {"headline": "Lãi 1% mỗi tháng.", "props": {"display": "1%"}},
    "receipt": {"headline": "Sau 12 tháng", "props": {"items": [["Vốn", "100tr"], ["Lãi", "12tr"]],
                                                    "total_label": "Tổng", "total_value": "112tr"}},
    "dots": {"props": {"count": "240", "unit": "tháng", "filled": 100, "tone": "good"}},
    "result": {"props": {"expression": "1,01 ^ 240", "result": "×10,9", "tone": "good"}},
    "chart": {"headline": "Lãi kép vs lãi đơn",
              "props": {"lines": [{"label": "Kép +989%", "end_value": 989, "tone": "good"},
                                  {"label": "Đơn +240%", "end_value": 240, "tone": "bad"}]}},
    "compare": {"props": {"rows": [["25 tuổi", "×10,9"], ["Bạn", "?"]], "highlight_last": True}},
    "bignumber": {"props": {"number": "11", "unit": "lần"}},
    "statement": {"props": {"text": "Thời gian quan trọng hơn lãi suất", "keyword": "Thời gian"}},
    "climax": {"props": {"lines": ["Bắt đầu", "từ hôm nay."]}},
}


@pytest.mark.parametrize("tpl", TEMPLATES)
@pytest.mark.parametrize("size", [(1080, 1920), (720, 1280), (1280, 720)])
def test_every_template_renders(tpl, size):
    card = normalize_card({"template": tpl, "sub_text": "Câu thoại thử nghiệm có dấu tiếng Việt.", **CARDS[tpl]})
    assert card["template"] == tpl
    for t in (0.0, 1.5, 3.9):
        im = render_frame(card, t, 4.0, *size, progress=0.5)
        assert im.size == size


def test_missing_data_falls_back_without_inventing_numbers():
    card = normalize_card({"template": "receipt", "sub_text": "Kỷ luật quan trọng hơn chiến lược."})
    assert card["template"] == "statement"


def test_offline_cards_come_from_user_script_not_sample_video():
    script = ("Lãi chỉ 1% mỗi tháng, nghe có vẻ ít. Nhưng sau 240 tháng vốn tăng gần 11 lần. "
              "Thời gian là đồng minh lớn nhất của nhà đầu tư. Vậy nên hãy bắt đầu từ hôm nay.")
    slots = build_timed_cutaway_slots(script, total_dur=20, interval=2.5)
    cards = generate_text_cards_with_claude(script, len(slots), slots, demo_mode=True)
    assert len(cards) == len(slots)
    blob = repr(cards)
    for s in SAMPLE_ONLY:
        assert s not in blob
    for c in cards:
        assert c["sub_text"] and c["sub_text"] in script


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="cần ffmpeg")
def test_clip_has_expected_size_and_length(tmp_path):
    out = tmp_path / "clip.mp4"
    render_text_effect_clip(card={"template": "bignumber", "sub_text": "Vốn tăng 11 lần.",
                                  "props": {"number": "11", "unit": "lần"}},
                            w=720, h=1280, duration=1.0, out_clip_path=out, fps=30)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                            "-show_entries", "stream=width,height,nb_read_frames", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert probe == "720,1280,30"
