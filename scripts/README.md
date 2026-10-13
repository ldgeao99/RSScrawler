# 서버에서 GitHub 확인 및 자동 재실행

Linux 서버에서 표준 Python 라이브러리와 git만 사용하는 감독 프로세스입니다.
30초마다 `origin/main`을 확인하고 `git pull --ff-only` 후 Python 구문을 검증한 뒤
기존 앱을 종료하고 `main_fastapi.py`를 다시 실행합니다. 앱이 종료되면 다음 확인 주기에
다시 실행합니다. GitHub Actions나 외부 SSH 접속은 필요하지 않습니다.

## 최초 전환

서버에서 저장소 폴더로 이동해 한 번만 실행합니다.

```bash
git pull --ff-only origin main
pgrep -af main_fastapi.py
kill <현재 앱 PID>
# 기존 앱 종료를 확인한 다음 시작합니다.
nohup python3 scripts/auto_run.py --interval 30 > auto_run.log 2>&1 &
```

가상환경을 쓴다면 `python3` 대신 해당 환경의 Python을 사용하세요.
앱도 감독 프로세스와 같은 Python으로 실행됩니다. `.env`와 기존 데이터 파일은 유지됩니다.
서버 계정에서 `git fetch origin main`이 대화형 로그인 없이 성공해야 합니다.
이미 systemd/cron 등 다른 방식으로 앱을 실행 중이면 해당 자동 실행을 먼저 해제하세요.

## 로그 및 종료

```bash
tail -f auto_run.log  # 업데이트 및 재시작 기록
tail -f output.log    # 앱 로그 (누적)
pgrep -af scripts/auto_run.py
kill <자동 실행 프로세스 PID>  # 앱도 함께 정상 종료
```

- 네트워크 오류나 충돌이 있으면 실행 중인 앱은 계속 유지하고 다음 주기에 재시도합니다.
- 추적 파일에 로컬 수정이 있거나 로컬 커밋이 원격보다 앞서 있으면 자동 pull을 건너뜁니다.
- Python 구문 오류가 있으면 기존 앱의 재시작을 보류합니다. 실행 중 발생하는 오류와
  의존성 변경은 별도 조치가 필요하며 패키지를 자동 설치하지 않습니다.
- 변경된 감독 스크립트 자체는 감독 프로세스를 재실행해야 적용됩니다.
- `nohup`은 서버 재부팅 후 자동 시작을 제공하지 않습니다. 재부팅 후에도 자동 실행하려면
  이 감독 스크립트를 systemd 서비스나 사용자 crontab의 `@reboot`로 등록하세요.
