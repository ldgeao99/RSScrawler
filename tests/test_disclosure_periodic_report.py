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
ANNUAL_MAIN = (FIXTURES / 'ieyeon_annual_main.html').read_text(encoding='utf-8')
ANNUAL_INCOME = (FIXTURES / 'ieyeon_annual_income.html').read_text(encoding='utf-8')
Q3_MAIN = (FIXTURES / 'ieyeon_q3_main.html').read_text(encoding='utf-8')
Q3_INCOME = (FIXTURES / 'ieyeon_q3_income.html').read_text(encoding='utf-8')
SEARCH = ('<table><tr><th>번호</th></tr><tr><td>1</td><td><a href="/dsaf001/main.do?rcpNo=20251114002999">'
          '분기보고서 (2025.09)</a></td></tr></table>')
ANNUAL_ITEM = {'title': '(유가)이연제약 - 사업보고서 (2025.12)',
               'link': 'https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260320001314'}
ITEM = {'title': '(코스닥)에스켐 - 분기보고서 (2026.09)',
        'link': 'https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261110000255'}


class PeriodicReportTest(unittest.TestCase):
    def test_detects_quarter_half_and_annual_reports(self):
        self.assertTrue(alert.is_periodic_report(ITEM['title']))
        self.assertTrue(alert.is_periodic_report('(유가)E1 - 반기보고서 (2026.06)'))
        self.assertTrue(alert.is_periodic_report('(유가)E1 - 사업보고서 (2025.12)'))
        self.assertFalse(alert.is_periodic_report('(유가)E1 - 감사보고서제출'))
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

    def test_revenue_line_named_with_other_income_is_recognized(self):
        source = (FIXTURES / 'lges_consolidated_income.html').read_text(encoding='utf-8')
        period, actual = alert.parse_income_statement(source)
        self.assertEqual(period, (2026, 3))
        self.assertEqual(actual, {'revenue': Decimal('96434.33'), 'operating_profit': Decimal('7560.05'),
                                  'net_income': Decimal('-2969.06')})
        revenue = alert._STATEMENT_METRICS[0][1]
        for label in ('매출원가', '매출총이익'):
            self.assertIsNone(revenue.match(label))

    def test_rejects_empty_or_foreign_currency_statements(self):
        with self.assertRaises(ValueError):
            alert.parse_income_statement(NO_CONSOLIDATED)
        with self.assertRaises(ValueError):
            alert.parse_income_statement(SEPARATE_INCOME.replace('(단위 : 원)', '(단위 : USD)'))

    def test_falls_back_to_separate_statement_when_consolidated_is_not_applicable(self):
        requested = []

        def fetch(opener, url, referer=None, data=None):
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
        def fetch(opener, url, referer=None, data=None):
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
        self.assertIn('2026.3Q 53억 / -5억 / -8억(-%)', message)
        self.assertEqual(alert._margin({'revenue': Decimal(100), 'operating_profit': Decimal('-0.01')}), '(-%)')
        self.assertEqual(alert._margin({'revenue': Decimal(100), 'operating_profit': Decimal(0)}), '(0.0%)')
        self.assertEqual(alert._margin({'revenue': None, 'operating_profit': Decimal(1)}), '(-)')
        self.assertEqual(alert._margin({'revenue': Decimal(0), 'operating_profit': Decimal(1)}), '(-)')


    def test_annual_report_reads_full_year_and_cumulative_columns(self):
        self.assertEqual(alert.parse_income_statement(ANNUAL_INCOME)[0], (2025, 4))
        self.assertEqual(alert.parse_income_statement(ANNUAL_INCOME)[1]['revenue'], Decimal('1459.28626140'))
        period, nine_months = alert.parse_income_statement(Q3_INCOME, cumulative=True)
        self.assertEqual(period, (2025, 3))
        self.assertEqual(nine_months['revenue'], Decimal('1120.32173078'))

    def test_annual_report_derives_fourth_quarter_from_third_quarter_cumulative(self):
        posted = []

        def fetch(opener, url, referer=None, data=None):
            query = parse_qs(urlparse(url).query)
            if 'detailSearch' in url:
                posted.append(parse_qs(data.decode()))
                return SEARCH
            if 'main.do' in url:
                return {'20260320001314': ANNUAL_MAIN, '20251114002999': Q3_MAIN}[query['rcpNo'][0]]
            if query.get('eleId') == ['21']:
                return {'20260320001314': ANNUAL_INCOME, '20251114002999': Q3_INCOME}[query['rcpNo'][0]]
            return ''

        with patch.object(alert, '_fetch', side_effect=fetch), \
                patch.object(alert, 'resolve_stock_code', side_effect=ValueError('offline')):
            records = {}
            message = alert.build_earnings_alert(ANNUAL_ITEM, on_record=records.update)
        self.assertEqual(posted[0]['textCrpCik'], ['00145598'])
        self.assertEqual(records['period'], '2025Q4')
        self.assertTrue(records['consolidated'])
        self.assertEqual(records['annual']['revenue'], Decimal('1459.28626140'))
        self.assertEqual(records['actual']['revenue'], Decimal('1459.28626140') - Decimal('1120.32173078'))
        self.assertIn('매출액 : 339억', message)
        self.assertIn('2025년 연간 : 1,459억 / -303억 / -296억', message)
        self.assertIn('※ 4Q 실적 = 사업보고서 연간 - 3분기보고서 누적', message)

    def test_annual_report_without_third_quarter_falls_back_to_original_alert(self):
        def fetch(opener, url, referer=None, data=None):
            if 'detailSearch' in url:
                return '<table></table>'
            if 'main.do' in url:
                return ANNUAL_MAIN
            return ANNUAL_INCOME if parse_qs(urlparse(url).query).get('eleId') == ['21'] else ''

        with patch.object(alert, '_fetch', side_effect=fetch):
            self.assertIsNone(alert.build_earnings_alert(ANNUAL_ITEM))


if __name__ == '__main__':
    unittest.main()
