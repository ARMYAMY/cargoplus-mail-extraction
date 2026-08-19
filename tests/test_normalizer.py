import json
from pathlib import Path
from app.core.normalizer import CargoNormalizer, TOP_LEVEL_FIELDS, CONTAINER_FIELDS

SAMPLE_SKILL_TEST_CASE = Path(__file__).resolve().parent.parent.parent / "cargo-mail-extraction-skill-v3" / "tests" / "cases" / "sample-01-draft.json"
EXPECTED_OUTPUT_FILE = Path(__file__).resolve().parent.parent.parent / "cargo-mail-extraction-skill-v3" / "tests" / "cases" / "sample-01-expected.json"


def test_normalizer_loads_fields():
    normalizer = CargoNormalizer()
    assert isinstance(normalizer.reference, dict)
    assert len(TOP_LEVEL_FIELDS) == 57
    assert len(CONTAINER_FIELDS) == 13


def test_normalizer_against_skill_v3_test_case():
    if not SAMPLE_SKILL_TEST_CASE.exists():
        return

    draft = json.loads(SAMPLE_SKILL_TEST_CASE.read_text(encoding="utf-8"))
    normalizer = CargoNormalizer()
    result = normalizer.normalize(draft)

    assert len(result.keys()) == 57
    assert "ContainerInfo" in result
    assert isinstance(result["ContainerInfo"], list)

    if EXPECTED_OUTPUT_FILE.exists():
        expected = json.loads(EXPECTED_OUTPUT_FILE.read_text(encoding="utf-8"))
        for k, v in expected.items():
            if k == "ContainerInfo":
                assert len(result["ContainerInfo"]) == len(v)
            else:
                assert result[k] == v, f"Mismatch on field {k}: got {result[k]!r}, expected {v!r}"


def test_normalizer_contact_and_address_split():
    normalizer = CargoNormalizer()
    draft = {
        "ShipperName": "ABC EXPORT CO., LTD.",
        "ShipperAddr": "ABC EXPORT CO., LTD.\n123 HIGHWAY, GUANGZHOU\nTEL: +86 20 88889999\nEMAIL: ops@abc.com",
        "GoodsName": "FURNITURE 家具",
        "Packages": "1,500 CARTONS",
        "GrossWeight": "12,345.67 KGS",
        "Volume": "45.5 CBM",
        "GoodsType": "GENERAL CARGO",
        "ContainerInfo": [
            {
                "ContainerNo": "COSU1234567",
                "SealNo": "SL001",
                "ContSize": "40",
                "ContType": "HQ",
                "KGS": "12000.0 KGS",
                "PCS": "1500 CARTONS",
                "CBM": "45.5 CBM",
                "GoodsName": "FURNITURE 家具",
            }
        ]
    }
    res = normalizer.normalize(draft)

    assert res["ShipperName"] == "ABC EXPORT CO., LTD."
    assert "ABC EXPORT CO., LTD." not in res["ShipperAddr"].splitlines()[0]
    assert "+86 20 88889999" in res["ShipperTel"]
    assert "ops@abc.com" in res["ShipperEmail"]
    assert res["Packages"] == "1500"
    assert res["PackagesUnit"] == "CARTONS"
    assert res["GrossWeight"] == "12345.67"
    assert res["GrossWeightUnit"] == "KGS"
    assert res["GoodsType"] == "S"
    assert res["GoodsName"] == "FURNITURE"
    assert res["GoodsNameCN"] == "家具"
    assert res["ContainerInfo"][0]["GoodsName"] == "FURNITURE"
    assert res["ContainerInfo"][0]["GoodsNameCN"] == "家具"
    assert res["ContainerInfo"][0]["ContSize"] == "40"
    assert res["ContainerInfo"][0]["ContType"] == "HQ"


def test_normalizer_handles_combined_container_codes_and_chinese_units():
    normalizer = CargoNormalizer()
    result = normalizer.normalize(
        {
            "Packages": "500 箱",
            "GoodsType": "EDGE PROTECTOR",
            "ShipperAddr": "HOTEL ROAD 8",
            "ContainerInfo": [
                {"ContSize": "40HQ", "ContType": "HQ"},
                {"ContSize": "40", "ContType": "HC"},
            ],
        }
    )
    assert result["Packages"] == "500"
    assert result["PackagesUnit"] == "箱"
    assert result["GoodsType"] == "S"
    assert result["ShipperTel"] == ""
    assert result["ContainerInfo"][0]["ContSize"] == "40"
    assert result["ContainerInfo"][0]["ContType"] == "HQ"
    assert result["ContainerInfo"][1]["ContSize"] == "40"
    assert result["ContainerInfo"][1]["ContType"] == "HQ"
