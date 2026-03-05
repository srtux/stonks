"""
Comprehensive tests for ingest_job/main.py

Covers:
  - OHLCV ingestion from Alpaca
  - Macro indicator ingestion from FRED
  - SEC EDGAR filing metadata ingestion
  - Dataform workflow trigger
  - Main orchestration function
  - Helper utilities

All external dependencies (Alpaca, FRED, SEC EDGAR, BigQuery, Dataform) are mocked.
"""

import importlib
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Mock modules that may not be installed locally
# ---------------------------------------------------------------------------
def _ensure_mock_modules():
    """Install mock modules into sys.modules for packages not available locally."""
    mocks = {}
    for mod_path in [
        "alpaca",
        "alpaca.data",
        "alpaca.data.requests",
        "alpaca.data.timeframe",
        "fredapi",
        "dotenv",
    ]:
        if mod_path not in sys.modules:
            m = MagicMock()
            sys.modules[mod_path] = m
            mocks[mod_path] = m
    # Set up TimeFrame.Day
    sys.modules["alpaca.data.timeframe"].TimeFrame = SimpleNamespace(Day="1Day")
    return mocks


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _ingest_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("BQ_DATASET", "amfe_data")
    monkeypatch.setenv("ALPACA_API_KEY", "fake-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fake-secret")
    monkeypatch.setenv("FRED_API_KEY", "fake-fred-key")
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "TestBot test@example.com")


@pytest.fixture()
def mod(monkeypatch):
    """Import/reload ingest_job.main with all external modules mocked."""
    _ensure_mock_modules()
    with patch("google.cloud.bigquery.Client"):
        if "ingest_job.main" in sys.modules:
            m = importlib.reload(sys.modules["ingest_job.main"])
        else:
            m = importlib.import_module("ingest_job.main")
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


def _setup_alpaca_mock(mod, bars_response, side_effect=None):
    """Patch the Alpaca client inside ingest_ohlcv.

    ingest_ohlcv does a local import:
        from alpaca.data import StockHistoricalDataClient
    We mock that imported class.
    """
    mock_cls = MagicMock()
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance
    if side_effect:
        mock_instance.get_stock_bars.side_effect = side_effect
    else:
        mock_instance.get_stock_bars.return_value = bars_response
    # Patch the class that gets imported inside the function
    sys.modules["alpaca.data"].StockHistoricalDataClient = mock_cls
    return mock_instance


def _setup_fred_mock(mod, series_return=None, side_effect=None):
    """Patch the Fred client inside ingest_macro."""
    mock_fred_cls = MagicMock()
    mock_fred_inst = MagicMock()
    mock_fred_cls.return_value = mock_fred_inst
    if side_effect:
        mock_fred_inst.get_series.side_effect = side_effect
    else:
        mock_fred_inst.get_series.return_value = series_return
    sys.modules["fredapi"].Fred = mock_fred_cls
    return mock_fred_inst


def _make_bq_client():
    bq = MagicMock()
    load_job = MagicMock()
    load_job.output_rows = 1
    bq.load_table_from_json.return_value = load_job
    return bq


def _make_series(data: dict):
    return pd.Series(list(data.values()), index=list(data.keys()))


