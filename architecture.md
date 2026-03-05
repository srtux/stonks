# StonxAI Architecture

## Stock Analysis + Screening + Q&A on BigQuery, Cloud Run & Vertex Agent Engine

---

## The Governing Principle

The three capabilities you want map cleanly to two infrastructure layers:

```
LAYER 1: BigQuery (the brain — data + pre-computation, runs in background)
LAYER 2: ADK Agents (the voice — natural language interface to Layer 1)
```

The biggest mistake in building this kind of system is making agents do data work.
Agents should **query pre-computed results**, not compute on the fly. Every screening
metric (RSI, P/E ratio, momentum, HMM regime) should already be in a BigQuery table
before any agent ever touches it.

---

## Full System Architecture

```
═══════════════════════════════════════════════════════════════════════
 LAYER 1: BigQuery Data Platform (runs continuously in background)
═══════════════════════════════════════════════════════════════════════

 Raw Data Ingestion (Cloud Run Jobs — scheduled nightly via Cloud Scheduler)
 ┌──────────────────────────────────────────────────────────────────────┐
 │  ingest-job (Cloud Run Job, runs at market close ~4:30PM ET daily)   │
 │  • Alpaca API → amfe_data.ohlcv_daily (OHLCV for 500+ tickers)      │
 │  • FRED API   → amfe_data.macro_indicators (VIX, CPI, FEDFUNDS)     │
 │  • SEC EDGAR  → amfe_data.sec_filings (latest 10-K/10-Q metadata)   │
 └──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼ triggers (Eventarc or just schedule)
 BigQuery Scheduled Pipelines (Dataform DAG — runs after ingestion)
 ┌──────────────────────────────────────────────────────────────────────┐
 │  Pipeline 1: technical_signals.sqlx                                  │
 │  Computes per-ticker for every day:                                   │
 │  • RSI-14, RSI-2 (overbought/oversold)                               │
 │  • MACD signal and histogram                                         │
 │  • Bollinger Band position (% from upper/lower)                      │
 │  • 20/50/200-day SMA and distance from price                        │
 │  • ATR (volatility measure)                                          │
 │  Writes to: amfe_data.technical_signals (partitioned by date)       │
 │                                                                       │
 │  Pipeline 2: fundamental_scores.sqlx                                 │
 │  Computes per-ticker per-filing-period:                              │
 │  • P/E, P/B, P/S ratios (from SEC EDGAR + price data)               │
 │  • Revenue growth QoQ, YoY                                           │
 │  • Debt-to-equity, current ratio                                     │
 │  • Earnings surprise (actual vs consensus if available)              │
 │  Writes to: amfe_data.fundamental_scores                             │
 │                                                                       │
 │  Pipeline 3: screening_master.sqlx  ← THE KEY TABLE                 │
 │  Joins everything into one wide table:                               │
 │  SELECT ticker, date, sector, market_cap,                            │
 │         rsi_14, macd_signal, bb_pct, sma_cross_20_50,               │
 │         pe_ratio, pb_ratio, revenue_growth_yoy,                      │
 │         hmm_regime, hmm_confidence,                                  │
 │         bq_forecast_5d_pct,  ← BigQuery AI.FORECAST (TimesFM)       │
 │         composite_score      ← weighted signal -1 to 1              │
 │  FROM technical_signals JOIN fundamental_scores JOIN macro           │
 │  Writes to: amfe_data.screening_master (partitioned by date)        │
 │                                                                       │
 │  Pipeline 4: latest_screening_master (View)                           │
 │  Points to the most recent trading day:                               │
 │  SELECT * FROM amfe_data.screening_master                             │
 │  WHERE date = (SELECT MAX(date) FROM amfe_data.screening_master)      │
 └──────────────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════════
 LAYER 2: ADK Agent System
═══════════════════════════════════════════════════════════════════════

 User Query: natural language
        │
        ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │                  AMFE ORCHESTRATOR (Root LlmAgent)                  │
 │                  Deployed on Vertex AI Agent Engine                 │
 │                  Model: gemini-2.0-flash                            │
 │  Intent classification → routes to one of 3 modes:                 │
 │                                                                     │
 │  MODE A: "Analyze NVDA"      → Stock Analysis Agent (sub_agent)    │
 │  MODE B: "What is P/E ratio" → Q&A Agent (sub_agent)               │
 │  MODE C: "Screen for value"  → Screener Agent (sub_agent)          │
 └──────────┬──────────────────────────┬───────────────────────────────┘
            │                          │
   ┌────────▼────────┐    ┌────────────▼────────────────────────┐
   │  MODE A + C     │    │  MODE B: Q&A AGENT                  │
   │  Use BigQuery   │    │  Model: gemini-2.0-flash             │
   │  Toolset (MCP)  │    │  Tools:                             │
   │  + Python tools │    │  • google_search (Grounding API)    │
   └─────────────────┘    │  • ask_data_insights (BQ)           │
                          │  • RemoteA2aAgent → Research :8001  │
                          └──────────────────────────────────────┘

 ┌───────────────────────────────────────────────────────────────────┐
 │ MODE A: STOCK ANALYSIS AGENT                                      │
 │ Model: gemini-2.0-flash                                           │
 │ Instruction: "Analyze a single ticker for BUY/HOLD/SELL"          │
 │                                                                   │
 │ Tool call sequence (always in this order):                        │
 │ 1. get_stock_profile → Gets latest_screening_master row +         │
 │                        live real-time quote                       │
 │    (gets pre-computed signals + intraday reality in one shot)     │
 │ 2. bq_forecast  → 5-day TimesFM price forecast for ticker        │
 │ 3. [CONDITIONAL] If intraday move > 5% or user requests deep dive:│
 │    → Provide initial answer and offer to call RemoteA2aAgent      │
 │      (Research Service on Cloud Run) asynchronously               │
 │ 4. execute_sql → INSERT INTO amfe_data.agent_decisions            │
 │    (persist decision for backtest tracking)                       │
 │                                                                   │
 │ Output: {action, confidence, composite_score, key_factors}       │
 └───────────────────────────────────────────────────────────────────┘

 ┌───────────────────────────────────────────────────────────────────┐
 │ MODE C: SCREENER AGENT                                            │
 │ Model: gemini-2.0-flash                                           │
 │ Instruction: "Translate natural language screens into tool calls" │
 │                                                                   │
 │ Examples of natural language → Tool formulation:                  │
 │                                                                   │
 │ User: "Find undervalued tech stocks with momentum"                │
 │ Agent: execute_screen →                                           │
 │   { "sector": "Technology", "pe_ratio_max": 20,                   │
 │     "rsi_14_min": 40, "rsi_14_max": 65, "sma_cross_20_50_min": 0, │
 │     "composite_score_min": 0.3 }                                  │
 │                                                                   │
 │ Backend API safely constructs standard parameterized SQL against  │
 │ `latest_screening_master` View to avoid SQL injection risks.      │
 │                                                                   │
 │ User: "Stocks in bear regime near 52-week low"                    │
 │ Agent: execute_sql →                                              │
 │   SELECT ticker, hmm_regime, bb_pct, composite_score             │
 │   FROM amfe_data.screening_master                                 │
 │   WHERE date = CURRENT_DATE()                                     │
 │     AND hmm_regime IN ('BEAR_QUIET', 'BEAR_VOLATILE')            │
 │     AND bb_pct < 0.1   -- near lower Bollinger Band              │
 │   ORDER BY bb_pct ASC LIMIT 20                                    │
 │                                                                   │
 │ Tool: ask_data_insights (for ambiguous queries where SQL          │
 │       translation is uncertain — falls back to BQ conversational  │
 │       analytics which handles it natively)                        │
 └───────────────────────────────────────────────────────────────────┘

 ┌────────────────────────────────────────────────────────────────────┐
 │ A2A RESEARCH SERVICE (Cloud Run — separate service, always on)    │
 │ Exposed via to_a2a() on port 8001                                 │
 │ Model: gemini-2.5-pro (heavyweight — called conditionally)        │
 │ Tools:                                                            │
 │ • google_search (Grounding API — real-time news)                  │
 │ • fetch_sec_filing (SEC EDGAR fetcher)                            │
 │ • score_news_sentiment (Gemini-based sentiment scorer)            │
 │ Output: {fundamental_signal, bull_thesis, bear_thesis, risk_flags}│
 └────────────────────────────────────────────────────────────────────┘
```

