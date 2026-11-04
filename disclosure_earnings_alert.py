"""잠정실적 공시 알림 보강. 현재 실적은 DART, 비교 자료는 WiseReport에서 읽는다."""
import calendar
import html
import json
import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

logger = logging.getLogger('news_logger')
MAP_FILE = Path('base_info/company_stock_map.json')
METRICS = ('revenue', 'operating_profit', 'net_income')
LABELS = dict(zip(METRICS, ('매출액', '영업익', '순이익')))


def is_earnings_disclosure(title):
    return bool(re.search(r'\(\s*잠정\s*\)\s*실적', html.unescape(title)))


def _text(value):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', value))).strip()


class _Rows(HTMLParser):
    """텍스트와 title 원금액을 보존하며 표의 행을 읽는다."""
    def __init__(self, source):
        super().__init__()
        self.rows, self.row_stack, self.cell_stack = [], [], []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row_stack.append([])
        elif tag in ('td', 'th') and self.row_stack:
            self.cell_stack.append({'parts': [], 'attrs': dict(attrs)})

    def handle_data(self, data):
        if self.cell_stack:
            self.cell_stack[-1]['parts'].append(data)

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell_stack and self.row_stack:
            cell = self.cell_stack.pop()
            cell['text'] = re.sub(r'\s+', ' ', ' '.join(cell.pop('parts'))).strip()
            self.row_stack[-1].append(cell)
        elif tag == 'tr' and self.row_stack:
            self.rows.append(self.row_stack.pop())


def _number(value):
    value = value.strip().replace(',', '').replace('−', '-').replace('△', '-')
    if re.fullmatch(r'\([\d.]+\)', value):
        value = '-' + value[1:-1]
    try:
        result = Decimal(value)
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def _value(cell):
    # 화면의 정수 표시보다 소수점이 보존된 title 값을 우선한다.
    title = _number(cell['attrs'].get('title') or '')
    return title if title is not None else _number(cell['text'])


