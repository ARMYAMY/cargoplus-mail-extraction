# CargoPlus 单机一键部署

适用于一台安装了 Docker Desktop 的 Windows 主机：PostgreSQL、Redis、API、Celery Worker、Celery Beat、Prometheus、Alertmanager、Grafana 和数据库备份都运行在同一台机器上，不需要域名。网站通过 Caddy 内部 CA 使用 HTTPS 对局域网发布。

## 一键安装或升级

双击项目根目录的 `deploy-single-host.cmd`。

首次执行会：

1. 复用现有 `cargoplus_postgres_data`、`cargoplus_redis_data` 和上传文件卷；
2. 生成本机 secret 文件，已有 secret 永不覆盖；
3. 构建并扫描应用镜像；
4. 只在 `0.0.0.0:80/443` 启动 HTTPS 入口，启动全部服务并等待健康检查通过；
5. 立即生成经过校验的 PostgreSQL custom-format 备份和 Redis RDB 备份。
6. 将 Caddy 内部 CA 根证书安装到当前 Windows 用户的受信任根证书库。

如果旧版 CargoPlus 已在运行，脚本会读取正在运行的 PostgreSQL 容器配置以兼容旧数据卷，不会初始化或删除原数据库。

脚本完成后会显示入口，例如 `https://172.30.0.131`。HTTP 80 端口只用于永久跳转 HTTPS，实际业务流量使用 443。API 不再发布 8001 端口，Grafana、Prometheus、Alertmanager、PostgreSQL 和 Redis 均只在 Docker 内部网络监听。

局域网其他终端访问时，需要把 `deploy/single-host/caddy-root.crt` 导入该终端的受信任根证书库。内部 CA 证书不是公网 CA 证书；如以后拥有域名，应切换为域名和公开受信任的 ACME 证书。

## 日常操作

在 PowerShell 中进入项目目录后执行：

```powershell
# 状态与日志
.\scripts\single_host.ps1 -Action Status
.\scripts\single_host.ps1 -Action Logs

# 启停；Stop 保留数据库和全部数据卷
.\scripts\single_host.ps1 -Action Stop
.\scripts\single_host.ps1 -Action Start

# 立即备份和隔离恢复演练
.\scripts\single_host.ps1 -Action Backup
.\scripts\single_host.ps1 -Action RestoreDrill
```

备份默认保存在 `data/backups`，每天一次、保留 14 天。PostgreSQL 备份通过 `pg_restore --list` 校验，Redis 备份通过 `redis-check-rdb` 校验。恢复演练只使用一次性 PostgreSQL 和内存数据目录，不会连接或修改正式数据库。

## 单机部署边界

这套方案可以可靠重启和恢复，但不是高可用架构：宿主机、Docker Desktop 或磁盘故障会同时影响数据库、Redis 和应用。至少每天把 `data/backups` 同步到另一块磁盘或另一台机器，并定期运行 `RestoreDrill`。

网站监听所有网卡，因此同一网络内能连接该主机 443 端口的设备都可能访问登录页面。不要把路由器公网端口转发到这台主机；需要互联网访问时，应准备域名和公开受信任证书，并配置防火墙、WAF 或 VPN。
