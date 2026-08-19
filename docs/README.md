# CargoPlus 货代邮件智能结构化抽取平台 · 官方文档中心 (Documentation Hub)

欢迎查阅 **CargoPlus** 工业级多模态货代邮件与单证智能结构化抽取平台全套工程、算法与业务文档。

---

## 📚 文档全景导航与索引

| 序号 | 文档名称 | 主要内容与受众 | 链接 |
| :---: | :--- | :--- | :--- |
| **01** | **产品需求说明书 (PRD)** | 货代业务痛点、产品定位、57 字段规范、大模型动态探测切换、0.50元/次计费模型与 SLA | [01_PRD_Product_Requirements_Document.md](./01_PRD_Product_Requirements_Document.md) |
| **02** | **系统架构设计说明书** | 异步微服务拓扑、上游 API 探活与模型发现、断电自愈机制、双模自适应降级、租户原子锁与 SSRF 安全 | [02_System_Architecture_Design.md](./02_System_Architecture_Design.md) |
| **03** | **开发与运维部署指南** | uv 虚拟环境配置、配置项字典、Postgres/SQLite 数据库迁移、单机/Docker 生产容器化部署 | [03_Developer_and_Deployment_Guide.md](./03_Developer_and_Deployment_Guide.md) |
| **04** | **API 接口规范与接入手册** | 鉴权格式、异步/同步/文件上传接口、大模型配置管理接口、Webhook 验签算法、调用示例 | [04_API_Reference_Manual.md](./04_API_Reference_Manual.md) |
| **05** | **测试与并发性能压测报告** | 198 项自动化测试矩阵（95% 覆盖率）、高并发压测实测数据、100% 账目平衡校验 | [05_Testing_and_Benchmark_Report.md](./05_Testing_and_Benchmark_Report.md) |
| **06** | **用户与管理员操作手册** | 客户自助注册、租户专属对账中心、CSV 账单导出、大模型配置与 1-Click 模型拉取、管理总控台操作指南 | [06_User_and_Admin_Manual.md](./06_User_and_Admin_Manual.md) |
| **07** | **Celery 队列运维与并发指南** | 队列监控、断电恢复、分布式信号量、Worker 调优与运维命令集 | [CELERY_OPERATIONS.md](./CELERY_OPERATIONS.md) |
| **08** | **云服务器单机一键部署手册** | 单机 Docker Compose 生产一键脚本、Caddy TLS 自动证书、数据备份与监控套件 | [CLOUD_ONE_CLICK_DEPLOYMENT.md](./CLOUD_ONE_CLICK_DEPLOYMENT.md) |

---

## 🌐 常用服务访问地址

- **统一登录页面**：[http://localhost:8000/login](http://localhost:8000/login)
- **企业开户注册**：[http://localhost:8000/register](http://localhost:8000/register)
- **租户对账中心**：[http://localhost:8000/portal](http://localhost:8000/portal)
- **管理总控台**：[http://localhost:8000/](http://localhost:8000/)
- **交互式 API 文档 (Swagger)**：[http://localhost:8000/docs](http://localhost:8000/docs)
- **Redoc 文档**：[http://localhost:8000/redoc](http://localhost:8000/redoc)
