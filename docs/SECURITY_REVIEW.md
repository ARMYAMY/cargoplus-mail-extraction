# CargoPlus 上线安全评审

评审日期：2026-08-19  
范围：FastAPI API、管理/租户前端、PostgreSQL、Redis/Celery、文件上传、LLM 与 Webhook 出站访问、Docker/Caddy 单机部署、备份恢复和依赖供应链。

## 1. 结论

当前版本已达到“受控公网单机部署”的基线：仅暴露 HTTPS 入口，数据服务不对宿主机发布端口，生产配置失败关闭，认证有限流，管理原始密钥不再直通 API，Webhook 有 SSRF 防护，上传有数量/单文件/总量/路径约束，任务成功和扣费同事务，镜像非 root 且只读，数据库与 Redis 有每日校验备份及 PostgreSQL 恢复演练。

本评审没有发现仍未处理的 Critical 问题。仍存在需要接受或后续治理的风险：单机单点、备份默认仍在同一台主机、浏览器会话存放在 `localStorage`、前端保留内联脚本导致 CSP 需要 `unsafe-inline`、Webhook DNS 校验与实际连接之间仍有很窄的 DNS 重绑定窗口、依赖锁定未使用 wheel 哈希。它们不阻止受控上线，但不应被描述成无风险或高可用。

## 2. 威胁模型与信任边界

```text
Internet client
      │ HTTPS 443 (HTTP 80 redirects/ACME)
      ▼
    Caddy ─────► FastAPI API ─────► HTTPS LLM provider
                     │  │
              internal│  └────────► HTTPS tenant webhook
                     ▼
              PostgreSQL + Redis
                     │
               Celery worker/Beat
                     │
              uploads + local backups
```

假设：云账号、宿主机 root、Docker daemon 和 LLM 密钥持有者是受信任管理员；租户、上传内容、邮件正文、Webhook URL、HTTP 请求头和 LLM 返回内容均不可信。

## 3. 已修复问题

| 等级 | 问题 | 风险 | 处理 |
|---|---|---|---|
| High | 登录/注册无速率限制 | 管理密码爆破、账户密码喷洒、注册滥用 | Redis 固定窗口限流；生产强制启用；Redis 故障时鉴权失败关闭 |
| High | 原始 `ADMIN_SECRET_KEY` 可作为管理 API Bearer/header | 长期根凭据在每次请求中传播 | 生产仅允许登录接口校验原始密钥，后续使用 4 小时签名会话 |
| High | 生产 API 文档公开 | 扩大攻击面并泄漏完整接口结构 | 生产关闭 docs/redoc/openapi，Caddy 再次阻断 |
| High | 依赖使用 `>=` 浮动版本 | 重建不可重复，升级可未经评审进入生产 | 直接依赖精确固定，增加完整 Linux 运行锁；部署前 Trivy 阻断扫描 |
| Medium | 无 Host 头白名单 | Host header poisoning、缓存边界错误 | 生产启用 `TrustedHostMiddleware`，只允许公网主机和内部健康检查名 |
| Medium | 生产可接受 HTTP CORS/LLM、本地队列 | 明文出站或错误部署绕过 Celery | 启动时强制 HTTPS origins/LLM、PostgreSQL、Redis、Celery 和认证限流 |
| Medium | Google Fonts 与无 SRI CDN 脚本 | 第三方跟踪和前端供应链篡改 | 删除 Google Fonts；Chart.js 固定版本并增加 SHA-384 SRI |
| Medium | 缺少 CSP | XSS 后利用面较大 | 禁止 object/base/frame，限制 connect/img/font；保留内联脚本兼容项 |
| Medium | 注册密码最短 6 位，登录输入无上限 | 弱口令、超大输入资源消耗 | 新注册最短 10 位；账号、密码、管理员字段增加上限 |
| Medium | 登录错误区分账户不存在/密码错误 | 账户枚举 | 两者统一为“账号或密码错误” |
| Medium | 云 Compose 误暴露内部组件的可能 | 数据库/Redis/监控面被扫描 | 云版只发布 Caddy 80/443，数据网络 internal，不部署监控 UI |

## 4. 已验证的控制

### 4.1 鉴权与租户隔离

- API Key 只在创建时返回原文，数据库保存 SHA-256 摘要；随机 Key 具有足够熵；
- 租户密码使用随机盐 PBKDF2-SHA256 310,000 次，并自动升级旧摘要；
- 会话包含角色、主体、签发/过期时间和随机 nonce，HMAC-SHA256 签名并常量时间比较；
- 管理路由统一依赖 `verify_admin_access`，租户任务和账务查询按 tenant ID 过滤；
- 生产关闭 DEBUG 本地绕过和演示租户种子；管理并发测试必须显式携带目标 `X-Tenant-ID`；
- 管理、会话、数据库、Redis 和 LLM 密钥分别生成并经 Docker secrets 文件挂载；宿主 secrets 目录为 root-only `700`，文件为 Compose 非 root 容器兼容的 `644`。

### 4.2 账务一致性与队列

- 余额、单价、充值、租户并发和 worker 并发均有服务端上下界；
- 新任务先原子预留余额，成功结果和扣费在同一事务提交，失败释放预留；
- billing transaction 对 task 保持幂等约束，恢复演练检查重复扣费和余额不变量；
- Celery 使用 late ack、worker lost 重入队、prefetch 1、任务租约、超时和恢复扫描；
- Redis 使用带 TTL 的租户级 ZSET 信号量，worker 崩溃后并发槽自动回收；
- Webhook 独立队列，避免慢回调占满 LLM worker；Beat 只启动一个实例且调度任务有分布式锁。

