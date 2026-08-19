# CargoPlus 云服务器一键部署

本文用于一台 Ubuntu/Debian 云服务器上的正式单机部署。它会自动安装 Docker、生成独立随机密钥、启用 UFW、构建并扫描应用镜像、启动 PostgreSQL/Redis/Celery/Caddy，并执行 HTTPS 健康检查。

> 这是“单机可靠部署”，不是多可用区高可用。数据库、队列和应用都在同一台服务器，主机故障会导致整体不可用。数据安全依赖本机备份加你配置的异机/对象存储备份。

## 1. 部署结果

- 公网只监听 `0.0.0.0:80`、`0.0.0.0:443/tcp` 和 `0.0.0.0:443/udp`；
- PostgreSQL、Redis、API、Celery 和备份容器不发布宿主机端口；
- 不部署 Grafana、Prometheus、Alertmanager；
- PostgreSQL 使用持久卷并每天生成校验过的自定义格式备份；
- Redis 开启 AOF `everysec`、RDB 快照和每日校验备份；
- 任务由 Celery 持久队列处理，独立 Webhook worker，Beat 只启动一个实例；
- 容器自动重启、日志轮转、应用容器只读根文件系统、删除 Linux capabilities；
- 为 API、worker、PostgreSQL、Redis 和 Caddy 设置 CPU、内存、PID 上限，降低单组件拖垮整机的风险；
- 管理端 API 文档在生产模式关闭，认证接口启用 Redis 限流；
- 应用镜像部署前默认用 Trivy 阻断存在可修复 HIGH/CRITICAL 漏洞的构建。

## 2. 最低准备

推荐配置：Ubuntu 24.04/22.04 或 Debian 12、4 核、8 GB 内存、40 GB SSD。最低 4 GB 内存可以启动，但 OCR 并发应保持为 1。

云厂商安全组只放行：

| 端口 | 来源 | 用途 |
|---|---|---|
| SSH 实际端口/tcp | 你的固定办公 IP（优先） | 运维 |
| 80/tcp | `0.0.0.0/0` | HTTPS 跳转与域名 ACME 验证 |
| 443/tcp | `0.0.0.0/0` | HTTPS |
| 443/udp | `0.0.0.0/0` | HTTP/3，可不开放 |

不要开放 5432、6379、8000、3000、9090、9093。

源码包必须保留如下相邻目录：

```text
cargo/
├── cargo_service/
└── cargo-mail-extraction-skill-v3/
```

从 Windows 上传示例：

```powershell
scp -r D:\agent\agv\cargo root@服务器公网IP:/opt/cargoplus-src
```

## 3. 有域名部署（推荐）

先把域名 A 记录指向服务器公网 IPv4，并等待公网 DNS 生效。脚本会校验 DNS 是否指向本机，然后由 Caddy 自动申请和续期公信证书。

```bash
ssh root@服务器公网IP
cd /opt/cargoplus-src/cargo/cargo_service
sudo bash deploy-cloud.sh deploy --domain cargo.example.com
```

首次运行会静默提示输入 LLM API Key。非交互部署可先创建权限为 `600` 的密钥文件：

```bash
install -m 600 /dev/null /root/cargoplus-llm-key
editor /root/cargoplus-llm-key
sudo bash deploy-cloud.sh deploy --domain cargo.example.com --llm-key-file /root/cargoplus-llm-key
```

部署完成后访问 `https://cargo.example.com/`。

## 4. 没有域名部署

公网 IP 一般不能直接取得通用公信 CA 证书，因此此模式使用 Caddy 私有 CA。网站仍然是 HTTPS，但每台访问设备必须先信任这台服务器独有的根证书。

```bash
cd /opt/cargoplus-src/cargo/cargo_service
sudo bash deploy-cloud.sh deploy --ip 服务器公网IPv4
```

不传 `--ip` 时脚本会自动探测公网 IPv4：

```bash
sudo bash deploy-cloud.sh deploy
```

取回根证书并在 Windows 当前用户信任：

```powershell
scp root@服务器公网IP:/opt/cargoplus/caddy-root.crt .
certutil -addstore -user Root .\caddy-root.crt
```

macOS：

```bash
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain caddy-root.crt
```

Ubuntu/Debian 客户端：

