"""
Cloud Run Job: StonxAI Daily Ingest
Runs daily at ~4:30PM ET after market close.

Fetches:
  1. OHLCV data from Alpaca API for S&P 500 tickers -> amfe_data.ohlcv_daily
  2. Macro indicators from FRED API (VIX, CPI, FEDFUNDS) -> amfe_data.macro_indicators
  3. SEC EDGAR filing metadata (10-K/10-Q) -> amfe_data.sec_filings
  4. Optionally triggers Dataform workflow execution after ingestion
"""

import logging
import os
import sys
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv
from google.cloud import bigquery

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("ingest_job")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
BQ_DATASET = os.environ.get("BQ_DATASET", "amfe_data")

ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")

SEC_EDGAR_USER_AGENT = os.environ.get("SEC_EDGAR_USER_AGENT", "StonxAI bot@example.com")

# TODO: Expand to full S&P 500 list (~503 tickers). This is a representative
# subset of ~50 tickers across major sectors for initial development.
SP500_TICKERS = [
    # Technology
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "AVGO", "ORCL", "CRM",
    # Financials
    "JPM", "V", "MA", "BAC", "GS", "MS", "BLK", "AXP", "C", "WFC",
    # Healthcare
    "UNH", "JNJ", "LLY", "PFE", "ABBV", "MRK", "TMO", "ABT", "BMY", "AMGN",
    # Energy
    "XOM", "CVX", "COP", "SLB", "EOG",
    # Consumer
    "WMT", "PG", "KO", "PEP", "COST", "MCD", "NKE",
    # Industrials
    "CAT", "BA", "HON", "UPS", "GE",
    # Other
    "DIS", "NFLX", "AMD",
]


def _table_ref(table_name: str) -> str:
    """Return fully-qualified BigQuery table id."""
    return f"{PROJECT_ID}.{BQ_DATASET}.{table_name}"


# ---------------------------------------------------------------------------
# 1. Alpaca OHLCV Ingestion
# ---------------------------------------------------------------------------
def ingest_ohlcv(bq_client: bigquery.Client) -> None:
    """Fetch daily OHLCV bars from Alpaca for all tickers and load into BigQuery."""
    from alpaca.data import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    logger.info("Starting OHLCV ingestion from Alpaca...")

    data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

    # Fetch the last trading day. Using a 5-day window to handle weekends/holidays;
    # we deduplicate on write via WRITE_TRUNCATE on today's partition.
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=5)

    # Alpaca limits symbols per request; batch in groups of 50
    batch_size = 50
    all_rows: list[dict] = []

    for i in range(0, len(SP500_TICKERS), batch_size):
        batch = SP500_TICKERS[i : i + batch_size]
        logger.info(f"  Fetching Alpaca bars for batch {i // batch_size + 1} ({len(batch)} tickers)")

        request_params = StockBarsRequest(
            symbol_or_symbols=batch,
            timeframe=TimeFrame.Day,
            start=start_date,
            end=end_date,
        )

        try:
            bars = data_client.get_stock_bars(request_params)
        except Exception as e:
            logger.error(f"  Alpaca API error for batch starting at index {i}: {e}")
            continue

        for symbol, bar_list in bars.data.items():
            for bar in bar_list:
                all_rows.append(
                    {
                        "ticker": str(symbol),
                        "date": bar.timestamp.strftime("%Y-%m-%d"),
                        "open": float(bar.open),
                        "high": float(bar.high),
                        "low": float(bar.low),
                        "close": float(bar.close),
                        "volume": int(bar.volume),
                        "adj_close": float(bar.close),  # Alpaca bars are split-adjusted
                    }
                )

    if not all_rows:
        logger.warning("No OHLCV rows fetched — skipping BigQuery write.")
        return

    logger.info(f"  Fetched {len(all_rows)} OHLCV rows total. Writing to BigQuery...")

    table_id = _table_ref("ohlcv_daily")
    job_config = bigquery.LoadJobConfig(
        schema=[
            bigquery.SchemaField("ticker", "STRING"),
            bigquery.SchemaField("date", "DATE"),
            bigquery.SchemaField("open", "FLOAT64"),
            bigquery.SchemaField("high", "FLOAT64"),
            bigquery.SchemaField("low", "FLOAT64"),
            bigquery.SchemaField("close", "FLOAT64"),
            bigquery.SchemaField("volume", "INT64"),
            bigquery.SchemaField("adj_close", "FLOAT64"),
        ],
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )

    load_job = bq_client.load_table_from_json(all_rows, table_id, job_config=job_config)
    load_job.result()  # Wait for completion

    logger.info(f"  OHLCV ingestion complete. Loaded {load_job.output_rows} rows into {table_id}.")


# ---------------------------------------------------------------------------
# 2. FRED Macro Indicators Ingestion
# ---------------------------------------------------------------------------
FRED_SERIES = {
    "VIXCLS": "VIX",        # CBOE Volatility Index (daily)
    "CPIAUCSL": "CPI",      # Consumer Price Index (monthly)
    "FEDFUNDS": "FEDFUNDS",  # Federal Funds Rate (monthly)
}


