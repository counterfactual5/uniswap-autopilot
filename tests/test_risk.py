"""Tests for token risk scoring, including the pair-age signal."""
import time

from uniswap_autopilot.search.risk import _age_days, _compute_risk, _risk_level


class TestRiskLevels:
    def test_thresholds(self):
        assert _risk_level(0) == "LOW"
        assert _risk_level(25) == "LOW"
        assert _risk_level(26) == "MEDIUM"
        assert _risk_level(50) == "MEDIUM"
        assert _risk_level(51) == "HIGH"
        assert _risk_level(75) == "HIGH"
        assert _risk_level(76) == "EXTREME"


class TestAgeDays:
    def test_none_and_zero(self):
        assert _age_days(None) is None
        assert _age_days(0) is None
        assert _age_days("0") is None

    def test_bad_value(self):
        assert _age_days("not-a-number") is None

    def test_recent_pair(self):
        ms = int((time.time() - 3600) * 1000)  # 1 hour ago
        age = _age_days(ms)
        assert age is not None
        assert 0 < age < 1


class TestComputeRiskAge:
    def _healthy(self):
        # deep liquidity / volume / mcap, no price spike → base score 0
        return dict(liquidity_usd=2_000_000, volume_24h=200_000, market_cap=5_000_000, price_change_24h=1.0)

    def test_no_age_unchanged(self):
        score, warnings = _compute_risk(**self._healthy(), pair_age_days=None)
        assert score == 0

    def test_fresh_pair_high_penalty(self):
        score, warnings = _compute_risk(**self._healthy(), pair_age_days=0.5)
        assert score == 25
        assert any("<24h" in w for w in warnings)

    def test_week_old_pair(self):
        score, _ = _compute_risk(**self._healthy(), pair_age_days=3)
        assert score == 15

    def test_month_old_pair(self):
        score, _ = _compute_risk(**self._healthy(), pair_age_days=20)
        assert score == 5

    def test_mature_pair_no_penalty(self):
        score, _ = _compute_risk(**self._healthy(), pair_age_days=200)
        assert score == 0

    def test_age_stacks_with_other_signals(self):
        # low liquidity (+30) + fresh pair (+25) → EXTREME
        score, _ = _compute_risk(
            liquidity_usd=5_000, volume_24h=200_000,
            market_cap=5_000_000, price_change_24h=1.0, pair_age_days=0.2,
        )
        assert score == 55
        assert _risk_level(score) == "HIGH"
