---
name: cargo-mail-extraction-v3
description: Deterministically extract CargoPlus freight forwarding email and attachment fields into the agreed V3 final JSON format.
---

# CargoPlus Mail Extraction Skill V3

## Purpose

Extract structured freight forwarding fields from preprocessed email body text and attachment contents. Return only the final JSON object matching the CargoPlus V3 schema.

This skill does not directly log in to mailboxes. Email collection, attachment splitting, OCR, and document parsing are handled by the caller.

V3 adds the二阶段 schema changes and is designed to be used with `rules/normalize_output.py` after model extraction. The skill extracts semantic business values; the normalizer enforces deterministic field order, missing-field defaults, contact extraction, numeric/unit splitting, container size/type normalization, and basic Chinese/English goods-name splitting.

## Input

Expected input:

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

## Required Runtime Settings

These settings belong to the caller or Agent platform, not to the skill file itself.

1. `temperature` must be `0`.
2. The model version must be fixed when the platform supports it.
3. `top_p` should be `1` when the platform supports it.
4. `presence_penalty` and `frequency_penalty` should be `0` when the platform supports them.
5. JSON Object or JSON Schema output mode should be used when the platform supports it.
6. The same batch must not switch model, prompt, schema, parsing method, or skill version.
7. `max_output_tokens` must be large enough to avoid truncating the final JSON.

## Recommended V3 Call Flow

1. Preprocess email body, attachment text, attachment tables, and OCR text into the agreed input format.
2. Run `prompts/extract.md` with fixed model settings and V3 schema output mode when available.
3. Validate or repair JSON structure with `schemas/output.schema.json` and `prompts/validate.md` if needed.
4. Run `rules/normalize_output.py --input draft.json --output final.json --reference references/container-code-table.json`.
5. Send `final.json` to the CargoPlus business system.

## Runtime Requirement

Full V3 behavior requires Python 3.9+ for the post-processing module. `rules/normalize_output.py` uses Python standard library only at runtime and reads `references/container-code-table.json`; it does not read Excel at runtime.

Optional dependencies:

1. `openpyxl`: only needed when regenerating `container-code-table.json` from Excel.
2. `jsonschema`: only needed if the customer wants Python-side full JSON Schema validation.

## Core Rules

1. Extract only values actually present in the provided input.
2. Do not infer, guess, complete, translate, or fabricate.
3. Preserve original text values exactly as much as possible except for the confirmed V3 structural splits.
4. If a field is absent, output an empty string `""`.
5. If a port code is absent, output an empty string `""`; do not infer a code from a port name.
6. `ContainerInfo` must only be an array.
7. If no container detail is found, output `"ContainerInfo": []`.
8. Each `ContainerInfo` item must include all 13 child fields.
9. Attachments are the primary source.
10. Email body only overrides attachment values when it explicitly updates or revises the attachment content.
11. If conflict cannot be resolved, leave the affected field empty.
12. Return valid JSON only.
13. Do not output Markdown, explanations, confidence scores, evidence, audit data, or fields outside the schema.

## V3 Structural Split Rules

1. Split party name and address: `ShipperName` / `ConsigneeName` / `NotifyName` hold the first-line party name, while `ShipperAddr` / `ConsigneeAddr` / `NotifyAddr` hold the original address and contact text after removing that first-line name.
2. Keep contact text in address fields and also extract tel/email/fax into independent fields.
3. Split English and Chinese goods names into `GoodsName` and `GoodsNameCN`; do not translate.
4. Split top-level package, weight, and volume fields into number and unit fields.
5. Normalize `PackagesUnit` and `ContainerInfo[].Package` to `GoodsPackage` codes from the reference table; preserve the original text only if no matching code can be found.
6. Split container-level `KGS`, `PCS`, and `CBM`; put `PCS` unit into `Package`.
6. Split container type by customer reference table, for example `40HQ` -> `ContSize="40"`, `ContType="HQ"`.
7. Unknown container type must not be guessed. If raw text can be preserved, append it to `Remark` during normalization.
8. `GoodsType` must be one code only: `S` for General Cargo/普货, `R` for Reefer Cargo/冷冻, `D` for Dangerous Goods/Hazardous Materials/危险品, and `O` for Exceed standard/Over Dimension Cargo/超标. If reefer, dangerous, or over-dimension cargo is not explicitly marked, normalize to `S`.

## Output

Return only the JSON structure defined in `schemas/output.schema.json`. The top-level object is the final business JSON; do not wrap it in `final_json`.

Each `ContainerInfo` item must contain all fields defined under `ContainerInfo.items.required` in `schemas/output.schema.json`.

## Field Aliases

Common aliases:

- `ShipperName`, `ShipperAddr`: Shipper, 发货人, 托运人
- `ShipperTel`, `ShipperEmail`, `ShipperFax`: TEL, PHONE, EMAIL, FAX, 电话, 邮箱, 传真
- `ConsigneeName`, `ConsigneeAddr`: Consignee, 收货人
- `ConsigneeTel`, `ConsigneeEmail`, `ConsigneeFax`: TEL, PHONE, EMAIL, FAX, 电话, 邮箱, 传真
- `NotifyName`, `NotifyAddr`: Notify, Notify Party, 通知人
- `NotifyTel`, `NotifyEmail`, `NotifyFax`: TEL, PHONE, EMAIL, FAX, 电话, 邮箱, 传真
- `Carrier`: Carrier, 承运船东, 船公司
- `BookingNo`: Booking No, Bkg No, SO No, 订舱单号
- `BLNo`: BL No, B/L No, 提单号
- `Vessel`: Vessel, Vsl Name, 船名
- `Voyage`: Voyage, Voy, 航次
- `PORName`: Place of Receipt, 收货地
- `POLName`: Port of Loading, 起运港, 装货港
- `PODName`: Port of Discharge, 目的港, 卸货港
- `DeliveryName`: Final Destination, Place of Delivery, 交货地, 目的地
- `ETD`: ETD, Sailing Date, 开航日, 预计离港日
- `ETA`: ETA, 预计到港日
- `CutOffDate`: Closing Date, Cut-off, 截关时间
- `SICutOff`: SI Cut-off, 截补料时间
- `GoodsName`, `GoodsNameCN`: Commodity, Commodity Name, Description of Goods, 货名, 品名, 中文品名
- `Marks`: Marks, Shipping Marks, 唛头
- `HSCode`: HS Code, 商品编码, 商编
- `Packages`, `PackagesUnit`: Packages, PCS, Pkg, 件数, 包装单位
- `GrossWeight`, `GrossWeightUnit`: Gross Weight, G.W., KGS, 毛重
- `NetWeight`, `NetWeightUnit`: Net Weight, N.W., 净重
- `Volume`, `VolumeUnit`: CBM, Measure, 体积
- `FreightTerm`: Freight Prepaid, Freight Collect, 运费条款
- `ReleaseBLType`: Telex Release, TLX Release, Sea Waybill, Original BL, 电放, 正本
- `Movement`: CY-CY, CY-DOOR, DOOR-CY, DOOR-DOOR
- `PackingMode`: FCL, LCL, 整箱, 拼箱
