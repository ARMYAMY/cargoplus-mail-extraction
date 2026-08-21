# 动态 Few-Shot 样本库 — 架构设计与实战使用手册

> **适用版本**：CargoPlus Mail Extraction v1.0.0+
>
> **适用模块**：管理员后台 →【反馈与持续优化】→【动态 Few-Shot 样本库】 & 【客户工单反馈审核】
>
> **核心源码**：`app/services/few_shot_service.py`、`app/api/admin/feedback.py`、`app/services/extraction_service.py`、`app/core/skill_runner.py`

---

## 1. 核心原理与技术架构

**Few-Shot In-Context Learning（上下文少样本学习）** 是一种**无需重新微调/训练大语言模型（LLM），在推理阶段将典型参考样本动态注入 Prompt** 的关键技术。

### 1.1 系统工作流与动态注入机制

```
  ┌─────────────────────────────────────────────────────────────┐
  │                      邮件/单证抽取任务触发                      │
  │        (POST /api/v1/extract/sync 或 Celery 异步队列)         │
  └──────────────────────────────┬──────────────────────────────┘
                                 │
                                 ▼
  ┌─────────────────────────────────────────────────────────────┐
  │                 FewShotService 动态样本检索                 │
  │  1. 租户隔离校验: 检索 (source_tenant_id = 当前租户 OR NULL)   │
  │  2. 状态过滤: WHERE is_active = true                        │
  │  3. 智能排序: ORDER BY priority DESC, created_at DESC       │
  │  4. 截断保护: LIMIT 2 (防止 Prompt 无限膨胀)                  │
  └──────────────────────────────┬──────────────────────────────┘
                                 │
                                 ▼
  ┌─────────────────────────────────────────────────────────────┐
  │                   Prompt 组装与 LLM 推理                    │
  │  System Prompt + 业务抽取规则 + Few-Shot 纠错案例 + 待抽取单证  │
  └──────────────────────────────┬──────────────────────────────┘
                                 │
                                 ▼
  ┌─────────────────────────────────────────────────────────────┐
  │           Skill V3 标准化与 Schema 校验 (Normalizer)         │
  └─────────────────────────────────────────────────────────────┘
```

### 1.2 注入 Prompt 的标准结构模板

系统会将命中的 Few-Shot 示例格式化为标准的 Markdown 代码块结构注入模型上下文：

```markdown
### 历史纠错与典型单证标准示例 (Few-Shot Reference):

[示例 1: 提单号去除前缀标签 (GENERAL)]
输入单证片段:
```text
B/L No: MSCU7042819365
Vessel: MAERSK HAMBURG / 045W
```
期望标准抽取字段 JSON (局部参考):
```json
{
  "bl_no": "MSCU7042819365",
  "vessel_name": "MAERSK HAMBURG",
  "voyage_no": "045W"
}
```
```

### 1.3 核心设计特性

1. **秒级热生效（Zero-Downtime Hot Reload）**：
   - 样本数据直接由 PostgreSQL/SQLite 实时读取，管理员在后台新增、修改或启停样本后，**对所有 API 及 Celery Worker 节点秒级即时生效**，无需重启服务或重新构建镜像。
2. **多租户安全隔离（Multi-Tenant Data Privacy）**：
   - 全局通用样本（`source_tenant_id IS NULL`）：全租户共享；
   - 租户专属样本（`source_tenant_id = tenant_xxx`）：仅在该租户提交抽取任务时注入，**绝对不会跨租户泄露**，有效保护企业客户单证商业隐私。
3. **熔断与降级隔离（Fault-Tolerant Isolation）**：
   - 若 Few-Shot 样本加载因网络或数据库瞬时抖动异常，系统捕获异常并记录 Debug 日志，自动平滑降级为 Zero-Shot 原始抽取，**绝不阻塞主抽取业务流程**。
4. **Token 截断防护（Context-Window Budgeting）**：
   - 单次抽取严格限制取 `priority` 最高的前 **2 条** 样本（总额外 Token 消耗控制在 200~500 tokens 以内），兼顾模型注意力聚焦与调用成本。

---

## 2. 数据库表结构与索引设计

### 2.1 表结构（`few_shot_examples`）

| 字段名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `VARCHAR(64)` | PRIMARY KEY | 样例唯一主键（如 `fs_1a2b3c4d5e6f`） |
| `feedback_id` | `VARCHAR(64)` | FOREIGN KEY, NULLABLE | 关联的客户反馈工单 ID（级联置空） |
| `source_tenant_id` | `VARCHAR(64)` | FOREIGN KEY, NULLABLE | 归属租户 ID；`NULL` 为全局共享 |
| `doc_type` | `VARCHAR(64)` | NOT NULL, DEFAULT `'GENERAL'` | 单据/错误类型（如 `GENERAL`, `BL`, `SO`, `INVOICE`） |
| `title` | `VARCHAR(255)` | NOT NULL | 样本标题（描述教学目的） |
| `input_excerpt` | `TEXT` | NOT NULL | 输入单证/邮件的原始文本片段 |
| `expected_output` | `JSON` | NOT NULL | 期望的正确抽取标准 JSON |
| `priority` | `INTEGER` | NOT NULL, DEFAULT `10` | 优先级权重（越大越优先被注入；管理后台表单默认填入 `20`） |
| `is_active` | `BOOLEAN` | NOT NULL, DEFAULT `TRUE` | 是否启用 |
| `created_at` | `TIMESTAMPTZ` | NOT NULL | 创建时间 |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL | 更新时间 |

