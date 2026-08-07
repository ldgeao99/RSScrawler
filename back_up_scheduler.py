import os
import json
import time
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

from batch_logger import report_batch_run

logger = logging.getLogger("news_logger")

# ====================================================
# 📂 [⚙️ 자동 백업 및 메모리 디톡스 정책 상수 설정]
# ====================================================
BACKUP_DIR = Path("news_back_up")  # 백업 파일이 저장될 상대 경로

# 백업 디렉토리가 없으면 즉시 생성
BACKUP_DIR.mkdir(parents=True, exist_ok=True)


async def daily_backup_and_cleanup_scheduler(db_lock, file_paths, memory_caches, load_content_func, print_status_func):
    """
    매일 00:00:00 KST를 계산하여 대기 후, 자정이 되면 백업 및
    디스크 파일 초기화, 메모리 디톡스를 수행합니다.
    """
    logger.info("⏰ 일별 자동 백업 및 청소 스케줄러가 독립 모듈에서 가동되었습니다.")

    while True:
        # KST(한국 표준시) 기준으로 현재 시간 계산
        kst_tz = timezone(timedelta(hours=9))
        now = datetime.now(kst_tz)

        # 다음 날 00:00:00 KST 계산
        tomorrow_date = now.date() + timedelta(days=1)
        tomorrow = datetime(tomorrow_date.year, tomorrow_date.month, tomorrow_date.day, 0, 0, 0, tzinfo=kst_tz)
        seconds_until_midnight = (tomorrow - now).total_seconds()

        # 자정 정각이 될 때까지 비동기 슬립 대기
        await asyncio.sleep(seconds_until_midnight)

        next_run_at = tomorrow + timedelta(days=1)
        try:
            execute_midnight_processing(db_lock, file_paths, memory_caches, load_content_func, print_status_func)
            report_batch_run("backup_cleanup", "백업 및 메모리 정리", next_run_at, True, "정상 완료")
        except Exception as e:
            logger.error(f"❌ [백업 에러] 자정 백업 및 정리 작업 중 치명적 오류 발생: {e}")
            report_batch_run("backup_cleanup", "백업 및 메모리 정리", next_run_at, False, f"오류 발생: {e}")
            await asyncio.sleep(10)  # 루프 파괴 방지용 유예 코드


def execute_midnight_processing(db_lock, file_paths, memory_caches, load_content_func, print_status_func):
    """
    스레드 락(db_lock)을 획득하여 안전하게 디스크 파일을 분리 백업 및 초기화하고,
    메모리 내부에서 오늘과 어제 날짜가 아닌 과거 기사들을 달력 기준으로 격리 제거합니다.
    """
    kst_tz = timezone(timedelta(hours=9))
    now = datetime.now(kst_tz)

    # '어제' 날짜 정보 추출 및 파일명 포맷팅 (yyMMdd)
    yesterday = now - timedelta(days=1)
    date_str = yesterday.strftime("%y%m%d")

    # 어제 00:00:00 KST ~ 23:59:59 KST 타임스탬프 범위 지정 (백업용 기존 로직 유지)
    yesterday_start_dt = datetime(yesterday.year, yesterday.month, yesterday.day, 0, 0, 0, tzinfo=kst_tz)
    yesterday_start = yesterday_start_dt.timestamp()

    yesterday_end_dt = datetime(yesterday.year, yesterday.month, yesterday.day, 23, 59, 59, tzinfo=kst_tz)
    yesterday_end = yesterday_end_dt.timestamp()

    # 데이터 수집 스레드와의 충돌을 막기 위해 락 획득
    with db_lock:
        logger.info(f"🔒 [자정 백업 시작] 공유 리소스 동기화 잠금을 획득했습니다.")

        # --------------------------------------------------
        # 💾 [기존 유지] 1~4번 영역: 어제 일자 디스크 백업 및 초기화
        # --------------------------------------------------
        for key in ["stream", "filtered", "blacklisted"]:
            filepath = file_paths[key]

            # 1. 메인에서 넘겨받은 로드 함수로 기존 디스크 전체 데이터 로드
            file_data = load_content_func(filepath)

            if file_data:
                # 2. 어제 범위(00:00:00 ~ 23:59:59) 데이터만 필터링하여 백업 타겟팅
                yesterday_data = [
                    item for item in file_data
                    if yesterday_start <= int(item.get("timestamp", 0)) <= yesterday_end
                ]

                # 3. 'news_back_up' 폴더에 분리 저장
                if yesterday_data:
                    backup_filename = BACKUP_DIR / f"news_list_{key}_backup_{date_str}.json"
                    with open(backup_filename, "w", encoding="utf-8") as f:
                        json.dump(yesterday_data, f, ensure_ascii=False, indent=4)
                    logger.info(f"💾 {filepath} -> {backup_filename.name} ({len(yesterday_data):,d}건) 분리 백업 완료.")
                else:
                    logger.info(f"ℹ️ 어제 날짜({date_str})에 해당되는 데이터가 디스크 파일에 없어 백업본을 생성하지 않습니다.")

            # 4. 라이브 디스크 파일 비우기
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump([], f)
            logger.info(f"🗑️ 라이브 디스크 파일 [{filepath}] 비우기 완료 (가상 메모리 데이터 보호 상태).")

        # --------------------------------------------------
        # 🧹 [새로 추가] 5번 영역: 가상 메모리(RAM) 달력 기준 디톡스
        # --------------------------------------------------
        # 자정 직후 시점 기준 보존할 달력 일자 정의 (자정이 넘었으므로 now.date()는 이미 새로운 '오늘'임)
        today_date = now.date()
        yesterday_date = yesterday.date()

        def is_valid_date(item_time_str):
            """기사의 time_kst ('YYYY-MM-DD HH:M:S') 문자열을 검증하여 오늘/어제이면 True 반환"""
            try:
                if not item_time_str:
                    return False
                # 앞자리 'YYYY-MM-DD' 문자열만 분리하여 비교
                date_part = item_time_str.split(" ")[0]
                item_date = datetime.strptime(date_part, "%Y-%m-%d").date()
                return item_date in [today_date, yesterday_date]
            except Exception:
                # 시간 형식이 비정상적이거나 파싱 실패 시 안전을 위해 제거
                return False

        # 메모리 참조 주소 무결성([:])을 보존하면서 오늘/어제 날짜가 아닌 과거 기사만 영구 제거
        memory_caches["stream"][:] = [item for item in memory_caches["stream"] if is_valid_date(item.get("time_kst"))]
        memory_caches["filtered"][:] = [item for item in memory_caches["filtered"] if is_valid_date(item.get("time_kst"))]
        memory_caches["blacklisted"][:] = [item for item in memory_caches["blacklisted"] if is_valid_date(item.get("time_kst"))]

        logger.info(f"🧹 자정 가상 메모리(RAM) 달력 일자 기준 청소 완료 (오늘: {today_date} / 어제: {yesterday_date} 데이터만 보존).")
        print_status_func()

    logger.info(f"🔓 [자정 백업 종료] 공유 리소스 잠금을 해제했습니다.\n")