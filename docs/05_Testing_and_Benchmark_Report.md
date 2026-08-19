# CargoPlus 测试分析与高并发性能压测报告 (Testing & Benchmark Report)

---

## 1. 测试体系概览

CargoPlus 平台建立了覆盖**单元测试、业务规则归一化测试、多模态单证解析测试、原子计量与资金对账测试、分布式信号量队列削峰压测、断电自愈测试及 Web 安全防护测试**的工业级质量保障体系。

- **总自动化测试用例数**: **183 项**
- **测试通过率**: **100.00% (全部绿灯)**
- **后端代码覆盖率 (Code Coverage)**: **95.0%**

---

## 2. 后端代码覆盖率详情 (pytest --cov=app)

```text
=============================== tests coverage ================================
Name                                 Stmts   Miss  Cover   Missing Lines
------------------------------------------------------------------
app\__init__.py                          1      0   100%
app\api\__init__.py                      3      0   100%
app\api\admin\__init__.py               12      0   100%
app\api\admin\billing.py                28      0   100%
app\api\admin\recharge.py               17      0   100%
app\api\admin\stats.py                  99      1    99%   265
app\api\admin\tasks.py                  63      0   100%
app\api\admin\tenants.py               123      0   100%
app\api\deps.py                         95     11    88%   45-48, 63-66, 145-147
app\api\v1\__init__.py                  12      0   100%
app\api\v1\auth.py                     111      2    98%   155, 238
app\api\v1\billing.py                   84      1    99%   39
app\api\v1\extract.py                  229     30    87%   182-187, 283, 301, 312-313, 317-318, 344-353, 359-360, 420-424, 451
app\api\v1\tasks.py                     41      2    95%   20-21
app\api\v1\tenants.py                   32      0   100%
app\celery_app.py                       11      3    73%   17-22
app\celery_tasks.py                    124      7    94%   49, 111-118, 140
app\config.py                          122      0   100%
app\core\__init__.py                    12      0   100%
app\core\limits.py                       7      0   100%
app\core\money.py                       26      1    96%   34
app\core\normalizer.py                 279      6    98%   102, 106-107, 147-148, 183
app\core\observability.py               25      2    92%   43-44
app\core\parser\__init__.py             82      1    99%   128
app\core\parser\eml_parser.py           73      7    90%   77-78, 85-86, 96-98
app\core\parser\excel_parser.py         39      0   100%
app\core\parser\ocr_engine.py           40      0   100%
app\core\parser\pdf_parser.py           31      2    94%   34-35
app\core\parser\word_parser.py          31      2    94%   36-37
app\core\redis_client.py                31      0   100%
app\core\skill_runner.py               133     10    92%   35-36, 41-42, 54, 56, 135-136, 190, 193
app\core\validator.py                   29      0   100%
app\database.py                         67     10    85%   39-41, 64, 66, 73, 77, 90, 106-107
app\main.py                            130      1    99%   80
app\models\__init__.py                   4      0   100%
app\models\billing.py                   22      0   100%
app\models\task.py                      52      0   100%
app\models\tenant.py                    39      0   100%
app\monitor.py                          86      6    93%   43-44, 128-151
app\schemas\__init__.py                  5      0   100%
app\schemas\billing.py                  32      0   100%
app\schemas\cargo_v3.py                 74      0   100%
app\schemas\task.py                     83      3    96%   18, 37, 49
app\schemas\tenant.py                   67      8    88%   54, 57, 83-88
app\services\__init__.py                 6      0   100%
app\services\auth_service.py            90      6    93%   71-72, 80-81, 88, 119
app\services\billing_service.py        178     19    89%   66-67, 120, 132, 171-172, 241-242, 270-276, 308, 330, 350-351
app\services\extraction_service.py     152      7    95%   103, 117, 170, 235, 243-245
app\services\queue_service.py          168     11    93%   57, 70-71, 114-116, 124-126, 129, 140
app\services\storage_service.py         55     11    80%   23, 35-41, 49, 54-55
app\services\webhook_dispatcher.py      12      0   100%
app\services\webhook_service.py         88      9    90%   33, 35, 52, 82, 115-116, 126-128
------------------------------------------------------------------
TOTAL                                 3455    179    95%
============================ 183 passed in 24.82s =============================
```

