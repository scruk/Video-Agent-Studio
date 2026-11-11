import io
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from PIL import Image

import stock_footage as sf


def _vhit(id_, w, h, tags, dur=10):
    v = lambda s, k: {"url": f"https://cdn.pixabay.com/v/{id_}_{s}.mp4", "width": w // k, "height": h // k,
                      "thumbnail": f"https://cdn.pixabay.com/t/{id_}_{s}.jpg"}
    return {"id": id_, "duration": dur, "user": "u", "tags": tags, "pageURL": f"https://pixabay.com/videos/{id_}/",
            "videos": {"tiny": v("t", 3), "small": v("s", 2), "medium": v("m", 1), "large": {"url": ""}}}


def _ihit(id_, tags):
    return {"id": id_, "tags": tags, "user": "u", "pageURL": f"https://pixabay.com/photos/{id_}/",
            "imageWidth": 1080, "imageHeight": 1920,
            "webformatURL": f"https://pixabay.com/get/{id_}_640.jpg", "largeImageURL": f"https://pixabay.com/get/{id_}_1280.jpg"}


def _jpeg():
    b = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 50, 50)).save(b, "JPEG")
    return b.getvalue()


def _client(tmp_path, videos, images, calls):
    def api(req: httpx.Request):
        calls.append(req)
        assert req.url.host == "pixabay.com" and req.url.params["key"] == "SECRETKEY"
        hits = videos if req.url.path.startswith("/api/videos") else images
        return httpx.Response(200, json={"hits": hits})

    def cdn(req: httpx.Request):
        assert "key" not in req.url.params  # key không bao giờ tới CDN
        return httpx.Response(200, content=_jpeg())

    return sf.PixabayClient("SECRETKEY", tmp_path, http=httpx.Client(transport=httpx.MockTransport(api)),
                            cdn=httpx.Client(transport=httpx.MockTransport(cdn)))


CARDS = [{"template": "statement"}, {"template": "bignumber"}, {"template": "statement"},
         {"template": "statement"}, {"template": "climax"}]
SLOTS = [{"spoken_excerpt": f"đoạn {i}", "time_range": f"{i}s"} for i in range(5)]


def test_tag_relevance_filters_off_topic():
    assert sf.tag_relevance("stock market, trading, chart", "stock chart") == 1.0
    assert sf.tag_relevance("tree, forest, nature", "stock chart") == 0.0
    assert sf.tag_relevance("markets, finance", "market") == 1.0


def test_only_opus_approved_relevant_media_is_used(tmp_path):
    calls = []
    videos = [_vhit(1, 1080, 1920, "forest, tree, nature"),           # lạc đề -> bị lọc bởi tag
              _vhit(2, 1080, 1920, "stock market, chart, trading"),
              _vhit(3, 1920, 1080, "stock, chart, screen")]
    images = [_ihit(9, "coins, money, savings")]
    c = _client(tmp_path, videos, images, calls)
    seen_rows, seen_cands = [], {}

    def planner(text, goal, rows, key):
        seen_rows.extend(rows)
        return {0: {"scene": "màn hình chứng khoán", "queries": ["stock chart", "trading", "stock"], "media": "video"},
                3: {"scene": "đồng xu", "queries": ["coins money", "coins", "money"], "media": "image"}}

    def picker(client, text, goal, slots, key, log):
        seen_cands.update({s: [x["id"] for x in v["cands"]] for s, v in slots.items()})
        return {0: (0, 9), 3: (0, 4)}  # cảnh 4 điểm thấp -> giữ đồ hoạ

    out = sf.find_stock_media("kịch bản", CARDS, SLOTS, 720, 1280, 5.0, "SECRETKEY", "ANTKEY", tmp_path,
                              client=c, planner=planner, picker=picker)
    # Chỉ xét cảnh không phải số liệu / cảnh cuối
    assert [r["slot"] for r in seen_rows] == [0, 2, 3]
    assert "v1" not in seen_cands[0]
    assert seen_cands[0][0] == "v2"  # khớp tag + đúng chiều dọc xếp đầu
    assert set(out) == {0} and out[0]["kind"] == "video" and out[0]["path"].exists()
    assert out[0]["path"].read_bytes()  # đã tải về máy


