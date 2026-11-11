"""Test lớp phủ cảnh người nói: thẻ text/icon trên + phụ đề dưới."""
import shutil
import subprocess

import pytest

from speaker_overlay import (
    ICONS,
    apply_speaker_overlay,
    build_speaker_slots,
    generate_speaker_overlays,
    render_overlay_frame,
)

SCRIPT = ("Lãi chỉ 1% mỗi tháng, nghe có vẻ ít. Nhưng sau 240 tháng vốn tăng gần 11 lần. "
          "Thời gian là đồng minh lớn nhất của nhà đầu tư. Vậy nên hãy bắt đầu từ hôm nay.")


@pytest.mark.parametrize("icon", ICONS)
def test_overlay_frame_is_transparent_in_middle(icon):
    ov = {"icon": icon, "tone": "good", "text": "Lãi kép tăng 11 lần", "keyword": "11 lần", "sub_text": "Vốn tăng gần 11 lần."}
    im = render_overlay_frame(ov, 2.0, 5.0, 720, 1280, progress=0.5)
    assert im.mode == "RGBA" and im.size == (720, 1280)
    assert im.getpixel((360, 640))[3] == 0          # giữa khung trống -> thấy người nói
    assert im.getpixel((360, 100))[3] > 200         # thẻ ở trên
    assert im.getpixel((360, 1013))[3] > 150        # hộp phụ đề (nhảy từng từ) ở dưới


def test_offline_overlays_follow_script():
    slots = build_speaker_slots(SCRIPT, total_dur=20, interval=4.0)
    ovs = generate_speaker_overlays(SCRIPT, slots, demo_mode=True)
    assert len(ovs) == len(slots) == 3
    assert [o["segment_index"] for o in ovs] == [0, 2, 4]
    for o in ovs:
        assert o["icon"] in ICONS
        assert o["text"] and o["sub_text"]
        assert all(wd in SCRIPT for wd in o["text"].split())


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="cần ffmpeg")
def test_apply_overlay_keeps_size_and_length(tmp_path):
    src, out = tmp_path / "src.mp4", tmp_path / "out.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:s=720x1280:r=30",
                    "-t", "1", "-pix_fmt", "yuv420p", str(src)], check=True)
    apply_speaker_overlay(src, out, {"icon": "check", "text": "Bắt đầu hôm nay", "sub_text": "Hãy bắt đầu từ hôm nay."},
                          720, 1280, 30, 1.0)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                            "-show_entries", "stream=width,height,nb_read_frames", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert probe == "720,1280,30"
