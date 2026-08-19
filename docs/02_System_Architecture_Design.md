# CargoPlus 系统架构设计说明书 (System Architecture Design)

---

## 1. 系统总体技术架构

CargoPlus 采用现代微服务架构，基于 Python 异步生态（FastAPI + SQLAlchemy 2.0 Async + Celery/Redis）构建，实现**多模态单证解析、大模型智能抽取、规则化归一化、分布式削峰队列与原子商业化计费**的一体化闭环。

```mermaid
graph TB
    subgraph 接入层 [接入层 API Gateway]
        Client[货代客户系统 / ERP / 业务客户端]
        AdminUI[管理员控制台 SPA]
        TenantUI[租户对账中心 SPA]
        Nginx[Caddy / Nginx 反向代理与 SSL 终止]
    end

    subgraph 应用服务层 [应用服务层 FastAPI Cluster]
        API[FastAPI 异步微服务 Core]
        AuthModule[认证鉴权模块<br/>API Key / JWT Session / Admin Secret]
        BillingModule[财务计费模块<br/>原子预留 / 成功扣费 / 充值记账]
        ParserModule[多模态解析引擎<br/>.eml / PDF / Excel / Word / RapidOCR]
        ValidatorModule[V3 模式验证与清洗<br/>normalize_output.py]
    end

    subgraph 队列与调度层 [削峰队列与任务调度]
        RedisQueue[(Redis Broker & Semaphore Cache)]
        CeleryWorker[Celery 抽取 Worker 池<br/>Bounded Concurrency: 1~100]
        WebhookWorker[独立 Webhook Dispatcher Worker<br/>HMAC-SHA256 & 指数退避重试]
        CeleryBeat[Celery Beat 定时巡检<br/>过期任务租约自愈 & 90天附件清理]
    end

    subgraph 外部智能服务 [外部 AI 服务]
        LLM[商汤科技开放平台 / DeepSeek 大模型<br/>https://api.senseaudio.cn/v1]
    end

    subgraph 数据存储层 [持久化数据层]
        DB[(PostgreSQL 16 / SQLite WAL 主数据库)]
        FileSystem[(本地/对象存储 uploads/<br/>90 天滚动清理)]
    end

    subgraph 监控可观测层 [监控可观测体系]
        Prometheus[Prometheus Metrics Exporter]
        Grafana[Grafana 仪表大盘]
    end

    Client --> Nginx
    AdminUI --> Nginx
    TenantUI --> Nginx
    Nginx --> API

    API --> AuthModule
    API --> BillingModule
    API --> ParserModule
    API --> RedisQueue
    API --> DB

    RedisQueue --> CeleryWorker
    RedisQueue --> WebhookWorker
    CeleryWorker --> ParserModule
    CeleryWorker --> LLM
    CeleryWorker --> ValidatorModule
    CeleryWorker --> BillingModule
    CeleryWorker --> DB
    CeleryWorker --> RedisQueue
    WebhookWorker --> Client

    CeleryBeat --> DB
    CeleryBeat --> FileSystem

    API --> Prometheus
    Prometheus --> Grafana
```

---

## 2. 核心模块架构设计

### 2.1 多模态单证解析与 OCR 引擎 (`app/core/parser/`)
- **调度分发中心 (`__init__.py`)**：根据文件 MIME/扩展名自动路由至专用解析器；
- **邮件解析 (`eml_parser.py`)**：利用标准 Python `email` 模块解析 RFC822 结构，递归提取内嵌正文与附件，HTML 正文利用自定义算法转为干净的 Markdown 纯文本；
- **表格解析 (`excel_parser.py`)**：利用 `openpyxl` 提取多 Sheet 表格，空行过滤并渲染为标准 Markdown 表格，单 Sheet 前 100 行限额保护；
- **文档解析 (`pdf_parser.py`, `word_parser.py`)**：利用 `pypdf` 与 `python-docx` 提取文档层文本与表格；
- **本地 OCR 引擎 (`ocr_engine.py`)**：基于 `rapidocr_onnxruntime` 构建本地高性能轻量 OCR 引擎，对各类海运单证扫描图片进行高精度文字识别。

### 2.2 规则归一化流水线 (`app/core/normalizer.py`)
- 严格遵循 `cargo-mail-extraction-skill-v3` 规范：
  1. **收发通主体分离**：`ShipperAddr` 自动剥离首行抬头，提取 `TEL:`、`FAX:`、`EMAIL:`；
  2. **件重体分离**：`Packages`、`GrossWeight`、`Volume` 数值与包装单位精确拆分；
  3. **品名中英文分离**：根据 CJK 字符区间智能拆分中英文品名；
  4. **箱型代码归一**：依据标准海运箱型代码对照表（`20GP`、`40HQ`、`45HQ`、`NOR`、`RF` 等）进行规范化转换。

