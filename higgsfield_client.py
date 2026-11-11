"""Client mỏng và an toàn cho Higgsfield API (https://docs.higgsfield.ai).

Theo tài liệu chính thức:
- Base URL: https://api.higgsfield.ai
- Xác thực: header `Authorization: Key <key-id>:<key-secret>` (chỉ dùng phía server)
- Sinh media là bất đồng bộ: POST model endpoint -> request_id/status_url -> poll
- Trạng thái kết thúc: completed | failed | nsfw | canceled (failed/nsfw không bị tính phí)
- 429 kèm Retry-After; Idempotency-Key giúp retry không tạo request trùng
- File output chỉ giữ 7 ngày -> phải tải về ngay
"""
from __future__ import annotations

import random
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

BASE_URL = "https://api.higgsfield.ai"
TERMINAL = {"completed", "failed", "nsfw", "canceled"}


class HiggsfieldError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None,
                 correlation_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.correlation_id = correlation_id


class GenerationTimeout(HiggsfieldError):
    pass


class GenerationFailed(HiggsfieldError):
    def __init__(self, message: str, *, request_id: str, status: str):
        super().__init__(message)
        self.request_id = request_id
        self.status = status


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return min(max(0.0, float(resp.headers.get("Retry-After"))), 120.0)
    except (TypeError, ValueError):
        return None


def _error(resp: httpx.Response) -> HiggsfieldError:
    try:
        body = resp.json()
        detail = body.get("detail", body) if isinstance(body, dict) else body
    except ValueError:
        detail = resp.text
    return HiggsfieldError(
        f"HTTP {resp.status_code}: {str(detail)[:500]}",
        status_code=resp.status_code,
        correlation_id=resp.headers.get("X-Correlation-ID"),
    )


