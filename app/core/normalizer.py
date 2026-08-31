import json
import logging
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set
from app.config import settings

logger = logging.getLogger(__name__)

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
CONTACT_LABEL_RE = re.compile(r'(?i)(?<![A-Z])(TEL|PHONE|MOBILE|MOB|电话|FAX|传真|EMAIL|邮箱)(?![A-Z])\s*[:：]?')
PHONE_CONTINUATION_RE = re.compile(r'^[+()0-9][+()0-9\s./\-]{2,29}$')
STOP_CONTINUATION_RE = re.compile(r'(?i)^\s*(CONTACT|ATTN|EMAIL|邮箱|TEL|PHONE|MOBILE|MOB|电话|FAX|传真|ADD|ADDRESS|ZIP|VAT|PIC|联系人)\b')
NUMBER_UNIT_RE = re.compile(r'^\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([A-Za-z㐀-鿿][A-Za-z0-9㐀-鿿()/ .\-]*)?\s*$')
CJK_RE = re.compile(r'[㐀-鿿]')
UNLOCODE_RE = re.compile(r'^[A-Z]{2}[A-Z0-9]{3}$')
SHORT_PORT_CODE_RE = re.compile(r'^[A-Z0-9]{2,4}$')
ISO_ALPHA2 = set(
    'AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BW BY BZ '
    'CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM '
    'FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT '
    'JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN '
    'MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT '
    'PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL '
    'TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW'.split()
)
KG_UNITS = {'KG', 'KGS', 'KILOGRAM', 'KILOGRAMS', '公斤', '千克'}
TON_UNITS = {'T', 'TON', 'TONS', 'TONNE', 'TONNES', 'MT', 'MTS', 'METRICTON', 'METRICTONS', '吨'}