def test_no_anthropic_key_means_no_stock(tmp_path):
    assert sf.find_stock_media("x", CARDS, SLOTS, 720, 1280, 5, "K", "", tmp_path) == {}


def test_unreviewed_media_is_never_inserted(tmp_path):
    c = _client(tmp_path, [_vhit(2, 1080, 1920, "stock chart")], [], [])

    def picker(*a, **k):
        raise RuntimeError("vision down")

    out = sf.find_stock_media("x", CARDS, SLOTS, 720, 1280, 5, "SECRETKEY", "A", tmp_path, client=c,
                              planner=lambda *a: {0: {"scene": "", "queries": ["stock chart"], "media": "video"}},
                              picker=picker)
    assert out == {}


def test_bad_key_error_does_not_leak_key(tmp_path):
    def api(req):
        return httpx.Response(400, text="[ERROR 400] Invalid or missing API key SECRETKEY")
    c = sf.PixabayClient("SECRETKEY", tmp_path, http=httpx.Client(transport=httpx.MockTransport(api)))
    logs = []
    out = sf.find_stock_media("x", CARDS, SLOTS, 720, 1280, 5, "SECRETKEY", "A", tmp_path, client=c,
                              planner=lambda *a: {0: {"scene": "", "queries": ["city"], "media": "video"}},
                              on_status=logs.append)
    assert out == {} and logs and all("SECRETKEY" not in l for l in logs)


def test_search_cached_24h(tmp_path):
    calls = []
    c = _client(tmp_path, [_vhit(2, 1080, 1920, "a")], [], calls)
    c.search_videos("city street")
    c.search_videos("City  street")
    assert len(calls) == 1


def test_download_rejects_non_pixabay_url(tmp_path):
    c = sf.PixabayClient("K", tmp_path)
    for url in ("https://evil.example.com/a.mp4", "https://pixabay.com.evil.io/a.mp4"):
        with pytest.raises(sf.PixabayError):
            c.download(url, tmp_path / "a.mp4")


def test_plan_parser_drops_invalid_slots(monkeypatch):
    monkeypatch.setattr(sf, "_claude_json", lambda *a, **k: [
        {"slot": 1, "use": True, "scene": "s", "queries": ["stock chart", "", "trader"], "media": "video"},
        {"slot": 2, "use": True, "queries": ["x"]},     # cảnh 2 không nằm trong danh sách
        {"slot": 3, "use": False, "queries": ["y"]},    # Opus nói không dùng
    ])
    plan = sf.plan_stock_queries("t", "", [{"slot": 0, "time": "", "excerpt": ""},
                                           {"slot": 2, "time": "", "excerpt": ""}], "A")
    assert plan == {0: {"scene": "s", "queries": ["stock chart", "trader"], "media": "video"}}


def _ff():
    from text_effects import get_ffmpeg_bin
    return get_ffmpeg_bin("ffmpeg"), get_ffmpeg_bin("ffprobe")


def _info(fp, out):
    r = subprocess.run([fp, "-v", "error", "-show_entries", "stream=width,height:format=duration", "-of", "json",
                        str(out)], capture_output=True, text=True, check=True)
    i = json.loads(r.stdout)
    return i["streams"][0]["width"], i["streams"][0]["height"], float(i["format"]["duration"])


has_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") or Path("C:/ffmpeg/ffmpeg.exe").exists()), reason="cần ffmpeg")


@has_ffmpeg
def test_render_video_fills_frame_and_loops(tmp_path):
    ff, fp = _ff()
    src = tmp_path / "src.mp4"
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=1.5",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    w, h, d = _info(fp, sf.render_stock_clip(src, tmp_path / "o.mp4", 360, 640, 3.0, 30, ff, fp))
    assert (w, h) == (360, 640) and abs(d - 3.0) < 0.15


@has_ffmpeg
def test_render_image_ken_burns(tmp_path):
    ff, fp = _ff()
    src = tmp_path / "p.jpg"
    Image.new("RGB", (1280, 853), (30, 90, 160)).save(src)
    w, h, d = _info(fp, sf.render_stock_clip(src, tmp_path / "o.mp4", 360, 640, 2.0, 30, ff, fp))
    assert (w, h) == (360, 640) and abs(d - 2.0) < 0.15
