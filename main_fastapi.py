import os
import re
import json
import html
import time
import calendar  # ★ 시간대 보정을 위해 추가된 표준 라이브러리
import random
import secrets
import threading
import feedparser
import email.utils  # ★ 모든 표준 타임존 규격을 완벽하게 파싱하기 위해 추가
from datetime import datetime, timezone, timedelta  # ★ timezone, timedelta 누락 보완 완료
import urllib.parse
from curl_cffi import requests
import logging
from contextlib import asynccontextmanager  # ★ 최신 FastAPI lifespan 설정을 위해 추가
import asyncio  # ★ 비동기 스케줄러 구동을 위해 추가

from dotenv import load_dotenv

# 🚀 분리한 백업/일정추출 스케줄러 모듈 임포트
import back_up_scheduler
import global_back_up_scheduler
import schedule_extraction_scheduler
import company_list_sync_scheduler

from fastapi import Depends, FastAPI, Body, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn

load_dotenv()

# ====================================================
# 🔐 [인증] 서버가 0.0.0.0으로 바인딩되어 외부에서도 도달 가능하므로,
# 대시보드 열람 + 모든 API를 HTTP Basic Auth로 전면 보호한다.
# ====================================================
security = HTTPBasic()
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")


def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        logger.error("❌ ADMIN_USERNAME/ADMIN_PASSWORD가 .env에 설정되지 않아 모든 요청을 차단합니다.")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})

    is_user_ok = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    is_pass_ok = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (is_user_ok and is_pass_ok):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})
    return True

# 로거 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("news_logger")

# ====================================================
# ⚙️ [전역 제어 및 메모리/파일 이원화 상수 설정]
# ====================================================
MEM_READ_COUNT = 100  # index.html 요청 혹은 새로고침 시 메모리에서 읽어와 화면에 채울 뉴스 개수
DEFAULT_CHECK_INTERVAL = 30
PORT = 8080
# ====================================================

# ====================================================
# 📨 [텔레그램] 키워드 포착 뉴스 실시간 알림
# ====================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SENTIMENT_EMOJI = {
    "positive": "🟢",
    "negative": "🔴",
    "mixed": "🟡",
    "neutral": "🔵",
}


def shorten_telegram_link(link: str) -> str:
    """
    일부 해외 매체(FinancialJuice 등)는 기사 제목을 그대로 슬러그로 붙여 URL이 지나치게 길다.
    슬러그는 장식용이라 잘라내도 같은 기사로 정상 접속되므로, 텔레그램 전송용으로만 축약한다.
    (저장/화면 표시용 원본 link는 그대로 유지되고, 이 함수는 텔레그램 메시지 조립 시에만 쓰인다.)
    """
    match = re.match(r'^(https?://www\.financialjuice\.com/News/\d+)/', link)
    return f"{match.group(1)}/" if match else link


