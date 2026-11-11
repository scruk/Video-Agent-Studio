"""Lấy cảnh quay / ảnh thật từ Pixabay, ĐÚNG CHỦ ĐỀ kịch bản, cho các cảnh minh hoạ.

Luồng 3 bước:
  1. LẬP KẾ HOẠCH (Opus đọc toàn bộ kịch bản): với mỗi cảnh minh hoạ hợp lệ -> mô tả hình cần thấy
     + 3 truy vấn tiếng Anh (cụ thể -> rộng) + ưu tiên video hay ảnh. Opus được phép nói "không dùng".
  2. TÌM ỨNG VIÊN trên Pixabay (video + ảnh), lọc theo tag khớp truy vấn, tối đa 5 ứng viên/cảnh.
  3. OPUS XEM THUMBNAIL các ứng viên và chọn cái khớp nhất (hoặc loại hết). Chỉ ứng viên đạt điểm mới được tải.

Theo điều khoản Pixabay API: cache kết quả tìm 24h, tải file về máy (không nhúng link),
key chỉ gửi tới pixabay.com/api, không gửi tới CDN và không ghi ra log.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

VIDEO_API = "https://pixabay.com/api/videos/"
IMAGE_API = "https://pixabay.com/api/"
CACHE_TTL = 24 * 3600
MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024
MAX_CANDIDATES = 5
MIN_PICK_SCORE = 6
_RENDITIONS = ("tiny", "small", "medium", "large")  # nhỏ -> lớn
# Template giàu số liệu: đồ hoạ truyền tải tốt hơn cảnh quay, không thay bằng stock
_DATA_TEMPLATES = {"calculator", "receipt", "dots", "result", "chart", "compare", "bignumber"}
_STOP = {"a", "an", "the", "of", "and", "or", "in", "on", "at", "with", "to", "for", "from", "by", "is", "are"}

Log = Callable[[str], None]


class PixabayError(RuntimeError):
    pass


def _safe_query(q: str) -> str:
    q = re.sub(r"[^\w\s\-]", " ", str(q or ""), flags=re.UNICODE)
    return " ".join(q.split())[:100]


def _is_pixabay_cdn(url: str) -> bool:
    try:
        host = httpx.URL(url).host
    except Exception:
        return False
    return url.startswith("https://") and (host == "pixabay.com" or host.endswith(".pixabay.com"))


# =============================================================================
# PIXABAY CLIENT
# =============================================================================
class PixabayClient:
    def __init__(self, api_key: str, cache_dir: Path, timeout: float = 20.0,
                 http: Optional[httpx.Client] = None, cdn: Optional[httpx.Client] = None):
        if not api_key:
            raise PixabayError("Chưa có PIXABAY_API_KEY")
        self._key = api_key
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._http = http or httpx.Client(timeout=timeout, follow_redirects=False,
                                          headers={"User-Agent": "video-agent/1.0"})
        # Client tải file KHÔNG mang key; CDN có thể redirect nên cho follow
        self._cdn = cdn or httpx.Client(timeout=60.0, follow_redirects=True,
                                        headers={"User-Agent": "video-agent/1.0"})

    def close(self) -> None:
        self._http.close()
        self._cdn.close()

    def _get(self, url: str, params: dict, kind: str) -> list[dict]:
        q = params["q"]
        tag = hashlib.sha1(f"{kind}|{json.dumps(params.get('orientation', ''))}|{q.lower()}".encode()).hexdigest()[:16]
        cache_file = self.cache_dir / f"search_{kind}_{tag}.json"
        if cache_file.exists() and time.time() - cache_file.stat().st_mtime < CACHE_TTL:
            try:
                return json.loads(cache_file.read_text("utf-8"))
            except Exception:
                pass
        params = {"key": self._key, **params}
        for attempt in range(3):
            try:
                r = self._http.get(url, params=params)
            except httpx.HTTPError as e:
                # Không in e (có thể chứa URL kèm key)
                if attempt == 2:
                    raise PixabayError(f"Không kết nối được Pixabay ({type(e).__name__})") from None
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 3)))
                continue
            if r.status_code >= 500 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise PixabayError(f"Pixabay trả lỗi {r.status_code}: {r.text[:200].replace(self._key, '***')}")
            hits = r.json().get("hits", [])
            cache_file.write_text(json.dumps(hits, ensure_ascii=False), "utf-8")
            return hits
        raise PixabayError("Pixabay giới hạn tốc độ (429), thử lại sau")

    def search_videos(self, query: str, per_page: int = 15) -> list[dict]:
        q = _safe_query(query)
        if not q:
            return []
        return self._get(VIDEO_API, {"q": q, "video_type": "film", "safesearch": "true",
                                     "per_page": per_page}, "video")

    def search_images(self, query: str, portrait: bool, per_page: int = 15) -> list[dict]:
        q = _safe_query(query)
        if not q:
            return []
        return self._get(IMAGE_API, {"q": q, "image_type": "photo", "safesearch": "true", "per_page": per_page,
                                     "orientation": "vertical" if portrait else "horizontal"}, "image")

    def fetch_bytes(self, url: str, limit: int = 4 * 1024 * 1024) -> bytes:
        if not _is_pixabay_cdn(url):
            raise PixabayError("URL không thuộc Pixabay, bỏ qua")
        r = self._cdn.get(url)
        if r.status_code != 200 or len(r.content) > limit:
            raise PixabayError(f"Tải thumbnail lỗi {r.status_code}")
        return r.content

    def download(self, url: str, dest: Path) -> Path:
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        if not _is_pixabay_cdn(url):
            raise PixabayError("URL không thuộc Pixabay, bỏ qua")
        tmp = dest.with_suffix(dest.suffix + ".part")
        size = 0
        with self._cdn.stream("GET", url) as r:
            if r.status_code != 200:
                raise PixabayError(f"Tải file lỗi {r.status_code}")
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        f.close()
                        tmp.unlink(missing_ok=True)
                        raise PixabayError("File quá lớn, bỏ qua")
                    f.write(chunk)
        tmp.replace(dest)
        return dest


# =============================================================================
# CHUẨN HOÁ ỨNG VIÊN (video + ảnh về cùng một dạng)
# =============================================================================
def _pick_rendition(hit: dict, w: int, h: int) -> Optional[dict]:
    """Bản video nhỏ nhất mà phủ kín khung không phải phóng quá 1.25 lần; không có thì lấy bản lớn nhất."""
    vids = hit.get("videos") or {}
    avail = [vids[k] for k in _RENDITIONS if isinstance(vids.get(k), dict) and vids[k].get("url")
             and vids[k].get("width") and vids[k].get("height")]
    if not avail:
        return None
    for v in avail:
        if max(w / v["width"], h / v["height"]) <= 1.25:
            return v
    return avail[-1]


def video_candidate(hit: dict, w: int, h: int) -> Optional[dict]:
    rend = _pick_rendition(hit, w, h)
    if not rend:
        return None
    vids = hit.get("videos") or {}
    thumb = next((vids[k].get("thumbnail") for k in ("small", "medium", "tiny", "large")
                  if isinstance(vids.get(k), dict) and vids[k].get("thumbnail")), "")
    return {"kind": "video", "id": f"v{hit.get('id')}", "tags": str(hit.get("tags") or ""),
            "url": rend["url"], "width": rend["width"], "height": rend["height"],
            "duration": float(hit.get("duration") or 0), "thumb": thumb,
            "credit": hit.get("user", ""), "page_url": hit.get("pageURL", "")}


def image_candidate(hit: dict) -> Optional[dict]:
    url = hit.get("largeImageURL") or hit.get("webformatURL")
    if not url:
        return None
    return {"kind": "image", "id": f"i{hit.get('id')}", "tags": str(hit.get("tags") or ""),
            "url": url, "width": int(hit.get("imageWidth") or 0), "height": int(hit.get("imageHeight") or 0),
            "duration": 0.0, "thumb": hit.get("webformatURL") or hit.get("previewURL") or "",
            "credit": hit.get("user", ""), "page_url": hit.get("pageURL", "")}


def _words(s: str) -> list[str]:
    return [x for x in re.findall(r"[a-z]+", s.lower()) if x not in _STOP and len(x) > 1]


def tag_relevance(tags: str, query: str) -> float:
    """Tỷ lệ từ của truy vấn xuất hiện trong tag (so khớp tiền tố 4 ký tự: market~markets)."""
    qw = _words(query)
    tw = _words(tags)
    if not qw:
        return 0.0
    hit = sum(1 for q in qw if any(t.startswith(q[:4]) or q.startswith(t[:4]) for t in tw if len(t) >= 3))
    return hit / len(qw)


def frame_fit(c: dict, w: int, h: int, need_dur: float) -> float:
    if not c.get("width") or not c.get("height"):
        return 0.0
    s = 1.0 if (h > w) == (c["height"] > c["width"]) else 0.0
    if c["kind"] == "video":
        s += 0.5 if c["duration"] >= need_dur else -0.3
    s -= max(0.0, max(w / c["width"], h / c["height"]) - 1.0)
    return s


# =============================================================================
# BƯỚC 1: OPUS LẬP KẾ HOẠCH TỪ KHOÁ
# =============================================================================
def _claude_json(anthropic_key: str, content: Any, max_tokens: int = 4000) -> list:
    import anthropic
    from text_effects import _extract_json_array
    client = anthropic.Anthropic(api_key=anthropic_key)
    preferred = os.getenv("BRAIN_MODEL") or "claude-opus-5-5"
    last: Exception = RuntimeError("không có model")
    for model in dict.fromkeys([preferred, "claude-opus-5-5", "claude-sonnet-5-5"]):
        try:
            res = client.messages.create(model=model, max_tokens=max_tokens,
                                         messages=[{"role": "user", "content": content}])
            if res.stop_reason == "refusal":
                last = RuntimeError("model từ chối")
                continue
            raw = "".join(b.text for b in res.content if b.type == "text")
            return _extract_json_array(raw)
        except Exception as e:  # thử model kế tiếp
            last = e
    raise last


def _plan_prompt(speech_text: str, goal: str, rows: list[dict]) -> str:
    lines = "\n".join(f"- Cảnh #{r['slot'] + 1} [{r['time']}]: lời đang đọc: \"{r['excerpt']}\"" for r in rows)
    return f"""Bạn là biên tập viên hình ảnh (B-roll researcher) cho video tiếng Việt dưới đây.
