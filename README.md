# StonxAI

Agentic Multi-model Financial Engine — Stock Analysis, Screening & Q&A powered by BigQuery, ADK, and Vertex AI Agent Engine.

## Architecture

```
LAYER 1: BigQuery ── data + pre-computation (runs in background)
LAYER 2: ADK Agents ── natural language interface to Layer 1
```

Three modes:
- **Stock Analysis** — BUY/HOLD/SELL recommendations for individual tickers
- **Stock Screening** — Natural language → structured queries against 500+ tickers
- **Financial Q&A** — Educational answers + real-time data lookups

See [architecture.md](architecture.md) for the full system design.

## Setup

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Google Cloud project with BigQuery, Dataform, Cloud Run, and Vertex AI enabled
- API keys for Alpaca, FRED, and SEC EDGAR

### 1. Clone and install dependencies

```bash
git clone <repo-url> && cd stonks
uv sync
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

| Variable | Description | Where to get it |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | Your GCP project ID | [GCP Console](https://console.cloud.google.com/) |
| `GOOGLE_CLOUD_REGION` | GCP region (default: `us-central1`) | — |
| `ALPACA_API_KEY` | Alpaca market data API key | [Alpaca Dashboard](https://app.alpaca.markets/) |
| `ALPACA_SECRET_KEY` | Alpaca market data secret key | Same as above |
| `ALPACA_BASE_URL` | Alpaca API base URL | Use `https://paper-api.alpaca.markets` for paper trading |
| `FRED_API_KEY` | Federal Reserve Economic Data API key | [FRED API Keys](https://fred.stlouisfed.org/docs/api/api_key.html) |
| `SEC_EDGAR_USER_AGENT` | Required User-Agent for SEC EDGAR API | Format: `YourName your@email.com` |
| `BQ_DATASET` | BigQuery dataset name (default: `amfe_data`) | — |
| `RESEARCH_SERVICE_URL` | URL of the A2A research service | Set after deploying to Cloud Run |

### 3. Authenticate with Google Cloud

```bash
gcloud auth application-default login
gcloud config set project $GOOGLE_CLOUD_PROJECT
```

### 4. Create BigQuery dataset

```bash
bq mk --dataset $GOOGLE_CLOUD_PROJECT:amfe_data
```

### 5. Seed historical data

```bash
uv run python scripts/seed_historical.py
```

This backfills OHLCV and macro data from 2022 to present.

### 6. Run Dataform pipelines

Deploy the Dataform repo and run the DAG to compute technical signals, fundamental scores, and the screening master table.

### 7. Test agents locally

```bash
uv run adk web amfe_orchestrator/
```

## Project Structure

```
stonks/
├── amfe_orchestrator/        # ADK root orchestrator + 3 sub-agents
│   ├── __init__.py
│   └── agent.py
├── research_service/         # A2A research agent (Cloud Run)
│   ├── __init__.py
│   └── agent.py
├── ingest_job/               # Daily data ingestion (Cloud Run Job)
│   └── main.py
├── mcp_toolbox/              # BigQuery tools + custom API tools
│   ├── tools.yaml
│   ├── stock_api.py          # execute_screen (parameterized screening)
│   └── realtime_quote.py     # get_stock_profile (BQ + realtime quote)
├── dataform/                 # SQL transformation pipelines
│   ├── technical_signals.sqlx
│   ├── fundamental_scores.sqlx
│   ├── screening_master.sqlx
│   └── latest_screening_master.sqlx
├── scripts/
│   ├── seed_historical.py    # One-time historical data backfill
│   └── backtest.py           # Walk-forward evaluation
├── architecture.md
├── pyproject.toml
└── .env.example
```

## Deployment

### Research Service (Cloud Run)

```bash
adk deploy cloud_run \
  --project=$GOOGLE_CLOUD_PROJECT \
  --region=us-central1 \
  --service_name=amfe-research-service \
  research_service/
```

### Orchestrator (Vertex AI Agent Engine)

```bash
adk deploy agent_engine \
  --project=$GOOGLE_CLOUD_PROJECT \
  --region=us-central1 \
  --display_name="AMFE Orchestrator" \
  --staging_bucket="gs://amfe-staging" \
  amfe_orchestrator/
```

### Ingest Job (Cloud Run Job + Cloud Scheduler)

```bash
gcloud run jobs create amfe-ingest-job \
  --source=ingest_job/ \
  --region=us-central1

gcloud scheduler jobs create http amfe-daily-ingest \
  --schedule="30 16 * * 1-5" \
  --time-zone="America/New_York" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/amfe-ingest-job:run" \
  --oauth-service-account-email="${SA_EMAIL}"
```
