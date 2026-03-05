"""
seed_historical.py — One-time backfill of historical OHLCV and macro data into BigQuery.

Fetches:
  - Daily OHLCV bars (2022-01-01 to present) from Alpaca for ~50 S&P 500 tickers
  - VIX, CPI (CPIAUCSL), and FEDFUNDS from FRED

Writes to:
  - amfe_data.ohlcv_daily
  - amfe_data.macro_indicators

Usage:
    python scripts/seed_historical.py
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

import pandas as pd
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from fredapi import Fred
from google.cloud import bigquery

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
DATASET = os.environ.get("BQ_DATASET", "amfe_data")
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
FRED_API_KEY = os.environ["FRED_API_KEY"]

START_DATE = "2022-01-01"
END_DATE = datetime.utcnow().strftime("%Y-%m-%d")

# Representative ~50 S&P 500 tickers.
# TODO: Expand to full S&P 500 or custom universe as needed.
DEFAULT_TICKERS = [
    "AAPL", "ABBV", "ABT", "ACN", "ADBE",
    "AMD", "AMGN", "AMZN", "AVGO", "BAC",
    "BRK.B", "CAT", "COP", "COST", "CRM",
    "CSCO", "CVX", "DHR", "DIS", "GE",
    "GOOG", "GOOGL", "GS", "HD", "HON",
    "IBM", "INTC", "JNJ", "JPM", "KO",
    "LLY", "MA", "MCD", "META", "MRK",
    "MSFT", "NEE", "NFLX", "NVDA", "ORCL",
    "PEP", "PFE", "PG", "QCOM", "RTX",
    "SBUX", "T", "TSLA", "UNH", "V",
    "WMT", "XOM",
]

BATCH_SIZE = 10  # tickers per Alpaca request batch
SLEEP_BETWEEN_BATCHES = 1.0  # seconds, to respect rate limits

# FRED series to fetch
FRED_SERIES = {
    "VIXCLS": "VIX",
    "CPIAUCSL": "CPI",
    "FEDFUNDS": "FEDFUNDS",
}

# ── BigQuery helpers ──────────────────────────────────────────────────────────

bq_client = bigquery.Client(project=PROJECT)

OHLCV_TABLE = f"{PROJECT}.{DATASET}.ohlcv_daily"
MACRO_TABLE = f"{PROJECT}.{DATASET}.macro_indicators"


def load_df_to_bq(df: pd.DataFrame, table_id: str) -> None:
    """Append a DataFrame to a BigQuery table, creating the table if needed."""
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        autodetect=True,
    )
    job = bq_client.load_table_from_dataframe(df, table_id, job_config=job_config)
    job.result()  # block until done
    logger.info("Loaded %d rows into %s", len(df), table_id)


# ── Alpaca OHLCV ──────────────────────────────────────────────────────────────


def fetch_ohlcv(tickers: list[str]) -> None:
    """Fetch daily OHLCV bars from Alpaca in batches and write to BigQuery."""
    alpaca_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    total = len(tickers)

    for i in range(0, total, BATCH_SIZE):
        batch = tickers[i : i + BATCH_SIZE]
        logger.info("Fetching OHLCV batch %d–%d / %d  (%s …)", i + 1, i + len(batch), total, batch[0])

        request = StockBarsRequest(
            symbol_or_symbols=batch,
            timeframe=TimeFrame.Day,
            start=START_DATE,
            end=END_DATE,
        )

        bars = alpaca_client.get_stock_bars(request)
        rows = []
        for bar in bars.data.values():
            for b in bar:
                rows.append(
                    {
                        "ticker": b.symbol,
                        "date": b.timestamp.strftime("%Y-%m-%d"),
                        "open": float(b.open),
                        "high": float(b.high),
                        "low": float(b.low),
                        "close": float(b.close),
                        "volume": int(b.volume),
                        "adj_close": float(b.close),
                    }
                )

        if rows:
            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"])
            load_df_to_bq(df, OHLCV_TABLE)

        logger.info("Loaded %d / %d tickers", min(i + BATCH_SIZE, total), total)
        time.sleep(SLEEP_BETWEEN_BATCHES)


# ── FRED macro indicators ────────────────────────────────────────────────────


def fetch_macro() -> None:
    """Fetch VIX, CPI, and FEDFUNDS from FRED and write to BigQuery."""
    fred = Fred(api_key=FRED_API_KEY)
    all_rows = []

    for series_id, label in FRED_SERIES.items():
        logger.info("Fetching FRED series: %s (%s)", series_id, label)
        data: pd.Series = fred.get_series(series_id, observation_start=START_DATE)
        for date, value in data.items():
            if pd.notna(value):
                all_rows.append(
                    {
                        "indicator": label,
                        "series_id": series_id,
                        "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
                        "value": float(value),
                    }
                )

    if all_rows:
        df = pd.DataFrame(all_rows)
        df["date"] = pd.to_datetime(df["date"])
        load_df_to_bq(df, MACRO_TABLE)

    logger.info("FRED macro data loaded (%d rows)", len(all_rows))


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    logger.info("Starting historical backfill: %s → %s", START_DATE, END_DATE)

    logger.info("─── Phase 1: OHLCV from Alpaca ───")
    fetch_ohlcv(DEFAULT_TICKERS)

    logger.info("─── Phase 2: Macro from FRED ───")
    fetch_macro()

    logger.info("Backfill complete.")


if __name__ == "__main__":
    main()
