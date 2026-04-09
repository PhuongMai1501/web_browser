# Chang AI Browser — Hướng Dẫn Deploy Thủ Công Lên Server

## Hiểu kiến trúc trước khi bắt đầu

```
[Local PC]                          [GPU Server]
web_UI_test (Vite dev)  ─HTTP/SSE─▶  FastAPI :8000
                                         │
                                       Redis :6379   ← hàng đợi job + trạng thái session
                                         │
                                    Browser Worker × N
                                         │  subprocess
                                    agent-browser CLI
                                         │
                                       Chrome (headless)
```

**Có 3 process phải chạy trên server:**

1. **Redis** — lưu job queue, session state, event buffer giữa API và Worker
2. **FastAPI API** (`uvicorn api.app:app`) — nhận request từ UI, đẩy job vào Redis queue, stream SSE về UI
3. **Browser Worker** (`python -m worker.browser_worker`) — lấy job từ Redis, mở Chrome, chạy LLM agent

**Vì sao tách API và Worker?**
API không chạy Chrome — nó chỉ nhận HTTP và stream SSE. Worker mới là process nặng, mỗi worker giữ 1 Chrome riêng. Muốn hỗ trợ 10 user đồng thời → cần 10 worker process chạy song song.

---

## Phần 1 — Chuẩn bị server

### Bước 1.1: Cài Python 3.11

API và Worker đều viết bằng Python 3.11. Kiểm tra xem server đã có chưa:

```bash
python3 --version
# Nếu ra 3.11.x thì OK. Nếu thấp hơn thì cài:

sudo apt update
sudo apt install -y software-properties-common
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-pip

# Kiểm tra lại
python3.11 --version
```

### Bước 1.2: Cài Node.js 18+

Cần để cài `agent-browser` CLI qua npm:

```bash
node --version
# Nếu chưa có hoặc < 18:

curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt install -y nodejs

node --version   # phải ra v18.x.x trở lên
npm --version
```

### Bước 1.3: Cài Redis

Redis đóng vai trò "trung gian" giữa API và Worker. API push job vào Redis list `pending_jobs`, Worker dùng `BLPOP` để block-wait lấy job ra chạy.

```bash
sudo apt install -y redis-server

# Bật Redis tự khởi động khi server reboot
sudo systemctl enable redis-server
sudo systemctl start redis-server

# Kiểm tra Redis đang chạy
redis-cli ping
# Phải ra: PONG
```

Nếu cần xem Redis đang làm gì (debug):
```bash
redis-cli monitor
# Ctrl+C để thoát
```

### Bước 1.4: Cài Chrome

`agent-browser` CLI điều khiển Chrome qua Chrome DevTools Protocol. Server cần có Chrome hoặc Chromium.

```bash
# Cài Google Chrome stable (khuyên dùng, ổn định hơn)
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb

# Nếu gặp lỗi deps thì thêm bước này
sudo apt --fix-broken install

# Kiểm tra
google-chrome --version
# Google Chrome 120.x.x.x

# Nếu không cài được Chrome, dùng Chromium thay thế
sudo apt install -y chromium-browser
chromium-browser --version
```

> **Lưu ý GPU server**: server headless không có màn hình, Chrome sẽ chạy ở chế độ headless. `agent-browser` tự xử lý flag `--headless` khi phát hiện môi trường không có display.

### Bước 1.5: Cài agent-browser CLI

`agent-browser` là binary Rust, phân phối qua npm. Khi `npm install -g`, script `postinstall.js` tự download binary Linux x64 về:

```bash
npm install -g agent-browser

# Kiểm tra
agent-browser --version
# agent-browser x.x.x
```

Nếu server **không có internet** để npm download binary, xem phần cuối (Offline Install).

---

## Phần 2 — Upload code

### Bước 2.1: Tạo file zip trên local PC

Mở Git Bash hoặc PowerShell trên Windows:

```bash
cd D:\research\ChangAI\web_brower
tar -czf deploy_server.tar.gz deploy_server/
```

File `deploy_server.tar.gz` (~64KB) chứa toàn bộ source code cần thiết.

### Bước 2.2: Upload lên server

```bash
# Thay USER và SERVER_IP cho phù hợp
scp deploy_server.tar.gz USER@SERVER_IP:/opt/

# Ví dụ:
scp deploy_server.tar.gz ubuntu@192.168.1.100:/opt/
```

### Bước 2.3: Giải nén trên server

```bash
# SSH vào server
ssh USER@SERVER_IP

# Giải nén và đặt tên thư mục làm việc
cd /opt
tar -xzf deploy_server.tar.gz
mv deploy_server chang-ai

# Kiểm tra cấu trúc
ls /opt/chang-ai/
# phải thấy: ai_tool_web/  LLM_base/  requirements.txt  .env.example  ...
```

