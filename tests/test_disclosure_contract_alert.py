import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import disclosure_contract_alert as contract

FIXTURES = Path(__file__).parent / 'fixtures' / 'contract'
BODY = (FIXTURES / 'hufs_body.html').read_text(encoding='utf-8')
CORRECTED = (FIXTURES / 'hj_corrected_body.html').read_text(encoding='utf-8')
ITEM = {'title': '(유가)후성 - 단일판매ㆍ공급계약체결',
        'link': 'https://dart.fss.or.kr/api/link.jsp?rcpNo=20261111800752'}


class SupplyContractTest(unittest.TestCase):
    def test_detects_contract_titles_but_not_termination(self):
        self.assertTrue(contract.is_supply_contract(ITEM['title']))
        self.assertTrue(contract.is_supply_contract('(유가)HD현대 - [기재정정]단일판매ㆍ공급계약체결(자회사의주요경영사항)'))
        self.assertTrue(contract.is_supply_contract('(코)A - 단일판매·공급계약 체결'))
        self.assertFalse(contract.is_supply_contract('(코)A - 단일판매ㆍ공급계약해지'))

    def test_parses_contract_fields(self):
        self.assertEqual(contract.parse_supply_contract(BODY), {
            'amount': Decimal('71194900000'), 'sales_ratio': '15.10', 'counterparty': '글로벌 화학 회사',
            'start': '2027-01-01', 'end': '2031-12-31'})

    def test_corrected_filing_uses_amount_after_correction(self):
        parsed = contract.parse_supply_contract(CORRECTED)
        self.assertEqual(parsed['amount'], Decimal('89866138541'))
        self.assertEqual(parsed['counterparty'], '한국전력공사')

    def test_formats_amount_in_eok(self):
        self.assertEqual(contract.format_supply_contract(contract.parse_supply_contract(BODY)),
                         '* 계약금액 : 712억\n* 매출액대비 : 15.10%\n'
                         '* 계약상대 : 글로벌 화학 회사\n* 계약기간 : 2027-01-01~2031-12-31')
        self.assertEqual(contract._eok(Decimal('626700000000')), '6,267억')
        self.assertEqual(contract._eok(Decimal('530000000')), '5.3억')
        small = {'amount': Decimal('1'), 'sales_ratio': '-', 'counterparty': 'A&B', 'start': '-', 'end': None}
        self.assertEqual(contract.format_supply_contract(small).splitlines()[1:],
                         ['* 매출액대비 : -', '* 계약상대 : A&amp;B', '* 계약기간 : -~-'])

    def test_missing_form_or_fetch_failure_falls_back(self):
        with self.assertRaises(ValueError):
            contract.parse_supply_contract('<table><tr><td>정정일자</td><td>2026-11-10</td></tr></table>')
        with patch.object(contract, 'fetch_dart_document', return_value=('', BODY)):
            self.assertTrue(contract.fetch_supply_contract_summary(ITEM).startswith('* 계약금액 : 712억'))
        with patch.object(contract, 'fetch_dart_document', side_effect=OSError('down')):
            self.assertIsNone(contract.fetch_supply_contract_summary(ITEM))
        self.assertIsNone(contract.fetch_supply_contract_summary({**ITEM, 'title': '(유가)A - 분기보고서'}))


if __name__ == '__main__':
    unittest.main()
