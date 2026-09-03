from pathlib import Path


MANUAL_PATH = Path(__file__).parents[1] / "app" / "static" / "manual.html"


def test_customer_manual_contains_current_integration_flow():
    content = MANUAL_PATH.read_text(encoding="utf-8")

    required = [
        "10. 完整文件调用示例",
        "/api/v1/extract/async/upload",
        "/api/v1/extract/async",
        "/api/v1/extract/sync",
        "/api/v1/tasks/$taskId",
        'recognition_mode=$recognitionMode',
        "high_accuracy",
        "ContainerInfo=[]",
        '模型返回空提取结果',
        "最大 50MB",
        "最大 100MB",
        "最多处理 20 页",
        "[guid]::NewGuid().ToString()",
        "Read-Host",
        "result_json",
        "error_message",
    ]
    for marker in required:
        assert marker in content


def test_customer_manual_does_not_restore_outdated_or_sensitive_examples():
    content = MANUAL_PATH.read_text(encoding="utf-8")

    forbidden = [
        "http://localhost:8000",
        "Skill V3",
        "cg_live_",
        "<code>SO</code>",
        "<code>ClosingDate</code>",
        "<code>TransitPort</code>",
        "<code>Shipper</code>",
        "<code>NotifyParty</code>",
    ]
    for marker in forbidden:
        assert marker not in content


def test_customer_manual_navigation_targets_exist():
    content = MANUAL_PATH.read_text(encoding="utf-8")
    anchors = [
        "quickstart",
        "auth",
        "file-upload",
        "json-async",
        "json-sync",
        "tasks-query",
        "webhook",
        "fields",
        "status-codes",
        "full-example",
    ]
    for anchor in anchors:
        assert f'href="#{anchor}"' in content
        assert f'id="{anchor}"' in content
