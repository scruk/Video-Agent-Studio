"""Client tích hợp Vbee AIVoice (Bee Voice AI) Text-to-Speech API và công cụ FFmpeg ghép âm thanh vào video.

Tài liệu chính thức Vbee TTS API v1:
- Endpoint gửi yêu cầu: POST https://vbee.vn/api/v1/tts
- Header: Authorization: Bearer <VBEE_ACCESS_TOKEN>
- Body: {"app_id": "...", "input_text": "...", "voice_code": "...", "audio_type": "mp3", "speed_rate": 1.0, "callback_url": "..."}
- Endpoint kiểm tra trạng thái: GET https://vbee.vn/api/v1/tts/<request_id>
- Khi status == "SUCCESS", tải file từ result.audio_link.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import httpx

DEFAULT_VBEE_BASE_URL = "https://vbee.vn/api/v1"
DEFAULT_VOICE_CODE = "hn_female_ngochuyen_full_48k-fhg"

# Danh sách các giọng đọc phổ biến trên Vbee AIVoice
POPULAR_VOICES: list[dict[str, str]] = [
    {
        "code": "hn_female_ngochuyen_full_48k-fhg",
        "name": "HN - Ngọc Huyền (Nữ miền Bắc - Truyền cảm, Quảng cáo/Review)",
        "gender": "female",
        "region": "Miền Bắc",
    },
    {
        "code": "hn_male_manhdung_news_48k-fhg",
        "name": "HN - Mạnh Dũng (Nam miền Bắc - Chuyên nghiệp, Tin tức/TVC)",
        "gender": "male",
        "region": "Miền Bắc",
    },
    {
        "code": "sg_female_thaotrinh_full_48k-fhg",
        "name": "SG - Thảo Trinh (Nữ miền Nam - Tự nhiên, Thuyết minh)",
        "gender": "female",
        "region": "Miền Nam",
    },
    {
        "code": "sg_male_minhhoang_full_48k-fhg",
        "name": "SG - Minh Hoàng (Nam miền Nam - Trầm ấm, Kể chuyện)",
        "gender": "male",
        "region": "Miền Nam",
    },
    {
        "code": "hn_female_maiphuong_vdts_48k-fhg",
        "name": "HN - Mai Phương (Nữ miền Bắc - Trong trẻo, Hiện đại)",
        "gender": "female",
        "region": "Miền Bắc",
    },
    {
        "code": "sg_female_tuongvy_call_44k-fhg",
        "name": "SG - Tường Vy (Nữ miền Nam - Nhẹ nhàng, Chăm sóc KH)",
        "gender": "female",
        "region": "Miền Nam",
    },
    {
        "code": "hue_male_duyphuong_full_48k-fhg",
        "name": "Huế - Duy Phương (Nam miền Trung - Trầm ấm)",
        "gender": "male",
        "region": "Miền Trung",
    },
    {
        "code": "hue_female_huonggiang_full_48k-fhg",
        "name": "Huế - Hương Giang (Nữ miền Trung - Dịu dàng)",
        "gender": "female",
        "region": "Miền Trung",
    },
]


class VoiceError(Exception):
    """Lỗi khi gọi API tạo giọng đọc."""
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def get_ffmpeg_bin(name: str = "ffmpeg") -> str:
    """Tìm binary ffmpeg/ffprobe từ PATH hoặc C:\\ffmpeg."""
    p = shutil.which(name)
    if p:
        return p
    for candidate in (
        Path(f"C:/ffmpeg/{name}.exe"),
        Path(f"C:/ffmpeg/bin/{name}.exe"),
    ):
        if candidate.exists():
            return str(candidate)
    return name


class VbeeVoiceClient:
    """Client giao tiếp với Vbee AIVoice TTS API (bất đồng bộ: submit -> poll -> download)."""

    def __init__(
        self,
        app_id: str,
        access_token: str,
        *,
        base_url: str = DEFAULT_VBEE_BASE_URL,
        default_voice: str = DEFAULT_VOICE_CODE,
        callback_url: str = "https://vbee.vn/api/v1/callback",
        timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.app_id = app_id.strip()
        self.access_token = access_token.strip()
        self.base_url = base_url.rstrip("/")
        self.default_voice = default_voice.strip() or DEFAULT_VOICE_CODE
        self.callback_url = callback_url
        self._sleep = sleep
        self._http = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        self._dl = httpx.Client(timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self._http.close()
        self._dl.close()

    def __enter__(self) -> VbeeVoiceClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def submit(
        self,
        text: str,
        *,
        voice_code: str | None = None,
        speed_rate: float = 1.0,
        audio_type: str = "mp3",
    ) -> dict[str, Any]:
        """Gửi yêu cầu chuyển văn bản thành giọng nói lên Vbee API."""
        if not self.app_id or not self.access_token:
            raise VoiceError("Thiếu VBEE_APP_ID hoặc VBEE_ACCESS_TOKEN. Vui lòng cấu hình trong .env hoặc mục API Keys.")
        clean_text = text.strip()
        if not clean_text:
            raise VoiceError("Nội dung văn bản (text) cần tạo giọng đọc không được để trống.")

        payload = {
            "app_id": self.app_id,
            "input_text": clean_text,
            "voice_code": (voice_code or self.default_voice).strip(),
            "audio_type": audio_type,
            "speed_rate": float(speed_rate),
            "callback_url": self.callback_url,
        }
        resp = self._http.post(f"{self.base_url}/tts", json=payload)
        if resp.status_code >= 400:
            raise VoiceError(f"Vbee HTTP {resp.status_code}: {resp.text[:400]}", status_code=resp.status_code)

        data = resp.json()
        # Vbee trả về {"status": 1, "result": {"request_id": "...", "status": "IN_PROGRESS", ...}}
        if isinstance(data, dict) and data.get("status") == 0:
            err_msg = data.get("error_message") or data.get("message") or str(data)
            raise VoiceError(f"Vbee từ chối yêu cầu: {err_msg}")

        result = data.get("result", data) if isinstance(data, dict) else {}
        return result if isinstance(result, dict) else {"raw": data}

    def poll(
        self,
        request_id: str,
        *,
        timeout: float = 180.0,
        interval: float = 2.0,
        on_status: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Kiểm tra trạng thái request_id cho tới khi có audio_link."""
        deadline = time.monotonic() + timeout
        last_status = None
        while time.monotonic() < deadline:
            resp = self._http.get(f"{self.base_url}/tts/{request_id}")
            if resp.status_code >= 400:
                raise VoiceError(f"Vbee Poll HTTP {resp.status_code}: {resp.text[:400]}", status_code=resp.status_code)
            body = resp.json()
            result = body.get("result", body) if isinstance(body, dict) else {}
            status = str(result.get("status", "")).upper()
            if status and status != last_status:
                last_status = status
                if on_status:
                    on_status(status)

            # Nếu đã có audio_link ngay hoặc status == SUCCESS
            audio_link = result.get("audio_link") or result.get("audio_url") or result.get("url")
            if status in ("SUCCESS", "COMPLETED", "DONE") or audio_link:
                if not audio_link:
                    raise VoiceError(f"Vbee báo hoàn tất ({status}) nhưng không trả về audio_link: {body}")
                return result
            if status in ("FAILURE", "FAILED", "ERROR", "CANCELED"):
                raise VoiceError(f"Tạo giọng đọc thất bại ({status}): {result.get('error_message') or body}")

            self._sleep(interval)
        raise VoiceError(f"Quá thời gian chờ Vbee xử lý ({timeout}s) cho request {request_id}")

    def download(self, url: str, dest: Path) -> Path:
        """Tải file âm thanh từ CDN của Vbee về máy."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._dl.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise VoiceError(f"Không thể tải file âm thanh (HTTP {resp.status_code})", status_code=resp.status_code)
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes(65536):
                    f.write(chunk)
        return dest

    def synthesize(
        self,
        text: str,
        dest_dir: Path,
        *,
        stem: str,
        voice_code: str | None = None,
        speed_rate: float = 1.0,
        audio_type: str = "mp3",
        on_status: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Quy trình trọn gói: Gửi văn bản -> Chờ xử lý -> Tải file âm thanh về dest_dir."""
        sub = self.submit(text, voice_code=voice_code, speed_rate=speed_rate, audio_type=audio_type)
        # Một số endpoint đồng bộ trả thẳng audio_link
        audio_link = sub.get("audio_link") or sub.get("audio_url") or sub.get("url")
        req_id = sub.get("request_id") or sub.get("id") or "sync"
        if not audio_link:
            if not req_id or req_id == "sync":
                raise VoiceError(f"Phản hồi Vbee thiếu request_id và audio_link: {sub}")
            polled = self.poll(str(req_id), on_status=on_status)
            audio_link = polled.get("audio_link") or polled.get("audio_url") or polled.get("url")

        ext = audio_type.lower() if audio_type.lower() in ("mp3", "wav", "ogg") else "mp3"
        out_path = Path(dest_dir) / f"{stem}.{ext}"
        self.download(str(audio_link), out_path)
        return {
            "request_id": req_id,
            "file": out_path.name,
            "path": str(out_path),
            "voice_code": voice_code or self.default_voice,
            "characters": len(text.strip()),
        }


def mux_voice_into_video(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    *,
    replace_audio: bool = True,
) -> Path:
    """Dùng FFmpeg ghép file giọng đọc (audio_path) vào video (video_path).

    Nếu video có sẵn tiếng động nền và replace_audio=False, sẽ trộn (amix) 2 luồng âm thanh.
    Mặc định gắn giọng đọc thuyết minh chuẩn AAC vào video.
    """
    if not video_path.is_file():
        raise FileNotFoundError(f"Không tìm thấy file video: {video_path.name}")
    if not audio_path.is_file():
        raise FileNotFoundError(f"Không tìm thấy file âm thanh: {audio_path.name}")

    ffmpeg_bin = get_ffmpeg_bin("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Ghép video gốc và luồng audio thuyết minh, tự động pad khoảng lặng nếu audio ngắn hơn video
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        str(output_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Lỗi FFmpeg khi ghép giọng đọc vào video: {res.stderr[:400]}")
    return output_path

