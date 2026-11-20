# Tìm log và tìm lỗi bằng lời thường: hướng dẫn cho BA và QA

Tài liệu này dành cho người không viết truy vấn Lucene và không cần biết tên field: business analyst, tester,
QA/QC. Bạn hỏi bằng tiếng Việt hoặc tiếng Anh, như khi hỏi một đồng nghiệp dev. graylog-mcp chỉ **đọc** log trong
Graylog, không sửa, không xoá, không tạo gì trong đó.

## 1. Cài đặt và mở trang

Cần [uv](https://docs.astral.sh/uv/). Mở Terminal và chạy:

```bash
uvx --from git+https://github.com/ntbang0901/graylog-mcp graylog-mcp start
```

Lệnh này cài graylog-mcp, chạy nó ở nền, tự chạy lại khi bạn đăng nhập máy và mở trang quản trị trên trình duyệt
(chỉ máy bạn mở được, địa chỉ `127.0.0.1`). Lần sau, mở lại trang bằng `graylog-mcp ui`.

**Nhận cấu hình của team.** Thường một người trong team đã cấu hình sẵn và gửi bạn một file, ví dụ `acme.toml`.
File đó không chứa token hay mật khẩu nào. Nhập nó:

```bash
graylog-mcp bundle import acme.toml   # xem trước những gì được thêm, rồi hỏi bạn có đồng ý không
graylog-mcp login                     # nhập token hoặc mật khẩu Graylog của riêng bạn
```

Trên trang quản trị, màn hình **Config file** cũng có nút nhập gói cấu hình của team. Token của bạn được lưu trên
máy bạn (`~/.config/graylog-mcp/secrets.toml`, chỉ tài khoản của bạn đọc được) và không bao giờ nằm trong file
cấu hình hay gói của team.

## 2. Hỏi trên trang web

Vào màn hình **Logs**. Ô **Ask in plain words** ở trên cùng nhận một câu bình thường:

- "Thanh toán bị lỗi timeout trên prod trong 1 giờ qua"
- "Sáng nay có bao nhiêu lỗi ở staging?"
- "Từ 9h đến 11h hôm qua đơn hàng bị từ chối nhiều không?"
- "Tại sao checkout lỗi kết nối ngân hàng trên prod sáng nay?"

Câu trả lời luôn mở đầu bằng **cách hệ thống đã hiểu câu hỏi**: môi trường nào, khoảng thời gian nào (giờ chính
xác), loại lỗi nào, từ nào của bạn được hiểu là gì. Mỗi ý hiện thành một **chip**; bấm vào chip để sửa (đổi môi
trường, đổi khoảng thời gian, bỏ một từ) mà không phải gõ lại câu hỏi.

Khi câu hỏi có thể hiểu nhiều cách (ví dụ "shop" có cả ở hai nhóm hệ thống), trang **hỏi lại** bạn với vài lựa
chọn. Chọn một, hoặc viết rõ hơn. Hệ thống không đoán bừa.

Khi đã tìm ra lỗi khớp với mô tả, **Its lines in Logs** mở đúng các dòng log của lỗi đó; lỗi khớp nhất có trace
thì **Follow its journey** mở đường đi của request.

**Dán một mã.** Có mã đơn hàng, mã giao dịch hay request id (ví dụ `ORD-1001`)? Dán vào ô **Follow a code**. Trang
hiện **hành trình** của mã đó: lúc nào, qua những service nào, bước nào lỗi và thông báo lỗi của bước đó.

**Từ của team.** Những từ team đã định nghĩa (ví dụ "thanh toán", "hoàn tiền") hiện thành chip; bấm một chip để
thu hẹp kết quả theo từ đó. Các tìm kiếm team hay dùng nằm trong menu **Open a preset**.

**Copy as Markdown.** Trên tab **By trace** và trên một dòng log đang mở, nút này chép trace thành bảng, hoặc dòng
log cùng các field của nó, thành văn bản Markdown để dán vào ticket Jira, tin nhắn hay tài liệu test. Kết quả trong
Claude (mục 3) với prompt `ticket_evidence` cũng là Markdown.

**Đã gửi gì đi.** Nếu team bật một nhà cung cấp AI (xem mục 5), nút **What was sent** cho thấy đúng những gì đã gửi
cho nó trong câu hỏi vừa rồi, sau khi đã che. Không bật thì danh sách này trống: không có gì rời máy bạn ngoài các
request chỉ đọc tới Graylog.

## 3. Hỏi trong Claude (hoặc chat client khác)

Sau khi `graylog-mcp start`, Claude Code đã được kết nối. Bạn có thể hỏi thẳng bằng lời, hoặc chọn một **prompt có
sẵn** (trong Claude Code, gõ `/` rồi tìm `graylog`):

| Prompt | Dùng khi | Bạn điền |
|---|---|---|
| `find_error` | Có một lỗi mô tả bằng lời | `description`: lỗi theo lời bạn, ví dụ "thanh toán bị lỗi sáng nay"; `environment` (không bắt buộc); `when` (không bắt buộc) |
| `trace_code` | Có một mã đơn, mã giao dịch, trace id | `code`, ví dụ `ORD-1001`; `when` mặc định 24h |
| `check_after_change` | Vừa release, deploy hoặc chạy xong một đợt test | `change`, ví dụ "release 2.4 của checkout"; `since` mặc định 30m |
| `ticket_evidence` | Cần bằng chứng log để mở bug | `problem`: lỗi hoặc mã; `when` mặc định 1h |
| `scan` | Muốn biết "có gì bất thường không?" | `target` (không bắt buộc), `range` mặc định 1h |

`environment` là tên môi trường như `prod`, `staging`, hoặc `nhóm/môi trường` như `payment/prod`. Bỏ trống thì
dùng môi trường mặc định. Thời gian viết bằng lời ("sáng nay", "từ 9h đến 11h") hoặc dạng ngắn (`15m`, `2h`,
`1d`).

Model được dặn: gọi `ask` với nguyên lời của bạn, nhắc lại một câu nó hiểu thế nào để bạn sửa nếu sai, hỏi lại khi
câu hỏi chưa rõ, và trả lời bằng ngôn ngữ bạn viết. Team có thể thêm prompt riêng (ví dụ "Kiểm tra sau release");
chúng hiện cùng danh sách.

**ChatGPT web.** Kênh này tắt theo mặc định. Nếu bạn (hoặc team) bật nó (README, mục *ChatGPT web*), ChatGPT sẽ
mở một trang hỏi **mã ghép đôi** khi bạn thêm connector. Lấy mã trên trang quản trị (**Connect**, thẻ **ChatGPT
web**, **New pairing code**) hoặc bằng lệnh `graylog-mcp pair`, rồi gõ mã vào trang đó. Mã chỉ dùng được một lần
trong 10 phút. ChatGPT chỉ thấy những môi trường đã được đánh dấu `remote_access`, và kết quả (đã che) sẽ đi tới
OpenAI, nên hãy hỏi người cấu hình trước khi bật cho một môi trường.

## 4. Đọc kết quả cho đúng

- **Số lượng là số thật** do Graylog đếm, không phải ước lượng.
- **Giá trị bị che** như `[EMAIL]`, `[CARD]`, `[PHONE]`, `[REDACTED]` là cố ý: thông tin nhạy cảm không bao giờ
  hiện ra, kể cả với model.
- **"Không tìm thấy"** đi kèm gợi ý (khoảng thời gian rộng hơn, môi trường khác, từ viết khác) và số kết quả của
  từng gợi ý. Thử gợi ý hợp với câu hỏi thay vì đoán lại.
- **Lỗi nghiệp vụ và lỗi kỹ thuật** được tách: "bị từ chối", "hết hạn mức" là lỗi nghiệp vụ; "timeout",
  "exception" là lỗi kỹ thuật. Câu hỏi nói "lỗi" chung thì tìm cả hai.
- Kết quả có **truy vấn đã chạy** (`ran`): gửi kèm cho dev để họ chạy lại đúng như vậy.

## 5. Quyền riêng tư và nhà cung cấp AI

Mặc định `ask` **không cần AI và không cần key**: câu hỏi được đọc bằng luật trên máy bạn. Team có thể thêm một
nhà cung cấp AI (màn hình **Providers** trên trang quản trị) để đọc những câu luật chưa hiểu. Khi đó:

- Chỉ phần luật chưa đọc được mới được hỏi AI.
- Log chỉ được gửi cho AI khi môi trường đó cho phép (`provider_data`), và luôn sau khi đã che giá trị nhạy cảm.
- Nút **Test** trên màn hình Providers gửi một câu hỏi mẫu cố định, không phải dữ liệu thật.

Hỏi người cấu hình của team nếu bạn không chắc môi trường nào được phép gửi gì.

## 6. Hỏi đáp nhanh

**Trang không mở được?** Chạy `graylog-mcp status`. Nếu nó không chạy, `graylog-mcp start`.

**Báo thiếu token hay "401"?** Chạy `graylog-mcp login` và nhập lại token của môi trường đó.

**Báo không kết nối được Graylog?** Thường do VPN chưa bật. Bật VPN rồi hỏi lại.

**Câu trả lời hiểu sai câu hỏi?** Sửa ở chip, hoặc viết rõ môi trường và thời gian: "trên prod, từ 9h đến 10h sáng
nay".

**Muốn một từ của team được hiểu đúng?** Nhờ người cấu hình thêm nó vào `[glossary]` trong cấu hình của team, rồi
xuất lại gói cấu hình cho mọi người.

**Muốn cập nhật?** `graylog-mcp update`, hoặc nút "Check for updates" trên trang.
