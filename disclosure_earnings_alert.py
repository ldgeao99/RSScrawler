"""잠정실적 공시 알림 보강. 현재 실적은 DART, 비교 자료는 WiseReport에서 읽는다."""
import calendar
import html
import http.client
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

logger = logging.getLogger('news_logger')
MAP_FILE = Path('base_info/company_stock_map.json')
ALIASES_FILE = Path('base_info/company_stock_aliases.json')
METRICS = ('revenue', 'operating_profit', 'net_income')
LABELS = dict(zip(METRICS, ('매출액', '영업익', '순이익')))


def is_earnings_disclosure(title):
    return bool(re.search(r'\(\s*잠정\s*\)\s*실적', html.unescape(title)))


def is_periodic_report(title):
    """분기·반기·사업보고서. 사업보고서는 연간에서 3분기 누적을 빼 4분기 단독 실적을 만든다."""
    return bool(re.search(r'(분기|반기|사업)\s*보고서', html.unescape(title)))


def is_earnings_report(title):
    return is_earnings_disclosure(title) or is_periodic_report(title)


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


def stock_code_from_dart(company, main_html, opener):
    """현재 공시에서 연결한 DART 기업정보의 공시회사명·종목코드를 확인한다."""
    corp_ids = set(re.findall(r"""openCorpInfoNew\(\s*['"](\d{8})['"]""", main_html))
    if len(corp_ids) != 1:
        return None
    url = 'https://dart.fss.or.kr/dsae001/selectPopup.ax?' + urlencode({'selectKey': corp_ids.pop()})
    source = _fetch(opener, url, 'https://dart.fss.or.kr/')
    fields = {}
    for row in _Rows(source).rows:
        if len(row) >= 2:
            label = re.sub(r'\s+', '', row[0]['text'])
            if label in ('공시회사명', '종목코드'):
                fields.setdefault(label, set()).add(re.sub(r'\s+', '', row[1]['text']))
    if fields.get('공시회사명') != {re.sub(r'\s+', '', company)}:
        raise ValueError('DART 기업정보의 공시회사명이 현재 공시와 다릅니다.')
    codes = fields.get('종목코드', set())
    if len(codes) != 1 or not re.fullmatch(r'[0-9A-Z]{6}', next(iter(codes))):
        raise ValueError('DART 기업정보에서 유일한 상장 종목코드를 확인하지 못했습니다.')
    return codes.pop()


def find_alias_stock_code(company, path=None):
    alias_path = Path(path or ALIASES_FILE)
    if not alias_path.exists():
        return None
    aliases = json.loads(alias_path.read_text(encoding='utf-8'))
    normalized = re.sub(r'\s+', '', company)
    codes = {code for name, code in aliases.items() if re.sub(r'\s+', '', name) == normalized}
    if not codes:
        return None
    if len(codes) != 1:
        raise ValueError('기업명 별칭의 종목코드가 중복됩니다.')
    code = codes.pop()
    if not isinstance(code, str) or not re.fullmatch(r'[0-9A-Z]{6}', code):
        raise ValueError('기업명 별칭의 종목코드가 올바르지 않습니다.')
    return code


def resolve_stock_code(company, main_html, opener):
    try:
        code = stock_code_from_dart(company, main_html, opener)
        if code:
            return code
    except Exception as error:
        logger.warning('DART 기업정보 종목코드 조회 실패, 로컬 매칭 사용: %s', error)
    try:
        return find_stock_code(company)
    except (FileNotFoundError, ValueError):
        code = find_alias_stock_code(company)
        if code:
            return code
        raise


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


