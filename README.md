# CargoPlus 货代邮件结构化抽取 API 服务平台 (V3)

云服务器单机一键部署见 [`docs/CLOUD_ONE_CLICK_DEPLOYMENT.md`](docs/CLOUD_ONE_CLICK_DEPLOYMENT.md)，上线安全评审见 [`docs/SECURITY_REVIEW.md`](docs/SECURITY_REVIEW.md)。

基于 `cargo-mail-extraction-skill-v3` 规范打造的工业级海运货代邮件与单证结构化抽取平台。系统采用 **AI 语义抽取管道 + 高性能异步微服务架构**，支持多模态单证解析、大模型服务热切换与 API 动态拉取、多租户按次计量扣费（默认 0.50 元/次）、异步高并发削峰（支撑日万级吞吐）、HMAC-SHA256 防篡改 Webhook 回调推送以及现代化 Web 管理控制台。

---

## 🌟 核心特性

1. **Skill V3 抽取规范全量继承**：
   - 提取 57 个顶层货代核心业务字段 + 13 个集装箱明细字段；
   - 执行二阶段规则清洗（`normalize_output.py`）：收发通主体与地址拆分、电话/邮箱/传真正则提取、件重体数值单位拆分、箱型代码标准归一、中英文品名拆分。
2. **大模型服务热切换与 API 动态模型拉取**：
   - **动态探活与模型发现 (`POST /admin/llm-config/models`)**：直接向上游大模型服务商标准 `GET {base_url}/models` 端点探测，自动拉取并解析可用模型列表（兼容 OpenAI、商汤 SenseAudio、DeepSeek 官方、硅基流动、阿里云百炼、智谱 GLM、本地 Ollama/vLLM 等）；
   - **运行时热更新与连通性自检 (`POST /admin/llm-config/test`)**：在管理后台随时修改 Base URL、API Key 与 Model，无需重启服务即可即时生效；
   - **全链路动态联动**：在线调试工作台与日志全量解耦，自动跟随当前生效的大模型动态渲染提示与状态。
3. **多模态与多格式文档自动解析与 RapidOCR**：
   - 既支持传入标准结构化文本 JSON，也支持直接上传原始邮件（`.eml`）与各类单证附件（`PDF` 提单、`Excel` 装箱单、`Word` 合同、图片扫描件 RapidOCR 识别）。
4. **精准的多租户计量、准入审核与原子扣费系统**：
   - 租户注册默认进入【待审核】状态（`is_active=False`），管理员审核开通并立赠 ¥50.00 试用金；
   - 支持管理员按租户动态调整调用单价（¥0.01~¥100.00）与并发上限（1~30）；
   - 租户独立获取并管理专属 API Key 与 Webhook Secret，支持 API Key 凭证免密登录；
   - 任务提交时通过数据库条件更新原子预留本次额度（不足返回 `402 Payment Required`），防止并发超额排队；
   - 仅在**任务成功提取并通过校验**后执行原子扣费（默认 0.50 元/次），失败或超时自动释放预留额度，严格**零扣费**；
   - 全页面财务对账单与流水支持标准分页与 1-Click CSV 电子账单导出。
5. **持久化异步削峰与双模自适应降级调度 (5 分钟 SLA)**：
   - PostgreSQL/SQLite (WAL 模式) 保存任务、租户、余额和不可变计费流水，Redis + Celery 提供可恢复的后台任务队列；
   - **自适应降级自愈**：在未挂载外部 Redis/Celery 队列环境或本地开发调试时，系统自动无缝降级为**进程内安全同步/异步处理**，彻底避免服务中断；
   - 抽取与 Webhook 使用独立队列，慢回调不会占用抽取 worker；
   - Redis 原子信号量实施租户级最大并发限制（1～30），Celery worker 实施全局并发限制（1～100）。
6. **安全可靠的 Webhook 推送**：
   - 任务处理完成后自动向客户系统的 `callback_url` 发送 POST 通知；
   - 携带 `X-Timestamp` 与 `X-Signature-SHA256` 签名（基于租户 Secret 进行 HMAC-SHA256 计算，防伪造、防篡改）；
   - 失败自动触发 3 次指数退避重试，具备完整公网 IP 校验白名单防御 SSRF。
