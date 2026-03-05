# StonxAI System Overview

## Purpose and Goals

StonxAI (internally codenamed AMFE -- Agentic Multi-model Financial Engine) is an AI-powered stock analysis platform that provides three core capabilities:

1. **Stock Analysis** -- Generate BUY/HOLD/SELL recommendations for individual tickers using pre-computed quantitative signals, real-time price data, and optional deep research.
2. **Stock Screening** -- Translate natural language queries (e.g., "find undervalued tech stocks with momentum") into structured queries against a pre-computed screening table.
3. **Financial Q&A** -- Answer educational and data-driven financial questions using a combination of LLM knowledge, web search, and BigQuery conversational analytics.

The system is designed for a science-fair-style demonstration and is built to be extensible toward a production-grade financial analytics platform.

---

## Two-Layer Architecture Principle

StonxAI follows a strict two-layer separation:

```
LAYER 1: BigQuery  (the brain)  -- data storage + pre-computation, runs in background
LAYER 2: ADK Agents (the voice) -- natural language interface to Layer 1
```

**The governing design rule:** Agents never compute data on the fly. Every screening metric (RSI, P/E ratio, momentum score, HMM regime) is pre-computed and stored in BigQuery before any agent touches it. Agents only query pre-computed results.

This separation provides:
- **Predictable latency** -- agent queries are simple lookups, not heavy computations
- **Auditability** -- every signal is stored and reproducible
- **Cost control** -- BigQuery slot usage happens on a schedule, not per-user-request
- **Testability** -- data pipelines can be validated independently of agent behavior

---

## Complete Data Flow

```
                        DATA SOURCES
                        ============
    Alpaca API          FRED API            SEC EDGAR
    (OHLCV bars)        (VIX, CPI,          (10-K, 10-Q
     ~50 tickers)        FEDFUNDS)           filing metadata)
         |                  |                     |
         v                  v                     v
  +---------------------------------------------------------+
  |        Cloud Run Job: ingest_job/main.py                |
  |        Scheduled: 4:30 PM ET daily via Cloud Scheduler  |
  |        Writes to BigQuery raw tables                    |
  +---------------------------------------------------------+
         |                  |                     |
         v                  v                     v
  +-------------+   +-----------------+   +---------------+
  | ohlcv_daily |   | macro_indicators|   | sec_filings   |
  | (partitioned|   | (VIX, CPI,     |   | (ticker,      |
  |  by date,   |   |  FEDFUNDS)     |   |  filing_date, |
  |  clustered  |   |                |   |  form_type,   |
  |  by ticker) |   |                |   |  filing_url)  |
  +------+------+   +-------+--------+   +-------+-------+
         |                  |                     |
         v                  v                     v
  +---------------------------------------------------------+
  |           Dataform DAG (triggered after ingestion)      |
  |                                                         |
  |   technical_signals.sqlx                                |
  |     RSI-14, RSI-2, MACD, Bollinger Bands, SMAs, ATR    |
  |         |                                               |
  |         v                                               |
  |   fundamental_scores.sqlx                               |
  |     P/E, P/B, P/S, debt/equity, revenue growth          |
  |         |                                               |
  |         v                                               |
  |   screening_master.sqlx                                 |
  |     Joins tech + fund + macro, computes composite_score |
  |     HMM regime classification, signal labels            |
  |         |                                               |
  |         v                                               |
  |   latest_screening_master (VIEW)                        |
  |     Points to MAX(date) in screening_master             |
  +---------------------------------------------------------+
                        |
                        v
  +---------------------------------------------------------+
  |           ADK Agent System (Vertex AI Agent Engine)     |
  |                                                         |
  |   AMFE Orchestrator (root agent, gemini-2.0-flash)      |
  |     |                                                   |
  |     +-- stock_analysis_agent  (MODE A: single-ticker)   |
  |     |     Uses: BQ Toolset, realtime_quote, forecast    |
  |     |     Writes to: agent_decisions (audit log)        |
  |     |                                                   |
  |     +-- screener_agent        (MODE C: multi-ticker)    |
  |     |     Uses: BQ Toolset, execute_screen              |
  |     |                                                   |
  |     +-- qa_agent              (MODE B: Q&A)             |
  |     |     Uses: BQ Toolset, google_search               |
  |     |                                                   |
  |     +-- research_proxy        (A2A Remote Agent)        |
  |           Calls: amfe-research-service on Cloud Run     |
  |           Uses: gemini-2.5-pro, Google Search, SEC      |
  +---------------------------------------------------------+
                        |
                        v
                  User Response
          (structured recommendation,
           screening results, or
           educational answer)
```

---

## Component Inventory

