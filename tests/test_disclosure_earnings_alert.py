import ast
import html
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import disclosure_earnings_alert as alert

FIXTURES = Path(__file__).parent / 'fixtures' / 'earnings'
MAIN = (FIXTURES / 'dart_main.html').read_text(encoding='utf-8')
BODY = (FIXTURES / 'dart_body.html').read_text(encoding='utf-8')
QUARTER = (FIXTURES / 'wise_quarter.html').read_text(encoding='utf-8')
LINK = 'https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261103800500'
ITEM = {'title': '(유가)하나투어 - 연결재무제표기준영업(잠정)실적(공정공시)', 'link': LINK}
WISE_MAIN = """
<title>하나투어 - 기업현황</title><p>[기준:2026.11.02]</p>
<table><tr><th>시가총액</th><td>4,306억원</td></tr></table>
<script>load({encparam: 'dynamic-value', id: 'dynamic-id'});</script>
"""


class EarningsAlertTest(unittest.TestCase):
    def earnings(self):
        return alert.parse_dart(MAIN, BODY)

    def test_dart_uses_quarter_not_cumulative_and_total_not_parent_income(self):
        earnings = self.earnings()
        self.assertEqual(earnings['company'], '하나투어')
        self.assertEqual(earnings['period'], (2026, 3))
        self.assertTrue(earnings['consolidated'])
        self.assertEqual(earnings['actual'], {
            'revenue': Decimal('1116.98'), 'operating_profit': Decimal('51.32'),
            'net_income': Decimal('81.82')})

    def test_units_missing_values_and_non_quarter_period(self):
        for unit, factor in [('억원', '1'), ('백만원', '.01'), ('천원', '.00001'), ('원', '.00000001')]:
            with self.subTest(unit=unit):
                earnings = alert.parse_dart(MAIN, BODY.replace('백만원', unit))
                self.assertEqual(earnings['actual']['revenue'], Decimal('111698') * Decimal(factor))
        missing = BODY.replace('8,182', '-')
        self.assertIsNone(alert.parse_dart(MAIN, missing)['actual']['net_income'])
        for source in (BODY.replace('2026-07-01', '2026-01-01'), BODY.replace('백만원', '달러')):
            with self.assertRaises(ValueError):
                alert.parse_dart(MAIN, source)

    def test_wise_estimates_precision_and_reporting_basis(self):
        history, consensus = alert.parse_wise_quarters(QUARTER, (2026, 3), True)
        self.assertEqual(history[(2026, 2)]['net_income'], Decimal('69.48'))
        self.assertEqual(history[(2025, 4)]['net_income'], Decimal('134.18'))
        self.assertNotIn((2026, 3), history)
        self.assertNotIn((2026, 4), history)
        self.assertEqual(consensus, {'revenue': Decimal('1239.00'),
                                    'operating_profit': Decimal('60.20'), 'net_income': Decimal('80.00')})
        with self.assertRaises(ValueError):
            alert.parse_wise_quarters(QUARTER, (2026, 3), False)
        _, missing = alert.parse_wise_quarters(QUARTER, (2027, 2), True)
        self.assertTrue(all(value is None for value in missing.values()))

    def test_mapping_preserves_zeroes_and_rejects_ambiguous_or_missing_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'map.json'
            path.write_text(json.dumps({'companies': [{'code': '039130', 'name': '하나투어'}]}))
            self.assertEqual(alert.find_stock_code('하나 투어', path), '039130')
            with self.assertRaises(ValueError):
                alert.find_stock_code('다른 기업', path)
            path.write_text(json.dumps({'companies': [
                {'code': '039130', 'name': '하나투어'}, {'code': '123456', 'name': '하나투어'}]}))
            with self.assertRaises(ValueError):
                alert.find_stock_code('하나투어', path)

    def test_wise_request_discovers_dynamic_parameters_and_uses_matching_basis(self):
        for consolidated, fin_type in [(True, '4'), (False, '3')]:
            source = QUARTER if consolidated else QUARTER.replace('IFRS연결', 'IFRS별도')
            earnings = {**self.earnings(), 'consolidated': consolidated}
            with patch.object(alert, '_fetch', side_effect=[WISE_MAIN, source]) as fetch:
                result = alert.fetch_wise(Mock(), '039130', earnings)
            params = parse_qs(urlparse(fetch.call_args_list[1].args[1]).query)
            self.assertEqual(params['fin_typ'], [fin_type])
            self.assertEqual(params['freq_typ'], ['Q'])
            self.assertEqual(params['encparam'], ['dynamic-value'])
            self.assertEqual(params['id'], ['dynamic-id'])
            self.assertEqual(result['market_cap'], Decimal(4306))
            self.assertEqual(result['market_date'], '2026.11.02')

    def test_complete_alert_and_partial_failure_paths(self):
        with patch.object(alert, '_fetch', side_effect=[MAIN, BODY, WISE_MAIN, QUARTER]), \
                patch.object(alert, 'find_stock_code', return_value='039130'):
            message = alert.build_earnings_alert(ITEM)
        for expected in ('시가총액: 4,306억', 'A039130', '1,239억 / -10%',
                         '60억 / -15%', '82억(예상치 : 80억 / +2%)',
                         '2026.2Q 1,154억 / 54억 / 69억',
                         '2025.4Q 1,752억 / 274억 / 134억', LINK):
            self.assertIn(expected, message)
        with patch.object(alert, '_fetch', side_effect=[MAIN, BODY]), \
                patch.object(alert, 'find_stock_code', side_effect=FileNotFoundError):
            message = alert.build_earnings_alert(ITEM)
            self.assertIn('순이익 : 82억', message)
            self.assertIn('예상치 : -', message)
        with patch.object(alert, '_fetch', side_effect=[MAIN, BODY]), \
                patch.object(alert, 'find_stock_code', return_value='039130'), \
                patch.object(alert, 'fetch_wise', side_effect=TimeoutError):
            self.assertIn('A039130', alert.build_earnings_alert(ITEM))
        with patch.object(alert, '_fetch', side_effect=TimeoutError):
            self.assertIsNone(alert.build_earnings_alert(ITEM))
        with patch.object(alert, '_fetch') as fetch:
            self.assertIsNone(alert.build_earnings_alert({'title': '결산실적공시예고', 'link': LINK}))
            self.assertIsNone(alert.build_earnings_alert({**ITEM, 'link': 'bad link'}))
            fetch.assert_not_called()

    def test_html_escaping_missing_history_and_negative_estimates(self):
        earnings = self.earnings()
        earnings['company'] = '기업<&>'
        message = alert.format_earnings(earnings, LINK)
        self.assertIn(html.escape('기업<&>'), message)
        self.assertIn('2025.4Q - / - / -', message)
        self.assertEqual(alert._surprise(Decimal(-10), Decimal(-5)), '')
        self.assertEqual(alert._surprise(Decimal(10), Decimal(0)), '')
        self.assertEqual(alert._amount(Decimal('-1.5')), '-2억')

    def test_telegram_dispatch_preserves_original_alert_on_failure_and_other_feeds(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'main_fastapi.py').read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == 'send_telegram_notification')
        request = Mock()
        request.post.return_value.status_code = 200
        builder = Mock(return_value='rich earnings')
        namespace = dict(TELEGRAM_BOT_TOKEN='test', TELEGRAM_CHAT_ID='test', html=html,
                         shorten_telegram_link=lambda link: link, SENTIMENT_EMOJI={},
                         build_earnings_alert=builder, requests=request, time=Mock(), logger=Mock())
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<dispatch>', 'exec'), namespace)
        send = namespace['send_telegram_notification']
        send([ITEM], is_disclosure=True)
        self.assertEqual(request.post.call_args.kwargs['json']['text'], 'rich earnings')
        builder.side_effect = RuntimeError('failed')
        send([ITEM], is_disclosure=True)
        self.assertIn(LINK, request.post.call_args.kwargs['json']['text'])
        builder.reset_mock()
        send([ITEM], is_global=True)
        builder.assert_not_called()
