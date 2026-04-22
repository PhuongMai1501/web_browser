Với thay đổi mới của anh, test kịch bản kiểu:

vào thuvienphapluat.vn → nhập nghị định → bấm tìm kiếm

thì cách làm sẽ là:

tạo một scenario flow
dry-run validate
gọi /v1/sessions để chạy thật
xem step stream/polling
nếu fail thì chỉnh target trong YAML/JSON

Vì Sprint 1 của anh đã có đủ các mảnh cần thiết rồi: mode=flow, steps, inputs, success/failure, 6 action chuẩn, validator action/input reference, và flow runner mới.

1. Kịch bản test nên viết thế nào

Anh có thể tạo một spec mới, ví dụ search_thuvienphapluat.

Bản YAML dễ hiểu
id: search_thuvienphapluat
display_name: "Tìm kiếm trên Thư Viện Pháp Luật"
description: "Mở trang thuvienphapluat.vn và tìm kiếm từ khóa"
mode: flow
start_url: https://thuvienphapluat.vn
allowed_domains:
  - thuvienphapluat.vn

inputs:
  - name: keyword
    type: string
    required: true
    source: context

steps:
  - action: wait_for
    target:
      placeholder_any: ["Tìm kiếm", "Nhập từ khóa", "Search"]
    timeout_ms: 10000

  - action: fill
    target:
      placeholder_any: ["Tìm kiếm", "Nhập từ khóa", "Search"]
    value_from: keyword

  - action: click
    target:
      text_any: ["Tìm kiếm", "Search"]

success:
  any_of:
    - url_contains: tim-kiem
    - text_any: ["Kết quả tìm kiếm", "nghị định"]

failure:
  any_of:
    - text_any: ["Không tìm thấy", "Có lỗi xảy ra"]
  code: SEARCH_FAILED
  message: "Tìm kiếm thất bại"

Spec này đúng với schema Sprint 1 của anh:

mode: flow
inputs
steps
success
failure
2. Nếu muốn tạo qua API thì gửi JSON gì

Vì admin API giữ nguyên từ v1, anh có thể POST /v1/scenarios để tạo scenario flow mới.

Ví dụ file search_thuvienphapluat.json:

{
  "id": "search_thuvienphapluat",
  "display_name": "Tìm kiếm trên Thư Viện Pháp Luật",
  "description": "Mở trang thuvienphapluat.vn và tìm kiếm từ khóa",
  "mode": "flow",
  "start_url": "https://thuvienphapluat.vn",
  "allowed_domains": ["thuvienphapluat.vn"],
  "inputs": [
    {
      "name": "keyword",
      "type": "string",
      "required": true,
      "source": "context"
    }
  ],
  "steps": [
    {
      "action": "wait_for",
      "target": {
        "placeholder_any": ["Tìm kiếm", "Nhập từ khóa", "Search"]
      },
      "timeout_ms": 10000
    },
    {
      "action": "fill",
      "target": {
        "placeholder_any": ["Tìm kiếm", "Nhập từ khóa", "Search"]
      },
      "value_from": "keyword"
    },
    {
      "action": "click",
      "target": {
        "text_any": ["Tìm kiếm", "Search"]
      }
    }
  ],
  "success": {
    "any_of": [
      { "url_contains": "tim-kiem" },
      { "text_any": ["Kết quả tìm kiếm", "nghị định"] }
    ]
  },
  "failure": {
    "any_of": [
      { "text_any": ["Không tìm thấy", "Có lỗi xảy ra"] }
    ],
    "code": "SEARCH_FAILED",
    "message": "Tìm kiếm thất bại"
  }
}
3. Test validate trước khi chạy thật

Nên làm bước này trước, vì validator Sprint 1 của anh sẽ chặn sớm các lỗi như:

mode=flow nhưng steps rỗng
action name sai
value_from trỏ tới input không tồn tại
goto thiếu url
if_visible thiếu branch/target.
Dry-run validate
curl -X POST \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d @search_thuvienphapluat.json \
  http://SERVER:9000/v1/scenarios/search_thuvienphapluat/dry-run

Nếu pass thì mới tạo thật.

4. Tạo scenario thật
curl -X POST \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d @search_thuvienphapluat.json \
  http://SERVER:9000/v1/scenarios

Sau đó list lại:

curl -H "X-Admin-Token: $ADMIN_TOKEN" \
  http://SERVER:9000/v1/scenarios
5. Chạy session test thật

Sau khi đã có scenario rồi, user runtime chỉ cần gửi:

{
  "scenario": "search_thuvienphapluat",
  "context": {
    "keyword": "nghị định"
  }
}

Ví dụ curl:

curl -X POST \
  -H "Content-Type: application/json" \
  -d '{
    "scenario": "search_thuvienphapluat",
    "context": {
      "keyword": "nghị định"
    }
  }' \
  http://SERVER:9000/v1/sessions

Sprint 1 của anh giữ nguyên sessions.py, job_handler.py, scenarios.py, app.py; việc route sang v2 hoàn toàn nằm trong generic_runner.py, nên cách gọi session không đổi.