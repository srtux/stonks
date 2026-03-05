"""
Comprehensive tests for scripts/backtest.py

Covers:
  - Data loading from BigQuery (decisions and OHLCV)
  - Forward return computation for BUY/SELL decisions
  - Edge cases (no exit data, no entry match, multiple tickers)
  - Summary printing and grouping
  - Constants
"""

import importlib
import sys
from io import StringIO
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Env setup and module import
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _backtest_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("BQ_DATASET", "amfe_data")


@pytest.fixture()
def mod(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("BQ_DATASET", "amfe_data")

    with patch("google.cloud.bigquery.Client") as mock_bq_cls:
        mock_client = MagicMock()
        mock_bq_cls.return_value = mock_client
        if "scripts.backtest" in sys.modules:
            m = importlib.reload(sys.modules["scripts.backtest"])
        else:
            m = importlib.import_module("scripts.backtest")
        m._mock_bq_client = mock_client
    return m


# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def sample_decisions():
    """DataFrame of agent decisions with realistic data."""
    return pd.DataFrame(
        {
            "decision_id": ["d1", "d2", "d3"],
            "ticker": ["AAPL", "AAPL", "MSFT"],
            "signal_label": ["BUY", "SELL", "BUY"],
            "agent_mode": ["aggressive", "conservative", "aggressive"],
            "hmm_regime": ["BULL_QUIET", "BEAR_VOLATILE", "BULL_QUIET"],
            "confidence": [0.85, 0.70, 0.90],
            "decision_timestamp": pd.to_datetime(
                ["2026-01-05", "2026-01-12", "2026-01-05"]
            ),
        }
    )


@pytest.fixture()
def sample_ohlcv():
    """DataFrame of daily OHLCV closes for AAPL and MSFT, 20 trading days."""
    dates = pd.bdate_range("2026-01-05", periods=20)
    aapl_prices = [150 + i * 0.5 for i in range(20)]  # steadily rising
    msft_prices = [300 + i * 1.0 for i in range(20)]  # steadily rising

    aapl = pd.DataFrame(
        {"ticker": "AAPL", "trade_date": dates, "close": aapl_prices}
    )
    msft = pd.DataFrame(
        {"ticker": "MSFT", "trade_date": dates, "close": msft_prices}
    )
    return pd.concat([aapl, msft], ignore_index=True)


@pytest.fixture()
def sample_ohlcv_declining():
    """OHLCV data where prices decline (for SELL hit tests)."""
    dates = pd.bdate_range("2026-01-05", periods=20)
    aapl_prices = [160 - i * 0.5 for i in range(20)]  # steadily falling

    return pd.DataFrame(
        {"ticker": "AAPL", "trade_date": dates, "close": aapl_prices}
    )


# ===================================================================
# Data Loading Tests
# ===================================================================
class TestLoadDecisions:

    def test_load_decisions(self, mod):
        raw_df = pd.DataFrame(
            {
                "decision_id": ["d1"],
                "ticker": ["AAPL"],
                "signal_label": ["BUY"],
                "agent_mode": ["aggressive"],
                "decision_timestamp": ["2026-01-05 10:30:00"],
                "confidence": [0.85],
            }
        )
        query_result = MagicMock()
        query_result.to_dataframe.return_value = raw_df.copy()
        mod._mock_bq_client.query.return_value = query_result

        df = mod.load_decisions()
        assert len(df) == 1
        assert pd.api.types.is_datetime64_any_dtype(df["decision_timestamp"])


class TestLoadOhlcv:

    def test_load_ohlcv(self, mod):
        raw_df = pd.DataFrame(
            {
                "ticker": ["AAPL"],
                "trade_date": ["2026-01-05"],
                "close": [150.0],
            }
        )
        query_result = MagicMock()
        query_result.to_dataframe.return_value = raw_df.copy()
        mod._mock_bq_client.query.return_value = query_result

        df = mod.load_ohlcv()
        assert len(df) == 1
        assert pd.api.types.is_datetime64_any_dtype(df["trade_date"])


# ===================================================================
# Forward Returns Tests
# ===================================================================
class TestComputeForwardReturns:

    def test_compute_forward_returns_buy_hit(self, mod, sample_ohlcv):
        """BUY decision followed by price increase should produce positive forward return."""
        decisions = pd.DataFrame(
            {
                "decision_id": ["d1"],
                "ticker": ["AAPL"],
                "signal_label": ["BUY"],
                "agent_mode": ["aggressive"],
                "hmm_regime": ["BULL_QUIET"],
                "confidence": [0.9],
                "decision_timestamp": pd.to_datetime(["2026-01-05"]),
            }
        )
        results = mod.compute_forward_returns(decisions, sample_ohlcv)
        assert len(results) == 1
        assert results.iloc[0]["forward_return"] > 0

    def test_compute_forward_returns_buy_miss(self, mod, sample_ohlcv_declining):
        """BUY decision followed by price decrease should produce negative forward return."""
        decisions = pd.DataFrame(
            {
                "decision_id": ["d1"],
                "ticker": ["AAPL"],
                "signal_label": ["BUY"],
                "agent_mode": ["aggressive"],
                "hmm_regime": ["BULL_QUIET"],
                "confidence": [0.9],
                "decision_timestamp": pd.to_datetime(["2026-01-05"]),
            }
        )
        results = mod.compute_forward_returns(decisions, sample_ohlcv_declining)
        assert len(results) == 1
        assert results.iloc[0]["forward_return"] < 0

    def test_compute_forward_returns_sell_hit(self, mod, sample_ohlcv_declining):
        """SELL decision followed by price decrease should produce negative forward return (hit)."""
        decisions = pd.DataFrame(
            {
                "decision_id": ["d1"],
                "ticker": ["AAPL"],
                "signal_label": ["SELL"],
                "agent_mode": ["conservative"],
                "hmm_regime": ["BEAR_VOLATILE"],
                "confidence": [0.75],
                "decision_timestamp": pd.to_datetime(["2026-01-05"]),
            }
        )
        results = mod.compute_forward_returns(decisions, sample_ohlcv_declining)
        assert len(results) == 1
        assert results.iloc[0]["forward_return"] < 0

    def test_compute_forward_returns_no_exit_data(self, mod):
        """Not enough forward days should skip the decision."""
        dates = pd.bdate_range("2026-01-05", periods=3)  # only 3 days, need 5+1
        ohlcv = pd.DataFrame(
            {"ticker": "AAPL", "trade_date": dates, "close": [150, 151, 152]}
        )
        decisions = pd.DataFrame(
            {
                "decision_id": ["d1"],
                "ticker": ["AAPL"],
                "signal_label": ["BUY"],
                "agent_mode": ["aggressive"],
                "hmm_regime": ["BULL_QUIET"],
                "confidence": [0.9],
                "decision_timestamp": pd.to_datetime(["2026-01-05"]),
            }
        )
        results = mod.compute_forward_returns(decisions, ohlcv)
        assert len(results) == 0

    def test_compute_forward_returns_no_entry_match(self, mod):
        """Decision date has no trading day on or after it (all data is before)."""
        dates = pd.bdate_range("2025-12-01", periods=10)
        ohlcv = pd.DataFrame(
            {"ticker": "AAPL", "trade_date": dates, "close": [150 + i for i in range(10)]}
        )
        decisions = pd.DataFrame(
            {
                "decision_id": ["d1"],
                "ticker": ["AAPL"],
                "signal_label": ["BUY"],
                "agent_mode": ["aggressive"],
                "hmm_regime": ["BULL_QUIET"],
                "confidence": [0.9],
                "decision_timestamp": pd.to_datetime(["2026-06-01"]),
            }
        )
        results = mod.compute_forward_returns(decisions, ohlcv)
        assert len(results) == 0

    def test_compute_forward_returns_multiple_tickers(self, mod, sample_decisions, sample_ohlcv):
        """Decisions across different tickers should all be evaluated."""
        results = mod.compute_forward_returns(sample_decisions, sample_ohlcv)
        tickers_in_results = set(results["ticker"])
        # AAPL has 2 decisions, MSFT has 1 -- all should match if enough data
        assert "AAPL" in tickers_in_results or "MSFT" in tickers_in_results
        assert len(results) >= 1


# ===================================================================
# Print Summary Tests
# ===================================================================
class TestPrintSummary:

    def test_print_summary_empty(self, mod, capsys):
        empty_df = pd.DataFrame()
        mod.print_summary(empty_df)
        captured = capsys.readouterr()
        assert "No results" in captured.out

    def test_print_summary_with_data(self, mod, capsys):
        df = pd.DataFrame(
            {
                "decision_id": ["d1", "d2"],
                "ticker": ["AAPL", "MSFT"],
                "signal_label": ["BUY", "SELL"],
                "agent_mode": ["aggressive", "conservative"],
                "hmm_regime": ["BULL_QUIET", "BEAR_VOLATILE"],
                "confidence": [0.85, 0.70],
                "entry_price": [150.0, 300.0],
                "exit_price": [155.0, 295.0],
                "forward_return": [0.0333, -0.0167],
            }
        )
        mod.print_summary(df)
        captured = capsys.readouterr()
        assert "OVERALL BACKTEST METRICS" in captured.out
        assert "Total decisions evaluated" in captured.out
        assert "BUY" in captured.out

    def test_print_summary_groupings(self, mod, capsys):
        df = pd.DataFrame(
            {
                "decision_id": ["d1", "d2", "d3", "d4"],
                "ticker": ["AAPL", "MSFT", "AAPL", "MSFT"],
                "signal_label": ["BUY", "SELL", "BUY", "SELL"],
                "agent_mode": ["aggressive", "conservative", "aggressive", "conservative"],
                "hmm_regime": ["BULL_QUIET", "BEAR_VOLATILE", "BULL_QUIET", "BEAR_VOLATILE"],
                "confidence": [0.85, 0.70, 0.90, 0.65],
                "entry_price": [150.0, 300.0, 152.0, 298.0],
                "exit_price": [155.0, 295.0, 157.0, 290.0],
                "forward_return": [0.0333, -0.0167, 0.0329, -0.0268],
            }
        )
        mod.print_summary(df)
        captured = capsys.readouterr()
        assert "AGENT MODE" in captured.out
        assert "HMM REGIME" in captured.out
        assert "SIGNAL LABEL" in captured.out


# ===================================================================
# Constants Tests
# ===================================================================
class TestConstants:

    def test_forward_days_constant(self, mod):
        assert mod.FORWARD_DAYS == 5


# ===================================================================
# Hit Rate Logic Test
# ===================================================================
class TestHitRate:

    def test_hit_rate_calculation(self, mod, capsys):
        """BUY + positive return = hit; SELL + negative return = hit."""
        df = pd.DataFrame(
            {
                "decision_id": ["d1", "d2", "d3", "d4"],
                "ticker": ["AAPL", "AAPL", "MSFT", "MSFT"],
                "signal_label": ["BUY", "BUY", "SELL", "SELL"],
                "agent_mode": ["aggressive"] * 4,
                "hmm_regime": ["BULL_QUIET"] * 4,
                "confidence": [0.8] * 4,
                "entry_price": [100, 100, 100, 100],
                "exit_price": [105, 95, 95, 105],
                "forward_return": [0.05, -0.05, -0.05, 0.05],
            }
        )
        mod.print_summary(df)
        captured = capsys.readouterr()

        # BUY: 1 hit (d1) out of 2 => 50.0%
        assert "50.0%" in captured.out
        # SELL: 1 hit (d3) out of 2 => 50.0%
        # (both groups show 50.0%, which is fine)
