import asyncio
import hashlib
import json
import logging
import os
import re
import urllib.request
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import AsyncOpenAI
from google import genai
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from cost_tracker import CostTracker

# ==========================================================
# ⚙️ [테스트 설정] 파일 경로 및 상수 지정
# ==========================================================
INPUT_BACKUP_FILE = "news_back_up/news_list_filtered_backup_260731.json"
OUTPUT_SCHEDULE_FILE = "extracted_schedule/extracted_schedules_260731.json"  # 최종 3단계 결과 저장 파일

# ==========================================================
# 🔥 Firebase(Firestore) 연동 설정
# - firestore.rules가 'allow read, write: if true'로 완전 공개돼 있어
#   (FirebaseStockCalendar/public/index.html의 클라이언트 SDK 쓰기와 동일한 방식)
#   서비스 계정 키 없이 REST API로 직접 쓴다.
# ==========================================================
FIREBASE_PROJECT_ID = "stockcalender-13042"
TEMP_EVENTS_COLLECTION = "temp_events"
FIRESTORE_BASE_URL = (
    f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
    f"/databases/(default)/documents/{TEMP_EVENTS_COLLECTION}"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("news_logger")

# 🔇 내부 통신 로그 숨기기 (OpenAI, Gemini 등 외부 라이브러리 로그 노이즈 차단)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("google").setLevel(logging.WARNING)
logging.getLogger("google.genai").setLevel(logging.WARNING)  # 👈 이 줄을 추가하면 해당 WARNING 로그가 완벽히 차단됩니다.
# thinking_config 미지정 시 응답에 섞이는 'non-text parts(thought_signature)' 안내성 경고 숨기기
# (실제 로거 이름이 위 'google.genai'와 다른 'google_genai.types'라 별도로 잡아야 함 - 기능엔 영향 없는 정보성 로그)
logging.getLogger("google_genai.types").setLevel(logging.ERROR)
# 'AFC is enabled with max remote calls: 10.' 같은 노이즈 안내 로그 숨기기 (function calling 미사용 - 무관)
logging.getLogger("google_genai.models").setLevel(logging.WARNING)

load_dotenv()


# ==========================================================
# 🧠 [순서 중요] 3단계용 스키마 정의 (함수보다 '무조건' 위에 위치해야 함)
# ==========================================================
class ScheduleSchema(BaseModel):
    event_title: str = Field(description="일정 핵심 제목")
    exact_date: str = Field(
        description=(
            "일정의 시점. 아래 셋 중 하나의 형식으로만 출력한다 (요일 표기 금지):\n"
            "1) 정확한 날짜를 알 때: 'YYYY-MM-DD'\n"
            "2) 월까지만(또는 월 범위로만) 알 때: 'YYYY-MM'\n"
            "3) 연도만 알 때: 'YYYY'"
        )
    )
    details: str = Field(description="배경 및 상세 내용 요약 (2문장 이내)")


DATE_FULL_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
DATE_YEAR_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
DATE_YEAR_ONLY_RE = re.compile(r"^(\d{4})$")


def normalize_schedule_date(event_title: str, exact_date: str) -> tuple:
    """
    모델이 형식 규칙을 놓쳤을 때를 대비한 방어적 정규화.
    - 'YYYY-MM-DD' (+ 요일 텍스트 제거)는 그대로 통과
    - 'YYYY-MM'(월만 아는 경우) -> event_title에 '[MM월 미정]' 접두, exact_date는 'YYYY-MM-01'
    - 'YYYY'(연도만 아는 경우) -> event_title에 '[YYYY년 미정]' 접두, exact_date는 'YYYY-01-01'
    """
    if not exact_date:
        return event_title, exact_date

    raw = re.sub(r"\(.*?\)", "", exact_date).strip()  # 요일 표기 등 괄호 제거

    if DATE_FULL_RE.match(raw):
        return event_title, raw

    m = DATE_YEAR_MONTH_RE.match(raw)
    if m:
        year, month = m.groups()
        return f"[{month}월 미정] {event_title}", f"{year}-{month}-01"

    m = DATE_YEAR_ONLY_RE.match(raw)
    if m:
        year = m.group(1)
        return f"[{year}년 미정] {event_title}", f"{year}-01-01"

    # 모델이 규칙을 따르지 않은 값은 원본 그대로 보존 (후속 검토용)
    return event_title, exact_date


# ==========================================================
# 🔥 [최종 단계] Firestore(temp_events) 업로드
# ==========================================================
def _make_temp_event_doc_id(event_name: str, date_str: str) -> str:
    """이벤트명+날짜 기반 결정론적 ID -> 같은 일정을 재실행해도 중복 저장되지 않고 덮어써짐(upsert)."""
    raw = f"{event_name}|{date_str}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _to_firestore_value(value):
    if isinstance(value, bool):
        return {"booleanValue": value}
    return {"stringValue": value or ""}


def push_schedules_to_firestore(schedule_results: list):
    """
    events 컬렉션과 동일한 필드 구성(date, category, isImportant, eventName, detail,
    relatedStocks, url)으로 매핑해 temp_events 컬렉션에 upsert한다.
    (events가 아니라 temp_events에 넣어 검수 후 수동으로 events에 반영하는 스테이징 컬렉션으로 사용)
    """
    success, failed, skipped = 0, 0, 0

    for news in schedule_results:
        event_name = news.get("extracted_event") or news.get("title", "")
        date_str = news.get("exact_date", "")

        if not event_name or not DATE_FULL_RE.match(date_str or ""):
            skipped += 1
            continue

        doc_id = _make_temp_event_doc_id(event_name, date_str)
        payload = {
            "fields": {
                "date": _to_firestore_value(date_str),
                "category": _to_firestore_value("일반"),
                "isImportant": _to_firestore_value(False),
                "eventName": _to_firestore_value(event_name),
                "title": _to_firestore_value(news.get("title", "")),
                "detail": _to_firestore_value(news.get("details", "")),
                "relatedStocks": _to_firestore_value(news.get("relatedStocks", "")),
                "url": _to_firestore_value(news.get("link", "")),
                # events의 'date'(일정 발생일)와는 별개로, 이 일정이 언제 수집(등록)됐는지를 남겨
                # temp_events 검수 화면에서 등록일 순으로 정렬/구분할 수 있게 한다.
                "articleDate": _to_firestore_value(news.get("time_kst", "")),
            }
        }

        try:
            resp = requests.patch(f"{FIRESTORE_BASE_URL}/{doc_id}", json=payload, timeout=10)
            if resp.status_code == 200:
                success += 1
            else:
                failed += 1
                logger.warning(f"⚠️ Firestore 저장 실패 ({resp.status_code}): {event_name[:30]} - {resp.text[:150]}")
        except Exception as e:
            failed += 1
            logger.warning(f"⚠️ Firestore 요청 에러 ({event_name[:30]}): {e}")

    logger.info(
        f"🔥 [Firestore 업로드] '{TEMP_EVENTS_COLLECTION}' 컬렉션 반영 완료: "
        f"성공 {success}건 / 실패 {failed}건 / 형식 불량 스킵 {skipped}건"
    )


OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

MAX_CONCURRENT_REQUESTS = 10
SIMILARITY_THRESHOLD = 60  # 뉴스 제목 간의 중복 판정 임계값

# 💰 [예산 방어선] 월 5,000원 목표 기준 하루 상한 (Stage3가 비용 대부분을 차지하므로 여기만 캡 걸어도 충분)
MAX_STAGE3_ITEMS_PER_DAY = 100

progress_tracker = {"completed": 0, "total": 0}


# ==========================================================
# 🛠️ [0단계] 정규화 및 [1단계] 중복 제거
# ==========================================================
def normalize_title(title: str) -> str:
    if not title:
        return ""
    text = title.strip()
    prefix_keywords = ["속보", "단독", "상보", "기습", "특징주"]
    for kw in prefix_keywords:
        if text.startswith(kw):
            text = text[len(kw):].strip()
    text = re.sub(r"\[.*?\]|\(.*?\)", "", text)
    text = re.sub(r"[^\w\s]", "", text)
    return " ".join(text.split()).lower()


def process_duplicate_purge_pipeline(raw_news_list: list) -> list:
    total_raw = len(raw_news_list)
    logger.info(f"🔄 [1단계] 교차 중복 제거 가동 - 원본 데이터: {total_raw}건")
    python_purged = []
    exact_seen = set()
    dup_count = 0

    for idx, news in enumerate(raw_news_list, 1):
        title = news.get("title", "").strip()
        if idx % 50 == 0 or idx == total_raw:
            logger.info(f"⏳ [중복 제거] {idx}/{total_raw} 건 처리 중...")

        if title in exact_seen:
            dup_count += 1
            continue
        exact_seen.add(title)

        normalized = normalize_title(title)
        is_duplicated = False

        for exist in python_purged:
            score = fuzz.token_sort_ratio(normalized, exist.get("_temp_normalized"))
            if score >= SIMILARITY_THRESHOLD:
                is_duplicated = True
                dup_count += 1
                break

        if is_duplicated:
            continue

        news["_temp_normalized"] = normalized
        python_purged.append(news)

    for news in python_purged:
        news.pop("_temp_normalized", None)

    # 📊 1단계 압축 통계 연산
    stage1_count = len(python_purged)
    stage1_compression = (dup_count / total_raw) * 100 if total_raw > 0 else 0
    logger.info(
        f"🏁 [1단계 완료] 중복 제거 결과: {total_raw}건 ➔ {stage1_count}건 (총 {dup_count}건 제외, {stage1_compression:.1f}% 압축됨)")
    return python_purged


# ==========================================================
# 🧠 [2단계] OpenAI 제목 스캔 엔진 (비용 방어를 위해 선별 조건 강화 - Stage3 유입량 억제 목적)
# ==========================================================
OPENAI_MODEL = "gpt-4o-mini"
GEMINI_MODEL = "models/gemini-3.5-flash-lite"


async def check_title_schedule_openai(client: AsyncOpenAI, semaphore: asyncio.Semaphore, news: dict,
                                       cost_tracker: CostTracker) -> dict or None:
    title = news.get("title", "")

    # 💡 Stage3(본문 추출, 비용 대부분 차지)로 넘어가는 물량을 줄이기 위해
    # "애매하면 통과"가 아니라 "구체적 시점/일정 단서가 제목에 실제로 있어야 통과"로 기준을 좁힘
    system_prompt = (
        "너는 뉴스 제목을 스캔하여 '구체적인 미래 일정이나 이벤트'가 실제로 언급된 기사만 골라내는 정밀 필터다.\n"
        "애매하면 통과시키지 말고 False로 제외해라. 후속 본문 분석 비용이 비싸므로 확신이 없으면 걸러내는 쪽으로 판단해라.\n\n"
        "[★ TRUE (구체적 시점/이벤트 단서가 제목에 명시된 경우만)]\n"
        "1. 명확한 날짜·기간이 박힌 일정: '~일 발표', '~월 출시', '실적발표 D-3', '공모 청약 시작', '임상 결과 발표 예정'\n"
        "2. 확정된 미래 이벤트: 'M&A 체결 예정', '공장 준공식', '학회 발표 확정', '국책과제 선정 발표'\n\n"
        "[★ FALSE (기본값 - 조금이라도 모호하면 여기)]\n"
        "1. 당일 주가 등락 중계: '특징주', '급등', '폭락', '상한가', '반등 성공', '외인 매수', '기관 매도'\n"
        "2. 완전히 끝난 과거 사실: '작년 매출 100억 달성', '지분 취득 완료'\n"
        "3. 시점 표현이 모호한 일반론: '조만간', '연내', '하반기', '앞으로', '차세대', '미래' (구체적 날짜/이벤트명이 없으면 제외)\n\n"
        "응답 포맷 (JSON): {\"is_likely_schedule\": true 또는 false}"
    )

    async with semaphore:
        try:
            response = await client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"뉴스 제목: {title}"}
                ],
                response_format={"type": "json_object"},
                temperature=0.0
            )

            usage = getattr(response, "usage", None)
            if usage:
                cost_tracker.add_usage(
                    "2단계_제목필터(OpenAI)", OPENAI_MODEL,
                    usage.prompt_tokens, usage.completion_tokens
                )

            result_json = json.loads(response.choices[0].message.content)
            is_likely_schedule = result_json.get("is_likely_schedule", False)
            news["schedule_reason"] = "일정유망주" if is_likely_schedule else "해당없음"

            return news if is_likely_schedule is True else None
        except Exception as e:
            logger.error(f"❌ OpenAI API 에러 ({title[:15]}...): {e}")
            return None
        finally:
            progress_tracker["completed"] += 1
            done = progress_tracker["completed"]
            total = progress_tracker["total"]
            if done % 10 == 0 or done == total:
                logger.info(f"░ [2단계 진행률] {done}/{total} ({(done / total) * 100 :.1f}%) 선별 중...")


