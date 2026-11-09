import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import disclosure_earnings_alert as alert

MAIN = """<span onclick="openCorpInfoNew('00120571', 'winCorpInfo', '/dsae001/selectPopup.ax');">롯데칠성음료</span>"""
POPUP = """<table>
<tr><th>공시회사명</th><td>롯데칠성음료</td></tr>
<tr><th>종목코드</th><td>005300</td></tr>
</table>"""


class CompanyResolutionTest(unittest.TestCase):
    def test_dart_code_has_priority_and_preserves_leading_zeroes(self):
        with patch.object(alert, '_fetch', return_value=POPUP) as fetch, \
                patch.object(alert, 'find_stock_code') as local:
            self.assertEqual(alert.resolve_stock_code('롯데칠성음료', MAIN, Mock()), '005300')
            self.assertIn('selectKey=00120571', fetch.call_args.args[1])
            local.assert_not_called()

    def test_missing_popup_or_network_failure_uses_existing_mapping(self):
        for source, failure in [('', None), (MAIN, TimeoutError('timeout'))]:
            with self.subTest(source=source), patch.object(alert, '_fetch', side_effect=failure), \
                    patch.object(alert, 'find_stock_code', return_value='005300'):
                self.assertEqual(alert.resolve_stock_code('롯데칠성음료', source, Mock()), '005300')

    def test_wrong_company_or_ambiguous_popup_is_not_trusted(self):
        for source in (POPUP.replace('롯데칠성음료', '다른기업'),
                       POPUP.replace('005300', 'unknown'),
                       POPUP.replace('</table>', '<tr><th>종목코드</th><td>123456</td></tr></table>')):
            with self.subTest(source=source), patch.object(alert, '_fetch', return_value=source):
                with self.assertRaises(ValueError):
                    alert.stock_code_from_dart('롯데칠성음료', MAIN, Mock())
        with patch.object(alert, '_fetch') as fetch:
            self.assertIsNone(alert.stock_code_from_dart('롯데칠성음료', MAIN + MAIN.replace('00120571', '00123456'), Mock()))
            fetch.assert_not_called()

    def test_explicit_alias_fixes_name_difference_without_changing_keyword_or_map(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'company_stock_map.json'
            original = json.dumps({'companies': [{'name': '롯데칠성', 'code': '005300'}]})
            path.write_text(original)
            with patch.object(alert, 'MAP_FILE', path):
                self.assertEqual(alert.resolve_stock_code('롯데칠성음료', '', Mock()), '005300')
                self.assertEqual(alert.resolve_stock_code('롯데칠성', '', Mock()), '005300')
                self.assertEqual(path.read_text(), original)
                for company in ('롯데', '롯데칠성음료우', '롯데칠성음료홀딩스'):
                    with self.subTest(company=company), self.assertRaises(ValueError):
                        alert.resolve_stock_code(company, '', Mock())

    def test_alias_works_without_map_and_invalid_aliases_are_rejected(self):
        with patch.object(alert, 'find_stock_code', side_effect=FileNotFoundError):
            self.assertEqual(alert.resolve_stock_code('롯데칠성음료', '', Mock()), '005300')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'aliases.json'
            for aliases in ({'롯데칠성음료': '5300'}, {'롯데칠성음료': '005300', '롯데 칠성음료': '123456'}):
                path.write_text(json.dumps(aliases))
                with self.assertRaises(ValueError):
                    alert.find_alias_stock_code('롯데칠성음료', path)
