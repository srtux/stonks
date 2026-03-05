# Backtesting Script — Walk-Forward Evaluation

> **Source:** [`/scripts/backtest.py`](../../scripts/backtest.py)
> **Data sources:** `amfe_data.agent_decisions` + `amfe_data.ohlcv_daily`
> **Output:** Formatted console tables with accuracy and return metrics

---

## Purpose

`backtest.py` evaluates the quality of agent decisions after the fact. For every BUY or
SELL decision the agent has logged to `agent_decisions`, the script looks up what actually
happened to the stock price over the following 5 trading days and computes whether the
agent was right.

This is a **walk-forward backtest** — it uses only data that was available at the time of
each decision (the agent made the decision on day T, and we measure the outcome on day
T+5). No look-ahead bias is introduced.

---

## How to Run

```bash
# Ensure environment is configured
export GOOGLE_CLOUD_PROJECT=amfe-project

# Run the backtest
python scripts/backtest.py
```

### Prerequisites

1. `GOOGLE_CLOUD_PROJECT` environment variable must be set
2. BigQuery credentials must be available (`gcloud auth application-default login`)
3. The `agent_decisions` table must have at least one row
4. The `ohlcv_daily` table must have price data covering the decision dates + 5 trading days

---

## Data Sources

### agent_decisions Table

```sql
SELECT
    decision_id,
    ticker,
    action AS signal_label,    -- BUY | SELL | HOLD
    agent_mode,                -- analysis | screening
    reasoning,
    timestamp AS decision_timestamp,
    confidence_score AS confidence
FROM `amfe_data.agent_decisions`
ORDER BY decision_timestamp
```

Each row represents a single decision the agent made. The `action` column (renamed to
`signal_label` in the query) indicates whether the agent recommended BUY, SELL, or HOLD.

### ohlcv_daily Table

```sql
SELECT
    ticker,
    date AS trade_date,
    close
FROM `amfe_data.ohlcv_daily`
ORDER BY ticker, timestamp
```

Daily closing prices for all tickers. Used to compute what actually happened after each
decision.

---

## Forward Return Calculation

### 5-Day Window

The script uses a fixed **5 trading day** forward window (`FORWARD_DAYS = 5`):

```
Decision Date (T)                    Exit Date (T+5)
      │                                    │
      ▼                                    ▼
 ┌────┬────┬────┬────┬────┬────┬────┬────┐
 │ Mon│Tue │Wed │Thu │Fri │ Mon│Tue │Wed │
 │ T  │T+1 │T+2 │T+3 │T+4 │T+5 │    │    │
 └────┴────┴────┴────┴────┴────┴────┴────┘
   ▲                              ▲
   entry_price                    exit_price
```

### Entry/Exit Matching Logic

```python
# Find the closest trading day on or after the decision date
dec_date = dec["decision_timestamp"].normalize()
mask_entry = group["trade_date"] >= dec_date
entry_idx = group.loc[mask_entry].index[0]
entry_price = group.loc[entry_idx, "close"]

# Forward exit: FORWARD_DAYS trading days later
exit_idx = entry_idx + FORWARD_DAYS
exit_price = group.loc[exit_idx, "close"]
```

Key behaviors:
- **Entry date:** The first trading day on or after the decision timestamp. If a decision
  is made on Saturday, the entry is Monday's close.
- **Exit date:** Exactly 5 trading days (rows in `ohlcv_daily`) after entry. This
  automatically skips weekends and holidays because the table only contains trading days.
- **Skipped decisions:** If there are fewer than 5 trading days of data after the entry,
  the decision is skipped (not enough data to evaluate).

### Forward Return Formula

```python
forward_return = (exit_price - entry_price) / entry_price
```

A positive return means the price went up; negative means it went down.

---

## Metrics Computed

### Hit Rate

A "hit" is defined as a correct directional call:

```python
df["hit"] = (
    (df["is_buy"] & (df["forward_return"] > 0))
    | (df["is_sell"] & (df["forward_return"] < 0))
)
```

| Signal | Forward Return | Hit? |
|--------|---------------|------|
| BUY | +2.3% | Yes |
| BUY | -1.5% | No |
| SELL | -3.1% | Yes |
| SELL | +0.8% | No |
| HOLD | (any) | Not evaluated |

HOLD decisions are excluded from hit rate calculation since there is no directional bet.

### Overall Metrics

| Metric | Description |
|--------|-------------|
| Total decisions evaluated | Number of decisions with sufficient forward data |
| BUY decisions | Count of BUY signals |
| SELL decisions | Count of SELL signals |
| BUY hit rate | % of BUY signals where price went up |
| Avg return on BUYs | Mean 5-day forward return for BUY signals |
| SELL hit rate | % of SELL signals where price went down |
| Avg return on SELLs | Mean 5-day forward return for SELL signals |

### Grouped Summaries

The script produces three grouped breakdowns:

#### By Agent Mode

Shows whether `analysis` mode (single-ticker deep dive) or `screening` mode (multi-ticker
scan) produces better decisions.

```
BY AGENT MODE
──────────────────────────────────────────────────────────────────────
agent_mode  count hit_rate avg_return median_return std_return
  analysis     45   62.2%     +1.34%        +0.98%      3.21%
 screening     23   56.5%     +0.87%        +0.45%      2.89%
```

#### By HMM Regime

Shows decision quality across different market regimes. Expect higher hit rates in
`BULL_QUIET` (trending, low volatility) and lower in `BEAR_VOLATILE` (chaotic).