```bash
sudo install -m 0644 caddy-root.crt /usr/local/share/ca-certificates/cargoplus.crt
sudo update-ca-certificates
```

只在你确认根证书来自自己的服务器时信任它。根 CA 私钥保存在 Caddy 持久卷中；丢失该卷后重新生成的 CA 需要在客户端重新安装。

## 5. 管理员登录

部署脚本不会把管理密码打印到终端或日志。它保存在服务器 root 专用文件：

```bash
sudo cat /opt/cargoplus/admin-credentials.txt
```

登录页用户名固定为 `admin`。管理密码只用于 `/auth/admin/login` 换取最长 4 小时的签名会话，生产环境不允许把原始管理密码直接作为管理 API 的 Bearer Token。

## 6. 日常命令

在 `cargo_service` 目录执行：

```bash
sudo bash deploy-cloud.sh status
sudo bash deploy-cloud.sh logs
sudo bash deploy-cloud.sh logs worker
sudo bash deploy-cloud.sh backup
sudo bash deploy-cloud.sh restore-drill
sudo bash deploy-cloud.sh upgrade
sudo bash deploy-cloud.sh export-ca
```

`upgrade` 会先生成备份，再构建和扫描当前源码，然后重建服务。常用文件：

| 文件 | 权限/用途 |
|---|---|
| `/opt/cargoplus/admin-credentials.txt` | `600`，管理员初始凭据 |
| `/opt/cargoplus/backups/` | `700`，数据库与 Redis 备份 |
| `/opt/cargoplus/caddy-root.crt` | IP 模式客户端根证书，不含私钥 |
| `deploy/cloud/.env` | `600`，非秘密运行参数 |
| `deploy/cloud/secrets/` | 目录 `700`、文件 `644`；普通宿主用户无法穿过目录，容器非 root UID 可读取明确挂载的文件 |

## 7. 备份与恢复要求

本机备份无法应对云盘损坏、误删整机或账号被攻陷。必须再配置一份异机备份，建议每天在备份完成后同步 `/opt/cargoplus/backups/` 到开启版本控制和生命周期策略的对象存储，并使用独立、最小权限凭据。

- 每天检查 `.last_backup_success` 和 `.last_redis_backup_success` 不超过 26 小时；
- 每周执行 `restore-drill`；
- 每月在一台全新服务器完成完整灾难恢复演练；
- 对象存储保留 30～90 天，并额外保留月度归档；
- 不要只备份 Docker 卷快照，PostgreSQL 逻辑备份仍是恢复验证的依据。

恢复到新服务器的安全流程：先部署同版本代码但暂不开放安全组 80/443，把目标备份放到 `/opt/cargoplus/backups/`，执行恢复演练验证，再把数据恢复到正式 PostgreSQL 卷，最后切换流量。不要直接在原生产库上试恢复。

## 8. 升级与回退

升级前记录当前源码提交和镜像 ID：

```bash
git rev-parse HEAD 2>/dev/null || true
docker image inspect cargoplus-app:cloud --format '{{.Id}}'
sudo bash deploy-cloud.sh backup
```

更新源码后执行 `sudo bash deploy-cloud.sh upgrade`。如果健康检查失败，脚本会保留日志并返回非零。回退时恢复旧源码/镜像后重新启动；数据库结构升级前必须先读迁移说明，不能假设应用镜像回退等于数据库回退。

## 9. 故障排查

```bash
sudo bash deploy-cloud.sh status
sudo bash deploy-cloud.sh logs api
sudo bash deploy-cloud.sh logs worker
sudo ufw status verbose
sudo ss -lntup
```

期望宿主机监听只有 SSH、80、443。域名证书失败时依次检查 A 记录、安全组、UFW、80/443 占用。任务堆积时先检查 worker 日志中的 LLM 429/超时，再调整上游配额；不要先盲目提高 Celery 并发。

## 10. 停止与卸载

方案刻意不提供“一键删除数据”，防止误操作。停止服务：

```bash
docker compose --project-directory . --env-file deploy/cloud/.env -f docker-compose.cloud.yml down
```

该命令不带 `-v`，不会删除 PostgreSQL、Redis、上传文件或 Caddy 证书卷。任何卷删除必须在验证异机备份后人工执行。
