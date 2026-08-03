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
import schedule_extraction_scheduler

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
TELEGRAM_MAX_MESSAGE_LEN = 3500  # 텔레그램 4096자 제한 대비 여유


def send_telegram_notification(items):
    """키워드 포착된 신규 기사를 텔레그램으로 실시간 전송. 설정 없으면 조용히 스킵."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not items:
        return

    lines = []
    for item in items:
        title = html.escape(item.get("title", ""))
        source = html.escape(item.get("source", ""))
        link = item.get("link", "")
        lines.append(f'⭐ <a href="{link}">{title}</a>\n   <i>{source}</i>')

    # 메시지 길이 제한에 맞춰 청크 분할
    chunks = []
    current = "🔥 <b>키워드 포착 뉴스</b>\n\n"
    for line in lines:
        if len(current) + len(line) + 2 > TELEGRAM_MAX_MESSAGE_LEN:
            chunks.append(current)
            current = ""
        current += line + "\n\n"
    if current.strip():
        chunks.append(current)

    for chunk in chunks:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"⚠️ 텔레그램 전송 실패 ({resp.status_code}): {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"⚠️ 텔레그램 전송 에러: {e}")

# ====================================================

RSS_DB_FILE = "base_info/rss_list.json"
KEYWORD_DB_FILE = "base_info/keyword_list_include.json"
BLACKLIST_DB_FILE = "base_info/keyword_list_exclude.json"
FILTERED_NEWS_FILE = "news_list_filtered.json"
STREAM_NEWS_FILE = "news_list_stream.json"
BLACKLISTED_NEWS_FILE = "news_list_blacklisted.json"

DEFAULT_CATEGORIZED_KEYWORDS = {
    "etc": ["속보", "트럼프", "이란", "단독", "특징주", "계약", "대통령", "美", "젠슨황", "중", "北"],
    "sector": ["반도체", "조선"],
    "company_kr": ["삼성전자", "SK하이닉스", "현대차"],
    "global_company": ["엔비디아", "애플", "테슬라", "도요타", "소니", "보스턴다이나믹스"],
    "brokerage": ["골드만삭스", "JP모건", "모건스탠리"],
    "performance": ["실적", "영업이익", "어닝서프라이즈", "흑자전환", "매출액", "영업익", "적자전환"]
}
DEFAULT_BLACKLIST = ["스팸", "찌라시"]
DEFAULT_RSS_CHANNELS = [
    {"name": "연합뉴스", "url": "https://www.yonhapnewstv.co.kr/category/news/feed/", "enabled": True},
    {"name": "매일경제", "url": "https://www.mk.co.kr/rss/30000001/", "enabled": True}
]

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


def load_keywords():
    if os.path.exists(KEYWORD_DB_FILE):
        try:
            with open(KEYWORD_DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                migrated = {"etc": [], "sector": [], "company_kr": [], "global_company": [], "brokerage": [],
                            "performance": []}
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
                for key in ["etc", "sector", "company_kr", "global_company", "brokerage", "performance"]:
                    if key not in data: data[key] = []
                if "company_us" in data: del data["company_us"]
                if "company" in data: del data["company"]
                return data
        except Exception:
            pass
    return DEFAULT_CATEGORIZED_KEYWORDS


def save_keywords(keywords): _safe_atomic_write(KEYWORD_DB_FILE, keywords)


def load_blacklist():
    if os.path.exists(BLACKLIST_DB_FILE):
        try:
            with open(BLACKLIST_DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return DEFAULT_BLACKLIST


def save_blacklist(blacklist): _safe_atomic_write(BLACKLIST_DB_FILE, blacklist)


def load_rss():
    if os.path.exists(RSS_DB_FILE):
        try:
            with open(RSS_DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for item in data:
                    if "enabled" not in item: item["enabled"] = True
                return data
        except Exception:
            pass
    return DEFAULT_RSS_CHANNELS


def save_rss(rss_list): _safe_atomic_write(RSS_DB_FILE, rss_list)


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


# ----------------------------------------------------
# 📡 실시간 RSS 뉴스 수집 백그라운드 데몬 스레드
# ----------------------------------------------------
def rss_monitor_thread():
    global cached_filtered, cached_stream, cached_blacklisted, rss_response_status
    print("📢 RSS 실시간 백그라운드 관제 엔진이 가동되었습니다.")

    session = requests.Session()
    is_first_scan = True  # 서버 재시작 직후 첫 스캔은 다운타임 동안 밀린 기사가 한꺼번에 잡히므로 텔레그램 알림만 건너뜀

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
                for item in cached_stream: seen_links.add(item["link"])
                for item in cached_filtered: seen_links.add(item["link"])
                for item in cached_blacklisted: seen_links.add(item["link"])

                target_channels = [dict(ch) for ch in cached_rss]
                categorized_kw = dict(cached_keywords)
                current_blacklist = list(cached_blacklist)

            flat_keywords = set()
            for key in ["etc", "sector", "company_kr", "global_company", "brokerage", "performance"]:
                flat_keywords.update(categorized_kw.get(key, []))

            new_stream_items = []
            new_filtered_items = []
            new_blacklisted_items = []

            for ch in target_channels:
                name = ch.get("name", "").strip()
                url = ch.get("url", "")
                enabled = ch.get("enabled", True)

                if not url: continue
                if not enabled:
                    with db_lock: rss_response_status[url] = "OFF"
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
                        rss_response_status[url] = response.status_code
                    response.raise_for_status()
                    print(f" ➔ 🟢 {response.status_code} OK")

                    feed = feedparser.parse(response.text)
                    source_name = name if name and name not in ["기존 채널", "수집 채널"] else feed.feed.get("title", "알 수 없음")

                    ch_entries = []
                    for entry in feed.entries:
                        link = entry.get("link", "")
                        if link in seen_links: continue
                        title = re.sub(r'\s+-\s+한국경제$', '', entry.get("title", ""))
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
                                        dt_kst = dt_obj.replace(tzinfo=timezone(timedelta(hours=9)))
                                        news_timestamp = int(dt_kst.timestamp())
                                        news_time_str = target_date_str
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

                        if any(bl in title for bl in current_blacklist if bl.strip()):
                            new_blacklisted_items.append(item)
                            print(f"    🗑️ [노이즈차단] {item['source']} ➔ 제목: {title}")
                        else:
                            new_stream_items.append(item)
                            matched_kws = [kw for kw in flat_keywords if kw.strip() and kw in title]

                            if matched_kws:
                                new_filtered_items.append(item)
                                print(f"    ⭐ [키워드포착] {item['source']} ➔ 키워드: {matched_kws} | 제목: {title}")
                            else:
                                print(f"    🆕 [실시간유입] {item['source']} ➔ 제목: {title}")

                    if channel_new_count > 0:
                        print(f"    └ 🏁 [{name}] 피드 탐색 완료 (신규 유입 기사: {channel_new_count}건)")
                    time.sleep(0.3)

                except Exception as feed_err:
                    print(f" ➔ 🔴 실패 ({feed_err})")
                    time.sleep(0.3)
                    continue

            if new_stream_items or new_filtered_items or new_blacklisted_items:
                _safe_atomic_append_write(STREAM_NEWS_FILE, new_stream_items)
                _safe_atomic_append_write(FILTERED_NEWS_FILE, new_filtered_items)
                _safe_atomic_append_write(BLACKLISTED_NEWS_FILE, new_blacklisted_items)

                with db_lock:
                    if new_stream_items:
                        for idx, item in enumerate(new_stream_items): cached_stream.insert(idx, item)
                    if new_filtered_items:
                        for idx, item in enumerate(new_filtered_items): cached_filtered.insert(idx, item)
                    if new_blacklisted_items:
                        for idx, item in enumerate(new_blacklisted_items): cached_blacklisted.insert(idx, item)

                    cached_stream.sort(key=lambda x: x["timestamp"], reverse=True)
                    cached_filtered.sort(key=lambda x: x["timestamp"], reverse=True)
                    cached_blacklisted.sort(key=lambda x: x["timestamp"], reverse=True)

                print(f"🏁 [결과 요약] 탐색 완료 ➔ 실시간 유입: +{len(new_stream_items)}건 | 키워드 포착: +{len(new_filtered_items)}건")

                if new_filtered_items and not is_first_scan:
                    send_telegram_notification(new_filtered_items)
                elif new_filtered_items and is_first_scan:
                    print(f"🔇 [알림 스킵] 서버 재시작 직후 첫 스캔이라 {len(new_filtered_items)}건의 텔레그램 알림을 건너뜁니다.")
            else:
                print(f"🏁 [결과 요약] 탐색 완료 ➔ 변동 없음")

            print_resource_status()

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


@app.get("/api/rss")
async def get_rss_channels():
    import datetime as dt
    today_start = dt.datetime.combine(dt.date.today(), dt.time.min).timestamp()
    with db_lock:
        response_channels = []
        for ch in cached_rss:
            ch_url = ch.get("url", "")
            ch_name = ch.get("name", "수집 채널")
            ch_status = rss_response_status.get(ch_url, "-")
            today_count = sum(1 for item in cached_stream if
                              item.get("source") == ch_name and item.get("timestamp", 0) >= today_start)
            response_channels.append(
                {"name": ch_name, "url": ch_url, "enabled": ch.get("enabled", True), "today_count": today_count,
                 "status_code": ch_status})
        return response_channels


@app.post("/api/keywords")
async def post_keywords(updated_categorized: dict):
    global cached_keywords
    with db_lock:
        cached_keywords.clear()
        cached_keywords.update(updated_categorized)
        save_keywords(cached_keywords)
    return {"status": "success"}


@app.post("/api/blacklist")
async def post_blacklist(updated_blacklist: list = Body(...)):
    global cached_blacklist
    with db_lock:
        cached_blacklist.clear()
        cached_blacklist.extend(updated_blacklist)
        save_blacklist(cached_blacklist)
    return {"status": "success"}


@app.post("/api/rss")
async def post_rss(updated_rss_urls: list):
    global cached_rss
    with db_lock:
        cached_rss.clear()
        for item in updated_rss_urls:
            cached_rss.append(
                {"name": item.get("name", "수집 채널"), "url": item.get("url", ""), "enabled": item.get("enabled", True)})
        save_rss(cached_rss)
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

    # 📡 1. RSS 일반 스레드 구동
    monitor = threading.Thread(target=rss_monitor_thread, daemon=True)
    monitor.start()

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

        # 일정 추출 파이프라인 스케줄러 등록 (백업 스케줄러와 실행 시각 안 겹치게 00:15 KST)
        asyncio.create_task(schedule_extraction_scheduler.daily_schedule_extraction_scheduler())
        logger.info("⚡ [엔진 직결] Uvicorn 루프에 일정 추출 스케줄러 비동기 태스크 등록을 완료했습니다.")

    server.startup = custom_startup

    # 서버 가동
    asyncio.run(server.serve())