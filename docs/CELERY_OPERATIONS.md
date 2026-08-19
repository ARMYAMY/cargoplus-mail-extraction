# CargoPlus Celery 队列与上线边界

## 运行链路

1. API 在 PostgreSQL 中原子预留租户额度并创建 `PENDING` 任务。
2. API 只把 `task_id` 投递到 Redis；API Key、租户 Secret、邮件正文不会进入 broker。
3. 抽取 worker 从 PostgreSQL 认领任务并写入带过期时间的租约，再调用 LLM。
4. 成功结果、任务状态、实际扣费和唯一扣费流水在同一数据库事务中提交。
5. Webhook 进入独立的 `cargo-webhooks` 队列，慢回调不占用抽取 worker。
6. Celery Beat 每 30 秒扫描未投递或租约过期的任务，并重新投递。

## 已设置的故障保护

- `acks_late` 与 `reject_on_worker_lost`：worker 异常退出时 broker 可重新投递。
- 数据库租约：同一业务任务只能由当前租约持有者结算。
- `(tenant_id, idempotency_key)` 唯一约束：客户端重试不会重复预留。
- `(task_id, type)` 唯一约束：同一任务不能生成两条扣费流水。
- Redis Lua 有序集合租约：跨 worker 实施租户并发上限，进程崩溃后占位会自动过期。
- 全局/租户待处理上限：过载时 API 返回 `429` 和 `Retry-After`。
- Redis AOF：降低 Docker 或主机异常退出时的 broker 数据损失窗口。
- PostgreSQL 金额和并发检查约束：绕过 API 的非法写入也会失败。
- worker 预取数为 1：降低长任务场景下某个 worker 预占大量任务的概率。

## 本地 Compose 不等于高可用生产集群

本地 `docker-compose.yml` 仍是单机可靠部署，不是无单点部署。仓库已在
`docker-compose.production.yml` 中补齐应用侧的生产能力，但上线方仍必须提供真实的托管基础设施和生产参数：

- PostgreSQL 定时备份、恢复演练及托管高可用；
- Redis 持久卷备份或托管 Redis/Sentinel；
- 可接收 Alertmanager 消息的生产告警 Webhook 与值班流程；
- 对真实 LLM 配额进行压测，确认上游限流后再提高 worker 并发；
- 可公开验证的生产域名、DNS 和镜像仓库；
- 真实密钥托管文件及其轮换流程。

生产 Compose 已提供 Caddy TLS/反向代理、Docker secrets、Prometheus/Alertmanager/Grafana、备份与隔离恢复演练、Trivy/SBOM 门禁，以及 Beat 调度任务的 Redis 分布式锁。即使误启动两个 Beat，当前唯一周期任务也会由锁去重；运维上仍建议只保持一个 Beat 实例。

默认抽取并发为 8，虽然代码允许 1～100，但不应直接调到 100。先从 8 开始，根据 LLM 的 429、P95 延迟、CPU、内存和 PostgreSQL 连接数逐级调整。租户配置上限为 1～30，实际并发还会被全局 worker 并发限制。

## 安全发布与停止

```powershell
docker compose up -d --build
docker compose ps
docker compose logs -f api worker webhook-worker beat
```

抽取 worker 的 `stop_grace_period` 为 7 分钟，正常 `docker compose stop` 会先暖关闭并等待在途任务。不要使用 `docker kill`；若进程被强杀，任务会在租约过期后由 Beat 恢复。

不要主动横向扩容 Beat；Redis 锁用于容错误启动，不是扩容手段。API 和 worker 可以扩容，但在单机 Compose 中扩容前应先确认 PostgreSQL 最大连接数与 Docker Desktop 资源配额。

## 生产部署基线

生产环境使用独立的 `docker-compose.production.yml`。该文件不启动本机 PostgreSQL 或 Redis，而是强制通过 Docker secrets 接入托管 PostgreSQL 与托管 Redis/Redis Sentinel，并只通过 Caddy 暴露 80/443。

### 1. 准备托管依赖

- PostgreSQL：启用多可用区、自动快照、时间点恢复（PITR）和 TLS；分别创建应用账号与只读备份账号。
- Redis：优先使用支持 TLS、持久化和自动故障转移的托管 Redis。若使用 Sentinel，至少部署在三个独立故障域。
- DNS：`APP_DOMAIN` 和 `MONITOR_DOMAIN` 的 A/AAAA 记录必须指向部署主机，80/443 必须可达，Caddy 才能自动签发和续期证书。
- 告警：准备一个 HTTPS 告警 Webhook，用于企业微信、飞书、Slack、PagerDuty 或内部告警网关。

