# BigQuery Schema Documentation

## Dataset Overview

All StonxAI data lives in a single BigQuery dataset:

- **Project**: `amfe-project` (configurable via `GOOGLE_CLOUD_PROJECT`)
- **Dataset**: `amfe_data` (configurable via `BQ_DATASET`)
- **Location**: `us-central1`

The dataset contains seven tables and one view, organized into three tiers:

| Tier | Tables | Updated By |
|------|--------|------------|
| Raw | `ohlcv_daily`, `macro_indicators`, `sec_filings` | Ingest job (Cloud Run) |
| Computed | `technical_signals`, `fundamental_scores`, `screening_master` | Dataform pipelines |
| Audit | `agent_decisions` | ADK agents (append-only) |
| Views | `latest_screening_master` | Automatic (references screening_master) |

---

## Table: `ohlcv_daily`

Daily Open-High-Low-Close-Volume bars for tracked tickers. This is the foundational price data table.

**Source**: Alpaca Markets API (daily bars, split-adjusted)

**Partitioning**: `PARTITION BY date`
**Clustering**: `CLUSTER BY ticker`

| Column | Type | Nullable | Description | Example |
|--------|------|----------|-------------|---------|
| `ticker` | STRING | NOT NULL | Stock ticker symbol | `NVDA` |
| `date` | DATE | NOT NULL | Trading date | `2025-03-04` |
| `open` | FLOAT64 | Yes | Opening price | `875.50` |
| `high` | FLOAT64 | Yes | Intraday high | `892.30` |
| `low` | FLOAT64 | Yes | Intraday low | `870.15` |
| `close` | FLOAT64 | Yes | Closing price | `888.75` |
| `volume` | INT64 | Yes | Number of shares traded | `45230100` |
| `adj_close` | FLOAT64 | Yes | Split-adjusted close (same as close for Alpaca) | `888.75` |

**Notes**:
- Alpaca bars are already split-adjusted, so `adj_close` equals `close`.
- The daily ingest job fetches a 5-day window and uses `WRITE_TRUNCATE`, so this table only contains the last ~5 trading days after a daily run. Historical data is loaded separately via `seed_historical.py` using `WRITE_APPEND`.
- The 52-week high/low calculations in `technical_signals` require at least 252 trading days of history.

---

## Table: `macro_indicators`

Macroeconomic time series fetched from the Federal Reserve Economic Data (FRED) API.

**Source**: FRED API

**Partitioning**: None (small table)
**Clustering**: None

| Column | Type | Nullable | Description | Example |
|--------|------|----------|-------------|---------|
| `date` | DATE | NOT NULL | Observation date | `2025-03-03` |
| `indicator` | STRING | NOT NULL | Indicator label | `VIX` |
| `value` | FLOAT64 | Yes | Observed value | `18.45` |

**Indicators currently fetched**:

| FRED Series ID | Indicator Label | Frequency | Description |
|----------------|-----------------|-----------|-------------|
| `VIXCLS` | `VIX` | Daily | CBOE Volatility Index -- measures expected 30-day S&P 500 volatility |
| `CPIAUCSL` | `CPI` | Monthly | Consumer Price Index for All Urban Consumers -- inflation gauge |
| `FEDFUNDS` | `FEDFUNDS` | Monthly | Effective Federal Funds Rate -- key interest rate benchmark |

**Notes**:
- VIX is published daily; CPI and FEDFUNDS are monthly. The `screening_master` pipeline uses VIX for regime classification.
- The `screening_master.sqlx` references additional macro columns (`sp500_close`, `us_10y_yield`, `us_2y_yield`) that are not yet populated by the ingest job. These represent a planned expansion of the macro data pipeline.
- Daily ingest fetches the last 30 days to capture any revisions.

---

## Table: `sec_filings`

Metadata for SEC 10-K (annual) and 10-Q (quarterly) filings. Used to derive fundamental scores.

**Source**: SEC EDGAR Submissions API

**Partitioning**: None
**Clustering**: None

