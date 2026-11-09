# Bot Telegram tìm truyện trong Notion

Bot nhận câu hỏi tự nhiên trên Telegram, nhờ Gemini hiểu câu hỏi, tìm trong bảng truyện Notion
rồi trả lời bằng tiếng Việt. Ngoài việc tìm truyện, bot còn có lệnh `/add` (thêm truyện mới) và
`/edit` (sửa truyện) để ghi vào Notion, và chỉ ghi sau khi bạn bấm **Xác nhận**. Bot không bao giờ
xóa truyện. `/ping` trả lời "pong".
Bot chỉ phản hồi đúng một người dùng (ID trong `TELEGRAM_USER_ID`).

## Cài đặt lần đầu

Cần Python 3.11 trở lên (dự án đang chạy trên Python 3.13). Kiểm tra bằng `python3 --version`.

1. Tạo môi trường ảo (venv) và cài thư viện, trong thư mục dự án:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Tạo file `.env` từ file mẫu và chỉ cho phép riêng bạn đọc nó:

   ```bash
   cp .env.example .env
   chmod 600 .env
   ```

3. Mở `.env` và điền giá trị của bạn:
   - `BOT_TOKEN`: token lấy từ @BotFather trên Telegram.
   - `TELEGRAM_USER_ID`: ID số của bạn (lấy từ bot @userinfobot).
   - `NOTION_TOKEN`: token của Notion integration (notion.so/profile/integrations). Nhớ share bảng truyện cho integration.
   - `NOTION_DATA_SOURCE_ID`: ID data source của bảng truyện.
   - `GEMINI_API_KEY`: khóa API lấy từ Google AI Studio (aistudio.google.com).
   - `GEMINI_MODEL` (tùy chọn): tên model Gemini. Để trống thì dùng mặc định `gemini-3.6-flash`.

   Không chia sẻ file `.env` cho ai và không đưa lên GitHub.

## Kích hoạt môi trường ảo (venv)

Sau lần cài đặt đầu, mỗi lần mở terminal mới bạn chỉ cần chạy lệnh này trong thư mục dự án:

```bash
source .venv/bin/activate
```

Khi thành công, đầu dòng lệnh sẽ có chữ `(.venv)`.
Để thoát venv: `deactivate`.

## Chạy bot

```bash
python bot.py
```

Dừng bot bằng `Ctrl+C`. Khi có lỗi, chi tiết được ghi vào file `logs/bot.log` (không đưa lên GitHub).
Mỗi câu hỏi cũng ghi một dòng lượng token Gemini đã dùng, ví dụ
`usage: requests=2 prompt=3203 output=163 total=3366` (`requests` là số lần gọi Gemini, hữu ích
để theo dõi giới hạn miễn phí 15 lần/phút).

Bot nhớ khoảng 10 câu hỏi gần nhất trong mỗi cuộc trò chuyện (mất khi tắt bot),
nên bạn có thể hỏi nối tiếp như "chỉ lấy truyện đã hoàn thôi".

## Thử nghiệm

Cách bot gợi ý truyện: mỗi lần bạn hỏi, bot xáo trộn ngẫu nhiên các truyện khớp rồi chọn 10 truyện,
nên hỏi lại sẽ ra gợi ý khác. Khi bạn xin gợi ý mà không nói rõ tiến độ đọc, bot mặc định chỉ tìm
truyện **chưa đọc** (và nói rõ điều đó). Nói "xem thêm" để xem 10 truyện tiếp theo của cùng lần tìm.

Thử trong terminal (không cần Telegram):

```bash
python -m tests.test_search   # thử tìm trực tiếp trong Notion
python -m tests.test_gemini   # hỏi 4 câu mẫu qua Gemini
```

Các bài kiểm tra tự động (không ghi gì vào Notion của bạn): `python -m tests.test_write`,
`tests.test_add_flow`, `tests.test_edit_flow`, `tests.test_paging`, `tests.test_errors`, `tests.test_usage`.

Rồi mở Telegram, chat với bot của bạn:

- Gửi `/ping` → bot trả lời `pong`
- Hỏi tự nhiên, ví dụ: `Gợi ý truyện cổ đại cung đấu đã hoàn`

## Thêm truyện mới: /add

Gõ `/add` trong Telegram, bot sẽ hỏi lần lượt: tên, link, tác giả, số chương (từ 1 trở lên),
tags (bấm nút để chọn) và status. Cuối cùng bot hiện bản tóm tắt, bạn bấm **Xác nhận** thì
truyện mới được thêm vào Notion. Gõ `/cancel` bất cứ lúc nào để dừng (chưa ghi gì).

Mặc định: Status = Full, Reading Status = Not started, Record Audio = Not started,
Ghi chú và Ngày để trống. Nếu truyện đã có trong bảng (trùng tên hoặc link), bot sẽ cảnh báo.

Lưu ý: muốn thêm được truyện, integration Notion phải được bật quyền **Insert content**.

## Sửa truyện: /edit

Gõ `/edit`, nhập một phần tên truyện hoặc tên tác giả, rồi chọn truyện trong danh sách.
Bot hiện các giá trị hiện tại và hỏi bạn muốn sửa cột nào (tên, tác giả, số chương, tags, link,
Status, Reading Status, Record Audio, ghi chú, ngày). Bạn nhập giá trị mới, bot hiện
"Trước / Sau" và chỉ ghi vào Notion khi bạn bấm **Xác nhận**. Sau mỗi lần sửa, bot hỏi
"Sửa cột nào nữa?" cho đến khi bạn bấm **Xong**. Chỉ ghi chú và ngày có thể để trống.
Gõ `/cancel` để dừng: những thay đổi đã xác nhận trước đó vẫn được giữ.

Bot không bao giờ xóa truyện. Muốn bỏ một truyện, hãy đổi Reading Status thành Drop.

Lưu ý: muốn sửa được, integration Notion phải được bật quyền **Update content**.
