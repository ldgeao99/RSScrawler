"""
LLM API 비용 실시간 추적 유틸리티.

Gemini 콘솔은 과금 반영까지 지연이 커서 사이클당 실사용 비용을 바로 확인하기 어렵다.
API 응답에 포함된 토큰 사용량(usage_metadata / usage)을 직접 집계해서
콘솔을 기다리지 않고 파이프라인 종료 직후 예상 비용을 알 수 있도록 한다.

단가는 실제 청구 기준과 오차가 있을 수 있으므로 참고용 추정치이며,
가격이 바뀌면 PRICING_USD_PER_1M 값을 업데이트해야 한다.
"""

import logging

logger = logging.getLogger("news_logger")

USD_TO_KRW = 1450  # 필요 시 환율 갱신

# 모델별 1M 토큰당 단가 (USD). (input, output)
PRICING_USD_PER_1M = {
    "models/gemini-3.5-flash": {"input": 1.50, "output": 9.00},
    "models/gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
}


class CostTracker:
    """단일 파이프라인 실행(1 사이클) 동안의 토큰/비용 누적기."""

    def __init__(self):
        # stage_name -> {"input": int, "output": int, "calls": int}
        self._by_stage = {}

    def add_usage(self, stage: str, model: str, input_tokens: int, output_tokens: int):
        entry = self._by_stage.setdefault(stage, {"model": model, "input": 0, "output": 0, "calls": 0})
        entry["input"] += input_tokens or 0
        entry["output"] += output_tokens or 0
        entry["calls"] += 1

    @staticmethod
    def _cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
        rate = PRICING_USD_PER_1M.get(model)
        if not rate:
            logger.warning(f"⚠️ 단가 미등록 모델 '{model}' - 비용 추정에서 제외됩니다.")
            return 0.0
        return (input_tokens / 1_000_000) * rate["input"] + (output_tokens / 1_000_000) * rate["output"]

    def stage_cost_usd(self, stage: str) -> float:
        entry = self._by_stage.get(stage)
        if not entry:
            return 0.0
        return self._cost_usd(entry["model"], entry["input"], entry["output"])

    def total_cost_usd(self) -> float:
        return sum(self.stage_cost_usd(stage) for stage in self._by_stage)

    def print_summary(self):
        if not self._by_stage:
            logger.info("💰 [비용 요약] 이번 사이클에 집계된 API 호출이 없습니다.")
            return

        logger.info("💰 [비용 요약] ── 사이클 예상 비용 (usage_metadata 기준 추정치) ──")
        total_usd = 0.0
        for stage, entry in self._by_stage.items():
            cost = self._cost_usd(entry["model"], entry["input"], entry["output"])
            total_usd += cost
            logger.info(
                f"   ▸ {stage} [{entry['model']}] "
                f"호출 {entry['calls']}회 / 입력 {entry['input']:,}tok / 출력 {entry['output']:,}tok "
                f"→ ${cost:.4f} (약 {cost * USD_TO_KRW:,.0f}원)"
            )
        logger.info(
            f"💰 [비용 요약] 총 예상 비용: ${total_usd:.4f} (약 {total_usd * USD_TO_KRW:,.0f}원)"
        )

    def check_budget(self, monthly_budget_krw: float, expected_runs_per_month: int = 30):
        """이번 1회 실행 비용을 월 예산과 대조해 초과 가능성을 즉시 경고."""
        total_krw = self.total_cost_usd() * USD_TO_KRW
        projected_monthly_krw = total_krw * expected_runs_per_month
        if projected_monthly_krw > monthly_budget_krw:
            logger.warning(
                f"⚠️ [예산 경고] 이번 실행 {total_krw:,.0f}원 기준 월 환산 시 약 {projected_monthly_krw:,.0f}원으로 "
                f"목표 예산({monthly_budget_krw:,.0f}원)을 초과할 것으로 추정됩니다."
            )
        else:
            logger.info(
                f"✅ [예산 확인] 이번 실행 {total_krw:,.0f}원 기준 월 환산 약 {projected_monthly_krw:,.0f}원 "
                f"(목표 {monthly_budget_krw:,.0f}원 이내)."
            )