class CargoNormalizer:
    def __init__(self, reference_path: Optional[str] = None):
        if reference_path is None:
            ref_file = settings.skill_path / "references" / "container-code-table.json"
        else:
            ref_file = Path(reference_path)
            
        if ref_file.exists():
            try:
                self.reference = json.loads(ref_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"Failed to load reference table from {ref_file}: {e}")
                self.reference = {}
        else:
            logger.warning(f"Reference table not found at {ref_file}, using defaults.")
            self.reference = {}

    @staticmethod
    def _as_string(value: Any) -> str:
        if value is None:
            return ''
        if isinstance(value, str):
            return value
        return str(value)

    def _ordered_top(self, draft: Any) -> Dict[str, Any]:
        result = {}
        source = draft if isinstance(draft, dict) else {}
        for field in TOP_LEVEL_FIELDS:
            if field == 'ContainerInfo':
                containers = source.get('ContainerInfo', [])
                result[field] = containers if isinstance(containers, list) else []
            else:
                result[field] = self._as_string(source.get(field, ''))
        return result

    def _ordered_container(self, item: Any) -> Dict[str, str]:
        source = item if isinstance(item, dict) else {}
        return {field: self._as_string(source.get(field, '')) for field in CONTAINER_FIELDS}

    def _normalized_line(self, value: Any) -> str:
        return re.sub(r'\s+', ' ', self._as_string(value).strip()).casefold()

    def _strip_party_name_from_address(self, name: str, address: str) -> str:
        name = self._as_string(name)
        address = self._as_string(address)
        if not name or not address:
            return address
        lines = address.splitlines(keepends=True)
        if not lines or self._normalized_line(lines[0]) != self._normalized_line(name):
            return address
        return ''.join(lines[1:])

    def _merge_contact_continuations(self, text: str) -> str:
        lines = self._as_string(text).splitlines()
        merged = []
        i = 0
        while i < len(lines):
            line = lines[i].rstrip()
            while i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if not next_line or STOP_CONTINUATION_RE.match(next_line) or not PHONE_CONTINUATION_RE.match(next_line):
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

    def _clean_contact_value(self, value: str) -> str:
        kept_lines = []
        for line in self._as_string(value).splitlines():
            stripped = line.strip()
            if kept_lines and STOP_CONTINUATION_RE.match(stripped):
                break
            kept_lines.append(stripped)
        return re.sub(r'\s+', ' ', ' '.join(kept_lines)).strip(' ;,')

    def _extract_labelled_contacts(self, text: str) -> Dict[str, List[str]]:
        scan_text = self._merge_contact_continuations(text)
        contacts = {'tel': [], 'fax': [], 'email': []}
        labels = list(CONTACT_LABEL_RE.finditer(scan_text))
        for index, match in enumerate(labels):
            label = match.group(1).upper()
            start = match.end()
            end = labels[index + 1].start() if index + 1 < len(labels) else len(scan_text)
            value = self._clean_contact_value(scan_text[start:end])
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

    def _extract_contacts(self, text: str) -> Dict[str, str]:
        text = self._as_string(text)
        labelled = self._extract_labelled_contacts(text)
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

    def _split_number_unit(self, value: str) -> Tuple[str, str]:
        value = self._as_string(value).strip()
        match = NUMBER_UNIT_RE.match(value)
        if not match:
            return value.replace(',', ''), ''
        number = match.group(1).replace(',', '')
        unit = (match.group(2) or '').strip()
        return number, unit

    def _normalize_weight(self, number: str, unit: str) -> Tuple[str, str]:
        number = self._as_string(number).strip().replace(',', '')
        unit = self._as_string(unit).strip()
        canonical_unit = re.sub(r'[\s().]', '', unit).upper()
        if canonical_unit in KG_UNITS:
            return number, 'KGS'
        if canonical_unit not in TON_UNITS:
            return number, unit
        try:
            kilograms = Decimal(number) * Decimal('1000')
        except (InvalidOperation, ValueError):
            return number, unit
        text = format(kilograms, 'f')
        if '.' in text:
            text = text.rstrip('0').rstrip('.')
        return text or '0', 'KGS'

    def _normalize_port_pair(self, code: str, name: str) -> Tuple[str, str]:
        raw_code = self._as_string(code).strip()
        raw_name = self._as_string(name).strip()
        if not raw_code:
            return '', raw_name

        compact = re.sub(r'[\s\-/]', '', raw_code).upper()
        if UNLOCODE_RE.fullmatch(compact) and compact[:2] in ISO_ALPHA2:
            return compact, raw_name

        # Preserve short code-like values because the normalizer cannot inspect
        # the original label and therefore cannot distinguish customer/IATA-like
        # codes from short place names with sufficient confidence.
        if SHORT_PORT_CODE_RE.fullmatch(compact):
            return compact, raw_name

        # Longer non-UN/LOCODE values are clear place-name candidates, such as
        # POL: YANTIAN or POD: MELBOURNE. Move them only when no name was already
        # extracted; otherwise discard the invalid code-field duplicate.
        if not raw_name:
            return '', raw_code
        return '', raw_name

    def _has_cjk(self, value: str) -> bool:
        return bool(CJK_RE.search(self._as_string(value)))

    def _split_goods_name(self, en_value: str, cn_value: str) -> Tuple[str, str]:
        en_value = self._as_string(en_value).strip()
        cn_value = self._as_string(cn_value).strip()
        if en_value and cn_value:
            return en_value, cn_value
        value = en_value or cn_value
        if not value:
            return '', ''
        if not self._has_cjk(value):
            return value, cn_value
        parts = re.findall(r'[㐀-鿿]+|[^㐀-鿿]+', value)
        en_parts = []
        cn_parts = []
        for part in parts:
            part = part.strip(' ,;:/|')
            if not part:
                continue
            if self._has_cjk(part):
                cn_parts.append(part)
            else:
                en_parts.append(part)
        return ' '.join(en_parts).strip(), ' '.join(cn_parts).strip()

    def _reference_values(self, category: str) -> Set[str]:
        values = set()
        for item in (self.reference or {}).get(category, []):
            for key in ('value', 'code', 'cn', 'en'):
                value = self._as_string(item.get(key, '')).strip().upper().replace("'", '')
                if value:
                    values.add(value)
        return values

    def _goods_package_lookup(self) -> Dict[str, str]:
        lookup = {}
        for item in (self.reference or {}).get('GoodsPackage', []):
            code = self._as_string(item.get('code', '')).strip().upper()
            if not code:
                continue
            values = []
            for key in ('code', 'value', 'cn', 'en'):
                value = self._as_string(item.get(key, '')).strip()
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

    def _normalize_goods_package(self, value: str) -> str:
        raw = self._as_string(value).strip()
        if not raw:
            return ''
        lookup = self._goods_package_lookup()
        candidates = [raw, raw.upper(), raw.casefold(), re.sub(r'\s+', ' ', raw).strip(), re.sub(r'\s+', ' ', raw).strip().upper(), re.sub(r'\s+', ' ', raw).strip().casefold()]
        for candidate in candidates:
            key = self._as_string(candidate).strip()
            if key in lookup:
                return lookup[key]
        return raw

    def _normalize_container_type(self, raw_size: str, raw_type: str) -> Tuple[str, str, str]:
        sizes = self._reference_values('CONT_SIZE') or {'20', '40', '45', 'LCL'}
        types = self._reference_values('CONT_TYPE') or {'GP', 'HQ', 'FR', 'NOR', 'OT', 'RF'}
        clean = lambda value: self._as_string(value).strip().upper().replace(' ', '').replace("'", '').replace('-', '').replace('/', '')
        explicit_size = clean(raw_size)
        explicit_type = clean(raw_type)
        type_aliases = {'HC': 'HQ'}
        explicit_type = type_aliases.get(explicit_type, explicit_type)
        candidates = list(dict.fromkeys(filter(None, [explicit_size, explicit_type, explicit_size + explicit_type])))
        if not candidates:
            return '', '', ''

        if explicit_size in sizes and explicit_type in types:
            return explicit_size, explicit_type, ''
        for candidate in candidates:
            if candidate in sizes:
                return candidate, explicit_type if explicit_type in types else '', ''
            for size in sorted(sizes, key=len, reverse=True):
                if not size or not candidate.startswith(size):
                    continue
                type_part = type_aliases.get(candidate[len(size):], candidate[len(size):])
                if type_part in types:
                    return size, type_part, ''
        return '', '', explicit_size + explicit_type

    def _append_remark(self, existing: str, message: str) -> str:
        existing = self._as_string(existing).strip()
        if not message:
            return existing
        if message in existing:
            return existing
        if existing:
            return existing + '; ' + message
        return message

    def _normalize_goods_type(self, value: str) -> str:
        raw = self._as_string(value).strip()
        if raw in {'S', 'R', 'D', 'O'}:
            return raw
        upper = raw.upper()
        if not raw:
            return 'S'
        if any(token in upper for token in ['REEFER', '冷冻', '冷藏']):
            return 'R'
        if any(token in upper for token in ['DANGEROUS', 'HAZARDOUS', '危险品']) or re.search(r'\bDG\b', upper):
            return 'D'
        if any(token in upper for token in ['EXCEED', 'OVER DIMENSION', '超标', '超限']) or re.search(r'\b(?:ODC|OOG)\b', upper):
            return 'O'
        if any(token in upper for token in ['GENERAL', 'NORMAL', '普货']):
            return 'S'
        return 'S'

    def normalize(self, draft: Dict[str, Any]) -> Dict[str, Any]:
        """Runs the complete V3 normalization pipeline on a draft JSON."""
        result = self._ordered_top(draft)

        for code_field, name_field in [
            ('POR', 'PORName'),
            ('POL', 'POLName'),
            ('POD', 'PODName'),
            ('DeliveryCode', 'DeliveryName'),
        ]:
            result[code_field], result[name_field] = self._normalize_port_pair(
                result[code_field], result[name_field]
            )

        for party in ('Shipper', 'Consignee', 'Notify'):
            addr_field = f'{party}Addr'
            contacts = self._extract_contacts(result[addr_field])
            result[addr_field] = self._strip_party_name_from_address(result[f'{party}Name'], result[addr_field])
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
            number, unit = self._split_number_unit(result[value_field])
            result[value_field] = number
            if not result[unit_field]:
                result[unit_field] = unit
            if value_field in {'GrossWeight', 'NetWeight'}:
                result[value_field], result[unit_field] = self._normalize_weight(
                    result[value_field], result[unit_field]
                )
        result['PackagesUnit'] = self._normalize_goods_package(result['PackagesUnit'])

        result['GoodsName'], result['GoodsNameCN'] = self._split_goods_name(result['GoodsName'], result['GoodsNameCN'])
        result['GoodsType'] = self._normalize_goods_type(result['GoodsType'])

        normalized_containers = []
        for raw_item in result.get('ContainerInfo', []):
            item = self._ordered_container(raw_item)
            size, cont_type, unmatched = self._normalize_container_type(item['ContSize'], item['ContType'])
            item['ContSize'] = size
            item['ContType'] = cont_type
            if unmatched:
                result['Remark'] = self._append_remark(result['Remark'], f'Unmatched container type: {unmatched}')

            number, unit = self._split_number_unit(item['KGS'])
            item['KGS'] = number
            if not item['KGSunit']:
                item['KGSunit'] = unit
            item['KGS'], item['KGSunit'] = self._normalize_weight(item['KGS'], item['KGSunit'])

            number, unit = self._split_number_unit(item['PCS'])
            item['PCS'] = number
            if not item['Package']:
                item['Package'] = unit
            item['Package'] = self._normalize_goods_package(item['Package'])

            number, unit = self._split_number_unit(item['CBM'])
            item['CBM'] = number
            if not item['CBMunit']:
                item['CBMunit'] = unit

            item['GoodsName'], item['GoodsNameCN'] = self._split_goods_name(item['GoodsName'], item['GoodsNameCN'])
            normalized_containers.append(item)
        result['ContainerInfo'] = normalized_containers
        return result


# Global default instance
default_normalizer = CargoNormalizer()
