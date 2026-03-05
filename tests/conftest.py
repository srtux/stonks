"""
Shared pytest fixtures for the StonxAI test suite.

Provides mock GCP clients, environment variable setup, and sample data
fixtures used across test_stock_api.py and test_realtime_quote.py.
"""

import datetime
import pytest
import pandas as pd
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Environment variable fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def set_env_vars(monkeypatch):
    """Set standard environment variables expected by the MCP toolbox modules."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("BQ_DATASET", "amfe_data")
    monkeypatch.setenv("BQ_TABLE", "screening_master")


# ---------------------------------------------------------------------------
# GCP mock fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_google_auth():
    """Patch google.auth.default to return a fake credential and project ID."""
    mock_creds = MagicMock()
    with patch("google.auth.default", return_value=(mock_creds, "test-project")) as auth_mock:
        yield auth_mock


@pytest.fixture()
def mock_bq_client():
    """Patch google.cloud.bigquery.Client with a MagicMock.

    The returned mock is the *class* patch; call ``mock_bq_client.return_value``
    to access the client instance.
    """
    with patch("google.cloud.bigquery.Client") as client_cls:
        yield client_cls


# ---------------------------------------------------------------------------
# Sample BigQuery row data
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_screening_row():
    """Return a single screening_master row dict with all columns populated.

    Dates and timestamps are kept as Python objects so that tests can verify
    serialisation logic.
    """
    return {
        "ticker": "NVDA",
        "date": datetime.date(2026, 3, 4),
        "sector": "Technology",
        "industry": "Semiconductors",
        "market_cap": 2_800_000_000_000.0,
        "rsi_14": 62.5,
        "macd_histogram": 1.35,
        "sma_cross_20_50": 4.12,
        "pe_ratio": 55.3,
        "revenue_growth_yoy": 0.94,
        "hmm_regime": "BULL_QUIET",
        "composite_score": 0.87,
        "signal_label": "STRONG_BUY",
        "bq_forecast_5d_pct": 0.023,
        "last_updated": datetime.datetime(2026, 3, 4, 14, 30, 0),
    }


@pytest.fixture()
def sample_screening_row_serialized(sample_screening_row):
    """Same as sample_screening_row but with date/last_updated as strings."""
    row = dict(sample_screening_row)
    row["date"] = str(row["date"])
    row["last_updated"] = str(row["last_updated"])
    return row


# ---------------------------------------------------------------------------
# yfinance sample data
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_yfinance_history():
    """Return a single-row DataFrame mimicking yfinance ``Ticker.history()``."""
    return pd.DataFrame(
        {
            "Open": [145.00],
            "High": [148.50],
            "Low": [144.20],
            "Close": [147.25],
            "Volume": [52_000_000],
        },
        index=pd.DatetimeIndex([datetime.datetime(2026, 3, 5)]),
    )


@pytest.fixture()
def empty_yfinance_history():
    """Return an empty DataFrame mimicking an empty yfinance history call."""
    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