---

## BigQuery Schema (The Most Important Part)

Getting this schema right means every agent query is a fast, cheap indexed lookup.

```sql
-- ────────────────────────────────────────────────
-- Core market data (raw, loaded by ingest job)
-- ────────────────────────────────────────────────
CREATE TABLE amfe_data.ohlcv_daily (
  ticker        STRING NOT NULL,
  date          DATE NOT NULL,
  open          FLOAT64,
  high          FLOAT64,
  low           FLOAT64,
  close         FLOAT64,
  volume        INT64,
  adj_close     FLOAT64
)
PARTITION BY date
CLUSTER BY ticker;  -- Clustering on ticker makes per-ticker queries ~10x faster

-- ────────────────────────────────────────────────
-- Pre-computed screening table (agent reads this)
-- ────────────────────────────────────────────────
CREATE TABLE amfe_data.screening_master (
  ticker              STRING NOT NULL,
  date                DATE NOT NULL,
  company_name        STRING,
  sector              STRING,
  industry            STRING,
  market_cap          FLOAT64,

  -- Price action
  close               FLOAT64,
  pct_change_1d       FLOAT64,
  pct_change_5d       FLOAT64,
  pct_change_30d      FLOAT64,
  week_52_high        FLOAT64,
  week_52_low         FLOAT64,
  pct_from_52w_high   FLOAT64,

  -- Technical signals
  rsi_14              FLOAT64,   -- 0-100, >70 overbought, <30 oversold
  rsi_2               FLOAT64,   -- Short-term mean reversion signal
  macd                FLOAT64,
  macd_signal         FLOAT64,
  macd_histogram      FLOAT64,
  bb_upper            FLOAT64,
  bb_lower            FLOAT64,
  bb_pct              FLOAT64,   -- 0=at lower band, 1=at upper band
  sma_20              FLOAT64,
  sma_50              FLOAT64,
  sma_200             FLOAT64,
  sma_cross_20_50     FLOAT64,   -- positive = bullish golden cross
  atr_14              FLOAT64,   -- volatility

  -- Fundamental scores (from SEC data)
  pe_ratio            FLOAT64,
  pb_ratio            FLOAT64,
  ps_ratio            FLOAT64,
  debt_to_equity      FLOAT64,
  current_ratio       FLOAT64,
  revenue_growth_yoy  FLOAT64,
  revenue_growth_qoq  FLOAT64,
  earnings_surprise   FLOAT64,   -- % beat/miss vs consensus

  -- Regime and forecast
  hmm_regime          STRING,    -- BULL_QUIET | BULL_VOLATILE | BEAR_QUIET | BEAR_VOLATILE | SIDEWAYS
  hmm_confidence      FLOAT64,
  bq_forecast_5d_pct  FLOAT64,   -- TimesFM 5-day forecast % change
  bq_forecast_30d_pct FLOAT64,

  -- Composite signal
  composite_score     FLOAT64,   -- -1.0 (strong sell) to 1.0 (strong buy)
  signal_label        STRING,    -- STRONG_BUY | BUY | HOLD | SELL | STRONG_SELL
  last_updated        TIMESTAMP
)
PARTITION BY date
CLUSTER BY ticker, sector, signal_label;

-- ────────────────────────────────────────────────
-- Active View (agents query this instead of raw master)
-- ────────────────────────────────────────────────
CREATE OR REPLACE VIEW amfe_data.latest_screening_master AS
SELECT *
FROM amfe_data.screening_master
WHERE date = (SELECT MAX(date) FROM amfe_data.screening_master);

-- ────────────────────────────────────────────────
-- Agent decisions (append-only audit log)
-- ────────────────────────────────────────────────
CREATE TABLE amfe_data.agent_decisions (
  decision_id         STRING NOT NULL,
  ticker              STRING,
  timestamp           TIMESTAMP,
  action              STRING,    -- BUY | SELL | HOLD
  confidence_score    FLOAT64,
  composite_score     FLOAT64,
  agent_mode          STRING,    -- analysis | screening
  quant_signal        FLOAT64,
  fundamental_signal  FLOAT64,
  research_used       BOOL,
  reasoning           STRING,
  session_id          STRING
)
PARTITION BY DATE(timestamp)
CLUSTER BY ticker, action;
```

