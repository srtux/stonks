# execute_screen Tool — Stock Screening API

> **Source:** [`/mcp_toolbox/stock_api.py`](../../mcp_toolbox/stock_api.py)
> **Exposed as:** Python function tool on the Screener Agent
> **Queries:** `amfe_data.latest_screening_master` (BigQuery view)

---

## Purpose

`execute_screen` is the primary tool used by the **Screener Agent (Mode C)** to translate
natural-language stock screening requests into safe, parameterized SQL queries against
BigQuery. It eliminates the need for the LLM to generate raw SQL for screening operations,
which would introduce SQL injection risk and inconsistent query structure.

The agent maps user intent (e.g., "find undervalued tech stocks with momentum") to a set
of typed keyword arguments. The function then dynamically constructs a WHERE clause using
BigQuery parameterized queries — no string interpolation ever touches the SQL.

---

## Function Signature

```python
def execute_screen(
    sector: Optional[str] = None,
    industry: Optional[str] = None,
    market_cap_min: Optional[float] = None,
    market_cap_max: Optional[float] = None,
    rsi_14_min: Optional[float] = None,
    rsi_14_max: Optional[float] = None,
    macd_histogram_min: Optional[float] = None,
    macd_histogram_max: Optional[float] = None,
    sma_cross_20_50_min: Optional[float] = None,
    sma_cross_20_50_max: Optional[float] = None,
    pe_ratio_min: Optional[float] = None,
    pe_ratio_max: Optional[float] = None,
    revenue_growth_yoy_min: Optional[float] = None,
    hmm_regime: Optional[list[str]] = None,
    composite_score_min: Optional[float] = None,
    signal_label: Optional[str] = None,
    bq_forecast_5d_pct_min: Optional[float] = None,
    limit: int = 20
) -> Dict[str, Any]
```

---

## Parameter Reference

| # | Parameter | Type | Required | Description | Valid Range / Values | Example |
|---|-----------|------|----------|-------------|---------------------|---------|
| 1 | `sector` | `str` | No | GICS sector filter | `"Technology"`, `"Healthcare"`, `"Energy"`, etc. | `"Technology"` |
| 2 | `industry` | `str` | No | GICS industry sub-classification | `"Software - Infrastructure"`, `"Semiconductors"` | `"Semiconductors"` |
| 3 | `market_cap_min` | `float` | No | Minimum market capitalization (USD) | `> 0` | `2e9` (2 billion) |
| 4 | `market_cap_max` | `float` | No | Maximum market capitalization (USD) | `> 0` | `10e9` (10 billion) |
| 5 | `rsi_14_min` | `float` | No | Minimum 14-day Relative Strength Index | `0 - 100` | `40` |
| 6 | `rsi_14_max` | `float` | No | Maximum 14-day Relative Strength Index | `0 - 100` | `65` |
| 7 | `macd_histogram_min` | `float` | No | Minimum MACD histogram value | Unbounded (typically `-5` to `5`) | `0.0` |
| 8 | `macd_histogram_max` | `float` | No | Maximum MACD histogram value | Unbounded | `2.0` |
| 9 | `sma_cross_20_50_min` | `float` | No | Minimum 20/50 SMA cross distance (positive = bullish golden cross) | Unbounded | `0` (bullish only) |
| 10 | `sma_cross_20_50_max` | `float` | No | Maximum 20/50 SMA cross distance | Unbounded | `5.0` |
| 11 | `pe_ratio_min` | `float` | No | Minimum price-to-earnings ratio | `> 0` typically | `5` |
| 12 | `pe_ratio_max` | `float` | No | Maximum price-to-earnings ratio | `> 0` typically | `20` |
| 13 | `revenue_growth_yoy_min` | `float` | No | Minimum year-over-year revenue growth (decimal) | `-1.0` to unbounded | `0.05` (5%) |
| 14 | `hmm_regime` | `list[str]` | No | Acceptable HMM market regime(s) | `BULL_QUIET`, `BULL_VOLATILE`, `BEAR_QUIET`, `BEAR_VOLATILE`, `SIDEWAYS` | `["BULL_QUIET", "BULL_VOLATILE"]` |
| 15 | `composite_score_min` | `float` | No | Minimum composite quant score | `-1.0` to `1.0` | `0.3` |
| 16 | `signal_label` | `str` | No | Exact signal bucket filter | `STRONG_BUY`, `BUY`, `HOLD`, `SELL`, `STRONG_SELL` | `"STRONG_BUY"` |
| 17 | `bq_forecast_5d_pct_min` | `float` | No | Minimum 5-day TimesFM forecast (decimal) | Unbounded | `0.02` (2%) |
| 18 | `limit` | `int` | No | Max results returned (default: 20, hard max: 100) | `1 - 100` | `10` |

All parameters except `limit` default to `None`, meaning "no constraint on this dimension."

---

## SQL Generation Logic

### Base Query

The function always starts from the `latest_screening_master` view, which is pre-filtered
to the most recent trading day:

```sql
SELECT * FROM `amfe_data.latest_screening_master` WHERE 1=1
```

The `WHERE 1=1` idiom allows every subsequent filter to be appended with `AND` uniformly.

### Dynamic WHERE Clause Construction

Each non-`None` parameter appends a clause and a corresponding `QueryParameter` object.
The pattern is consistent across all scalar parameters:

```python
if sector:
    query += " AND sector = @sector"
    query_params.append(bigquery.ScalarQueryParameter("sector", "STRING", sector))
```

For range filters (min/max pairs), both bounds are independently optional:

```python
if rsi_14_min is not None:
    query += " AND rsi_14 >= @rsi_14_min"
    query_params.append(bigquery.ScalarQueryParameter("rsi_14_min", "FLOAT64", rsi_14_min))
if rsi_14_max is not None:
    query += " AND rsi_14 <= @rsi_14_max"
    query_params.append(bigquery.ScalarQueryParameter("rsi_14_max", "FLOAT64", rsi_14_max))
```

### Parameterized Query Safety

No user-provided value is ever interpolated into the SQL string. All values flow through
BigQuery's native parameterization system:

- **`ScalarQueryParameter`** — Used for single-value filters (`STRING`, `FLOAT64`, `INT64`)
- **`ArrayQueryParameter`** — Used exclusively for the `hmm_regime` list filter

This provides protection against SQL injection at the BigQuery engine level, not just at
the application level. The query text contains only `@placeholder` references; actual
values are bound separately via `QueryJobConfig.query_parameters`.

### UNNEST Pattern for Array Filtering

The `hmm_regime` parameter accepts a list of strings (e.g., `["BULL_QUIET", "BULL_VOLATILE"]`).
BigQuery does not support `IN (@array_param)` directly. Instead, the function uses the
`UNNEST` pattern:

```sql
AND hmm_regime IN UNNEST(@hmm_regime)
```

```python
query_params.append(bigquery.ArrayQueryParameter("hmm_regime", "STRING", hmm_regime))
```

`UNNEST` expands the array parameter into a set of rows that `IN` can match against. This
is the idiomatic BigQuery approach for parameterized multi-value filters.

---

## Result Ordering and Limit

Results are always ordered by `composite_score DESC` — the highest-conviction signals
appear first:

```sql
ORDER BY composite_score DESC LIMIT @limit
```

The limit is clamped to a safe range:

```python
safe_limit = min(max(1, limit), 100)
```

This prevents both zero-result queries and excessively large result sets that could slow
down agent response times or exceed token limits.

---

## Date Serialization

BigQuery returns `datetime.date` and `datetime.datetime` objects that are not JSON-serializable.
Before returning results to the Agent Engine, the function converts these to strings:

```python
for row in rows:
    if 'date' in row and row['date']:
         row['date'] = str(row['date'])
    if 'last_updated' in row and row['last_updated']:
         row['last_updated'] = str(row['last_updated'])
```

This ensures the response can be serialized back through the ADK tool interface without errors.

---

## Error Handling

The entire query execution is wrapped in a try/except block. On failure, the function
returns an error dictionary rather than raising an exception, which allows the agent to
gracefully inform the user:

```python
try:
    results = bq_client.query(query, job_config=job_config).result()
    # ... process rows ...
    return {"status": "success", "matches_found": len(rows), "results": rows}
except Exception as e:
    return {"status": "error", "error_message": str(e)}
```

---

## BigQuery Client Initialization

The client is initialized at module load time using Application Default Credentials:

```python
creds, project_id = google.auth.default()
project_id = project_id or os.getenv("GOOGLE_CLOUD_PROJECT")
bq_client = bigquery.Client(credentials=creds, project=project_id)
```

The `google.auth.default()` call works in three environments:
1. **Local development** — Uses credentials from `gcloud auth application-default login`
2. **Cloud Run** — Uses the service account attached to the Cloud Run service
3. **Vertex Agent Engine** — Uses the agent's service account

The `os.getenv("GOOGLE_CLOUD_PROJECT")` fallback handles cases where the default credentials
do not include a project ID (common in some local configurations).

---

## Response Structure

### Success Response

```json
{
  "status": "success",
  "matches_found": 8,
  "results": [
    {
      "ticker": "NVDA",
      "date": "2026-03-04",
      "sector": "Technology",
      "industry": "Semiconductors",
      "market_cap": 2800000000000.0,
      "close": 875.50,
      "rsi_14": 58.3,
      "macd_histogram": 1.24,
      "composite_score": 0.72,
      "signal_label": "STRONG_BUY",
      "hmm_regime": "BULL_QUIET",
      "bq_forecast_5d_pct": 0.031,
      "last_updated": "2026-03-04 21:00:00+00:00"
    }
  ]
}
```

### Error Response

```json
{
  "status": "error",
  "error_message": "403 Access Denied: Table amfe_data.latest_screening_master..."
}
```

---

## Example Tool Calls

### Momentum stocks in tech

```python
execute_screen(
    sector="Technology",
    rsi_14_min=50,
    rsi_14_max=68,
    sma_cross_20_50_min=0,
    composite_score_min=0.4,
    limit=10
)
```

### Oversold value plays

```python
execute_screen(
    rsi_14_max=35,
    pe_ratio_max=15,
    revenue_growth_yoy_min=0.05
)
```

### Bull regime stocks with strong forecast

```python
execute_screen(
    hmm_regime=["BULL_QUIET", "BULL_VOLATILE"],
    bq_forecast_5d_pct_min=0.02,
    composite_score_min=0.3,
    limit=15
)
```

### Strong sell signals (contrarian research)

```python
execute_screen(
    signal_label="STRONG_SELL",
    limit=20
)
```

---

## Related Documentation

- [Realtime Quote Tool](./realtime-quote.md) — The companion tool for single-ticker analysis
- [MCP Toolbox Configuration](./mcp-toolbox.md) — BigQuery tools exposed via tools.yaml
- [Environment Setup](./environment-setup.md) — Credential and project configuration
- [Architecture](../../architecture.md) — Full system design