# ===================================================================
# OHLCV Ingestion Tests
# ===================================================================
class TestIngestOhlcv:

    def test_ingest_ohlcv_success(self, mod):
        bq = _make_bq_client()
        bars = _make_bars_response({"AAPL": [_make_bar("AAPL"), _make_bar("AAPL")]})
        _setup_alpaca_mock(mod, bars)

        with patch.object(mod, "SP500_TICKERS", ["AAPL"]):
            mod.ingest_ohlcv(bq)

        bq.load_table_from_json.assert_called_once()
        rows = bq.load_table_from_json.call_args[0][0]
        assert len(rows) == 2

    def test_ingest_ohlcv_batching(self, mod):
        bq = _make_bq_client()
        bars = _make_bars_response({})
        alpaca_inst = _setup_alpaca_mock(mod, bars)

        tickers = [f"T{i:03d}" for i in range(75)]
        with patch.object(mod, "SP500_TICKERS", tickers):
            mod.ingest_ohlcv(bq)

        # 75 tickers / 50 per batch = 2 batches
        assert alpaca_inst.get_stock_bars.call_count == 2

    def test_ingest_ohlcv_row_format(self, mod):
        bq = _make_bq_client()
        bars = _make_bars_response({"NVDA": [_make_bar("NVDA")]})
        _setup_alpaca_mock(mod, bars)

        with patch.object(mod, "SP500_TICKERS", ["NVDA"]):
            mod.ingest_ohlcv(bq)

        row = bq.load_table_from_json.call_args[0][0][0]
        expected_keys = {"ticker", "date", "open", "high", "low", "close", "volume", "adj_close"}
        assert set(row.keys()) == expected_keys

    def test_ingest_ohlcv_date_format(self, mod):
        bq = _make_bq_client()
        ts = datetime(2026, 3, 4, 16, 0, 0)
        bars = _make_bars_response({"AAPL": [_make_bar("AAPL", ts=ts)]})
        _setup_alpaca_mock(mod, bars)

        with patch.object(mod, "SP500_TICKERS", ["AAPL"]):
            mod.ingest_ohlcv(bq)

        row = bq.load_table_from_json.call_args[0][0][0]
        assert row["date"] == "2026-03-04"

    def test_ingest_ohlcv_alpaca_error(self, mod):
        """If one batch fails, the other batches are still processed."""
        bq = _make_bq_client()
        good_bars = _make_bars_response({"MSFT": [_make_bar("MSFT")]})
        tickers = [f"T{i:03d}" for i in range(50)] + ["MSFT"]
        _setup_alpaca_mock(mod, None, side_effect=[RuntimeError("API down"), good_bars])

        with patch.object(mod, "SP500_TICKERS", tickers):
            mod.ingest_ohlcv(bq)

        bq.load_table_from_json.assert_called_once()
        rows = bq.load_table_from_json.call_args[0][0]
        assert len(rows) == 1
        assert rows[0]["ticker"] == "MSFT"

    def test_ingest_ohlcv_no_data(self, mod):
        bq = MagicMock()
        bars = _make_bars_response({})
        _setup_alpaca_mock(mod, bars)

        with patch.object(mod, "SP500_TICKERS", ["AAPL"]):
            mod.ingest_ohlcv(bq)

        bq.load_table_from_json.assert_not_called()

    def test_ingest_ohlcv_schema(self, mod):
        bq = _make_bq_client()
        bars = _make_bars_response({"AAPL": [_make_bar()]})
        _setup_alpaca_mock(mod, bars)

        with patch.object(mod, "SP500_TICKERS", ["AAPL"]):
            mod.ingest_ohlcv(bq)

        job_config = bq.load_table_from_json.call_args[1]["job_config"]
        field_names = [f.name for f in job_config.schema]
        assert field_names == ["ticker", "date", "open", "high", "low", "close", "volume", "adj_close"]

    def test_ingest_ohlcv_write_disposition(self, mod):
        bq = _make_bq_client()
        bars = _make_bars_response({"AAPL": [_make_bar()]})
        _setup_alpaca_mock(mod, bars)

        with patch.object(mod, "SP500_TICKERS", ["AAPL"]):
            mod.ingest_ohlcv(bq)

        from google.cloud import bigquery as bq_mod

        job_config = bq.load_table_from_json.call_args[1]["job_config"]
        assert job_config.write_disposition == bq_mod.WriteDisposition.WRITE_TRUNCATE

    def test_ingest_ohlcv_table_id(self, mod):
        bq = _make_bq_client()
        bars = _make_bars_response({"AAPL": [_make_bar()]})
        _setup_alpaca_mock(mod, bars)

        with patch.object(mod, "SP500_TICKERS", ["AAPL"]), \
             patch.object(mod, "PROJECT_ID", "test-project"), \
             patch.object(mod, "BQ_DATASET", "amfe_data"):
            mod.ingest_ohlcv(bq)

        table_id = bq.load_table_from_json.call_args[0][1]
        assert table_id == "test-project.amfe_data.ohlcv_daily"


