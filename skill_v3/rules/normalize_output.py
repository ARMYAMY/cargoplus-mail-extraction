#!/usr/bin/env python3
import argparse
import json
import re
from pathlib import Path

TOP_LEVEL_FIELDS = [
    'ShipperName', 'ShipperAddr', 'ShipperTel', 'ShipperEmail', 'ShipperFax',
    'ConsigneeName', 'ConsigneeAddr', 'ConsigneeTel', 'ConsigneeEmail', 'ConsigneeFax',
    'NotifyName', 'NotifyAddr', 'NotifyTel', 'NotifyEmail', 'NotifyFax',
    'POR', 'PORName', 'POL', 'POLName', 'POD', 'PODName', 'TransPort',
    'DeliveryCode', 'DeliveryName', 'ETD', 'ETA', 'Vessel', 'Voyage',
    'CutOffDate', 'SICutOff', 'ContainerInfo', 'TotalContainerQty',
    'GoodsName', 'GoodsNameCN', 'Marks', 'HSCode', 'Packages', 'PackagesUnit',
    'GrossWeight', 'GrossWeightUnit', 'NetWeight', 'NetWeightUnit',
    'Volume', 'VolumeUnit', 'Incoterms', 'Movement', 'PackingMode', 'GoodsType',
    'FreightTerm', 'Carrier', 'IsTrucking', 'IsCustomsDeclare', 'ReleaseBLType',
    'BookingNo', 'BLNo', 'ContractNo', 'Remark'
]

CONTAINER_FIELDS = [
    'ContainerNo', 'SealNo', 'ContSize', 'ContType', 'KGS', 'KGSunit',
    'PCS', 'Package', 'CBM', 'CBMunit', 'HSCode', 'GoodsName', 'GoodsNameCN'
]

EMAIL_RE = re.compile(r'[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}', re.I)
CONTACT_LABEL_RE = re.compile(r'(?i)(TEL|PHONE|MOBILE|MOB|电话|FAX|传真|EMAIL|邮箱)\s*[:：]?')
PHONE_CONTINUATION_RE = re.compile(r'^[+()0-9][+()0-9\s./\-]{2,29}$')
STOP_CONTINUATION_RE = re.compile(r'(?i)^\s*(CONTACT|ATTN|EMAIL|邮箱|TEL|PHONE|MOBILE|MOB|电话|FAX|传真|ADD|ADDRESS|ZIP|VAT|PIC|联系人)\b')
NUMBER_UNIT_RE = re.compile(r'^\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([A-Za-z][A-Za-z0-9()/ .\-]*)?\s*$')
CJK_RE = re.compile(r'[㐀-鿿]')


def load_reference(path):
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _as_string(value):
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    return str(value)


def _ordered_top(draft):
    result = {}
    source = draft if isinstance(draft, dict) else {}
    for field in TOP_LEVEL_FIELDS:
        if field == 'ContainerInfo':
            containers = source.get('ContainerInfo', [])
            result[field] = containers if isinstance(containers, list) else []
        else:
            result[field] = _as_string(source.get(field, ''))
    return result


def _ordered_container(item):
    source = item if isinstance(item, dict) else {}
    return {field: _as_string(source.get(field, '')) for field in CONTAINER_FIELDS}


def _normalized_line(value):
    return re.sub(r'\\s+', ' ', _as_string(value).strip()).casefold()


def _strip_party_name_from_address(name, address):
    name = _as_string(name)
    address = _as_string(address)
    if not name or not address:
        return address
    lines = address.splitlines(keepends=True)
    if not lines or _normalized_line(lines[0]) != _normalized_line(name):
        return address
    return ''.join(lines[1:])


def _merge_contact_continuations(text):
    lines = _as_string(text).splitlines()
    merged = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        while i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            if not next_line:
                break
            if STOP_CONTINUATION_RE.match(next_line):
                break
            if not PHONE_CONTINUATION_RE.match(next_line):
                break
            labels = list(CONTACT_LABEL_RE.finditer(line))
            if not labels:
                break
            last_label = labels[-1].group(1).upper()
            if last_label in {'EMAIL', '邮箱'}:
                break
            line = line + ' ' + next_line
            i += 1
        merged.append(line)
        i += 1
    return '\n'.join(merged)


def _clean_contact_value(value):
    kept_lines = []
    for line in _as_string(value).splitlines():
        stripped = line.strip()
        if kept_lines and STOP_CONTINUATION_RE.match(stripped):
            break
        kept_lines.append(stripped)
    return re.sub(r'\s+', ' ', ' '.join(kept_lines)).strip(' ;,')


