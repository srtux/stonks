"""
Comprehensive tests for scripts/seed_historical.py

Covers:
  - OHLCV fetching from Alpaca with batching and rate limiting
  - Macro indicator fetching from FRED with NaN filtering
  - BigQuery write operations
  - Main orchestration

All external dependencies (Alpaca, FRED, BigQuery) are mocked.
"""

import importlib
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Mock modules not installed locally (alpaca, fredapi)
# ---------------------------------------------------------------------------
def _ensure_mock_modules():
    mocks = {}
    for mod_path in [
        "alpaca",
        "alpaca.data",
        "alpaca.data.historical",
        "alpaca.data.requests",
        "alpaca.data.timeframe",
        "fredapi",
        "dotenv",
    ]:
        if mod_path not in sys.modules:
            m = MagicMock()
            sys.modules[mod_path] = m
            mocks[mod_path] = m
        else:
            # Ensure submodules exist on parent
            parts = mod_path.split(".")
            if len(parts) > 1:
                parent = ".".join(parts[:-1])
                if parent in sys.modules:
                    setattr(sys.modules[parent], parts[-1], sys.modules[mod_path])

    # Set up TimeFrame.Day
    sys.modules["alpaca.data.timeframe"].TimeFrame = SimpleNamespace(Day="1Day")
    return mocks


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _seed_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("BQ_DATASET", "amfe_data")
    monkeypatch.setenv("ALPACA_API_KEY", "fake-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fake-secret")
    monkeypatch.setenv("FRED_API_KEY", "fake-fred-key")


@pytest.fixture()
def mod(monkeypatch):
    """Import/reload scripts.seed_historical with external deps mocked."""
    _ensure_mock_modules()
    with patch("google.cloud.bigquery.Client") as mock_bq_cls:
        mock_client = MagicMock()
        mock_bq_cls.return_value = mock_client

        if "scripts.seed_historical" in sys.modules:
            m = importlib.reload(sys.modules["scripts.seed_historical"])
        else:
            m = importlib.import_module("scripts.seed_historical")
        m._mock_bq_client = mock_client
        # Also patch the module-level bq_client
        m.bq_client = mock_client
    return m


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_bar(symbol="AAPL", ts=None, o=100.0, h=105.0, lo=99.0, c=102.0, v=1_000_000):
    return SimpleNamespace(
        symbol=symbol,
        timestamp=ts or datetime(2026, 3, 4, 16, 0, 0),
        open=o,
        high=h,
        low=lo,
        close=c,
        volume=v,
    )


def _make_bars_response(bars_by_symbol: dict):
    return SimpleNamespace(data=bars_by_symbol)


def _setup_alpaca_mock(mod, bars_response):
    """Set up the Alpaca mock at module-import level."""
    mock_cls = MagicMock()
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance
    mock_instance.get_stock_bars.return_value = bars_response
    sys.modules["alpaca.data.historical"].StockHistoricalDataClient = mock_cls
    return mock_instance


def _make_series(data: dict):
    return pd.Series(list(data.values()), index=list(data.keys()))


# ===================================================================
# fetch_ohlcv Tests
# ===================================================================
class TestFetchOhlcv:

    @patch("time.sleep")
    def test_fetch_ohlcv_success(self, mock_sleep, mod):
        bars = _make_bars_response({"AAPL": [_make_bar("AAPL")]})
        alpaca_inst = _setup_alpaca_mock(mod, bars)
        load_job = MagicMock()
        mod.bq_client.load_table_from_dataframe.return_value = load_job

        mod.fetch_ohlcv(["AAPL"])

        alpaca_inst.get_stock_bars.assert_called_once()

    @patch("time.sleep")
    def test_fetch_ohlcv_batching(self, mock_sleep, mod):
        """BATCH_SIZE=10, so 25 tickers should produce 3 batches."""
        bars = _make_bars_response({})
        alpaca_inst = _setup_alpaca_mock(mod, bars)
        load_job = MagicMock()
        mod.bq_client.load_table_from_dataframe.return_value = load_job

        tickers = [f"T{i:03d}" for i in range(25)]
        mod.fetch_ohlcv(tickers)

        assert alpaca_inst.get_stock_bars.call_count == 3

    @patch("time.sleep")
    def test_fetch_ohlcv_rate_limiting(self, mock_sleep, mod):
        bars = _make_bars_response({})
        _setup_alpaca_mock(mod, bars)
        load_job = MagicMock()
        mod.bq_client.load_table_from_dataframe.return_value = load_job

        tickers = [f"T{i:03d}" for i in range(25)]
        mod.fetch_ohlcv(tickers)

        # sleep called once per batch (3 batches)
        assert mock_sleep.call_count == 3
        mock_sleep.assert_called_with(1.0)

    @patch("time.sleep")
    def test_fetch_ohlcv_bq_write(self, mock_sleep, mod):
        bars = _make_bars_response({"AAPL": [_make_bar("AAPL")]})
        _setup_alpaca_mock(mod, bars)
        load_job = MagicMock()
        mod.bq_client.load_table_from_dataframe.return_value = load_job

        mod.fetch_ohlcv(["AAPL"])

        mod.bq_client.load_table_from_dataframe.assert_called_once()
        call_args = mod.bq_client.load_table_from_dataframe.call_args
        job_config = call_args[1]["job_config"]

        from google.cloud import bigquery

        assert job_config.write_disposition == bigquery.WriteDisposition.WRITE_APPEND

    @patch("time.sleep")
    def test_fetch_ohlcv_row_format(self, mock_sleep, mod):
        bars = _make_bars_response({"NVDA": [_make_bar("NVDA")]})
        _setup_alpaca_mock(mod, bars)
        load_job = MagicMock()
        mod.bq_client.load_table_from_dataframe.return_value = load_job

        mod.fetch_ohlcv(["NVDA"])

        df_arg = mod.bq_client.load_table_from_dataframe.call_args[0][0]
        expected_cols = {"ticker", "date", "open", "high", "low", "close", "volume", "adj_close"}
        assert set(df_arg.columns) == expected_cols

    @patch("time.sleep")
    def test_fetch_ohlcv_date_format(self, mock_sleep, mod):
        ts = datetime(2026, 3, 4, 16, 0, 0)
        bars = _make_bars_response({"AAPL": [_make_bar("AAPL", ts=ts)]})
        _setup_alpaca_mock(mod, bars)
        load_job = MagicMock()
        mod.bq_client.load_table_from_dataframe.return_value = load_job

        mod.fetch_ohlcv(["AAPL"])

        df_arg = mod.bq_client.load_table_from_dataframe.call_args[0][0]
        date_val = df_arg["date"].iloc[0]
        assert pd.Timestamp(date_val) == pd.Timestamp("2026-03-04")