# ==========================================================
# 🌐 [중간 다리] 웹 기사 본문 스크래퍼 구동 함수
# ==========================================================
def scrape_news_body(url: str) -> str:
    """기사 URL을 읽어와 순수 본문 텍스트만 추출합니다."""
    if not url or not url.startswith("http"):
        return ""
    try:
        req = urllib.request.Request(
            url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            html = response.read().decode('utf-8', errors='ignore')

        soup = BeautifulSoup(html, "html.parser")

        # 언론사 페이지의 무관한 태그 미리 제거
        for s in soup(["script", "style", "nav", "header", "footer", "aside"]):
            s.extract()

        # 일반적인 본문 컨테이너 텍스트 추출 (없으면 전체 텍스트)
        article_body = soup.find("div", id="articleBody") or soup.find("article") or soup.find("div",
                                                                                               class_="article_body")
        if article_body:
            text = article_body.get_text(separator=" ")
        else:
            text = soup.get_text(separator=" ")

        # 공백 정리
        cleaned_text = " ".join(text.split())
        return cleaned_text[:2500]  # 상한선 축소 (일정/날짜 정보는 대개 기사 도입부에 위치 - 비용 절감)
    except Exception as e:
        logger.warning(f"⚠️ 본문 크롤링 실패 ({url}): {e}")
        return ""


# ==========================================================
# 🧠 [3단계] Google Gemini 본문 정밀 추출 엔진
# ==========================================================
async def extract_schedule_from_body_gemini(gemini_client: genai.Client, news: dict,
                                             cost_tracker: CostTracker) -> dict:
    url = news.get("link", "")
    title = news.get("title", "")
    publish_date = (news.get("time_kst") or "").split(" ")[0]  # 'YYYY-MM-DD' (상대 시점 표현 해석 기준일)
    publish_year = publish_date[:4] if publish_date else None

    # 파이썬 크롤러로 본문 선확보
    body_text = await asyncio.to_thread(scrape_news_body, url)

    if not body_text or len(body_text) < 100:
        body_text = f"기사 제목: {title} (본문 수집 실패로 제목 기반으로 추출 바랍니다.)"

    n_year_rule = (
        f"4) 제목이나 본문에 '3년 내', '5년 이내'처럼 'N년 내/이내'라는 표현이 있을 때 -> "
        f"발행연도({publish_year or '알 수 없음'}) + N을 계산한 연도에 '-01'을 붙여 'YYYY-01' 형식으로 출력해라 "
        "(구체적 월/일은 모르는 것으로 취급).\n"
        if publish_year else
        "4) 제목이나 본문에 'N년 내/이내'라는 표현이 있을 때 -> 기사 내 다른 연도 단서로 발행연도를 추정한 뒤 "
        "그 연도 + N을 계산해 'YYYY-01' 형식으로 출력해라.\n"
    )

    gemini_prompt = (
        "너는 뉴스 본문 전체를 읽고 핵심적인 '향후 미래 일정 및 예고된 이벤트'를 구조화 데이터로 뽑아내는 데이터 엔지니어다.\n"
        "본문 안의 광고, 기자 메일 등 노이즈는 완전히 배제하고 오직 미래 일정 정보에만 집중해라.\n\n"
        f"이 기사의 발행일은 '{publish_date or '알 수 없음'}'이다. '내년', '다음달', '이번 분기' 같은 상대적 시점 표현은 "
        "반드시 이 발행일을 기준으로 절대 날짜로 환산해라.\n\n"
        "exact_date 형식 규칙 (요일 표기 절대 금지):\n"
        "1) 구체적인 날짜(연/월/일)를 알 때 -> 'YYYY-MM-DD'\n"
        "2) 월까지만 알거나 특정 월 범위로만 언급될 때 -> 'YYYY-MM' (일자는 쓰지 마라)\n"
        "3) 연도만 알 때 -> 'YYYY' (월/일은 쓰지 마라)\n"
        f"{n_year_rule}"
    )

    try:
        # ⚠️ flash-lite는 thinking_budget=0(완전 비활성)을 지원하지 않아 400 에러가 나고,
        # 반대로 명시적으로 값을 주면(예: 1) 오히려 출력 토큰이 늘어 비용이 커진다.
        # thinking_config 자체를 아예 넣지 않는 것이 가장 저렴하고 안전하다.
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=GEMINI_MODEL,
            contents=f"[뉴스 본문 원문]\n{body_text}",
            config={
                "system_instruction": gemini_prompt,
                "response_mime_type": "application/json",
                "response_schema": ScheduleSchema,
                "temperature": 0.0,
            }
        )

        usage = getattr(response, "usage_metadata", None)
        if usage:
            cost_tracker.add_usage(
                "3단계_본문추출(Gemini)", GEMINI_MODEL,
                usage.prompt_token_count, usage.candidates_token_count
            )

        extracted_data = json.loads(response.text)

        normalized_event, normalized_date = normalize_schedule_date(
            extracted_data.get("event_title"), extracted_data.get("exact_date")
        )
        news["extracted_event"] = normalized_event
        news["exact_date"] = normalized_date
        news["details"] = extracted_data.get("details")

        logger.info(f"▓ 📅 [3단계 정밀 추출 완수] [{news['exact_date']}] {news['extracted_event']}")
        return news

    except Exception as e:
        logger.error(f"❌ Gemini 본문 추출 에러 발생: {e}")
        news["extracted_event"] = title
        news["exact_date"] = "본문 확인 필요"
        news["details"] = f"Gemini 정밀 분석 중 에러가 발생했습니다: {str(e)[:50]}"
        return news


