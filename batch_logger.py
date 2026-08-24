import os
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("news_logger")

# ==========================================================
# 🔥 [배치 실행 기록] Firestore(batch_logs) 연동 설정
# - firestore.rules가 강화되어 인증 없는 REST 접근(읽기/쓰기)이 모두 403으로 막힌다.
#   그래서 schedule_extraction_scheduler.py와 동일하게 서비스 계정 키(Admin SDK)로 인증한다.
#   Admin SDK는 보안 규칙을 우회하므로 규칙 변경과 무관하게 항상 읽고 쓸 수 있다.
# - 대시보드도 Firestore를 직접 읽지 않고 main_fastapi의 /api/batch-logs(→ get_batch_logs)를 경유한다.
# ==========================================================
FIREBASE_PROJECT_ID = "stockcalender-13042"
BATCH_LOGS_COLLECTION = "batch_logs"
FIREBASE_KEY_PATH = "stockcalender-13042-firebase-adminsdk-fbsvc-18b1748d9a.json"

KST = timezone(timedelta(hours=9))

# 배치별 표시 순서(대시보드 정렬용). 스케줄러 실행 순서 그대로.
BATCH_ORDER = {
    "backup_cleanup": 0,
    "schedule_extraction": 1,
    "company_sync": 2,
}

_firestore_db = None
_firestore_init_done = False


def _get_db():
    """Admin SDK Firestore 클라이언트를 지연 생성한다. 키가 없으면 None을 반환하고 이후 호출은 스킵된다."""
    global _firestore_db, _firestore_init_done
    if _firestore_init_done:
        return _firestore_db
    _firestore_init_done = True
    try:
        if not os.path.exists(FIREBASE_KEY_PATH):
            logger.warning(f"⚠️ [배치 로그] 파이어베이스 인증 파일({FIREBASE_KEY_PATH})이 없어 batch_logs 연동이 스킵됩니다.")
            return None
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", FIREBASE_KEY_PATH)
        from google.cloud import firestore
        _firestore_db = firestore.Client(project=FIREBASE_PROJECT_ID)
    except Exception as e:
        logger.warning(f"⚠️ [배치 로그] Firestore 클라이언트 초기화 실패: {e}")
        _firestore_db = None
    return _firestore_db


def report_batch_run(batch_id: str, batch_name: str, next_run_at: datetime, success: bool, message: str = ""):
    """
    스케줄러 실행 결과를 Firestore batch_logs 컬렉션에 upsert한다.
    문서 ID를 batch_id로 고정해서, 배치별로 항상 '가장 최근 실행 결과' 1건만 남긴다.
    """
    db = _get_db()
    if db is None:
        return

    now_kst = datetime.now(KST)
    doc = {
        "batchName": batch_name or "",
        "lastRunAt": now_kst.strftime("%Y-%m-%d %H:%M:%S"),
        "nextRunAt": next_run_at.strftime("%Y-%m-%d %H:%M:%S") if next_run_at else "",
        "success": bool(success),
        "message": (message or "")[:500],
    }

    try:
        db.collection(BATCH_LOGS_COLLECTION).document(batch_id).set(doc)
        logger.info(f"🗂️ [배치 로그] '{batch_name}' 실행 결과 기록 완료 ({'성공' if success else '실패'})")
    except Exception as e:
        logger.warning(f"⚠️ [배치 로그] Firestore 기록 에러: {e}")


def get_batch_logs():
    """
    batch_logs 컬렉션 전체를 읽어 대시보드용 정규화 리스트로 반환한다.
    배치 실행 순서(BATCH_ORDER)대로 정렬하고, 알려지지 않은 배치는 뒤에 붙인다.
    Firestore 접근 불가/실패 시 빈 리스트를 반환한다.
    """
    db = _get_db()
    if db is None:
        return []

    try:
        batches = []
        for snap in db.collection(BATCH_LOGS_COLLECTION).stream():
            d = snap.to_dict() or {}
            batches.append({
                "id": snap.id,
                "batchName": d.get("batchName") or snap.id,
                "lastRunAt": d.get("lastRunAt") or "",
                "nextRunAt": d.get("nextRunAt") or "",
                "success": bool(d.get("success")),
                "message": d.get("message") or "",
            })
        batches.sort(key=lambda b: BATCH_ORDER.get(b["id"], 99))
        return batches
    except Exception as e:
        logger.warning(f"⚠️ [배치 로그] Firestore 조회 에러: {e}")
        return []