def _extract_labelled_contacts(text):
    scan_text = _merge_contact_continuations(text)
    contacts = {'tel': [], 'fax': [], 'email': []}
    labels = list(CONTACT_LABEL_RE.finditer(scan_text))
    for index, match in enumerate(labels):
        label = match.group(1).upper()
        start = match.end()
        end = labels[index + 1].start() if index + 1 < len(labels) else len(scan_text)
        value = _clean_contact_value(scan_text[start:end])
        if not value:
            continue
        if label in {'TEL', 'PHONE', 'MOBILE', 'MOB', '电话'}:
            contacts['tel'].append(value)
        elif label in {'FAX', '传真'}:
            contacts['fax'].append(value)
        elif label in {'EMAIL', '邮箱'}:
            emails = EMAIL_RE.findall(value)
            contacts['email'].extend(emails or [value])
    return contacts


def _extract_contacts(text):
    text = _as_string(text)
    labelled = _extract_labelled_contacts(text)
    labelled_emails = set(labelled['email'])
    for email in EMAIL_RE.findall(text):
        if email not in labelled_emails:
            labelled['email'].append(email)
            labelled_emails.add(email)
    return {
        'tel': '; '.join(labelled['tel']),
        'email': '; '.join(labelled['email']),
        'fax': '; '.join(labelled['fax']),
    }


def _split_number_unit(value):
    value = _as_string(value).strip()
    match = NUMBER_UNIT_RE.match(value)
    if not match:
        return value.replace(',', ''), ''
    number = match.group(1).replace(',', '')
    unit = (match.group(2) or '').strip()
    return number, unit


def _has_cjk(value):
    return bool(CJK_RE.search(_as_string(value)))


def _split_goods_name(en_value, cn_value):
    en_value = _as_string(en_value).strip()
    cn_value = _as_string(cn_value).strip()
    if en_value and cn_value:
        return en_value, cn_value
    value = en_value or cn_value
    if not value:
        return '', ''
    if not _has_cjk(value):
        return value, cn_value
    parts = re.findall(r'[㐀-鿿]+|[^㐀-鿿]+', value)
    en_parts = []
    cn_parts = []
    for part in parts:
        part = part.strip(' ,;:/|')
        if not part:
            continue
        if _has_cjk(part):
            cn_parts.append(part)
        else:
            en_parts.append(part)
    return ' '.join(en_parts).strip(), ' '.join(cn_parts).strip()


def _reference_values(reference, category):
    values = set()
    for item in (reference or {}).get(category, []):
        for key in ('value', 'code', 'cn', 'en'):
            value = _as_string(item.get(key, '')).strip().upper().replace("'", '')
            if value:
                values.add(value)
    return values


def _goods_package_lookup(reference):
    lookup = {}
    for item in (reference or {}).get('GoodsPackage', []):
        code = _as_string(item.get('code', '')).strip().upper()
        if not code:
            continue
        values = []
        for key in ('code', 'value', 'cn', 'en'):
            value = _as_string(item.get(key, '')).strip()
            if value:
                values.append(value)
        if code == 'CARTONS':
            values.append('Carton')
        if code == 'WOODEN CASE':
            values.append('木托')
        for value in values:
            lookup[value.casefold()] = code
            lookup[value.upper()] = code
            lookup[re.sub(r'\s+', ' ', value).strip().casefold()] = code
    return lookup


def _normalize_goods_package(value, reference):
    raw = _as_string(value).strip()
    if not raw:
        return ''
    lookup = _goods_package_lookup(reference)
    candidates = [raw, raw.upper(), raw.casefold(), re.sub(r'\s+', ' ', raw).strip(), re.sub(r'\s+', ' ', raw).strip().upper(), re.sub(r'\s+', ' ', raw).strip().casefold()]
    for candidate in candidates:
        key = _as_string(candidate).strip()
        if key in lookup:
            return lookup[key]
    return raw