| Component | Type | Location | Description |
|-----------|------|----------|-------------|
| `ingest_job/main.py` | Cloud Run Job | Scheduled daily | Fetches raw data from Alpaca, FRED, and SEC EDGAR; writes to BigQuery raw tables; optionally triggers Dataform |
| `dataform/technical_signals.sqlx` | Dataform Pipeline | BigQuery | Computes RSI, MACD, Bollinger Bands, SMAs, ATR for every ticker/date |
| `dataform/fundamental_scores.sqlx` | Dataform Pipeline | BigQuery | Computes P/E, P/B, P/S, debt ratios, revenue growth from SEC filing data |
| `dataform/screening_master.sqlx` | Dataform Pipeline | BigQuery | Joins all signals, classifies HMM regime, computes composite score and signal labels |
| `dataform/latest_screening_master.sqlx` | Dataform View | BigQuery | View pointing to the most recent trading day in screening_master |
| `amfe_orchestrator/agent.py` | ADK Agent | Vertex AI Agent Engine | Root orchestrator with 3 sub-agents + A2A research proxy |
| `research_service/agent.py` | ADK Agent | Cloud Run Service | Deep research agent using gemini-2.5-pro, exposed via A2A protocol |
| `mcp_toolbox/tools.yaml` | MCP Config | Cloud Run Service | BigQuery toolset configuration for agent access |
| `mcp_toolbox/stock_api.py` | Python Tool | Cloud Run Service | Structured `execute_screen` tool with parameterized queries |
| `mcp_toolbox/realtime_quote.py` | Python Tool | Cloud Run Service | Real-time price fetcher for `get_stock_profile` |
| `scripts/seed_historical.py` | Script | Local/CI | One-time historical data backfill (2022-present) |
| `scripts/backtest.py` | Script | Local/CI | Walk-forward evaluation of composite score predictions |

---

## Technology Stack

| Technology | Version/Service | Role |
|------------|----------------|------|
| Python | 3.11+ | Ingest job, agent code, tooling |
| Google BigQuery | Managed | Data warehouse, pre-computation engine |
| Google Dataform | Managed | SQL transformation pipeline orchestration |
| Google Cloud Run | Managed | Ingest job (Job), research service (Service), MCP Toolbox (Service) |
| Google Cloud Scheduler | Managed | Triggers ingest job daily at 16:30 ET |
| Vertex AI Agent Engine | Managed | Hosts the ADK orchestrator with managed sessions |
| Google ADK (Agent Development Kit) | Latest | Agent framework for orchestrator + sub-agents |
| Gemini 2.0 Flash | Model | Primary model for all three sub-agents + orchestrator |
| Gemini 2.5 Pro | Model | Heavyweight model for deep research service |
| Alpaca Markets API | v2 | OHLCV price data (paper trading endpoint for dev) |
| FRED API | v1 | Macroeconomic indicators (VIX, CPI, FEDFUNDS) |
| SEC EDGAR | Public API | 10-K/10-Q filing metadata |
| MCP Toolbox for Databases | Latest | Structured BigQuery access for agents |
| `alpaca-py` | Latest | Python SDK for Alpaca API |
| `fredapi` | Latest | Python SDK for FRED API |
| `google-cloud-bigquery` | Latest | Python SDK for BigQuery |
| `google-cloud-dataform` | v1beta1 | Python SDK for triggering Dataform workflows |

---

## GCP Services and Their Roles

| GCP Service | Resource Name | Role |
|-------------|---------------|------|
| BigQuery | Dataset: `amfe_data` | Central data warehouse. Stores raw data, computed signals, screening table, and agent audit log. |
| Dataform | Repository: `amfe-pipelines` | Manages SQL transformation DAG. Compiles and executes `.sqlx` pipelines in dependency order. |
| Cloud Run Jobs | `amfe-ingest-job` | Serverless execution of daily data ingestion. Pay-per-execution model. |
| Cloud Run Services | `amfe-research-service` | Always-on A2A research agent. Scales to zero when idle. |
| Cloud Run Services | MCP Toolbox sidecar | Provides structured BigQuery tool access to agents. |
| Cloud Scheduler | Daily trigger | Invokes the ingest Cloud Run Job at 16:30 ET every trading day. |
| Vertex AI Agent Engine | `amfe-orchestrator` | Managed deployment of the ADK agent system with persistent sessions. |
| Eventarc (optional) | Dataform trigger | Can trigger Dataform execution after ingest job completion. |

---

## Design Principles and Trade-offs

### Why pre-compute everything?

Agents are expensive and slow at computation. A BigQuery SQL query computing RSI-14 over 3 years of data for 500 tickers takes seconds in Dataform but would take minutes if an agent tried to orchestrate it. Pre-computation means agent latency is bounded by a simple `SELECT ... WHERE ticker = 'NVDA' AND date = CURRENT_DATE()` query.

