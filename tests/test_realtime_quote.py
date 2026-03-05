"""
Comprehensive tests for mcp_toolbox/realtime_quote.py -- get_stock_profile function.

Covers BigQuery batch signal retrieval, yfinance real-time quote fetching,
error handling, serialisation, and response structure.
"""

import datetime
import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Patch module-level google.auth.default and bigquery.Client before import,
# since realtime_quote.py executes them at import time.
# Also inject a mock yfinance into sys.modules so that realtime_quote.py's
# top-level ``import yfinance as yf`` does not require the real package.
# ---------------------------------------------------------------------------

_mock_yf_module = MagicMock()
sys.modules.setdefault("yfinance", _mock_yf_module)

_mock_creds = MagicMock()
_mock_bq_client = MagicMock()

_auth_patch = patch("google.auth.default", return_value=(_mock_creds, "test-project"))
_client_patch = patch("google.cloud.bigquery.Client", return_value=_mock_bq_client)
_auth_patch.start()
_client_patch.start()

from mcp_toolbox.realtime_quote import get_stock_profile  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bq_row(data: dict) -> MagicMock:
    """Create a MagicMock behaving like a BigQuery Row."""
    row = MagicMock()
    row.items.return_value = data.items()
    return row


def _setup_bq_return(rows: list[dict]):
    """Configure mock BQ client to return given rows."""
    mock_result = MagicMock()
    mock_result.__iter__ = lambda self: iter([_make_bq_row(r) for r in rows])
    # get_stock_profile wraps in list(): list(bq_client.query(...).result())
    _mock_bq_client.query.return_value.result.return_value = mock_result


def _setup_bq_error(exc: Exception):
    """Configure mock BQ client to raise an exception."""
    _mock_bq_client.query.side_effect = exc


def _clear_bq_side_effect():
    _mock_bq_client.query.side_effect = None


def _make_yf_hist(open_price: float, close_price: float) -> pd.DataFrame:
    """Build a minimal 1-row yfinance history DataFrame."""
    return pd.DataFrame(
        {
            "Open": [open_price],
            "High": [close_price + 1.0],
            "Low": [open_price - 1.0],
            "Close": [close_price],
            "Volume": [50_000_000],
        },
        index=pd.DatetimeIndex([datetime.datetime(2026, 3, 5)]),
    )


_EMPTY_HIST = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


# ---------------------------------------------------------------------------
# Sample row used by multiple tests
# ---------------------------------------------------------------------------

_SAMPLE_ROW = {
    "ticker": "NVDA",
    "date": datetime.date(2026, 3, 4),
    "sector": "Technology",
    "industry": "Semiconductors",
    "market_cap": 2_800_000_000_000.0,
    "rsi_14": 62.5,
    "composite_score": 0.87,
    "signal_label": "STRONG_BUY",
    "last_updated": datetime.datetime(2026, 3, 4, 14, 30, 0),
}


# ---------------------------------------------------------------------------
# Tests -- ticker normalisation
# ---------------------------------------------------------------------------


class TestTickerNormalisation:
    """Verify that ticker is upper-cased and stripped before use."""

    def setup_method(self):
        _mock_bq_client.reset_mock()
        _clear_bq_side_effect()
        _setup_bq_return([_SAMPLE_ROW])

    @patch("mcp_toolbox.realtime_quote.yf")
    def test_get_stock_profile_ticker_uppercased(self, mock_yf):
        """Lowercase ticker should be converted to uppercase."""
        mock_ticker_obj = MagicMock()
        mock_ticker_obj.history.return_value = _make_yf_hist(100, 102)
        mock_yf.Ticker.return_value = mock_ticker_obj

        result = get_stock_profile("nvda")
        assert result["ticker"] == "NVDA"

    @patch("mcp_toolbox.realtime_quote.yf")
    def test_get_stock_profile_ticker_stripped(self, mock_yf):
        """Whitespace around ticker should be stripped."""
        mock_ticker_obj = MagicMock()
        mock_ticker_obj.history.return_value = _make_yf_hist(100, 102)
        mock_yf.Ticker.return_value = mock_ticker_obj

        result = get_stock_profile("  AAPL  ")
        assert result["ticker"] == "AAPL"


# ---------------------------------------------------------------------------
# Tests -- batch signals (BigQuery path)
# ---------------------------------------------------------------------------


