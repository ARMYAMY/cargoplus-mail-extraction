# CargoPlus 接口参考手册与集成指南 (API Reference Manual)

---

## 1. 认证鉴权规范

### 1.1 租户 API Key 鉴权 (推荐)
客户端发起抽取或查询请求时，需在 HTTP 请求头中传入 API Key：
```http
Authorization: Bearer cg_live_xxxxxxxx_yyyyyyyyyyyyyyyyyyyyyyyy
```
或通过专属 Header 传递：
```http
X-API-Key: cg_live_xxxxxxxx_yyyyyyyyyyyyyyyyyyyyyyyy
```

### 1.2 管理员管理端鉴权
管理员登录后调用管理端 API（`/admin/*`）时，应在请求头中传入管理员会话令牌：
```http
Authorization: Bearer <ADMIN_SESSION_TOKEN>
```
非生产环境或显式启用兼容开关时，也可使用 `X-Admin-Secret` 旧式鉴权。

---

## 2. 租户端核心 API 接口 (`/api/v1`)

### 2.1 租户在线注册开户
- **Endpoint**: `POST /api/v1/auth/register`
- **说明**: 提交企业开户申请。注册成功后默认进入【待审核】状态（`is_active=False`），待管理员审核通过后激活并赠送 ¥50.00 体验金。
- **请求体 (JSON)**:
  ```json
  {
    "company_name": "上海迅捷国际货运代理有限公司",
    "contact_email": "ops@xunjie-cargo.com",
    "contact_phone": "13800000000",
    "password": "Password123!"
  }
  ```
- **响应示例 (HTTP 200)**:
  ```json
  {
    "code": 0,
    "message": "开户申请已提交！您的企业租户目前处于【待审核】状态，待管理员审核开通后即可登录使用。",
    "data": {
      "tenant_id": "tenant_a1b2c3d4",
      "company_name": "上海迅捷国际货运代理有限公司",
      "contact_email": "ops@xunjie-cargo.com",
      "balance": 50.0,
      "unit_price": 0.5,
      "is_active": false,
      "api_key": "cg_a1b2c3d4_8f7e6d5c4b3a2918",
      "api_secret": "sec_9a8b7c6d5e4f3a2b1c"
    }
  }
  ```

---

### 2.2 租户登录认证
- **Endpoint**: `POST /api/v1/auth/login`
- **说明**: 支持邮箱密码登录或 API Key 免密凭证登录。
- **请求体 (JSON)**:
  ```json
  {
    "account": "ops@xunjie-cargo.com",
    "password": "Password123!"
  }
  ```
- **响应示例 (HTTP 200)**:
  ```json
  {
    "code": 0,
    "message": "登录成功",
    "data": {
      "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
      "tenant_id": "tenant_a1b2c3d4",
      "tenant_name": "上海迅捷国际货运代理有限公司",
      "balance": 50.0,
      "unit_price": 0.5
    }
  }
  ```

---

### 2.3 获取租户 API 密钥信息
- **Endpoint**: `GET /api/v1/tenants/api-key`
- **说明**: 租户登录后获取本企业当前生效的 API Key 前缀与 Webhook 签名 Secret。
- **响应示例 (HTTP 200)**:
  ```json
  {
    "code": 0,
    "data": {
      "tenant_id": "tenant_a1b2c3d4",
      "key_id": "key_e5f6g7h8",
      "key_prefix": "cg_a1b2c3d4",
      "api_secret": "sec_9a8b7c6d5e4f3a2b1c",
      "is_active": true
    }
  }
  ```

---

### 2.4 异步提交邮件抽取任务 (JSON 模式)
- **Endpoint**: `POST /api/v1/extract/async`
- **Headers**:
  - `Authorization: Bearer <API_KEY>`
  - `Idempotency-Key: <UUID>` (可选，防重复提交)
- **请求体 (JSON)**:
  ```json
  {
    "mail_subject": "Booking Confirmation - COSCO SHIPPING SHANGHAI TO ROTTERDAM",
    "mail_body": "DEAR CUSTOMER, PLEASE FIND BOOKING DETAILS BELOW:\nBOOKING NO: COSU638291048\nVESSEL/VOYAGE: CSCL GLOBE / 042W\nPOL: SHANGHAI, CHINA\nPOD: ROTTERDAM, NETHERLANDS\nCONTAINER: 2 x 40HQ (CSLU1234567, CSLU7654321)\nGOODS: 1200 CARTONS FOOTWEAR, GW: 14500.00 KGS, VOL: 68.5 CBM",
    "callback_url": "https://erp.xunjie-cargo.com/api/webhooks/cargo"
  }
  ```
- **响应示例 (HTTP 200)**:
  ```json
  {
    "code": 0,
    "message": "Task submitted successfully",
    "data": {
      "task_id": "task_9f8e7d6c5b4a3928",
      "status": "PENDING",
      "created_at": "2026-08-19T13:30:00.000Z"
    }
  }
  ```