---

## 3. 核心测试集覆盖清单

| 测试模块文件 | 测试用例数 | 关键测试验证目标 |
| :--- | :--- | :--- |
| `test_api_flow.py` | 6 | 健康检查、鉴权拦截、同步抽取链路、HMAC Webhook 签名 |
| `test_api_admin_comprehensive.py` | 5 | 管理员租户增删改查、人工充值、单价与并发上限修改、全局日账单 |
| `test_api_v1_auth_and_tenants.py` | 2 | 租户自助注册待审核、API Key 免密登录、密码哈希升级迁移 |
| `test_api_v1_billing_and_tasks.py` | 2 | 租户端日账单分页、流水明细分页、CSV 电子账单导出 |
| `test_api_v1_extract_and_deps.py` | 2 | 异步 JSON 抽取、异步文件上传抽取、DoS 上限截断 |
| `test_billing.py` | 1 | 原子预留金额、成功扣费、失败零扣费、账户余额一致性 |
| `test_celery_queue_regressions.py` | 6 | Celery 分布式信号量租约、429 频率限制、超时自动熔断、断电自愈 |
| `test_concurrency_limits.py` | 11 | 多租户独立并发限额、全局工作协程调度、幂等键去重 |
| `test_core_parsers_all.py` | 7 | .eml 邮件、多页 PDF、Excel 多 Sheet、Word、图片 RapidOCR |
| `test_normalizer.py` | 4 | Skill V3 57 顶层字段规范、收发通剥离、件重体分离、中英品名拆分 |
| `test_monitor_observability_and_config.py` | 4 | Prometheus 监控 Exporter、Redis Sentinel 容灾、生产安全校验 |
| `test_security_regressions.py` | 14 | SSRF 防御、路径穿越防御、SQL 注入、XSS、密码加盐防护 |

---

## 4. 高并发基准压测数据 (Concurrency Benchmark)

### 4.1 压测环境配置
- **测试环境**: 10 工作协程并行消费池
- **上游大模型**: 商汤科技开放平台 (`deepseek-v4-flash-0731`)
- **压测任务数**: 20 封复杂货代订舱确认邮件 (中英混排 + 附件)

### 4.2 压测结果指标
```text
================================================================================
                      CargoPlus 并发压力测试基准报告
================================================================================
测试租户 ID: tenant_demo_001 (初始余额: ¥241.00)
提交任务总数: 20 封邮件
并发提交耗时: 4.05 秒 (请求吞吐量: 4.94 QPS)
--------------------------------------------------------------------------------
任务调度与完成统计:
  - 成功完成任务数: 18 / 20 (90.0%)
  - 上游超时/失败数: 2 / 20 (10.0%, 捕获上游商汤 API 60s 超时)
  - 平均单单处理时长: 4,820 ms
--------------------------------------------------------------------------------
财务扣费与资金一致性核对:
  - 实际扣费金额: ¥9.00 (18 笔成功 × ¥0.50)
  - 失败免扣费校验: 2 笔失败任务扣费均为 ¥0.00 (通过)
  - 租户期末余额: ¥232.00
  - 资金对账平衡校验: PASSED (账目偏差: ¥0.00, 准确率: 100.00%)
================================================================================
```

---

## 5. 核心测试结论

1. **削峰排队稳固**：20 个并发任务在 4 秒内全量被 API 接收并写入异步任务队列，上游客户端无任何连接阻塞或丢单；
2. **扣费准确度 100%**：高并发扣费采用数据库原子条件更新（`UPDATE ... WHERE balance - reserved >= unit_price`），**无任何账目误差**；
3. **失败严格免扣费**：失败任务原子释放预留金额，确保客户商业权益不受技术波动影响；
4. **测试覆盖率达标**：全后端代码覆盖率达到 **95%**，满足企业级高质量交付标准。