---

## The composite_score Formula (Computed in Dataform/SQL)

This is the heart of the screening_master pipeline. Computed entirely in SQL:

```sql
-- technical_signals.sqlx (Dataform pipeline step 1)
WITH rsi_calc AS (
  SELECT ticker, date, close,
    100 - (100 / (1 + AVG(CASE WHEN daily_return > 0 THEN daily_return ELSE 0 END)
                    OVER (PARTITION BY ticker ORDER BY date ROWS 13 PRECEDING)
                / NULLIF(AVG(CASE WHEN daily_return < 0 THEN ABS(daily_return) ELSE 0 END)
                    OVER (PARTITION BY ticker ORDER BY date ROWS 13 PRECEDING), 0)))
    AS rsi_14
  FROM (
    SELECT ticker, date, close,
      (close - LAG(close) OVER (PARTITION BY ticker ORDER BY date)) / LAG(close) OVER (PARTITION BY ticker ORDER BY date)
    AS daily_return
    FROM amfe_data.ohlcv_daily
  )
)
-- [MACD, Bollinger Bands, SMA computed similarly...]

-- screening_master.sqlx (Dataform pipeline final step)
SELECT
  ticker, date, sector, market_cap, close,
  rsi_14, macd_histogram, bb_pct, sma_cross_20_50,
  pe_ratio, revenue_growth_yoy,
  hmm_regime, bq_forecast_5d_pct,

  -- Composite score: weighted sum, regime-adjusted
  (
    CASE hmm_regime
      WHEN 'BULL_QUIET'    THEN 0.40 * TANH(bq_forecast_5d_pct * 10)
                                + 0.25 * (50 - rsi_14) / 50
                                + 0.20 * SIGN(macd_histogram)
                                + 0.15 * SIGN(sma_cross_20_50)
      WHEN 'BEAR_VOLATILE' THEN 0.10 * TANH(bq_forecast_5d_pct * 10)
                                + 0.50 * (50 - rsi_14) / 50
                                + 0.25 * SIGN(macd_histogram)
                                + 0.15 * SIGN(sma_cross_20_50)
      ELSE                      0.30 * TANH(bq_forecast_5d_pct * 10)
                                + 0.30 * (50 - rsi_14) / 50
                                + 0.20 * SIGN(macd_histogram)
                                + 0.20 * SIGN(sma_cross_20_50)
    END
  ) AS composite_score,

  CASE
    WHEN composite_score > 0.6  THEN 'STRONG_BUY'
    WHEN composite_score > 0.2  THEN 'BUY'
    WHEN composite_score > -0.2 THEN 'HOLD'
    WHEN composite_score > -0.6 THEN 'SELL'
    ELSE 'STRONG_SELL'
  END AS signal_label

FROM technical_signals
JOIN fundamental_scores USING (ticker, date)
JOIN amfe_data.macro_indicators m ON m.date = technical_signals.date
```