### 2.2 索引与约束保障

- `uq_few_shot_feedback_id`：`UNIQUE (feedback_id) WHERE feedback_id IS NOT NULL`（防止同一张工单重复生成多个 Few-Shot 样本）。
- `ix_few_shot_examples_source_tenant_id`：`INDEX (source_tenant_id)`（保证多租户高效过滤与隔离检索）。

---

## 3. 样本新增与维护方式

### 方式一：管理员手动新增（人工构造高质量基准样本）

1. 登录管理后台（`http://<SERVER_IP>:30010/`）→ 导航至 **【反馈与持续优化】** → **【动态 Few-Shot 样本库】**；
2. 点击右上角 **「+ 新增少样本示例」** 按钮；
3. 填写表单项：
   - **单据类型**：通用单据建议填 `GENERAL`；特定单证类型填 `BL`（提单）、`SO`（订舱单）、`CONTAINER`（集装箱列表）等；
   - **标题**：清晰说明纠错目的（例如：“*提单号去除前缀标签与末尾校验位*”）；
   - **输入片段**：粘贴包含目标特征的真实原文片段（建议控制在 200~800 字符内，突出特征上下文）；
   - **期望输出 JSON**：填写针对该片段的标准 JSON 片段（**无需全量 57 个字段，只需包含关键纠正字段**）；
   - **优先级**：默认为 `20`。重大核心纠错建议设置为 `40 ~ 60`；
4. 点击保存，系统完成 JSON 语法校验后即刻作为全局样本入库生效。租户专属样本目前由反馈采纳流程自动生成。

---

### 方式二：客户反馈工单审核一键沉淀（数据飞轮闭环）

```
  ┌─────────────────────────────────────────────────────────────┐
  │                 客户在客户端 Portal 提交纠错反馈              │
  │     (附带修改后的期望 JSON / diff_fields / 纠错说明)        │
  └──────────────────────────────┬──────────────────────────────┘
                                 │
                                 ▼
  ┌─────────────────────────────────────────────────────────────┐
  │                 管理员在管理后台【反馈审核】面板核验             │
  │  - 展开「📄 关联任务原始输入」查看原始邮件主题与文本          │
  │  - 确认纠错合理性与退款对账                                │
  └──────────────────────────────┬──────────────────────────────┘
                                 │
                                 ▼
  ┌─────────────────────────────────────────────────────────────┐
  │                    勾选闭环沉淀选项并提交审核                  │
  │  [☑] 自动将该用例生成 Few-Shot 少样本 (优先权重 20)          │
  │  [☑] 同时沉淀为金标评测基准用例 (Benchmark Case)             │
  └──────────────────────────────┬──────────────────────────────┘
                                 │
                                 ▼
  ┌─────────────────────────────────────────────────────────────┐
  │       FewShot 样本库自动新增一条绑定该租户的专用纠错示例        │
  │       后续该租户发起的所有同类抽取任务自动命中此纠错逻辑        │
  └─────────────────────────────────────────────────────────────┘
```

1. 在管理后台 **【客户反馈工单】** 列表中点击待审核条目；
2. 弹窗内可点击展开 **「📄 关联任务原始输入」** 查看任务原始报文，核对客户修改字段；
3. 勾选 **「☑ 自动将该用例生成 Few-Shot 少样本」**（可同时勾选金标评测）；
4. 点击 **「采纳并归档」**，系统自动关联生成 `source_tenant_id` 为该客户的专有 Few-Shot 样本。

---

## 4. RESTful API 接口参考

| 方法 | 路径 | 说明 | 鉴权要求 |
|---|---|---|---|
| `GET` | `/admin/few-shots` | 按优先级和创建时间返回全部样本 | Admin JWT |
| `POST` | `/admin/few-shots` | 创建新的全局 Few-Shot 样本 | Admin JWT |
| `PUT` | `/admin/few-shots/{fs_id}` | 更新样本（包括 `is_active` 启停状态） | Admin JWT |
| `DELETE` | `/admin/few-shots/{fs_id}` | 删除样本（物理删除） | Admin JWT |

> 当前手动创建接口只创建全局样本；租户专属样本由客户反馈采纳流程自动生成。

#### 创建样本请求示例（`POST /admin/few-shots`）

```json
{
  "doc_type": "GENERAL",
  "title": "提单号去除前缀标签与空格",
  "input_excerpt": "Dear Sirs, please find confirmed B/L info:\nBL NO.: MSCU7042819365\nVESSEL: MAERSK HAMBURG / 045W\nPOL: YANTIAN",
  "expected_output": {
    "bl_no": "MSCU7042819365",
    "vessel_name": "MAERSK HAMBURG",
    "voyage_no": "045W",
    "pol_name": "Yantian"
  },
  "priority": 50,
  "is_active": true
}
```

