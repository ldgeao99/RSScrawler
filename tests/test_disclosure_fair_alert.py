import unittest
from pathlib import Path
from unittest.mock import patch

import disclosure_earnings_alert as alert

BODY = (Path(__file__).parent / 'fixtures' / 'fair' / 'dart_body.html').read_text(encoding='utf-8')
ITEM = {'title': '(유가)동아지질 - 수시공시의무관련사항(공정공시)',
        'link': 'https://dart.fss.or.kr/api/link.jsp?rcpNo=20261110800167'}


class FairDisclosureTitleTest(unittest.TestCase):
    def test_detects_only_fair_disclosure_titles(self):
        self.assertTrue(alert.is_fair_disclosure(ITEM['title']))
        self.assertTrue(alert.is_fair_disclosure('(코)A사 - 수시공시의무 관련사항(공정공시)'))
        self.assertFalse(alert.is_fair_disclosure('(유가)하나투어 - 연결재무제표기준영업(잠정)실적(공정공시)'))

    def test_parses_disclosure_title_from_body(self):
        self.assertEqual(alert.parse_fair_disclosure_title(BODY), '2026년 결산배당 및 자기주식 취득·소각 계획')
        with self.assertRaises(ValueError):
            alert.parse_fair_disclosure_title(BODY.replace('공시제목', '제목없음'))

    def test_fetch_returns_title_and_falls_back_to_none(self):
        with patch.object(alert, 'fetch_dart_document', return_value=('', BODY)) as fetch:
            self.assertEqual(alert.fetch_fair_disclosure_title(ITEM), '2026년 결산배당 및 자기주식 취득·소각 계획')
            self.assertEqual(fetch.call_args.args[1], '20261110800167')
        with patch.object(alert, 'fetch_dart_document', side_effect=OSError('down')):
            self.assertIsNone(alert.fetch_fair_disclosure_title(ITEM))
        self.assertIsNone(alert.fetch_fair_disclosure_title({**ITEM, 'link': 'https://dart.fss.or.kr/'}))
        self.assertIsNone(alert.fetch_fair_disclosure_title({**ITEM, 'title': '(유가)A - 주요사항보고서'}))


if __name__ == '__main__':
    unittest.main()