---

## Deployment Map

```
Google Cloud Project: amfe-project
│
├── BigQuery Dataset: amfe_data
│   ├── ohlcv_daily            (raw, ~500 tickers, partitioned daily)
│   ├── macro_indicators       (raw, VIX/FRED)
│   ├── sec_filings            (raw, metadata only)
│   ├── technical_signals      (computed, Dataform step 1)
│   ├── fundamental_scores     (computed, Dataform step 2)
│   ├── screening_master       (computed, Dataform final — AGENT READS THIS)
│   └── agent_decisions        (append-only audit log)
│
├── Dataform Repository: amfe-pipelines
│   ├── technical_signals.sqlx
│   ├── fundamental_scores.sqlx
│   └── screening_master.sqlx
│   Schedule: daily at 17:00 ET (after market close)
│
├── Cloud Run Services:
│   ├── amfe-ingest-job         (Cloud Run JOB — triggered by Cloud Scheduler)
│   │   Runs at 16:30 ET daily
│   │   Fetches Alpaca + FRED + SEC → writes to BQ raw tables
│   │   Then triggers Dataform workflow execution
│   │
│   └── amfe-research-service   (Cloud Run SERVICE — always-on, A2A)
│       Port 8001, exposed via to_a2a(research_agent)
│       Called conditionally by orchestrator for deep dives
│       Uses gemini-2.5-pro + Google Search + SEC EDGAR
│
├── MCP Toolbox (Cloud Run SERVICE — sidecar to Agent Engine)
│   tools.yaml maps amfe-bq-toolset → amfe_data dataset
│   Pre-built tools: execute_sql, forecast, ask_data_insights, get_table_info
│
└── Vertex AI Agent Engine: amfe-orchestrator
    Deployed via: adk deploy agent_engine --display_name="AMFE Orchestrator"
    Includes: orchestrator + stock_analysis_agent + screener_agent + qa_agent
    Uses managed sessions (conversation history persists across turns)
    Connects to: MCP Toolbox (BQ) + research-service (A2A via RemoteA2aAgent)
```

