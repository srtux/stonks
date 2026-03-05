# Screener Agent

> **Source files:**
> - `/amfe_orchestrator/agent.py` (lines 90-110)
> - `/mcp_toolbox/stock_api.py`

---

## Purpose

The screener agent translates **natural language stock screening criteria** into structured, parameterized queries against the BigQuery `latest_screening_master` view. It is invoked as MODE C by the orchestrator when a user wants to find stocks matching specific criteria.

The key design principle: the LLM maps human language to tool parameters, but **never generates raw SQL** for screening queries. Instead, it calls the `execute_screen` function which constructs safe parameterized SQL server-side.

---

## Model: `gemini-2.0-flash`

Same lightweight model as the orchestrator. The screener's task is essentially structured extraction -- mapping natural language concepts to a fixed set of numeric and categorical parameters. This does not require advanced reasoning.

---

## How `execute_screen` Works

**Source:** `/mcp_toolbox/stock_api.py` (lines 14-156)

### Architecture

```
User: "Find momentum tech stocks"
         |
         v
+------------------+
| Screener Agent   |  LLM maps NL -> parameters
| (gemini-2.0-flash)|
+------------------+
         |
         v
execute_screen(
    sector="Technology",
    rsi_14_min=50,
    rsi_14_max=68,
    sma_cross_20_50_min=0,
    composite_score_min=0.4
)
         |
         v
+------------------+
| BigQuery Client  |  Builds parameterized SQL
| (server-side)    |  No user input in SQL string
+------------------+
         |
         v
latest_screening_master VIEW
```

### Parameterized Query Construction

The function starts with a base query:

```python
query = "SELECT * FROM `amfe_data.latest_screening_master` WHERE 1=1"
```

Then dynamically appends `AND` clauses using BigQuery `@parameter` syntax:

```python
if sector:
    query += " AND sector = @sector"
    query_params.append(bigquery.ScalarQueryParameter("sector", "STRING", sector))
```

Every filter uses `bigquery.ScalarQueryParameter` or `bigquery.ArrayQueryParameter` -- values are never interpolated into the SQL string. Results are ordered by `composite_score DESC` and capped at the `limit` parameter (default 20, max 100).

### Return Format

```python
{
    "status": "success",
    "matches_found": 12,
    "results": [
        {
            "ticker": "NVDA",
            "sector": "Technology",
            "composite_score": 0.78,
            "signal_label": "STRONG_BUY",
            "rsi_14": 62.3,
            # ... all columns from latest_screening_master
        },
        # ... up to limit results
    ]
}
```

On error:

```python
{
    "status": "error",
    "error_message": "..."
}
```

---

## Filterable Parameters

All parameters are optional. The agent selects which to include based on the user's natural language query.

| Parameter | Type | Description | Example Value |
|---|---|---|---|
| `sector` | `str` | GICS sector name | `"Technology"`, `"Healthcare"`, `"Energy"` |
| `industry` | `str` | GICS industry name | `"Software - Infrastructure"`, `"Semiconductors"` |
| `market_cap_min` | `float` | Minimum market cap in USD | `2e9` (2 billion) |
| `market_cap_max` | `float` | Maximum market cap in USD | `10e9` (10 billion) |
| `rsi_14_min` | `float` | Minimum 14-day RSI (0-100) | `30` (oversold threshold) |
| `rsi_14_max` | `float` | Maximum 14-day RSI (0-100) | `70` (overbought threshold) |
| `macd_histogram_min` | `float` | Minimum MACD histogram value | `0` (positive = bullish) |
| `macd_histogram_max` | `float` | Maximum MACD histogram value | `-0.5` |
| `sma_cross_20_50_min` | `float` | Min distance between 20 and 50 SMA. Positive = golden cross (bullish) | `0` |
| `sma_cross_20_50_max` | `float` | Max distance between 20 and 50 SMA | `5.0` |
| `pe_ratio_min` | `float` | Minimum P/E ratio | `5` |
| `pe_ratio_max` | `float` | Maximum P/E ratio | `20` (value territory) |
| `revenue_growth_yoy_min` | `float` | Minimum year-over-year revenue growth (decimal) | `0.05` (5% growth) |
| `hmm_regime` | `list[str]` | List of acceptable HMM regimes | `["BULL_QUIET", "BULL_VOLATILE"]` |
| `composite_score_min` | `float` | Minimum composite quant score (-1 to 1) | `0.3` |
| `signal_label` | `str` | Exact signal label match | `"STRONG_BUY"` |
| `bq_forecast_5d_pct_min` | `float` | Minimum 5-day forecasted % change | `0.02` (2% upside) |
| `limit` | `int` | Max results (default 20, max 100) | `10` |

