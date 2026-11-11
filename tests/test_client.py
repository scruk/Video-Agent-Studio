import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from higgsfield_client import GenerationFailed, HiggsfieldClient, HiggsfieldError  # noqa: E402

API = "https://api.higgsfield.ai"
CDN = "https://cdn.example.com/out.mp4"


def make_client(handler, sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    return HiggsfieldClient("kid", "sec", transport=httpx.MockTransport(handler),
                            sleep=sleeps.append), sleeps


def test_happy_path_download_and_header_hygiene(tmp_path):
    seen = []
    polls = {"n": 0}

    def handler(req: httpx.Request):
        seen.append(req)
        if req.method == "POST":
            return httpx.Response(200, json={
                "status": "queued", "request_id": "r1",
                "status_url": f"{API}/requests/r1/status",
                "cancel_url": f"{API}/requests/r1/cancel"})
        if req.url.host == "cdn.example.com":
            return httpx.Response(200, content=b"VIDEO")
        polls["n"] += 1
        if polls["n"] < 2:
            return httpx.Response(200, json={"status": "in_progress", "request_id": "r1"})
        return httpx.Response(200, json={"status": "completed", "request_id": "r1",
                                         "expires_at": "2026-11-16T00:00:00Z",
                                         "video": {"url": CDN}})

    client, _ = make_client(handler)
    res = client.generate("higgsfield/cinema-studio/4.0", {"prompt": "x"}, tmp_path, "s01_a1")
    assert (tmp_path / "s01_a1.mp4").read_bytes() == b"VIDEO"
    assert res["status"] == "completed" and res["request_id"] == "r1"

    submit = seen[0]
    assert submit.headers["Authorization"] == "Key kid:sec"
    assert submit.headers["Idempotency-Key"]
    cdn_req = [r for r in seen if r.url.host == "cdn.example.com"][0]
    assert "authorization" not in cdn_req.headers  # không lộ credential cho CDN


def test_429_retry_uses_retry_after_and_same_idempotency_key():
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"},
                                  json={"detail": {"code": "account_queue_full"}})
        return httpx.Response(200, json={"status": "queued", "request_id": "r2",
                                         "status_url": f"{API}/requests/r2/status",
                                         "cancel_url": f"{API}/requests/r2/cancel"})

    client, sleeps = make_client(handler)
    data = client.submit("m/x", {"prompt": "x"})
    assert sleeps == [7.0]
    assert calls[0].headers["Idempotency-Key"] == calls[1].headers["Idempotency-Key"]
    assert data["request_id"] == "r2"


def test_failed_and_nsfw_raise(tmp_path):
    def handler(req: httpx.Request):
        if req.method == "POST":
            return httpx.Response(200, json={"status": "queued", "request_id": "r3",
                                             "status_url": f"{API}/requests/r3/status",
                                             "cancel_url": f"{API}/requests/r3/cancel"})
        return httpx.Response(200, json={"status": "nsfw", "request_id": "r3", "error": None})

    client, _ = make_client(handler)
    with pytest.raises(GenerationFailed) as exc:
        client.generate("m/x", {"prompt": "x"}, tmp_path, "s")
    assert exc.value.status == "nsfw"


def test_refuses_to_send_credentials_to_foreign_host():
    def handler(req):
        raise AssertionError("không được gửi request đi")

    client, _ = make_client(handler)
    with pytest.raises(HiggsfieldError):
        client.status("https://evil.example.com/requests/r1/status")


def test_4xx_not_retried_and_estimate_degrades_to_none():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(403, json={"detail": "Insufficient balance"})

    client, sleeps = make_client(handler)
    with pytest.raises(HiggsfieldError) as exc:
        client.submit("m/x", {"prompt": "x"})
    assert exc.value.status_code == 403 and len(calls) == 1 and sleeps == []
    assert client.estimate("m/x", {"prompt": "x"}) is None
