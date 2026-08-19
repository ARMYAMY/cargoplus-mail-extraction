# cargo-mail-extraction-skill-v3

CargoPlus 宜运货代邮件处理智能体 Skill 文件包 V3。

V3 在 V2 基础上升级二阶段 Schema、抽取规则、校验规则，并增加 `rules/normalize_output.py` 后处理校验模块。最终交付物仍是 `cargo-mail-extraction-skill-v3.zip`，但完整方案 B 需要客户 Agent 流程或业务系统在模型输出后调用后处理模块。

## 目录

```text
cargo-mail-extraction-skill-v3/
  SKILL.md
  README.md
  SOWORK_AGENT_PERSONA.md
  prompts/
    extract.md
    validate.md
  schemas/
    output.schema.json
  references/
    container-code-table.xlsx
    container-code-table.json
    container-code-table.md
  rules/
    normalize_output.py
  examples/
    sample-input.json
    sample-output.json
  tests/
    test_normalize_output.py
    cases/
      sample-01-draft.json
      sample-01-expected.json
```

## V3 Runtime Requirement

The package is delivered as `cargo-mail-extraction-skill-v3.zip`. Full V3 behavior requires the customer Agent flow or business system to run the post-processing module after model extraction:

```bash
python rules/normalize_output.py --input draft.json --output final.json --reference references/container-code-table.json
```

The normalizer uses Python 3.9+ standard library only. Runtime does not read Excel. The Excel file is kept as the customer-maintained source table, and `references/container-code-table.json` is used at runtime.

Optional dependencies:

1. `openpyxl`: only needed when regenerating `container-code-table.json` from Excel.
2. `jsonschema`: only needed if the customer wants Python-side full JSON Schema validation.

## 设计原则

1. 智能体只接收宜运侧预处理后的邮件正文和附件解析内容，不直接连接邮箱。
2. 附件是主来源；邮件正文只有明确更新附件内容时才覆盖附件。
3. 只输出原文中实际出现的值，不推断、不猜测、不补全、不编造。
4. 港口代码原文没有出现时留空，不根据港口名称推导。
5. `ContainerInfo` 只保留数组形式。
6. 最终只输出业务 JSON 本身，不输出 `audit`、证据、置信度或其他内部信息。
7. 对同一输入不得随机选择候选值；无法判断时留空。
8. 输出字段顺序必须固定。
9. V3 指定字段允许结构化拆分，包括联系方式、中文品名、数值单位、柜型。

## SoWork 运行参数要求

```json
{
  "temperature": 0,
  "top_p": 1,
  "presence_penalty": 0,
  "frequency_penalty": 0,
  "response_format": "json_object 或 json_schema",
  "model": "固定模型版本",
  "max_output_tokens": "固定且足够大"
}
```

最低要求：

1. `temperature` 必须设置为 `0`。
2. 同一个 Agent 必须固定使用同一个 skill 版本。
3. 同一个批次不得切换模型、prompt、schema 或解析流程。
4. 如果 SoWork 支持固定模型版本，必须固定模型版本。
5. 如果 SoWork 支持 JSON Object 或 JSON Schema 输出，必须启用。
6. `max_output_tokens` 必须足够大，避免 JSON 被截断。

## 推荐调用流程

1. 宜运侧完成邮件收取和附件拆分。
2. 文档解析层将 PDF、图片、Excel、Word 转成文本和表格结构。
3. 使用固定解析流程生成固定输入格式。
4. 使用 `prompts/extract.md` 进行字段抽取，模型参数必须为 `temperature=0`。
5. 使用 `schemas/output.schema.json` 做 JSON Schema 校验。
6. 如果结构校验失败，使用 `prompts/validate.md` 修复结构。
7. 调用 `rules/normalize_output.py` 进行字段补齐、联系方式拆分、数值单位拆分、柜型拆分和字段顺序固定。
8. 将最终 JSON 交给业务系统录入。

## 输入格式

```json
{
  "mail_subject": "",
  "mail_body": "",
  "attachments": [
    {
      "filename": "",
      "content_type": "",
      "text": "",
      "tables": [],
      "ocr_text": ""
    }
  ]
}
```

## 输出格式

输出必须符合 `schemas/output.schema.json`。顶层结构直接是业务字段对象，每个 `ContainerInfo` 对象包含 13 个字段。

## 后处理模块能力

`rules/normalize_output.py` 负责：

1. 顶层 57 个字段补齐、排序、删除多余字段。
2. `ContainerInfo` 每项 13 个字段补齐、排序、删除多余字段。
3. 拆分收发通名称和地址：`Name` 保留第一行主体名称，`Addr` 保留去掉第一行主体名称后的地址和联系方式原文。
4. 从收发通地址中抽取电话、邮箱、传真，并保留地址中的联系方式原文。
5. 拆分件数、毛重、净重、体积的数值和单位。
6. 将 `PackagesUnit` 和 `ContainerInfo[].Package` 归一为 `GoodsPackage` 代码；若没有匹配项，保留原文。
7. 拆分箱明细中的 `KGS`、`PCS`、`CBM`。
8. 根据 `references/container-code-table.json` 拆分和校验 `ContSize` / `ContType`。
9. 拆分中英文品名，不做翻译。
10. 将 `GoodsType` 归一化为客户代码：`S`=普货，`R`=冷冻，`D`=危险品，`O`=超标；未标出冷冻、危险品或超标时默认 `S`。

## 稳定性验收建议

1. 选择固定测试附件。
2. 使用同一 SoWork Agent、同一 skill v3、同一模型参数重复运行 3 次。
3. 每次都调用 `rules/normalize_output.py`。
4. 比较 3 次最终 JSON 是否完全一致。
5. 如果不一致，先检查文档解析或 OCR 输出是否一致。
6. 如果解析文本一致但 JSON 不一致，检查 SoWork 是否真的对抽取和校验阶段都设置了 `temperature=0`。
7. 如果 JSON 结构偶发不合法，优先启用 JSON Object 或 JSON Schema 输出模式。

## 安全建议

不要把邮箱账号、密码、API Key 或客户敏感凭证写入配置文件、prompt 或样本输出。正式系统应使用环境变量、密钥管理服务或企业内部凭证系统。
