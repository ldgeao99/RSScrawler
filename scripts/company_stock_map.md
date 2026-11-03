# 기업명·종목코드 동기화

기존 기업명 동기화 배치(매일 01:00 KST)가 KRX 목록을 한 번 조회하여
`base_info/company_stock_map.json`에 다음 형식으로 저장합니다.

```json
{
  "updated_at": "2026-11-03T01:00:00+09:00",
  "source": "FinanceDataReader.StockListing(KRX)",
  "companies": [
    {"code": "005930", "name": "삼성전자"}
  ]
}
```

코드는 앞자리 0 및 영문을 보존한 6자리 문자열입니다. 매칭 파일은 매일 조회한
목록으로 교체하며 빈 조회나 잘못된 코드가 있으면 기존 파일을 유지합니다.
종목명 변경도 매칭 파일에 반영됩니다.

키워드 관리 센터의 `base_info/keyword_list_include.json` 및 `company_kr`는
기존 기업명 문자열 목록을 유지합니다. 수동 등록 기업명을 삭제하거나 코드·객체로
변경하지 않으며, 기존처럼 새로운 기업명만 추가합니다. 매칭 파일에는 KRX 조회 결과만
저장하므로 사용자가 등록한 별칭은 자동으로 종목코드에 연결하지 않습니다.

운영 데이터 파일은 Git에서 제외되며, 서버에서 배치 실행 시 생성됩니다.
수동 갱신은 기존처럼 프로젝트 루트에서 `python company_list_sync_scheduler.py`로 실행합니다.