### Why not real-time ML?

Real-time ML inference (e.g., running a neural network on every agent call) introduces latency, cost, and failure modes. Instead, StonxAI uses BigQuery ML's `ML.FORECAST` (TimesFM) which runs as part of the scheduled Dataform pipeline. The forecast is pre-computed and stored in `screening_master`. This means the agent reads a forecast value, not runs a model.

The `bq_forecast_5d_pct` and `bq_forecast_30d_pct` columns are currently placeholder NULLs -- they will be populated once the BigQuery ML model is trained and integrated into the Dataform DAG.

### Why HMM regime classification in SQL, not a real HMM?

A true Hidden Markov Model requires iterative Bayesian inference (Viterbi algorithm, Baum-Welch). This is hard to express in pure SQL. Instead, the system uses a rule-based regime classifier that captures the same intuition:

- **BULL_QUIET**: S&P 500 above 200-day SMA + VIX below 20
- **BULL_VOLATILE**: S&P 500 above 200-day SMA + VIX above 20
- **BEAR_QUIET**: S&P 500 below 200-day SMA + VIX below 25
- **BEAR_VOLATILE**: S&P 500 below 200-day SMA + VIX above 25
- **SIDEWAYS**: fallback

This is a deliberate simplification. A real HMM could be added later as a BigQuery ML model or an external Python computation step.

### Why LEFT JOINs in screening_master?

Not every ticker will have fundamental data (SEC filings lag by weeks), and not every date will have macro data. LEFT JOINs ensure that technical signals are always present even when fundamentals or macro data are missing. The composite score formula uses `COALESCE(..., 0)` to handle NULLs gracefully.

### Why WRITE_TRUNCATE for daily ingestion?

The ingest job uses `WRITE_TRUNCATE` to overwrite the entire table on each run. This provides idempotency -- running the job twice produces the same result. The trade-off is that historical data must be maintained separately (via `seed_historical.py` with `WRITE_APPEND`). For the daily job, the 5-day fetch window ensures weekends and holidays are covered, and truncation prevents duplicate rows.

---

## Scalability Considerations

- **Ticker universe**: Currently 50 tickers. The architecture supports the full S&P 500 (~503 tickers) without changes. Alpaca batching (50 per request) and SEC rate limiting (10 req/sec) are already implemented.
- **BigQuery partitioning**: All time-series tables are partitioned by `date`, which means queries for a single day scan only one partition regardless of historical depth.
- **BigQuery clustering**: Tables are clustered by `ticker` (and `sector`/`signal_label` where applicable), making per-ticker lookups extremely efficient.
- **Dataform**: Runs incrementally. Adding more tickers increases Dataform execution time linearly, but BigQuery can handle this with slot autoscaling.
- **Agent Engine**: Vertex AI Agent Engine manages session state and scales agent instances automatically.
- **Research service**: Cloud Run scales to zero when idle, scales up on demand. The heavyweight gemini-2.5-pro model is only called conditionally (large intraday moves or user-requested deep dives).

---

## Security Model

- **No raw SQL from agents**: The `execute_screen` tool uses parameterized queries. The agent provides filter parameters (e.g., `{"sector": "Technology", "rsi_14_max": 70}`), and the backend constructs safe SQL. This prevents SQL injection.
- **BigQuery write mode**: The MCP Toolbox is configured with `WriteMode.ALLOWED` only for the `agent_decisions` audit table. Agents cannot modify raw data or computed tables.
- **API keys as environment variables**: Alpaca, FRED, and SEC EDGAR credentials are stored as environment variables (or Cloud Run secrets), never hardcoded.
- **SEC EDGAR User-Agent**: The SEC requires a User-Agent header identifying the application and contact email. This is configurable via `SEC_EDGAR_USER_AGENT`.
- **Vertex AI Agent Engine auth**: Managed by Google Cloud IAM. Sessions are isolated per user.
- **`ask_data_insights` fallback**: For ambiguous queries where SQL generation is uncertain, the agent falls back to BigQuery's Conversational Analytics API rather than attempting to generate its own SQL.

---

## Cross-References

- BigQuery schema details: [docs/architecture/bigquery-schema.md](./bigquery-schema.md)
- Ingestion pipeline: [docs/data-pipeline/ingestion.md](../data-pipeline/ingestion.md)
- Transformation pipeline: [docs/data-pipeline/transformations.md](../data-pipeline/transformations.md)
- Historical backfill: [docs/data-pipeline/seed-historical.md](../data-pipeline/seed-historical.md)
