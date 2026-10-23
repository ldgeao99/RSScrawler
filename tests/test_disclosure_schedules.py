import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import disclosure_schedule_extraction as extraction
import disclosure_back_up_scheduler as backup


def body(notes='', heading='기타 투자판단과 관련한 중요사항', date='2026-10-30'):
    return f'<table><tr><td>3. 결산실적 공시예정일</td><td>{date}</td></tr><tr><td>4. {heading}</td><td>{notes}</td></tr></table>'


class DisclosureScheduleTest(unittest.TestCase):
    def test_both_market_formats_and_blank_details(self):
        for title, heading, notes, expected in [
            ('(유가)DL이앤씨 - 결산실적공시예고(안내공시)', '기타 투자판단과 관련한 중요사항', '일정 변경 가능', 'DL이앤씨 실적발표'),
            ('(코스닥)컴투스 - 결산실적공시예고', '기타 투자판단에 참고할 사항', '확정치가 아닌\n 잠정치이며', '컴투스 (잠정)실적발표'),
        ]:
            result = extraction.parse_announcement(body(notes, heading), title, 'url')
            self.assertEqual(result['eventName'], expected)
            self.assertEqual(result['detail'], '')
            self.assertEqual(result['date'], '2026-10-30')

    def test_accounting_period_in_detail(self):
        period = '<tr><td>2. 결산대상기간</td><td>시작일</td><td>2026-07-01</td><td>종료일</td><td>2026-09-30</td></tr>'
        html = body('잠정 실적').replace('<table>', '<table>' + period)
        result = extraction.parse_announcement(html, '기업 - 결산실적공시예고', 'url')
        self.assertEqual(result['detail'], '결산대상기간: 2026-07-01 ~ 2026-09-30')
        self.assertEqual(result['date'], '2026-10-30')
        self.assertEqual(result['category'], '한국실적')

    def test_invalid_accounting_period_is_rejected(self):
        for start, end in [('2026-02-30', '2026-09-30'), ('2026-10-01', '2026-09-30'), ('-', '2026-09-30')]:
            with self.subTest(start=start, end=end):
                html = body().replace('<table>', f'<table><tr><td>2. 결산대상기간</td><td>시작일</td><td>{start}</td><td>종료일</td><td>{end}</td></tr>')
                with self.assertRaises(ValueError):
                    extraction.parse_announcement(html, '기업 - 결산실적공시예고', 'url')

    def test_provisional_keyword_variations(self):
        for notes in ('잠정치입니다.', '잠정 실적을 발표할 예정입니다.',
                      '이번 결산실적은 잠정치로 변경될 수 있습니다.', '잠정',
                      '잠<span>정</span>치'):
            with self.subTest(notes=notes):
                result = extraction.parse_announcement(body(notes), '기업 - 결산실적공시예고', 'url')
                self.assertEqual(result['eventName'], '기업 (잠정)실적발표')

    def test_missing_body_or_invalid_date_is_not_classified_as_final(self):
        for html in ('error', body(date='2026-02-30'), '<p>3. 결산실적 공시예정일 2026-10-30</p>'):
            with self.assertRaises(ValueError):
                extraction.parse_announcement(html, '(유가)기업 - 결산실적공시예고', 'url')

    def test_phrase_outside_notes_does_not_mark_provisional(self):
        result = extraction.parse_announcement('<p>확정치가 아닌 잠정치</p>' + body('일정 변경 가능'), '기업 - 결산실적공시예고', 'url')
        self.assertEqual(result['eventName'], '기업 실적발표')

    def test_dart_link_resolves_viewer(self):
        item = {'title': '(유가)기업 - 결산실적공시예고', 'link': 'https://dart.fss.or.kr/api/link.jsp?rcpNo=20261023800970'}
        with patch.object(extraction, '_fetch', side_effect=['viewDoc("20261023800970", "1234", "0", "0", "0", "HTML", "");', body()]) as fetch:
            self.assertEqual(extraction.fetch_announcement(item)['eventName'], '기업 실적발표')
            self.assertIn('dcmNo=1234', fetch.call_args.args[0])

    def test_registration_is_idempotent_and_notifies_only_after_create(self):
        item = {'title': '기업 - 결산실적공시예고', 'link': 'https://dart.fss.or.kr/api/link.jsp?rcpNo=20261023800970'}
        collection = Mock()
        collection.where.return_value.stream.return_value = []
        ref = collection.document.return_value
        ref.get.return_value.exists = False
        db = Mock()
        db.collection.return_value = collection
        modules = {'google.api_core.exceptions': types.SimpleNamespace(AlreadyExists=type('AlreadyExists', (Exception,), {}))}
        with patch.dict(sys.modules, modules), patch('batch_logger._get_db', return_value=db), patch.object(extraction, 'fetch_announcement', return_value={**extraction.parse_announcement(body(), item['title'], item['link']), 'detail': '결산대상기간: 2026-07-01 ~ 2026-09-30'}), patch.object(extraction, '_send_pending') as notify:
            extraction.extract_disclosure_schedules([item, item])
            self.assertEqual(ref.create.call_count, 1)
            saved = ref.create.call_args.args[0]
            self.assertTrue(saved['disclosureNotificationPending'])
            self.assertEqual(saved['detail'], '결산대상기간: 2026-07-01 ~ 2026-09-30')
            notify.assert_called_once_with(collection)
            ref.get.return_value.exists = True
            extraction.extract_disclosure_schedules([item])
            self.assertEqual(ref.create.call_count, 1)

    def test_telegram_failure_keeps_pending(self):
        collection = Mock()
        snap = Mock()
        snap.to_dict.return_value = {'date': '2026-10-30', 'eventName': '기업 실적발표', 'url': 'url', 'registeredDate': '2026-10-24'}
        collection.where.return_value.stream.return_value = [snap]
        with patch.dict('os.environ', TELEGRAM_BOT_TOKEN='test', TELEGRAM_CHAT_ID='test'), patch.object(extraction, 'urlopen', side_effect=OSError('network')):
            with self.assertRaises(OSError):
                extraction._send_pending(collection)
        snap.reference.update.assert_not_called()

    def test_telegram_combines_events_without_links(self):
        collection = Mock()
        snapshots = []
        for name, date in [('컴투스 (잠정)실적발표', '2026-11-12'), ('DL이앤씨 실적발표', '2026-10-30')]:
            snap = Mock()
            snap.to_dict.return_value = {'date': date, 'eventName': name, 'url': 'https://dart.fss.or.kr/example'}
            snapshots.append(snap)
        collection.where.return_value.stream.return_value = snapshots
        response = Mock()
        response.read.return_value = b'{"ok": true}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.dict('os.environ', TELEGRAM_BOT_TOKEN='test', TELEGRAM_CHAT_ID='test'), patch.object(extraction, 'urlopen', return_value=response) as send:
            extraction._send_pending(collection)
        send.assert_called_once()
        text = json.loads(send.call_args.args[0].data)['text']
        self.assertIn('(2건)', text)
        self.assertIn('2026-10-30 | DL이앤씨 실적발표', text)
        self.assertIn('2026-11-12 | 컴투스 (잠정)실적발표', text)
        self.assertNotIn('http', text)
        for snap in snapshots:
            snap.reference.update.assert_called_once_with({'disclosureNotificationPending': False})

    def test_backup_inputs_include_archives_and_all_live_feeds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'disclosure_news_list_stream_backup_261023.json'
            archive.write_text(json.dumps([{'title': 'archived'}]))
            live = root / 'filtered.json'
            live.write_text(json.dumps([{'title': 'live'}]))
            with patch.object(backup, 'BACKUP_DIR', root), patch.object(extraction, 'extract_disclosure_schedules', return_value='ok') as extract:
                self.assertEqual(backup.process_earnings_schedules({'filtered': str(live)}), 'ok')
                self.assertEqual(len(extract.call_args.args[0]), 2)