# ===================================================================
# Macro Ingestion Tests
# ===================================================================
class TestIngestMacro:

    def test_ingest_macro_success(self, mod):
        bq = _make_bq_client()
        ts = pd.Timestamp("2026-03-01")
        fred = _setup_fred_mock(mod, series_return=_make_series({ts: 15.5}))

        mod.ingest_macro(bq)

        assert fred.get_series.call_count == 3
        bq.load_table_from_json.assert_called_once()

    def test_ingest_macro_series_mapping(self, mod):
        bq = _make_bq_client()
        ts = pd.Timestamp("2026-03-01")
        _setup_fred_mock(mod, series_return=_make_series({ts: 10.0}))

        mod.ingest_macro(bq)

        rows = bq.load_table_from_json.call_args[0][0]
        indicators = {r["indicator"] for r in rows}
        assert indicators == {"VIX", "CPI", "FEDFUNDS"}

    def test_ingest_macro_nan_filtering(self, mod):
        bq = _make_bq_client()
        ts1 = pd.Timestamp("2026-03-01")
        ts2 = pd.Timestamp("2026-03-02")
        _setup_fred_mock(mod, series_return=_make_series({ts1: float("nan"), ts2: 20.0}))

        mod.ingest_macro(bq)

        rows = bq.load_table_from_json.call_args[0][0]
        for r in rows:
            assert r["value"] == 20.0

    def test_ingest_macro_dot_filtering(self, mod):
        bq = _make_bq_client()
        ts1 = pd.Timestamp("2026-03-01")
        ts2 = pd.Timestamp("2026-03-02")
        _setup_fred_mock(mod, series_return=_make_series({ts1: ".", ts2: 5.0}))

        mod.ingest_macro(bq)

        rows = bq.load_table_from_json.call_args[0][0]
        for r in rows:
            assert r["value"] == 5.0

    def test_ingest_macro_fred_error(self, mod):
        """If one series fails, others are still fetched."""
        bq = _make_bq_client()
        ts = pd.Timestamp("2026-03-01")
        good = _make_series({ts: 10.0})
        _setup_fred_mock(mod, side_effect=[RuntimeError("FRED down"), good, good])

        mod.ingest_macro(bq)

        bq.load_table_from_json.assert_called_once()
        rows = bq.load_table_from_json.call_args[0][0]
        assert len(rows) == 2

    def test_ingest_macro_no_data(self, mod):
        bq = MagicMock()
        empty = pd.Series([], dtype=float)
        _setup_fred_mock(mod, series_return=empty)

        mod.ingest_macro(bq)

        bq.load_table_from_json.assert_not_called()

    def test_ingest_macro_row_format(self, mod):
        bq = _make_bq_client()
        ts = pd.Timestamp("2026-03-01")
        _setup_fred_mock(mod, series_return=_make_series({ts: 22.5}))

        mod.ingest_macro(bq)

        row = bq.load_table_from_json.call_args[0][0][0]
        assert set(row.keys()) == {"date", "indicator", "value"}

    def test_ingest_macro_write_disposition(self, mod):
        bq = _make_bq_client()
        ts = pd.Timestamp("2026-03-01")
        _setup_fred_mock(mod, series_return=_make_series({ts: 1.0}))

        mod.ingest_macro(bq)

        from google.cloud import bigquery as bq_mod

        job_config = bq.load_table_from_json.call_args[1]["job_config"]
        assert job_config.write_disposition == bq_mod.WriteDisposition.WRITE_TRUNCATE


