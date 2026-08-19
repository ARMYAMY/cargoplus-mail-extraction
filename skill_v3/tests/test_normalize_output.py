import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / 'rules' / 'normalize_output.py'
spec = importlib.util.spec_from_file_location('normalize_output', MODULE_PATH)
normalizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(normalizer)


class NormalizeOutputTests(unittest.TestCase):
    def setUp(self):
        self.reference = {
            'CONT_SIZE': [
                {'code': "20'", 'value': '20', 'cn': "20'", 'en': "20'"},
                {'code': "40'", 'value': '40', 'cn': "40'", 'en': "40'"},
                {'code': "45'", 'value': '45', 'cn': "45'", 'en': "45'"},
                {'code': 'LCL', 'value': 'LCL', 'cn': 'LCL', 'en': 'LCL'},
            ],
            'CONT_TYPE': [
                {'code': 'GP', 'value': 'GP', 'cn': 'GP', 'en': 'GP'},
                {'code': 'HQ', 'value': 'HQ', 'cn': 'HQ', 'en': 'HQ'},
                {'code': 'FR', 'value': 'FR', 'cn': 'FR', 'en': 'FR'},
            ],
            'GoodsPackage': [
                {'code': 'PAG', 'value': 'PAG', 'cn': 'PACKAGES', 'en': 'PACKAGES'},
                {'code': 'CARTONS', 'value': 'CARTONS', 'cn': 'CARTONS', 'en': 'CARTONS'},
                {'code': 'CL', 'value': 'CL', 'cn': '卷', 'en': 'COILS'},
                {'code': 'WOODEN CASE', 'value': 'WOODEN CASE', 'cn': '木托', 'en': 'WOODEN CASE'},
            ],
        }

    def test_adds_v3_fields_and_removes_extra_fields(self):
        draft = {'ShipperName': 'ABC', 'extra': 'remove me', 'ContainerInfo': [{'ContainerNo': 'ABCU1234567'}]}
        result = normalizer.normalize_output(draft, self.reference)
        self.assertEqual(list(result.keys()), normalizer.TOP_LEVEL_FIELDS)
        self.assertNotIn('extra', result)
        self.assertEqual(list(result['ContainerInfo'][0].keys()), normalizer.CONTAINER_FIELDS)
        self.assertEqual(result['ShipperName'], 'ABC')
        self.assertEqual(result['ShipperTel'], '')

    def test_extracts_contacts_without_changing_address(self):
        draft = {
            'ShipperAddr': 'NO.1 ROAD\nTEL: +86 755 12345678\nFAX: 0755-88889999\nEMAIL: ops@example.com',
            'ContainerInfo': [],
        }
        result = normalizer.normalize_output(draft, self.reference)
        self.assertIn('TEL: +86 755 12345678', result['ShipperAddr'])
        self.assertEqual(result['ShipperTel'], '+86 755 12345678')
        self.assertEqual(result['ShipperFax'], '0755-88889999')
        self.assertEqual(result['ShipperEmail'], 'ops@example.com')

    def test_removes_first_line_party_name_from_address(self):
        draft = {
            'NotifyName': 'SARL SKY ZON ENERGY ALGERIA',
            'NotifyAddr': 'SARL SKY ZON ENERGY ALGERIA\n9 EME KM RN 5 CONSTANTINE ALGERIA\nPhone: +213 560 992 595 /\n00213 770 80 36 55\nEmail: MANAGER@SKYZON-ENERGY.COM',
            'ContainerInfo': [],
        }
        result = normalizer.normalize_output(draft, self.reference)
        self.assertEqual(
            result['NotifyAddr'],
            '9 EME KM RN 5 CONSTANTINE ALGERIA\nPhone: +213 560 992 595 /\n00213 770 80 36 55\nEmail: MANAGER@SKYZON-ENERGY.COM',
        )
        self.assertEqual(result['NotifyTel'], '+213 560 992 595 / 00213 770 80 36 55')
        self.assertEqual(result['NotifyEmail'], 'MANAGER@SKYZON-ENERGY.COM')

    def test_extracts_line_broken_contact_values(self):
        draft = {
            'ShipperAddr': 'TEL:+020 32102688 FAX:+020\n32102688\nCONTACT:MAGGIE\nEMAIL:MAGGIE_CAN@UIF.COM.HK',
            'ContainerInfo': [],
        }
        result = normalizer.normalize_output(draft, self.reference)
        self.assertEqual(result['ShipperTel'], '+020 32102688')
        self.assertEqual(result['ShipperFax'], '+020 32102688')
        self.assertEqual(result['ShipperEmail'], 'MAGGIE_CAN@UIF.COM.HK')
        self.assertIn('FAX:+020\n32102688', result['ShipperAddr'])

    def test_splits_top_level_numbers_and_units(self):
        draft = {
            'Packages': '1,501 PACKAGES',
            'GrossWeight': '9,170.000 KGS',
            'NetWeight': '8,900.50 KGS',
            'Volume': '68.000 CBM',
            'ContainerInfo': [],
        }
        result = normalizer.normalize_output(draft, self.reference)
        self.assertEqual(result['Packages'], '1501')
        self.assertEqual(result['PackagesUnit'], 'PAG')
        self.assertEqual(result['GrossWeight'], '9170.000')
        self.assertEqual(result['GrossWeightUnit'], 'KGS')
        self.assertEqual(result['NetWeight'], '8900.50')
        self.assertEqual(result['NetWeightUnit'], 'KGS')
        self.assertEqual(result['Volume'], '68.000')
        self.assertEqual(result['VolumeUnit'], 'CBM')

    def test_splits_container_fields_and_container_type(self):
        draft = {
            'PackagesUnit': 'Carton',
            'ContainerInfo': [{
                'ContainerNo': 'ABCU1234567',
                'ContSize': '40HQ',
                'KGS': '9,170.000 KGS',
                'PCS': '501 PACKAGES',
                'CBM': '68.000 CBM',
            }]
        }
        result = normalizer.normalize_output(draft, self.reference)
        item = result['ContainerInfo'][0]
        self.assertEqual(result['PackagesUnit'], 'CARTONS')
        self.assertEqual(item['ContSize'], '40')
        self.assertEqual(item['ContType'], 'HQ')
        self.assertEqual(item['KGS'], '9170.000')
        self.assertEqual(item['KGSunit'], 'KGS')
        self.assertEqual(item['PCS'], '501')
        self.assertEqual(item['Package'], 'PAG')
        self.assertEqual(item['CBM'], '68.000')
        self.assertEqual(item['CBMunit'], 'CBM')

    def test_splits_parenthesized_package_units(self):
        draft = {'ContainerInfo': [{'PCS': '701 CTN(S)'}]}
        result = normalizer.normalize_output(draft, self.reference)
        item = result['ContainerInfo'][0]
        self.assertEqual(item['PCS'], '701')
        self.assertEqual(item['Package'], 'CTN(S)')

    def test_maps_package_units_to_goods_package_codes_and_keeps_unknown(self):
        draft = {
            'PackagesUnit': 'Wooden Case',
            'ContainerInfo': [
                {'PCS': '10 CL'},
                {'PCS': '2 UNKNOWN'},
            ],
        }
        result = normalizer.normalize_output(draft, self.reference)
        self.assertEqual(result['PackagesUnit'], 'WOODEN CASE')
        self.assertEqual(result['ContainerInfo'][0]['Package'], 'CL')
        self.assertEqual(result['ContainerInfo'][1]['Package'], 'UNKNOWN')

    def test_unknown_container_type_is_blank_and_remarked(self):
        draft = {'ContainerInfo': [{'ContSize': '99XX'}], 'Remark': 'original remark'}
        result = normalizer.normalize_output(draft, self.reference)
        item = result['ContainerInfo'][0]
        self.assertEqual(item['ContSize'], '')
        self.assertEqual(item['ContType'], '')
        self.assertIn('original remark', result['Remark'])
        self.assertIn('Unmatched container type: 99XX', result['Remark'])

    def test_splits_chinese_and_english_goods_names(self):
        draft = {'GoodsName': 'DAILY NECESSITIES 日用品', 'ContainerInfo': [{'GoodsName': 'TOYS 玩具'}]}
        result = normalizer.normalize_output(draft, self.reference)
        self.assertEqual(result['GoodsName'], 'DAILY NECESSITIES')
        self.assertEqual(result['GoodsNameCN'], '日用品')
        self.assertEqual(result['ContainerInfo'][0]['GoodsName'], 'TOYS')
        self.assertEqual(result['ContainerInfo'][0]['GoodsNameCN'], '玩具')

    def test_goods_type_defaults_to_general_cargo_code(self):
        result = normalizer.normalize_output({'ContainerInfo': []}, self.reference)
        self.assertEqual(result['GoodsType'], 'S')

    def test_goods_type_maps_to_customer_codes(self):
        cases = [
            ('GENERAL', 'S'),
            ('普货', 'S'),
            ('General Cargo', 'S'),
            ('Reefer Cargo', 'R'),
            ('冷冻', 'R'),
            ('Dangerous Goods', 'D'),
            ('Hazardous Materials', 'D'),
            ('危险品', 'D'),
            ('Exceed standard', 'O'),
            ('Over Dimension Cargo (ODC)', 'O'),
            ('超标', 'O'),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                result = normalizer.normalize_output({'GoodsType': source, 'ContainerInfo': []}, self.reference)
                self.assertEqual(result['GoodsType'], expected)

    def test_cli_writes_expected_json(self):
        cases = ROOT / 'tests' / 'cases'
        draft_path = cases / 'sample-01-draft.json'
        expected_path = cases / 'sample-01-expected.json'
        reference_path = ROOT / 'references' / 'container-code-table.json'
        draft = json.loads(draft_path.read_text(encoding='utf-8'))
        reference = normalizer.load_reference(reference_path)
        result = normalizer.normalize_output(draft, reference)
        expected = json.loads(expected_path.read_text(encoding='utf-8'))
        self.assertEqual(result, expected)


if __name__ == '__main__':
    unittest.main()
