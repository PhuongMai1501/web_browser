# MySQL Setup Guide — Phase 1.5

> **Audience:** Dev/admin set up DB để test Phase 1 user-scenario với MySQL (thay vì SQLite local).
> **Target:** MySQL 5.7+ hoặc 8.0+.

---

## 1. Yêu cầu

- MySQL server đang chạy (local hoặc remote)
- Client truy cập: MySQL Workbench / `mysql` CLI
- Quyền tạo database + user (DBA hoặc tự-host)

---

## 2. Setup DB + User

Chạy các lệnh sau trong Workbench (ssh vào server MySQL rồi mysql CLI cũng được):

### 2.1 Tạo database

```sql
-- Khuyến nghị: tên rõ scope
CREATE DATABASE chang_scenarios
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;
```

### 2.2 Tạo user riêng cho app

```sql
-- Thay <STRONG_PASSWORD> bằng mật khẩu thật
CREATE USER 'chang_app'@'%' IDENTIFIED BY '<STRONG_PASSWORD>';

-- Grant full quyền trên DB vừa tạo
GRANT ALL PRIVILEGES ON chang_scenarios.* TO 'chang_app'@'%';
FLUSH PRIVILEGES;
```

**Lưu ý:**
- `@'%'` = cho kết nối từ mọi host. Nếu chỉ từ 1 IP: `@'192.168.x.x'`
- Với MySQL 8: user tạo với plugin `caching_sha2_password` mặc định — một số Python driver cũ không support. Nếu gặp lỗi auth, dùng:
  ```sql
  ALTER USER 'chang_app'@'%' IDENTIFIED WITH mysql_native_password BY '<STRONG_PASSWORD>';
  ```

### 2.3 Verify connect

Trong Workbench:
- Host: `<MYSQL_HOST>`
- Port: `3306` (hoặc port thật)
- User: `chang_app`
- Pass: `<STRONG_PASSWORD>`
- Default schema: `chang_scenarios`

Test connection OK → proceed.

---

## 3. Apply schema (3 tables)

### 3.1 Qua Workbench

- Mở `chang_scenarios` database (double-click trong Navigator)
- File → Open SQL Script → chọn `001_init_mysql.sql`
- Execute (Ctrl+Shift+Enter hoặc ⚡ icon)

### 3.2 Qua CLI

```bash
mysql -h <MYSQL_HOST> -u chang_app -p chang_scenarios < \
  dev/deploy_server/ai_tool_web/store/migrations/001_init_mysql.sql
```

### 3.3 Verify 3 tables

```sql
USE chang_scenarios;

SHOW TABLES;
-- Expected:
-- scenario_definitions
-- scenario_revisions
-- scenario_runs

-- Check columns + types
DESCRIBE scenario_definitions;
DESCRIBE scenario_revisions;
DESCRIBE scenario_runs;

-- Verify không có data (chưa seed)
SELECT COUNT(*) FROM scenario_definitions;  -- → 0
```

---

## 4. Connection string

Backend sẽ đọc `DATABASE_URL` env. Format:

```
DATABASE_URL=mysql://<user>:<pass>@<host>:<port>/<db_name>?charset=utf8mb4
```

Ví dụ:
```bash
DATABASE_URL=mysql://chang_app:my_password_123@192.168.1.10:3306/chang_scenarios?charset=utf8mb4
```

**Encoding password special chars:** Nếu password có `@`, `/`, `#`, `?` → URL-encode:
- `@` → `%40`
- `/` → `%2F`
- `#` → `%23`

---

## 5. Xoá schema (nếu cần reset)

```sql
USE chang_scenarios;

-- Thứ tự: drop FK children trước (runs → revisions → definitions)
DROP TABLE IF EXISTS scenario_runs;
DROP TABLE IF EXISTS scenario_revisions;
DROP TABLE IF EXISTS scenario_definitions;
```

Hoặc drop hẳn database rồi tạo lại (cần quyền admin):
```sql
DROP DATABASE chang_scenarios;
CREATE DATABASE chang_scenarios CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
-- Re-run 2.2 GRANT + 3.x schema
```

---

## 6. Info cần gửi mình sau khi setup xong

Để mình viết tiếp `MysqlScenarioRepo` + test:

1. **Connection string** (không cần share password, chỉ cần cho biết format):
   - Host: `<MYSQL_HOST>`
   - Port: `3306`
   - User: `chang_app`
   - Database: `chang_scenarios`
2. **MySQL version:** `SELECT VERSION();`
3. **Confirm 3 tables tạo OK:** paste output `SHOW TABLES;`

Sau đó mình:
- Viết `MysqlScenarioRepo` (aiomysql)
- Viết factory trong `app.py` startup: `DATABASE_URL` → chọn Sqlite/Mysql
- Test dev với MySQL bạn vừa setup
- Sau khi xong sync sang product_build

---

## 7. Useful queries (for debug)

```sql
-- List all scenarios
SELECT id, name, owner_id, source_type, is_archived,
       published_revision_id, created_at
FROM scenario_definitions
ORDER BY updated_at DESC;

-- Revision chain của 1 scenario
SELECT id, version_no, static_validation_status,
       parent_revision_id, clone_source_revision_id,
       created_by, created_at
FROM scenario_revisions
WHERE scenario_id = 'chang_login'
ORDER BY version_no DESC;

-- Publish qua SQL (admin only)
UPDATE scenario_definitions
SET published_revision_id = 42, updated_at = NOW(6)
WHERE id = 'user_hiepqn_my_scenario';

-- Quota check per user
SELECT owner_id, COUNT(*) AS scenarios_count
FROM scenario_definitions
WHERE is_archived = 0 AND owner_id IS NOT NULL
GROUP BY owner_id
ORDER BY scenarios_count DESC;

-- Recent runs
SELECT r.id, r.scenario_id, r.mode, r.status,
       r.started_by, r.created_at
FROM scenario_runs r
ORDER BY r.id DESC
LIMIT 20;
```