def _fetch(opener, url, referer=None, data=None):
    headers = {'User-Agent': 'Mozilla/5.0'}
    if referer:
        headers['Referer'] = referer
    for attempt in range(2):
        try:
            with opener.open(Request(url, data=data, headers=headers), timeout=8) as response:
                raw = response.read()
                charset = response.headers.get_content_charset()
            break
        except (http.client.RemoteDisconnected, ConnectionResetError):
            # DART는 연속 요청 시 응답 없이 연결을 끊는 경우가 있어 한 번만 다시 시도한다.
            if attempt:
                raise
            time.sleep(1)
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
    params = {'cmp_cd': code, 'fin_typ': 4 if earnings['consolidated'] else 3,
              'freq_typ': 'Q', 'extY': 0, 'extQ': 1,
              'encparam': enc[1], 'id': element[1]}
    quarter = _fetch(opener, 'https://comp.wisereport.co.kr/company/ajax/cF1001.aspx?' + urlencode(params), url)
    # 응답 표도 공시와 같은 연결/별도 기준인지 확인한다.
    bases = set()
    for row in _Rows(quarter).rows:
        for cell in row:
            if re.search(r'\d{4}/(03|06|09|12)', cell['text']):
                match = re.search(r'IFRS(연결|별도)', cell['text'].replace(' ', ''))
                if not match:
                    raise ValueError('WiseReport 회계기준을 확인하지 못했습니다.')
                bases.add(match[1])
    if len(bases) != 1:
        raise ValueError('WiseReport 연결/별도 기준이 일정하지 않습니다.')
    consolidated = bases.pop() == '연결'
    if consolidated != earnings['consolidated']:
        raise ValueError('WiseReport 회계기준이 공시와 다릅니다.')
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
    if earnings.get('annual'):
        annual = earnings['annual']
        lines.append(f"{earnings['period'][0]}년 연간 : " + ' / '.join(_amount(annual.get(m)) for m in METRICS))
    history_basis = f'당기 {basis} / 과거 {reference_basis}' if mixed_basis else f'{basis}기준'
    lines += ['', f'**최근 실적 추이({history_basis})**', '매출액 / 영업익 / 당기순이익']
    period = earnings['period']
    for offset in range(5):
        ordinal = period[0] * 4 + period[1] - 1 - offset
        previous = (ordinal // 4, ordinal % 4 + 1)
        values = earnings['actual'] if offset == 0 else wise.get('history', {}).get(previous, {})
        row_basis = f'({basis if offset == 0 else reference_basis})' if mixed_basis else ''
        lines.append(f'{previous[0]}.{previous[1]}Q{row_basis} ' + ' / '.join(_amount(values.get(m)) for m in METRICS))
    lines.append('')
    if earnings.get('annual'):
        lines.append(f'※ {period[1]}Q 실적 = 사업보고서 연간 - 3분기보고서 누적')
    if wise:
        lines.append(f'※ 예상치: WiseReport K-IFRS({reference_basis}) 기준')
        if wise.get('market_date') and wise.get('market_cap') is not None:
            lines.append(f"※ 시가총액 기준일: {wise['market_date']}")
    lines += ['', '공시링크:', escape(link)]
    if code:
        history_link = 'https://comp.wisereport.co.kr/company/c1010001.aspx?' + urlencode({'cn': '', 'cmp_cd': code})
        lines += ['과거실적:', escape(history_link)]
    return '\n'.join(lines)


_UNIT_FACTORS = {'억원': Decimal(1), '백만원': Decimal('0.01'),
                 '천원': Decimal('0.00001'), '원': Decimal('0.00000001')}
_STATEMENT_METRICS = (
    ('revenue', re.compile(r'^(매출액|매출|영업수익|수익)(\(.*\))?$')),
    ('operating_profit', re.compile(r'^영업(이익|손실|손익)')),
    ('net_income', re.compile(r'^(당기|분기|반기)?순(이익|손실|손익)')),
)


def _statement_label(text):
    label = re.sub(r'\s+', '', text)
    label = re.sub(r'^([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+|[IVX]+|\d+|[가-하])[.)]', '', label)
    return re.sub(r'\(주석[^)]*\)$', '', label)


def parse_income_statement(section_html, cumulative=False):
    """정기보고서 손익계산서에서 분기 단독(3개월) 매출·영업이익·순이익(억원)과 분기를 읽는다.
    cumulative=True면 분기보고서의 누적 열을, 사업보고서(12월 결산)는 연간 열을 읽고 (연도, 4)를 돌려준다."""
    start = re.search(r'(포괄)?\s*손\s*익\s*계\s*산\s*서', section_html)
    if not start:
        raise ValueError('손익계산서를 찾지 못했습니다.')
    end = re.search(r'자\s*본\s*변\s*동\s*표', section_html[start.end():])
    source = section_html[start.start():start.end() + end.start() if end else len(section_html)]
    text = _text(source)
    if re.search(r'해당\s*사항\s*(이\s*)?없음', text[:300]):
        raise ValueError('손익계산서가 해당사항 없음입니다.')
    span = re.search(r'(\d{4})[.-](\d{2})[.-](\d{2})\s*부터\s*(\d{4})[.-](\d{2})[.-](\d{2})\s*까지', text)
    if not span:
        raise ValueError('손익계산서 기간을 확인하지 못했습니다.')
    begin, finish = date(*map(int, span.groups()[:3])), date(*map(int, span.groups()[3:]))
    annual = finish.month == 12 and finish.day == 31 and (begin.month, begin.day) == (1, 1)
    if not annual and (finish.month not in (3, 6, 9)
                       or finish.day != calendar.monthrange(finish.year, finish.month)[1]):
        raise ValueError('분기 말 또는 12월 결산 기준 손익계산서가 아닙니다.')
    unit = re.search(r'단위\s*[:：]\s*(백만원|천원|억원|원)', text)
    if not unit:
        raise ValueError('손익계산서 금액 단위를 확인하지 못했습니다.')
    factor = _UNIT_FACTORS[unit[1]]
    rows = _Rows(source).rows
    first_metric = next((i for i, row in enumerate(rows) if row and any(
        pattern.match(_statement_label(row[0]['text'])) for _, pattern in _STATEMENT_METRICS)), None)
    if first_metric is None:
        raise ValueError('손익계산서 항목을 찾지 못했습니다.')
    headers = rows[:first_metric]
    # '주석' 열이 있으면 금액은 그 다음 열부터, '3개월' 열이 있으면 그 열이 분기 단독 실적이다.
    offset = 2 if any(re.sub(r'\s+', '', c['text']) == '주석' for row in headers for c in row) else 1
    quarter_row = next((row for row in headers
                        if any(re.sub(r'\s+', '', c['text']) == '3개월' for c in row)), None)
    column = offset
    wanted = '누적' if cumulative else '3개월'
    if annual:
        pass  # 사업보고서는 첫 금액 열이 당기 연간 실적이다.
    elif quarter_row is not None:
        column += next(i for i, c in enumerate(quarter_row) if re.sub(r'\s+', '', c['text']) == wanted)
    elif finish.month != 3:
        raise ValueError(f'{wanted} 실적 열을 찾지 못했습니다.')
    actual = dict.fromkeys(METRICS)
    for row in rows[first_metric:]:
        label = _statement_label(row[0]['text']) if row else ''
        for metric, pattern in _STATEMENT_METRICS:
            if actual[metric] is None and pattern.match(label) and len(row) > column:
                value = _value(row[column])
                actual[metric] = value * factor if value is not None else None
    if all(v is None for v in actual.values()):
        raise ValueError('손익계산서의 분기 실적을 읽지 못했습니다.')
    return (finish.year, (finish.month + 2) // 3), actual


def _report_sections(main_html):
    pattern = (r"node\d*\['text'\]\s*=\s*\"(.*?)\";.*?node\d*\['rcpNo'\]\s*=\s*\"(\d+)\";"
               r".*?node\d*\['dcmNo'\]\s*=\s*\"(\d+)\";.*?node\d*\['eleId'\]\s*=\s*\"(\d+)\";"
               r".*?node\d*\['offset'\]\s*=\s*\"(\d+)\";.*?node\d*\['length'\]\s*=\s*\"(\d+)\";"
               r".*?node\d*\['dtd'\]\s*=\s*\"([^\"]+)\";")
    keys = ('rcpNo', 'dcmNo', 'eleId', 'offset', 'length', 'dtd')
    return [(re.sub(r'\s+', '', html.unescape(m[0])), dict(zip(keys, m[1:])))
            for m in re.findall(pattern, main_html, re.S)]


def _statement_candidates(sections, consolidated):
    def is_consolidated(name):
        return '연결' in name
    exact = [p for name, p in sections if '손익계산서' in name and is_consolidated(name) == consolidated]
    whole = [p for name, p in sections
             if re.fullmatch(r'\d+\.' + ('연결' if consolidated else '') + r'재무제표', name)]
    return exact + whole


def _read_statement(sections, consolidated, opener, link, errors, cumulative=False):
    for params in _statement_candidates(sections, consolidated):
        try:
            body = _fetch(opener, 'https://dart.fss.or.kr/report/viewer.do?' + urlencode(params), link)
            return parse_income_statement(body, cumulative)
        except Exception as error:
            errors.append(str(error))
    return None


def find_third_quarter_report(opener, main_html, year):
    """같은 회사의 해당 연도 3분기보고서 접수번호를 DART 상세검색에서 찾는다(정정본이 있으면 최신)."""
    corp_ids = set(re.findall(r"""openCorpInfoNew\(\s*['"](\d{8})['"]""", main_html))
    if len(corp_ids) != 1:
        raise ValueError('사업보고서의 DART 기업코드를 확인하지 못했습니다.')
    data = urlencode({'currentPage': 1, 'maxResults': 15, 'textCrpCik': corp_ids.pop(),
                      'startDate': f'{year}1001', 'endDate': f'{year + 1}1231',
                      'publicType': 'A003'}).encode()
    source = _fetch(opener, 'https://dart.fss.or.kr/dsab007/detailSearch.ax',
                    'https://dart.fss.or.kr/dsab007/main.do', data)
    for row in source.split('<tr')[1:]:
        if re.search(rf'분기보고서\s*\(\s*{year}\.09\s*\)', _text(row)):
            receipt = re.search(r'rcpNo=(\d{14})', row)
            if receipt:
                return receipt[1]
    raise ValueError(f'{year}년 3분기보고서를 찾지 못했습니다.')


def parse_periodic_report(item, main_html, opener):
    """정기보고서는 연결재무제표가 있으면 연결, '해당사항 없음'이면 별도(개별) 기준으로 읽는다."""
    title = re.search(r'<title[^>]*>(.*?)</title>', main_html, re.I | re.S)
    parts = _text(title.group(1)).split('/') if title else []
    if len(parts) < 3 or not is_periodic_report(parts[1]):
        raise ValueError('정기보고서의 기업명/보고서명을 확인하지 못했습니다.')
    sections = _report_sections(main_html)
    link = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={sections[0][1]['rcpNo'] if sections else ''}"
    errors = []
    for consolidated in (True, False):
        statement = _read_statement(sections, consolidated, opener, link, errors)
        if statement is None:
            continue
        period, actual = statement
        report = re.sub(r'^\s*(\([^)]*\))?\s*.*?\s+-\s+', '', html.unescape(item.get('title', ''))).strip()
        earnings = {'company': parts[0].strip(), 'report': report or parts[1].strip(), 'period': period,
                    'consolidated': consolidated, 'actual': actual}
        if period[1] == 4:
            # 4분기 단독 = 연간 - 같은 기준(연결/별도) 3분기 누적.
            receipt = find_third_quarter_report(opener, main_html, period[0])
            quarter_link = f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}'
            quarter_sections = _report_sections(_fetch(opener, quarter_link))
            nine_months = _read_statement(quarter_sections, consolidated, opener, quarter_link, errors, True)
            if nine_months is None or nine_months[0] != (period[0], 3):
                raise ValueError('3분기 누적 실적을 읽지 못했습니다: ' + '; '.join(errors[-2:]))
            earnings['annual'] = actual
            earnings['actual'] = {
                metric: actual[metric] - nine_months[1][metric]
                if actual[metric] is not None and nine_months[1][metric] is not None else None
                for metric in METRICS}
        return earnings
    raise ValueError('정기보고서 손익계산서를 읽지 못했습니다: ' + '; '.join(errors[-2:]))


def fetch_dart_document(opener, receipt):
    """DART 공시 메인 페이지와 본문 문서(viewer.do) HTML을 함께 가져온다."""
    link = f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}'
    main = _fetch(opener, link)
    doc = re.search(r'viewDoc\(\s*[\'"]' + receipt + r'[\'"]\s*,\s*[\'"](\d+)[\'"]\s*,\s*[\'"](\d+)[\'"]\s*,\s*[\'"](\d+)[\'"]\s*,\s*[\'"](\d+)[\'"]\s*,\s*[\'"]([^\'"]+)[\'"]', main)
    if not doc:
        raise ValueError('DART 본문 위치를 찾지 못했습니다.')
    params = dict(zip(('dcmNo', 'eleId', 'offset', 'length', 'dtd'), doc.groups()))
    params['rcpNo'] = receipt
    return main, _fetch(opener, 'https://dart.fss.or.kr/report/viewer.do?' + urlencode(params), link)