def _normalize_container_type(raw_size, raw_type, reference):
    sizes = _reference_values(reference, 'CONT_SIZE') or {'20', '40', '45', 'LCL'}
    types = _reference_values(reference, 'CONT_TYPE') or {'GP', 'HQ', 'FR', 'NOR', 'OT', 'RF'}
    raw = (raw_size + raw_type).strip().upper().replace(' ', '').replace("'", '')
    raw = raw.replace('-', '').replace('/', '')
    if not raw:
        return '', '', ''
    if raw in sizes:
        return raw, '', ''
    size_match = None
    for size in sorted(sizes, key=len, reverse=True):
        if size and raw.startswith(size):
            size_match = size
            break
    if size_match:
        type_part = raw[len(size_match):]
        if not type_part and raw_type:
            type_part = raw_type.strip().upper().replace("'", '')
        if type_part in types:
            return size_match, type_part, ''
    explicit_size = raw_size.strip().upper().replace("'", '')
    explicit_type = raw_type.strip().upper().replace("'", '')
    if explicit_size in sizes and explicit_type in types:
        return explicit_size, explicit_type, ''
    return '', '', raw


def _append_remark(existing, message):
    existing = _as_string(existing).strip()
    if not message:
        return existing
    if message in existing:
        return existing
    if existing:
        return existing + '; ' + message
    return message


def _normalize_goods_type(value):
    raw = _as_string(value).strip()
    if raw in {'S', 'R', 'D', 'O'}:
        return raw
    upper = raw.upper()
    if not raw:
        return 'S'
    if any(token in upper for token in ['REEFER', '冷冻', '冷藏']):
        return 'R'
    if any(token in upper for token in ['DANGEROUS', 'HAZARDOUS', 'DG', '危险品']):
        return 'D'
    if any(token in upper for token in ['EXCEED', 'OVER DIMENSION', 'ODC', 'OOG', '超标', '超限']):
        return 'O'
    if any(token in upper for token in ['GENERAL', 'NORMAL', '普货']):
        return 'S'
    return 'S'


def normalize_output(draft, reference=None):
    result = _ordered_top(draft)

    for party in ('Shipper', 'Consignee', 'Notify'):
        addr_field = f'{party}Addr'
        contacts = _extract_contacts(result[addr_field])
        result[addr_field] = _strip_party_name_from_address(result[f'{party}Name'], result[addr_field])
        if not result[f'{party}Tel']:
            result[f'{party}Tel'] = contacts['tel']
        if not result[f'{party}Email']:
            result[f'{party}Email'] = contacts['email']
        if not result[f'{party}Fax']:
            result[f'{party}Fax'] = contacts['fax']

    for value_field, unit_field in [
        ('Packages', 'PackagesUnit'),
        ('GrossWeight', 'GrossWeightUnit'),
        ('NetWeight', 'NetWeightUnit'),
        ('Volume', 'VolumeUnit'),
    ]:
        number, unit = _split_number_unit(result[value_field])
        result[value_field] = number
        if not result[unit_field]:
            result[unit_field] = unit
    result['PackagesUnit'] = _normalize_goods_package(result['PackagesUnit'], reference or {})

    result['GoodsName'], result['GoodsNameCN'] = _split_goods_name(result['GoodsName'], result['GoodsNameCN'])
    result['GoodsType'] = _normalize_goods_type(result['GoodsType'])

    normalized_containers = []
    for raw_item in result['ContainerInfo']:
        item = _ordered_container(raw_item)
        size, cont_type, unmatched = _normalize_container_type(item['ContSize'], item['ContType'], reference or {})
        item['ContSize'] = size
        item['ContType'] = cont_type
        if unmatched:
            result['Remark'] = _append_remark(result['Remark'], f'Unmatched container type: {unmatched}')

        number, unit = _split_number_unit(item['KGS'])
        item['KGS'] = number
        if not item['KGSunit']:
            item['KGSunit'] = unit

        number, unit = _split_number_unit(item['PCS'])
        item['PCS'] = number
        if not item['Package']:
            item['Package'] = unit
        item['Package'] = _normalize_goods_package(item['Package'], reference or {})

        number, unit = _split_number_unit(item['CBM'])
        item['CBM'] = number
        if not item['CBMunit']:
            item['CBMunit'] = unit

        item['GoodsName'], item['GoodsNameCN'] = _split_goods_name(item['GoodsName'], item['GoodsNameCN'])
        normalized_containers.append(item)
    result['ContainerInfo'] = normalized_containers
    return result


def main():
    parser = argparse.ArgumentParser(description='Normalize CargoPlus V3 JSON output.')
    parser.add_argument('--input', required=True, help='Draft JSON path')
    parser.add_argument('--output', required=True, help='Final JSON path')
    parser.add_argument('--reference', default='', help='container-code-table.json path')
    args = parser.parse_args()
    draft = json.loads(Path(args.input).read_text(encoding='utf-8'))
    reference = load_reference(args.reference) if args.reference else {}
    result = normalize_output(draft, reference)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