| Column | Type | Nullable | Description | Example |
|--------|------|----------|-------------|---------|
| `ticker` | STRING | NOT NULL | Stock ticker symbol | `AAPL` |
| `filing_date` | DATE | NOT NULL | Date the filing was submitted to SEC | `2025-01-31` |
| `form_type` | STRING | NOT NULL | Filing type | `10-K` or `10-Q` |
| `filing_url` | STRING | Yes | URL to the primary filing document on EDGAR | `https://www.sec.gov/Archives/edgar/data/...` |
| `company_name` | STRING | Yes | Company name from EDGAR | `Apple Inc` |

**Notes**:
- The `fundamental_scores.sqlx` pipeline expects additional columns that are not yet present in the ingest job output: `period_end_date`, `earnings_per_share`, `book_value_per_share`, `revenue`, `net_income`, `total_debt`, `total_equity`, `current_assets`, `current_liabilities`, `total_assets`, `shares_outstanding`, `earnings_estimate`, `earnings_actual`. These would need to be extracted from the filing documents or supplemented from a financial data provider.
- The ingest job fetches up to 4 filings per ticker (2 most recent 10-K and 2 most recent 10-Q).

---

## Table: `technical_signals`

Pre-computed technical analysis indicators for every ticker on every trading day.

**Source**: Dataform pipeline `technical_signals.sqlx` (reads from `ohlcv_daily`)

**Partitioning**: `PARTITION BY date`
**Clustering**: `CLUSTER BY ticker`

| Column | Type | Nullable | Description | Example |
|--------|------|----------|-------------|---------|
| `ticker` | STRING | NOT NULL | Stock ticker symbol | `NVDA` |
| `date` | DATE | NOT NULL | Trading date | `2025-03-04` |
| `open` | FLOAT64 | Yes | Opening price (pass-through) | `875.50` |
| `high` | FLOAT64 | Yes | Intraday high (pass-through) | `892.30` |
| `low` | FLOAT64 | Yes | Intraday low (pass-through) | `870.15` |
| `close` | FLOAT64 | Yes | Closing price (pass-through) | `888.75` |
| `volume` | INT64 | Yes | Volume (pass-through) | `45230100` |
| `rsi_14` | FLOAT64 | Yes | 14-period Relative Strength Index (0-100) | `62.5` |
| `rsi_2` | FLOAT64 | Yes | 2-period RSI for short-term mean reversion | `78.3` |
| `macd_line` | FLOAT64 | Yes | MACD line (EMA-12 minus EMA-26) | `3.45` |
| `macd_signal_line` | FLOAT64 | Yes | 9-period EMA of MACD line | `2.80` |
| `macd_histogram` | FLOAT64 | Yes | MACD minus signal line | `0.65` |
| `bb_upper` | FLOAT64 | Yes | Upper Bollinger Band (SMA-20 + 2 * stddev) | `900.00` |
| `bb_middle` | FLOAT64 | Yes | Middle Bollinger Band (SMA-20) | `880.00` |
| `bb_lower` | FLOAT64 | Yes | Lower Bollinger Band (SMA-20 - 2 * stddev) | `860.00` |
| `bb_pct` | FLOAT64 | Yes | Bollinger Band %B (0 = at lower, 1 = at upper) | `0.72` |
| `sma_20` | FLOAT64 | Yes | 20-day Simple Moving Average | `880.00` |
| `sma_50` | FLOAT64 | Yes | 50-day Simple Moving Average | `850.00` |
| `sma_200` | FLOAT64 | Yes | 200-day Simple Moving Average | `780.00` |
| `dist_from_sma_20` | FLOAT64 | Yes | (close - SMA20) / SMA20 | `0.010` |
| `dist_from_sma_50` | FLOAT64 | Yes | (close - SMA50) / SMA50 | `0.046` |
| `dist_from_sma_200` | FLOAT64 | Yes | (close - SMA200) / SMA200 | `0.139` |
| `sma_cross_20_50` | FLOAT64 | Yes | SMA-20 minus SMA-50 (positive = golden cross) | `30.00` |
| `atr_14` | FLOAT64 | Yes | 14-period Average True Range | `15.20` |
| `pct_change_1d` | FLOAT64 | Yes | 1-day percent change | `0.015` |
| `pct_change_5d` | FLOAT64 | Yes | 5-day percent change | `0.032` |
| `pct_change_30d` | FLOAT64 | Yes | 30-day percent change | `0.085` |
| `high_52w` | FLOAT64 | Yes | 52-week (252 trading days) high | `950.00` |
| `low_52w` | FLOAT64 | Yes | 52-week low | `620.00` |
| `pct_from_52w_high` | FLOAT64 | Yes | (close - 52w_high) / 52w_high (always <= 0) | `-0.064` |

