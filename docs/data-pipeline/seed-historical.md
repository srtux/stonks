# Historical Data Backfill Documentation

## Purpose

The `seed_historical.py` script performs a one-time backfill of historical OHLCV price data and macroeconomic indicators into BigQuery. This is necessary because the daily ingest job uses `WRITE_TRUNCATE` (overwriting the table each run with only the last 5 days), so historical data must be loaded separately.

**Source file**: `/scripts/seed_historical.py`

---

## When to Run

Run this script:
- **Initial setup**: Before running the Dataform pipeline for the first time. The technical signals require at least 200+ trading days of history for the SMA-200 calculation, and 252 trading days for the 52-week high/low.
- **After table recreation**: If `ohlcv_daily` or `macro_indicators` tables are dropped and recreated.
- **Adding new tickers**: If you expand the ticker universe, run the script with the new tickers to backfill their history.
- **One-time only**: Under normal operation, this script is run once. The daily ingest job handles ongoing data after that.

**Do NOT run** this script repeatedly without clearing the target tables first, as it uses `WRITE_APPEND` and will create duplicate rows.

---

## Data Range

| Property | Value |
|----------|-------|
| Start date | `2022-01-01` |
| End date | Current date (dynamically computed via `datetime.utcnow()`) |
| OHLCV history | ~3+ years of daily bars per ticker |
| Macro history | ~3+ years of VIX (daily), CPI (monthly), FEDFUNDS (monthly) |

The 2022 start date provides sufficient history for:
- 200-day SMA: requires ~200 trading days (~10 months)
- 52-week high/low: requires 252 trading days (~1 year)
- YoY revenue growth: requires 4 quarters of filing data
- Backtesting: provides 2+ years of fully-computed signals after warmup

---

## OHLCV Data Fetching from Alpaca

### Batching Strategy

```python
BATCH_SIZE = 10  # tickers per Alpaca request batch
SLEEP_BETWEEN_BATCHES = 1.0  # seconds
```

Unlike the daily ingest job (which uses batches of 50 for a small date range), the historical script uses **smaller batches of 10 tickers** because each request fetches years of data, resulting in much larger response payloads.

### Fetch Flow

```python
for i in range(0, total, BATCH_SIZE):
    batch = tickers[i : i + BATCH_SIZE]

    request = StockBarsRequest(
        symbol_or_symbols=batch,
        timeframe=TimeFrame.Day,
        start=START_DATE,    # "2022-01-01"
        end=END_DATE,        # today
    )

    bars = alpaca_client.get_stock_bars(request)

    # Convert to DataFrame and write to BigQuery
    rows = [
        {
            "ticker": b.symbol,
            "date": b.timestamp.strftime("%Y-%m-%d"),
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
            "volume": int(b.volume),
            "adj_close": float(b.close),  # Alpaca bars are split-adjusted
        }
        for bar_list in bars.data.values()
        for b in bar_list
    ]

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    load_df_to_bq(df, OHLCV_TABLE)

    time.sleep(SLEEP_BETWEEN_BATCHES)
```

**Key details**:
- Each batch is written to BigQuery immediately after fetching (not accumulated in memory). This keeps memory usage low for large datasets.
- `pd.to_datetime(df["date"])` converts the date string to a proper datetime type, which BigQuery's `load_table_from_dataframe` maps to a `DATE` column.
- The 1-second sleep between batches respects Alpaca's rate limits.

### Rate Limits

| Alpaca Tier | Rate Limit | Impact |
|-------------|------------|--------|
| Free | 200 requests/minute | ~52 tickers / 10 per batch = 6 batches. Well within limits even without sleep. |
| Paid | Higher limits | No issues |

The 1-second sleep is conservative and could be reduced, but the script runs once so speed is not critical.

### Ticker Universe

The script uses its own ticker list (`DEFAULT_TICKERS`), which is similar but not identical to the daily ingest job's `SP500_TICKERS`:

| Script | Ticker List | Count | Notable Differences |
|--------|-------------|-------|---------------------|
| `seed_historical.py` | `DEFAULT_TICKERS` | 52 | Includes BRK.B, GOOG, ACN, ADBE, HD, IBM, INTC, CSCO, DHR, NEE, QCOM, RTX, SBUX, T |
| `ingest_job/main.py` | `SP500_TICKERS` | 50 | Includes BA, BLK, AXP, C, WFC, TMO, SLB, UPS |

**Recommendation**: Align both lists, or better yet, define the ticker universe in a shared configuration file or the proposed `ticker_metadata` table (see [transformations.md](./transformations.md#the-sector-data-gap)).

---

## FRED Historical Data

The script fetches the same three FRED series as the daily ingest job, but with the full historical range:

```python
FRED_SERIES = {
    "VIXCLS": "VIX",
    "CPIAUCSL": "CPI",
    "FEDFUNDS": "FEDFUNDS",
}

data = fred.get_series(series_id, observation_start=START_DATE)  # "2022-01-01"
```

**Additional columns**: The historical script includes a `series_id` column in the output that the daily ingest job does not:

| Column | seed_historical.py | ingest_job/main.py |
|--------|-------------------|-------------------|
| `date` | Yes | Yes |
| `indicator` | Yes | Yes |
| `value` | Yes | Yes |
| `series_id` | Yes | No |

This mismatch may cause issues if the BigQuery table schema is strict. If using `autodetect=True` (as the seed script does), BigQuery will add the column on first write. Subsequent daily ingests without this column will leave it NULL.

---

## BigQuery Write Strategy

```python
job_config = bigquery.LoadJobConfig(
    write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
    autodetect=True,
)
job = bq_client.load_table_from_dataframe(df, table_id, job_config=job_config)
```

| Property | Value | Rationale |
|----------|-------|-----------|
| Write disposition | `WRITE_APPEND` | Adds rows to the table without deleting existing data. Each batch appends. |
| Schema detection | `autodetect=True` | BigQuery infers the schema from the DataFrame. Creates the table on first write. |

**Contrast with daily ingest**:
- Daily ingest uses `WRITE_TRUNCATE` (idempotent, overwrites)
- Historical seed uses `WRITE_APPEND` (additive, not idempotent)

**Warning**: Running `seed_historical.py` multiple times without clearing the table will create duplicate rows. To re-run safely:
```sql
-- Option 1: Truncate first
TRUNCATE TABLE amfe_data.ohlcv_daily;
TRUNCATE TABLE amfe_data.macro_indicators;

-- Option 2: Delete the specific date range
DELETE FROM amfe_data.ohlcv_daily WHERE date >= '2022-01-01';
DELETE FROM amfe_data.macro_indicators WHERE date >= '2022-01-01';
```

---

## Schema Alignment with `ohlcv_daily`

The seed script produces rows with this schema:

| Column | Python Type | BigQuery Type | Matches `ohlcv_daily` |
|--------|-------------|---------------|----------------------|
| `ticker` | str | STRING | Yes |
| `date` | datetime64 | DATE | Yes |
| `open` | float | FLOAT64 | Yes |
| `high` | float | FLOAT64 | Yes |
| `low` | float | FLOAT64 | Yes |
| `close` | float | FLOAT64 | Yes |
| `volume` | int | INT64 | Yes |
| `adj_close` | float | FLOAT64 | Yes |

The schema is fully aligned. If the table already exists (e.g., created by a `CREATE TABLE` DDL), the append will succeed as long as column names and types match.

---

## Execution

### Prerequisites

1. Set environment variables (or create a `.env` file in the project root):
   ```bash
   export GOOGLE_CLOUD_PROJECT="amfe-project"
   export BQ_DATASET="amfe_data"
   export ALPACA_API_KEY="your-alpaca-key"
   export ALPACA_SECRET_KEY="your-alpaca-secret"
   export FRED_API_KEY="your-fred-key"
   ```

2. Install Python dependencies:
   ```bash
   pip install alpaca-py fredapi google-cloud-bigquery pandas python-dotenv
   ```

3. Authenticate with GCP:
   ```bash
   gcloud auth application-default login
   ```

### Running the Script

```bash
python scripts/seed_historical.py
```

**Expected output**:
```
2025-03-04 16:00:00 INFO Starting historical backfill: 2022-01-01 -> 2025-03-04
2025-03-04 16:00:00 INFO --- Phase 1: OHLCV from Alpaca ---
2025-03-04 16:00:00 INFO Fetching OHLCV batch 1-10 / 52  (AAPL ...)
2025-03-04 16:00:02 INFO Loaded 7560 rows into amfe-project.amfe_data.ohlcv_daily
2025-03-04 16:00:02 INFO Loaded 10 / 52 tickers
...
2025-03-04 16:00:15 INFO --- Phase 2: Macro from FRED ---
2025-03-04 16:00:15 INFO Fetching FRED series: VIXCLS (VIX)
2025-03-04 16:00:16 INFO Fetching FRED series: CPIAUCSL (CPI)
2025-03-04 16:00:17 INFO Fetching FRED series: FEDFUNDS (FEDFUNDS)
2025-03-04 16:00:18 INFO Loaded 850 rows into amfe-project.amfe_data.macro_indicators
2025-03-04 16:00:18 INFO FRED macro data loaded (850 rows)
2025-03-04 16:00:18 INFO Backfill complete.
```

**Estimated runtime**: 1-3 minutes for 52 tickers over 3 years of data.

---

## Troubleshooting

### `alpaca.common.exceptions.APIError: forbidden`
- **Cause**: Invalid or expired Alpaca API credentials.
- **Fix**: Verify `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`. Ensure you're using the correct base URL (paper vs. live).

### `fredapi.fred.FredError: Bad Request`
- **Cause**: Invalid FRED API key or invalid series ID.
- **Fix**: Verify `FRED_API_KEY` at [https://fred.stlouisfed.org/docs/api/api_key.html](https://fred.stlouisfed.org/docs/api/api_key.html).

### `google.api_core.exceptions.NotFound: Not found: Table amfe-project.amfe_data.ohlcv_daily`
- **Cause**: The BigQuery table doesn't exist yet.
- **Fix**: The script uses `autodetect=True`, which should create the table automatically on first write. If this fails, create the table manually using the DDL in [bigquery-schema.md](../architecture/bigquery-schema.md).

### `google.api_core.exceptions.Forbidden: Access Denied`
- **Cause**: GCP authentication not set up or insufficient permissions.
- **Fix**: Run `gcloud auth application-default login` and ensure your account has `BigQuery Data Editor` and `BigQuery Job User` roles.

### Duplicate rows after re-running
- **Cause**: `WRITE_APPEND` adds rows without deduplication.
- **Fix**: Truncate the tables before re-running (see BigQuery Write Strategy section above). Alternatively, run a deduplication query:
  ```sql
  CREATE OR REPLACE TABLE amfe_data.ohlcv_daily AS
  SELECT DISTINCT * FROM amfe_data.ohlcv_daily;
  ```

### Memory issues with large ticker lists
- **Cause**: The script writes each batch to BigQuery immediately, so memory usage should be bounded. However, if `BATCH_SIZE` is too large with a long date range, the Alpaca response may be large.
- **Fix**: Reduce `BATCH_SIZE` (default is 10, which is conservative).

### `BRK.B` ticker issues
- **Cause**: The period in `BRK.B` can cause issues with some APIs.
- **Fix**: Alpaca handles `BRK.B` correctly. If issues arise, try `BRK/B` or remove it from the ticker list.

---

## Cross-References

- System overview: [docs/architecture/system-overview.md](../architecture/system-overview.md)
- BigQuery schema: [docs/architecture/bigquery-schema.md](../architecture/bigquery-schema.md)
- Ingestion pipeline: [docs/data-pipeline/ingestion.md](./ingestion.md)
- Transformation pipeline: [docs/data-pipeline/transformations.md](./transformations.md)
