import logging
from datetime import datetime, timezone, timedelta

import requests

logger = logging.getLogger("news_logger")

# ==========================================================
# 🔥 [배치 실행 기록] Firestore(batch_logs) 연동 설정
# - schedule_extraction_scheduler.py의 push_schedules_to_firestore()와 동일한 방식으로,
#   서비스 계정 키 없이 REST API로 직접 쓴다 (firestore.rules가 공개 상태).
# ==========================================================
FIREBASE_PROJECT_ID = "stockcalender-13042"
BATCH_LOGS_COLLECTION = "batch_logs"
FIRESTORE_BATCH_LOGS_URL = (
    f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
    f"/databases/(default)/documents/{BATCH_LOGS_COLLECTION}"
)

KST = timezone(timedelta(hours=9))


def _to_firestore_value(value):
    if isinstance(value, bool):
        return {"booleanValue": value}
    return {"stringValue": value if value is not None else ""}


def report_batch_run(batch_id: str, batch_name: str, next_run_at: datetime, success: bool, message: str = ""):
    """
    스케줄러 실행 결과를 Firestore batch_logs 컬렉션에 upsert한다.
    문서 ID를 batch_id로 고정해서, 배치별로 항상 '가장 최근 실행 결과' 1건만 남긴다
    (대시보드는 컬렉션 전체 이력이 아니라 이 3개 문서만 조회).
    """
    now_kst = datetime.now(KST)
    payload = {
        "fields": {
            "batchName": _to_firestore_value(batch_name),
            "lastRunAt": _to_firestore_value(now_kst.strftime("%Y-%m-%d %H:%M:%S")),
            "nextRunAt": _to_firestore_value(next_run_at.strftime("%Y-%m-%d %H:%M:%S") if next_run_at else ""),
            "success": _to_firestore_value(bool(success)),
            "message": _to_firestore_value((message or "")[:500]),
        }
    }

    try:
        resp = requests.patch(f"{FIRESTORE_BATCH_LOGS_URL}/{batch_id}", json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info(f"🗂️ [배치 로그] '{batch_name}' 실행 결과 기록 완료 ({'성공' if success else '실패'})")
        else:
            logger.warning(f"⚠️ [배치 로그] Firestore 기록 실패 ({resp.status_code}): {resp.text[:150]}")
    except Exception as e:
        logger.warning(f"⚠️ [배치 로그] Firestore 요청 에러: {e}")
