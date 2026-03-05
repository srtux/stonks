# Ingestion Pipeline Documentation

## Overview

The ingestion pipeline is a **Cloud Run Job** (`ingest_job/main.py`) that runs daily after U.S. market close. It fetches raw data from three external sources and writes it to BigQuery raw tables in the `amfe_data` dataset. After ingestion, it optionally triggers the Dataform transformation pipeline.

**Source file**: `/ingest_job/main.py`

---

## Data Sources

### 1. Alpaca Markets API -- OHLCV Price Data

| Property | Value |
|----------|-------|
| API | Alpaca Markets Data API v2 |
| Python SDK | `alpaca-py` (`alpaca.data.StockHistoricalDataClient`) |
| Endpoint | `get_stock_bars()` |
| Data type | Daily OHLCV bars (split-adjusted) |
| Base URL | `https://paper-api.alpaca.markets` (paper trading for dev) |
| Target table | `amfe_data.ohlcv_daily` |

Alpaca provides institutional-quality market data. The system uses the historical data API to fetch daily bars. Bars are split-adjusted, which means `adj_close` equals `close` -- no separate adjustment is needed.

**Authentication**: API key + secret key, passed via environment variables.

### 2. FRED API -- Macroeconomic Indicators

| Property | Value |
|----------|-------|
| API | Federal Reserve Economic Data (FRED) |
| Python SDK | `fredapi` (`Fred`) |
| Series fetched | `VIXCLS` (VIX), `CPIAUCSL` (CPI), `FEDFUNDS` (Federal Funds Rate) |
| Target table | `amfe_data.macro_indicators` |

FRED is the Federal Reserve Bank of St. Louis's public data portal. The VIX (CBOE Volatility Index) is the most critical series -- it's used in the HMM regime classification in `screening_master.sqlx`.

**Authentication**: API key, passed via environment variable.

**Series details**:

| FRED Series ID | Label | Frequency | Description |
|----------------|-------|-----------|-------------|
| `VIXCLS` | `VIX` | Daily | Expected 30-day S&P 500 volatility. Key input to regime classification. |
| `CPIAUCSL` | `CPI` | Monthly | Consumer Price Index. Inflation gauge. |
| `FEDFUNDS` | `FEDFUNDS` | Monthly | Effective Federal Funds Rate. Interest rate environment. |

### 3. SEC EDGAR -- Filing Metadata

| Property | Value |
|----------|-------|
| API | SEC EDGAR Submissions API |
| Endpoints | `company_tickers.json` (CIK lookup), `CIK{cik}.json` (submissions) |
| Data type | 10-K and 10-Q filing metadata (date, URL, company name) |
| Target table | `amfe_data.sec_filings` |

SEC EDGAR is the SEC's public filing database. The ingest job fetches recent 10-K (annual) and 10-Q (quarterly) filing metadata for each ticker. It does NOT fetch the full filing text -- only metadata (filing date, form type, filing URL, company name).

**Authentication**: No API key required. SEC requires a `User-Agent` header with application name and contact email.

**CIK resolution flow**:
1. Fetch `https://www.sec.gov/files/company_tickers.json` to get the complete ticker-to-CIK mapping
2. For each ticker, resolve to a 10-digit zero-padded CIK
3. Fetch `https://data.sec.gov/submissions/CIK{cik}.json` to get all recent filings
4. Filter for `10-K` and `10-Q` form types
5. Extract up to 4 filings per ticker (most recent of each type)

---

## Ticker Universe

The current ticker universe is a representative subset of ~50 S&P 500 tickers across major sectors:

| Sector | Tickers | Count |
|--------|---------|-------|
| Technology | AAPL, MSFT, NVDA, GOOGL, META, AMZN, TSLA, AVGO, ORCL, CRM | 10 |
| Financials | JPM, V, MA, BAC, GS, MS, BLK, AXP, C, WFC | 10 |
| Healthcare | UNH, JNJ, LLY, PFE, ABBV, MRK, TMO, ABT, BMY, AMGN | 10 |
| Energy | XOM, CVX, COP, SLB, EOG | 5 |
| Consumer | WMT, PG, KO, PEP, COST, MCD, NKE | 7 |
| Industrials | CAT, BA, HON, UPS, GE | 5 |
| Other | DIS, NFLX, AMD | 3 |
| **Total** | | **50** |

**Expansion plan**: The architecture supports the full S&P 500 (~503 tickers). The ticker list is defined as the `SP500_TICKERS` constant in `main.py` and can be expanded without code changes. Alpaca batching and SEC rate limiting are already implemented to handle larger universes.

---

## API Rate Limits and Handling