7. **现代化 Web 管理控制台与交互**：
   - 全面剔除原生浏览器 `alert`/`confirm` 弹窗，采用现代化 Toast 气泡与自定义 Modal 交互；
   - 实时数据大盘、近 14 天吞吐与营收折线图；
   - 租户开户审核、API Key 管理、在线余额充值、自定义单价修改；
   - 邮件任务全流程追踪、耗时统计、V3 JSON 高亮查看与一键复制；
   - 在线调试工作台（支持直接粘贴文本或拖拽文件上传实时测试）。
8. **企业级质量保障与测试覆盖**：
   - 拥有 **198 项自动化单元/集成测试用例 (100% 绿灯通过)**，后端核心代码覆盖率达到 **94%~95%**。

---

## 🚀 Docker Desktop 一键启动（推荐）

项目的 `docker-compose.yml` 包含 PostgreSQL、Redis、API、抽取 worker、独立 Webhook worker 和 Celery Beat。首次启动前，请确保同级目录中存在 `cargo-mail-extraction-skill-v3`。

```powershell
cd D:\agent\agv\cargo\cargo_service
Copy-Item .env.example .env   # 已有 .env 时不要覆盖
# 编辑 .env，至少设置 LLM_API_KEY、ADMIN_SECRET_KEY、SESSION_SECRET_KEY

# 先启动基础设施
docker compose up -d postgres redis

# 如果要保留旧 SQLite 数据，仅首次执行；目标 PostgreSQL 必须为空
$env:POSTGRES_MIGRATION_URL = "postgresql+asyncpg://cargo:cargo-local-password@127.0.0.1:5432/cargo"
.\.venv\Scripts\python.exe scripts\migrate_sqlite_to_postgres.py `
  --source data\cargo_service.db `
  --report data\migration_rejected_billing.json

# 构建并启动应用服务
docker compose up -d --build api worker webhook-worker beat
docker compose ps
```

