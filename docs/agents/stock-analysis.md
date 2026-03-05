# Stock Analysis Agent

> **Source files:**
> - `/amfe_orchestrator/agent.py` (lines 36-60)
> - `/mcp_toolbox/realtime_quote.py`

---

## Purpose

The stock analysis agent provides **single-ticker BUY/HOLD/SELL recommendations** by combining pre-computed quantitative signals from BigQuery with real-time market data. It is invoked as MODE A by the orchestrator when a user asks for a recommendation on a specific stock.

---

## Model: `gemini-2.0-flash`

Uses the same lightweight model as the orchestrator. The agent does not perform heavy reasoning -- it follows a rigid 5-step workflow and interprets pre-computed signals. The intelligence is in the data pipeline (BigQuery), not the LLM.

---

## 5-Step Workflow

The agent's instruction mandates a strict sequential workflow:

```
STEP 1: get_stock_profile
    |
    v
STEP 2: forecast (BigQuery TimesFM)
    |
    v
STEP 3: Interpret signals + check discrepancies
    |
    v
STEP 4: [CONDITIONAL] Offer deep dive if discrepancy or user request
    |
    v
STEP 5: Log decision via execute_sql INSERT
```

### Step 1: `get_stock_profile`

Fetches two data sources in a single call:

**Source:** `/mcp_toolbox/realtime_quote.py` (lines 11-80)

```python
def get_stock_profile(ticker: str) -> Dict[str, Any]:
```

**Returns a dict with:**

- `batch_signals` -- The full row from `latest_screening_master` for this ticker. Contains all pre-computed fields: `signal_label`, `composite_score`, `rsi_14`, `macd_histogram`, `hmm_regime`, `pe_ratio`, `bq_forecast_5d_pct`, and ~30 other columns.
- `realtime_quote` -- Live intraday data from Yahoo Finance (`yfinance`):
  - `current_price` (float, rounded to 2 decimals)
  - `open_price` (float, rounded to 2 decimals)
  - `intraday_pct_change` (float, 4 decimals, e.g., 0.0234 = 2.34%)
- `status` -- `"success"` or `"partial_failure"` if one source failed.

**Implementation details:**
- BigQuery query uses parameterized `@ticker` to prevent injection.
- Yahoo Finance call uses `stock.history(period="1d")` for minimal latency.
- If either source fails, the other is still returned (graceful degradation).

### Step 2: `forecast` (BigQuery AI.FORECAST)

Calls the BigQuery `forecast` tool (provided by `BigQueryToolset`) to get a 5-day price prediction from Google's TimesFM foundation model. This is a built-in BigQuery ML capability that runs `AI.FORECAST` on the `ohlcv_daily` time series.

Returns a forecasted 5-day percentage change and confidence interval.

### Step 3: Signal Interpretation

The agent interprets the combined data using these rules:

#### Primary Signal: `signal_label`

The `signal_label` from `batch_signals` is the primary recommendation driver. It is pre-computed in the `screening_master` Dataform pipeline:

| composite_score Range | signal_label |
|---|---|
| > 0.6 | `STRONG_BUY` |
| 0.2 to 0.6 | `BUY` |
| -0.2 to 0.2 | `HOLD` |
| -0.6 to -0.2 | `SELL` |
| < -0.6 | `STRONG_SELL` |

#### Intraday Discrepancy Detection

The agent compares the batch `signal_label` against `realtime_quote.intraday_pct_change`. If the intraday move **contradicts the batch signal by more than 5%**, the agent must **flag this discrepancy heavily** in its response.

Examples of discrepancies:
- `signal_label = "STRONG_BUY"` but intraday is -6% -- something broke since the batch ran.
- `signal_label = "SELL"` but intraday is +7% -- possible news catalyst overriding technical signals.

This is defined in the instruction (lines 47-49):

```
If the intraday move wildly contradicts the batch signal (e.g.,
> 5% down when batch is STRONG_BUY), flag this discrepancy heavily.
```

#### Regime Context Incorporation

The `hmm_regime` field (Hidden Markov Model state) provides market regime context:

| Regime | Meaning | Impact on Interpretation |
|---|---|---|
| `BULL_QUIET` | Trending up, low volatility | Forecast weight is highest (40%) |
| `BULL_VOLATILE` | Trending up, high volatility | Caution on position sizing |
| `BEAR_QUIET` | Trending down, low volatility | Mean-reversion signals weighted more |
| `BEAR_VOLATILE` | Trending down, high volatility | RSI weight highest (50%) |
| `SIDEWAYS` | No clear trend | Equal weighting across signals |

### Step 4: Deep Dive Trigger (Conditional)

The agent offers to trigger the `research_proxy` (A2A Research Service) when:

1. **Major discrepancy detected** -- Intraday reality contradicts batch signals significantly.
2. **User explicitly requests deep dive** -- e.g., "Give me a deep analysis of NVDA."
3. **Borderline signals** -- When `composite_score` is near the boundary between labels (e.g., 0.19 -- barely HOLD vs. BUY).

The research proxy runs on Cloud Run with `gemini-2.5-pro` and performs SEC filing analysis, news sentiment scoring, and web search. See [Research Service](./research-service.md).

### Step 5: Decision Logging

The agent logs every recommendation to `amfe_data.agent_decisions` via `execute_sql`:

```sql
INSERT INTO amfe_data.agent_decisions (
    decision_id, ticker, timestamp, action,
    confidence_score, composite_score, agent_mode,
    quant_signal, fundamental_signal, research_used,
    reasoning, session_id
) VALUES (...)
```

#### Decision Table Schema