Sentinel 模式示例：

```text
CELERY broker secret:
sentinel://sentinel-a:26379/0;sentinel://sentinel-b:26379/0;sentinel://sentinel-c:26379/0

REDIS_SENTINEL_URLS:
sentinel-a:26379,sentinel-b:26379,sentinel-c:26379
```

应用的 Redis Lua 并发信号量和 Celery 会解析相同的 Sentinel master。`REDIS_SENTINEL_MASTER_NAME` 必须与服务端一致。

### 2. 创建环境和 secrets

```powershell
Copy-Item deploy\.env.production.example deploy\.env.production
Copy-Item deploy\secrets\database_url.example deploy\secrets\database_url
# 对其余 *.example 文件执行同样操作，再写入真实值
```

在 `deploy/.env.production` 中把所有 `*_SECRET_FILE` 改为非 `.example` 文件。管理员密钥、会话密钥必须不同且至少 32 字符。数据库与 Redis 使用 TLS URL；生产镜像必须填写镜像仓库返回的不可变 `@sha256:` 摘要。

生产检查会拒绝示例域名、示例密钥、可变镜像标签、非 TLS 依赖和不存在的备份目录：

```powershell
.\.venv\Scripts\python.exe scripts\production_readiness.py `
  --env-file deploy\.env.production
```

### 3. 构建、扫描、发布

```powershell
docker build -f Dockerfile -t cargoplus-app:production ..
.\scripts\scan_image.ps1 -Image cargoplus-app:production
# 推送镜像后，把 registry 返回的 sha256 digest 写入 deploy/.env.production

docker compose --env-file deploy\.env.production `
  -f docker-compose.production.yml pull
docker compose --env-file deploy\.env.production `
  -f docker-compose.production.yml up -d
```

Trivy 会阻止存在未修复的 HIGH/CRITICAL 漏洞、密钥或高危配置的镜像，并在 `reports/` 生成 CycloneDX SBOM。

### 4. 监控与告警

Prometheus 每 15 秒采集 API 和平台 exporter。默认规则覆盖：

- API/monitor 不可用与 API 5xx 比例；
- 抽取/Webhook worker 离线；
- Beat 调度心跳超过 90 秒；
- 队列深度、15 分钟任务失败率和过期任务租约；
- LLM HTTP 429、异常、超时和平均延迟；
- PostgreSQL 备份超过 26 小时、恢复演练超过 8 天。

Grafana 通过 `https://MONITOR_DOMAIN` 访问，不允许匿名登录。外部请求无法访问应用 `/metrics`，Prometheus 只在 Docker 网络内抓取。

### 5. 备份与恢复演练

`postgres-backup` 启动后立即执行一次 `pg_dump -Fc`，随后默认每 24 小时运行。每份备份都会先由 `pg_restore --list` 校验，再生成 SHA-256 校验文件，最后原子改名。`POSTGRES_BACKUP_DIR` 必须指向加密磁盘或 NFS，并额外复制到异地对象存储；不要把唯一备份留在 Docker 命名卷或部署主机。

执行隔离恢复演练：

```powershell
docker compose --env-file deploy\.env.production `
  -f docker-compose.production.yml --profile restore-drill up `
  --abort-on-container-exit --exit-code-from restore-drill restore-db restore-drill

docker compose --env-file deploy\.env.production `
  -f docker-compose.production.yml --profile restore-drill down
```

演练使用 tmpfs 中的一次性 PostgreSQL，不连接或清空生产数据库，并检查租户金额约束与重复扣费。

### 6. LLM 配额压测

从 5、10、20 并发逐级测试，每一级观察至少 15 分钟。压测会真实调用 LLM 并真实扣费，所以脚本要求显式确认：

```powershell
$env:ADMIN_SECRET_KEY = "从安全存储临时读取"
.\.venv\Scripts\python.exe scripts\benchmark_concurrency.py `
  --url https://cargo.example.com `
  --tasks 100 --concurrency 5 `
  --confirm-billable-load-test
```

只有在 LLM 429 为零或处于供应商批准范围、P95 延迟满足 SLA、队列持续回落且数据库连接有余量时，才提高 `CELERY_WORKER_CONCURRENCY`。修改后重复同级测试，不要从 8 直接调到 100。