---

## Agent Code Structure (What Claude Code Builds)

```
amfe/
├── amfe_orchestrator/          ← deployed to Vertex Agent Engine
│   ├── __init__.py
│   └── agent.py                ← defines root orchestrator + 3 sub-agents
│
├── research_service/           ← deployed to Cloud Run (always-on A2A)
│   ├── __init__.py
│   └── agent.py                ← defines research_agent + to_a2a() exposure
│
├── ingest_job/                 ← deployed to Cloud Run Jobs
│   └── main.py                 ← fetches data, writes BQ, triggers Dataform
│
├── mcp_toolbox/
│   ├── tools.yaml              ← BigQuery toolset config
│   ├── stock_api.py            ← structured backend for execute_screen
│   └── realtime_quote.py       ← fast realtime price fetcher
│
├── dataform/                   ← SQL transformation pipelines
│   ├── technical_signals.sqlx
│   ├── fundamental_scores.sqlx
│   └── screening_master.sqlx
│
└── scripts/
    ├── seed_historical.py      ← backfill 2022-2025 data
    └── backtest.py             ← walk-forward evaluation
```

---

## Agent Orchestrator (agent.py for Vertex Agent Engine)

```python
from google.adk.agents import LlmAgent
from google.adk.tools.bigquery import BigQueryToolset, BigQueryToolConfig, WriteMode
from google.adk.tools import google_search
import google.auth

# BigQuery toolset — reads from amfe_data, write mode BLOCKED for safety
creds, _ = google.auth.default()
bq_toolset = BigQueryToolset(
    bigquery_tool_config=BigQueryToolConfig(
        write_mode=WriteMode.ALLOWED  # needed to log decisions to agent_decisions
    )
)

# ─── MODE A: Stock Analysis Agent ────────────────────────────────────────────
stock_analysis_agent = LlmAgent(
    name="stock_analysis_agent",
    model="gemini-2.0-flash",
    description="Analyzes a single stock ticker for a BUY/HOLD/SELL recommendation "
                "using pre-computed signals and realtime data.",
    instruction="""You are a stock analyst for the AMFE system.
    Given a ticker symbol, ALWAYS follow these steps in order:
    
    STEP 1: Call get_stock_profile to fetch the latest batch signals and real-time quote.
    
    STEP 2: Call forecast to get the 5-day price outlook from TimesFM.
    
    STEP 3: Interpret the data using the `signal_label` from the batch signals.
      Ensure you also check the real-time intraday percentage. If the intraday move
      wildly contradicts the batch signal (e.g., > 5% down when batch is STRONG_BUY),
      flag this discrepancy heavily.
    
    STEP 4: If there is a major discrepancy, or if the user requested a deep dive,
      offer to trigger the asynchronous report generation.
    
    STEP 5: Log the decision via execute_sql INSERT INTO amfe_data.agent_decisions.
    
    Output a structured recommendation with: action, confidence_score, key_factors,
    regime_context, and a 2-3 sentence rationale.""",
    tools=[bq_toolset], # Assume get_stock_profile is also injected here
)

# ─── MODE B: Q&A Agent ───────────────────────────────────────────────────────
qa_agent = LlmAgent(
    name="qa_agent",
    model="gemini-2.0-flash",
    description="Answers broad financial questions using web search and BigQuery data.",
    instruction="""You are a financial educator for the AMFE system.
    For educational questions (what is P/E ratio, how does RSI work, explain DCF):
      → Answer directly from knowledge.
    
    For questions about specific stocks (what happened to NVDA today, latest earnings):
      → Call google_search to ground your answer in current information.
    
    For questions about patterns in the AMFE data (which sectors are in bull regimes,
    average RSI across S&P 500 today):
      → Call ask_data_insights with the question directly.
    
    For very deep company research requests:
      → Route to research_proxy for a comprehensive fundamental analysis.""",
    tools=[bq_toolset, google_search],
)

# ─── MODE C: Screener Agent ───────────────────────────────────────────────────
screener_agent = LlmAgent(
    name="screener_agent",
    model="gemini-2.0-flash",
    description="Translates natural language stock screening criteria into structured "
                "tool calls to find stocks meeting criteria safely.",
    instruction="""You are a stock screener for the AMFE system.
    You translate natural language screening criteria into structural queries via the 
    `execute_screen` tool.
    
    Available keys you can filter on in the tool:
    sector, industry, market_cap_min/max, rsi_14_min/max, macd_histogram_min/max,
    sma_cross_20_50_min/max, pe_ratio_min/max, revenue_growth_yoy_min,
    hmm_regime, composite_score_min, signal_label, bq_forecast_5d_pct_min
    
    EXAMPLES:
    "Find momentum stocks" →
      { "rsi_14_min": 50, "rsi_14_max": 68, "sma_cross_20_50_min": 0, "composite_score_min": 0.4 }
    
    "Oversold value plays" →
      { "rsi_14_max": 35, "pe_ratio_max": 15, "revenue_growth_yoy_min": 0.05 }
    
    "Tech stocks in bull regime with strong forecast" →
      { "sector": "Technology", "hmm_regime": ["BULL_QUIET", "BULL_VOLATILE"], "bq_forecast_5d_pct_min": 0.02 }
    
    After getting results, summarize: total matches found, top 5 tickers, 
    common characteristics of the screened list, and recommended next step 
    (e.g. 'run full analysis on AAPL').""",
    tools=[bq_toolset], # Assume execute_screen is injected alongside
)

# ─── A2A Research Proxy ───────────────────────────────────────────────────────
from google.adk.agents import RemoteA2aAgent
research_proxy = RemoteA2aAgent(
    name="research_proxy",
    description="Deep fundamental research: SEC filings + news analysis + web search. "
                "Call for borderline signals or user requests for deep company analysis.",
    agent_card_url="https://amfe-research-service-[hash]-uc.a.run.app/.well-known/agent-card.json",
)

# ─── Root Orchestrator ────────────────────────────────────────────────────────
root_agent = LlmAgent(
    name="amfe_orchestrator",
    model="gemini-2.0-flash",
    description="AMFE: Agentic Multi-model Financial Engine. Routes financial queries "
                "to specialized agents.",
    instruction="""You are AMFE, an AI-powered financial analysis system.
    
    Classify every user message into one of these modes and route accordingly:
    
    MODE A — STOCK ANALYSIS: User wants a recommendation on a specific ticker
      Keywords: "analyze", "should I buy/sell", "what do you think about [TICKER]",
                "recommendation for", "rate [TICKER]"
      Route to: stock_analysis_agent
    
    MODE B — FINANCIAL Q&A: User has a general or educational financial question
      Keywords: questions starting with "what is", "how does", "explain",
                "tell me about", current events questions about markets
      Route to: qa_agent
    
    MODE C — STOCK SCREENING: User wants to find stocks matching criteria
      Keywords: "find stocks", "screen for", "which stocks", "show me stocks that",
                "list stocks with"
      Route to: screener_agent
    
    After routing, synthesize the sub-agent's response into a clean, 
    well-formatted answer. Always include: which agent(s) were used and why.
    
    If the user's intent is ambiguous, ask ONE clarifying question.""",
    sub_agents=[stock_analysis_agent, qa_agent, screener_agent, research_proxy],
)
```