Một số cảnh minh hoạ có thể thay bằng VIDEO/ẢNH THẬT tìm trên kho Pixabay. Hãy đọc KỸ toàn bộ kịch bản,
hiểu chủ đề chung, rồi với từng cảnh trong danh sách, quyết định hình ảnh thật nào minh hoạ đúng ý đang nói.

Yêu cầu:
- "scene": 1 câu tiếng Việt mô tả cụ thể người xem cần THẤY gì (vật thể, người, hành động, bối cảnh).
- "queries": đúng 3 truy vấn TIẾNG ANH, 1-3 từ mỗi truy vấn, từ cụ thể nhất đến rộng nhất.
  Dùng danh từ/hành động nhìn thấy được, kiểu từ khoá trong kho stock
  (vd "stock chart screen", "trader computer", "coins"); KHÔNG dùng khái niệm trừu tượng
  ("risk", "strategy", "success") vì kho stock trả về hình chung chung, lạc đề.
- Hình phải khớp CHỦ ĐỀ của cả video (vd video về tài chính thì "growth" phải là biểu đồ/tiền, không phải cây cối).
- "media": "video" nếu cảnh cần chuyển động, "image" nếu một bức ảnh đẹp là đủ.
- "use": false nếu không có hình thật nào minh hoạ tốt (khi đó đồ hoạ chữ giữ nguyên, như vậy tốt hơn hình lạc đề).