### 4.3 文件上传与解析

- 服务端限制扩展名、文件数、单文件和总大小；Caddy 另有 110 MB 请求上限；
- 文件名取 basename、字符白名单化、添加随机前缀并使用独占创建 `xb`；
- 处理前对路径 `resolve()`，要求位于 uploads 根目录且为普通文件；
- 部分上传、幂等冲突和任务创建失败会清理已写文件；
- 上传目录是应用唯一可写持久卷，应用根文件系统只读。

文件解析库仍是高复杂度攻击面。上线应保持大小限制，不要把 worker 并发设得超过内存承受能力，并在解析依赖升级后重新执行恶意样本和压缩炸弹测试。

### 4.4 Webhook SSRF

- 只接受 HTTP(S)，生产只接受 HTTPS，拒绝 URL 用户名/密码；
- 每次投递前解析全部 A/AAAA，任一地址不是全局公网地址即拒绝；
- 拒绝 localhost、环回、RFC1918、链路本地、组播和云元数据地址；
- 不自动跟随 3xx，降低跳转到内网的风险；
- HMAC-SHA256 覆盖时间戳和原始 JSON，重试次数和超时有上限。

残余：校验 DNS 后 HTTP 客户端会再次解析，理论上存在 DNS 重绑定 TOCTOU 窗口。更高安全级别应使用自定义 transport 把校验后的 IP 固定到连接并保留原 Host/SNI，或通过独立出站代理实施 CIDR 策略。

### 4.5 容器与网络

- 应用镜像固定 Python 基础镜像 digest，运行 UID 10001，不保留 pip/setuptools/wheel；
- 应用容器 `read_only`、`cap_drop: ALL`、`no-new-privileges`、独立 tmpfs；
- PostgreSQL/Redis 只在 internal data network，不映射任何宿主机端口；
- Caddy admin API 关闭，TLS 数据卷持久化，HTTP 自动跳转 HTTPS；
- JSON 日志限制为 20 MB × 5，避免日志填满系统盘；
- Caddy、PostgreSQL、Redis 镜像使用标签加多架构 digest 固定。

## 5. 残余风险与上线条件

| 等级 | 残余风险 | 要求 |
|---|---|---|
| High（可用性） | 单台云服务器是共同故障域 | 接受停机窗口；重要业务改用托管 PostgreSQL/Redis、多 API/worker 和外部负载均衡 |
| High（恢复） | 默认备份与生产数据同机 | 上线前配置对象存储/异机复制，并从异机备份恢复到新服务器至少一次 |
| Medium | 管理/租户令牌存 `localStorage` | 仅可信终端使用；后续迁移 HttpOnly+Secure+SameSite Cookie、CSRF 和服务端撤销 |
| Medium | CSP 包含 `unsafe-inline` | 把四个 HTML 的内联脚本/事件迁移到静态 JS，用 nonce/hash 后移除该项 |
| Medium | 依赖锁无 wheel SHA-256 哈希 | CI 使用受信任包镜像，后续生成 `--require-hashes` 多架构 lock，持续镜像扫描 |
| Medium | 没有 WAF/云 DDoS 业务限流 | 云安全组收紧 SSH；按真实流量启用云 WAF/DDoS 和租户/IP 配额监控 |
| Medium | 无 MFA、即时会话吊销/密码重置 | 管理入口限制源 IP；高价值部署加入 MFA、token version 或企业身份系统 |
| Low | `/health` 暴露队列概况 | 外部只需 `/health/live`；可在 Caddy 继续收紧详细健康接口 |

## 6. 上线检查清单

- [ ] 云账号开启 MFA，root SSH 禁止密码登录，使用密钥并限制安全组来源；
- [ ] 安全组和 `ss -lntup` 确认无 5432/6379/8000/3000/9090/9093；
- [ ] 域名 A 记录正确，或 IP 模式根 CA 已安全安装到客户端；
- [ ] `deploy-cloud.sh status` 所有核心服务 healthy/running；
- [ ] `/docs`、`/openapi.json`、`/metrics` 从公网返回 404；
- [ ] HTTP 跳 HTTPS，证书链有效，响应有 HSTS/CSP/nosniff/frame deny；
- [ ] 管理登录连续错误触发 429，Redis 故障时登录返回 503；
- [ ] 完成成功、失败、余额不足、幂等和并发上限测试；
- [ ] Webhook 对 localhost、私网、云元数据、生产 HTTP 和重定向均拒绝；
- [ ] `backup` 和 `restore-drill` 成功；
- [ ] 备份已复制到异机位置并实际恢复；
- [ ] Trivy HIGH/CRITICAL 可修复漏洞为 0，镜像 secret scan 为 0；
- [ ] 根据真实 LLM 配额阶梯压测，429/P95 可接受后才提高并发；
- [ ] 配置 CPU、内存、磁盘、重启、备份新鲜度、失败率、队列深度、LLM 429/延迟告警。

## 7. 交付验证项

- Python 语法编译和全量 pytest；
- 新增生产安全配置与认证限流测试；
- `docker compose config --quiet`；
- Caddy domain/IP 模板 `caddy validate`；
- 部署脚本 ShellCheck；
- 从锁文件重建应用镜像；
- Trivy 漏洞、secret、misconfiguration 扫描；
- 容器 HTTPS、私有路径、非公开端口、备份和恢复检查。

最终结果以交付时测试输出及目标云主机上的上线清单为准。