---

## Phần 3 — Cài Python packages

### Bước 3.1: Tạo virtualenv

Không cài thẳng vào system Python, dùng virtualenv để cô lập:

```bash
cd /opt/chang-ai

# Tạo venv
python3.11 -m venv venv

# Kích hoạt venv (cần làm lại mỗi khi mở terminal mới)
source venv/bin/activate

# Dấu nhắc sẽ thêm (venv) ở đầu:
# (venv) user@server:/opt/chang-ai$
```

### Bước 3.2: Cài packages

```bash
# Đảm bảo venv đang active (thấy "(venv)" ở đầu dòng)
pip install --upgrade pip
pip install -r requirements.txt

# Kiểm tra các package quan trọng
python -c "import fastapi; print('fastapi OK')"
python -c "import redis; print('redis OK')"
python -c "import openai; print('openai OK')"
python -c "import uvicorn; print('uvicorn OK')"
```

---

## Phần 4 — Cấu hình môi trường

### Bước 4.1: Tạo file .env

```bash
cd /opt/chang-ai
cp .env.example .env
nano .env
```

Điền vào các giá trị thực:

```env
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx

REDIS_URL=redis://localhost:6379/0

ARTIFACTS_ROOT=/opt/chang-ai/LLM_base/artifacts

LOG_DIR=/opt/chang-ai/logs
```

- `OPENAI_API_KEY`: key thật để gọi GPT-4o
- `REDIS_URL`: để mặc định `localhost:6379` vì Redis chạy cùng server
- `ARTIFACTS_ROOT`: nơi lưu screenshots và logs của mỗi run
- `LOG_DIR`: nơi lưu api.log và worker.log

### Bước 4.2: Tạo thư mục cần thiết

```bash
mkdir -p /opt/chang-ai/LLM_base/artifacts
mkdir -p /opt/chang-ai/logs/system

# Đảm bảo process có quyền ghi
chmod -R 755 /opt/chang-ai
```

---

## Phần 5 — Chạy các services

Cần 3 loại process chạy song song. Khuyên dùng `tmux` để quản lý dễ hơn:

```bash
sudo apt install -y tmux
tmux new-session -s chang   # tạo session tmux tên "chang"
# Ctrl+B, C  → tạo tab mới trong tmux
# Ctrl+B, số → chuyển tab
# Ctrl+B, D  → detach (thoát tmux nhưng process vẫn chạy)
# tmux attach -t chang → quay lại tmux đang chạy
```

### Service 1: Kiểm tra Redis

Redis đã cài và bật qua systemd ở trên. Xác nhận:

```bash
redis-cli ping
# Phải ra: PONG

# Xem Redis đang lắng nghe port nào
redis-cli info server | grep tcp_port
# tcp_port:6379
```

### Service 2: FastAPI API

Đây là HTTP server nhận request từ UI. **Entry point**: `ai_tool_web/api/app.py`.

Tại sao `cd` vào `ai_tool_web/` trước khi chạy? Vì Python tìm module theo thư mục hiện tại. Khi đứng ở `ai_tool_web/`, các import như `from config import ...`, `from store import ...`, `from api.routes import ...` đều resolve đúng.

```bash
# Terminal / tmux tab 1
cd /opt/chang-ai/ai_tool_web
source ../venv/bin/activate

# Load biến môi trường từ .env
export $(grep -v '^#' /opt/chang-ai/.env | xargs)

# Chạy API server
uvicorn api.app:app --host 0.0.0.0 --port 8000 --workers 1 --log-level info
```

Kiểm tra API đang chạy (từ terminal khác hoặc local PC):

```bash
curl http://SERVER_IP:8000/v1/health
# {"status":"ok","active_session":null}   ← OK
```

### Service 3: Browser Workers

Mỗi worker là 1 process Python riêng, giữ 1 Chrome riêng. **Entry point**: `ai_tool_web/worker/browser_worker.py`.

Tại sao chạy từ `ai_tool_web/`? Cùng lý do — Python path. File `browser_worker.py` còn tự append `../LLM_base` vào `sys.path` để import `browser_adapter`, `runner`, `llm_planner`.

```bash
# Terminal / tmux tab 2 (worker-1)
cd /opt/chang-ai/ai_tool_web
source ../venv/bin/activate
export $(grep -v '^#' /opt/chang-ai/.env | xargs)

python -m worker.browser_worker --id worker-1
# Log sẽ ra: [worker-1] Worker started, waiting for jobs
```

Mở tab tmux mới và lặp cho 10 worker:

```bash
# Tab 3 (worker-2)
cd /opt/chang-ai/ai_tool_web
source ../venv/bin/activate
export $(grep -v '^#' /opt/chang-ai/.env | xargs)
python -m worker.browser_worker --id worker-2

# Tab 4 (worker-3) ... tiếp tục đến worker-10
```

> Mỗi worker khi khởi động sẽ:
> 1. Đăng ký tên mình vào Redis (`worker_registry`)
> 2. Loop vô hạn gọi `BLPOP pending_jobs` — đây là lệnh block, ngủ cho đến khi có job
> 3. Khi có job: lock session, mở Chrome, chạy LLM agent, push event lên Redis Pub/Sub
> 4. API nhận event từ Pub/Sub và forward ra SSE về UI

---

## Phần 6 — Cấu hình UI trên Local PC

UI dùng Vite proxy để forward `/v1/*` → API. Hiện tại đang trỏ `localhost:8000`, cần đổi thành địa chỉ server.

Mở file `web_UI_test/vite.config.js` trên local:

```js
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/v1': {
        target: 'http://192.168.1.100:8000',  // ← đổi thành IP server thực
        changeOrigin: true,
      },
    },
  },
})
```

Sau đó chạy UI:

```bash
cd D:\research\ChangAI\web_brower\web_UI_test
npm install     # lần đầu
npm run dev
# Mở trình duyệt: http://localhost:5173
```

> Nếu UI gọi không được, kiểm tra firewall server:
> ```bash
> sudo ufw allow 8000/tcp
> sudo ufw status
> ```

---

## Phần 7 — Kiểm tra toàn bộ luồng

### Smoke test từ server

```bash
# Gọi thử tạo 1 session
curl -X POST http://localhost:8000/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{
    "scenario": "chang_login",
    "context": {"email": "test@fpt.net", "password": "testpass"},
    "max_steps": 5
  }'

# Phải trả về:
# {"session_id":"abc-123","status":"queued","stream_url":"/v1/sessions/abc-123/stream","queue_position":1}
```

Khi tạo session thành công, worker đang chạy sẽ log ra:
```
[worker-1] Picked up session abc-123
```

### Xem logs real-time

```bash
# API log
tail -f /opt/chang-ai/logs/system/api.log

# Worker log
tail -f /opt/chang-ai/logs/system/worker.log

# Xem session state trong Redis
redis-cli hgetall session:abc-123
```

---

## Phần 8 — Offline Install agent-browser

Nếu server không có internet để npm download binary, build thủ công trên local PC rồi copy lên.

**Trên local PC** (cần Docker):

```bash
cd D:\research\ChangAI\web_brower\agent-browser
npm run build:linux
# Script này chạy Docker để build Rust binary cho Linux x64
# Output file: bin/agent-browser-linux-x64
```

**Copy binary lên server và đặt vào PATH:**

```bash
# Từ local
scp agent-browser/bin/agent-browser-linux-x64 USER@SERVER_IP:/usr/local/bin/agent-browser

# Trên server
chmod +x /usr/local/bin/agent-browser
agent-browser --version   # kiểm tra
```

---

## Phần 9 — Checklist cuối trước khi test

```
[ ] redis-cli ping → PONG
[ ] curl http://SERVER_IP:8000/v1/health → {"status":"ok"}
[ ] agent-browser --version → ra version number
[ ] .env có OPENAI_API_KEY đúng
[ ] Đang có đủ worker process chạy (kiểm tra: redis-cli hgetall worker:worker-1)
[ ] vite.config.js đã trỏ đúng SERVER_IP:8000
[ ] Port 8000 mở trên firewall
```

---

## Troubleshooting

| Lỗi | Nguyên nhân | Fix |
|-----|-------------|-----|
| `ModuleNotFoundError: No module named 'fastapi'` | Chưa activate venv | `source /opt/chang-ai/venv/bin/activate` |
| `redis.exceptions.ConnectionError` | Redis chưa chạy | `sudo systemctl start redis-server` |
| `OPENAI_API_KEY not set` | Chưa export env vars | Chạy lại `export $(grep -v '^#' /opt/chang-ai/.env \| xargs)` |
| `agent-browser: command not found` | Chưa cài hoặc không trong PATH | `npm install -g agent-browser` hoặc xem Offline Install |
| `chrome not found` | Chrome chưa cài | Xem bước 1.4 |
| SSE stream không có data | Không có worker nào đang chạy | Khởi động ít nhất 1 worker |
| API trả 503 Queue full | Queue > 100 session | Tạm thời: tăng `_HARD_CAP` trong `ai_tool_web/store/job_queue.py` |
| Worker log thấy "already locked — skipping" | Race condition lành tính | Bình thường, worker khác sẽ nhận job |
