# Dataform Transformation Pipeline Documentation

## Overview

The Dataform pipeline transforms raw data (OHLCV prices, macro indicators, SEC filings) into a single wide screening table that agents can query efficiently. It runs as a DAG (directed acyclic graph) in Google Dataform after each ingestion.

**Repository**: `amfe-pipelines`
**Schedule**: Triggered after ingestion (~16:30 ET) or scheduled at 17:00 ET as fallback

---

## DAG Structure

```
ohlcv_daily ─────────┐
                      ├──> technical_signals.sqlx ──┐
                      │                             │
sec_filings ──────────┤                             ├──> screening_master.sqlx ──> latest_screening_master (VIEW)
                      ├──> fundamental_scores.sqlx ─┘          │
ohlcv_daily ──────────┘                                        │
                                                               │
macro_indicators ──────────────────────────────────────────────┘
```

**Execution order** (enforced by Dataform `${ref()}` dependencies):
1. `technical_signals.sqlx` -- depends on `ohlcv_daily`
2. `fundamental_scores.sqlx` -- depends on `sec_filings` and `ohlcv_daily`
3. `screening_master.sqlx` -- depends on `technical_signals`, `fundamental_scores`, and `macro_indicators`
4. `latest_screening_master.sqlx` -- depends on `screening_master` (view, no materialization cost)

Steps 1 and 2 are independent and can run in parallel if Dataform's scheduler allows.

---

## Pipeline 1: `technical_signals.sqlx`

**Source file**: `/dataform/technical_signals.sqlx`

### Configuration

```
config {
  type: "table",
  schema: "amfe_data",
  bigquery: {
    partitionBy: "date",
    clusterBy: ["ticker"]
  }
}
```

### CTE Structure

The query is organized into a chain of Common Table Expressions (CTEs):

```
source → rsi_components → rsi_calc → ema_calc → macd_line → macd_signal → indicators → final SELECT
```

### RSI-14 and RSI-2 Computation

The Relative Strength Index measures the speed and magnitude of recent price changes to identify overbought/oversold conditions.