def send_telegram_notification(items):
    """키워드 포착된 신규 기사를 텔레그램으로 실시간 전송. 설정 없으면 조용히 스킵."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not items:
        return

    # 💡 여러 건을 한 메시지에 묶으면 모바일 푸시 미리보기엔 첫 건만 보여서, 건마다 별도 메시지로 보낸다.
    # 💡 텔레그램은 순수 공백/개행만 있는 꼬리를 렌더링 시 잘라내므로, 마지막 줄에 보이지 않는 문자
    # (zero-width space)를 넣어 다음 메시지와의 간격을 실제로 남긴다.
    for item in items:
        # RSS 피드에서 이미 &quot; 같은 HTML 엔티티가 한 번 덜 풀린 채로 들어오는 경우가 있어
        # 먼저 unescape로 실제 문자(")로 되돌린 뒤, 텔레그램 HTML 파싱용으로 다시 escape한다.
        raw_title = html.unescape(item.get("title", ""))
        title = html.escape(raw_title)
        link = html.escape(shorten_telegram_link(item.get("link", "")))
        emoji = SENTIMENT_EMOJI.get(item.get("sentiment", "neutral"), "🔵")
        # 제목은 링크로 감싸지 않아 기본 텍스트색(흰색)으로 표시하고,
        # URL은 별도 줄에 그대로 둬서 텔레그램이 자동으로 링크 처리하게 한다.
        text = f'{emoji} {title}\n\n{link}\n​'

        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"⚠️ 텔레그램 전송 실패 ({resp.status_code}): {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"⚠️ 텔레그램 전송 에러: {e}")

        time.sleep(0.3)  # 텔레그램 플러드 제한 방지용 짧은 간격

# ====================================================
# 🌐 [해외] 영문 피드(FinancialJuice 등) 제목 한글 번역
# 비용 발생을 피하기 위해 유료 LLM API 대신, 구글 번역의 비공식 무료 웹 엔드포인트를 사용한다.
# (translate.googleapis.com의 /translate_a/single - 키 발급/과금 없이 누구나 호출 가능하지만
#  비공식이라 언젠가 막히거나 바뀔 수 있음. 실패 시 원문 제목을 그대로 반환하도록 방어한다.)
# ====================================================
# 도메인에 이 문자열이 포함되면 제목을 한글로 번역한다. 영문 전용 해외 피드가 늘어나면 여기에 추가.
ENGLISH_TITLE_TRANSLATE_DOMAINS = ["financialjuice.com"]


def translate_title_to_korean(title: str) -> str:
    """영문 뉴스 제목을 한국어로 번역한다. 실패 시 원문 제목을 그대로 반환한다."""
    if not title.strip():
        return title

    try:
        response = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "en", "tl": "ko", "dt": "t", "q": title},
            timeout=8,
        )
        response.raise_for_status()
        data = response.json()
        translated = "".join(segment[0] for segment in data[0] if segment and segment[0])
        translated = translated.strip()
        return translated if translated else title
    except Exception as e:
        logger.warning(f"⚠️ [제목 번역 실패] '{title[:40]}...' 번역 중 오류: {e}")
        return title


def should_translate_title(url: str) -> bool:
    return any(domain in url for domain in ENGLISH_TITLE_TRANSLATE_DOMAINS)

# ====================================================

RSS_DB_FILE = "base_info/rss_list.json"
KEYWORD_DB_FILE = "base_info/keyword_list_include.json"
BLACKLIST_DB_FILE = "base_info/keyword_list_exclude.json"
FILTERED_NEWS_FILE = "news_list_filtered.json"
STREAM_NEWS_FILE = "news_list_stream.json"
BLACKLISTED_NEWS_FILE = "news_list_blacklisted.json"

# 🌐 [해외 뉴스] 국내 파이프라인과 완전히 분리된 별도의 DB/캐시 파일 세트
GLOBAL_RSS_DB_FILE = "base_info/global_rss_list.json"
GLOBAL_KEYWORD_DB_FILE = "base_info/global_keyword_list_include.json"
GLOBAL_BLACKLIST_DB_FILE = "base_info/global_keyword_list_exclude.json"
GLOBAL_FILTERED_NEWS_FILE = "global_news_list_filtered.json"
GLOBAL_STREAM_NEWS_FILE = "global_news_list_stream.json"
GLOBAL_BLACKLISTED_NEWS_FILE = "global_news_list_blacklisted.json"

DEFAULT_CATEGORIZED_KEYWORDS = {
    "etc": ["속보", "트럼프", "이란", "단독", "특징주", "계약", "대통령", "美", "젠슨황", "중", "北"],
    "sector": ["반도체", "조선"],
    "company_kr": ["삼성전자", "SK하이닉스", "현대차"],
    "global_company": ["엔비디아", "애플", "테슬라", "도요타", "소니", "보스턴다이나믹스"],
    "brokerage": ["골드만삭스", "JP모건", "모건스탠리"],
    "performance": ["실적", "영업이익", "어닝서프라이즈", "흑자전환", "매출액", "영업익", "적자전환"],
    "positive": [],
    "negative": []
}
DEFAULT_BLACKLIST = ["스팸", "찌라시"]
DEFAULT_RSS_CHANNELS = [
    {"name": "연합뉴스", "url": "https://www.yonhapnewstv.co.kr/category/news/feed/", "enabled": True},
    {"name": "매일경제", "url": "https://www.mk.co.kr/rss/30000001/", "enabled": True}
]

# 🌐 [해외 뉴스] 아직 확정된 채널/키워드가 없으므로 빈 값으로 시작 (설정 화면에서 직접 구성)
DEFAULT_GLOBAL_CATEGORIZED_KEYWORDS = {
    "etc": [], "sector": [], "company_kr": [], "global_company": [], "brokerage": [],
    "performance": [], "positive": [], "negative": []
}
DEFAULT_GLOBAL_BLACKLIST = []
DEFAULT_GLOBAL_RSS_CHANNELS = []

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0"
]



# ----------------------------------------------------
# 💾 파일 시스템 입출력 제어 로직 (원자적 쓰기 & 무제한 누적)
# ----------------------------------------------------
def _safe_atomic_append_write(file_path, new_items):
    if not new_items:
        return

    old_data = []
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    old_data = json.loads(content)
        except Exception:
            old_data = []

    total_data = new_items + old_data

    temp_file = file_path + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(total_data, f, ensure_ascii=False, indent=4)

        if os.path.exists(temp_file):
            if os.path.exists(file_path):
                os.remove(file_path)
            os.rename(temp_file, file_path)
    except Exception as e:
        logger.error(f"❌ [물리 디스크 저장 오류] {file_path} 추가 쓰기 실패: {e}")
        if os.path.exists(temp_file):
            os.remove(temp_file)


def _safe_atomic_write(file_path, data):
    temp_file = file_path + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        if os.path.exists(temp_file):
            if os.path.exists(file_path):
                os.remove(file_path)
            os.rename(temp_file, file_path)
    except Exception as e:
        if os.path.exists(temp_file):
            os.remove(temp_file)


def load_keywords(file_path=KEYWORD_DB_FILE, default=DEFAULT_CATEGORIZED_KEYWORDS):
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                migrated = {"etc": [], "sector": [], "company_kr": [], "global_company": [], "brokerage": [],
                            "performance": [], "positive": [], "negative": []}
                if isinstance(data, list):
                    for kw in data:
                        if kw in ["반도체", "조선"]:
                            migrated["sector"].append(kw)
                        elif kw in ["삼성전자", "SK하이닉스", "현대차"]:
                            migrated["company_kr"].append(kw)
                        elif kw in ["골드만삭스", "JP모건", "모건스탠리"]:
                            migrated["brokerage"].append(kw)
                        else:
                            migrated["etc"].append(kw)
                    return migrated
                if "company_us" in data and ("global_company" not in data or not data["global_company"]):
                    data["global_company"] = data["company_us"]
                for key in ["etc", "sector", "company_kr", "global_company", "brokerage", "performance", "positive", "negative"]:
                    if key not in data: data[key] = []
                if "company_us" in data: del data["company_us"]
                if "company" in data: del data["company"]
                return data
        except Exception:
            pass
    return default


def save_keywords(keywords, file_path=KEYWORD_DB_FILE): _safe_atomic_write(file_path, keywords)


def keyword_matches(kw: str, title: str) -> bool:
    """
    '/패턴/'처럼 슬래시로 감싼 키워드는 정규식으로, 그 외에는 기존처럼 단순 부분 문자열로 매칭한다.
    기존에 등록된 수천 개의 일반 키워드 동작은 그대로 유지된다.
    """
    if len(kw) > 2 and kw.startswith("/") and kw.endswith("/"):
        pattern = kw[1:-1]
        try:
            return re.search(pattern, title) is not None
        except re.error:
            return False
    return kw in title


def load_blacklist(file_path=BLACKLIST_DB_FILE, default=DEFAULT_BLACKLIST):
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_blacklist(blacklist, file_path=BLACKLIST_DB_FILE): _safe_atomic_write(file_path, blacklist)


def load_rss(file_path=RSS_DB_FILE, default=DEFAULT_RSS_CHANNELS):
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for item in data:
                    if "enabled" not in item: item["enabled"] = True
                return data
        except Exception:
            pass
    return default


def save_rss(rss_list, file_path=RSS_DB_FILE): _safe_atomic_write(file_path, rss_list)


def load_file_entire_content(file_path):
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    data = json.loads(content)
                    if isinstance(data, list):
                        return data
        except Exception:
            pass
    return []


# ----------------------------------------------------
# ⚙️ 글로벌 데이터 동기화 객체 및 초기 세팅
# ----------------------------------------------------
db_lock = threading.Lock()
cached_keywords = load_keywords()
cached_blacklist = load_blacklist()
cached_rss = load_rss()

cached_stream = []
cached_filtered = []
cached_blacklisted = []

rss_response_status = {}

# ----------------------------------------------------
# ⚙️ 글로벌 데이터 동기화 객체 및 초기 세팅
# ----------------------------------------------------
db_lock = threading.Lock()
cached_keywords = load_keywords()
cached_blacklist = load_blacklist()
cached_rss = load_rss()

# ★ 이제 파이썬이 이 변수들을 메모리에 정상적으로 등록했습니다.
cached_stream = []
cached_filtered = []
cached_blacklisted = []

rss_response_status = {}

# 🌐 [해외 뉴스] 국내 파이프라인과 동일한 락(db_lock)을 공유하되, 완전히 별도의 캐시/키워드/RSS 목록을 사용
cached_global_keywords = load_keywords(GLOBAL_KEYWORD_DB_FILE, DEFAULT_GLOBAL_CATEGORIZED_KEYWORDS)
cached_global_blacklist = load_blacklist(GLOBAL_BLACKLIST_DB_FILE, DEFAULT_GLOBAL_BLACKLIST)
cached_global_rss = load_rss(GLOBAL_RSS_DB_FILE, DEFAULT_GLOBAL_RSS_CHANNELS)

cached_global_stream = []
cached_global_filtered = []
cached_global_blacklisted = []

global_rss_response_status = {}

# ====================================================
# 🪐 [이 위치로 이동] 백업 스케줄러 자원 매핑 데이터 준비
# ====================================================
FILE_PATHS_CONFIG = {
    "stream": STREAM_NEWS_FILE,
    "filtered": FILTERED_NEWS_FILE,
    "blacklisted": BLACKLISTED_NEWS_FILE
}
MEMORY_CACHES_CONFIG = {
    "stream": cached_stream,      # 이제 정상적으로 인식됩니다!
    "filtered": cached_filtered,
    "blacklisted": cached_blacklisted
}

FILE_PATHS_CONFIG_GLOBAL = {
    "stream": GLOBAL_STREAM_NEWS_FILE,
    "filtered": GLOBAL_FILTERED_NEWS_FILE,
    "blacklisted": GLOBAL_BLACKLISTED_NEWS_FILE
}
MEMORY_CACHES_CONFIG_GLOBAL = {
    "stream": cached_global_stream,
    "filtered": cached_global_filtered,
    "blacklisted": cached_global_blacklisted
}

app = FastAPI(
    title="실시간 뉴스 감시 시그널 감시센터 API",
    description="FastAPI 기반 고속 비동기 트레이딩 뉴스 관제 백엔드",
    dependencies=[Depends(verify_admin)]  # 대시보드 + 모든 API 라우트 전면 인증 보호
)


# ----------------------------------------------------
# 📊 실시간 상태 모니터링 브리핑 함수
# ----------------------------------------------------
def print_resource_status():
    global cached_stream, cached_filtered, cached_blacklisted
    print("\n📊 [시스템 리소스 전체 메모리 적재 모니터링 브리핑]")
    print("----------------------------------------------------------------------")
    print(f"    └ 메모리 변수 : stream_news        ({len(cached_stream):,d}건)")
    print(f"    └ 메모리 변수 : filtered_news      ({len(cached_filtered):,d}건)")
    print(f"    └ 메모리 변수 : blacklisted_news   ({len(cached_blacklisted):,d}건)")



    print("======================================================================\n")


def print_global_resource_status():
    global cached_global_stream, cached_global_filtered, cached_global_blacklisted
    print("\n📊 [🌐 해외 뉴스 리소스 메모리 적재 모니터링 브리핑]")
    print("----------------------------------------------------------------------")
    print(f"    └ 메모리 변수 : global_stream_news      ({len(cached_global_stream):,d}건)")
    print(f"    └ 메모리 변수 : global_filtered_news    ({len(cached_global_filtered):,d}건)")
    print(f"    └ 메모리 변수 : global_blacklisted_news ({len(cached_global_blacklisted):,d}건)")
    print("======================================================================\n")


# ----------------------------------------------------
# 📡 실시간 RSS 뉴스 수집 백그라운드 데몬 스레드
# (국내/해외 두 파이프라인이 동일한 로직을 서로 다른 캐시·파일 세트로 각자 구동한다)
# ----------------------------------------------------
def rss_monitor_thread(
    cached_rss_ref,
    cached_keywords_ref,
    cached_blacklist_ref,
    cached_stream_ref,
    cached_filtered_ref,
    cached_blacklisted_ref,
    response_status_ref,
    stream_file,
    filtered_file,
    blacklisted_file,
    label="",
):
    print(f"📢 {label}RSS 실시간 백그라운드 관제 엔진이 가동되었습니다.")

    session = requests.Session()
    is_first_scan = True  # 서버 재시작 직후 첫 스캔은 다운타임 동안 밀린 기사가 한꺼번에 잡히므로 텔레그램 알림만 건너뜀

    # 🐢 [429 백오프] 채널(url)별 연속 429 횟수와 "이 시각까지는 재시도하지 않음" 시각을 기억한다.
    # 스레드 지역 변수라 국내/해외 파이프라인이 서로 독립적으로 관리된다.
    channel_fail_streak = {}
    channel_backoff_until = {}
    BACKOFF_STEP_SECONDS = 60      # 1회차 60초, 2회차 120초, 3회차 180초 ... 선형 증가
    BACKOFF_CAP_SECONDS = 1800     # 최대 30분

    while True:
        try:
            # ⏰ KST 기준 정확한 '오늘'과 '어제'의 달력상 날짜(Date) 정의
            kst_tz = timezone(timedelta(hours=9))
            now_kst = datetime.now(kst_tz)

            today_date = now_kst.date()                      # 달력상 오늘 (예: 2026-07-19)
            yesterday_date = (now_kst - timedelta(days=1)).date()  # 달력상 어제 (예: 2026-07-18)

            current_time_log = now_kst.strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n==================================================")
            print(f"🔄 [{current_time_log}] 정기 뉴스 파싱 수집 루프 가동 시작")
            print(f"==================================================")

            # 🛠️ [무결성 보정] 매 스캔 턴마다 동적으로 풀 구성
            seen_links = set()
            with db_lock:
                for item in cached_stream_ref: seen_links.add(item["link"])
                for item in cached_filtered_ref: seen_links.add(item["link"])
                for item in cached_blacklisted_ref: seen_links.add(item["link"])

                target_channels = [dict(ch) for ch in cached_rss_ref]
                categorized_kw = dict(cached_keywords_ref)
                current_blacklist = list(cached_blacklist_ref)

            flat_keywords = set()
            for key in ["etc", "sector", "company_kr", "global_company", "brokerage", "performance", "positive", "negative"]:
                flat_keywords.update(categorized_kw.get(key, []))

            positive_set = set(categorized_kw.get("positive", []))
            negative_set = set(categorized_kw.get("negative", []))

            total_new_stream = 0
            total_new_filtered = 0
            total_new_blacklisted = 0

            for ch in target_channels:
                name = ch.get("name", "").strip()
                url = ch.get("url", "")
                enabled = ch.get("enabled", True)

                if not url: continue
                if not enabled:
                    with db_lock: response_status_ref[url] = "OFF"
                    continue

                backoff_until = channel_backoff_until.get(url, 0)
                if time.time() < backoff_until:
                    remaining = int(backoff_until - time.time())
                    print(f" ⏭️ [{name}] 429 백오프 중이라 스킵 (연속 {channel_fail_streak.get(url, 0)}회 실패, {remaining}초 후 재시도)")
                    continue

                print(f" 🔍 [{name}] 피드 연결 중...", end="", flush=True)

                try:
                    chosen_ua = random.choice(USER_AGENTS)
                    headers = {
                        'User-Agent': chosen_ua,
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                        'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
                    }

                    request_url = url
                    if "hankyung.com" in url:
                        sub_topic = "주식"
                        if "realestate" in url or "property" in url:
                            sub_topic = "부동산"
                        elif "it" in url:
                            sub_topic = "IT"
                        elif "economy" in url:
                            sub_topic = "경제"
                        request_url = f"https://news.google.com/rss/search?q=site:hankyung.com+{urllib.parse.quote(sub_topic)}&hl=ko&gl=KR&ceid=KR:ko"

                    response = session.get(request_url, headers=headers, impersonate="chrome", timeout=10)
                    with db_lock:
                        response_status_ref[url] = response.status_code

                    if response.status_code == 429:
                        channel_fail_streak[url] = channel_fail_streak.get(url, 0) + 1
                        backoff_seconds = min(BACKOFF_STEP_SECONDS * channel_fail_streak[url], BACKOFF_CAP_SECONDS)
                        channel_backoff_until[url] = time.time() + backoff_seconds
                        print(f" ➔ 🟡 429 Rate Limited - {channel_fail_streak[url]}회 연속 실패라 앞으로 {backoff_seconds}초간 이 채널을 스킵합니다")
                        time.sleep(0.3)
                        continue

                    response.raise_for_status()
                    print(f" ➔ 🟢 {response.status_code} OK")

                    # 정상 응답을 받았으니 이 채널의 429 백오프 상태를 초기화한다.
                    channel_fail_streak.pop(url, None)
                    channel_backoff_until.pop(url, None)

                    feed = feedparser.parse(response.text)
                    source_name = name if name and name not in ["기존 채널", "수집 채널"] else feed.feed.get("title", "알 수 없음")

                    new_stream_items = []
                    new_filtered_items = []
                    new_blacklisted_items = []

                    ch_entries = []
                    for entry in feed.entries:
                        link = entry.get("link", "")
                        if link in seen_links: continue
                        title = re.sub(r'\s+-\s+한국경제$', '', entry.get("title", ""))
                        title = re.sub(r'^\s*FinancialJuice\s*:\s*', '', title, flags=re.IGNORECASE)
                        ch_entries.append((title, link, entry))

                    channel_new_count = 0
                    for title, link, entry in ch_entries:
                        try:
                            news_timestamp = None
                            news_time_str = None
                            date_error_flag = False
                            news_date_obj = None  # 날짜 비교를 위한 객체 변수

                            raw_pub_date = entry.get("published", "").strip()

                            if not raw_pub_date:
                                raise ValueError("RSS 피드 내 날짜 문자열이 완전히 누락되었습니다.")

                            try:
                                has_tz_marker = any(tz in raw_pub_date.lower() for tz in ["+", "gmt", "z"])
                                is_pure_datetime = re.search(r'\d{4}[-\/]\d{2}[-\/]\d{2}', raw_pub_date) is not None

                                if is_pure_datetime and not has_tz_marker:
                                    clean_date = raw_pub_date.replace("/", "-")
                                    match_dt = re.search(r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}', clean_date)
                                    if match_dt:
                                        target_date_str = match_dt.group(0)
                                        dt_obj = datetime.strptime(target_date_str, "%Y-%m-%d %H:%M:%S")
                                        if "investing.com" in url:
                                            # 💡 인베스팅닷컴 피드의 pubDate는 타임존 표기가 없지만 실제로는 UTC 기준이라
                                            # 그대로 KST로 취급하면 9시간이 밀린다. UTC로 해석한 뒤 KST로 변환한다.
                                            dt_kst = dt_obj.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=9)))
                                        else:
                                            dt_kst = dt_obj.replace(tzinfo=timezone(timedelta(hours=9)))
                                        news_timestamp = int(dt_kst.timestamp())
                                        news_time_str = dt_kst.strftime("%Y-%m-%d %H:%M:%S")
                                        news_date_obj = dt_kst.date()
                                    else:
                                        raise ValueError("순수 날짜 형태 정규식 내부 추출 실패")

                                else:
                                    fixed_pub_date = re.sub(r'([+-]\d{2}):(\d{2})$', r'\1\2', raw_pub_date)
                                    dt_parsed = email.utils.parsedate_to_datetime(fixed_pub_date)

                                    if dt_parsed.tzinfo:
                                        news_timestamp = int(dt_parsed.timestamp())
                                        dt_kst = datetime.fromtimestamp(news_timestamp, tz=timezone(timedelta(hours=9)))
                                        news_time_str = dt_kst.strftime("%Y-%m-%d %H:%M:%S")
                                        news_date_obj = dt_kst.date()
                                    else:
                                        raise ValueError("타임존 객체 반환 실패")

                            except Exception as parse_inner_err:
                                pub_parsed = entry.get("published_parsed")
                                if pub_parsed is not None:
                                    utc_timestamp = calendar.timegm(pub_parsed)
                                    news_timestamp = utc_timestamp

                                    dt_kst = datetime.fromtimestamp(news_timestamp, tz=timezone(timedelta(hours=9)))
                                    news_time_str = dt_kst.strftime("%Y-%m-%d %H:%M:%S")
                                    news_date_obj = dt_kst.date()
                                else:
                                    raise parse_inner_err

                        except Exception as date_err:
                            logger.error(f"❌ [UNKNOWN_PUBDATE_FORMAT] 새로운 날짜 포맷 발견! 매체: {source_name} | 원본 문자열: '{raw_pub_date}' | 에러: {date_err}")
                            date_error_flag = True

                            news_timestamp = int(time.time())
                            dt_now = datetime.now(kst_tz)
                            news_time_str = dt_now.strftime("%Y-%m-%d %H:%M:%S")
                            news_date_obj = dt_now.date()

                        # 🛑 [달력 일자별 체크 반영 영역] 오늘이나 어제 날짜가 아닌 과거 뉴스 필터링
                        if news_date_obj not in [today_date, yesterday_date]:
                            # 무의미하게 터미널을 채우던 대량의 print 코드를 제거하여 자원을 아낍니다.
                            continue

                        # 🌐 [해외] 영문 전용 피드(FinancialJuice 등)는 제목을 한글로 번역한 뒤
                        # 이후 블랙리스트/키워드 매칭도 번역된 한글 제목 기준으로 수행되게 한다.
                        if should_translate_title(url):
                            original_title = title
                            title = translate_title_to_korean(title)
                            if title != original_title:
                                print(f"    🌐 [제목 번역] '{original_title[:30]}...' → '{title[:30]}...'")

                        # 🟢 유효 일자(오늘/어제) 내의 기사만 통과되어 출력 및 수집 진행
                        print(f" 🔍 [날짜 변환 모니터링]")
                        print(f"    ├ 매체명: {source_name}")
                        print(f"    ├ 기사 제목: {title[:25]}...")
                        print(f"    ├ 수집된 원본 <pubDate> : {raw_pub_date}")
                        print(f"    └ JSON 입력 최종 일시(time_kst): {news_time_str}")
                        print(f" --------------------------------------------------")

                        item = {
                            "title": title,
                            "link": link,
                            "source": source_name if "한국경제" in source_name or "hankyung.com" not in url else f"한국경제({name})",
                            "time_kst": news_time_str,
                            "timestamp": news_timestamp,
                            "date_error": date_error_flag
                        }

                        seen_links.add(link)
                        channel_new_count += 1

                        if any(keyword_matches(bl.strip(), title) for bl in current_blacklist if bl.strip()):
                            new_blacklisted_items.append(item)
                            print(f"    🗑️ [노이즈차단] {item['source']} ➔ 제목: {title}")
                        else:
                            new_stream_items.append(item)
                            matched_kws = [kw for kw in flat_keywords if kw.strip() and keyword_matches(kw.strip(), title)]

                            if matched_kws:
                                has_positive = any(kw in positive_set for kw in matched_kws)
                                has_negative = any(kw in negative_set for kw in matched_kws)
                                if has_positive and has_negative:
                                    item["sentiment"] = "mixed"
                                elif has_positive:
                                    item["sentiment"] = "positive"
                                elif has_negative:
                                    item["sentiment"] = "negative"
                                else:
                                    item["sentiment"] = "neutral"

                                new_filtered_items.append(item)
                                print(f"    ⭐ [키워드포착] {item['source']} ➔ 키워드: {matched_kws} | 감성: {item['sentiment']} | 제목: {title}")
                            else:
                                print(f"    🆕 [실시간유입] {item['source']} ➔ 제목: {title}")

                    if channel_new_count > 0:
                        print(f"    └ 🏁 [{name}] 피드 탐색 완료 (신규 유입 기사: {channel_new_count}건)")

                    # 📡 [채널 단위 즉시 반영] 전체 채널을 다 돌 때까지 기다리지 않고,
                    # 이 채널의 신규 기사가 생기는 즉시 캐시/파일에 반영해 브라우저가 바로 받아갈 수 있게 한다.
                    if new_stream_items or new_filtered_items or new_blacklisted_items:
                        _safe_atomic_append_write(stream_file, new_stream_items)
                        _safe_atomic_append_write(filtered_file, new_filtered_items)
                        _safe_atomic_append_write(blacklisted_file, new_blacklisted_items)

                        with db_lock:
                            if new_stream_items:
                                for idx, item in enumerate(new_stream_items): cached_stream_ref.insert(idx, item)
                            if new_filtered_items:
                                for idx, item in enumerate(new_filtered_items): cached_filtered_ref.insert(idx, item)
                            if new_blacklisted_items:
                                for idx, item in enumerate(new_blacklisted_items): cached_blacklisted_ref.insert(idx, item)

                            cached_stream_ref.sort(key=lambda x: x["timestamp"], reverse=True)
                            cached_filtered_ref.sort(key=lambda x: x["timestamp"], reverse=True)
                            cached_blacklisted_ref.sort(key=lambda x: x["timestamp"], reverse=True)

                        print(f"    └ 📡 [{name}] 신규 기사 즉시 반영 ➔ 실시간: +{len(new_stream_items)}건 | 키워드 포착: +{len(new_filtered_items)}건")

                        if new_filtered_items and not is_first_scan:
                            send_telegram_notification(new_filtered_items)
                        elif new_filtered_items and is_first_scan:
                            print(f"🔇 [알림 스킵] 서버 재시작 직후 첫 스캔이라 {len(new_filtered_items)}건의 텔레그램 알림을 건너뜁니다.")

                        total_new_stream += len(new_stream_items)
                        total_new_filtered += len(new_filtered_items)
                        total_new_blacklisted += len(new_blacklisted_items)

                    time.sleep(0.3)

                except Exception as feed_err:
                    print(f" ➔ 🔴 실패 ({feed_err})")
                    time.sleep(0.3)
                    continue

            if total_new_stream or total_new_filtered or total_new_blacklisted:
                print(f"🏁 [결과 요약] 탐색 완료 ➔ 실시간 유입: +{total_new_stream}건 | 키워드 포착: +{total_new_filtered}건")
            else:
                print(f"🏁 [결과 요약] 탐색 완료 ➔ 변동 없음")

            print(f"\n📊 [{label}시스템 리소스 메모리 적재 모니터링 브리핑]")
            print(f"    └ stream: {len(cached_stream_ref):,d}건 | filtered: {len(cached_filtered_ref):,d}건 | blacklisted: {len(cached_blacklisted_ref):,d}건")

        except Exception as e:
            print(f"❌ RSS 수집 루프 내부 크리티컬 에러: {e}")
        finally:
            is_first_scan = False

        time.sleep(DEFAULT_CHECK_INTERVAL)


# ----------------------------------------------------
# 📡 FastAPI 엔드포인트 제어
# ----------------------------------------------------
@app.get("/")
@app.get("/index.html")
async def get_dashboard():
    return FileResponse("base_info/index.html")


@app.get("/channel-view")
async def get_channel_view():
    return FileResponse("base_info/channel_view.html")


@app.get("/trash-view")
async def get_trash_view():
    return FileResponse("base_info/trash_view.html")


@app.get("/keyword-settings")
async def get_keyword_settings():
    return FileResponse("base_info/keyword_settings.html")


@app.get("/rss-settings")
async def get_rss_settings():
    return FileResponse("base_info/rss_settings.html")


# 🌐 [해외 뉴스] 국내 화면과 완전히 분리된 별도 페이지 세트
@app.get("/global")
@app.get("/global-index.html")
async def get_global_dashboard():
    return FileResponse("base_info/global_index.html")


@app.get("/global-channel-view")
async def get_global_channel_view():
    return FileResponse("base_info/global_channel_view.html")


@app.get("/global-trash-view")
async def get_global_trash_view():
    return FileResponse("base_info/global_trash_view.html")


@app.get("/global-keyword-settings")
async def get_global_keyword_settings():
    return FileResponse("base_info/global_keyword_settings.html")


@app.get("/global-rss-settings")
async def get_global_rss_settings():
    return FileResponse("base_info/global_rss_settings.html")


def _get_safe_memory_data(source_list, offset: int = 0):
    safe_list = []
    snapshot = list(source_list)
    total_count = len(snapshot)
    end_idx = offset + MEM_READ_COUNT
    for item in snapshot[offset:end_idx]:
        safe_item = dict(item)
        safe_item["timestamp"] = int(item.get("timestamp", time.time()))
        if "time_kst" in item:
            safe_item["time"] = item["time_kst"]
        safe_list.append(safe_item)
    return {"total_count": total_count, "news": safe_list}


@app.get("/api/realtime-news")
def get_all_news(offset: int = 0):
    with db_lock: current_data = list(cached_stream)
    return _get_safe_memory_data(current_data, offset)


@app.get("/api/filtered-news")
def get_filtered_news(offset: int = 0):
    with db_lock: current_data = list(cached_filtered)
    return _get_safe_memory_data(current_data, offset)


@app.get("/api/blacklisted-news")
def get_blacklisted_news(offset: int = 0):
    with db_lock: current_data = list(cached_blacklisted)
    return _get_safe_memory_data(current_data, offset)


@app.get("/api/global-realtime-news")
def get_global_all_news(offset: int = 0):
    with db_lock: current_data = list(cached_global_stream)
    return _get_safe_memory_data(current_data, offset)


@app.get("/api/global-filtered-news")
def get_global_filtered_news(offset: int = 0):
    with db_lock: current_data = list(cached_global_filtered)
    return _get_safe_memory_data(current_data, offset)


@app.get("/api/global-blacklisted-news")
def get_global_blacklisted_news(offset: int = 0):
    with db_lock: current_data = list(cached_global_blacklisted)
    return _get_safe_memory_data(current_data, offset)


@app.get("/api/keywords")
async def get_keywords(mode: str = "flat"):
    global cached_keywords
    with db_lock:
        if mode == "raw": return cached_keywords
        flat = []
        for k in ["etc", "sector", "company_kr", "global_company", "brokerage", "performance"]:
            flat.extend(cached_keywords.get(k, []))
        return flat


@app.get("/api/blacklist")
async def get_blacklist():
    global cached_blacklist
    with db_lock: return cached_blacklist


@app.get("/api/global-keywords")
async def get_global_keywords(mode: str = "flat"):
    global cached_global_keywords
    with db_lock:
        if mode == "raw": return cached_global_keywords
        flat = []
        for k in ["etc", "sector", "company_kr", "global_company", "brokerage", "performance"]:
            flat.extend(cached_global_keywords.get(k, []))
        return flat


@app.get("/api/global-blacklist")
async def get_global_blacklist():
    global cached_global_blacklist
    with db_lock: return cached_global_blacklist


@app.get("/api/rss")
async def get_rss_channels():
    # ⏰ 서버의 시스템 타임존(UTC 등)이 아닌 KST 기준으로 '오늘'의 시작 시각을 계산한다.
    # (item.timestamp는 항상 KST 기준으로 계산되어 저장되므로 경계도 KST로 맞춰야 정확히 일치한다)
    kst_tz = timezone(timedelta(hours=9))
    now_kst = datetime.now(kst_tz)
    today_start = datetime.combine(now_kst.date(), datetime.min.time(), tzinfo=kst_tz).timestamp()

    with db_lock:
        response_channels = []
        for ch in cached_rss:
            ch_url = ch.get("url", "")
            ch_name = ch.get("name", "수집 채널")
            ch_status = rss_response_status.get(ch_url, "-")

            # 🗂️ 실시간/필터링/격리 세 캐시를 모두 훑되(격리 뉴스는 실시간 캐시에 안 남으므로 별도 합산 필요),
            # 필터링 뉴스는 실시간 캐시의 부분집합이라 링크 기준으로 중복 제거해 정확히 센다.
            today_links = set()
            for source_list in (cached_stream, cached_filtered, cached_blacklisted):
                for item in source_list:
                    if item.get("source") == ch_name and item.get("timestamp", 0) >= today_start:
                        today_links.add(item.get("link"))
            today_count = len(today_links)

            response_channels.append(
                {"name": ch_name, "url": ch_url, "enabled": ch.get("enabled", True), "today_count": today_count,
                 "status_code": ch_status})
        return response_channels


@app.get("/api/global-rss")
async def get_global_rss_channels():
    kst_tz = timezone(timedelta(hours=9))
    now_kst = datetime.now(kst_tz)
    today_start = datetime.combine(now_kst.date(), datetime.min.time(), tzinfo=kst_tz).timestamp()

    with db_lock:
        response_channels = []
        for ch in cached_global_rss:
            ch_url = ch.get("url", "")
            ch_name = ch.get("name", "수집 채널")
            ch_status = global_rss_response_status.get(ch_url, "-")

            today_links = set()
            for source_list in (cached_global_stream, cached_global_filtered, cached_global_blacklisted):
                for item in source_list:
                    if item.get("source") == ch_name and item.get("timestamp", 0) >= today_start:
                        today_links.add(item.get("link"))
            today_count = len(today_links)

            response_channels.append(
                {"name": ch_name, "url": ch_url, "enabled": ch.get("enabled", True), "today_count": today_count,
                 "status_code": ch_status})
        return response_channels


KEYWORD_CATEGORY_KEYS = ["etc", "sector", "company_kr", "global_company", "brokerage", "performance", "positive", "negative"]


def _flatten_categorized_keywords(categorized: dict) -> set:
    flat = set()
    for k in KEYWORD_CATEGORY_KEYS:
        flat.update(kw.strip() for kw in categorized.get(k, []) if kw.strip())
    return flat


def _promote_newly_matched_keywords(new_keywords, positive_set, negative_set, stream_ref, filtered_ref, filtered_file):
    """
    신규로 등록된 감시 키워드에 걸리는 기사를 실시간 스트림에서 찾아
    키워드 포착 피드(필터링 목록)로 소급 편입시킨다. (db_lock을 보유한 상태에서 호출되어야 함)
    필터링 목록은 스트림의 부분집합 개념(원본은 스트림에 그대로 남는다)이라
    스트림에서는 제거하지 않고 필터링 목록에만 추가한다.
    """
    if not new_keywords:
        return

    existing_filtered_links = {item["link"] for item in filtered_ref}
    promoted_items = []

    for item in stream_ref:
        if item["link"] in existing_filtered_links:
            continue
        title = item.get("title", "")
        matched_kws = [kw for kw in new_keywords if kw.strip() and keyword_matches(kw.strip(), title)]
        if not matched_kws:
            continue

        has_positive = any(kw in positive_set for kw in matched_kws)
        has_negative = any(kw in negative_set for kw in matched_kws)
        if has_positive and has_negative:
            item["sentiment"] = "mixed"
        elif has_positive:
            item["sentiment"] = "positive"
        elif has_negative:
            item["sentiment"] = "negative"
        else:
            item["sentiment"] = "neutral"

        promoted_items.append(item)
        existing_filtered_links.add(item["link"])

    if not promoted_items:
        return

    filtered_ref[:0] = promoted_items
    filtered_ref.sort(key=lambda x: x["timestamp"], reverse=True)
    _safe_atomic_append_write(filtered_file, promoted_items)

    print(f"⭐ [소급 포착조치] 신규 등록 키워드로 {len(promoted_items)}건의 기사를 키워드 포착 피드로 편입했습니다.")


@app.post("/api/keywords")
async def post_keywords(updated_categorized: dict):
    global cached_keywords
    with db_lock:
        newly_added = list(_flatten_categorized_keywords(updated_categorized) - _flatten_categorized_keywords(cached_keywords))

        cached_keywords.clear()
        cached_keywords.update(updated_categorized)
        save_keywords(cached_keywords)

        positive_set = set(cached_keywords.get("positive", []))
        negative_set = set(cached_keywords.get("negative", []))
        _promote_newly_matched_keywords(newly_added, positive_set, negative_set,
                                         cached_stream, cached_filtered, FILTERED_NEWS_FILE)
    return {"status": "success"}


@app.post("/api/global-keywords")
async def post_global_keywords(updated_categorized: dict):
    global cached_global_keywords
    with db_lock:
        newly_added = list(_flatten_categorized_keywords(updated_categorized) - _flatten_categorized_keywords(cached_global_keywords))

        cached_global_keywords.clear()
        cached_global_keywords.update(updated_categorized)
        save_keywords(cached_global_keywords, GLOBAL_KEYWORD_DB_FILE)

        positive_set = set(cached_global_keywords.get("positive", []))
        negative_set = set(cached_global_keywords.get("negative", []))
        _promote_newly_matched_keywords(newly_added, positive_set, negative_set,
                                         cached_global_stream, cached_global_filtered, GLOBAL_FILTERED_NEWS_FILE)
    return {"status": "success"}


def _relocate_newly_blacklisted(new_keywords, stream_ref, filtered_ref, blacklisted_ref,
                                 stream_file, filtered_file, blacklisted_file):
    """
    신규로 등록된 제외 키워드에 걸리는 기사를 실시간/필터링 피드에서 걷어내
    격리 보관소로 소급 이동시킨다. (db_lock을 보유한 상태에서 호출되어야 함)
    신규 키워드에 한해서만 검사하므로 전체 재검사 대비 부하가 크지 않다.
    국내/해외 파이프라인이 각자의 캐시·파일 세트를 넘겨 동일 로직을 공유한다.
    """
    if not new_keywords:
        return

    existing_blacklisted_links = {item["link"] for item in blacklisted_ref}
    moved_items = []
    remaining_stream = []
    remaining_filtered = []

    for item in stream_ref:
        title = item.get("title", "")
        if any(keyword_matches(kw, title) for kw in new_keywords):
            if item["link"] not in existing_blacklisted_links:
                moved_items.append(item)
                existing_blacklisted_links.add(item["link"])
        else:
            remaining_stream.append(item)

    moved_links = {item["link"] for item in moved_items}

    for item in filtered_ref:
        if item["link"] in moved_links:
            continue
        title = item.get("title", "")
        if any(keyword_matches(kw, title) for kw in new_keywords):
            if item["link"] not in existing_blacklisted_links:
                moved_items.append(item)
                moved_links.add(item["link"])
                existing_blacklisted_links.add(item["link"])
        else:
            remaining_filtered.append(item)

    if not moved_items:
        return

    stream_ref[:] = remaining_stream
    filtered_ref[:] = remaining_filtered
    blacklisted_ref[:0] = moved_items
    blacklisted_ref.sort(key=lambda x: x["timestamp"], reverse=True)

    _safe_atomic_write(stream_file, remaining_stream)
    _safe_atomic_write(filtered_file, remaining_filtered)
    _safe_atomic_append_write(blacklisted_file, moved_items)

    print(f"🗑️ [소급 격리조치] 신규 제외 키워드로 {len(moved_items)}건의 기사를 격리 보관소로 이동했습니다.")


@app.post("/api/blacklist")
async def post_blacklist(updated_blacklist: list = Body(...)):
    global cached_blacklist
    with db_lock:
        old_set = {kw.strip() for kw in cached_blacklist if kw.strip()}
        new_set = {kw.strip() for kw in updated_blacklist if kw.strip()}
        newly_added = list(new_set - old_set)

        cached_blacklist.clear()
        cached_blacklist.extend(updated_blacklist)
        save_blacklist(cached_blacklist)

        _relocate_newly_blacklisted(newly_added, cached_stream, cached_filtered, cached_blacklisted,
                                     STREAM_NEWS_FILE, FILTERED_NEWS_FILE, BLACKLISTED_NEWS_FILE)
    return {"status": "success"}


@app.post("/api/global-blacklist")
async def post_global_blacklist(updated_blacklist: list = Body(...)):
    global cached_global_blacklist
    with db_lock:
        old_set = {kw.strip() for kw in cached_global_blacklist if kw.strip()}
        new_set = {kw.strip() for kw in updated_blacklist if kw.strip()}
        newly_added = list(new_set - old_set)

        cached_global_blacklist.clear()
        cached_global_blacklist.extend(updated_blacklist)
        save_blacklist(cached_global_blacklist, GLOBAL_BLACKLIST_DB_FILE)

        _relocate_newly_blacklisted(newly_added, cached_global_stream, cached_global_filtered, cached_global_blacklisted,
                                     GLOBAL_STREAM_NEWS_FILE, GLOBAL_FILTERED_NEWS_FILE, GLOBAL_BLACKLISTED_NEWS_FILE)
    return {"status": "success"}


@app.post("/api/rss")
async def post_rss(updated_rss_urls: list = Body(...)):
    global cached_rss
    with db_lock:
        cached_rss.clear()
        for item in updated_rss_urls:
            cached_rss.append(
                {"name": item.get("name", "수집 채널"), "url": item.get("url", ""), "enabled": item.get("enabled", True)})
        save_rss(cached_rss)
    return {"status": "success"}


@app.post("/api/global-rss")
async def post_global_rss(updated_rss_urls: list = Body(...)):
    global cached_global_rss
    with db_lock:
        cached_global_rss.clear()
        for item in updated_rss_urls:
            cached_global_rss.append(
                {"name": item.get("name", "수집 채널"), "url": item.get("url", ""), "enabled": item.get("enabled", True)})
        save_rss(cached_global_rss, GLOBAL_RSS_DB_FILE)
    return {"status": "success"}


# ----------------------------------------------------
# 🚀 서버 최초 실행 및 파일 전체 메모리 적재 프로세스
# ----------------------------------------------------
if __name__ == "__main__":
    print("\n🏁 [시스템 부팅] JSON 데이터베이스 파일 전체를 가상 메모리에 초기 적재합니다...")

    with db_lock:
        initial_stream = load_file_entire_content(STREAM_NEWS_FILE)
        initial_filtered = load_file_entire_content(FILTERED_NEWS_FILE)
        initial_blacklisted = load_file_entire_content(BLACKLISTED_NEWS_FILE)

        for s_item in initial_stream:
            if "time" in s_item and "time_kst" not in s_item: s_item["time_kst"] = s_item["time"]
        for f_item in initial_filtered:
            if "time" in f_item and "time_kst" not in f_item: f_item["time_kst"] = f_item["time"]
        for b_item in initial_blacklisted:
            if "time" in b_item and "time_kst" not in b_item: b_item["time_kst"] = b_item["time"]

        initial_stream.sort(key=lambda x: x["timestamp"], reverse=True)
        initial_filtered.sort(key=lambda x: x["timestamp"], reverse=True)
        initial_blacklisted.sort(key=lambda x: x["timestamp"], reverse=True)

        cached_stream.extend(initial_stream)
        cached_filtered.extend(initial_filtered)
        cached_blacklisted.extend(initial_blacklisted)

        print("--------------------------------------------------------------------------------")
        print(f"메모리 변수 : stream_news        <- {STREAM_NEWS_FILE:<30} ({len(cached_stream):,d}건)")
        print(f"메모리 변수 : filtered_news      <- {FILTERED_NEWS_FILE:<30} ({len(cached_filtered):,d}건)")
        print(f"메모리 변수 : blacklisted_news   <- {BLACKLISTED_NEWS_FILE:<30} ({len(cached_blacklisted):,d}건)")
        print("--------------------------------------------------------------------------------\n")

        # 🌐 [해외 뉴스] 별도 디스크 파일에서 해외 캐시를 초기 적재
        initial_global_stream = load_file_entire_content(GLOBAL_STREAM_NEWS_FILE)
        initial_global_filtered = load_file_entire_content(GLOBAL_FILTERED_NEWS_FILE)
        initial_global_blacklisted = load_file_entire_content(GLOBAL_BLACKLISTED_NEWS_FILE)

        for s_item in initial_global_stream:
            if "time" in s_item and "time_kst" not in s_item: s_item["time_kst"] = s_item["time"]
        for f_item in initial_global_filtered:
            if "time" in f_item and "time_kst" not in f_item: f_item["time_kst"] = f_item["time"]
        for b_item in initial_global_blacklisted:
            if "time" in b_item and "time_kst" not in b_item: b_item["time_kst"] = b_item["time"]

        initial_global_stream.sort(key=lambda x: x["timestamp"], reverse=True)
        initial_global_filtered.sort(key=lambda x: x["timestamp"], reverse=True)
        initial_global_blacklisted.sort(key=lambda x: x["timestamp"], reverse=True)

        cached_global_stream.extend(initial_global_stream)
        cached_global_filtered.extend(initial_global_filtered)
        cached_global_blacklisted.extend(initial_global_blacklisted)

        print("--------------------------------------------------------------------------------")
        print(f"메모리 변수 : global_stream_news        <- {GLOBAL_STREAM_NEWS_FILE:<30} ({len(cached_global_stream):,d}건)")
        print(f"메모리 변수 : global_filtered_news      <- {GLOBAL_FILTERED_NEWS_FILE:<30} ({len(cached_global_filtered):,d}건)")
        print(f"메모리 변수 : global_blacklisted_news   <- {GLOBAL_BLACKLISTED_NEWS_FILE:<30} ({len(cached_global_blacklisted):,d}건)")
        print("--------------------------------------------------------------------------------\n")

    # 📡 1. RSS 일반 스레드 구동 (국내 + 해외 각자 독립 스레드)
    monitor = threading.Thread(
        target=rss_monitor_thread,
        args=(cached_rss, cached_keywords, cached_blacklist, cached_stream, cached_filtered, cached_blacklisted,
              rss_response_status, STREAM_NEWS_FILE, FILTERED_NEWS_FILE, BLACKLISTED_NEWS_FILE, ""),
        daemon=True,
    )
    monitor.start()

    global_monitor = threading.Thread(
        target=rss_monitor_thread,
        args=(cached_global_rss, cached_global_keywords, cached_global_blacklist, cached_global_stream,
              cached_global_filtered, cached_global_blacklisted, global_rss_response_status,
              GLOBAL_STREAM_NEWS_FILE, GLOBAL_FILTERED_NEWS_FILE, GLOBAL_BLACKLISTED_NEWS_FILE, "[해외] "),
        daemon=True,
    )
    global_monitor.start()

    # ⚡ 2. Uvicorn 구동 옵션 및 비동기 루프 제어
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)

    # Uvicorn이 초기화될 때 백업 스케줄러 비동기 태스크를 강제 등록하는 훅(Hook)을 심습니다.
    original_startup = server.startup


    async def custom_startup(**kwargs):
        # 원본 startup 함수에 그대로 kwargs 전달
        await original_startup(**kwargs)

        # 비동기 이벤트 루프에 스케줄러 태스크 등록 명시
        asyncio.create_task(
            back_up_scheduler.daily_backup_and_cleanup_scheduler(
                db_lock=db_lock,
                file_paths=FILE_PATHS_CONFIG,
                memory_caches=MEMORY_CACHES_CONFIG,
                load_content_func=load_file_entire_content,
                print_status_func=print_resource_status
            )
        )
        logger.info("⚡ [엔진 직결] Uvicorn 루프에 백업 스케줄러 비동기 태스크 등록을 완료했습니다.")

        # 🌐 [해외 뉴스] 국내 백업 스케줄러와 완전히 분리된 global_back_up_scheduler 모듈로 별도 등록
        asyncio.create_task(
            global_back_up_scheduler.daily_backup_and_cleanup_scheduler(
                db_lock=db_lock,
                file_paths=FILE_PATHS_CONFIG_GLOBAL,
                memory_caches=MEMORY_CACHES_CONFIG_GLOBAL,
                load_content_func=load_file_entire_content,
                print_status_func=print_global_resource_status
            )
        )
        logger.info("⚡ [엔진 직결] Uvicorn 루프에 해외 뉴스 백업 스케줄러(global_back_up_scheduler) 비동기 태스크 등록을 완료했습니다.")

        # 일정 추출 파이프라인 스케줄러 등록 (백업 스케줄러와 실행 시각 안 겹치게 00:15 KST)
        asyncio.create_task(schedule_extraction_scheduler.daily_schedule_extraction_scheduler())
        logger.info("⚡ [엔진 직결] Uvicorn 루프에 일정 추출 스케줄러 비동기 태스크 등록을 완료했습니다.")

        # 상장사 기업명 동기화 스케줄러 등록 (앞의 둘과 안 겹치게 01:00 KST)
        asyncio.create_task(
            company_list_sync_scheduler.daily_company_sync_scheduler(
                cached_keywords=cached_keywords,
                db_lock=db_lock,
                save_keywords_func=save_keywords
            )
        )
        logger.info("⚡ [엔진 직결] Uvicorn 루프에 기업명 동기화 스케줄러 비동기 태스크 등록을 완료했습니다.")

    server.startup = custom_startup

    # 서버 가동
    asyncio.run(server.serve())