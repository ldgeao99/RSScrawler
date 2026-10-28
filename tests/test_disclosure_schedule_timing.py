import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import disclosure_back_up_scheduler as scheduler


class DisclosureTimingTest(unittest.TestCase):
    def test_all_weekdays_before_and_at_execution_time(self):
        monday = datetime(2026, 10, 26, 21, 59, tzinfo=scheduler.KST)
        for day in range(7):
            before = monday + timedelta(days=day)
            expected = before.replace(hour=22, minute=0)
            while expected.weekday() >= 5:
                expected += timedelta(days=1)
            with self.subTest(day=day):
                self.assertEqual(scheduler.next_earnings_schedule_run(before), expected)
                at = before.replace(hour=22, minute=0)
                following = at + timedelta(days=1)
                while following.weekday() >= 5:
                    following += timedelta(days=1)
                self.assertEqual(scheduler.next_earnings_schedule_run(at), following)

    def test_utc_input_and_year_boundary(self):
        now = datetime(2027, 1, 1, 13, tzinfo=timezone.utc)
        self.assertEqual(scheduler.next_earnings_schedule_run(now),
                         datetime(2027, 1, 4, 22, tzinfo=scheduler.KST))


class DisclosureSchedulerTest(unittest.IsolatedAsyncioTestCase):
    async def test_separate_execution_and_failure_reporting(self):
        for error in (None, ValueError('failed')):
            with self.subTest(error=error), \
                    patch.object(scheduler.asyncio, 'sleep', new_callable=AsyncMock,
                                 side_effect=[None, asyncio.CancelledError()]), \
                    patch.object(scheduler.asyncio, 'to_thread', new_callable=AsyncMock,
                                 return_value='registered', side_effect=error) as work, \
                    patch.object(scheduler, 'report_batch_run') as report:
                lock, files = object(), {'stream': 'live.json'}
                with self.assertRaises(asyncio.CancelledError):
                    await scheduler.weekday_earnings_schedule_scheduler(lock, files)
                work.assert_awaited_once_with(scheduler.process_earnings_schedules, files, lock)
                self.assertEqual(report.call_args.args[0], 'disclosure_earnings_schedules')
                self.assertEqual(report.call_args.args[3], error is None)

    async def test_midnight_backup_does_not_register_schedules(self):
        with patch.object(scheduler.asyncio, 'sleep', new_callable=AsyncMock,
                          side_effect=[None, asyncio.CancelledError()]), \
                patch.object(scheduler.asyncio, 'to_thread', new_callable=AsyncMock) as work, \
                patch.object(scheduler, 'report_batch_run'):
            with self.assertRaises(asyncio.CancelledError):
                await scheduler.daily_backup_and_cleanup_scheduler('lock', {}, {})
            work.assert_awaited_once_with(scheduler.execute_midnight_processing, 'lock', {}, {})
