# get_stock_profile Tool — Realtime Quote + Batch Signals

> **Source:** [`/mcp_toolbox/realtime_quote.py`](../../mcp_toolbox/realtime_quote.py)
> **Exposed as:** Python function tool on the Stock Analysis Agent
> **Queries:** `amfe_data.latest_screening_master` (BigQuery) + yfinance (realtime)

---

## Purpose

`get_stock_profile` is the first tool called by the **Stock Analysis Agent (Mode A)** when
analyzing a single ticker. It combines two data sources in a single call:

1. **Batch signals** from BigQuery's `latest_screening_master` view — pre-computed overnight
   indicators including RSI, MACD, composite score, HMM regime, and forecast data.
2. **Realtime intraday quote** from yfinance — current price, open, and intraday percentage
   change.

This two-phase design gives the agent both the "overnight thesis" (batch signals) and the
"morning reality" (live price action) so it can detect discrepancies (e.g., a STRONG_BUY
signal with a -7% intraday drop).

---

## Function Signature

```python
def get_stock_profile(ticker: str) -> Dict[str, Any]
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `ticker` | `str` | Yes | Stock symbol, e.g., `"NVDA"`, `"AAPL"`. Automatically uppercased and stripped. |

---

## Two-Phase Fetch Architecture

### Phase 1: BigQuery Batch Signals

```python
query = """
    SELECT * FROM `amfe_data.latest_screening_master`
    WHERE ticker = @ticker
    LIMIT 1
"""
job_config = bigquery.QueryJobConfig(
    query_parameters=[
        bigquery.ScalarQueryParameter("ticker", "STRING", ticker)
    ]
)
```

This retrieves the full screening_master row for the ticker from the most recent trading
day. The query is parameterized (same safety pattern as `execute_screen`). The view
`latest_screening_master` is pre-filtered to `MAX(date)`, so no date parameter is needed.

**Returned fields include:** `ticker`, `date`, `sector`, `industry`, `market_cap`, `close`,
`rsi_14`, `rsi_2`, `macd`, `macd_signal`, `macd_histogram`, `bb_pct`, `sma_20`, `sma_50`,
`sma_200`, `sma_cross_20_50`, `pe_ratio`, `pb_ratio`, `ps_ratio`, `revenue_growth_yoy`,
`hmm_regime`, `hmm_confidence`, `bq_forecast_5d_pct`, `bq_forecast_30d_pct`,
`composite_score`, `signal_label`, and more.

### Phase 2: yfinance Realtime Quote

```python
stock = yf.Ticker(ticker)
hist = stock.history(period="1d")

current_price = float(hist['Close'].iloc[-1])
open_price = float(hist['Open'].iloc[-1])
intraday_pct = ((current_price - open_price) / open_price)
```

This fetches the current trading day's OHLCV data. The `period="1d"` parameter returns
only today's bar, keeping the request lightweight.

---

## Intraday Percentage Calculation

The intraday move is computed as:

```
intraday_pct_change = (current_price - open_price) / open_price
```

This measures how much the stock has moved since market open, not since the previous close.
This is intentional — the agent needs to detect intraday momentum or reversals relative to
the opening auction, which better reflects today's market sentiment.

The value is returned as a decimal (e.g., `0.0325` for +3.25%) and rounded to 4 decimal
places.

---

## Response Structure

### Full Success

```json
{
  "ticker": "NVDA",
  "batch_signals": {
    "ticker": "NVDA",
    "date": "2026-03-04",
    "sector": "Technology",
    "close": 875.50,
    "rsi_14": 58.3,
    "macd_histogram": 1.24,
    "composite_score": 0.72,
    "signal_label": "STRONG_BUY",
    "hmm_regime": "BULL_QUIET",
    "bq_forecast_5d_pct": 0.031,
    "last_updated": "2026-03-04 21:00:00+00:00"
  },
  "realtime_quote": {
    "current_price": 882.15,
    "open_price": 876.00,
    "intraday_pct_change": 0.0070
  },
  "status": "success"
}
```

### Partial Failure

If either phase fails, the function does not abort. Instead, it returns the data that was
successfully fetched and sets `status` to `"partial_failure"`:

```json
{
  "ticker": "NVDA",
  "batch_signals": {
    "error": "BQ fetch failed: 403 Access Denied..."
  },
  "realtime_quote": {
    "current_price": 882.15,
    "open_price": 876.00,
    "intraday_pct_change": 0.0070
  },
  "status": "partial_failure"
}
```

This design ensures the agent always gets some data to work with. Possible partial failure
scenarios:

| BigQuery | yfinance | Status | Agent Behavior |
|----------|----------|--------|----------------|
| OK | OK | `"success"` | Full analysis with both signals and live data |
| OK | Failed | `"partial_failure"` | Analysis using batch signals only, note stale pricing |
| Failed | OK | `"partial_failure"` | Limited analysis using only live price, no technical/fundamental signals |
| Failed | Failed | `"partial_failure"` | Agent reports inability to analyze, suggests retry |

### No Data Found

When the ticker is not in the screening universe:

```json
{
  "ticker": "XYZZ",
  "batch_signals": {
    "error": "No batch data found for this ticker."
  },
  "realtime_quote": {
    "error": "No realtime trade data found today."
  },
  "status": "success"
}
```

Note that missing data is **not** treated as a failure — the function succeeded in
determining that no data exists.

---

## Date Serialization

Same pattern as `execute_screen`. BigQuery's `datetime.date` and `datetime.datetime` objects
are converted to strings before returning:

```python
if 'date' in row_dict and row_dict['date']:
     row_dict['date'] = str(row_dict['date'])
