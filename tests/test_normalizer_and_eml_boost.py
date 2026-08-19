import email
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from pathlib import Path
import pytest

from app.core.normalizer import CargoNormalizer
from app.core.parser.eml_parser import parse_eml, html_to_plain_text


def test_normalizer_edge_branches():
    norm = CargoNormalizer()

    # 1. _merge_contact_continuations with EMAIL / 邮箱 label
    text_email_label = "CONTACT INFO\nEMAIL: ops@cargo.com\n+86 13800000000"
    cleaned = norm._merge_contact_continuations(text_email_label)
    assert "ops@cargo.com" in cleaned

    # 2. _extract_labelled_contacts with empty trailing value
    text_empty_label = "TEL:   \nFAX: 021-12345678"
    contacts = norm._extract_labelled_contacts(text_empty_label)
    assert contacts["fax"] == ["021-12345678"]

    # 3. _split_goods_name with both EN and CN provided
    en_res, cn_res = norm._split_goods_name("SHOES", "鞋子")
    assert en_res == "SHOES"
    assert cn_res == "鞋子"

    # 4. _split_goods_name with purely EN value
    en_pure, cn_pure = norm._split_goods_name("ELECTRONICS", "")
    assert en_pure == "ELECTRONICS"
    assert cn_pure == ""

    # 5. _goods_package_lookup with item missing code
    norm_custom = CargoNormalizer()
    norm_custom.reference = {"GoodsPackage": [{"cn": "无编码箱子", "value": "BOX"}]}
    lookup = norm_custom._goods_package_lookup()
    assert lookup == {}


def test_eml_parser_max_attachments_and_empty_body(tmp_path):
    # 1. EML with max attachments exceeded (> 10)
    msg = MIMEMultipart()
    msg["Subject"] = "Many Attachments"
    msg["From"] = "sender@example.com"
    msg.attach(MIMEText("Body here", "plain"))

    for i in range(15):
        att = MIMEApplication(b"Fake PDF Content", _subtype="pdf")
        att.add_header("Content-Disposition", "attachment", filename=f"att_{i}.pdf")
        msg.attach(att)

    eml_file = tmp_path / "many_att.eml"
    eml_file.write_bytes(msg.as_bytes())

    subj, body, att_paths = parse_eml(eml_file, tmp_path)
    assert subj == "Many Attachments"
    assert len(att_paths) == 10  # Capped at MAX_EML_ATTACHMENTS (10)

    # 2. EML with no body parts at all
    empty_msg = MIMEMultipart()
    empty_msg["Subject"] = "No Body"
    eml_empty = tmp_path / "empty_body.eml"
    eml_empty.write_bytes(empty_msg.as_bytes())

    s_emp, b_emp, a_emp = parse_eml(eml_empty, tmp_path)
    assert s_emp == "No Body"
    assert b_emp == ""

    # 3. Corrupt EML file
    corrupt_file = tmp_path / "corrupt.eml"
    corrupt_file.write_bytes(b"\x00\xff\xfe\x00")
    # Will either parse with errors='replace' or trigger exception
    parse_eml(corrupt_file, tmp_path)