---

## Natural Language Screening: What's Possible

Once `screening_master` is populated, these queries all work out of the box:

| User Says | SQL Criteria Generated |
|---|---|
| "Find undervalued small caps" | `market_cap < 2e9 AND pe_ratio < 15 AND pb_ratio < 1.5` |
| "Momentum stocks not overbought" | `rsi_14 BETWEEN 50 AND 68 AND sma_cross_20_50 > 0` |
| "Dividend plays with low debt" | `debt_to_equity < 0.5 AND revenue_growth_yoy > 0` |
| "Stocks near 52-week high in bull regime" | `pct_from_52w_high > -0.05 AND hmm_regime LIKE 'BULL%'` |
| "High volatility names with bearish forecast" | `atr_14 > 3 AND bq_forecast_5d_pct < -0.02` |
| "Energy sector strong buys" | `sector='Energy' AND signal_label='STRONG_BUY'` |
| "Oversold with positive earnings surprise" | `rsi_14 < 35 AND earnings_surprise > 0.05` |
| "All tickers in BEAR_VOLATILE regime" | `hmm_regime='BEAR_VOLATILE' ORDER BY composite_score ASC` |

The `ask_data_insights` BigQuery tool handles even more ambiguous phrasing by using
BigQuery's Conversational Analytics API — so if the agent can't confidently write SQL,
it falls back to this tool which handles it natively.

