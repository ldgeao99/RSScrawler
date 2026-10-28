"""AI/Firestore에 연결하지 않고 원본 입력과 KST 실행 경계를 검증한다."""
import ast
import asyncio
import json
import tempfile
import threading
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


def scheduler_namespace():
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'schedule_extraction_scheduler.py').read_text())
    functions = {'get_live_file_paths', 'get_file_paths_for_date_str', 'load_news_snapshot',
                 'daily_schedule_extraction_scheduler', 'main'}
    constants = {'SCHEDULE_EXTRACTION_RUN_HOUR', 'SCHEDULE_EXTRACTION_RUN_MINUTE'}
    nodes = [node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions
             or isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets)]
    namespace = dict(datetime=datetime, timezone=timezone, timedelta=timedelta,
                     asyncio=asyncio, json=json, nullcontext=nullcontext,
                     logger=Mock(), report_batch_run=Mock(), send_batch_telegram_notification=Mock())
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<scheduler>', 'exec'), namespace)
    return namespace


class ScheduleExtractionTimingTest(unittest.TestCase):
    def test_pipeline_live_input_filters_today_but_selected_backup_keeps_all(self):
        ns = scheduler_namespace()
        today = datetime(2026, 10, 28, tzinfo=timezone(timedelta(hours=9)))

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return today.astimezone(tz)

        ns['datetime'] = Clock
        items = [dict(title=name, timestamp=str((today + delta).timestamp()))
                 for name, delta in [('yesterday', timedelta(seconds=-1)),
                                     ('start', timedelta()),
                                     ('end', timedelta(hours=23, minutes=59, seconds=59)),
                                     ('tomorrow', timedelta(days=1))]]
        items += [{'title': 'unknown'}, {'timestamp': 'invalid'}, {'timestamp': 'nan'}]
        # AI 호출 직전에서 멈추고 실제 파이프라인에 전달한 뉴스를 검사한다.
        ns['process_duplicate_purge_pipeline'] = Mock(side_effect=RuntimeError('stop before AI'))
        ns['get_live_file_paths'] = Mock(return_value=('live.json', 'output.json'))
        with patch('builtins.open', unittest.mock.mock_open(read_data=json.dumps(items))):
            for args in ({}, {'input_file': 'backup.json', 'output_file': 'backup_output.json'}):
                with self.subTest(args=args):
                    asyncio.run(ns['main'](**args))
                    ns['process_duplicate_purge_pipeline'].assert_called_with(items if args else items[1:3])

    def test_default_input_is_live_and_output_uses_kst_date(self):
        ns = scheduler_namespace()
        clock = Mock()
        clock.now.return_value = datetime(2026, 10, 28, 22, tzinfo=timezone(timedelta(hours=9)))
        ns['datetime'] = clock
        self.assertEqual(ns['get_live_file_paths'](),
                         ('news_list_filtered.json', 'extracted_schedule/extracted_schedules_261028.json'))
        self.assertEqual(clock.now.call_args.args[0].utcoffset(None), timedelta(hours=9))
        self.assertEqual(ns['get_file_paths_for_date_str']('261027')[0],
                         'news_back_up/news_list_filtered_backup_261027.json')

    def test_snapshot_holds_shared_lock_only_while_reading(self):
        ns = scheduler_namespace()
        lock = threading.Lock()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'news.json'
            source.write_text('[{"title": "news"}]')
            original_load = json.load

            def read_locked(stream):
                self.assertTrue(lock.locked())
                return original_load(stream)

            with patch.object(json, 'load', side_effect=read_locked):
                self.assertEqual(ns['load_news_snapshot'](source, lock), [{'title': 'news'}])
            self.assertFalse(lock.locked())
            self.assertEqual(ns['load_news_snapshot'](source), [{'title': 'news'}])

    def test_run_at_22_kst_and_roll_to_next_day_after_start_time(self):
        for hour, minute, expected_delay in [(21, 59, 60), (22, 0, 86400), (23, 0, 82800)]:
            with self.subTest(hour=hour, minute=minute):
                ns = scheduler_namespace()
                now = datetime(2026, 10, 28, hour, minute, tzinfo=timezone(timedelta(hours=9)))

                class Clock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return now.astimezone(tz)

                ns['datetime'] = Clock
                ns['main'] = AsyncMock(return_value=(True, 'ok'))
                lock = threading.Lock()
                with patch.object(asyncio, 'sleep', new_callable=AsyncMock,
                                  side_effect=[None, asyncio.CancelledError()]) as sleep:
                    with self.assertRaises(asyncio.CancelledError):
                        asyncio.run(ns['daily_schedule_extraction_scheduler'](lock))
                self.assertEqual(sleep.call_args_list[0].args[0], expected_delay)
                ns['main'].assert_awaited_once_with(db_lock=lock)
                next_run = ns['report_batch_run'].call_args.args[2]
                self.assertEqual((next_run.hour, next_run.minute), (22, 0))
                self.assertEqual(next_run, now + timedelta(seconds=expected_delay, days=1))