默认访问地址为 [http://localhost:8001](http://localhost:8001)，健康检查为 [http://localhost:8001/health/ready](http://localhost:8001/health/ready)。默认使用 8001 是为了避免与旧的本地 `run.py` 进程占用的 8000 冲突；可在 `.env` 中通过 `APP_PORT` 修改。

常用运维命令：

```powershell
docker compose logs -f api worker webhook-worker beat
docker compose exec worker celery -A app.celery_app:celery_app inspect ping
docker compose stop                         # 停止但保留数据库与队列数据
docker compose down                         # 删除容器但保留命名卷
```

不要在有用数据时运行 `docker compose down -v`，该命令会删除 PostgreSQL、Redis 和上传文件卷。迁移脚本会拒绝写入非空 PostgreSQL，并将不符合现行金额约束的旧流水写入报告而不是强行导入。

队列恢复机制、并发调优和正式上线仍需补齐的高可用能力见 [`docs/CELERY_OPERATIONS.md`](docs/CELERY_OPERATIONS.md)。

正式环境请使用 [`docker-compose.production.yml`](docker-compose.production.yml)，并先运行 `scripts/production_readiness.py`。生产栈包含 Caddy 自动 TLS、Docker secrets、Prometheus、Alertmanager、Grafana、数据库备份与隔离恢复演练；它要求外部托管 PostgreSQL 和 Redis/Sentinel，不会把同一台机器上的多个容器伪装成跨故障域高可用。

---

## 本地开发启动

### 1. 环境准备 (推荐使用 uv)

```bash
# 进入项目目录
cd D:/agent/agv/cargo/cargo_service

# 创建虚拟环境
uv venv .venv

# 安装项目依赖
uv pip install -r requirements-dev.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env` 并配置您的大模型 API 密钥：

```ini
# 大模型配置 (支持商汤 / DeepSeek / 硅基 / 百炼 / 智谱 / OpenAI / Ollama 等)
LLM_BASE_URL=https://api.senseaudio.cn/v1
LLM_API_KEY=your-llm-api-key-here
LLM_MODEL=deepseek-v4-flash-0731

# 本地测试可使用 SQLite；Docker/生产使用 PostgreSQL
DATABASE_URL=sqlite+aiosqlite:///./data/cargo_service.db
TASK_QUEUE_MODE=local

# 租户默认扣费单价
DEFAULT_UNIT_PRICE=0.50

# 管理员登录与会话签名（必须分别生成随机值，生产环境禁止使用示例值）
ADMIN_SECRET_KEY=replace-with-a-random-admin-secret
SESSION_SECRET_KEY=replace-with-a-different-random-session-secret

# 仅允许可信前端来源跨域访问
CORS_ALLOWED_ORIGINS=http://127.0.0.1:8000,http://localhost:8000
```

可使用 `python -c "import secrets; print(secrets.token_urlsafe(48))"` 分别生成两个密钥。管理员登录成功后，浏览器保存的是限时签名会话令牌，不是管理员主密钥；管理员 API 不再因本机访问或调试模式而自动放行。

### 3. 启动本地开发服务

```bash
.venv/Scripts/python.exe run.py
```

本地开发服务启动后：
- **Web 管理控制台**：访问 [http://localhost:8000](http://localhost:8000)
- **Swagger API 文档**：访问 [http://localhost:8000/docs](http://localhost:8000/docs)
- **Redoc 文档**：访问 [http://localhost:8000/redoc](http://localhost:8000/redoc)

---

## 📡 API 调用指南

### 1. 异步提交邮件抽取任务（JSON 文本模式 - 推荐）

**请求接口**：`POST /api/v1/extract/async`  
**请求头**：`Authorization: Bearer <YOUR_API_KEY>`

```bash
curl -X POST "http://localhost:8000/api/v1/extract/async" \
  -H "Authorization: Bearer cg_xxxx_your_secret_key" \
  -H "Content-Type: application/json" \
  -d '{
    "mail_subject": "Booking BK123456 - Yantian to Melbourne",
    "mail_body": "Please arrange booking. Freight prepaid.",
    "attachments": [
      {
        "filename": "booking.pdf",
        "content_type": "application/pdf",
        "text": "SHIPPER: ABC TRADING CO., LTD.\nADD: NO.1 ROAD, SHENZHEN\nTEL: +86 755 12345678\nCONSIGNEE: XYZ IMPORT PTY LTD\nPOL: YANTIAN\nPOD: MELBOURNE\nCONTAINER: ABCU1234567 / SEAL123 / 40HQ\nGOODS: DAILY NECESSITIES 日用品\nHS CODE: 3924900000\nPACKAGES: 501 PACKAGES\nG.W.: 9,170.000 KGS\nMEAS: 68.000 CBM",
        "tables": [],
        "ocr_text": ""
      }
    ],
    "callback_url": "https://your-domain.com/api/cargo/webhook"
  }'
```

**响应示例**：
```json
{
  "code": 0,
  "message": "Task submitted successfully",
  "task_id": "task_a1b2c3d4e5f6",
  "status": "PENDING",
  "created_at": "2026-08-18T10:00:00Z"
}
```

---

### 2. 异步提交任务（直接上传原始文件模式）

**请求接口**：`POST /api/v1/extract/async/upload`

```bash
curl -X POST "http://localhost:8000/api/v1/extract/async/upload" \
  -H "Authorization: Bearer cg_xxxx_your_secret_key" \
  -F "files=@booking_confirmation.pdf" \
  -F "files=@packing_list.xlsx" \
  -F "mail_subject=订舱确认单抽单" \
  -F "callback_url=https://your-domain.com/api/cargo/webhook"
```

---

### 3. 查询任务抽取结果

**请求接口**：`GET /api/v1/tasks/{task_id}`

```bash
curl -X GET "http://localhost:8000/api/v1/tasks/task_a1b2c3d4e5f6" \
  -H "Authorization: Bearer cg_xxxx_your_secret_key"
```

**响应示例**：
```json
{
  "id": "task_a1b2c3d4e5f6",
  "tenant_id": "tenant_xxx",
  "input_type": "JSON",
  "mail_subject": "Booking BK123456 - Yantian to Melbourne",
  "status": "SUCCESS",
  "charged_amount": 0.50,
  "is_charged": true,
  "duration_ms": 3420,
  "callback_status": "SUCCESS",
  "result_json": {
    "ShipperName": "ABC TRADING CO., LTD.",
    "ShipperAddr": "NO.1 ROAD, SHENZHEN\nTEL: +86 755 12345678",
    "ShipperTel": "+86 755 12345678",
    "ShipperEmail": "",
    "ShipperFax": "",
    "ConsigneeName": "XYZ IMPORT PTY LTD",
    "ConsigneeAddr": "",
    "ConsigneeTel": "",
    "ConsigneeEmail": "",
    "ConsigneeFax": "",
    "POLName": "YANTIAN",
    "PODName": "MELBOURNE",
    "ContainerInfo": [
      {
        "ContainerNo": "ABCU1234567",
        "SealNo": "SEAL123",
        "ContSize": "40",
        "ContType": "HQ",
        "KGS": "9170.000",
        "KGSunit": "KGS",
        "PCS": "501",
        "Package": "PACKAGES",
        "CBM": "68.000",
        "CBMunit": "CBM",
        "HSCode": "3924900000",
        "GoodsName": "DAILY NECESSITIES",
        "GoodsNameCN": "日用品"
      }
    ],
    "GoodsName": "DAILY NECESSITIES",
    "GoodsNameCN": "日用品",
    "HSCode": "3924900000",
    "Packages": "501",
    "PackagesUnit": "PACKAGES",
    "GrossWeight": "9170.000",
    "GrossWeightUnit": "KGS",
    "Volume": "68.000",
    "VolumeUnit": "CBM",
    "GoodsType": "S",
    "FreightTerm": "PREPAID",
    "BookingNo": "BK123456",
    "Remark": ""
  }
}
```

---

### 4. 同步即时抽取接口（轻量调试模式）

**请求接口**：`POST /api/v1/extract/sync`

```bash
curl -X POST "http://localhost:8000/api/v1/extract/sync" \
  -H "Authorization: Bearer cg_xxxx_your_secret_key" \
  -H "Content-Type: application/json" \
  -d '{
    "mail_subject": "Booking Confirmation",
    "mail_body": "Freight Prepaid",
    "attachments": [
      {
        "filename": "booking.txt",
        "text": "POL: YANTIAN\nPOD: MELBOURNE\nCONTAINER: ABCU1234567 / 40HQ"
      }
    ]
  }'
```

---

## 🔒 Webhook 回调与 HMAC-SHA256 签名校验

当任务处理完成后，系统会向您配置的 `callback_url` 发送 POST 请求：

### 请求头：
- `Content-Type: application/json`
- `X-Timestamp: 1723971234567` (毫秒时间戳)
- `X-Signature-SHA256: <生成的哈希值>`

### 签名校验算法：
```text
Signature = HMAC_SHA256(tenant_secret, timestamp + "." + request_body_json)
```

### Python 校验代码示例：
```python
import hmac
import hashlib

def verify_webhook_signature(secret: str, timestamp: str, raw_body_str: str, signature_header: str) -> bool:
    message = f"{timestamp}.{raw_body_str}".encode("utf-8")
    expected_sig = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_sig, signature_header)
```

---

## 🧪 运行自动化测试套件与覆盖率

```bash
# 运行完整 198 项自动化测试并生成覆盖率报告 (>= 94%~95%)
.venv/Scripts/python.exe -m pytest --cov=app --cov-report=term-missing tests/
```

---

## 📚 完整项目文档导航

- 📘 [01. 产品需求文档 (PRD)](docs/01_PRD_Product_Requirements_Document.md)
- 🏗️ [02. 系统架构设计说明书](docs/02_System_Architecture_Design.md)
- 🛠️ [03. 开发者与部署运维手册](docs/03_Developer_and_Deployment_Guide.md)
- 📖 [04. 接口参考手册与集成指南](docs/04_API_Reference_Manual.md)
- 📊 [05. 测试分析与高并发性能压测报告](docs/05_Testing_and_Benchmark_Report.md)
- 🖥️ [06. 用户与管理员操作手册](docs/06_User_and_Admin_Manual.md)
- ⚡ [Celery 队列运维与并发指南](docs/CELERY_OPERATIONS.md)
