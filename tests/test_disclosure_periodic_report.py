import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import disclosure_earnings_alert as alert

FIXTURES = Path(__file__).parent / 'fixtures' / 'periodic'
MAIN = (FIXTURES / 'escam_main.html').read_text(encoding='utf-8')
NO_CONSOLIDATED = (FIXTURES / 'escam_consolidated.html').read_text(encoding='utf-8')
SEPARATE_INCOME = (FIXTURES / 'escam_income.html').read_text(encoding='utf-8')
CONSOLIDATED_INCOME = (FIXTURES / 'bqai_consolidated_income.html').read_text(encoding='utf-8')
ITEM = {'title': '(코스닥)에스켐 - 분기보고서 (2026.09)',
        'link': 'https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261110000255'}


class PeriodicReportTest(unittest.TestCase):
    def test_detects_quarter_and_half_reports_only(self):
        self.assertTrue(alert.is_periodic_report(ITEM['title']))
        self.assertTrue(alert.is_periodic_report('(유가)E1 - 반기보고서 (2026.06)'))
        self.assertFalse(alert.is_periodic_report('(유가)E1 - 사업보고서 (2025.12)'))
        self.assertTrue(alert.is_earnings_report('(유가)하나투어 - 연결재무제표기준영업(잠정)실적(공정공시)'))

    def test_reads_three_month_column_not_cumulative(self):
        period, actual = alert.parse_income_statement(SEPARATE_INCOME)
        self.assertEqual(period, (2026, 3))
        self.assertEqual(actual, {'revenue': Decimal('53.49147169'),
                                  'operating_profit': Decimal('-5.48894110'),
                                  'net_income': Decimal('-8.43054055')})

    def test_consolidated_statement_uses_total_net_income_and_operating_revenue(self):
        period, actual = alert.parse_income_statement(CONSOLIDATED_INCOME)
        self.assertEqual(period, (2026, 3))
        self.assertEqual(actual, {'revenue': Decimal('44.16553408'),
                                  'operating_profit': Decimal('-1.55424842'),
                                  'net_income': Decimal('5.48147982')})

    def test_rejects_empty_or_foreign_currency_statements(self):
        with self.assertRaises(ValueError):
            alert.parse_income_statement(NO_CONSOLIDATED)
        with self.assertRaises(ValueError):
            alert.parse_income_statement(SEPARATE_INCOME.replace('(단위 : 원)', '(단위 : USD)'))

    def test_falls_back_to_separate_statement_when_consolidated_is_not_applicable(self):
        requested = []

        def fetch(opener, url, referer=None):
            element = parse_qs(urlparse(url).query).get('eleId', [''])[0]
            requested.append(element)
            return {'19': NO_CONSOLIDATED, '23': SEPARATE_INCOME}.get(element, '')

        with patch.object(alert, '_fetch', side_effect=fetch):
            earnings = alert.parse_periodic_report(ITEM, MAIN, None)
        self.assertEqual(requested, ['19', '23'])
        self.assertFalse(earnings['consolidated'])
        self.assertEqual(earnings['company'], '에스켐')
        self.assertEqual(earnings['report'], '분기보고서 (2026.09)')
        self.assertEqual(earnings['period'], (2026, 3))

    def test_alert_message_marks_separate_basis(self):
        def fetch(opener, url, referer=None):
            if 'main.do' in url:
                return MAIN
            element = parse_qs(urlparse(url).query).get('eleId', [''])[0]
            return {'19': NO_CONSOLIDATED, '23': SEPARATE_INCOME}.get(element, '')

        with patch.object(alert, '_fetch', side_effect=fetch), \
                patch.object(alert, 'resolve_stock_code', side_effect=ValueError('offline')):
            message = alert.build_earnings_alert(ITEM)
        self.assertTrue(message.startswith('에스켐 - 분기보고서 (2026.09)\n'))
        self.assertIn('매출액 : 53억', message)
        self.assertIn('최근 실적 추이(별도기준)', message)
        self.assertIn('2026.3Q 53억 / -5억 / -8억', message)


if __name__ == '__main__':
    unittest.main()