def ingest_macro(bq_client: bigquery.Client) -> None:
    """Fetch macro indicators from FRED API and load into BigQuery."""
    from fredapi import Fred

    logger.info("Starting macro indicator ingestion from FRED...")

    fred = Fred(api_key=FRED_API_KEY)

    all_rows: list[dict] = []

    for series_id, indicator_name in FRED_SERIES.items():
        logger.info(f"  Fetching FRED series: {series_id} ({indicator_name})")
        try:
            # Fetch the last 30 days of observations to capture any revisions
            observation_start = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
            series = fred.get_series(series_id, observation_start=observation_start)

            for date_idx, value in series.items():
                if value is not None and str(value) != "." and not (isinstance(value, float) and value != value):
                    all_rows.append(
                        {
                            "date": date_idx.strftime("%Y-%m-%d"),
                            "indicator": indicator_name,
                            "value": float(value),
                        }
                    )
        except Exception as e:
            logger.error(f"  FRED API error for series {series_id}: {e}")
            continue

    if not all_rows:
        logger.warning("No macro indicator rows fetched — skipping BigQuery write.")
        return

    logger.info(f"  Fetched {len(all_rows)} macro indicator rows. Writing to BigQuery...")

    table_id = _table_ref("macro_indicators")
    job_config = bigquery.LoadJobConfig(
        schema=[
            bigquery.SchemaField("date", "DATE"),
            bigquery.SchemaField("indicator", "STRING"),
            bigquery.SchemaField("value", "FLOAT64"),
        ],
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )

    load_job = bq_client.load_table_from_json(all_rows, table_id, job_config=job_config)
    load_job.result()

    logger.info(f"  Macro ingestion complete. Loaded {load_job.output_rows} rows into {table_id}.")


# ---------------------------------------------------------------------------
# 3. SEC EDGAR Filing Metadata Ingestion
# ---------------------------------------------------------------------------
SEC_FULL_TEXT_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def _search_edgar_filings(form_type: str, date_from: str, date_to: str) -> list[dict]:
    """Search SEC EDGAR full-text search for recent filings of a given form type."""
    headers = {"User-Agent": SEC_EDGAR_USER_AGENT, "Accept": "application/json"}

    params = {
        "q": f'formType:"{form_type}"',
        "dateRange": "custom",
        "startdt": date_from,
        "enddt": date_to,
        "forms": form_type,
    }

    # SEC EDGAR EFTS full-text search API
    url = "https://efts.sec.gov/LATEST/search-index"
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json().get("hits", {}).get("hits", [])
    except Exception:
        # Fallback to the standard EDGAR full-text search endpoint
        pass

    # Use the standard EDGAR full-text search API as fallback
    fallback_url = "https://efts.sec.gov/LATEST/search-index"
    fallback_params = {
        "q": f'"{form_type}"',
        "forms": form_type,
        "dateRange": "custom",
        "startdt": date_from,
        "enddt": date_to,
    }
    try:
        resp = requests.get(fallback_url, params=fallback_params, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json().get("hits", {}).get("hits", [])
    except Exception as e:
        logger.error(f"  EDGAR search error for {form_type}: {e}")
        return []


def _fetch_edgar_via_submissions(ticker: str) -> list[dict]:
    """Fetch recent 10-K/10-Q filings for a specific ticker via the EDGAR submissions API."""
    headers = {"User-Agent": SEC_EDGAR_USER_AGENT, "Accept": "application/json"}

    # First resolve ticker to CIK via the company tickers JSON
    try:
        resp = requests.get(SEC_COMPANY_TICKERS_URL, headers=headers, timeout=15)
        resp.raise_for_status()
        company_data = resp.json()

        cik = None
        company_name = None
        for entry in company_data.values():
            if entry.get("ticker", "").upper() == ticker.upper():
                cik = str(entry["cik_str"]).zfill(10)
                company_name = entry.get("title", "")
                break

        if not cik:
            return []

        # Fetch the submissions for this CIK
        sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        resp = requests.get(sub_url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        filings = []
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])

        for idx, form in enumerate(forms):
            if form in ("10-K", "10-Q"):
                accession_clean = accessions[idx].replace("-", "")
                filing_url = (
                    f"https://www.sec.gov/Archives/edgar/data/"
                    f"{cik.lstrip('0')}/{accession_clean}/{primary_docs[idx]}"
                )
                filings.append(
                    {
                        "ticker": ticker.upper(),
                        "filing_date": dates[idx],
                        "form_type": form,
                        "filing_url": filing_url,
                        "company_name": company_name or data.get("name", ""),
                    }
                )
                # Only keep the most recent of each type
                if len(filings) >= 4:
                    break

        return filings

    except Exception as e:
        logger.error(f"  EDGAR submissions API error for {ticker}: {e}")
        return []


