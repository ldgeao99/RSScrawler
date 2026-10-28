"""기업공시 일별 백업: 날짜별 보관, 최근 이틀 라이브 데이터 유지."""
import asyncio
import json
import logging
import os
from collections import defaultdict
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

from batch_logger import report_batch_run

logger = logging.getLogger('news_logger')
BACKUP_DIR = Path('news_back_up')
KST = timezone(timedelta(hours=9))


def _read(path):
    # 읽기 오류가 있는 파일을 빈 데이터로 취급하여 덮어쓰지 않는다.
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, list):
        raise ValueError(f'{path}: JSON 배열이 필요합니다.')
    return data


def _write(path, data):
    temporary = path.with_name(path.name + '.tmp')
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _item_date(item):
    try:
        return datetime.fromtimestamp(float(item['timestamp']), KST).date()
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        return None


def execute_midnight_processing(db_lock, file_paths, memory_caches):
    today = datetime.now(KST).date()
    yesterday = today - timedelta(days=1)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with db_lock:
        for key in ('stream', 'filtered', 'blacklisted'):
            path = Path(file_paths[key])
            data = _read(path)
            grouped = defaultdict(list)
            for item in data:
                day = _item_date(item)
                if day is not None and day < today:
                    grouped[day].append(item)
            # 최초 실행 전 누적된 과거 데이터도 날짜별로 모두 백업한다.
            for day, items in sorted(grouped.items()):
                backup = BACKUP_DIR / f'disclosure_news_list_{key}_backup_{day:%y%m%d}.json'
                existing = _read(backup)
                seen = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in existing}
                for item in items:
                    identity = json.dumps(item, sort_keys=True, ensure_ascii=False)
                    if identity not in seen:
                        existing.append(item)
                        seen.add(identity)
                _write(backup, existing)
                logger.info('💾 [기업공시] %s (%s건)', backup.name, len(existing))
            # 재시작 후 복구할 수 있도록 오늘/어제 데이터는 디스크에도 남긴다.
            # 날짜를 판단할 수 없는 데이터는 삭제하지 않는다.
            keep = lambda item: _item_date(item) is None or _item_date(item) >= yesterday
            _write(path, [item for item in data if keep(item)])
            memory_caches[key][:] = [item for item in memory_caches[key] if keep(item)]
    logger.info('🧹 [기업공시] 일별 백업 및 최근 이틀 데이터 정리 완료')


async def daily_backup_and_cleanup_scheduler(db_lock, file_paths, memory_caches):
    logger.info('⏰ [기업공시] 매일 00:00 KST 백업 스케줄러 시작')
    while True:
        now = datetime.now(KST)
        tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), KST)
        await asyncio.sleep((tomorrow - now).total_seconds())
        next_run = tomorrow + timedelta(days=1)
        try:
            await asyncio.to_thread(execute_midnight_processing, db_lock, file_paths, memory_caches)
            report_batch_run('disclosure_backup_cleanup', '기업공시 백업 및 메모리 정리', next_run, True, '백업 및 정리 완료')
        except Exception as error:
            logger.exception('[기업공시] 백업 실패')
            report_batch_run('disclosure_backup_cleanup', '기업공시 백업 및 메모리 정리', next_run, False, str(error))
            await asyncio.sleep(10)


def next_earnings_schedule_run(now):
    """현재 시각 이후의 월~금 22:00 KST를 반환한다."""
    now = now.astimezone(KST)
    target = now.replace(hour=22, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target


async def weekday_earnings_schedule_scheduler(db_lock, file_paths):
    logger.info('⏰ [기업공시] 월~금 22:00 KST 실적발표 일정 등록 스케줄러 시작')
    while True:
        now = datetime.now(KST)
        target = next_earnings_schedule_run(now)
        await asyncio.sleep((target - now).total_seconds())
        next_run = next_earnings_schedule_run(datetime.now(KST))
        try:
            message = await asyncio.to_thread(process_earnings_schedules, file_paths, db_lock)
            report_batch_run('disclosure_earnings_schedules', '공시 실적발표 일정 등록', next_run, True, message)
        except Exception as error:
            logger.exception('[기업공시] 실적발표 일정 등록 실패')
            report_batch_run('disclosure_earnings_schedules', '공시 실적발표 일정 등록', next_run, False, str(error))


def process_earnings_schedules(file_paths, db_lock=None):
    from disclosure_schedule_extraction import extract_disclosure_schedules
    items = []
    # 파일 읽기만 잠그고, DART/Firestore/Telegram 네트워크 작업은 잠금 밖에서 수행한다.
    with db_lock if db_lock is not None else nullcontext():
        # 백업 전체를 포함하여 이전 배치의 등록 실패도 다음 실행에 재시도한다.
        for path in list(BACKUP_DIR.glob('disclosure_news_list_*_backup_*.json')) + [Path(p) for p in file_paths.values()]:
            items.extend(_read(path))
    return extract_disclosure_schedules(items)