# ===================================================================
# SEC Filings Ingestion Tests
# ===================================================================
class TestIngestSecFilings:

    @pytest.fixture()
    def company_tickers_json(self):
        return {
            "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corporation"},
        }

    @pytest.fixture()
    def submissions_json(self):
        return {
            "name": "Apple Inc.",
            "filings": {
                "recent": {
                    "form": ["10-K", "10-Q", "8-K", "10-Q"],
                    "filingDate": ["2026-01-15", "2025-10-30", "2025-09-01", "2025-07-20"],
                    "accessionNumber": [
                        "0000320193-26-000001",
                        "0000320193-25-000002",
                        "0000320193-25-000003",
                        "0000320193-25-000004",
                    ],
                    "primaryDocument": ["aapl-10k.htm", "aapl-10q.htm", "aapl-8k.htm", "aapl-10q2.htm"],
                },
            },
        }

    def _mock_requests(self, mod, company_tickers_json, submissions_json):
        resp_tickers = MagicMock()
        resp_tickers.json.return_value = company_tickers_json
        resp_tickers.raise_for_status = MagicMock()

        resp_subs = MagicMock()
        resp_subs.json.return_value = submissions_json
        resp_subs.raise_for_status = MagicMock()

        mock_req = MagicMock()
        mock_req.get.side_effect = lambda url, **kw: (
            resp_tickers if "company_tickers" in url else resp_subs
        )
        return mock_req

    @patch("time.sleep")
    def test_ingest_sec_filings_success(self, mock_sleep, mod, company_tickers_json, submissions_json):
        bq = _make_bq_client()
        mock_req = self._mock_requests(mod, company_tickers_json, submissions_json)

        with patch.object(mod, "requests", mock_req), \
             patch.object(mod, "SP500_TICKERS", ["AAPL"]):
            mod.ingest_sec_filings(bq)

        bq.load_table_from_json.assert_called_once()

    def test_ingest_sec_filings_cik_resolution(self, mod, company_tickers_json, submissions_json):
        mock_req = self._mock_requests(mod, company_tickers_json, submissions_json)
        calls = []
        orig_side_effect = mock_req.get.side_effect

        def tracking_side_effect(url, **kw):
            calls.append(url)
            return orig_side_effect(url, **kw)

        mock_req.get.side_effect = tracking_side_effect

        with patch.object(mod, "requests", mock_req):
            mod._fetch_edgar_via_submissions("AAPL")

        sub_calls = [c for c in calls if "CIK" in c]
        assert len(sub_calls) == 1
        assert "CIK0000320193" in sub_calls[0]

    def test_ingest_sec_filings_submissions_api(self, mod, company_tickers_json, submissions_json):
        mock_req = self._mock_requests(mod, company_tickers_json, submissions_json)
        with patch.object(mod, "requests", mock_req):
            filings = mod._fetch_edgar_via_submissions("AAPL")
        assert len(filings) >= 1

    @patch("time.sleep")
    def test_ingest_sec_filings_rate_limiting(self, mock_sleep, mod, company_tickers_json, submissions_json):
        bq = _make_bq_client()
        mock_req = self._mock_requests(mod, company_tickers_json, submissions_json)

        tickers = [f"T{i:02d}" for i in range(15)]
        with patch.object(mod, "requests", mock_req), \
             patch.object(mod, "SP500_TICKERS", tickers):
            mod.ingest_sec_filings(bq)

        assert mock_sleep.call_count > 0

    def test_ingest_sec_filings_10k_and_10q(self, mod, company_tickers_json, submissions_json):
        mock_req = self._mock_requests(mod, company_tickers_json, submissions_json)
        with patch.object(mod, "requests", mock_req):
            filings = mod._fetch_edgar_via_submissions("AAPL")
        form_types = {f["form_type"] for f in filings}
        assert "10-K" in form_types
        assert "10-Q" in form_types

    def test_ingest_sec_filings_filing_url_format(self, mod, company_tickers_json, submissions_json):
        mock_req = self._mock_requests(mod, company_tickers_json, submissions_json)
        with patch.object(mod, "requests", mock_req):
            filings = mod._fetch_edgar_via_submissions("AAPL")
        for f in filings:
            assert f["filing_url"].startswith("https://www.sec.gov/Archives/edgar/data/")

    @patch("time.sleep")
    def test_ingest_sec_filings_ticker_error(self, mock_sleep, mod):
        """If one ticker fails, others proceed."""
        bq = _make_bq_client()

        with patch.object(mod, "SP500_TICKERS", ["AAPL", "MSFT"]), \
             patch.object(mod, "_fetch_edgar_via_submissions") as mock_fetch:
            mock_fetch.side_effect = [
                RuntimeError("network error"),
                [{"ticker": "MSFT", "filing_date": "2026-01-01", "form_type": "10-K",
                  "filing_url": "http://x", "company_name": "Microsoft"}],
            ]
            mod.ingest_sec_filings(bq)

        bq.load_table_from_json.assert_called_once()

    @patch("time.sleep")
    def test_ingest_sec_filings_no_filings_found(self, mock_sleep, mod):
        bq = MagicMock()
        with patch.object(mod, "SP500_TICKERS", ["AAPL"]), \
             patch.object(mod, "_fetch_edgar_via_submissions", return_value=[]):
            mod.ingest_sec_filings(bq)
        bq.load_table_from_json.assert_not_called()

    def test_ingest_sec_filings_row_format(self, mod, company_tickers_json, submissions_json):
        mock_req = self._mock_requests(mod, company_tickers_json, submissions_json)
        with patch.object(mod, "requests", mock_req):
            filings = mod._fetch_edgar_via_submissions("AAPL")
        expected_keys = {"ticker", "filing_date", "form_type", "filing_url", "company_name"}
        for f in filings:
            assert set(f.keys()) == expected_keys