# ==========================================================
# 🚀 통합 제어 메인 오케스트레이터
# ==========================================================
async def main():
    logger.info("🧪 하이브리드 [OpenAI + Gemini] 3단계 뉴스 정제 파이프라인 가동")

    try:
        with open(INPUT_BACKUP_FILE, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
        total_raw_count = len(raw_data)
        logger.info(f"📂 백업 로드 성공: '{INPUT_BACKUP_FILE}' (총 {total_raw_count}건)")

        # 1차 단계: 중복 제거 파이프라인 구동
        purged_data = process_duplicate_purge_pipeline(raw_data)
        stage1_count = len(purged_data)

        # 2차 단계: OpenAI 제목 선별
        logger.info(f"\n🧠 [2단계] OpenAI gpt-4o-mini 기반 1차 필터링 개시...")
        openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        cost_tracker = CostTracker()

        progress_tracker["completed"] = 0
        progress_tracker["total"] = stage1_count

        tasks = [check_title_schedule_openai(openai_client, semaphore, news, cost_tracker) for news in purged_data]
        title_filter_results = await asyncio.gather(*tasks)

        # True로 선별된 뉴스만 정제
        selected_news = [news for news in title_filter_results if news is not None]

        # 📊 2단계 압축 통계 연산 및 실시간 출력
        stage2_count = len(selected_news)
        stage2_filtered = stage1_count - stage2_count
        stage2_compression = (stage2_filtered / stage1_count) * 100 if stage1_count > 0 else 0
        total_compression = ((total_raw_count - stage2_count) / total_raw_count) * 100 if total_raw_count > 0 else 0

        logger.info(
            f"🏁 [2단계 완료] 일정 선별 결과: {stage1_count}건 ➔ {stage2_count}건 "
            f"(이 단계에서 {stage2_filtered}건 제외, {stage2_compression:.1f}% 압축됨 / 원본 대비 총 {total_compression:.1f}% 압축됨)"
        )

        # 3차 단계: Gemini 본문 정밀 추출
        if not selected_news:
            logger.info("📅 추출 대상인 일정 뉴스가 존재하지 않아 파이프라인을 종료합니다.")
            cost_tracker.print_summary()
            cost_tracker.check_budget(monthly_budget_krw=5000)
            return

        # 💰 [예산 하드 캡] Stage3가 비용의 대부분을 차지하므로, 물량이 갑자기 튀어도
        # 여기서 하루 처리 상한을 넘지 않도록 강제로 잘라낸다 (오래된/우선순위 낮은 항목부터 제외)
        if len(selected_news) > MAX_STAGE3_ITEMS_PER_DAY:
            skipped_count = len(selected_news) - MAX_STAGE3_ITEMS_PER_DAY
            logger.warning(
                f"⚠️ [예산 하드 캡] Stage3 대상 {len(selected_news)}건이 일일 상한({MAX_STAGE3_ITEMS_PER_DAY}건)을 초과하여 "
                f"{skipped_count}건을 이번 실행에서 제외합니다."
            )
            selected_news = selected_news[:MAX_STAGE3_ITEMS_PER_DAY]

        logger.info(f"\n⚡ [3단계] Gemini 2.5 공식 모델 기반 본문 정밀 크롤링 및 일정 가공 가동... (대상 {len(selected_news)}건)")

        gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

        gemini_tasks = [extract_schedule_from_body_gemini(gemini_client, news, cost_tracker) for news in selected_news]
        final_schedule_results = await asyncio.gather(*gemini_tasks)

        # 최종 저장
        with open(OUTPUT_SCHEDULE_FILE, "w", encoding="utf-8") as f:
            json.dump(final_schedule_results, f, ensure_ascii=False, indent=2)

        logger.info(
            f"\n💾 [파이프라인 최종 완수] 총 {len(final_schedule_results)}건의 정밀 일정 캘린더 데이터 저장 완료: '{OUTPUT_SCHEDULE_FILE}'")

        # 🔥 Firestore(temp_events) 업로드
        push_schedules_to_firestore(final_schedule_results)

        cost_tracker.print_summary()
        cost_tracker.check_budget(monthly_budget_krw=5000)

    except FileNotFoundError:
        logger.error(f"❌ 백업 파일을 찾을 수 없습니다: '{INPUT_BACKUP_FILE}'")
    except Exception as e:
        logger.error(f"❌ 파이프라인 가동 중 치명적 예외 발생: {e}")


if __name__ == "__main__":
    import sys

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())