def parse_dart(main_html, body_html):
    title = re.search(r'<title[^>]*>(.*?)</title>', main_html, re.I | re.S)
    parts = _text(title.group(1)).split('/') if title else []
    if len(parts) < 3 or not is_earnings_disclosure(parts[1]):
        raise ValueError('잠정실적 공시의 기업명/보고서명을 확인하지 못했습니다.')
    rows = _Rows(body_html).rows
    period = None
    for row in rows:
        if row and row[0]['text'].replace(' ', '') == '당기실적':
            dates = re.findall(r'\d{4}-\d{2}-\d{2}', ' '.join(c['text'] for c in row[1:]))
            if len(dates) == 2:
                start, end = (date.fromisoformat(v) for v in dates)
                if (start.month in (1, 4, 7, 10) and start.day == 1
                        and start.year == end.year and end.month == start.month + 2
                        and end.day == calendar.monthrange(end.year, end.month)[1]):
                    period = (end.year, (end.month + 2) // 3)
                    break
    if period is None:
        raise ValueError('분기 단독 실적기간을 확인하지 못했습니다.')
    unit = re.search(r'단위\s*[:：]\s*(백만원|천원|억원|원)', _text(body_html))
    if not unit:
        raise ValueError('공시 금액 단위를 확인하지 못했습니다.')
    factor = {'억원': Decimal(1), '백만원': Decimal('0.01'),
              '천원': Decimal('0.00001'), '원': Decimal('0.00000001')}[unit[1]]
    names = {'매출액': 'revenue', '영업이익': 'operating_profit', '당기순이익': 'net_income'}
    actual = dict.fromkeys(METRICS)
    for row in rows:
        if len(row) < 3:
            continue
        metric = names.get(re.sub(r'\s+', '', row[0]['text']))
        if metric and row[1]['text'].replace(' ', '') == '당해실적':
            value = _value(row[2])
            actual[metric] = value * factor if value is not None else None
    if all(v is None for v in actual.values()):
        raise ValueError('공시의 분기 실적을 읽지 못했습니다.')
    return {'company': parts[0].strip(), 'report': parts[1].strip(), 'period': period,
            'consolidated': '연결' in parts[1], 'actual': actual}


def find_stock_code(company, path=None):
    data = json.loads(Path(path or MAP_FILE).read_text(encoding='utf-8'))
    normalized = re.sub(r'\s+', '', company)
    matches = {str(item['code']) for item in data['companies']
               if re.sub(r'\s+', '', item['name']) == normalized}
    if len(matches) != 1:
        raise ValueError('기업명과 종목코드를 유일하게 매칭하지 못했습니다.')
    code = matches.pop()
    if not re.fullmatch(r'[0-9A-Z]{6}', code):
        raise ValueError('잘못된 종목코드입니다.')
    return code


def parse_wise_quarters(source, target, consolidated):
    rows = _Rows(source).rows
    headers = None
    basis = 'IFRS연결' if consolidated else 'IFRS별도'
    for row in rows:
        found = []
        for cell in row:
            match = re.search(r'(\d{4})/(03|06|09|12)\s*(\(E\))?', cell['text'])
            if match:
                if basis not in cell['text'].replace(' ', ''):
                    raise ValueError('WiseReport 회계기준이 공시와 다릅니다.')
                found.append(((int(match[1]), int(match[2]) // 3), bool(match[3])))
        if found:
            headers = found
            break
    if not headers:
        raise ValueError('WiseReport 분기 표가 없습니다.')
    by_name = {}
    for row in rows:
        if row:
            name = re.sub(r'\s+', '', row[0]['text'])
            if name in ('매출액', '영업이익', '영업이익(발표기준)', '당기순이익'):
                if len(row) - 1 != len(headers):
                    raise ValueError('WiseReport 열 수가 분기와 일치하지 않습니다.')
                by_name[name] = [_value(c) for c in row[1:]]
    def values(index):
        result = {}
        for key, name in zip(METRICS, ('매출액', '영업이익', '당기순이익')):
            result[key] = by_name.get(name, [None] * len(headers))[index]
        if result['operating_profit'] is None:
            result['operating_profit'] = by_name.get('영업이익(발표기준)', [None] * len(headers))[index]
        return result
    historical, consensus = {}, dict.fromkeys(METRICS)
    for index, (period, estimated) in enumerate(headers):
        if period < target and not estimated:
            historical[period] = values(index)
        elif period == target and estimated:
            consensus = values(index)
    return historical, consensus


def _fetch(opener, url, referer=None):
    headers = {'User-Agent': 'Mozilla/5.0'}
    if referer:
        headers['Referer'] = referer
    with opener.open(Request(url, headers=headers), timeout=8) as response:
        raw = response.read()
        charset = response.headers.get_content_charset()
    if not charset:
        meta = re.search(br'charset\s*=\s*[\"\x27]?([\w-]+)', raw[:5000], re.I)
        charset = meta[1].decode('ascii') if meta else 'utf-8'
    return raw.decode(charset)


def fetch_wise(opener, code, earnings):
    url = 'https://comp.wisereport.co.kr/company/c1010001.aspx?' + urlencode({'cmp_cd': code, 'cn': ''})
    main = _fetch(opener, url)
    enc = re.search(r"encparam\s*:\s*'([^']+)'", main)
    element = re.search(r",\s*id\s*:\s*'([^']+)'", main)
    if not enc or not element:
        raise ValueError('WiseReport 분기 표 요청 정보를 찾지 못했습니다.')
    params = {'cmp_cd': code, 'fin_typ': 0,
              'freq_typ': 'Q', 'extY': 0, 'extQ': 1,
              'encparam': enc[1], 'id': element[1]}
    quarter = _fetch(opener, 'https://comp.wisereport.co.kr/company/ajax/cF1001.aspx?' + urlencode(params), url)
    # 주재무제표가 실제로 선택한 연결/별도 기준을 표에서 확인한다.
    bases = set()
    for row in _Rows(quarter).rows:
        for cell in row:
            if re.search(r'\d{4}/(03|06|09|12)', cell['text']):
                match = re.search(r'IFRS(연결|별도)', cell['text'].replace(' ', ''))
                if not match:
                    raise ValueError('주재무제표의 회계기준을 확인하지 못했습니다.')
                bases.add(match[1])
    if len(bases) != 1:
        raise ValueError('주재무제표의 연결/별도 기준이 일정하지 않습니다.')
    consolidated = bases.pop() == '연결'
    history, consensus = parse_wise_quarters(quarter, earnings['period'], consolidated)
    market_cap = None
    for row in _Rows(main).rows:
        if len(row) >= 2 and row[0]['text'] == '시가총액':
            match = re.fullmatch(r'([\d,.]+)\s*억원', row[1]['text'])
            market_cap = _number(match[1]) if match else None
            break
    market_date = re.search(r'\[기준\s*:\s*(\d{4}\.\d{2}\.\d{2})\]', main)
    return {'history': history, 'consensus': consensus, 'market_cap': market_cap,
            'consolidated': consolidated,
            'market_date': market_date[1] if market_date else None}


def _amount(value):
    if value is None:
        return '-'
    rounded = value.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = abs(rounded)
    return f'{rounded:,}억'


def _surprise(actual, expected):
    if actual is None or expected is None or expected == 0:
        return ''
    # 적자 예상도 개선은 +, 악화는 -로 표시한다.
    difference = ((actual - expected) / abs(expected) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    if difference == 0:
        difference = abs(difference)
    return f' / {difference:+.0f}%'


def format_earnings(earnings, link, code=None, wise=None):
    wise = wise or {}
    escape = html.escape
    heading = f"{escape(earnings['company'])} - {escape(earnings['report'])}"
    details = []
    if wise.get('market_cap') is not None:
        details.append(f"(시가총액: {_amount(wise['market_cap'])})")
    if code:
        details.append(f'A{code}')
    lines = [heading]
    if details:
        lines.append(' '.join(details))
    lines.append('')
    basis = '연결' if earnings['consolidated'] else '별도'
    reference_basis = '연결' if wise.get('consolidated', earnings['consolidated']) else '별도'
    mixed_basis = basis != reference_basis
    for metric in METRICS:
        actual = earnings['actual'][metric]
        expected = wise.get('consensus', {}).get(metric)
        comparison = _surprise(actual, expected)
        lines.append(f'{LABELS[metric]} : {_amount(actual)}(예상치 : {_amount(expected)}{comparison})')
    history_basis = f'당기 {basis} / 과거 주재무제표·{reference_basis}' if mixed_basis else f'{basis}기준'
    lines += ['', f'**최근 실적 추이({history_basis})**', '매출액 / 영업익 / 당기순이익']
    period = earnings['period']
    for offset in range(5):
        ordinal = period[0] * 4 + period[1] - 1 - offset
        previous = (ordinal // 4, ordinal % 4 + 1)
        values = earnings['actual'] if offset == 0 else wise.get('history', {}).get(previous, {})
        row_basis = f'({basis if offset == 0 else reference_basis})' if mixed_basis else ''
        lines.append(f'{previous[0]}.{previous[1]}Q{row_basis} ' + ' / '.join(_amount(values.get(m)) for m in METRICS))
    lines.append('')
    if wise:
        lines.append(f'※ 예상치: WiseReport 주재무제표({reference_basis}) 기준')
        if wise.get('market_date') and wise.get('market_cap') is not None:
            lines.append(f"※ 시가총액 기준일: {wise['market_date']}")
    lines += ['', '공시링크:', escape(link)]
    if code:
        history_link = 'https://comp.wisereport.co.kr/company/c1010001.aspx?' + urlencode({'cn': '', 'cmp_cd': code})
        lines += ['과거실적:', escape(history_link)]
    return '\n'.join(lines)


def build_earnings_alert(item):
    """DART 실패는 기존 알림으로, 매칭/비교자료 실패는 현재 실적만으로 대체한다."""
    if not is_earnings_disclosure(item.get('title', '')):
        return None
    receipt = parse_qs(urlparse(item.get('link', '')).query).get('rcpNo', [''])[0]
    if not re.fullmatch(r'\d{14}', receipt):
        return None
    link = f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}'
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    try:
        main = _fetch(opener, link)
        doc = re.search(r'viewDoc\(\s*[\'"]' + receipt + r'[\'"]\s*,\s*[\'"](\d+)[\'"]\s*,\s*[\'"](\d+)[\'"]\s*,\s*[\'"](\d+)[\'"]\s*,\s*[\'"](\d+)[\'"]\s*,\s*[\'"]([^\'"]+)[\'"]', main)
        if not doc:
            raise ValueError('DART 본문 위치를 찾지 못했습니다.')
        params = dict(zip(('dcmNo', 'eleId', 'offset', 'length', 'dtd'), doc.groups()))
        params['rcpNo'] = receipt
        body = _fetch(opener, 'https://dart.fss.or.kr/report/viewer.do?' + urlencode(params), link)
        earnings = parse_dart(main, body)
    except Exception as error:
        logger.warning('잠정실적 본문 보강 실패, 기존 공시 알림 사용: %s', error)
        return None
    code, wise = None, None
    try:
        code = find_stock_code(earnings['company'])
        wise = fetch_wise(opener, code, earnings)
    except Exception as error:
        logger.warning('잠정실적 비교자료 보강 실패, DART 실적만 알림: %s', error)
    message = format_earnings(earnings, link, code, wise)
    # HTML 태그/엔티티를 중간에서 자르지 않고 기존 알림으로 대체한다.
    return message if len(message.encode('utf-16-le')) // 2 <= 4000 else None