**Formula (Wilder's RSI)**:

```
RS = Average Gain over N periods / Average Loss over N periods
RSI = 100 - (100 / (1 + RS))
```

**Implementation approach**: The pipeline uses a **windowed average approximation** of Wilder's smoothing. True Wilder's smoothing is recursive (`new_avg = (prev_avg * 12 + current) / 13`), which is difficult to express in SQL. The approximation uses `AVG() OVER (ROWS BETWEEN N-1 PRECEDING AND CURRENT ROW)`, which is a simple moving average of gains/losses.

**Step 1 -- Compute price changes**:
```sql
close - LAG(close, 1) OVER (PARTITION BY ticker ORDER BY date) AS price_change
```

**Step 2 -- Separate gains and losses**:
```sql
GREATEST(price_change, 0) AS gain,
GREATEST(-price_change, 0) AS loss
```

**Step 3 -- Windowed averages**:
```sql
-- RSI-14: 14-period average
AVG(gain) OVER (PARTITION BY ticker ORDER BY date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS avg_gain_14,
AVG(loss) OVER (PARTITION BY ticker ORDER BY date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS avg_loss_14,

-- RSI-2: 2-period average
AVG(gain) OVER (PARTITION BY ticker ORDER BY date ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS avg_gain_2,
AVG(loss) OVER (PARTITION BY ticker ORDER BY date ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS avg_loss_2
```

**Step 4 -- Final RSI calculation**:
```sql
CASE
  WHEN avg_loss_14 = 0 THEN 100.0
  ELSE 100.0 - (100.0 / (1.0 + avg_gain_14 / avg_loss_14))
END AS rsi_14
```

**Interpretation**:
- RSI > 70: Overbought (potential reversal down)
- RSI < 30: Oversold (potential reversal up)
- RSI-2 is more sensitive and used for short-term mean reversion signals

### MACD (12-26-9) Computation

The Moving Average Convergence Divergence indicator identifies trend direction and momentum.

**Components**:
- **MACD Line**: EMA-12 minus EMA-26 of the closing price
- **Signal Line**: 9-period EMA of the MACD Line
- **Histogram**: MACD Line minus Signal Line

**EMA approximation in SQL**: True EMA is recursive, so the pipeline uses a **weighted average approximation** with exponential decay weights:

```sql
-- EMA-12 approximation using correlated subquery
(
  SELECT SUM(sub.close * POW(1.0 - 2.0/13.0, rc.rn - sub.rn))
    / NULLIF(SUM(POW(1.0 - 2.0/13.0, rc.rn - sub.rn)), 0)
  FROM source sub
  WHERE sub.ticker = rc.ticker
    AND sub.rn BETWEEN GREATEST(rc.rn - 11, 1) AND rc.rn
) AS ema_12
```

**How the EMA approximation works**:
- The smoothing factor `alpha = 2 / (N + 1)`:
  - EMA-12: `alpha = 2/13 = 0.1538`
  - EMA-26: `alpha = 2/27 = 0.0741`
  - Signal line (EMA-9 of MACD): `alpha = 2/10 = 0.2`
- Each observation is weighted by `(1 - alpha)^k` where `k` is the lag (0 for current, 1 for previous, etc.)
- The weighted sum is divided by the sum of weights for normalization
- This is computed using correlated subqueries with `ROW_NUMBER()` for positional indexing

**Signal line computation** (same weighted-average approach, applied to MACD values):
```sql
(
  SELECT SUM(sub.macd * POW(1.0 - 2.0/10.0, ml.rn - sub.rn))
    / NULLIF(SUM(POW(1.0 - 2.0/10.0, ml.rn - sub.rn)), 0)
  FROM macd_line sub
  WHERE sub.ticker = ml.ticker
    AND sub.rn BETWEEN GREATEST(ml.rn - 8, 1) AND ml.rn
) AS macd_signal
```

**Interpretation**:
- Positive MACD histogram: Bullish momentum (MACD above signal)
- Negative MACD histogram: Bearish momentum (MACD below signal)
- Histogram crossing zero: Potential trend change

### Bollinger Bands (20-day, 2 standard deviations)

Bollinger Bands measure price volatility and identify overbought/oversold conditions relative to recent price action.

**Components**:
```sql
-- Middle band: 20-day SMA
AVG(close) OVER (PARTITION BY ticker ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS bb_sma_20,

-- Standard deviation for band width
STDDEV_POP(close) OVER (PARTITION BY ticker ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS bb_stddev_20,

-- Upper band: SMA + 2 * stddev
bb_sma_20 + 2 * bb_stddev_20 AS bb_upper,

-- Lower band: SMA - 2 * stddev
bb_sma_20 - 2 * bb_stddev_20 AS bb_lower
```

**%B (bb_pct)** -- The key derived metric:
```sql
SAFE_DIVIDE(
  close - (bb_sma_20 - 2 * bb_stddev_20),
  (bb_sma_20 + 2 * bb_stddev_20) - (bb_sma_20 - 2 * bb_stddev_20)
) AS bb_pct
```

**Interpretation of bb_pct**:
| bb_pct Value | Meaning |
|--------------|---------|
| 0.0 | Price at the lower band |
| 0.5 | Price at the middle band (SMA-20) |
| 1.0 | Price at the upper band |
| < 0.0 | Price below the lower band (extreme oversold) |
| > 1.0 | Price above the upper band (extreme overbought) |

**Note**: `STDDEV_POP` is used instead of `STDDEV_SAMP` because we're computing over a fixed 20-day population, not estimating a sample statistic.

### Simple Moving Averages (SMA 20/50/200)

```sql
AVG(close) OVER (PARTITION BY ticker ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)  AS sma_20,
AVG(close) OVER (PARTITION BY ticker ORDER BY date ROWS BETWEEN 49 PRECEDING AND CURRENT ROW)  AS sma_50,
AVG(close) OVER (PARTITION BY ticker ORDER BY date ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS sma_200,
```

**SMA Cross (Golden/Death Cross)**:
```sql
sma_20 - sma_50 AS sma_cross_20_50
```

- **Positive value (golden cross)**: SMA-20 is above SMA-50, indicating bullish short-term trend relative to medium-term.
- **Negative value (death cross)**: SMA-20 is below SMA-50, indicating bearish trend.
- The magnitude indicates the strength of the divergence.

**Distance from SMAs** -- How far the current price is from each SMA, as a percentage:
```sql
SAFE_DIVIDE(close - sma_20,  sma_20)  AS dist_from_sma_20,
SAFE_DIVIDE(close - sma_50,  sma_50)  AS dist_from_sma_50,
SAFE_DIVIDE(close - sma_200, sma_200) AS dist_from_sma_200,
```

### ATR-14 (Average True Range)

ATR measures volatility. It is the 14-day average of the **True Range**, which accounts for gaps between days.

**True Range** = max of:
1. `high - low` (intraday range)
2. `|high - previous_close|` (gap up then reversal)
3. `|low - previous_close|` (gap down then reversal)

```sql
-- True Range components (computed in source CTE)
high - low AS hl_range,
ABS(high - LAG(close, 1) OVER (...)) AS hc_range,
ABS(low  - LAG(close, 1) OVER (...)) AS lc_range,

-- ATR-14: average of True Range over 14 days
AVG(GREATEST(hl_range, hc_range, lc_range)) OVER (
  PARTITION BY ticker ORDER BY date
  ROWS BETWEEN 13 PRECEDING AND CURRENT ROW
) AS atr_14
```

### Price Changes and 52-Week High/Low

```sql
-- Lagged closes
LAG(close, 1)  OVER (...) AS close_1d_ago,
LAG(close, 5)  OVER (...) AS close_5d_ago,
LAG(close, 30) OVER (...) AS close_30d_ago,

-- Percent changes
SAFE_DIVIDE(close - close_1d_ago,  close_1d_ago)  AS pct_change_1d,
SAFE_DIVIDE(close - close_5d_ago,  close_5d_ago)  AS pct_change_5d,
SAFE_DIVIDE(close - close_30d_ago, close_30d_ago) AS pct_change_30d,

-- 52-week window (252 trading days)
MAX(high) OVER (PARTITION BY ticker ORDER BY date ROWS BETWEEN 251 PRECEDING AND CURRENT ROW) AS high_52w,
MIN(low)  OVER (PARTITION BY ticker ORDER BY date ROWS BETWEEN 251 PRECEDING AND CURRENT ROW) AS low_52w,

-- Distance from 52-week high (always <= 0)
SAFE_DIVIDE(close - high_52w, high_52w) AS pct_from_52w_high
```

### Row Filtering

```sql
WHERE rn >= 2  -- need at least one prior row for price_change (LAG)
```

The `ROW_NUMBER()` column (`rn`) filters out the very first row per ticker, which has no prior close for computing price changes.

---

## Pipeline 2: `fundamental_scores.sqlx`

**Source file**: `/dataform/fundamental_scores.sqlx`

### Configuration

```
config {
  type: "table",
  schema: "amfe_data",
  bigquery: {
    partitionBy: "date",
    clusterBy: ["ticker"]
  }
}
```

### CTE Structure

```
filing_data → filing_with_growth → filing_scores → trading_days → joined → forward_filled → final SELECT
```

### Point-in-Time Join Logic

The most critical design decision in this pipeline is the **point-in-time join**, which prevents look-ahead bias:

```sql
-- For each trading day, attach the most recent filing on or BEFORE that date
LEFT JOIN filing_scores fs
  ON td.ticker = fs.ticker
  AND fs.filing_date = (
    SELECT MAX(fs2.filing_date)
    FROM filing_scores fs2
    WHERE fs2.ticker = td.ticker
      AND fs2.filing_date <= td.date  -- KEY: only filings published before this date
  )
```

**Why this matters**: A company might report Q4 earnings on February 15. If we used today's fundamental data for January trading days, we'd be using information that didn't exist yet (look-ahead bias). The point-in-time join ensures that January trading days only use Q3 data (the latest filing available at that time).

### Forward-Fill with LAST_VALUE IGNORE NULLS

After the point-in-time join, some trading days may have NULL fundamentals (e.g., the very first trading days before any filing is available). The pipeline fills these gaps:

```sql
LAST_VALUE(earnings_per_share IGNORE NULLS) OVER (
  PARTITION BY ticker ORDER BY date
  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
) AS earnings_per_share
```

This carries forward the most recent non-NULL value. Applied to all fundamental columns: `earnings_per_share`, `book_value_per_share`, `revenue`, `net_income`, `shares_outstanding`, `debt_to_equity`, `current_ratio`, `revenue_growth_qoq`, `revenue_growth_yoy`, `earnings_surprise_pct`.

### Ratio Formulas

**P/E Ratio** (Price-to-Earnings):
```sql
SAFE_DIVIDE(close, earnings_per_share) AS pe_ratio
```
- Uses the current day's close price divided by the most recent reported EPS
- Higher P/E = more expensive relative to earnings
- `SAFE_DIVIDE` returns NULL if EPS is 0 or NULL (avoiding division by zero)

**P/B Ratio** (Price-to-Book):
```sql
SAFE_DIVIDE(close, book_value_per_share) AS pb_ratio
```
- Close price divided by book value per share
- P/B < 1 may indicate undervaluation (or distressed assets)

**P/S Ratio** (Price-to-Sales):
```sql
SAFE_DIVIDE(close * shares_outstanding, revenue) AS ps_ratio
```
- Market capitalization divided by total revenue
- Useful for comparing companies that don't yet have earnings

### Revenue Growth Calculations

**Quarter-over-Quarter (QoQ)**:
```sql
LAG(revenue, 1) OVER (PARTITION BY ticker ORDER BY period_end_date) AS revenue_prev_q,
SAFE_DIVIDE(revenue - revenue_prev_q, ABS(revenue_prev_q)) AS revenue_growth_qoq
```

**Year-over-Year (YoY)**:
```sql
LAG(revenue, 4) OVER (PARTITION BY ticker ORDER BY period_end_date) AS revenue_prev_y,
SAFE_DIVIDE(revenue - revenue_prev_y, ABS(revenue_prev_y)) AS revenue_growth_yoy
```

- YoY uses `LAG(..., 4)` because each row represents one quarter, and 4 quarters back is the same quarter from the previous year.
- `ABS()` in the denominator handles the case where previous revenue was negative (rare but possible for certain accounting treatments).

### Earnings Surprise

```sql
SAFE_DIVIDE(
  earnings_actual - earnings_estimate,
  ABS(earnings_estimate)
) AS earnings_surprise_pct
```

- Positive value: Company beat estimates (positive surprise)
- Negative value: Company missed estimates
- Example: `earnings_actual = 1.50`, `earnings_estimate = 1.25` yields `(1.50 - 1.25) / 1.25 = 0.20` (20% beat)

---

## Pipeline 3: `screening_master.sqlx`

**Source file**: `/dataform/screening_master.sqlx`

### Configuration

```
config {
  type: "table",
  schema: "amfe_data",
  bigquery: {
    partitionBy: "date",
    clusterBy: ["ticker", "sector", "signal_label"]
  }
}
```

### CTE Structure

```
tech → fund → macro → regime → combined → scored → final SELECT
```

### Join Strategy

The pipeline uses **LEFT JOINs** throughout:

```sql
FROM tech t
LEFT JOIN fund f
  ON t.ticker = f.ticker AND t.date = f.date
LEFT JOIN regime r
  ON t.date = r.date
```

**Why LEFT JOINs?**
- `technical_signals` is the driving table (always has data for every ticker/date with OHLCV data).
- `fundamental_scores` may be missing for tickers without SEC filings or before the first filing date.
- `macro/regime` data may be missing for dates where FRED data isn't yet available.
- LEFT JOINs ensure that the screening table always has a row for every ticker/date, even if some columns are NULL.
- The composite score formula uses `COALESCE(..., 0)` to handle NULLs from missing joins.

### HMM Regime Classification Logic

The "HMM regime" is a rule-based market regime classifier (not a true Hidden Markov Model -- see [system-overview.md](../architecture/system-overview.md#design-principles-and-trade-offs) for the design rationale).

**Macro CTE** -- Computes S&P 500 trend indicators:
```sql
AVG(sp500_close) OVER (ORDER BY date ROWS BETWEEN 49 PRECEDING AND CURRENT ROW)  AS sp500_sma_50,
AVG(sp500_close) OVER (ORDER BY date ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS sp500_sma_200
```

**Regime classification**:
```sql
CASE
  WHEN sp500_close > sp500_sma_200 AND vix_close < 20  THEN 'BULL_QUIET'
  WHEN sp500_close > sp500_sma_200 AND vix_close >= 20 THEN 'BULL_VOLATILE'
  WHEN sp500_close <= sp500_sma_200 AND vix_close < 25 THEN 'BEAR_QUIET'
  WHEN sp500_close <= sp500_sma_200 AND vix_close >= 25 THEN 'BEAR_VOLATILE'
  ELSE 'SIDEWAYS'
END AS hmm_regime
```

**Decision logic**:

| S&P 500 vs. 200-day SMA | VIX Level | Regime |
|--------------------------|-----------|--------|
| Above | < 20 | BULL_QUIET |
| Above | >= 20 | BULL_VOLATILE |
| Below | < 25 | BEAR_QUIET |
| Below | >= 25 | BEAR_VOLATILE |
| Fallback | Any | SIDEWAYS |

**Note on VIX thresholds**: The asymmetric thresholds (20 for bull, 25 for bear) reflect the observation that VIX tends to run higher during bear markets. A VIX of 22 during a bull market is notable volatility, but during a bear market it's relatively calm.

### Composite Score Formula

The composite score is a regime-adjusted weighted sum of four signal components, producing a value roughly in the range of -1.0 to 1.0.

**Components and their contributions**:

| Component | Calculation | Range | Meaning |
|-----------|-------------|-------|---------|
| ML forecast | `TANH(bq_forecast_5d_pct * 10)` | -1 to 1 | TimesFM 5-day forecast direction |
| RSI contrarian | `(50 - rsi_14) / 50` | -1 to 1 | Negative when overbought, positive when oversold |
| MACD momentum | `SIGN(macd_histogram)` | -1, 0, or 1 | Direction of momentum |
| SMA trend | `SIGN(sma_cross_20_50)` | -1, 0, or 1 | Short-term vs. medium-term trend |

**Regime-adjusted weights**:

| Regime | Forecast | RSI | MACD | SMA Trend | Rationale |
|--------|----------|-----|------|-----------|-----------|
| BULL_QUIET | 0.40 | 0.25 | 0.20 | 0.15 | Trust forecasts in calm bull markets |
| BEAR_VOLATILE | 0.10 | 0.50 | 0.25 | 0.15 | Heavy mean-reversion bias; don't trust forecasts in chaos |
| All others | 0.30 | 0.30 | 0.20 | 0.20 | Balanced approach |

**Implementation**:
```sql
CASE hmm_regime
  WHEN 'BULL_QUIET' THEN
      0.40 * TANH(COALESCE(bq_forecast_5d_pct, 0) * 10)
    + 0.25 * (50 - COALESCE(rsi_14, 50)) / 50
    + 0.20 * SIGN(COALESCE(macd_histogram, 0))
    + 0.15 * SIGN(COALESCE(sma_cross_20_50, 0))
  WHEN 'BEAR_VOLATILE' THEN
      0.10 * TANH(COALESCE(bq_forecast_5d_pct, 0) * 10)
    + 0.50 * (50 - COALESCE(rsi_14, 50)) / 50
    + 0.25 * SIGN(COALESCE(macd_histogram, 0))
    + 0.15 * SIGN(COALESCE(sma_cross_20_50, 0))
  ELSE
      0.30 * TANH(COALESCE(bq_forecast_5d_pct, 0) * 10)
    + 0.30 * (50 - COALESCE(rsi_14, 50)) / 50
    + 0.20 * SIGN(COALESCE(macd_histogram, 0))
    + 0.20 * SIGN(COALESCE(sma_cross_20_50, 0))
END AS composite_score
```

**TANH normalization**: The `TANH(x * 10)` function squashes the forecast percentage into the [-1, 1] range. The `* 10` factor means that a 5% forecast is already saturated near 1.0 (`TANH(0.05 * 10) = TANH(0.5) = 0.46`), while a 20% forecast is fully saturated (`TANH(2.0) = 0.96`). This prevents extreme forecasts from dominating the score.

**NULL handling**: Every input is wrapped in `COALESCE(..., neutral_value)`:
- `bq_forecast_5d_pct` defaults to 0 (no forecast = no signal)
- `rsi_14` defaults to 50 (neutral RSI)
- `macd_histogram` defaults to 0 (no momentum signal)
- `sma_cross_20_50` defaults to 0 (no trend signal)

### Signal Label Bucketing

```sql
CASE
  WHEN composite_score >  0.6 THEN 'STRONG_BUY'
  WHEN composite_score >  0.2 THEN 'BUY'
  WHEN composite_score > -0.2 THEN 'HOLD'
  WHEN composite_score > -0.6 THEN 'SELL'
  ELSE 'STRONG_SELL'
END AS signal_label
```

**Distribution note**: Because the composite score components are bounded, the practical range clusters around [-0.5, 0.5]. STRONG_BUY (> 0.6) and STRONG_SELL (< -0.6) are relatively rare, requiring alignment across multiple signals.

### ML Forecast Placeholder Columns

```sql
CAST(NULL AS FLOAT64) AS bq_forecast_5d_pct,
CAST(NULL AS FLOAT64) AS bq_forecast_30d_pct
```

These columns are placeholders for BigQuery ML `ML.FORECAST` (TimesFM) predictions. They will be populated once a BigQuery ML time series model is trained on the `ohlcv_daily` data. Until then, the composite score formula treats them as 0 via `COALESCE`.

### The Sector Data Gap

The `sector` column is derived as:
```sql
COALESCE(t.sector, 'UNKNOWN') AS sector
```

**Current issue**: The `ohlcv_daily` table (from Alpaca) does not include a `sector` column, so `t.sector` is always NULL, defaulting to `'UNKNOWN'`. This means sector-based screening queries (e.g., "find tech stocks") will not work correctly.

**Recommended fix**: Create a `ticker_metadata` dimension table:

```sql
CREATE TABLE amfe_data.ticker_metadata (
  ticker       STRING NOT NULL,
  company_name STRING,
  sector       STRING,
  industry     STRING,
  market_cap   FLOAT64,
  exchange     STRING,
  PRIMARY KEY (ticker) NOT ENFORCED
);
```

This table can be populated from Alpaca's asset metadata API or a static CSV. The `screening_master.sqlx` join would then become:

```sql
LEFT JOIN `amfe_data.ticker_metadata` meta ON t.ticker = meta.ticker
...
COALESCE(meta.sector, 'UNKNOWN') AS sector
```

---

## Pipeline 4: `latest_screening_master.sqlx` (View)

**Source file**: `/dataform/latest_screening_master.sqlx`

```
config {
  type: "view",
  schema: "amfe_data",
  description: "Latest day snapshot of the screening master table"
}

SELECT *
FROM ${ref("screening_master")}
WHERE date = (SELECT MAX(date) FROM ${ref("screening_master")})
```

This is a **view** (not a materialized table), meaning:
- No additional storage cost
- Always up-to-date with the latest `screening_master` data
- The subquery `SELECT MAX(date)` triggers BigQuery partition pruning, so only the most recent partition is scanned

**Agent usage**: This is the primary view that agents query. When a user asks "analyze NVDA" or "find momentum stocks", the agent queries `latest_screening_master` to get today's signals.

---

## Cross-References

- System overview: [docs/architecture/system-overview.md](../architecture/system-overview.md)
- BigQuery schema: [docs/architecture/bigquery-schema.md](../architecture/bigquery-schema.md)
- Ingestion pipeline: [docs/data-pipeline/ingestion.md](./ingestion.md)
- Historical backfill: [docs/data-pipeline/seed-historical.md](./seed-historical.md)