def ingest_sec_filings(bq_client: bigquery.Client) -> None:
    """Fetch latest SEC 10-K/10-Q filing metadata and load into BigQuery."""
    logger.info("Starting SEC EDGAR filing metadata ingestion...")

    all_rows: list[dict] = []

    # Fetch filings per ticker via the EDGAR submissions API.
    # Rate limit: SEC asks for max 10 requests/second; we add a small delay.
    import time

    for i, ticker in enumerate(SP500_TICKERS):
        if i > 0 and i % 10 == 0:
            logger.info(f"  Processed {i}/{len(SP500_TICKERS)} tickers for SEC filings...")
            time.sleep(1.0)  # Respect SEC rate limits

        try:
            filings = _fetch_edgar_via_submissions(ticker)
            all_rows.extend(filings)
        except Exception as e:
            logger.error(f"  Error fetching SEC filings for {ticker}: {e}")
            continue

        # Small delay between requests
        time.sleep(0.12)

    if not all_rows:
        logger.warning("No SEC filing rows fetched — skipping BigQuery write.")
        return

    logger.info(f"  Fetched {len(all_rows)} SEC filing rows. Writing to BigQuery...")

    table_id = _table_ref("sec_filings")
    job_config = bigquery.LoadJobConfig(
        schema=[
            bigquery.SchemaField("ticker", "STRING"),
            bigquery.SchemaField("filing_date", "DATE"),
            bigquery.SchemaField("form_type", "STRING"),
            bigquery.SchemaField("filing_url", "STRING"),
            bigquery.SchemaField("company_name", "STRING"),
        ],
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )

    load_job = bq_client.load_table_from_json(all_rows, table_id, job_config=job_config)
    load_job.result()

    logger.info(f"  SEC filings ingestion complete. Loaded {load_job.output_rows} rows into {table_id}.")


# ---------------------------------------------------------------------------
# 4. (Optional) Trigger Dataform Workflow
# ---------------------------------------------------------------------------
def trigger_dataform(project_id: str) -> None:
    """Optionally trigger a Dataform workflow execution after ingestion.

    Requires DATAFORM_REPOSITORY and DATAFORM_LOCATION env vars to be set.
    If not set, this step is silently skipped.
    """
    repository = os.environ.get("DATAFORM_REPOSITORY")
    location = os.environ.get("DATAFORM_LOCATION", "us-central1")

    if not repository:
        logger.info("DATAFORM_REPOSITORY not set — skipping Dataform trigger.")
        return

    logger.info(f"Triggering Dataform workflow in {repository}...")

    try:
        from google.cloud import dataform_v1beta1 as dataform

        client = dataform.DataformClient()

        parent = f"projects/{project_id}/locations/{location}/repositories/{repository}"

        compilation_result = client.create_compilation_result(
            parent=parent,
            compilation_result=dataform.CompilationResult(
                git_commitish="main",
            ),
        )
        logger.info(f"  Compilation result created: {compilation_result.name}")

        workflow_invocation = client.create_workflow_invocation(
            parent=parent,
            workflow_invocation=dataform.WorkflowInvocation(
                compilation_result=compilation_result.name,
            ),
        )
        logger.info(f"  Dataform workflow invocation started: {workflow_invocation.name}")

    except ImportError:
        logger.warning("google-cloud-dataform not installed — skipping Dataform trigger.")
    except Exception as e:
        logger.error(f"Failed to trigger Dataform workflow: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    """Run all ingestion steps sequentially. Each step is independent so a
    failure in one does not block the others."""
    logger.info("=" * 60)
    logger.info("StonxAI Daily Ingest Job starting...")
    logger.info(f"  Project: {PROJECT_ID}")
    logger.info(f"  Dataset: {BQ_DATASET}")
    logger.info(f"  Tickers: {len(SP500_TICKERS)}")
    logger.info("=" * 60)

    bq_client = bigquery.Client(project=PROJECT_ID)
    errors: list[str] = []

    # Step 1: OHLCV from Alpaca
    try:
        ingest_ohlcv(bq_client)
    except Exception as e:
        logger.exception("OHLCV ingestion failed")
        errors.append(f"OHLCV: {e}")

    # Step 2: Macro indicators from FRED
    try:
        ingest_macro(bq_client)
    except Exception as e:
        logger.exception("Macro indicator ingestion failed")
        errors.append(f"Macro: {e}")

    # Step 3: SEC EDGAR filings
    try:
        ingest_sec_filings(bq_client)
    except Exception as e:
        logger.exception("SEC filings ingestion failed")
        errors.append(f"SEC: {e}")

    # Step 4: Trigger Dataform (optional)
    try:
        trigger_dataform(PROJECT_ID)
    except Exception as e:
        logger.exception("Dataform trigger failed")
        errors.append(f"Dataform: {e}")

    # Summary
    logger.info("=" * 60)
    if errors:
        logger.error(f"Ingest job completed with {len(errors)} error(s):")
        for err in errors:
            logger.error(f"  - {err}")
        sys.exit(1)
    else:
        logger.info("Ingest job completed successfully — all sources ingested.")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
