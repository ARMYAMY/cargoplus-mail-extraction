# CargoPlus 开发者与部署运维手册 (Developer & Deployment Guide)

---

## 1. 快速上手指南 (本地开发)

### 1.1 环境要求
- **Python**: 3.11+
- **包管理工具**: `uv` (推荐) 或 `pip`
- **外部服务**:
  - 商汤科技 / OpenAI 兼容 LLM API (`https://api.senseaudio.cn/v1` 或 DeepSeek / 硅基 / 阿里百炼 / 智谱 / Ollama 等)
  - Redis 6.0+ (用于 Celery 削峰队列与分布式信号量；本地调试支持自适应降级自愈)
  - PostgreSQL 14+ (生产推荐) 或 SQLite 3.35+ (开发自测)

### 1.2 安装依赖
```powershell
# 进入服务目录
cd D:\agent\agv\cargo\cargo_service

# 创建虚拟环境
uv venv .venv

# 激活虚拟环境 (Windows PowerShell)
.\.venv\Scripts\Activate.ps1

# 安装核心依赖
uv pip install -r requirements-dev.txt
```

### 1.3 环境变量配置
复制 `.env.example` 为 `.env`：
```ini
# 运行环境
ENVIRONMENT=development
DEBUG=true
PORT=8000

# 数据库连接 (开发模式默认使用带 WAL 的 SQLite)
DATABASE_URL=sqlite+aiosqlite:///./data/cargo_service.db

# 大模型配置 (支持主流 OpenAI 规范服务端点)
LLM_BASE_URL=https://api.senseaudio.cn/v1
LLM_API_KEY=your-llm-api-key
LLM_MODEL=deepseek-v4-flash-0731
LLM_TEMPERATURE=0.0
LLM_REQUEST_TIMEOUT_SECONDS=60

# 管理员核心秘钥与会话秘钥 (生产环境长度至少 32 字符)
ADMIN_SECRET_KEY=cargo-plus-admin-secret-2026
SESSION_SECRET_KEY=cargo-plus-session-secret-2026

# 队列模式: local (单进程内存 Worker) 或 celery (Redis 分布式 Worker)
TASK_QUEUE_MODE=local
CELERY_BROKER_URL=redis://127.0.0.1:6379/0
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/1

# 租户计费与并发限制
DEFAULT_TENANT_UNIT_PRICE=0.50
DEFAULT_TENANT_CONCURRENCY=20
WORKER_CONCURRENCY=10
```

### 1.4 启动应用服务
```powershell
# 启动 FastAPI 主应用服务 (包含内置本地队列 Worker)
.\.venv\Scripts\python.exe run.py
```
- 服务访问地址：`http://localhost:8000`
- 交互式 Swagger API 文档：`http://localhost:8000/docs`
- 租户财务对账中心：`http://localhost:8000/portal`
- 统一登录入口：`http://localhost:8000/login`
- 自助注册开户：`http://localhost:8000/register`

---

## 2. 自动化测试与覆盖率验证

系统内置完整的自动化测试套件（覆盖率保持在 **94%~95%**），包含 **198 项测试用例 (100% 绿灯通过)**。

```powershell
# 运行完整测试套件并生成代码覆盖率报告
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing tests/

# 仅运行特定测试模块
.\.venv\Scripts\python.exe -m pytest tests/test_admin_llm_config.py -v
.\.venv\Scripts\python.exe -m pytest tests/test_api_flow.py -v
.\.venv\Scripts\python.exe -m pytest tests/test_concurrency_limits.py -v
```

---

## 3. Docker 容器化部署

### 3.1 Docker Compose 一键启动 (推荐)
```powershell
# 1. 启动基础设施与服务集群
docker compose up -d --build

# 2. 查看容器运行状态
docker compose ps

# 3. 查看实时日志
docker compose logs -f api worker webhook-worker
```

### 3.2 容器服务编排矩阵
| 服务名称 | 镜像/构建 | 端口映射 | 职能描述 |
| :--- | :--- | :--- | :--- |
| `api` | `Dockerfile` | `8001:8000` | FastAPI 异步 HTTP 核心网关 |
| `worker` | `Dockerfile` | - | Celery 邮件抽取主 Worker 池 (并发 10) |
| `webhook-worker` | `Dockerfile` | - | Celery 独立 Webhook 推送 Worker 池 (并发 5) |
| `beat` | `Dockerfile` | - | 定时任务调度器 (任务租约自愈、附件清理) |
| `postgres` | `postgres:16-alpine` | `5432:5432` | 生产级关系型数据库 |
| `redis` | `redis:7-alpine` | `6379:6379` | 消息代理与分布式信号量缓存 |

### 3.3 SQLite 数据无损迁移至 PostgreSQL
若需将开发阶段的 SQLite 数据迁移至 PostgreSQL：
```powershell
$env:POSTGRES_MIGRATION_URL = "postgresql+asyncpg://cargo:cargo-local-password@127.0.0.1:5432/cargo"
.\.venv\Scripts\python.exe scripts\migrate_sqlite_to_postgres.py `
  --source data\cargo_service.db `
  --report data\migration_rejected_billing.json
```

---

## 4. 生产环境加固与就绪检查清单

在正式部署生产环境前，请执行加固检查脚本：
```powershell
.\.venv\Scripts\python.exe scripts\production_readiness.py
```

### 生产必须核验项：
1. `ENVIRONMENT=production` 已启用；
2. `ADMIN_SECRET_KEY` 与 `SESSION_SECRET_KEY` 为不同且至少 32 位的强随机密码；
3. `DATABASE_URL` 已切换至生产 PostgreSQL；
4. `CELERY_BROKER_URL` 切换至托管 Redis 或 Redis Sentinel 集群；
5. `CORS_ALLOWED_ORIGINS` 移除通配符 `*`，明确限定为业务前端域名；
6. Prometheus 监控采集与 90 天附件自动清理正常运行。