---

## Natural Language to Parameter Mapping Examples

The screener agent's instruction (lines 94-106) includes example mappings. Below is the full set from the architecture and instruction:

### 1. "Find undervalued small caps"

```python
execute_screen(
    market_cap_max=2e9,       # small cap = under $2B
    pe_ratio_max=15,          # undervalued = low P/E
    pb_ratio_max=1.5          # (handled via execute_sql fallback if needed)
)
```

### 2. "Momentum stocks not overbought"

```python
execute_screen(
    rsi_14_min=50,            # has upward momentum
    rsi_14_max=68,            # but not yet overbought (< 70)
    sma_cross_20_50_min=0,    # golden cross (20 SMA above 50 SMA)
    composite_score_min=0.4   # reasonably strong composite signal
)
```

### 3. "Tech stocks in bull regime with strong forecast"

```python
execute_screen(
    sector="Technology",
    hmm_regime=["BULL_QUIET", "BULL_VOLATILE"],
    bq_forecast_5d_pct_min=0.02   # at least 2% upside forecasted
)
```

### 4. "Find undervalued tech stocks with momentum"

```python
execute_screen(
    sector="Technology",
    pe_ratio_max=20,
    rsi_14_min=40,
    rsi_14_max=65,
    sma_cross_20_50_min=0,
    composite_score_min=0.3
)
```

### 5. "Oversold value plays"

```python
execute_screen(
    rsi_14_max=35,                # oversold territory
    pe_ratio_max=15,              # value pricing
    revenue_growth_yoy_min=0.05   # still growing (not a value trap)
)
```

### 6. "Energy sector strong buys"

```python
execute_screen(
    sector="Energy",
    signal_label="STRONG_BUY"
)
```

### 7. "High volatility names with bearish forecast"

This query may require `execute_sql` fallback since `atr_14` is not a parameter of `execute_screen`. The agent can use the BigQuery toolset's `execute_sql` tool for non-standard filters:

```sql
SELECT ticker, atr_14, bq_forecast_5d_pct, composite_score
FROM amfe_data.latest_screening_master
WHERE atr_14 > 3 AND bq_forecast_5d_pct < -0.02
ORDER BY bq_forecast_5d_pct ASC LIMIT 20
```

### 8. "Oversold with positive earnings surprise"

```python
# RSI filter via execute_screen, but earnings_surprise requires execute_sql:
```

```sql
SELECT ticker, rsi_14, earnings_surprise, composite_score
FROM amfe_data.latest_screening_master
WHERE rsi_14 < 35 AND earnings_surprise > 0.05
ORDER BY earnings_surprise DESC LIMIT 20
```

**Note:** Not all `screening_master` columns have corresponding `execute_screen` parameters. For columns like `atr_14`, `bb_pct`, `debt_to_equity`, `pct_from_52w_high`, and `earnings_surprise`, the agent can fall back to `execute_sql` from the BigQuery toolset, or use `ask_data_insights`.

---

## Result Summarization Format

After receiving results from `execute_screen`, the agent is instructed (lines 103-106) to provide:

1. **Total matches found** -- How many stocks passed the filter.
2. **Top 5 tickers** -- Ranked by composite_score (default ordering).
3. **Common characteristics** -- Patterns across the screened list (e.g., "7 of 12 are in BULL_QUIET regime", "average RSI is 58").
4. **Recommended next step** -- Actionable follow-up, typically suggesting full analysis: `"Run full analysis on AAPL for a detailed recommendation."`

