# 공시 전용 텔레그램 채널 (키워드 공시 레이더)

기업공시 알림을 뉴스 채널(키워드 뉴스 레이더)과 분리해 보냅니다.

| 환경변수 | 용도 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 기존 봇 토큰 (두 채널 공용) |
| `TELEGRAM_CHAT_ID` | 키워드 뉴스 레이더 (국내·해외 뉴스, 서버 시작 알림, 일정 추출 알림) |
| `TELEGRAM_DISCLOSURE_CHAT_ID` | 키워드 공시 레이더 (기업공시 실시간 알림, 공시 실적발표 일정 추가 알림) |

`TELEGRAM_DISCLOSURE_CHAT_ID`가 비어 있으면 공시 알림도 기존처럼 `TELEGRAM_CHAT_ID`로 보냅니다.

## 설정 순서

1. 텔레그램에서 새 채널 `키워드 공시 레이더`를 만듭니다.
2. 채널 관리자에 기존 봇을 추가하고 '메시지 게시' 권한을 줍니다.
3. 채널에 아무 메시지나 올린 뒤 `https://api.telegram.org/bot<봇토큰>/getUpdates`를 열어
   `channel_post.chat.id`(`-100`으로 시작하는 숫자)를 확인합니다.
   (응답이 비어 있으면 메시지를 한 번 더 올리고 다시 엽니다.)
4. 서버 `.env`에 `TELEGRAM_DISCLOSURE_CHAT_ID=-100...`을 추가하고 서버를 재시작합니다.
