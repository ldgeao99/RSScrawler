"""DART 결산실적 공시예고를 규칙으로 추출한다 (LLM 사용 없음)."""
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger('news_logger')
KST = timezone(timedelta(hours=9))


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def parse_announcement(body, title, link):
    parser = _TextParser()
    parser.feed(body)
    text = ' '.join(parser.parts)
    date_match = re.search(r'3\s*\.\s*결산실적\s*공시\s*예정일\s*(\d{4}-\d{2}-\d{2})', text)
    notes = re.search(r'4\s*\.\s*기타\s*투자판단(?:과\s*관련한\s*중요사항|에\s*참고할\s*사항)(.*?)(?:※\s*관련공시|$)', text, re.S)
    company = re.search(r'^(?:\([^)]*\)\s*)?(.+?)\s*[-–]\s*(?:\[.*?\]\s*)?결산실적\s*공시\s*예고', title)
    if not date_match or not notes or not company:
        raise ValueError('회사명, 공시예정일 또는 기타 투자판단 항목을 확인할 수 없습니다.')
    date = date_match.group(1)
    datetime.strptime(date, '%Y-%m-%d')
    name = company.group(1).strip()
    provisional = '잠정' in re.sub(r'\s+', '', notes.group(1))
    event_name = f'{name} {"(잠정)" if provisional else ""}실적발표'
    return {'date': date, 'category': '한국실적', 'isImportant': False,
            'eventName': event_name, 'title': event_name, 'detail': '',
            'relatedStocks': '', 'url': link}


def _fetch(url):
    with urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=30) as response:
        raw = response.read()
        charset = response.headers.get_content_charset()
    if charset:
        return raw.decode(charset)
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('cp949')


def fetch_announcement(item):
    receipt = parse_qs(urlparse(item['link']).query).get('rcpNo', [''])[0]
    if not re.fullmatch(r'\d{14}', receipt):
        raise ValueError('DART 접수번호가 없습니다.')
    link = f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}'
    main = _fetch(link)
    doc = re.search(r'viewDoc\(\s*[\'"]' + receipt + r'[\'"]\s*,\s*[\'"](\d+)[\'"]\s*,\s*[\'"](\d+)[\'"]\s*,\s*[\'"](\d+)[\'"]\s*,\s*[\'"](\d+)[\'"]\s*,\s*[\'"]([^\'"]+)[\'"]', main)
    if not doc:
        raise ValueError('DART 본문 문서 정보를 찾을 수 없습니다.')
    params = dict(zip(('dcmNo', 'eleId', 'offset', 'length', 'dtd'), doc.groups()))
    params['rcpNo'] = receipt
    return parse_announcement(_fetch('https://dart.fss.or.kr/report/viewer.do?' + urlencode(params)), item['title'], link)


def _send_pending(collection):
    token, chat = os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID')
    pending = list(collection.where('disclosureNotificationPending', '==', True).stream())
    if pending and (not token or not chat):
        raise RuntimeError('텔레그램 설정이 없어 신규 일정 알림을 보류합니다.')
    if not pending:
        return
    pending.sort(key=lambda snap: (snap.to_dict()['date'], snap.to_dict()['eventName']))
    heading = f'📅 공시 실적발표 일정 추가 ({len(pending)}건)\n'
    # Telegram의 4096자 제한을 넘는 경우에만 묶음을 나눈다.
    batches = []
    text, included = heading, []
    for snap in pending:
        event = snap.to_dict()
        line = f'\n{event["date"]} | {event["eventName"]}'
        if len((text + line).encode('utf-16-le')) // 2 > 4096:
            batches.append((text, included))
            text, included = heading, []
        text += line
        included.append(snap)
    if included:
        batches.append((text, included))
    # 실패한 묶음은 다음 배치에서 재전송한다.
    for text, included in batches:
        request = Request(f'https://api.telegram.org/bot{token}/sendMessage',
                          data=json.dumps({'chat_id': chat, 'text': text}).encode(),
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=15) as response:
            if not json.load(response).get('ok'):
                raise RuntimeError('텔레그램 일정 알림 전송 실패')
        for snap in included:
            snap.reference.update({'disclosureNotificationPending': False})


def extract_disclosure_schedules(items):
    from batch_logger import _get_db
    candidates = {}
    for item in items:
        if re.search(r'결산실적\s*공시\s*예고', item.get('title', '')):
            candidates[item.get('link', '')] = item
    db = _get_db()
    if db is None:
        if candidates:
            raise RuntimeError('Firestore 인증이 없어 공시 일정 등록을 실행하지 못했습니다.')
        return '실적발표 대상 없음'
    from google.api_core.exceptions import AlreadyExists
    collection = db.collection('temp_events')
    known_urls = {snap.to_dict().get('url') for snap in
                  collection.where('disclosureSchedule', '==', True).stream()}
    added, failed = 0, 0
    for item in candidates.values():
        try:
            receipt = parse_qs(urlparse(item.get('link', '')).query).get('rcpNo', [''])[0]
            if f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}' in known_urls:
                continue
            event = fetch_announcement(item)
            doc_id = hashlib.sha1(f'{event["eventName"]}|{event["date"]}'.encode()).hexdigest()[:24]
            ref = collection.document(doc_id)
            if ref.get().exists:
                continue
            now = datetime.now(KST)
            event.update(articleDate=now.strftime('%Y-%m-%d %H:%M:%S'),
                         registeredDate=now.strftime('%Y-%m-%d'), disclosureNotificationPending=True,
                         disclosureSchedule=True)
            try:
                ref.create(event)
                added += 1
            except AlreadyExists:
                pass
        except Exception:
            failed += 1
            logger.exception('공시 일정 등록 실패: %s', item.get('title'))
    _send_pending(collection)
    if failed:
        raise RuntimeError(f'공시 일정 {added}건 추가, {failed}건 처리 실패 (백업에서 재처리 가능)')
    return f'실적발표 일정 {added}건 신규 등록 및 알림 처리'
