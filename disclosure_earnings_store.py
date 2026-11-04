"""공시 실적 알림의 구조화 데이터와 텔레그램 메시지를 JSON으로 보관한다."""
import html
import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger('news_logger')
STORE_FILE = Path('disclosure_earnings.json')
STORE_LOCK = threading.RLock()
KST = timezone(timedelta(hours=9))


def _load():
    if not STORE_FILE.exists():
        return []
    records = json.loads(STORE_FILE.read_text(encoding='utf-8'))
    if not isinstance(records, list):
        raise ValueError('실적 기록은 JSON 배열이어야 합니다.')
    return records


def _write(records):
    STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=STORE_FILE.parent,
                                         prefix=STORE_FILE.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(records, stream, ensure_ascii=False, indent=2, default=_json_default)
        os.replace(temporary, STORE_FILE)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f'JSON으로 저장할 수 없는 값: {type(value).__name__}')


def record_notification(item, message, fields=None, status='pending'):
    """저장 실패가 텔레그램 전송을 중단시키지 않도록 한다."""
    try:
        receipt = parse_qs(urlparse(item.get('link', '')).query).get('rcpNo', [''])[0]
        if not re.fullmatch(r'\d{14}', receipt):
            return None
        now = datetime.now(KST).isoformat(timespec='seconds')
        with STORE_LOCK:
            records = _load()
            existing = next((entry for entry in records if entry['receipt_no'] == receipt), None)
            record = dict(existing or {})
            record.update(fields or {})
            record.update({
                'receipt_no': receipt, 'source_title': item.get('title', ''),
                'source_time': item.get('time_kst', ''), 'sentiment': item.get('sentiment', 'neutral'),
                'url': f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}',
                'message_html': message,
                'message_text': html.unescape(re.sub(r'</?b>', '', message)),
                'telegram_status': status, 'updated_at': now,
            })
            record.setdefault('created_at', now)
            if status == 'sent':
                record['sent_at'] = now
            else:
                record.pop('sent_at', None)
            records = [entry for entry in records if entry['receipt_no'] != receipt]
            records.append(record)
            records.sort(key=lambda entry: (entry['updated_at'], entry['receipt_no']), reverse=True)
            _write(records)
        return receipt
    except Exception:
        logger.exception('공시 실적 JSON 저장 실패 (텔레그램 전송은 계속)')
        return None


def list_notifications(q='', day='', status='', offset=0, limit=30):
    with STORE_LOCK:
        records = _load()
    total = len(records)
    query = q.strip().casefold()
    records = [entry for entry in records
               if (not query or query in ' '.join(str(entry.get(key, '')) for key in
                   ('company', 'code', 'report', 'source_title', 'period')).casefold())
               and (not day or entry['created_at'].startswith(day))
               and (not status or entry.get('telegram_status') == status)]
    offset, limit = max(0, offset), min(100, max(1, limit))
    return {'items': records[offset:offset + limit], 'total': total, 'filtered_total': len(records),
            'offset': offset, 'limit': limit, 'has_more': offset + limit < len(records)}