class TestBatchSignals:
    """Tests for the BigQuery batch_signals section of get_stock_profile."""

    def setup_method(self):
        _mock_bq_client.reset_mock()
        _clear_bq_side_effect()

    @patch("mcp_toolbox.realtime_quote.yf")
    def test_get_stock_profile_batch_signals_returned(self, mock_yf):
        """BQ row data is correctly mapped into batch_signals."""
        mock_yf.Ticker.return_value.history.return_value = _EMPTY_HIST
        _setup_bq_return([_SAMPLE_ROW])

        result = get_stock_profile("NVDA")
        bs = result["batch_signals"]
        assert bs["ticker"] == "NVDA"
        assert bs["composite_score"] == 0.87
        assert bs["signal_label"] == "STRONG_BUY"

    @patch("mcp_toolbox.realtime_quote.yf")
    def test_get_stock_profile_batch_signals_date_serialized(self, mock_yf):
        """date field in batch_signals is converted to string."""
        mock_yf.Ticker.return_value.history.return_value = _EMPTY_HIST
        _setup_bq_return([_SAMPLE_ROW])

        result = get_stock_profile("NVDA")
        assert result["batch_signals"]["date"] == "2026-03-04"

    @patch("mcp_toolbox.realtime_quote.yf")
    def test_get_stock_profile_batch_signals_timestamp_serialized(self, mock_yf):
        """last_updated field in batch_signals is converted to string."""
        mock_yf.Ticker.return_value.history.return_value = _EMPTY_HIST
        _setup_bq_return([_SAMPLE_ROW])

        result = get_stock_profile("NVDA")
        assert result["batch_signals"]["last_updated"] == "2026-03-04 14:30:00"

    @patch("mcp_toolbox.realtime_quote.yf")
    def test_get_stock_profile_batch_signals_not_found(self, mock_yf):
        """When BQ returns no rows, batch_signals contains an error message."""
        mock_yf.Ticker.return_value.history.return_value = _EMPTY_HIST
        _setup_bq_return([])

        result = get_stock_profile("XYZ")
        assert "error" in result["batch_signals"]
        assert "No batch data found" in result["batch_signals"]["error"]

    @patch("mcp_toolbox.realtime_quote.yf")
    def test_get_stock_profile_batch_signals_bq_error(self, mock_yf):
        """BQ exception sets status to partial_failure and populates error."""
        mock_yf.Ticker.return_value.history.return_value = _EMPTY_HIST
        _setup_bq_error(Exception("Connection refused"))

        result = get_stock_profile("NVDA")
        assert result["status"] == "partial_failure"
        assert "BQ fetch failed" in result["batch_signals"]["error"]
        _clear_bq_side_effect()

    @patch("mcp_toolbox.realtime_quote.yf")
    def test_get_stock_profile_parameterized_query(self, mock_yf):
        """BQ query uses @ticker parameterised placeholder."""
        mock_yf.Ticker.return_value.history.return_value = _EMPTY_HIST
        _setup_bq_return([_SAMPLE_ROW])

        get_stock_profile("NVDA")
        args, kwargs = _mock_bq_client.query.call_args
        query_string = args[0]
        assert "@ticker" in query_string
        job_config = kwargs.get("job_config")
        param = job_config.query_parameters[0]
        assert param.name == "ticker"
        assert param.value == "NVDA"


# ---------------------------------------------------------------------------
# Tests -- realtime quote (yfinance path)
# ---------------------------------------------------------------------------