---

## Table: `fundamental_scores`

Per-ticker fundamental valuation metrics, computed from SEC filings joined to daily price data with point-in-time accuracy.

**Source**: Dataform pipeline `fundamental_scores.sqlx` (reads from `sec_filings` and `ohlcv_daily`)

**Partitioning**: `PARTITION BY date`
**Clustering**: `CLUSTER BY ticker`

| Column | Type | Nullable | Description | Example |
|--------|------|----------|-------------|---------|
| `ticker` | STRING | NOT NULL | Stock ticker symbol | `AAPL` |
| `date` | DATE | NOT NULL | Trading date | `2025-03-04` |
| `close` | FLOAT64 | Yes | Closing price on this date | `178.50` |
| `latest_filing_date` | DATE | Yes | Most recent SEC filing date on or before this date | `2025-01-31` |
| `latest_period_end_date` | DATE | Yes | Period-end date of the latest filing | `2024-12-31` |
| `pe_ratio` | FLOAT64 | Yes | Price-to-Earnings ratio (close / EPS) | `28.5` |
| `pb_ratio` | FLOAT64 | Yes | Price-to-Book ratio (close / book_value_per_share) | `45.2` |
| `ps_ratio` | FLOAT64 | Yes | Price-to-Sales ratio (market_cap / revenue) | `7.8` |
| `debt_to_equity` | FLOAT64 | Yes | Total debt / total equity | `1.52` |
| `current_ratio` | FLOAT64 | Yes | Current assets / current liabilities | `1.07` |
| `revenue_growth_qoq` | FLOAT64 | Yes | Quarter-over-quarter revenue growth rate | `0.045` |
| `revenue_growth_yoy` | FLOAT64 | Yes | Year-over-year revenue growth rate | `0.082` |
| `earnings_surprise_pct` | FLOAT64 | Yes | (actual - estimate) / |estimate| | `0.12` |
| `earnings_per_share` | FLOAT64 | Yes | EPS from latest filing | `6.26` |
| `book_value_per_share` | FLOAT64 | Yes | Book value per share | `3.95` |
| `revenue` | FLOAT64 | Yes | Total revenue from latest filing | `94680000000` |
| `net_income` | FLOAT64 | Yes | Net income from latest filing | `23636000000` |
| `shares_outstanding` | FLOAT64 | Yes | Shares outstanding | `15460000000` |

**Notes**:
- Uses a **point-in-time join** to avoid look-ahead bias: for each trading day, only the most recent filing with `filing_date <= date` is used.
- **Forward-fill**: `LAST_VALUE ... IGNORE NULLS` fills gaps between filing dates, so fundamental metrics are available for every trading day.

---

## Table: `screening_master`

The central screening table. Joins technical signals, fundamental scores, and macro/regime data into one wide, query-ready table. This is what agents read.

**Source**: Dataform pipeline `screening_master.sqlx`

**Partitioning**: `PARTITION BY date`
**Clustering**: `CLUSTER BY ticker, sector, signal_label`

This table contains all columns from `technical_signals` and `fundamental_scores` (see above), plus the following additional columns:

| Column | Type | Nullable | Description | Example |
|--------|------|----------|-------------|---------|
| `sector` | STRING | Yes | Stock sector (currently defaults to `'UNKNOWN'`) | `Technology` |
| `vix_close` | FLOAT64 | Yes | VIX closing value for this date | `18.45` |
| `fed_funds_rate` | FLOAT64 | Yes | Federal funds rate | `5.33` |
| `us_10y_yield` | FLOAT64 | Yes | US 10-year Treasury yield | `4.25` |
| `us_2y_yield` | FLOAT64 | Yes | US 2-year Treasury yield | `4.60` |
| `yield_spread` | FLOAT64 | Yes | 10Y minus 2Y yield (negative = inverted curve) | `-0.35` |
| `sp500_close` | FLOAT64 | Yes | S&P 500 closing value | `5100.00` |
| `sp500_sma_50` | FLOAT64 | Yes | 50-day SMA of S&P 500 | `5050.00` |
| `sp500_sma_200` | FLOAT64 | Yes | 200-day SMA of S&P 500 | `4800.00` |
| `hmm_regime` | STRING | Yes | Market regime classification | `BULL_QUIET` |
| `bq_forecast_5d_pct` | FLOAT64 | Yes | 5-day price forecast (placeholder, currently NULL) | `0.025` |
| `bq_forecast_30d_pct` | FLOAT64 | Yes | 30-day price forecast (placeholder, currently NULL) | `0.068` |
| `composite_score` | FLOAT64 | Yes | Weighted signal score from -1.0 to 1.0 | `0.45` |
| `signal_label` | STRING | Yes | Human-readable signal classification | `BUY` |

**HMM Regime values**:

| Regime | Condition | Interpretation |
|--------|-----------|----------------|
| `BULL_QUIET` | S&P 500 > 200-day SMA AND VIX < 20 | Trending up, low volatility |
| `BULL_VOLATILE` | S&P 500 > 200-day SMA AND VIX >= 20 | Trending up, high volatility |
| `BEAR_QUIET` | S&P 500 <= 200-day SMA AND VIX < 25 | Trending down, low volatility |
| `BEAR_VOLATILE` | S&P 500 <= 200-day SMA AND VIX >= 25 | Trending down, high volatility |
| `SIDEWAYS` | Fallback | Indeterminate |

**Signal label thresholds**:

| Label | Composite Score Range |
|-------|----------------------|
| `STRONG_BUY` | > 0.6 |
| `BUY` | > 0.2 and <= 0.6 |
| `HOLD` | > -0.2 and <= 0.2 |
| `SELL` | > -0.6 and <= -0.2 |
| `STRONG_SELL` | <= -0.6 |

---

## View: `latest_screening_master`

A convenience view that returns only the most recent trading day from `screening_master`.

**Source**: Dataform `latest_screening_master.sqlx`

```sql
SELECT *
FROM amfe_data.screening_master
WHERE date = (SELECT MAX(date) FROM amfe_data.screening_master)
```

This is the primary view that agents query. It returns one row per ticker for the latest available date.

---

## Table: `agent_decisions`

Append-only audit log of agent recommendations. Every time the stock analysis agent produces a BUY/HOLD/SELL recommendation, it inserts a row here for backtesting and accountability.

**Source**: ADK stock_analysis_agent (via `execute_sql INSERT`)

**Partitioning**: `PARTITION BY DATE(timestamp)`
**Clustering**: `CLUSTER BY ticker, action`

| Column | Type | Nullable | Description | Example |
|--------|------|----------|-------------|---------|
| `decision_id` | STRING | NOT NULL | Unique identifier for this decision | `uuid-v4-string` |
| `ticker` | STRING | Yes | Stock ticker analyzed | `NVDA` |
| `timestamp` | TIMESTAMP | Yes | When the decision was made | `2025-03-04 15:30:00 UTC` |
| `action` | STRING | Yes | Recommendation | `BUY` |
| `confidence_score` | FLOAT64 | Yes | Agent's confidence (0.0 to 1.0) | `0.85` |
| `composite_score` | FLOAT64 | Yes | Composite score at time of decision | `0.45` |
| `agent_mode` | STRING | Yes | Which agent mode produced this | `analysis` |
| `quant_signal` | FLOAT64 | Yes | Technical signal component | `0.60` |
| `fundamental_signal` | FLOAT64 | Yes | Fundamental signal component | `0.30` |
| `research_used` | BOOL | Yes | Whether A2A research was invoked | `false` |
| `reasoning` | STRING | Yes | Free-text rationale from the agent | `Strong momentum with RSI at 62...` |
| `session_id` | STRING | Yes | Conversation session identifier | `session-abc-123` |