---

### 2.5 异步提交邮件与单证附件抽取 (文件上传模式)
- **Endpoint**: `POST /api/v1/extract/async/upload`
- **Content-Type**: `multipart/form-data`
- **表单字段**:
  - `files`: 文件列表（支持 `.eml`, `.pdf`, `.xlsx`, `.docx`, 图片，最多 10 个文件）
  - `mail_subject`: (可选) 邮件主题
  - `callback_url`: (可选) Webhook 回调地址

---

### 2.6 同步即时抽取任务 (调试与小单证)
- **Endpoint**: `POST /api/v1/extract/sync`
- **说明**: 客户端阻塞等待抽取完成并直接返回 57 字段 JSON 及当前使用的实际大模型（大文件或慢速网络建议使用异步模式）。
- **响应示例 (HTTP 200)**:
  ```json
  {
    "code": 0,
    "message": "Success",
    "task_id": "task_9f8e7d6c5b4a3928",
    "status": "SUCCESS",
    "duration_ms": 3420,
    "charged_amount": 0.50,
    "model_used": "deepseek-v4-flash-0731",
    "data": {
      "BookingNo": "COSU638291048",
      "POLName": "SHANGHAI",
      "PODName": "ROTTERDAM"
    }
  }
  ```

---

### 2.7 查询单任务状态与抽取结果
- **Endpoint**: `GET /api/v1/tasks/{task_id}`
- **响应示例 (抽取成功状态)**:
  ```json
  {
    "code": 0,
    "data": {
      "task_id": "task_9f8e7d6c5b4a3928",
      "status": "SUCCESS",
      "duration_ms": 3420,
      "charged_amount": 0.5,
      "result": {
        "BookingNo": "COSU638291048",
        "Vessel": "CSCL GLOBE",
        "Voyage": "042W",
        "POL": "CNSHA",
        "POLName": "SHANGHAI",
        "POD": "NLRTM",
        "PODName": "ROTTERDAM",
        "Packages": "1200",
        "PackagesUnit": "CARTONS",
        "GrossWeight": "14500.00",
        "GrossWeightUnit": "KGS",
        "Volume": "68.5",
        "VolumeUnit": "CBM",
        "GoodsName": "FOOTWEAR",
        "ContainerInfo": [
          {
            "ContainerNo": "CSLU1234567",
            "ContSize": "40",
            "ContType": "HQ"
          },
          {
            "ContainerNo": "CSLU7654321",
            "ContSize": "40",
            "ContType": "HQ"
          }
        ]
      },
      "created_at": "2026-08-19T13:30:00.000Z",
      "completed_at": "2026-08-19T13:30:03.420Z"
    }
  }
  ```

---

### 2.8 租户财务日账单与流水查询 (支持分页)
- **日账单汇总**: `GET /api/v1/billing/daily?page=1&page_size=20&start_date=2026-08-01`
- **逐笔流水明细**: `GET /api/v1/billing/transactions?page=1&page_size=20`
- **导出 CSV 账单**: `GET /api/v1/billing/export-csv?start_date=2026-08-01&end_date=2026-08-31`

---

## 3. 管理端核心 API 接口 (`/admin`)

### 3.1 大模型配置与动态探活接口

#### (1) 获取当前大模型配置
- **Endpoint**: `GET /admin/llm-config`
- **Headers**: `X-Admin-Secret: <ADMIN_SECRET_KEY>`
- **响应示例**:
  ```json
  {
    "base_url": "https://api.senseaudio.cn/v1",
    "api_key": "",
    "api_key_masked": "sk-R...5f8c (67 字符)",
    "is_configured": true,
    "model": "deepseek-v4-flash-0731",
    "timeout_seconds": 60,
    "temperature": 0.0,
    "runtime_editable": true
  }
  ```

#### (2) 保存并热更新大模型配置
- **Endpoint**: `PUT /admin/llm-config`
- **请求体**:
  ```json
  {
    "base_url": "https://api.senseaudio.cn/v1",
    "api_key": "sk-your-new-api-key",
    "model": "deepseek-v4-flash-0731",
    "timeout_seconds": 60
  }
  ```

#### (3) 探测大模型连通性与往返延迟
- **Endpoint**: `POST /admin/llm-config/test`
- **请求体**:
  ```json
  {
    "base_url": "https://api.senseaudio.cn/v1",
    "api_key": "sk-your-key-here",
    "model": "deepseek-v4-flash-0731"
  }
  ```
- **响应示例**:
  ```json
  {
    "code": 0,
    "message": "大模型连通性测试成功！",
    "data": {
      "model": "deepseek-v4-flash-0731",
      "latency_ms": 1280,
      "response_preview": "{\"status\":\"ok\"}"
    }
  }
  ```