class TestRealtimeQuote:
    """Tests for the yfinance realtime_quote section of get_stock_profile."""

    def setup_method(self):
        _mock_bq_client.reset_mock()
        _clear_bq_side_effect()
        _setup_bq_return([_SAMPLE_ROW])

    @patch("mcp_toolbox.realtime_quote.yf")
    def test_get_stock_profile_realtime_quote_success(self, mock_yf):
        """Successful yfinance call populates current_price, open_price, intraday_pct_change."""
        mock_yf.Ticker.return_value.history.return_value = _make_yf_hist(145.00, 147.25)
        result = get_stock_profile("NVDA")
        rq = result["realtime_quote"]
        assert rq["current_price"] == 147.25
        assert rq["open_price"] == 145.00
        assert "intraday_pct_change" in rq

    @patch("mcp_toolbox.realtime_quote.yf")
    def test_get_stock_profile_realtime_quote_empty_history(self, mock_yf):
        """Empty yfinance DataFrame produces an error in realtime_quote."""
        mock_yf.Ticker.return_value.history.return_value = _EMPTY_HIST
        result = get_stock_profile("NVDA")
        assert "error" in result["realtime_quote"]
        assert "No realtime trade data" in result["realtime_quote"]["error"]

    @patch("mcp_toolbox.realtime_quote.yf")
    def test_get_stock_profile_realtime_quote_yfinance_error(self, mock_yf):
        """yfinance exception sets status to partial_failure."""
        mock_yf.Ticker.return_value.history.side_effect = Exception("Network error")
        result = get_stock_profile("NVDA")
        assert result["status"] == "partial_failure"
        assert "Realtime fetch failed" in result["realtime_quote"]["error"]

    @patch("mcp_toolbox.realtime_quote.yf")
    def test_get_stock_profile_intraday_pct_calculation(self, mock_yf):
        """Verify ((close - open) / open) formula for intraday_pct_change."""
        open_price = 100.0
        close_price = 105.0
        expected_pct = round((close_price - open_price) / open_price, 4)
        mock_yf.Ticker.return_value.history.return_value = _make_yf_hist(open_price, close_price)

        result = get_stock_profile("NVDA")
        assert result["realtime_quote"]["intraday_pct_change"] == expected_pct

    @patch("mcp_toolbox.realtime_quote.yf")
    def test_get_stock_profile_price_rounding(self, mock_yf):
        """Prices are rounded to 2 decimal places."""
        mock_yf.Ticker.return_value.history.return_value = _make_yf_hist(100.456, 102.789)
        result = get_stock_profile("NVDA")
        rq = result["realtime_quote"]
        assert rq["current_price"] == round(102.789, 2)
        assert rq["open_price"] == round(100.456, 2)

    @patch("mcp_toolbox.realtime_quote.yf")
    def test_get_stock_profile_intraday_pct_rounding(self, mock_yf):
        """Intraday percentage change is rounded to 4 decimal places."""
        open_price = 100.0
        close_price = 103.123456
        mock_yf.Ticker.return_value.history.return_value = _make_yf_hist(open_price, close_price)
        result = get_stock_profile("NVDA")
        pct = result["realtime_quote"]["intraday_pct_change"]
        # Should match round(((close - open) / open), 4)
        expected = round((close_price - open_price) / open_price, 4)
        assert pct == expected


# ---------------------------------------------------------------------------
# Tests -- combined / full response
# ---------------------------------------------------------------------------


class TestFullResponse:
    """End-to-end response structure tests."""

    def setup_method(self):
        _mock_bq_client.reset_mock()
        _clear_bq_side_effect()

    @patch("mcp_toolbox.realtime_quote.yf")
    def test_get_stock_profile_success(self, mock_yf):
        """Both BQ and yfinance succeed: status is 'success', both sections populated."""
        _setup_bq_return([_SAMPLE_ROW])
        mock_yf.Ticker.return_value.history.return_value = _make_yf_hist(145, 147)

        result = get_stock_profile("NVDA")
        assert result["status"] == "success"
        assert result["batch_signals"] is not None
        assert result["realtime_quote"] is not None
        assert "error" not in result["batch_signals"]
        assert "error" not in result["realtime_quote"]

    @patch("mcp_toolbox.realtime_quote.yf")
    def test_get_stock_profile_both_fail(self, mock_yf):
        """When both BQ and yfinance fail, status is partial_failure and both have errors."""
        _setup_bq_error(Exception("BQ down"))
        mock_yf.Ticker.return_value.history.side_effect = Exception("YF down")

        result = get_stock_profile("NVDA")
        assert result["status"] == "partial_failure"
        assert "error" in result["batch_signals"]
        assert "error" in result["realtime_quote"]
        _clear_bq_side_effect()

    @patch("mcp_toolbox.realtime_quote.yf")
    def test_get_stock_profile_response_structure(self, mock_yf):
        """Verify all top-level keys are present in the response."""
        _setup_bq_return([_SAMPLE_ROW])
        mock_yf.Ticker.return_value.history.return_value = _make_yf_hist(100, 102)

        result = get_stock_profile("NVDA")
        assert set(result.keys()) == {"ticker", "batch_signals", "realtime_quote", "status"}