---

## 5. 典型实战案例库（高频纠错规范）

### 案例 1：提单号（B/L No）去除前缀与空格
- **问题**：大模型偶发性将 `B/L No:` 或 `BL NO.` 作为号码一部分输出。
- **输入片段**：
  ```text
  BOOKING CONFIRMATION
  B/L NUMBER: COSU6389201948
  VESSEL/VOYAGE: COSCO PRIDE / 102E
  ```
- **期望 JSON**：
  ```json
  {
    "bl_no": "COSU6389201948",
    "vessel_name": "COSCO PRIDE",
    "voyage_no": "102E"
  }
  ```
- **推荐优先级**：`50`（全局通用）

---

### 案例 2：集装箱号去除内部空格与规范校验
- **问题**：扫描件 OCR 将集装箱号识别为 `TCLU 123456 7`。
- **输入片段**：
  ```text
  CONTAINER & SEAL DETAILS:
  1) TCLU 123456 7 / 40HC / SEAL: SHL9876543 / 25 CTNS
  2) MSKU 765432 1 / 20GP / SEAL: SHL1122334 / 18 CTNS
  ```
- **期望 JSON**：
  ```json
  {
    "containers": [
      {
        "container_no": "TCLU1234567",
        "size_type": "40HC",
        "seal_no": "SHL9876543",
        "quantity": 25,
        "unit": "CTNS"
      },
      {
        "container_no": "MSKU7654321",
        "size_type": "20GP",
        "seal_no": "SHL1122334",
        "quantity": 18,
        "unit": "CTNS"
      }
    ]
  }
  ```
- **推荐优先级**：`50`（全局通用）

---

### 案例 3：重量单位磅（LBS）自动换算为千克（KG）
- **问题**：北美航线单证中常使用 LBS，系统要求统一输出标准 KG。
- **输入片段**：
  ```text
  WEIGHT SPECIFICATION:
  GROSS WEIGHT: 12,500 LBS
  NET WEIGHT: 11,200 LBS
  MEASUREMENT: 68.5 CBM
  ```
- **期望 JSON**：
  ```json
  {
    "gross_weight_kg": 5670.0,
    "net_weight_kg": 5080.2,
    "volume_cbm": 68.5
  }
  ```
- **推荐优先级**：`40`（全局通用）

---

### 案例 4：中英混排港口标准化及 UN/LOCODE 代码映射
- **问题**：邮件中混杂中文港口名或口语化缩写，需标准化为国际英文港口名及 5 位五字码。
- **输入片段**：
  ```text
  ROUTING DETAILS:
  POL: 上海洋山港 (SHPG)
  POD: Hamburg, Germany (DEHAM)
  FND: Rotterdam, Netherlands (NLRTM)
  ```
- **期望 JSON**：
  ```json
  {
    "pol_name": "Shanghai",
    "pol_code": "CNSHA",
    "pod_name": "Hamburg",
    "pod_code": "DEHAM",
    "fnd_name": "Rotterdam",
    "fnd_code": "NLRTM"
  }
  ```
- **推荐优先级**：`40`（全局通用）

---

### 案例 5：租户专属业务映射（特定客户的固定业务规则）
- **问题**：某特定货代客户（如 `tenant_2131380d29cf`）的邮件格式特殊，发货人固定且贸易条款总是 CIF。
- **输入片段**：
  ```text
  Standard Customer Booking
  Terms: CIF Hamburg
  Destination Port: DEHAM
  Payment: Prepaid at POL
  ```
- **期望 JSON**：
  ```json
  {
    "incoterm": "CIF",
    "pod_code": "DEHAM",
    "freight_payment": "PREPAID"
  }
  ```
- **来源租户**：通过 `tenant_2131380d29cf` 的反馈工单采纳流程自动绑定
- **推荐优先级**：`45`

---

## 6. 运维与优化最佳实践

1. **样本原则 —— “小、准、专”**：
   - `input_excerpt` 只截取与纠错强相关的关键段落（200 ~ 600 字符），**不要整封大邮件全量贴入**；
   - `expected_output` 仅声明需要纠偏的字段键值，降低 LLM 注意力干扰。
2. **优先级梯度规划**：
   - `50 ~ 60`：严重基础格式错误（提单号、箱封号格式校验）；
   - `35 ~ 45`：业务字段标准化（港口代码、单位换算、费用条款）；
   - `20 ~ 30`：日常边缘长尾样本（备用池）。
3. **金标回归防护网（Regression Guard）**：
   - 在调整或新增高优先级全局 Few-Shot 样本后，建议前往管理后台 **【金标评测体系】** 执行一次基准回归测试，确保新样本在修正目标问题的同时，未引起其他字段的负向回退。
4. **定期审查与脱敏**：
   - Few-Shot 样本将作为 Prompt 一部分发送至大模型上游，录入样本时请确保剔除敏感个人隐私或企业机密财务数据。
