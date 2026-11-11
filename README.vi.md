# Video Agent Studio

[English](README.md) · **Tiếng Việt**

Công cụ làm video ngắn tiếng Việt bằng nhiều mô hình AI phối hợp: Claude Opus lên kịch bản và dựng, Higgsfield quay, Vbee đọc lời, Pixabay cung cấp cảnh quay thật. Chạy trên máy bạn qua giao diện web.

Có hai chế độ:

- **Phim phân cảnh**: Opus chia ý tưởng thành danh sách shot, Haiku/Sonnet viết prompt cho từng shot, Higgsfield báo giá rồi render sau khi bạn duyệt, Opus xem lại 3 khung hình mỗi shot để chấm điểm và cho làm lại nếu cần.
- **Người dẫn nói**: một ảnh chân dung và lời thoại thành video người dẫn nói xen kẽ cảnh minh hoạ. Chỉ tốn một clip AI 5 giây (hoặc 0 đồng nếu dùng hiệu ứng zoom).

## Chế độ người dẫn làm gì

1. Opus viết lời thoại (hoặc dùng lời bạn dán), Vbee đọc thành giọng tiếng Việt.
2. Canh thời gian cho từng từ ngay trên file giọng: dò khoảng lặng và từng âm tiết bằng ffmpeg + numpy, tự thích ứng với tốc độ đọc (0.9x đến 1.5x).
3. Opus đọc kịch bản, chọn đoạn dẫn chuyện để người nói, ý chính chuyển sang minh hoạ. Điểm cắt rơi vào chỗ ngắt hơi.
4. Cảnh minh hoạ: Claude dựng đồ hoạ chữ động từ chính lời thoại (con số lớn, biểu đồ, hoá đơn, bảng so sánh...). Một số cảnh dùng video/ảnh thật từ Pixabay, do Opus chọn từ khoá và xem ảnh thu nhỏ để duyệt.
5. Phụ đề nhảy từng từ khớp giọng đọc, chuyển cảnh, hiệu ứng âm thanh tự tổng hợp, rồi ghép thành một file MP4.

## Trước khi bắt đầu

- **Cần tài khoản API của riêng bạn.** Claude và Higgsfield trả phí, Vbee trả phí khi lồng tiếng, Pixabay miễn phí. Chế độ chạy thử chạy cả luồng mà không cần key và không tốn tiền, nên có thể thử trước.
- **ffmpeg phải cài riêng.** `pip` không cài ffmpeg.
- **Viết và thử trên Windows.** Chạy được ở hệ khác, nhưng trên macOS/Linux đồ hoạ sẽ dùng font mặc định của Pillow, trừ khi bạn trỏ `_FONT_DIRS` trong `text_effects.py` tới một font có dấu tiếng Việt.
- **Chế độ người dẫn tải ảnh chân dung lên dịch vụ lưu file công khai.** Higgsfield cần link ảnh công khai, nên khi chạy thật (không phải chạy thử) ảnh được tải lên catbox.moe (công khai, không tự xoá) hoặc tmpfiles.org. Đừng dùng ảnh bạn không muốn công khai.
- **Model Higgsfield tuỳ theo tài khoản.** Model nào trong danh sách tài khoản bạn không dùng được thì chọn model khác hoặc đặt `HF_VIDEO_MODEL`.

## Cài đặt

Cần Python 3.10+ và `ffmpeg`/`ffprobe` trong PATH (hoặc đặt ở `C:\ffmpeg\` hay `C:\ffmpeg\bin\` trên Windows).

```bash
pip install -r requirements.txt
cp .env.example .env      # rồi điền key, hoặc điền trong giao diện web (nút "API keys")
```

| Biến | Dùng cho | Bắt buộc? |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude (kịch bản, dựng, chọn cảnh) | Có, trừ khi chạy thử |
| `HF_API_KEY_ID`, `HF_API_KEY_SECRET` | Higgsfield, tạo tại https://console.higgsfield.ai | Có, trừ khi chạy thử |
| `VBEE_APP_ID`, `VBEE_ACCESS_TOKEN` | Giọng đọc Vbee (https://vbee.vn) | Khi bật lồng tiếng |
| `PIXABAY_API_KEY` | Video/ảnh B-roll (https://pixabay.com/api/docs) | Không |

**Font:** đồ hoạ dùng bộ font SVN-Gilroy nếu máy có cài. Font này không kèm trong repo; không có thì code tự dùng font đậm có sẵn của Windows.

## Chạy

```bash
python web_app.py          # mở http://localhost:8080  (đổi cổng bằng biến PORT)
```

Trên Windows có thể chạy `start_web.bat`. Bật **Chạy thử** trong form để kiểm tra cả luồng mà không tốn tiền API.

Chạy bằng dòng lệnh (chế độ phân cảnh):

```bash
python agent.py "Video quảng cáo 15 giây cho trà thảo mộc, 9:16, tông ấm" --budget 5
```

## Cấu trúc

| File | Nhiệm vụ |
|---|---|
| `web_app.py`, `templates/index.html` | Server Tornado và giao diện web |
| `agent.py`, `higgsfield_client.py` | Agent chế độ phân cảnh và client Higgsfield |
| `voice_client.py` | Client Vbee, ghép giọng vào video |
| `word_timing.py` | Canh thời gian từng từ từ file giọng |
| `shot_planner.py` | Chọn đoạn người nói / đoạn minh hoạ |
| `text_effects.py` | Đồ hoạ chữ động và phụ đề |
| `speaker_overlay.py` | Thẻ tóm ý và phụ đề trên cảnh người nói |
| `stock_footage.py` | Tìm, duyệt, tải cảnh quay Pixabay |
| `sfx.py` | Hiệu ứng âm thanh tổng hợp bằng numpy |
| `avatar_director.py` | Dựng và ghép video người dẫn |

## Test

```bash
python -m pytest -q
```

Test không cần API key: các lời gọi Claude, Higgsfield, Pixabay đều được giả lập. Một số test cần ffmpeg. Nếu pytest báo `INTERNALERROR ... NodeId ... _str_cache`, đó là lỗi của pytest 9.2.0: chạy `pip install "pytest<9.2"`.

## Các chốt an toàn

| Cơ chế | Tác dụng |
|---|---|
| Ngân sách, số shot, số lần thử tối đa | Chặn cứng trong code, Opus không tự vượt được |
| Hỏi duyệt trước mỗi lượt render | Người duyệt chi tiền (tắt bằng "Tự duyệt chi phí") |
| `Idempotency-Key` cho mỗi lần gửi Higgsfield | Retry khi lỗi mạng không bị tính phí hai lần |
| Key chỉ gửi tới đúng API của nó | Không gửi key tới CDN, không ghi key ra log |
| Pixabay: cache tìm kiếm 24 giờ, tải file về máy | Theo điều khoản Pixabay API |

File video Higgsfield chỉ được lưu 7 ngày trên máy chủ của họ; agent tải về `./output` ngay khi xong.
