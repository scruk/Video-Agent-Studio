"""Lập danh sách shot theo NỘI DUNG lời thoại: dẫn chuyện -> người nói, ý chính -> cảnh minh hoạ.

Luồng:
  1. Chia lời thoại thành các "ý" (câu, hoặc vế câu nếu câu dài) với mốc thời gian thật từ word_timing.
  2. Opus đọc toàn bộ kịch bản, gắn nhãn từng ý: "speaker" (dẫn dắt, chào, chuyển ý, hỏi người xem,
     kể chuyện, kêu gọi) hoặc "visual" (ý chính: con số, so sánh, quy trình, khái niệm, kết luận).
  3. Gộp các ý liền nhau cùng nhãn thành một shot; điểm cắt đặt GIỮA chỗ ngắt hơi giữa hai ý
     (không bao giờ cắt ngang câu). Shot quá ngắn bị nhập vào shot kề bên; shot quá dài được tách
     ở ranh giới ý (người nói -> đổi góc máy, minh hoạ -> sang thẻ minh hoạ mới).
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Optional

_SENT_END = re.compile(r"[.!?…]+[\"'”’)]*$")
_CLAUSE_END = re.compile(r"[,;:]+[\"'”’)]*$")
Log = Callable[[str], None]

MIN_SHOT = 1.8        # shot ngắn hơn: chớp nhoáng, khó đọc -> nhập vào shot kề bên
MIN_BEAT = 1.2


# =============================================================================
# 1. CHIA Ý
# =============================================================================
def split_beats(timeline: list[dict], total_dur: float, clause_min: float = 2.5) -> list[dict]:
    """-> [{"i", "text", "w0", "w1", "start", "end"}] liền mạch phủ [0, total_dur]."""
    if not timeline:
        return []
    groups: list[tuple[int, int]] = []
    s = 0
    for k, x in enumerate(timeline):
        last = k == len(timeline) - 1
        long_enough = timeline[k]["e"] - timeline[s]["s"] >= clause_min
        if last or _SENT_END.search(x["w"]) or (_CLAUSE_END.search(x["w"]) and long_enough):
            groups.append((s, k))
            s = k + 1
    # Nhập ý quá ngắn ("Vâng.", "Đúng vậy,") vào ý trước (hoặc ý sau nếu là ý đầu)
    merged: list[list[int]] = []
    for a, b in groups:
        dur = timeline[b]["e"] - timeline[a]["s"]
        if merged and dur < MIN_BEAT:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    if len(merged) > 1 and timeline[merged[0][1]]["e"] - timeline[merged[0][0]]["s"] < MIN_BEAT:
        merged[1][0] = merged[0][0]
        merged.pop(0)
    # Điểm cắt = giữa khoảng hở (chỗ ngắt hơi) giữa hai ý
    beats = []
    for n, (a, b) in enumerate(merged):
        start = 0.0 if n == 0 else (timeline[merged[n - 1][1]]["e"] + timeline[a]["s"]) / 2
        end = total_dur if n == len(merged) - 1 else (timeline[b]["e"] + timeline[merged[n + 1][0]]["s"]) / 2
        beats.append({"i": n, "text": " ".join(x["w"] for x in timeline[a:b + 1]),
                      "w0": a, "w1": b, "start": round(start, 3), "end": round(end, 3)})
    return beats


# =============================================================================
# 2. GẮN NHÃN
# =============================================================================
def _label_prompt(speech_text: str, beats: list[dict], goal: str) -> str:
    rows = "\n".join(f"{b['i'] + 1}. [{b['start']:.1f}s–{b['end']:.1f}s, {b['end'] - b['start']:.1f}s] {b['text']}"
                     for b in beats)
    return f"""Bạn là đạo diễn dựng phim cho video ngắn dạng người dẫn nói trước camera, xen kẽ cảnh minh hoạ
(infographic động: con số, biểu đồ, bảng so sánh, hoá đơn, hoặc video/ảnh thật).

Đọc TOÀN BỘ kịch bản rồi quyết định từng ý dưới đây nên hiện gì:
- "speaker" (thấy mặt người dẫn): câu mở đầu/chào/hook, dẫn dắt, chuyển ý ("tiếp theo", "vậy thì"),
  câu hỏi hướng tới người xem, kể trải nghiệm cá nhân, bình luận cảm xúc, kêu gọi hành động (follow, like, comment).
- "visual" (cảnh minh hoạ, không thấy mặt): Ý CHÍNH người xem cần nhớ: con số, phép tính, so sánh,
  liệt kê/các bước, định nghĩa khái niệm, ví dụ cụ thể, kết quả, câu kết luận then chốt.

Nguyên tắc nhịp dựng:
- Mở đầu bằng người dẫn (người xem cần thấy mặt để tin), trừ khi câu đầu là một con số gây sốc.
- Cảnh minh hoạ chiếm khoảng 40–60% thời lượng. Không để người dẫn nói liền quá ~12 giây,
  cũng không để minh hoạ liền quá ~15 giây mà không quay lại mặt người dẫn.