| Field | Type | Description |
|---|---|---|
| `decision_id` | STRING | Unique ID for this decision |
| `ticker` | STRING | Stock symbol |
| `timestamp` | TIMESTAMP | When decision was made |
| `action` | STRING | `BUY`, `HOLD`, or `SELL` |
| `confidence_score` | FLOAT64 | Agent's confidence (0-1) |
| `composite_score` | FLOAT64 | Raw composite score from batch (-1 to 1) |
| `agent_mode` | STRING | `"analysis"` for this agent |
| `quant_signal` | FLOAT64 | Technical signal strength |
| `fundamental_signal` | FLOAT64 | Fundamental analysis score (if research used) |
| `research_used` | BOOL | Whether research_proxy was invoked |
| `reasoning` | STRING | Free-text rationale |
| `session_id` | STRING | Vertex AI session ID for audit trail |

This append-only audit log enables backtesting via `/scripts/backtest.py`.

---

## Output Format

The agent produces a structured recommendation with these fields:

```
action:           BUY | HOLD | SELL
confidence_score: 0.0 - 1.0
key_factors:      [list of the most influential signals]
regime_context:   current HMM regime and its implication
rationale:        2-3 sentence explanation
```

---

## Tool Dependencies

| Tool | Source | Purpose |
|---|---|---|
| `get_stock_profile` | `/mcp_toolbox/realtime_quote.py` | Batch signals + live quote |
| `forecast` | BigQueryToolset (built-in) | TimesFM 5-day prediction |
| `execute_sql` | BigQueryToolset (built-in) | INSERT into agent_decisions |
| `ask_data_insights` | BigQueryToolset (built-in) | Available but rarely used |

---

## Example Interaction Flows

### Normal Case

```
User: "Analyze AAPL"

Agent workflow:
1. get_stock_profile("AAPL")
   -> batch_signals: signal_label="BUY", composite_score=0.35, rsi_14=55,
      hmm_regime="BULL_QUIET", bq_forecast_5d_pct=0.018
   -> realtime_quote: current_price=198.50, intraday_pct_change=0.0082

2. forecast("AAPL")
   -> 5-day forecast: +1.8% with 70% confidence

3. Interpretation:
   - signal_label is BUY (composite 0.35)
   - Intraday +0.82% aligns with bullish batch signal (no discrepancy)
   - BULL_QUIET regime: forecast-weight is dominant, and forecast is positive
   - RSI 55 = neutral, not overbought

4. No discrepancy -> skip deep dive

5. Log decision:
   INSERT INTO agent_decisions: action=BUY, confidence=0.72

Output:
  action: BUY
  confidence_score: 0.72
  key_factors: [positive forecast, bullish regime, neutral RSI]
  regime_context: BULL_QUIET -- low volatility uptrend favors momentum
  rationale: "AAPL shows a BUY signal with composite score 0.35 in a
  BULL_QUIET regime. The 5-day forecast is +1.8% and intraday action
  confirms the bullish lean. RSI at 55 leaves room for upside."
```

### Discrepancy Case

```
User: "Analyze TSLA"

Agent workflow:
1. get_stock_profile("TSLA")
   -> batch_signals: signal_label="STRONG_BUY", composite_score=0.72
   -> realtime_quote: intraday_pct_change=-0.068 (-6.8%)

2. forecast("TSLA")
   -> 5-day forecast: +3.2%

3. Interpretation:
   - STRONG_BUY batch signal BUT -6.8% intraday drop
   - DISCREPANCY FLAGGED: >5% contradiction
   - Something happened after batch signals were computed

4. Offer deep dive:
   "The batch signals show STRONG_BUY but TSLA is down 6.8% today.
    This suggests a major intraday event. Would you like me to run
    a deep-dive research report to understand what happened?"

5. Log decision:
   action=HOLD, confidence=0.35 (lowered due to discrepancy)
```

### Deep Dive Case

```
User: "Give me a deep analysis of MSFT"

Agent workflow:
1-3. Normal analysis steps

4. User explicitly requested deep dive -> trigger research_proxy
   research_proxy returns:
   {
     fundamental_signal: "BULLISH",
     bull_thesis: "Cloud revenue growing 28% YoY...",
     bear_thesis: "AI capex may pressure margins...",
     risk_flags: ["Antitrust regulatory risk"]
   }

5. Log decision with research_used=True

Output includes both quantitative signals AND fundamental analysis.
```

---

## Sequence Diagram: Full Analysis Flow

```
User        Orchestrator    StockAnalysis    get_stock_profile    BQ Forecast    BQ execute_sql
 |               |               |                 |                  |               |
 |--"Analyze X"->|               |                 |                  |               |
 |               |--delegate---->|                 |                  |               |
 |               |               |--call---------->|                  |               |
 |               |               |                 |--BQ query------->|               |
 |               |               |                 |<--batch_signals--|               |
 |               |               |                 |--yfinance------->|               |
 |               |               |                 |<--realtime_quote-|               |
 |               |               |<--profile-------|                  |               |
 |               |               |                 |                  |               |
 |               |               |--forecast--------------------------->              |
 |               |               |<--5d_prediction--------------------|               |
 |               |               |                 |                  |               |
 |               |               |  [interpret signals]               |               |
 |               |               |  [check discrepancy]               |               |
 |               |               |                 |                  |               |
 |               |               |--INSERT decision------------------------------------->
 |               |               |<--logged-------------------------------------------|
 |               |               |                 |                  |               |
 |               |<--recommendation|               |                  |               |
 |<--synthesized-|               |                 |                  |               |
```

---

## Cross-References

- [Orchestrator](./orchestrator.md) -- Parent agent that routes to this agent
- [Research Service](./research-service.md) -- Deep-dive agent triggered from Step 4
- [Screener Agent](./screener.md) -- Shares the same BigQuery data source
- [Q&A Agent](./qa-agent.md) -- Alternative routing path from orchestrator
