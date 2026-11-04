import ast
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

import disclosure_earnings_alert as alert
import disclosure_earnings_store as store


class EarningsStoreTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'disclosure_earnings.json'
        patcher = patch.object(store, 'STORE_FILE', self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.item = {'title': '기업 영업(잠정)실적', 'link': 'https://dart.fss.or.kr/api/link.jsp?rcpNo=20261103800500'}

    def test_message_and_precise_metrics_persist_across_delivery_updates(self):
        fields = {'company': '기업', 'code': '039130', 'period': '2026Q3',
                  'actual': {'net_income': Decimal('81.82')}}
        message = '🟢📢기업\n**최근 실적 추이**\n기업&lt;&amp;&gt;'
        store.record_notification(self.item, message, fields, 'pending')
        store.record_notification(self.item, message, fields, 'sent')
        records = json.loads(self.path.read_text())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['actual']['net_income'], '81.82')
        self.assertEqual(records[0]['message_html'], message)
        self.assertIn('기업<&>', records[0]['message_text'])
        self.assertIn('**최근 실적 추이**', records[0]['message_text'])
        self.assertEqual(records[0]['telegram_status'], 'sent')
        self.assertIn('sent_at', records[0])
        store.record_notification(self.item, message, {}, 'failed')
        self.assertNotIn('sent_at', store.list_notifications()['items'][0])

    def test_search_filter_pagination_and_missing_file(self):
        self.assertEqual(store.list_notifications()['total'], 0)
        for index in range(3):
            item = {**self.item, 'link': f'https://dart.fss.or.kr/api/link.jsp?rcpNo=2026110380050{index}'}
            store.record_notification(item, 'message', {'company': '기업' + str(index), 'code': '00000' + str(index)},
                                      'failed' if index == 1 else 'sent')
        self.assertEqual(store.list_notifications(q='000001')['filtered_total'], 1)
        self.assertEqual(store.list_notifications(q='기업1', status='sent')['filtered_total'], 0)
        self.assertEqual(store.list_notifications(status='failed')['filtered_total'], 1)
        day = store.list_notifications()['items'][0]['created_at'][:10]
        self.assertEqual(store.list_notifications(day=day)['filtered_total'], 3)
        self.assertEqual(store.list_notifications(day='2000-01-01')['filtered_total'], 0)
        first = store.list_notifications(limit=2)
        self.assertTrue(first['has_more'])
        second = store.list_notifications(offset=2, limit=2)
        self.assertEqual(len(second['items']), 1)
        self.assertFalse(second['has_more'])

    def test_concurrent_writes_do_not_lose_records(self):
        def save(index):
            item = {**self.item, 'link': f'https://dart.fss.or.kr/api/link.jsp?rcpNo=20261103800{index:03d}'}
            store.record_notification(item, 'message')
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(save, range(12)))
        self.assertEqual(store.list_notifications()['total'], 12)

    def test_corrupt_file_and_failed_atomic_replace_preserve_previous_data(self):
        self.path.write_text('broken')
        with self.assertLogs('news_logger', level='ERROR'):
            self.assertIsNone(store.record_notification(self.item, 'message'))
        self.assertEqual(self.path.read_text(), 'broken')
        self.path.write_text('[]')
        with patch.object(store.os, 'replace', side_effect=OSError('disk error')), \
                self.assertLogs('news_logger', level='ERROR'):
            self.assertIsNone(store.record_notification(self.item, 'message'))
        self.assertEqual(self.path.read_text(), '[]')
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_structured_record_uses_total_income_and_serializable_history(self):
        fixture = Path(__file__).parent / 'fixtures' / 'earnings'
        source = lambda name: (fixture / name).read_text(encoding='utf-8')
        received = {}
        with patch.object(alert, '_fetch', side_effect=[source('dart_main.html'), source('dart_body.html')]), \
                patch.object(alert, 'find_stock_code', side_effect=FileNotFoundError):
            message = alert.build_earnings_alert(self.item, on_record=received.update)
        self.assertEqual(received['actual']['net_income'], Decimal('81.82'))
        self.assertEqual(received['period'], '2026Q3')
        self.assertEqual(received['history'][0]['period'], '2026Q3')
        self.assertEqual(len(received['history']), 5)
        self.assertIsNotNone(store.record_notification(self.item, message, received, 'sent'))
        self.assertEqual(store.list_notifications()['items'][0]['actual']['net_income'], '81.82')

    def test_api_and_page_routes_use_same_store_and_auth_middleware(self):
        source = (Path(__file__).resolve().parents[1] / 'main_fastapi.py').read_text()
        tree = ast.parse(source)
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == 'get_disclosure_earnings')
        self.assertIn('/api/disclosure-earnings', ast.unparse(function.decorator_list[0]))
        function.decorator_list = []
        response = Mock(side_effect=lambda **kwargs: kwargs)
        namespace = dict(disclosure_earnings_store=store, JSONResponse=response)
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<route>', 'exec'), namespace)
        self.assertEqual(namespace['get_disclosure_earnings']()['content']['total'], 0)
        self.assertEqual(response.call_args.kwargs['headers']['Cache-Control'], 'no-store')
        # 새 경로는 기존 인증 미들웨어의 공개 경로(/login, /static)에 포함되지 않는다.
        middleware = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                          and node.name == 'auth_middleware')
        self.assertNotIn('disclosure-earnings', ast.unparse(middleware))
        page = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == 'get_disclosure_earnings_page')
        self.assertIn('base_info/disclosure_earnings.html', ast.unparse(page))

    def test_http_page_and_api_require_login_and_return_persisted_records(self):
        import urllib.parse
        from fastapi import FastAPI, Request
        from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
        from fastapi.security import HTTPBasicCredentials
        from fastapi.testclient import TestClient

        tree = ast.parse((Path(__file__).resolve().parents[1] / 'main_fastapi.py').read_text())
        selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in ('auth_middleware', 'get_disclosure_earnings', 'get_disclosure_earnings_page')]
        namespace = dict(app=FastAPI(), Request=Request, JSONResponse=JSONResponse,
                         RedirectResponse=RedirectResponse, FileResponse=FileResponse,
                         HTTPBasicCredentials=HTTPBasicCredentials, urllib=urllib,
                         SESSION_COOKIE_NAME='test_session',
                         verify_session_token=lambda token: token == 'authorized',
                         verify_basic_credentials=lambda credentials: False,
                         disclosure_earnings_store=store)
        exec(compile(ast.Module(body=selected, type_ignores=[]), '<routes>', 'exec'), namespace)
        with TestClient(namespace['app']) as client:
            self.assertEqual(client.get('/api/disclosure-earnings').status_code, 401)
            self.assertEqual(client.get('/disclosure-earnings', follow_redirects=False).status_code, 302)
            client.cookies.set('test_session', 'authorized')
            store.record_notification(self.item, '🟢📢실적', {'company': '기업', 'code': '039130'}, 'sent')
            response = client.get('/api/disclosure-earnings?q=039130')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['items'][0]['telegram_status'], 'sent')
            self.assertEqual(response.headers['cache-control'], 'no-store')
            response = client.get('/disclosure-earnings')
            self.assertEqual(response.status_code, 200)
            self.assertIn('실적 알림', response.text)
