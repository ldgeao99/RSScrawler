import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import FinanceDataReader as fdr

from batch_logger import report_batch_run

logger = logging.getLogger("news_logger")

# 기업명/종목코드 동기화는 매일 01:00 KST에 실행한다.
COMPANY_SYNC_RUN_HOUR = 1
COMPANY_SYNC_RUN_MINUTE = 0
COMPANY_STOCK_MAP_FILE = Path('base_info/company_stock_map.json')


def fetch_krx_companies() -> list:
    """KRX 목록에서 종목코드와 기업명을 수집한다. 코드는 문자열로 유지한다."""
    df = fdr.StockListing('KRX')
    companies = {}
    for row in df.to_dict('records'):
        name = row.get('Name')
        if not isinstance(name, str) or not name.strip():
            continue
        code = str(row.get('Code', '')).strip().upper()
        if code.isascii() and code.isdigit():
            code = code.zfill(6)
        # 영문이 포함된 신규 종목코드도 허용한다.
        if not re.fullmatch(r'[0-9A-Z]{6}', code):
            raise ValueError(f'유효하지 않은 KRX 종목코드: {code!r} ({name})')
        company = {'code': code, 'name': name.strip()}
        if code in companies and companies[code] != company:
            raise ValueError(f'KRX 종목코드의 기업명이 중복됩니다: {code}')
        companies[code] = company
    return list(companies.values())


def save_company_stock_map(companies):
    """수집 목록을 원자적으로 교체한다. 키워드 관리 파일과 별도로 보관한다."""
    path = COMPANY_STOCK_MAP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'updated_at': datetime.now(timezone(timedelta(hours=9))).isoformat(),
        'source': 'FinanceDataReader.StockListing(KRX)',
        'companies': companies,
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def fetch_krx_company_names() -> list:
    """KRX(KOSPI+KOSDAQ) 상장 종목 전체의 기업명 리스트를 가져온다."""
    return list(dict.fromkeys(company['name'] for company in fetch_krx_companies()))


def sync_company_kr_keywords(cached_keywords: dict, db_lock, save_keywords_func) -> int:
    """새로 상장된 기업명을 company_kr 키워드 목록에 추가하고, 추가된 건수를 반환한다."""
    companies = fetch_krx_companies()
    krx_names = list(dict.fromkeys(company['name'] for company in companies))

    if not krx_names:
        logger.warning("⚠️ [기업명 동기화] KRX 조회 결과가 비어있어 이번 실행은 건너뜁니다.")
        return 0

    with db_lock:
        # 새 기업명이 없어도 종목명 변경 등을 매일 매칭 파일에 반영한다.
        save_company_stock_map(companies)
        existing = set(cached_keywords.get("company_kr", []))
        new_names = [name for name in krx_names if name not in existing]

        if new_names:
            cached_keywords.setdefault("company_kr", []).extend(new_names)
            save_keywords_func(cached_keywords)
            preview = ", ".join(new_names[:10]) + (" 등" if len(new_names) > 10 else "")
            logger.info(f"🏢 [기업명 동기화] 신규 상장 종목 {len(new_names)}건 추가: {preview}")
        else:
            logger.info("🏢 [기업명 동기화] 신규 추가할 상장 종목 없음 (이미 최신 상태).")

        return len(new_names)


async def daily_company_sync_scheduler(cached_keywords: dict, db_lock, save_keywords_func):
    logger.info("⏰ 일별 상장사 기업명 동기화 스케줄러가 독립 모듈에서 가동되었습니다.")

    while True:
        kst_tz = timezone(timedelta(hours=9))
        now = datetime.now(kst_tz)

        target_today = datetime(
            now.year, now.month, now.day,
            COMPANY_SYNC_RUN_HOUR, COMPANY_SYNC_RUN_MINUTE, 0,
            tzinfo=kst_tz
        )
        target = target_today if now < target_today else target_today + timedelta(days=1)
        seconds_until_run = (target - now).total_seconds()

        logger.info(f"⏳ 다음 기업명 동기화 배치까지 대기: {seconds_until_run / 60:.1f}분 후 ({target.strftime('%Y-%m-%d %H:%M:%S')} KST)")
        await asyncio.sleep(seconds_until_run)

        next_run_at = target + timedelta(days=1)
        try:
            # fdr.StockListing은 동기/블로킹 호출이라 이벤트 루프를 막지 않도록 스레드로 위임
            added = await asyncio.to_thread(sync_company_kr_keywords, cached_keywords, db_lock, save_keywords_func)
            report_batch_run("company_sync", "상장사 기업명 동기화", next_run_at, True,
                             f"기업명/종목코드 동기화 처리, 신규 기업명 {added}건 추가")
        except Exception as e:
            logger.error(f"❌ [기업명 동기화 스케줄러 에러] 정기 실행 중 치명적 오류 발생: {e}")
            report_batch_run("company_sync", "상장사 기업명 동기화", next_run_at, False, f"오류 발생: {e}")
            await asyncio.sleep(10)  # 루프 파괴 방지용 유예 코드


# 수동 재실행/백필용: python3 company_list_sync_scheduler.py
# (반드시 main_fastapi.py가 있는 프로젝트 루트에서 실행해야 함)
if __name__ == "__main__":
    import main_fastapi as mf

    before = len(mf.cached_keywords.get("company_kr", []))
    added = sync_company_kr_keywords(mf.cached_keywords, mf.db_lock, mf.save_keywords)
    after = len(mf.cached_keywords.get("company_kr", []))
    print(f"before: {before}, added: {added}, after: {after}")
