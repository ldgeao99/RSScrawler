"""단일판매ㆍ공급계약체결 공시 알림 보강. DART 본문에서 계약금액·매출액대비·계약상대·계약기간을 읽는다."""
import html
import logging
import re
from decimal import Decimal, ROUND_HALF_UP
from http.cookiejar import CookieJar
from urllib.request import HTTPCookieProcessor, build_opener

from disclosure_earnings_alert import _Rows, _number, _receipt, fetch_dart_document

logger = logging.getLogger('news_logger')


def is_supply_contract(title):
    """단일판매ㆍ공급계약체결(정정·자회사 공시 포함). 해지 공시는 양식이 달라 제외한다."""
    normalized = re.sub(r'\s+', '', html.unescape(title))
    return bool(re.search(r'단일판매.?공급계약체결', normalized))


def _label(text):
    return re.sub(r'^\d+\.|^-', '', re.sub(r'\s+', '', text))


def parse_supply_contract(body_html):
    rows = _Rows(body_html).rows
    # 정정공시는 정정 내역 표 뒤에 정정 후 전체 양식이 붙으므로 본 양식 첫 행부터 읽는다.
    start = next((i for i, row in enumerate(rows)
                  if row and re.match(r'판매.?공급계약구분', _label(row[0]['text']))), None)
    if start is None:
        raise ValueError('단일판매ㆍ공급계약 본문 양식을 찾지 못했습니다.')
    fields = {}
    for row in rows[start:]:
        texts = [cell['text'].strip() for cell in row]
        # 행 머리(예: '2. 계약내역', '5. 계약기간') 다음 칸이 항목명, 그 다음 칸이 값이다.
        for index in range(len(texts) - 1):
            fields.setdefault(_label(texts[index]), texts[index + 1])
    amount = next((_number(fields[key]) for key in ('계약금액(원)', '계약금액총액(원)', '확정계약금액(원)')
                   if key in fields and _number(fields[key]) is not None), None)
    if amount is None:
        raise ValueError('계약금액을 찾지 못했습니다.')
    return {
        'amount': amount,
        'sales_ratio': fields.get('매출액대비(%)'),
        'counterparty': fields.get('계약상대'),
        'start': fields.get('시작일'),
        'end': fields.get('종료일'),
    }


def _eok(amount):
    value = amount / Decimal(100_000_000)
    # 100억 미만은 소수 첫째 자리까지 보여 작은 계약 금액이 0억/1억으로 뭉개지지 않게 한다.
    if abs(value) < 100:
        return f'{value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP):,}억'
    return f'{value.quantize(Decimal(1), rounding=ROUND_HALF_UP):,}억'


def _present(value):
    return value if value and value != '-' else '-'


def format_supply_contract(contract):
    ratio = _present(contract['sales_ratio'])
    lines = [
        f'* 계약금액 : {_eok(contract["amount"])}',
        f'* 매출액대비 : {ratio}%' if ratio != '-' else '* 매출액대비 : -',
        f'* 계약상대 : {_present(contract["counterparty"])}',
        f'* 계약기간 : {_present(contract["start"])}~{_present(contract["end"])}',
    ]
    return html.escape('\n'.join(lines), quote=False)


def fetch_supply_contract_summary(item):
    """실패하면 None을 돌려 기존 알림(제목+링크)으로 보낸다."""
    if not is_supply_contract(item.get('title', '')):
        return None
    receipt = _receipt(item)
    if not receipt:
        return None
    try:
        _, body = fetch_dart_document(build_opener(HTTPCookieProcessor(CookieJar())), receipt)
        return format_supply_contract(parse_supply_contract(body))
    except Exception as error:
        logger.warning('공급계약 공시 보강 실패, 기존 공시 알림 사용: %s', error)
        return None
