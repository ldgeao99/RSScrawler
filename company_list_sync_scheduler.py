import asyncio
import logging
from datetime import datetime, timezone, timedelta

import FinanceDataReader as fdr

logger = logging.getLogger("news_logger")

# 다른 두 스케줄러(00:00 백업, 00:15 일정추출)와 겹치지 않게 01:00 KST로 잡는다.
COMPANY_SYNC_RUN_HOUR = 1
COMPANY_SYNC_RUN_MINUTE = 0


def fetch_krx_company_names() -> list:
    """KRX(KOSPI+KOSDAQ) 상장 종목 전체의 기업명 리스트를 가져온다."""
    df = fdr.StockListing('KRX')
    names = [str(n).strip() for n in df['Name'].dropna().tolist() if str(n).strip()]
    return names


def sync_company_kr_keywords(cached_keywords: dict, db_lock, save_keywords_func) -> int:
    """새로 상장된 기업명을 company_kr 키워드 목록에 추가하고, 추가된 건수를 반환한다."""
    try:
        krx_names = fetch_krx_company_names()
    except Exception as e:
        logger.error(f"❌ [기업명 동기화] KRX 상장 종목 목록 조회 실패: {e}")
        return 0

    if not krx_names:
        logger.warning("⚠️ [기업명 동기화] KRX 조회 결과가 비어있어 이번 실행은 건너뜁니다.")
        return 0

    with db_lock:
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

        try:
            # fdr.StockListing은 동기/블로킹 호출이라 이벤트 루프를 막지 않도록 스레드로 위임
            await asyncio.to_thread(sync_company_kr_keywords, cached_keywords, db_lock, save_keywords_func)
        except Exception as e:
            logger.error(f"❌ [기업명 동기화 스케줄러 에러] 정기 실행 중 치명적 오류 발생: {e}")
            await asyncio.sleep(10)  # 루프 파괴 방지용 유예 코드


# 수동 재실행/백필용: python3 company_list_sync_scheduler.py
# (반드시 main_fastapi.py가 있는 프로젝트 루트에서 실행해야 함)
if __name__ == "__main__":
    import main_fastapi as mf

    before = len(mf.cached_keywords.get("company_kr", []))
    added = sync_company_kr_keywords(mf.cached_keywords, mf.db_lock, mf.save_keywords)
    after = len(mf.cached_keywords.get("company_kr", []))
    print(f"before: {before}, added: {added}, after: {after}")