def _receipt(item):
    receipt = parse_qs(urlparse(item.get('link', '')).query).get('rcpNo', [''])[0]
    return receipt if re.fullmatch(r'\d{14}', receipt) else None


def is_fair_disclosure(title):
    """수시공시의무관련사항(공정공시): 제목만으로는 내용을 알 수 없어 본문의 공시제목을 덧붙인다."""
    return '수시공시의무관련사항' in re.sub(r'\s+', '', html.unescape(title))


def parse_fair_disclosure_title(body_html):
    for row in _Rows(body_html).rows:
        for index, cell in enumerate(row[:-1]):
            if cell['text'].replace(' ', '') == '공시제목':
                title = row[index + 1]['text'].strip()
                if title and title != '-':
                    return title
    raise ValueError('공정공시 본문의 공시제목을 찾지 못했습니다.')


def fetch_fair_disclosure_title(item):
    """실패하면 None을 돌려 기존 알림(원래 제목만)으로 보낸다."""
    if not is_fair_disclosure(item.get('title', '')):
        return None
    receipt = _receipt(item)
    if not receipt:
        return None
    try:
        _, body = fetch_dart_document(build_opener(HTTPCookieProcessor(CookieJar())), receipt)
        title = parse_fair_disclosure_title(body)
    except Exception as error:
        logger.warning('공정공시 공시제목 보강 실패, 기존 공시 알림 사용: %s', error)
        return None
    return title if len(title) <= 300 else title[:300] + '…'