# ===================================================================
# fetch_macro Tests
# ===================================================================
class TestFetchMacro:

    def test_fetch_macro_success(self, mod):
        ts = pd.Timestamp("2026-03-01")
        series = _make_series({ts: 15.5})
        load_job = MagicMock()
        mod.bq_client.load_table_from_dataframe.return_value = load_job

        mock_fred_cls = MagicMock()
        mock_fred_inst = MagicMock()
        mock_fred_cls.return_value = mock_fred_inst
        mock_fred_inst.get_series.return_value = series
        sys.modules["fredapi"].Fred = mock_fred_cls

        mod.fetch_macro()

        assert mock_fred_inst.get_series.call_count == 3
        mod.bq_client.load_table_from_dataframe.assert_called_once()

    def test_fetch_macro_nan_filtered(self, mod):
        ts1 = pd.Timestamp("2026-03-01")
        ts2 = pd.Timestamp("2026-03-02")
        series = _make_series({ts1: float("nan"), ts2: 10.0})
        load_job = MagicMock()
        mod.bq_client.load_table_from_dataframe.return_value = load_job

        mock_fred_cls = MagicMock()
        mock_fred_inst = MagicMock()
        mock_fred_cls.return_value = mock_fred_inst
        mock_fred_inst.get_series.return_value = series
        sys.modules["fredapi"].Fred = mock_fred_cls

        mod.fetch_macro()

        df_arg = mod.bq_client.load_table_from_dataframe.call_args[0][0]
        # 3 series * 1 valid value each = 3 rows (NaN filtered out)
        assert len(df_arg) == 3
        assert all(df_arg["value"] == 10.0)

    def test_fetch_macro_bq_write(self, mod):
        ts = pd.Timestamp("2026-03-01")
        series = _make_series({ts: 5.0})
        load_job = MagicMock()
        mod.bq_client.load_table_from_dataframe.return_value = load_job

        mock_fred_cls = MagicMock()
        mock_fred_inst = MagicMock()
        mock_fred_cls.return_value = mock_fred_inst
        mock_fred_inst.get_series.return_value = series
        sys.modules["fredapi"].Fred = mock_fred_cls

        mod.fetch_macro()

        call_args = mod.bq_client.load_table_from_dataframe.call_args
        job_config = call_args[1]["job_config"]

        from google.cloud import bigquery

        assert job_config.write_disposition == bigquery.WriteDisposition.WRITE_APPEND


# ===================================================================
# load_df_to_bq Tests
# ===================================================================
class TestLoadDfToBq:

    def test_load_df_to_bq(self, mod):
        df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
        load_job = MagicMock()
        mod.bq_client.load_table_from_dataframe.return_value = load_job

        mod.load_df_to_bq(df, "test-project.amfe_data.test_table")

        mod.bq_client.load_table_from_dataframe.assert_called_once()
        call_args = mod.bq_client.load_table_from_dataframe.call_args
        assert call_args[0][1] == "test-project.amfe_data.test_table"

        job_config = call_args[1]["job_config"]
        assert job_config.autodetect is True

        from google.cloud import bigquery

        assert job_config.write_disposition == bigquery.WriteDisposition.WRITE_APPEND


# ===================================================================
# Main Tests
# ===================================================================
class TestMain:

    @patch("time.sleep")
    def test_main_runs_both_phases(self, mock_sleep, mod):
        with patch.object(mod, "fetch_ohlcv") as mock_ohlcv, \
             patch.object(mod, "fetch_macro") as mock_macro:
            mod.main()

        mock_ohlcv.assert_called_once_with(mod.DEFAULT_TICKERS)
        mock_macro.assert_called_once()
