import importlib.util
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location(
    'company_sync_under_test', Path(__file__).resolve().parents[1] / 'company_list_sync_scheduler.py')
sync = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'FinanceDataReader': types.SimpleNamespace(StockListing=Mock())}):
    spec.loader.exec_module(sync)


class CompanyStockMapTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'company_stock_map.json'
        patcher = patch.object(sync, 'COMPANY_STOCK_MAP_FILE', self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def listing(self, rows):
        frame = Mock()
        frame.to_dict.return_value = rows
        return patch.object(sync.fdr, 'StockListing', return_value=frame)

    def test_sync_preserves_keyword_strings_custom_names_and_other_categories(self):
        names = ['삼성전자', '사용자 별칭']
        keywords = {'company_kr': names, 'etc': ['속보']}
        save = Mock()
        with self.listing([{'Code': '005930', 'Name': '삼성전자'},
                           {'Code': '0013Y0', 'Name': '신규기업'},
                           {'Code': '0013Y0', 'Name': '신규기업'}]):
            self.assertEqual(sync.sync_company_kr_keywords(keywords, threading.Lock(), save), 1)
        self.assertIs(keywords['company_kr'], names)
        self.assertEqual(keywords, {'company_kr': ['삼성전자', '사용자 별칭', '신규기업'], 'etc': ['속보']})
        save.assert_called_once_with(keywords)
        self.assertEqual(json.loads(self.path.read_text())['companies'],
                         [{'code': '005930', 'name': '삼성전자'}, {'code': '0013Y0', 'name': '신규기업'}])

    def test_mapping_refreshes_even_when_no_keyword_is_added(self):
        self.path.write_text('old mapping')
        save = Mock()
        with self.listing([{'Code': 5930, 'Name': '삼성전자'}]):
            self.assertEqual(sync.sync_company_kr_keywords({'company_kr': ['삼성전자']}, threading.Lock(), save), 0)
        save.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text())['companies'][0]['code'], '005930')

    def test_empty_or_invalid_listing_does_not_replace_existing_data(self):
        self.path.write_text('previous')
        for rows in ([], [{'Code': None, 'Name': '기업'}],
                     [{'Name': '기업'}], [{'Code': '1234567', 'Name': '기업'}],
                     [{'Code': '005930', 'Name': '기업1'}, {'Code': '005930', 'Name': '기업2'}]):
            with self.subTest(rows=rows), self.listing(rows):
                keywords, save = {'company_kr': ['기존기업']}, Mock()
                if rows:
                    with self.assertRaises(ValueError):
                        sync.sync_company_kr_keywords(keywords, threading.Lock(), save)
                else:
                    sync.sync_company_kr_keywords(keywords, threading.Lock(), save)
                self.assertEqual(self.path.read_text(), 'previous')
                self.assertEqual(keywords, {'company_kr': ['기존기업']})
                save.assert_not_called()

    def test_replace_failure_keeps_previous_mapping_and_removes_temporary_file(self):
        self.path.write_text('previous')
        with patch.object(sync.os, 'replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                sync.save_company_stock_map([{'code': '005930', 'name': '삼성전자'}])
        self.assertEqual(self.path.read_text(), 'previous')
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])