if 'last_updated' in row_dict and row_dict['last_updated']:
     row_dict['last_updated'] = str(row_dict['last_updated'])
```

---

## Why yfinance Over Alpaca for Realtime

The system uses Alpaca for historical data ingestion (nightly batch job) but yfinance for
realtime quotes. The reasons:

| Factor | yfinance | Alpaca Realtime |
|--------|----------|-----------------|
| **Authentication** | None required | API key + secret required |
| **Rate limits** | Generous for single-ticker lookups | Stricter, designed for streaming |
| **Latency** | ~200-500ms per call | Lower with WebSocket, but setup overhead |
| **Data scope** | OHLCV + basic info | Full market data |
| **Complexity** | 3 lines of code | Client setup + auth + error handling |
| **Cost** | Free | Free tier limited |

For a single-ticker profile lookup (not streaming), yfinance provides the simplest path
with no configuration. The trade-off is slightly higher latency and less granular data,
which is acceptable for an agent that runs one analysis at a time.

---

## Performance Considerations

### BigQuery Phase
- **Cold query:** ~1-2 seconds (BigQuery slot allocation overhead)
- **Warm query:** ~300-800ms (view already resolved, data cached)
- The `latest_screening_master` view resolves to a single partition, and the table is
  clustered by `ticker`, making single-ticker lookups very fast.

### yfinance Phase
- **Typical latency:** 200-500ms
- **Market hours vs. off-hours:** During market hours, `history(period="1d")` returns live
  data. Outside market hours, it returns the most recent completed trading day.
- **Rate limiting:** yfinance uses Yahoo Finance's undocumented API. Under normal single-ticker
  usage, rate limits are not an issue. Avoid calling in tight loops.

### Total Latency
- **Expected:** 500ms - 2.5 seconds for the complete profile
- **Sequential execution:** The two phases run sequentially (BigQuery first, then yfinance).
  A future optimization could run them in parallel using `asyncio` or `concurrent.futures`.

---

## BigQuery Client Initialization

Identical pattern to `stock_api.py`:

```python
creds, project_id = google.auth.default()
project_id = project_id or os.getenv("GOOGLE_CLOUD_PROJECT")
bq_client = bigquery.Client(credentials=creds, project=project_id)
```

See the [Stock API documentation](./stock-api.md#bigquery-client-initialization) for details
on how credentials resolve in different environments.

---

## Usage in Agent Flow

The Stock Analysis Agent calls `get_stock_profile` as **Step 1** in its analysis sequence:

```
1. get_stock_profile(ticker)   <-- This tool
2. forecast(ticker)            <-- MCP Toolbox: 5-day TimesFM forecast
3. [Conditional] Research      <-- If intraday move > 5% or user requests
4. execute_sql(INSERT INTO)    <-- Log decision to agent_decisions
```

The agent uses the `signal_label` from batch signals as the starting thesis, then checks
whether the realtime intraday data contradicts it. A significant discrepancy (e.g.,
`signal_label = "STRONG_BUY"` but `intraday_pct_change = -0.07`) triggers the conditional
deep-dive research step.

---

## Related Documentation

- [Stock Screening API](./stock-api.md) — Companion tool for multi-ticker screening
- [MCP Toolbox Configuration](./mcp-toolbox.md) — The `forecast` tool used in Step 2
- [Environment Setup](./environment-setup.md) — Credential configuration
- [Architecture](../../architecture.md) — Full system design, Mode A flow