---

## Partitioning Strategy

All time-series tables use **date-based partitioning**:

- **Why**: Financial data is inherently time-series. Queries almost always filter by date (e.g., "latest day" or "last 30 days"). Date partitioning means BigQuery only scans the relevant partitions, dramatically reducing costs and latency.
- **Cost impact**: A query for `WHERE date = CURRENT_DATE()` scans one partition (~50 rows for 50 tickers) instead of the entire table (potentially millions of rows across years).
- **Partition pruning**: BigQuery automatically prunes partitions when the `WHERE` clause contains a date filter. This is why the `latest_screening_master` view uses a subquery for `MAX(date)` -- it triggers partition pruning.

**Clustering** further optimizes queries within a partition:

- `ticker` clustering means `WHERE ticker = 'NVDA'` scans only the relevant storage blocks.
- `sector` and `signal_label` clustering on `screening_master` optimizes screening queries like `WHERE sector = 'Technology' AND signal_label = 'BUY'`.

---

## Data Retention and Cost Optimization

- **BigQuery storage pricing**: Active storage ($0.02/GB/month) vs. long-term storage ($0.01/GB/month for data > 90 days old). Historical data automatically moves to long-term pricing.
- **Estimated data volume**: 50 tickers x 252 trading days/year x ~1 KB/row = ~12.6 MB/year for `ohlcv_daily`. Even at full S&P 500 (500 tickers), this is ~126 MB/year -- negligible cost.
- **Query costs**: On-demand pricing is $6.25/TB scanned. With partitioning and clustering, most agent queries scan < 1 MB, costing fractions of a cent.
- **Recommended retention**: Keep all historical data (it's cheap). Consider setting a partition expiration on `agent_decisions` if audit data grows large (e.g., 365-day expiration).

---

## Query Patterns and Performance Tips

### Get latest signals for a single ticker
```sql
SELECT *
FROM amfe_data.latest_screening_master
WHERE ticker = 'NVDA'
```
**Performance**: Scans 1 partition, 1 cluster block. Sub-second.

### Screen for stocks matching criteria
```sql
SELECT ticker, close, rsi_14, composite_score, signal_label
FROM amfe_data.latest_screening_master
WHERE sector = 'Technology'
  AND rsi_14 BETWEEN 40 AND 65
  AND composite_score > 0.3
ORDER BY composite_score DESC
LIMIT 20
```
**Performance**: Scans 1 partition, clustered by sector. Sub-second.

### Get historical signals for backtesting
```sql
SELECT ticker, date, composite_score, signal_label, close
FROM amfe_data.screening_master
WHERE ticker = 'AAPL'
  AND date BETWEEN '2024-01-01' AND '2024-12-31'
ORDER BY date
```
**Performance**: Scans ~252 partitions, but clustered by ticker. Fast.

### Count stocks by regime
```sql
SELECT hmm_regime, COUNT(*) AS cnt, AVG(composite_score) AS avg_score
FROM amfe_data.latest_screening_master
GROUP BY hmm_regime
```

### Log an agent decision
```sql
INSERT INTO amfe_data.agent_decisions
  (decision_id, ticker, timestamp, action, confidence_score, composite_score,
   agent_mode, quant_signal, fundamental_signal, research_used, reasoning, session_id)
VALUES
  (GENERATE_UUID(), 'NVDA', CURRENT_TIMESTAMP(), 'BUY', 0.85, 0.45,
   'analysis', 0.60, 0.30, FALSE, 'Strong momentum with moderate RSI', 'session-123')
```

---

## Cross-References

- System overview: [docs/architecture/system-overview.md](./system-overview.md)
- Transformation pipeline details: [docs/data-pipeline/transformations.md](../data-pipeline/transformations.md)
- Ingestion pipeline: [docs/data-pipeline/ingestion.md](../data-pipeline/ingestion.md)