### Alpaca
- **Limit**: Alpaca's data API allows generous rate limits for historical data (200 requests/minute for free tier).
- **Handling**: Tickers are batched in groups of 50 (`batch_size = 50`). Each batch is a single API call with multiple symbols. For the current 50-ticker universe, this is a single batch.
- **Error handling**: If a batch fails, the error is logged and the loop continues to the next batch. Failed tickers are not retried within the same run.

### FRED
- **Limit**: 120 requests per minute.
- **Handling**: Only 3 series are fetched (one request each). No batching or throttling needed.
- **Error handling**: Each series is fetched independently. A failure in one does not affect the others.

### SEC EDGAR
- **Limit**: SEC asks for a maximum of 10 requests per second.
- **Handling**: A `time.sleep(0.12)` delay is inserted between every ticker request. Additionally, every 10 tickers, a 1-second pause is added with a progress log.
- **Error handling**: Failures for individual tickers are logged and skipped. The CIK resolution and submissions fetch are wrapped in try/except blocks.

---

## Schema Mappings

### Alpaca API Response to `ohlcv_daily`

| Alpaca Field | BigQuery Column | Transformation |
|--------------|-----------------|----------------|
| `bar.symbol` | `ticker` | `str(symbol)` |
| `bar.timestamp` | `date` | `.strftime("%Y-%m-%d")` |
| `bar.open` | `open` | `float()` |
| `bar.high` | `high` | `float()` |
| `bar.low` | `low` | `float()` |
| `bar.close` | `close` | `float()` |
| `bar.volume` | `volume` | `int()` |
| `bar.close` | `adj_close` | `float()` (same as close; Alpaca bars are split-adjusted) |

### FRED API Response to `macro_indicators`

| FRED Field | BigQuery Column | Transformation |
|------------|-----------------|----------------|
| Series index (date) | `date` | `.strftime("%Y-%m-%d")` |
| Configured label | `indicator` | Mapped via `FRED_SERIES` dict |
| Observation value | `value` | `float()`, NaN/None/`.` values filtered out |

### SEC EDGAR Response to `sec_filings`

| EDGAR Field | BigQuery Column | Transformation |
|-------------|-----------------|----------------|
| Ticker (from input list) | `ticker` | `.upper()` |
| `filingDate[idx]` | `filing_date` | Direct string (YYYY-MM-DD) |
| `form[idx]` | `form_type` | Direct (filtered to `10-K` and `10-Q`) |
| Constructed URL | `filing_url` | Built from CIK + accession number + primary document |
| `title` or `name` | `company_name` | From company_tickers.json or submissions response |

---

## Error Handling Strategy

The ingest job follows an **isolated failure** pattern: each data source is fetched independently, and a failure in one does not prevent the others from running.

```python
def main() -> None:
    errors: list[str] = []

    try:
        ingest_ohlcv(bq_client)
    except Exception as e:
        errors.append(f"OHLCV: {e}")

    try:
        ingest_macro(bq_client)
    except Exception as e:
        errors.append(f"Macro: {e}")

    try:
        ingest_sec_filings(bq_client)
    except Exception as e:
        errors.append(f"SEC: {e}")

    try:
        trigger_dataform(PROJECT_ID)
    except Exception as e:
        errors.append(f"Dataform: {e}")

    if errors:
        sys.exit(1)  # Non-zero exit code signals failure to Cloud Run
```

**Key behaviors**:
- All four steps always run, regardless of failures in earlier steps.
- Errors are collected and summarized at the end.
- A non-zero exit code (`sys.exit(1)`) is returned if any step failed. Cloud Run and Cloud Scheduler can use this for alerting.
- Within Alpaca ingestion, individual batch failures are caught and logged without stopping the loop.
- Within SEC ingestion, individual ticker failures are caught and logged.

---

## Scheduling

| Property | Value |
|----------|-------|
| Trigger | Google Cloud Scheduler |
| Schedule | Daily at **16:30 ET** (21:30 UTC during EST, 20:30 UTC during EDT) |
| Target | Cloud Run Job `amfe-ingest-job` |

**Why 16:30 ET?**
- U.S. stock markets close at 16:00 ET.
- A 30-minute buffer allows for:
  - Final trade settlement and bar calculation by data providers
  - Alpaca's data pipeline to finalize daily bars
  - Any minor delays in market close procedures
- The Dataform pipeline runs after ingestion completes (triggered by the job or scheduled at 17:00 ET as a fallback).

**Weekend/holiday handling**: Cloud Scheduler fires every day. On weekends and holidays, Alpaca returns no new bars. The ingest job handles this gracefully -- if no rows are fetched, it logs a warning and skips the BigQuery write. The Dataform pipeline will not produce new rows for non-trading days.

---

## Dataform Trigger Mechanism

After all three ingestion steps complete, the job optionally triggers a Dataform workflow execution:

```python
def trigger_dataform(project_id: str) -> None:
    # Requires DATAFORM_REPOSITORY env var
    repository = os.environ.get("DATAFORM_REPOSITORY")
    if not repository:
        return  # Silently skip if not configured

    client = dataform.DataformClient()
    parent = f"projects/{project_id}/locations/{location}/repositories/{repository}"

    # 1. Create a compilation result from the 'main' branch
    compilation_result = client.create_compilation_result(
        parent=parent,
        compilation_result=dataform.CompilationResult(git_commitish="main"),
    )

    # 2. Execute the workflow based on the compilation
    workflow_invocation = client.create_workflow_invocation(
        parent=parent,
        workflow_invocation=dataform.WorkflowInvocation(
            compilation_result=compilation_result.name,
        ),
    )
```

**Flow**:
1. The Dataform repository is compiled from the `main` branch.
2. A workflow invocation is created, which executes all `.sqlx` files in dependency order.
3. The workflow is asynchronous -- the ingest job starts it but does not wait for completion.

**Fallback**: If `DATAFORM_REPOSITORY` is not set, this step is silently skipped. A separate Cloud Scheduler job can trigger Dataform at 17:00 ET as a fallback.

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GOOGLE_CLOUD_PROJECT` | Yes | `""` | GCP project ID (e.g., `amfe-project`) |
| `BQ_DATASET` | No | `amfe_data` | BigQuery dataset name |
| `ALPACA_API_KEY` | Yes | `""` | Alpaca Markets API key |
| `ALPACA_SECRET_KEY` | Yes | `""` | Alpaca Markets secret key |
| `ALPACA_BASE_URL` | No | `https://paper-api.alpaca.markets` | Alpaca API base URL (paper vs. live) |
| `FRED_API_KEY` | Yes | `""` | FRED API key |
| `SEC_EDGAR_USER_AGENT` | No | `StonxAI bot@example.com` | User-Agent header for SEC EDGAR requests |
| `DATAFORM_REPOSITORY` | No | None | Dataform repository name (if set, triggers Dataform after ingestion) |
| `DATAFORM_LOCATION` | No | `us-central1` | GCP region for Dataform |

---

## Monitoring and Alerting Recommendations

### Cloud Run Job monitoring
- **Execution logs**: Cloud Run Job logs are automatically sent to Cloud Logging. The job uses Python's `logging` module with structured output.
- **Exit code monitoring**: Set up a Cloud Monitoring alert on non-zero exit codes from the Cloud Run Job.
- **Duration monitoring**: Typical execution time is 2-5 minutes. Alert if execution exceeds 15 minutes.

### Data quality checks
- **Row count monitoring**: After each run, verify that `ohlcv_daily` has the expected number of rows (~50 tickers x ~5 days = ~250 rows). A significantly lower count indicates API failures.
- **Staleness monitoring**: Create a BigQuery scheduled query that checks `MAX(date)` in `ohlcv_daily` and alerts if it's more than 2 trading days old.
- **NULL rate monitoring**: Track the percentage of NULL values in key columns. A sudden increase may indicate API response format changes.

### Recommended alerts
1. **Ingest job failure**: Cloud Scheduler + Cloud Monitoring alert on job failure.
2. **Data staleness**: BigQuery scheduled query checking `MAX(date) < CURRENT_DATE() - 2`.
3. **Row count anomaly**: BigQuery scheduled query checking row count drops below threshold.
4. **Dataform pipeline failure**: Dataform has built-in failure notifications via Cloud Monitoring.

---

## Idempotency Guarantees

The daily ingest job uses `WRITE_TRUNCATE` for all three target tables. This means:

- **Running the job twice** produces the same result -- the second run overwrites the first.
- **No duplicate rows**: Unlike `WRITE_APPEND`, truncation ensures exactly one copy of the data.
- **Trade-off**: Historical data is lost on each run. This is acceptable because:
  - Historical data is loaded separately via `seed_historical.py` (using `WRITE_APPEND`).
  - The daily job fetches a 5-day window, so recent data is always present.
  - The Dataform pipeline operates on whatever data is in the raw tables.

**Important**: If you need to maintain historical data in `ohlcv_daily`, either switch to `WRITE_APPEND` with deduplication, or run `seed_historical.py` as a one-time backfill and then switch the daily job to append only today's data.

---

## Cross-References

- System overview: [docs/architecture/system-overview.md](../architecture/system-overview.md)
- BigQuery schema: [docs/architecture/bigquery-schema.md](../architecture/bigquery-schema.md)
- Transformation pipeline: [docs/data-pipeline/transformations.md](./transformations.md)
- Historical backfill: [docs/data-pipeline/seed-historical.md](./seed-historical.md)