{"Mục tiêu video: " + goal if goal else ""}
TOÀN BỘ KỊCH BẢN:
\"\"\"{speech_text}\"\"\"

CÁC CẢNH CẦN XÉT ({len(rows)} cảnh):
{lines}

Chỉ trả về JSON array, mỗi phần tử:
{{"slot": <số cảnh>, "use": true/false, "scene": "...", "queries": ["...", "...", "..."], "media": "video"|"image"}}"""


def plan_stock_queries(speech_text: str, goal: str, rows: list[dict], anthropic_key: str) -> dict[int, dict]:
    items = _claude_json(anthropic_key, _plan_prompt(speech_text, goal, rows))
    valid = {r["slot"] for r in rows}
    out: dict[int, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            slot = int(it.get("slot")) - 1
        except (TypeError, ValueError):
            continue
        qs = [_safe_query(q) for q in (it.get("queries") or []) if _safe_query(q)][:3]
        if slot in valid and it.get("use") and qs:
            out[slot] = {"scene": str(it.get("scene") or ""), "queries": qs,
                         "media": "image" if it.get("media") == "image" else "video"}
    return out


# =============================================================================
# BƯỚC 2: TÌM ỨNG VIÊN
# =============================================================================
def gather_candidates(client: PixabayClient, plan: dict, w: int, h: int, need_dur: float,
                      log: Log) -> list[dict]:
    portrait = h > w
    seen: set = set()
    pool: list[tuple[float, dict]] = []
    order = ["video", "image"] if plan["media"] == "video" else ["image", "video"]
    for q in plan["queries"]:
        for kind in order:
            try:
                if kind == "video":
                    cands = [video_candidate(x, w, h) for x in client.search_videos(q)]
                else:
                    cands = [image_candidate(x) for x in client.search_images(q, portrait)]
            except PixabayError:
                raise
            for c in cands:
                if not c or c["id"] in seen or not c["thumb"]:
                    continue
                seen.add(c["id"])
                rel = max(tag_relevance(c["tags"], qq) for qq in plan["queries"])
                if rel < 0.5:  # tag không chứa từ khoá chính -> gần như chắc chắn lạc đề
                    continue
                c["query"] = q
                pref = 1.2 if kind == plan["media"] else 0.0  # đúng loại Opus yêu cầu thắng lợi thế chiều khung
                pool.append((rel * 2 + frame_fit(c, w, h, need_dur) + pref, c))
        if len(pool) >= MAX_CANDIDATES * 2:
            break  # truy vấn cụ thể đã đủ ứng viên, không cần nới rộng
    pool.sort(key=lambda p: -p[0])
    return [c for _, c in pool[:MAX_CANDIDATES]]


# =============================================================================
# BƯỚC 3: OPUS XEM THUMBNAIL VÀ CHỌN
# =============================================================================
def _thumb_b64(data: bytes, max_side: int = 384) -> str:
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def vision_pick(client: PixabayClient, speech_text: str, goal: str, slots: dict[int, dict],
                anthropic_key: str, log: Log) -> dict[int, tuple[int, int]]:
    """slots: {slot: {"scene", "excerpt", "cands"}} -> {slot: (chỉ số ứng viên, điểm 0-10)}"""
    content: list[dict] = [{"type": "text", "text": (
        "Bạn là biên tập viên hình ảnh. Video tiếng Việt có chủ đề như kịch bản sau"
        + (f" (mục tiêu: {goal})" if goal else "") + ":\n\"\"\"" + speech_text[:4000] + "\"\"\"\n\n"
        "Với mỗi cảnh dưới đây, tôi gửi vài ảnh ứng viên (ảnh thumbnail của video hoặc ảnh stock). "
        "Hãy NHÌN từng ảnh và chọn ảnh minh hoạ ĐÚNG NHẤT cho lời đang đọc và mô tả cảnh. "
        "Chấm điểm 0-10: 8-10 = khớp rõ ràng; 6-7 = chấp nhận được; dưới 6 = lạc đề/chung chung/gây hiểu nhầm. "
        "Loại ảnh có chữ/logo nổi bật, watermark, hoặc nội dung lệch chủ đề video. "
        "Nếu không ảnh nào đạt 6 thì pick = -1.")}]
    index: dict[int, list[int]] = {}
    for slot, info in slots.items():
        content.append({"type": "text", "text": f"\n=== CẢNH #{slot + 1} ===\nLời đang đọc: \"{info['excerpt']}\"\n"
                                                f"Cần thấy: {info['scene']}"})
        index[slot] = []
        for ci, c in enumerate(info["cands"]):
            try:
                b64 = _thumb_b64(client.fetch_bytes(c["thumb"]))
            except Exception:
                continue
            content.append({"type": "text", "text": f"Cảnh #{slot + 1} – ứng viên {ci} ({'video' if c['kind'] == 'video' else 'ảnh'}, tag: {c['tags'][:80]}):"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
            index[slot].append(ci)
    content.append({"type": "text", "text": "\nChỉ trả về JSON array: "
                                            "[{\"slot\": <số cảnh>, \"pick\": <số ứng viên hoặc -1>, \"score\": <0-10>, \"why\": \"<ngắn>\"}]"})
    if not any(index.values()):
        return {}
    items = _claude_json(anthropic_key, content, max_tokens=3000)
    out: dict[int, tuple[int, int]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            slot, pick, score = int(it["slot"]) - 1, int(it.get("pick", -1)), int(it.get("score", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if slot in index and pick in index[slot]:
            out[slot] = (pick, score)
            log(f"     👁️ Cảnh #{slot + 1}: chọn ứng viên {pick} ({score}/10) – {str(it.get('why', ''))[:90]}")
        elif slot in index:
            log(f"     👁️ Cảnh #{slot + 1}: không ứng viên nào đủ khớp – {str(it.get('why', ''))[:90]}")
    return out


# =============================================================================
# ĐIỀU PHỐI
# =============================================================================
def find_stock_media(
    speech_text: str,
    cards: list[dict],
    slots: list[dict],
    w: int,
    h: int,
    seg_dur: float,
    pixabay_key: str,
    anthropic_key: str,
    cache_dir: Path,
    goal: str = "",
    max_ratio: float = 0.5,
    on_status: Optional[Log] = None,
    client: Optional[PixabayClient] = None,
    planner: Callable = plan_stock_queries,
    picker: Callable = vision_pick,
) -> dict[int, dict]:
    """Trả về {chỉ số cảnh minh hoạ: {"path", "kind", "credit", "page_url", "query", "score"}}."""
    log = on_status or (lambda s: None)
    eligible = [i for i, c in enumerate(cards)
                if c.get("template") not in _DATA_TEMPLATES and c.get("template") != "climax"]
    limit = int(len(cards) * max_ratio + 0.5)
    if not eligible or limit <= 0 or not anthropic_key:
        return {}

    rows = [{"slot": i, "time": (slots[i].get("time_range", "") if i < len(slots) else ""),
             "excerpt": (slots[i].get("spoken_excerpt", "") if i < len(slots) else cards[i].get("sub_text", ""))}
            for i in eligible]
    try:
        plan = planner(speech_text, goal, rows, anthropic_key)
    except Exception as e:
        log(f"  ⚠️ [Pixabay] Opus không lập được danh sách từ khoá ({type(e).__name__}), giữ cảnh đồ hoạ.")
        return {}
    if not plan:
        log("  🔎 [Pixabay] Opus đánh giá không cảnh nào nên dùng hình thật, giữ đồ hoạ.")
        return {}
    for s, p in sorted(plan.items()):
        log(f"  🧠 Cảnh #{s + 1}: {p['scene']} → {', '.join(p['queries'])} ({p['media']})")

    own = client is None
    try:
        client = client or PixabayClient(pixabay_key, cache_dir)
    except PixabayError as e:
        log(f"  ⚠️ [Pixabay] {e}")
        return {}
    out: dict[int, dict] = {}
    try:
        pick_in: dict[int, dict] = {}
        for s, p in sorted(plan.items()):
            try:
                cands = gather_candidates(client, p, w, h, seg_dur, log)
            except PixabayError as e:
                log(f"  ⚠️ [Pixabay] {e}. Giữ cảnh đồ hoạ.")
                if any(code in str(e) for code in ("400", "401", "403")):
                    return {}  # key sai / bị chặn: dừng hẳn
                continue
            if cands:
                excerpt = next((r["excerpt"] for r in rows if r["slot"] == s), "")
                pick_in[s] = {"scene": p["scene"], "excerpt": excerpt, "cands": cands}
                log(f"  🔎 Cảnh #{s + 1}: {len(cands)} ứng viên khớp tag")
            else:
                log(f"  🔎 Cảnh #{s + 1}: Pixabay không có hình khớp tag, giữ đồ hoạ")
        if not pick_in:
            return {}

        log(f"  👁️ [Opus] Đang xem thumbnail {sum(len(v['cands']) for v in pick_in.values())} ứng viên để chọn hình đúng ý...")
        try:
            picks = picker(client, speech_text, goal, pick_in, anthropic_key, log)
        except Exception as e:
            log(f"  ⚠️ [Pixabay] Opus không duyệt được thumbnail ({type(e).__name__}); "
                "không chèn hình chưa được duyệt.")
            return {}

        good = sorted(((sc, s, ci) for s, (ci, sc) in picks.items() if sc >= MIN_PICK_SCORE), reverse=True)[:limit]
        used: set = set()
        for sc, s, ci in good:
            c = pick_in[s]["cands"][ci]
            if c["id"] in used:
                continue
            ext = ".mp4" if c["kind"] == "video" else (Path(httpx.URL(c["url"]).path).suffix or ".jpg")
            dest = Path(cache_dir) / f"pixabay_{c['id']}_{c['width']}x{c['height']}{ext}"
            try:
                client.download(c["url"], dest)
            except Exception as e:
                log(f"  ⚠️ [Pixabay] Cảnh #{s + 1}: tải lỗi ({str(e)[:80]}), giữ đồ hoạ.")
                continue
            used.add(c["id"])
            out[s] = {"path": dest, "kind": c["kind"], "credit": c["credit"], "page_url": c["page_url"],
                      "query": c["query"], "score": sc, "duration": c["duration"]}
            log(f"  🎥 Cảnh #{s + 1}: {'video' if c['kind'] == 'video' else 'ảnh'} {c['id']} "
                f"\"{c['query']}\" ({sc}/10, by {c['credit'] or '?'})")
    finally:
        if own:
            client.close()
    return out


# =============================================================================
# RENDER VÀO KHUNG
# =============================================================================
def _probe_duration(ffprobe: str, src: Path) -> float:
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
                           capture_output=True, text=True, check=True)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def render_stock_clip(src: Path, out: Path, w: int, h: int, duration: float, fps: int,
                      ffmpeg_bin: str = "ffmpeg", ffprobe_bin: str = "ffprobe", variant: int = 0,
                      kind: Optional[str] = None) -> Path:
    """Video: phủ kín khung, bỏ tiếng, lặp nếu ngắn. Ảnh: Ken Burns (zoom chậm, đổi hướng theo variant)."""
    kind = kind or ("image" if Path(src).suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") else "video")
    grade = "eq=saturation=1.05:contrast=1.03"
    if kind == "image":
        frames = max(1, int(round(duration * fps)))
        W2, H2 = w * 2, h * 2  # zoompan trên ảnh gấp đôi rồi thu nhỏ -> chuyển động mượt, không giật
        z = f"1+0.10*on/{frames}" if variant % 2 == 0 else f"1.10-0.10*on/{frames}"
        vf = (f"scale={W2}:{H2}:force_original_aspect_ratio=increase:flags=lanczos,crop={W2}:{H2},setsar=1,"
              f"zoompan=z='{z}':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W2}x{H2}:fps={fps},"
              f"scale={w}:{h}:flags=lanczos,{grade},format=yuv420p")
        inp = ["-loop", "1", "-i", str(src)]
    else:
        src_dur = _probe_duration(ffprobe_bin, src)
        start = 0.0
        if src_dur > duration + 1.0:  # bỏ ~0.5s đầu (hay có fade-in); clip dài thì đổi điểm vào
            start = min(0.5 + (variant % 3) * 1.5, max(0.0, src_dur - duration - 0.2))
        loop = ["-stream_loop", "-1"] if src_dur and src_dur - start < duration else []
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},setsar=1,"
              f"{grade},fps={fps},format=yuv420p")
        inp = [*loop, "-ss", f"{start:.2f}", "-i", str(src)]
    cmd = [ffmpeg_bin, "-y", "-loglevel", "error", *inp, "-t", f"{duration:.3f}", "-vf", vf, "-an",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(fps), str(out)]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg lỗi khi dựng {kind} Pixabay: {res.stderr[-400:]}")
    return out


def write_credits(clips: dict[int, dict], out_video: Path) -> Optional[Path]:
    """Ghi nguồn clip cạnh video (Pixabay không bắt buộc ghi công, nhưng nên giữ để tra lại)."""
    if not clips:
        return None
    p = out_video.with_suffix(".credits.txt")
    lines = ["Hình ảnh / video B-roll từ Pixabay (Pixabay Content License):"]
    for i in sorted(clips):
        c = clips[i]
        lines.append(f"- Cảnh minh hoạ #{i + 1} ({c.get('kind', 'video')}, \"{c.get('query', '')}\", "
                     f"{c.get('score', '?')}/10): {c.get('page_url', '')} (tác giả: {c.get('credit', '')})")
    p.write_text("\n".join(lines) + "\n", "utf-8")
    return p