- Ý chính nằm ở nửa sau câu thì vẫn chọn "visual" cho cả ý đó.

{"Mục tiêu video: " + goal if goal else ""}
TOÀN BỘ KỊCH BẢN:
\"\"\"{speech_text}\"\"\"

CÁC Ý ({len(beats)} ý, đúng thứ tự thời gian):
{rows}

Chỉ trả về JSON array gồm đúng {len(beats)} phần tử theo thứ tự:
[{{"beat": <số ý>, "shot": "speaker"|"visual", "why": "<lý do ngắn>"}}]"""


_VISUAL_HINT = re.compile(r"\d|%|so với|gấp|lần|bước|thứ nhất|thứ hai|đầu tiên|cuối cùng|công thức|nghĩa là|tức là|"
                          r"kết quả|tổng|trung bình|tỷ lệ|tỉ lệ|chi phí|lợi nhuận", re.I)
_SPEAKER_HINT = re.compile(r"xin chào|chào các bạn|hôm nay|các bạn có|bạn có|bạn đã|theo dõi|follow|đăng ký|"
                           r"like|comment|bình luận|mình nghĩ|tôi nghĩ|tiếp theo|vậy thì|\?", re.I)


def heuristic_labels(beats: list[dict]) -> list[str]:
    """Không gọi được Claude: ý có số/so sánh -> minh hoạ; chào hỏi/dẫn dắt/câu hỏi -> người nói."""
    labels = []
    for b in beats:
        t = b["text"]
        if _SPEAKER_HINT.search(t) and not re.search(r"\d", t):
            labels.append("speaker")
        elif _VISUAL_HINT.search(t):
            labels.append("visual")
        else:
            labels.append("")
    if labels and not labels[0]:
        labels[0] = "speaker"
    # Ý trung tính: xen kẽ với ý trước để video không đứng một kiểu quá lâu
    for k in range(len(labels)):
        if not labels[k]:
            labels[k] = "visual" if labels[k - 1] == "speaker" else "speaker"
    return labels


def label_beats(speech_text: str, beats: list[dict], anthropic_key: str, goal: str = "",
                log: Optional[Log] = None) -> tuple[list[str], str]:
    """-> (nhãn từng ý, nguồn nhãn: 'opus' | 'heuristic')"""
    log = log or (lambda s: None)
    if anthropic_key and beats:
        try:
            import anthropic
            from text_effects import _extract_json_array
            client = anthropic.Anthropic(api_key=anthropic_key)
            preferred = os.getenv("BRAIN_MODEL") or "claude-opus-5-5"
            for model in dict.fromkeys([preferred, "claude-opus-5-5", "claude-sonnet-5-5"]):
                try:
                    res = client.messages.create(model=model, max_tokens=4000, messages=[
                        {"role": "user", "content": _label_prompt(speech_text, beats, goal)}])
                    if res.stop_reason == "refusal":
                        continue
                    items = _extract_json_array("".join(b.text for b in res.content if b.type == "text"))
                    labels = [""] * len(beats)
                    for it in items:
                        try:
                            k = int(it.get("beat")) - 1
                        except (TypeError, ValueError, AttributeError):
                            continue
                        if 0 <= k < len(beats) and it.get("shot") in ("speaker", "visual"):
                            labels[k] = it["shot"]
                            beats[k]["why"] = str(it.get("why") or "")[:100]
                    if sum(1 for x in labels if x) >= max(1, len(beats) * 0.7):
                        fb = heuristic_labels(beats)
                        return [x or fb[k] for k, x in enumerate(labels)], "opus"
                except Exception:
                    continue
        except Exception:
            pass
    return heuristic_labels(beats), "heuristic"


# =============================================================================
# 3. GỘP THÀNH SHOT
# =============================================================================
def _group(beats: list[dict], labels: list[str]) -> list[dict]:
    shots: list[dict] = []
    for b, lb in zip(beats, labels):
        if shots and shots[-1]["type"] == lb:
            shots[-1]["end"] = b["end"]
            shots[-1]["beats"].append(b)
        else:
            shots.append({"type": lb, "start": b["start"], "end": b["end"], "beats": [b]})
    return shots


def _absorb_short(shots: list[dict]) -> list[dict]:
    """Shot quá ngắn: đổi sang loại của shot kề bên (ngắn hơn trong hai bên) rồi gộp lại."""
    changed = True
    while changed and len(shots) > 1:
        changed = False
        for k, s in enumerate(shots):
            if s["end"] - s["start"] >= MIN_SHOT:
                continue
            nb = [j for j in (k - 1, k + 1) if 0 <= j < len(shots)]
            j = min(nb, key=lambda j: shots[j]["end"] - shots[j]["start"])
            a, b = sorted((j, k))
            shots[a] = {"type": shots[j]["type"], "start": shots[a]["start"], "end": shots[b]["end"],
                        "beats": shots[a]["beats"] + shots[b]["beats"]}
            del shots[b]
            # gộp tiếp nếu giờ hai shot liền nhau cùng loại
            m = []
            for s2 in shots:
                if m and m[-1]["type"] == s2["type"]:
                    m[-1] = {"type": s2["type"], "start": m[-1]["start"], "end": s2["end"],
                             "beats": m[-1]["beats"] + s2["beats"]}
                else:
                    m.append(s2)
            shots[:] = m
            changed = True
            break
    return shots


def _split_long(shot: dict, max_len: float) -> list[dict]:
    """Tách shot dài ở ranh giới ý, mỗi phần gần max_len nhất có thể."""
    if shot["end"] - shot["start"] <= max_len or len(shot["beats"]) < 2:
        return [shot]
    parts, cur = [], [shot["beats"][0]]
    for b in shot["beats"][1:]:
        if b["end"] - cur[0]["start"] > max_len and cur[-1]["end"] - cur[0]["start"] >= MIN_SHOT:
            parts.append(cur)
            cur = [b]
        else:
            cur.append(b)
    if cur:
        if parts and cur[-1]["end"] - cur[0]["start"] < MIN_SHOT:
            parts[-1] += cur
        else:
            parts.append(cur)
    return [{"type": shot["type"], "start": p[0]["start"], "end": p[-1]["end"], "beats": p} for p in parts]


def plan_shots(speech_text: str, timeline: list[dict], total_dur: float, interval: float = 5.0,
               anthropic_key: str = "", goal: str = "", demo_mode: bool = False,
               log: Optional[Log] = None) -> list[dict]:
    """-> [{"type": "speaker"|"visual", "start", "end", "text", "split": bool}] phủ kín [0, total_dur]."""
    log = log or (lambda s: None)
    beats = split_beats(timeline, total_dur)
    if not beats:
        return []
    labels, src = label_beats(speech_text, beats, "" if demo_mode else anthropic_key, goal, log)
    shots = _absorb_short(_group(beats, labels))
    max_visual = max(6.0, interval * 1.6)
    max_speaker = max(8.0, interval * 2.0)
    out: list[dict] = []
    for s in shots:
        parts = _split_long(s, max_visual if s["type"] == "visual" else max_speaker)
        for n, p in enumerate(parts):
            out.append({"type": p["type"], "start": round(p["start"], 3), "end": round(p["end"], 3),
                        "text": " ".join(b["text"] for b in p["beats"]),
                        "why": "; ".join(b.get("why", "") for b in p["beats"] if b.get("why"))[:120],
                        "split": n > 0})
    out[0]["start"], out[-1]["end"] = 0.0, round(total_dur, 3)
    vis = sum(s["end"] - s["start"] for s in out if s["type"] == "visual")
    log(f"🎬 [{'Opus' if src == 'opus' else 'Quy tắc'}] {len(beats)} ý → {len(out)} shot | "
        f"minh hoạ {vis / total_dur * 100:.0f}% thời lượng")
    for k, s in enumerate(out):
        icon = "🗣️" if s["type"] == "speaker" else "✨"
        log(f"   {icon} #{k + 1} [{s['start']:.1f}–{s['end']:.1f}s] \"{s['text'][:60]}{'…' if len(s['text']) > 60 else ''}\""
            + (f" — {s['why']}" if s.get("why") else ""))
    return out


def uniform_shots(total_dur: float, interval: float = 5.0) -> list[dict]:
    """Cách cũ: chia đều, xen kẽ người nói / minh hoạ (dùng khi không có mốc thời gian từng từ)."""
    n = max(1, int(round(total_dur / max(2.5, float(interval)))))
    seg = total_dur / n
    return [{"type": "speaker" if i % 2 == 0 else "visual", "start": i * seg, "end": (i + 1) * seg,
             "text": "", "split": False} for i in range(n)]


def slots_from_shots(shots: list[dict], kind: str, timeline: Optional[list] = None) -> list[dict[str, Any]]:
    """Các shot thuộc một loại -> slot (cùng định dạng build_segment_slots) để soạn thẻ minh hoạ / overlay."""
    out = []
    for k, s in enumerate(shots):
        if s["type"] != kind:
            continue
        st, et = s["start"], s["end"]
        text = s.get("text") or (" ".join(x["w"] for x in (timeline or []) if st - 1e-6 <= x["s"] < et))
        out.append({
            "slot_index": len(out), "segment_index": k, "start_sec": st, "end_sec": et, "duration": et - st,
            "time_range": f"{int(st // 60):02d}:{int(st % 60):02d} - {int(et // 60):02d}:{int(et % 60):02d}",
            "spoken_excerpt": text, "is_last_segment": k == len(shots) - 1,
        })
    return out