### 2.3 原子计量扣费与预留锁机制 (`app/services/billing_service.py`)
为了杜绝高并发下超额欠费排队与重复扣费，系统采用 **二阶段原子扣费机制**：
```mermaid
sequenceDiagram
    autonumber
    participant Client as 客户端
    participant API as API 服务
    participant DB as 数据库 (PostgreSQL/SQLite)
    participant Worker as Celery Worker
    participant LLM as 商汤大模型

    Client->>API: POST /extract/async (提交任务)
    API->>DB: UPDATE Tenant SET reserved = reserved + unit_price WHERE balance - reserved >= unit_price
    alt 余额不足
        DB-->>API: 0 row updated
        API-->>Client: HTTP 402 Payment Required (请充值)
    else 预留成功
        DB-->>API: 1 row updated
        API->>DB: INSERT INTO tasks (status='PENDING', is_reserved=TRUE)
        API->>Worker: Enqueue Task ID
        API-->>Client: HTTP 200 (返回 Task ID)
    end

    Worker->>DB: 获取任务并标记 status='PROCESSING' (租约锁定)
    Worker->>LLM: 调用大模型抽取
    Worker->>Worker: 规则归一化清洗与 V3 校验

    alt 抽取成功
        Worker->>DB: 原子事务: balance = balance - unit_price, reserved = reserved - unit_price, status='SUCCESS'
        Worker->>Client: Webhook 推送抽取结果 (HMAC 签名)
    else 抽取失败 / 超时
        Worker->>DB: 原子事务: reserved = reserved - unit_price, status='FAILED' (释放预留, 扣费 0 元)
        Worker->>Client: Webhook 推送失败通知
    end
```

### 2.4 分布式削峰队列与自愈恢复 (`app/services/queue_service.py` & `app/celery_tasks.py`)
- **租户并发隔离**：基于 Redis Lua 脚本原子信号量，限制单租户活跃执行任务数不超过配置上限（1~30），超出返回 `HTTP 429`（附带 `Retry-After: 3`）；
- **任务租约与超时自愈**：每个执行中任务获得 60s 数据库 Lease 租约，Celery Beat / 本地恢复巡检定时扫描超期租约任务并重新入队，确保断电重启零丢单；
- **独立 Webhook 消费**：Webhook 回调任务投递至独立队列，慢速客户服务器不影响主抽取流水线吞吐。

---

## 3. 数据模型设计 (Database Schemas)

```mermaid
erDiagram
    TENANTS ||--o{ API_KEYS : "has many"
    TENANTS ||--o{ EMAIL_TASKS : "owns"
    TENANTS ||--o{ BILLING_TRANSACTIONS : "records"
    EMAIL_TASKS ||--o| BILLING_TRANSACTIONS : "charged for"

    TENANTS {
        string id PK "租户ID (UUID)"
        string name "企业名称"
        string contact_email "联系邮箱 (唯一索引)"
        string contact_phone "联系电话"
        string password_hash "加盐哈希密码"
        decimal balance "账户余额 (>= 0)"
        decimal reserved_balance "预留金额 (<= balance)"
        decimal unit_price "单次调用单价 (0.01~100.00)"
        int max_concurrency "最大并发数 (1~30)"
        boolean is_active "审核激活状态 (默认 False)"
        datetime created_at "创建时间"
    }

    API_KEYS {
        string id PK "密钥ID (UUID)"
        string tenant_id FK "所属租户ID"
        string name "密钥别名"
        string key_prefix "密钥前缀 (cg_xxxxxxxx)"
        string key_hash "密钥哈希值"
        string api_secret "Webhook 签名密钥"
        boolean is_active "启用状态"
    }

    EMAIL_TASKS {
        string id PK "任务ID (UUID)"
        string tenant_id FK "所属租户ID"
        string idempotency_key "客户端幂等键 (唯一索引)"
        string status "任务状态 (PENDING/PROCESSING/SUCCESS/FAILED)"
        string mail_subject "邮件主题"
        text raw_input_json "原始输入 JSON"
        text result_json "V3 提取结果 JSON"
        int duration_ms "耗时 (毫秒)"
        boolean is_charged "是否扣费"
        decimal charged_amount "扣费金额"
        string callback_url "Webhook 回调地址"
        string callback_status "回调状态 (PENDING/DELIVERED/FAILED)"
        string lease_owner "租约持有 Worker ID"
        datetime lease_expires_at "租约过期时间"
    }

    BILLING_TRANSACTIONS {
        int id PK "流水自增ID"
        string tenant_id FK "所属租户ID"
        string task_id FK "关联任务ID"
        string type "流水类型 (RECHARGE/DEDUCTION/REFUND)"
        decimal amount "交易金额"
        decimal balance_before "变动前余额"
        decimal balance_after "变动后余额"
        string description "流水说明"
        string operator "操作人 (ADMIN/SYSTEM)"
        datetime created_at "交易时间"
    }
```

---

## 4. 关键安全与可用性指标

| 指标维度 | 设计规范与保障措施 |
| :--- | :--- |
| **资金一致性保障** | 数据库 CHECK 约束、原子 UPDATE 条件更新、幂等防重扣保证 100% 账实相符 |
| **SSRF 防御** | Webhook URL 解析验证，强制阻断 RFC1918 私网、127.0.0.1、169.254.169.254 等私有网段 |
| **身份与权限隔离** | JWT Session 签名、PBKDF2 强哈希密码存储、租户级数据隔离防越权 |
| **单证防 DoS 截断** | 邮件附件数上限 10、PDF 单文件上限 20 页、Excel 单表上限 100 行、文本最大 10,000 字符 |
| **附件生命周期清理** | 后台每天自动扫描 `uploads/`，安全删除 90 天前原始附件，结构化 JSON 永久归档 |
| **代码测试覆盖率** | 183 项自动化单元与集成测试用例，**全后端代码覆盖率达 95.0%** |
