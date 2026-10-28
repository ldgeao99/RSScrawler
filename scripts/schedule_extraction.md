# 뉴스 임시일정 추출

매일 22:00 KST(주말 포함)에 `news_list_filtered.json` 원본 전체를 읽어
기존 AI 추출 파이프라인으로 처리하고 Firestore `temp_events`에 등록합니다.
입력은 공유 파일 잠금 안에서 한 번 읽으며, AI 처리와 등록 중에는 잠금을 유지하지 않습니다.
산출물은 실행일(KST) 기준 `extracted_schedule/extracted_schedules_YYMMDD.json`입니다.

자정 백업·파일 초기화 배치는 변경하지 않습니다. 두 배치는 기존과 같이 독립된
비동기 스케줄러이며, 별도 프로세스를 추가로 실행할 필요는 없습니다.
22시 스냅샷 이후 자정까지 수집한 뉴스는 자정에 원본이 비워지므로 자동 추출에서 빠질 수 있습니다.

인자 없는 `python schedule_extraction_scheduler.py` 실행은 원본을 즉시 처리합니다.
기존 `python schedule_extraction_scheduler.py YYMMDD`는 지정일 백업을 수동 재처리합니다.
서버에 변경 코드를 반영하고 앱을 재시작해야 새 정기 실행시간이 적용됩니다.