#### (4) 从上游 API 动态拉取可用模型列表
- **Endpoint**: `POST /admin/llm-config/models`
- **说明**: 向上游服务商的 `/models` 端点探测，自动解析 OpenAI 与 Ollama 格式并返回可用模型列表。
- **请求体**:
  ```json
  {
    "base_url": "https://api.senseaudio.cn/v1",
    "api_key": "sk-your-key-here"
  }
  ```
- **响应示例**:
  ```json
  {
    "code": 0,
    "message": "成功从 API 获取到 39 个可用模型",
    "data": {
      "models": [
        "deepseek-v4-flash",
        "deepseek-v4-flash-0731",
        "deepseek-v4-pro",
        "glm-5.2",
        "kimi-k2.6",
        "qwen3.8-27b",
        "sensenova-6.8-flash-lite"
      ],
      "count": 39,
      "source": "https://api.senseaudio.cn/v1/models"
    }
  }
  ```

---

### 3.2 租户管理与财务流水接口

| 接口路径 | 方法 | 功能描述 |
| :--- | :--- | :--- |
| `/admin/tenants` | `GET` | 查询所有租户列表（包含审核状态、余额、单价、并发上限） |
| `/admin/tenants` | `POST` | 管理员直接开通新企业租户并分配初始密钥 |
| `/admin/tenants/{tenant_id}` | `PUT` | 修改租户名称、电话、单价、并发上限或启用状态 |
| `/admin/tenants/{tenant_id}/keys` | `GET` | 查询指定租户名下所有 API Key 凭证与 Secret |
| `/admin/tenants/{tenant_id}/keys` | `POST` | 为指定租户生成新的 API Key 凭证 |
| `/admin/tenants/keys/{key_id}` | `DELETE` | 吊销指定 API Key |
| `/admin/tenants/{tenant_id}/status?is_active=true` | `PUT` | 审核通过或禁用租户 |
| `/admin/recharge/{tenant_id}` | `POST` | 管理员为指定租户人工充值余额 (`{"amount": 500.00}`) |
| `/admin/tenants/{tenant_id}/unit-price`| `PUT` | 动态修改租户单次调用单价 (`{"unit_price": 0.35}`) |
| `/admin/tasks` | `GET` | 分页全局检索所有抽取任务与状态 |
| `/admin/tasks/statuses` | `POST` | 批量精确查询最多 100 个任务的状态 |
| `/admin/tasks/{task_id}/retry` | `POST` | 重试未扣费且未预留资金的失败任务 |
| `/admin/billing/transactions` | `GET` | 管理员全局财务扣费与充值流水（分页） |
| `/admin/stats` | `GET` | 总控台大盘实时指标、今日消耗与近 14 天营收趋势 |

---

### 3.3 客户反馈工单与动态 Few-Shot 样本库接口

| 接口路径 | 方法 | 功能描述 |
| :--- | :--- | :--- |
| `/admin/feedbacks` | `GET` | 分页查询客户纠错反馈工单列表（支持状态与租户过滤） |
| `/admin/feedbacks/{feedback_id}` | `GET` | 查询工单详情，包含任务原始邮件主题与原始输入文本 |
| `/admin/feedbacks/{feedback_id}/accept` | `POST` | 采纳工单并执行退款，支持一键沉淀为 Few-Shot 样本及金标评测用例 |
| `/admin/feedbacks/{feedback_id}/reject` | `POST` | 驳回工单并填写驳回原因 |
| `/admin/few-shots` | `GET` | 按优先级查询动态 Few-Shot 样本列表 |
| `/admin/few-shots` | `POST` | 新增全局 Few-Shot 示例（支持局部 JSON 与优先级定义） |
| `/admin/few-shots/{id}` | `PUT` | 更新 Few-Shot 示例（包括 `is_active` 启停状态） |
| `/admin/few-shots/{id}` | `DELETE` | 删除指定 Few-Shot 示例 |

---

## 4. Webhook 签名校验规范 (HMAC-SHA256)

### 4.1 回调请求头
系统向客户提供的 `callback_url` 推送 POST 通知时，会附带签名请求头：
```http
X-Timestamp: 1724064600000
X-Signature-SHA256: d5a4e6b2c8...
Content-Type: application/json
```

### 4.2 客户端 Python 验签代码示例
```python
import hmac
import hashlib
import time

def verify_webhook(raw_body_bytes: bytes, timestamp_str: str, signature_header: str, tenant_secret: str) -> bool:
    # 1. 防重放攻击检查 (时间戳偏差不超过 5 分钟)
    req_time = int(timestamp_str)
    now_time = int(time.time() * 1000)
    if abs(now_time - req_time) > 300_000:
        return False

    # 2. 计算签名: HMAC_SHA256(secret, timestamp + "." + body)
    message = f"{timestamp_str}.".encode("utf-8") + raw_body_bytes
    expected_sig = hmac.new(tenant_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()

    # 3. 恒定时间比对，防止时序攻击
    return hmac.compare_digest(expected_sig, signature_header)
```