# ===================================================================
# Dataform Trigger Tests
# ===================================================================
class TestTriggerDataform:

    def test_trigger_dataform_no_env_var(self, mod, monkeypatch):
        monkeypatch.delenv("DATAFORM_REPOSITORY", raising=False)
        mod.trigger_dataform("test-project")

    def test_trigger_dataform_success(self, mod, monkeypatch):
        monkeypatch.setenv("DATAFORM_REPOSITORY", "my-repo")
        monkeypatch.setenv("DATAFORM_LOCATION", "us-central1")

        mock_dataform = MagicMock()
        mock_client = MagicMock()
        mock_dataform.DataformClient.return_value = mock_client

        comp_result = MagicMock()
        comp_result.name = "projects/test-project/compilationResults/123"
        mock_client.create_compilation_result.return_value = comp_result

        wf_result = MagicMock()
        wf_result.name = "projects/test-project/workflowInvocations/456"
        mock_client.create_workflow_invocation.return_value = wf_result

        with patch.dict("sys.modules", {"google.cloud.dataform_v1beta1": mock_dataform}):
            mod.trigger_dataform("test-project")

        mock_client.create_compilation_result.assert_called_once()
        mock_client.create_workflow_invocation.assert_called_once()

    def test_trigger_dataform_import_error(self, mod, monkeypatch):
        monkeypatch.setenv("DATAFORM_REPOSITORY", "my-repo")
        # Setting module to None triggers ImportError on `from X import Y`
        with patch.dict("sys.modules", {"google.cloud.dataform_v1beta1": None}):
            mod.trigger_dataform("test-project")

    def test_trigger_dataform_api_error(self, mod, monkeypatch):
        monkeypatch.setenv("DATAFORM_REPOSITORY", "my-repo")
        mock_dataform = MagicMock()
        mock_client = MagicMock()
        mock_dataform.DataformClient.return_value = mock_client
        mock_client.create_compilation_result.side_effect = RuntimeError("API error")

        with patch.dict("sys.modules", {"google.cloud.dataform_v1beta1": mock_dataform}):
            mod.trigger_dataform("test-project")


# ===================================================================
# Main Function Tests
# ===================================================================
class TestMain:

    def test_main_all_success(self, mod):
        with patch.object(mod, "ingest_ohlcv"), \
             patch.object(mod, "ingest_macro"), \
             patch.object(mod, "ingest_sec_filings"), \
             patch.object(mod, "trigger_dataform"), \
             patch("google.cloud.bigquery.Client"):
            mod.main()

    def test_main_partial_failure(self, mod):
        with patch.object(mod, "ingest_ohlcv", side_effect=RuntimeError("fail")), \
             patch.object(mod, "ingest_macro"), \
             patch.object(mod, "ingest_sec_filings"), \
             patch.object(mod, "trigger_dataform"), \
             patch("google.cloud.bigquery.Client"):
            with pytest.raises(SystemExit) as exc_info:
                mod.main()
            assert exc_info.value.code == 1

    def test_main_all_fail(self, mod):
        with patch.object(mod, "ingest_ohlcv", side_effect=RuntimeError("fail1")), \
             patch.object(mod, "ingest_macro", side_effect=RuntimeError("fail2")), \
             patch.object(mod, "ingest_sec_filings", side_effect=RuntimeError("fail3")), \
             patch.object(mod, "trigger_dataform", side_effect=RuntimeError("fail4")), \
             patch("google.cloud.bigquery.Client"):
            with pytest.raises(SystemExit) as exc_info:
                mod.main()
            assert exc_info.value.code == 1

    def test_main_bq_client_created(self, mod):
        with patch.object(mod, "ingest_ohlcv"), \
             patch.object(mod, "ingest_macro"), \
             patch.object(mod, "ingest_sec_filings"), \
             patch.object(mod, "trigger_dataform"), \
             patch("google.cloud.bigquery.Client") as mock_bq_cls, \
             patch.object(mod, "PROJECT_ID", "test-project"):
            mod.main()

        mock_bq_cls.assert_called_once_with(project="test-project")


# ===================================================================
# Helper Tests
# ===================================================================
class TestTableRef:

    def test_table_ref(self, mod):
        with patch.object(mod, "PROJECT_ID", "my-project"), \
             patch.object(mod, "BQ_DATASET", "my_dataset"):
            result = mod._table_ref("my_table")
        assert result == "my-project.my_dataset.my_table"
