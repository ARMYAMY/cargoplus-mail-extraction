# 宜运 CargoPlus API 真实用例测试包

本目录用于 2026 年 9 月 7 日至 9 月 11 日的一周加速测试。测试对象为宜运准备的 30～50 个真实业务案例。

## 目录内容

- `Invoke-CargoPlusFileTest.ps1`：上传一个业务案例、自动轮询任务，并保存完整结果。
- `Submit-CargoPlusFeedback.ps1`：在业务人员确认后提交字段纠错反馈。
- `complete-gold-template.json`：完整 57 字段人工标准答案模板。
- `CargoPlus_API使用手册_宜运测试版_HTTPS版_20260903.docx`：客户接入、证书安装和完整验证教程。
- `宜运CargoPlus_API真实用例测试台账_HTTPS版.xlsx`：含测试Checklist、案例、字段差异、执行日志、问题和统计台账。
- `宜运CargoPlus_API真实用例一周测试计划_HTTPS版.docx`：对外执行计划和每日安排。

目录中不带“HTTPS版”的旧文档仅供内部归档，不再对外发送。

## 运行前检查

请先打开Excel台账中的“测试Checklist”，逐项完成“测试准备”和“案例与金标准备”。只有必需项全部通过或经确认不适用，且没有未关闭P0问题时，才进入下一阶段。

1. 通过安全渠道取得 `caddy-root.crt`，并通过另一渠道核对项目方提供的 SHA-256 校验信息。将根证书安装到实际 API 调用环境后，使用 HTTPS 健康检查确认信任生效。未完成证书信任不得上传真实文件。
2. 不得使用 `curl -k`、Python `verify=False` 或其他关闭 TLS 校验的方式绕过证书验证。
3. 在当前 PowerShell 窗口中设置 API Key，不要写入脚本、Excel、邮件或聊天：

   ```powershell
   $secureKey = Read-Host "API Key" -AsSecureString
   $env:CARGOPLUS_API_KEY = [System.Net.NetworkCredential]::new("", $secureKey).Password
   ```

4. 确认文件可正常打开、单次最多 10 个文件、普通单文件不超过 50MB、旧版 `.doc` 不超过 20MB、合计不超过 100MB。
5. 标准模式 PDF 只处理前 20 页；高精度模式 PDF 超过 20 页会直接失败。

## 单案例执行

在本目录打开 PowerShell：

```powershell
.\Invoke-CargoPlusFileTest.ps1 `
  -BaseUrl "https://115.29.213.72:30010" `
  -CaseId "YY-001" `
  -FilePath "C:\CargoPlusTest\YY-001.pdf" `
  -RecognitionMode standard
```

同一封邮件及其附件可放在同一个任务中：

```powershell
.\Invoke-CargoPlusFileTest.ps1 `
  -BaseUrl "https://115.29.213.72:30010" `
  -CaseId "YY-002" `
  -FilePath @("C:\CargoPlusTest\mail.eml", "C:\CargoPlusTest\attachment.pdf") `
  -RecognitionMode standard
```

扫描件或复杂版面使用高精度模式：

```powershell
.\Invoke-CargoPlusFileTest.ps1 `
  -BaseUrl "https://115.29.213.72:30010" `
  -CaseId "YY-003" `
  -FilePath "C:\CargoPlusTest\YY-003-scan.pdf" `
  -RecognitionMode high_accuracy
```

脚本默认每 3 秒查询一次，最长等待 10 分钟。输出保存在当前目录下的 `results/<案例编号>/<模式>/r<次数>/`，包括：

- `request-metadata.json`：请求信息，不包含 API Key。
- `submit-response.json`：提交响应。
- `task-result.json`：最终任务状态及抽取结果。
- `extracted-result.json`：成功时的 57 字段结果。

## 幂等与重试

- 默认幂等键为 `<案例编号>-<模式>-r<次数>`。
- 网络中断且不确定任务是否创建时，使用原命令重跑，脚本会复用同一幂等键。
- 已取得 `task_id` 后，不要重新上传，只查询原任务。
- 确认需要重新调用模型时，将 `-Attempt` 改为 `1`；同一案例最多主动复测一次。
- standard 与 high_accuracy 必须分别运行，不能共用幂等键。

## 提交纠错反馈

先将完整标准答案模板复制为案例文件并修改。业务人员确认后执行：

```powershell
.\Submit-CargoPlusFeedback.ps1 `
  -BaseUrl "https://115.29.213.72:30010" `
  -TaskId "任务编号" `
  -CorrectedJsonPath ".\YY-001-corrected.json" `
  -Notes "YY-001：BookingNo 原文位于第一页右上角" `
  -BusinessConfirmed
```

`corrected_result` 可以只包含确认需要修改的字段。基线测试完成前仅在 Excel 台账登记差异，不审核采纳反馈。

## 建议执行节奏

- 9 月 7 日：5 个案例冒烟测试，单并发开始，确认稳定后升至 2 并发。
- 9 月 8 日：运行约一半案例，并发不超过 3。
- 9 月 9 日：完成剩余基线，确认业务口径和关键字段。
- 9 月 10 日：复测失败案例，并完成 8～12 个 standard/high_accuracy 对比。
- 9 月 11 日：冻结版本回归并输出结论。

不要一次性提交全部案例。建议每批 5 个，上一批状态明确后再提交下一批。