---

## Cloud Run vs. Vertex Agent Engine: Decision Guide

| What | Where | Why |
|---|---|---|
| Ingest job (nightly) | **Cloud Run Job** | Pay per execution; only runs ~once/day; serverless |
| Research service (A2A) | **Cloud Run Service** | Always-on; needs its own port (8001); handles variable load; scales to zero when idle |
| MCP Toolbox | **Cloud Run Service** | Sidecar to agents; stateless; scales independently |
| Main ADK orchestrator | **Vertex AI Agent Engine** | Managed sessions (conversation history persists); built-in auth; `adk deploy agent_engine` in one command; Memory Bank for long-term user context |

The deploy command for Agent Engine is literally:
```bash
adk deploy agent_engine \
  --project=$PROJECT_ID \
  --region=us-central1 \
  --display_name="AMFE Orchestrator" \
  --staging_bucket="gs://amfe-staging" \
  amfe_orchestrator/
```

And for the Research service on Cloud Run:
```bash
adk deploy cloud_run \
  --project=$PROJECT_ID \
  --region=us-central1 \
  --service_name=amfe-research-service \
  research_service/
```

---

## Build Order for Claude Code

```
Phase 1: BigQuery Schema + Seed Data
  → Create all tables with correct partitioning/clustering
  → Backfill 2022-2025 data via scripts/seed_historical.py
  → Verify with manual BQ queries

Phase 2: Dataform Pipelines
  → Build technical_signals.sqlx (RSI, MACD, Bollinger, SMA)
  → Build fundamental_scores.sqlx (P/E, P/B from SEC)
  → Build screening_master.sqlx (join + composite_score formula)
  → Schedule in Dataform: daily at 17:00 ET
  → Verify: screening_master should have rows for CURRENT_DATE()

Phase 3: Cloud Run Ingest Job
  → alpaca_client.py + fred_client.py → write to BQ raw tables
  → Trigger Dataform workflow on completion
  → Deploy as Cloud Run Job + Cloud Scheduler trigger

Phase 4: ADK Agents & Tools (local adk web testing)
  → Build mcp_toolbox/stock_api.py (execute_screen structured tool)
  → Build mcp_toolbox/realtime_quote.py (get_stock_profile tool)
  → Build stock_analysis_agent.py (BQ toolset + realtime_quote)
  → Build screener_agent.py (BQ toolset + execute_screen)
  → Build qa_agent.py (BQ toolset + google_search)
  → Build root orchestrator
  → Test all three modes with adk web

Phase 5: A2A Research Service
  → Build research_agent.py (gemini-2.5-pro + sec + search)
  → Expose via to_a2a()
  → Deploy to Cloud Run
  → Update orchestrator with Cloud Run URL for RemoteA2aAgent
  → Integration test: trigger borderline analysis query

Phase 6: Production Deployment
  → adk deploy agent_engine (orchestrator)
  → Verify managed sessions persist
  → End-to-end test all three modes
```

---

## Science Fair Demo Script (3-mode live demo)

```
"Let me show you three things AMFE can do."

1. SCREENING (most visually impressive — shows NL→SQL in action):
   Type: "Find tech stocks in bull regime with strong momentum"
   Show: [screener_agent SQL trace] → results table of 8-12 tickers
   
2. ANALYSIS (most impressive for depth):
   Type: "Analyze NVDA"
   Show: [BQ tool calls, conditional A2A call to research service,
          terminal 2 lighting up, final BUY/HOLD/SELL recommendation]

3. Q&A (most accessible to non-technical judges):
   Type: "What is an HMM and why does it matter for trading?"
   Show: [qa_agent answers without any tool calls — Gemini's knowledge]
   Then: "Which sectors have the most stocks in bull regime right now?"
   Show: [ask_data_insights tool call → real BQ data answer]
```