def output_urls(result: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in ("video", "audio"):
        item = result.get(key)
        if isinstance(item, dict) and item.get("url"):
            urls.append(item["url"])
    for item in result.get("images") or []:
        if isinstance(item, dict) and item.get("url"):
            urls.append(item["url"])
    return urls


class HiggsfieldClient:
    def __init__(self, key_id: str, key_secret: str, *, base_url: str = BASE_URL,
                 timeout: float = 60.0, max_attempts: int = 5,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        if not key_id or not key_secret:
            raise ValueError("Thiếu HF_API_KEY_ID hoặc HF_API_KEY_SECRET")
        if key_secret.startswith(f"{key_id}:"):
            key_secret = key_secret[len(key_id) + 1:]
        self._auth = f"Key {key_id}:{key_secret}"
        self._host = urlparse(base_url).netloc
        self._max_attempts = max_attempts
        self._sleep = sleep
        # Không follow redirect để tránh lộ header Authorization sang host khác.
        self._http = httpx.Client(base_url=base_url, timeout=timeout,
                                  transport=transport, follow_redirects=False)

    # ---- nội bộ -------------------------------------------------------
    def _guard(self, url: str) -> None:
        """Chỉ gửi credential tới đúng host của Higgsfield."""
        if url.startswith(("http://", "https://")) and urlparse(url).netloc != self._host:
            raise HiggsfieldError(f"Từ chối gọi host lạ kèm credential: {urlparse(url).netloc}")

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(2.0 ** attempt, 30.0) + random.uniform(0, 1)

    def _request(self, method: str, url: str, *, json: Any = None,
                 headers: dict[str, str] | None = None,
                 max_attempts: int | None = None) -> httpx.Response:
        self._guard(url)
        limit = max_attempts or self._max_attempts
        hdrs = {"Authorization": self._auth, **(headers or {})}
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._http.request(method, url, json=json, headers=hdrs)
            except httpx.TransportError as exc:
                if attempt >= limit:
                    raise HiggsfieldError(f"Lỗi mạng: {exc}") from exc
                self._sleep(self._backoff(attempt))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= limit:
                    raise _error(resp)
                delay = _retry_after(resp)
                self._sleep(delay if delay is not None else self._backoff(attempt))
                continue
            if resp.status_code >= 400:
                raise _error(resp)
            return resp

    # ---- API ----------------------------------------------------------
    def estimate(self, model: str, params: dict[str, Any]) -> Decimal | None:
        """Ước tính chi phí (USD). Trả None nếu không lấy được."""
        try:
            resp = self._request("POST", f"/estimate/{model.lstrip('/')}",
                                 json=params, max_attempts=2)
            return Decimal(str(resp.json()["usd"]))
        except (HiggsfieldError, KeyError, InvalidOperation, ValueError, TypeError):
            return None

    def submit(self, model: str, params: dict[str, Any], *,
               idempotency_key: str | None = None) -> dict[str, Any]:
        key = idempotency_key or str(uuid.uuid4())
        resp = self._request("POST", f"/{model.lstrip('/')}", json=params,
                             headers={"Idempotency-Key": key})
        data = resp.json()
        data["idempotency_key"] = key
        return data

    def status(self, status_url_or_id: str) -> dict[str, Any]:
        url = (status_url_or_id if status_url_or_id.startswith("http")
               else f"/requests/{status_url_or_id}/status")
        return self._request("GET", url).json()

    def cancel(self, cancel_url_or_id: str) -> None:
        url = (cancel_url_or_id if cancel_url_or_id.startswith("http")
               else f"/requests/{cancel_url_or_id}/cancel")
        self._request("POST", url, max_attempts=2)

    def wait(self, status_url: str, *, timeout: float = 900.0,
             on_status: Callable[[str], None] | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        delay = 2.0
        while True:
            data = self.status(status_url)
            if on_status:
                on_status(str(data.get("status")))
            if data.get("status") in TERMINAL:
                return data
            if time.monotonic() + delay > deadline:
                raise GenerationTimeout(
                    f"Quá {timeout:.0f}s chờ request {data.get('request_id')} "
                    f"(trạng thái cuối: {data.get('status')})")
            self._sleep(delay + random.uniform(0, 0.5))
            delay = min(delay * 1.5, 10.0)

    def download(self, url: str, dest: Path | str) -> Path:
        """Tải file output. Không gửi credential Higgsfield tới CDN."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with self._http.stream("GET", url) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            tmp.unlink(missing_ok=True)
            raise HiggsfieldError(f"Tải file thất bại: {exc}") from exc
        tmp.replace(dest)
        return dest

    def upload_file(self, local_path: Path | str) -> str:
        """Tải file cục bộ lên hạ tầng Higgsfield bằng presigned URL chính thức.
        Trả về public_url dùng được ngay trong Higgsfield model payload."""
        path = Path(local_path)
        if not path.exists():
            raise FileNotFoundError(f"Không tìm thấy file: {local_path}")

        suffix = path.suffix.lower()
        content_type_map = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".mp4": "video/mp4",
            ".wav": "audio/wav",
        }
        content_type = content_type_map.get(suffix, "image/jpeg")

        # 1. Gọi POST /files/generate-upload-url
        resp = self._request("POST", "/files/generate-upload-url", json={"content_type": content_type})
        data = resp.json()
        upload_url = data["upload_url"]
        public_url = data["public_url"]
        upload_headers = data.get("upload_headers", {})

        # 2. PUT file trực tiếp lên presigned S3/GCS URL (không gửi Authorization header của Higgsfield)
        put_headers = {
            "Content-Type": upload_headers.get("Content-Type", content_type),
        }
        for k, v in upload_headers.items():
            if k.lower() != "content-type":
                put_headers[k] = v

        with open(path, "rb") as fh:
            content = fh.read()

        with httpx.Client(timeout=60.0) as uploader:
            put_resp = uploader.put(upload_url, headers=put_headers, content=content)
            put_resp.raise_for_status()

        return public_url

    def generate(self, model: str, params: dict[str, Any], dest_dir: Path | str,
                 stem: str, *, timeout: float = 900.0,
                 on_status: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Submit -> chờ -> tải về. Raise GenerationFailed nếu failed/nsfw/canceled."""
        sub = self.submit(model, params)
        try:
            result = self.wait(sub["status_url"], timeout=timeout, on_status=on_status)
        except GenerationTimeout:
            try:
                self.cancel(sub["cancel_url"])  # chỉ hủy được khi còn trong hàng đợi
            except HiggsfieldError:
                pass
            raise
        rid = result.get("request_id", sub.get("request_id"))
        if result.get("status") != "completed":
            raise GenerationFailed(
                f"Request {rid} kết thúc với trạng thái {result.get('status')}: "
                f"{result.get('error')}", request_id=rid, status=str(result.get("status")))
        urls = output_urls(result)
        if not urls:
            raise HiggsfieldError(f"Request {rid} completed nhưng không có URL output")
        files = []
        for i, url in enumerate(urls):
            suffix = "" if i == 0 else f"_{i}"
            ext = Path(urlparse(url).path).suffix or ".mp4"
            files.append(str(self.download(url, Path(dest_dir) / f"{stem}{suffix}{ext}")))
        return {"request_id": rid, "status": "completed", "files": files, "urls": urls,
                "expires_at": result.get("expires_at"),
                "idempotency_key": sub["idempotency_key"]}

    def close(self) -> None:
        self._http.close()