```
BY HMM REGIME
──────────────────────────────────────────────────────────────────────
   hmm_regime  count hit_rate avg_return median_return std_return
   BULL_QUIET     28   67.9%     +1.89%        +1.45%      2.34%
BULL_VOLATILE     12   58.3%     +0.67%        +0.23%      4.12%
   BEAR_QUIET      8   50.0%     -0.34%        -0.12%      1.98%
BEAR_VOLATILE      5   40.0%     -1.23%        -0.89%      5.67%
     SIDEWAYS     15   53.3%     +0.12%        +0.05%      2.56%
```

#### By Signal Label

Shows performance by signal strength. Ideally, STRONG_BUY should have higher hit rates
and returns than BUY, and STRONG_SELL should have more negative returns than SELL.

```
BY SIGNAL LABEL
──────────────────────────────────────────────────────────────────────
signal_label  count hit_rate avg_return median_return std_return
  STRONG_BUY     10   70.0%     +2.45%        +1.89%      2.12%
         BUY     30   60.0%     +1.12%        +0.78%      2.89%
        HOLD     15      0%     +0.23%        +0.12%      1.45%
        SELL      8   62.5%     -0.89%        -0.67%      2.34%
 STRONG_SELL      5   80.0%     -2.12%        -1.78%      3.01%
```

---

## Output Format

The script prints formatted tables directly to the console:

```
Loading agent decisions from BigQuery...
  68 decisions loaded.
Loading OHLCV data from BigQuery...
  125000 price rows loaded.
Computing 5-day forward returns...
  63 decisions matched to price data.

======================================================================
OVERALL BACKTEST METRICS
======================================================================
  Total decisions evaluated : 63
  BUY decisions             : 40
  SELL decisions            : 13
  BUY hit rate              : 62.5%
  Avg return on BUYs        : +1.23%
  SELL hit rate              : 61.5%
  Avg return on SELLs       : -0.98%

──────────────────────────────────────────────────────────────────────
BY AGENT MODE
──────────────────────────────────────────────────────────────────────
[table output]
...
```

---

## Interpreting Results

### What "Good" Looks Like

| Metric | Poor | Acceptable | Good | Excellent |
|--------|------|------------|------|-----------|
| BUY hit rate | < 50% | 50-55% | 55-65% | > 65% |
| SELL hit rate | < 50% | 50-55% | 55-65% | > 65% |
| Avg BUY return | Negative | 0-0.5% | 0.5-2% | > 2% |
| Avg SELL return | Positive | 0 to -0.5% | -0.5 to -2% | < -2% |

### Key Things to Check

1. **BUY hit rate > 50%** — The agent picks winners more often than not
2. **SELL hit rate > 50%** — The agent correctly identifies declines
3. **STRONG_BUY outperforms BUY** — Signal strength is meaningful
4. **Regime awareness** — Better performance in trending regimes (BULL_QUIET) is expected;
   poor performance in BEAR_VOLATILE suggests the agent should be more conservative
5. **Analysis vs Screening** — If one mode significantly outperforms, it informs whether
   deep analysis adds value over screening signals alone

### Red Flags

- BUY hit rate below 50%: the agent is worse than random
- Average return on BUYs is negative: even when "right" directionally, the magnitude is wrong
- STRONG_BUY underperforms BUY: the confidence calibration is broken
- High standard deviation on returns: the agent is making high-variance bets

---

## Limitations

1. **5-day window only:** Does not capture longer-term accuracy (30-day, 90-day). The
   `bq_forecast_30d_pct` signal is not evaluated.
2. **No transaction costs:** Returns are gross, not net. Real trading would incur spreads,
   commissions, and slippage.
3. **No position sizing:** All decisions are treated equally regardless of confidence score.
   A 0.9 confidence BUY and a 0.5 confidence BUY are weighted the same.
4. **No benchmark comparison:** Does not compare against a simple buy-and-hold strategy
   or market index.
5. **Survivorship bias potential:** If tickers are removed from the screening universe,
   historical decisions on those tickers may not match to price data.
6. **HOLD is ignored:** HOLD decisions are not evaluated, but they represent a real
   opportunity cost (the agent chose not to act).
7. **Entry timing:** Uses the close price on the decision date, not the open of the next
   day, which is more realistic for implementable trades.

---

## Future Improvements

| Improvement | Description | Priority |
|-------------|-------------|----------|
| **Sharpe ratio** | Risk-adjusted return metric: `mean(return) / std(return) * sqrt(252/5)` | High |
| **Max drawdown** | Largest peak-to-trough decline across all BUY decisions | High |
| **Benchmark comparison** | Compare agent BUYs against SPY over the same periods | High |
| **Multiple horizons** | Evaluate at 1-day, 5-day, 10-day, 30-day windows | Medium |
| **Confidence weighting** | Weight returns by `confidence_score` to reward calibration | Medium |
| **Win/loss ratio** | Average winner size vs. average loser size | Medium |
| **Sector breakdown** | Performance by sector (are tech picks better than healthcare?) | Low |
| **Time series analysis** | Plot cumulative returns over time to detect model decay | Low |
| **Statistical significance** | Bootstrap confidence intervals on hit rate | Low |

---

## Related Documentation

- [Stock Screening API](../tools/stock-api.md) — How screening decisions are generated
- [Realtime Quote Tool](../tools/realtime-quote.md) — How analysis decisions are generated
- [Cloud Run Deployment](../deployment/cloud-run.md) — Where the ingest job populates ohlcv_daily
- [Architecture](../../architecture.md) — agent_decisions table schema