Example output:

```
Found 12 stocks matching your criteria.

Top 5:
1. NVDA - composite: 0.78, STRONG_BUY, RSI: 62
2. AVGO - composite: 0.65, BUY, RSI: 58
3. MSFT - composite: 0.52, BUY, RSI: 54
4. AAPL - composite: 0.48, BUY, RSI: 51
5. CRM  - composite: 0.44, BUY, RSI: 56

Common characteristics: All 12 are in BULL_QUIET regime with positive
SMA crossovers. Average P/E is 28, indicating growth-oriented names.

Next step: Run "Analyze NVDA" for a full BUY/HOLD/SELL recommendation
with real-time data.
```

---

## Fallback: `ask_data_insights`

When the user's screening query is too ambiguous for the agent to confidently map to `execute_screen` parameters, it falls back to BigQuery's Conversational Analytics API via `ask_data_insights`.

This tool accepts a natural language question and lets BigQuery's built-in NL-to-SQL engine handle the translation. It is useful for:

- Vague queries: `"Which sectors are doing well?"`
- Complex multi-table queries the agent cannot express with `execute_screen` alone.
- Statistical questions: `"What's the average RSI across all tech stocks?"`

The `ask_data_insights` tool is available because the `bq_toolset` is included in the screener's tools list (line 108).

---

## Security Model

### Why Parameterized Queries Matter

The `execute_screen` function is the primary defense against SQL injection:

1. **No string interpolation** -- User input never appears in the SQL string. All values are bound via `bigquery.ScalarQueryParameter` or `bigquery.ArrayQueryParameter`.
2. **Fixed schema** -- The function only allows filtering on a predefined set of columns. An LLM cannot be prompt-injected into adding arbitrary `WHERE` clauses.
3. **View-based access** -- Queries always target `latest_screening_master` (a view), not raw tables. The view enforces `WHERE date = MAX(date)`, preventing historical data scraping.
4. **Limit enforcement** -- Results are capped at 100 rows (`safe_limit = min(max(1, limit), 100)`) on line 130.
5. **Read-only for screening** -- Although the `bq_toolset` has `WriteMode.ALLOWED` (needed by the stock analysis agent for decision logging), the `execute_screen` function itself only performs `SELECT` queries.

### Attack Vector Mitigation

| Attack | Mitigation |
|---|---|
| SQL injection via sector name | Parameterized `@sector` binding |
| Requesting all rows | `LIMIT @limit` capped at 100 |
| Accessing other tables | Function hardcodes `latest_screening_master` |
| LLM prompt injection to modify query | Function signature is the contract; LLM can only set defined parameters |

---

## Sequence Diagram: Screening Flow

```
User           Orchestrator      Screener         execute_screen       BigQuery
 |                  |                |                  |                  |
 |--"Find value"--->|                |                  |                  |
 |                  |--MODE C------->|                  |                  |
 |                  |                |                  |                  |
 |                  |                |--map NL to params |                  |
 |                  |                |                  |                  |
 |                  |                |--call----------->|                  |
 |                  |                |                  |--parameterized-->|
 |                  |                |                  |  SQL query       |
 |                  |                |                  |<--result rows----|
 |                  |                |<--{status, rows}-|                  |
 |                  |                |                  |                  |
 |                  |                |--summarize results                  |
 |                  |                |  (top 5, patterns, next step)       |
 |                  |                |                  |                  |
 |                  |<--summary------|                  |                  |
 |<--formatted------|                |                  |                  |
```

---

## Cross-References

- [Orchestrator](./orchestrator.md) -- Parent agent that routes MODE C here
- [Stock Analysis Agent](./stock-analysis.md) -- Suggested as follow-up ("Analyze NVDA")
- [Q&A Agent](./qa-agent.md) -- Alternative routing path; also uses `ask_data_insights`
- [Research Service](./research-service.md) -- Not directly called from screener