def build_earnings_alert(item, on_record=None):
    """잠정실적 공시와 분기·반기보고서를 같은 실적 알림으로 만든다. DART 실패는 기존 알림으로, 매칭/비교자료 실패는 현재 실적만으로 대체한다."""
    if not is_earnings_report(item.get('title', '')):
        return None
    receipt = _receipt(item)
    if not receipt:
        return None
    link = f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}'
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    try:
        if is_earnings_disclosure(item.get('title', '')):
            main, body = fetch_dart_document(opener, receipt)
            earnings = parse_dart(main, body)
        else:
            main = _fetch(opener, link)
            earnings = parse_periodic_report(item, main, opener)
    except Exception as error:
        logger.warning('실적 본문 보강 실패, 기존 공시 알림 사용: %s', error)
        return None
    code, wise = None, None
    try:
        code = resolve_stock_code(earnings['company'], main, opener)
        wise = fetch_wise(opener, code, earnings)
    except Exception as error:
        logger.warning('실적 비교자료 보강 실패, DART 실적만 알림: %s', error)
    message = format_earnings(earnings, link, code, wise)
    if on_record is not None:
        reference = wise or {}
        history = []
        for offset in range(5):
            ordinal = earnings['period'][0] * 4 + earnings['period'][1] - 1 - offset
            period = (ordinal // 4, ordinal % 4 + 1)
            history.append({
                'period': f'{period[0]}Q{period[1]}',
                'consolidated': earnings['consolidated'] if offset == 0 else reference.get('consolidated'),
                'actual': earnings['actual'] if offset == 0 else reference.get('history', {}).get(period, {}),
            })
        on_record({
            'company': earnings['company'], 'code': code, 'report': earnings['report'],
            'period': f"{earnings['period'][0]}Q{earnings['period'][1]}", 'unit': '억원',
            'consolidated': earnings['consolidated'], 'actual': earnings['actual'],
            'consensus': reference.get('consensus', {}),
            'reference_consolidated': reference.get('consolidated'),
            'market_cap': reference.get('market_cap'), 'market_date': reference.get('market_date'),
            'reference_fetched_at': datetime.now(timezone(timedelta(hours=9))).isoformat() if wise else None,
            'history': history, 'annual': earnings.get('annual'),
        })
    # HTML 태그/엔티티를 중간에서 자르지 않고 기존 알림으로 대체한다.
    return message if len(message.encode('utf-16-le')) // 2 <= 4000 else None
