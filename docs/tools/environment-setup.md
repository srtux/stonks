# Environment and Configuration Setup

> **Source:** [`.env.example`](../../.env.example), [`pyproject.toml`](../../pyproject.toml)
> **Python version:** >= 3.11
> **Package manager:** uv (recommended) or pip

---

## Environment Variables

### Complete Reference

| Variable | Required | Default | Where to Get It | Used By |
|----------|----------|---------|-----------------|---------|
| `GOOGLE_CLOUD_PROJECT` | Yes | — | GCP Console > Project Settings | All services |
| `GOOGLE_CLOUD_REGION` | Yes | — | Choose based on data locality | Deployment scripts |
| `ALPACA_API_KEY` | Yes | — | [app.alpaca.markets](https://app.alpaca.markets/) > API Keys | Ingest job |
| `ALPACA_SECRET_KEY` | Yes | — | Same as above | Ingest job |
| `ALPACA_BASE_URL` | Yes | — | `https://paper-api.alpaca.markets` (paper) or `https://api.alpaca.markets` (live) | Ingest job |
| `FRED_API_KEY` | Yes | — | [fred.stlouisfed.org/docs/api/api_key.html](https://fred.stlouisfed.org/docs/api/api_key.html) | Ingest job |
| `SEC_EDGAR_USER_AGENT` | Yes | — | Format: `"YourName your@email.com"` (SEC requires identification) | Ingest job |
| `BQ_DATASET` | No | `amfe_data` | N/A — choose a name | All BQ queries |
| `RESEARCH_SERVICE_URL` | No | `http://localhost:8001` | Cloud Run service URL after deployment | Orchestrator |

### .env.example Walkthrough

```bash
# ─── Google Cloud ────────────────────────────────────────────────────
# Your GCP project ID — found in Cloud Console dashboard
GOOGLE_CLOUD_PROJECT=amfe-project

# Region for Cloud Run, Agent Engine, and BigQuery
# us-central1 is recommended for lowest latency to BigQuery
GOOGLE_CLOUD_REGION=us-central1

# ─── Alpaca API (Market Data) ────────────────────────────────────────
# Sign up at https://app.alpaca.markets/
# Use paper trading URL for development, live URL for production
ALPACA_API_KEY=your_alpaca_api_key
ALPACA_SECRET_KEY=your_alpaca_secret_key
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# ─── FRED API (Macroeconomic Data) ──────────────────────────────────
# Free API key from https://fred.stlouisfed.org/docs/api/api_key.html
# Provides VIX, CPI, Federal Funds Rate, etc.
FRED_API_KEY=your_fred_api_key

# ─── SEC EDGAR (Company Filings) ────────────────────────────────────
# SEC requires a User-Agent header with your name and email
# This is for rate-limit compliance, not authentication
SEC_EDGAR_USER_AGENT=YourName your@email.com

# ─── BigQuery ────────────────────────────────────────────────────────
# Dataset name in BigQuery — all tables live here
BQ_DATASET=amfe_data

# ─── Research Service ────────────────────────────────────────────────
# URL of the A2A research service on Cloud Run
# Use localhost for local development, Cloud Run URL for production
RESEARCH_SERVICE_URL=http://localhost:8001
```

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
# Edit .env with your actual credentials
```

---

## GCP Authentication

### Local Development

Use Application Default Credentials (ADC) for local development:

```bash
# Authenticate with your Google account
gcloud auth login

# Set up Application Default Credentials (used by all Google Cloud client libraries)
gcloud auth application-default login

# Set the default project
gcloud config set project ${GOOGLE_CLOUD_PROJECT}
```

After running `gcloud auth application-default login`, the credentials are stored at:
- **macOS/Linux:** `~/.config/gcloud/application_default_credentials.json`
- **Windows:** `%APPDATA%\gcloud\application_default_credentials.json`

All Google Cloud client libraries (`google-cloud-bigquery`, `google-auth`, etc.) will
automatically use these credentials when `google.auth.default()` is called.

### Production (Cloud Run / Agent Engine)

In production, authentication is handled by the attached service account. No credential
files or environment variables are needed — `google.auth.default()` automatically detects
the Cloud Run or Agent Engine environment and uses the service account.

### Verifying Authentication

```bash
# Check current authenticated account
gcloud auth list

# Check application default credentials
gcloud auth application-default print-access-token

# Test BigQuery access
bq query --project_id=${GOOGLE_CLOUD_PROJECT} "SELECT 1"
```

---

## Python Dependencies

### pyproject.toml Overview

```toml
[project]
name = "stonxai"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    # Google Cloud & ADK
    "google-adk>=0.5.0",              # Agent Development Kit
    "google-cloud-bigquery>=3.20.0",  # BigQuery client
    "google-cloud-dataform>=0.5.0",   # Dataform workflow triggers
    "google-auth>=2.28.0",            # Authentication
    "google-cloud-run>=0.10.0",       # Cloud Run management

    # Data ingestion
    "alpaca-py>=0.28.0",              # Alpaca Markets API
    "fredapi>=0.5.2",                 # FRED economic data
    "sec-edgar-downloader>=5.0.0",    # SEC filing downloads

    # Realtime quotes
    "yfinance>=0.2.36",              # Yahoo Finance (real-time prices)

    # Utilities
    "python-dotenv>=1.0.0",          # .env file loading
    "pandas>=2.2.0",                 # Data manipulation
    "numpy>=1.26.0",                 # Numerical operations
    "requests>=2.31.0",              # HTTP client
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",                 # Testing
    "ruff>=0.3.0",                   # Linting
]
```

### Installing with uv (Recommended)

```bash
# Install uv if not already installed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create virtual environment and install dependencies
uv sync

# Install with dev dependencies
uv sync --dev
```

### Installing with pip

```bash
# Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e .

# Install with dev dependencies
pip install -e ".[dev]"
```

---

## BigQuery Dataset Creation

Before running any agent or script, the BigQuery dataset and tables must exist:

```bash
# Create the dataset
bq mk --dataset \
  --location=US \
  --description="StonxAI - Agentic Multi-model Financial Engine" \
  ${GOOGLE_CLOUD_PROJECT}:amfe_data
```

### Create Core Tables

```sql
-- Run these in BigQuery Console or via bq query

-- Raw market data
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
CLUSTER BY ticker;

-- Screening master (populated by Dataform pipeline)
CREATE TABLE amfe_data.screening_master (
  ticker              STRING NOT NULL,
  date                DATE NOT NULL,
  company_name        STRING,
  sector              STRING,
  industry            STRING,
  market_cap          FLOAT64,
  close               FLOAT64,
  pct_change_1d       FLOAT64,
  pct_change_5d       FLOAT64,
  pct_change_30d      FLOAT64,
  week_52_high        FLOAT64,
  week_52_low         FLOAT64,
  pct_from_52w_high   FLOAT64,
  rsi_14              FLOAT64,
  rsi_2               FLOAT64,
  macd                FLOAT64,
  macd_signal         FLOAT64,
  macd_histogram      FLOAT64,
  bb_upper            FLOAT64,
  bb_lower            FLOAT64,
  bb_pct              FLOAT64,
  sma_20              FLOAT64,
  sma_50              FLOAT64,
  sma_200             FLOAT64,
  sma_cross_20_50     FLOAT64,
  atr_14              FLOAT64,
  pe_ratio            FLOAT64,
  pb_ratio            FLOAT64,
  ps_ratio            FLOAT64,
  debt_to_equity      FLOAT64,
  current_ratio       FLOAT64,
  revenue_growth_yoy  FLOAT64,
  revenue_growth_qoq  FLOAT64,
  earnings_surprise   FLOAT64,
  hmm_regime          STRING,
  hmm_confidence      FLOAT64,
  bq_forecast_5d_pct  FLOAT64,
  bq_forecast_30d_pct FLOAT64,
  composite_score     FLOAT64,
  signal_label        STRING,
  last_updated        TIMESTAMP
)
PARTITION BY date
CLUSTER BY ticker, sector, signal_label;

-- Latest screening view
CREATE OR REPLACE VIEW amfe_data.latest_screening_master AS
SELECT *
FROM amfe_data.screening_master
WHERE date = (SELECT MAX(date) FROM amfe_data.screening_master);

-- Agent decisions audit log
CREATE TABLE amfe_data.agent_decisions (
  decision_id         STRING NOT NULL,
  ticker              STRING,
  timestamp           TIMESTAMP,
  action              STRING,
  confidence_score    FLOAT64,
  composite_score     FLOAT64,
  agent_mode          STRING,
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

## Development Workflow

### Local Testing with adk web

The ADK CLI provides a web-based testing interface:

```bash
# Start the agent locally
adk web amfe_orchestrator/

# Opens browser at http://localhost:8000
# You can interact with all three agent modes
```

### Testing Individual Tools

```bash
# Test execute_screen directly
python -c "
from mcp_toolbox.stock_api import execute_screen
result = execute_screen(sector='Technology', rsi_14_max=65, limit=5)
print(result)
"

# Test get_stock_profile directly
python -c "
from mcp_toolbox.realtime_quote import get_stock_profile
result = get_stock_profile('NVDA')
print(result)
"
```

### Running the Backtest

```bash
python scripts/backtest.py
```

### Linting

```bash
# Using ruff (configured in pyproject.toml)
ruff check .
ruff format .
```

### Running Tests

```bash
pytest
```

---

## Troubleshooting Common Issues

### "Could not automatically determine credentials"

```
google.auth.exceptions.DefaultCredentialsError: Could not automatically
determine credentials.
```

**Fix:** Run `gcloud auth application-default login` to set up local credentials.

### "403 Access Denied: Table amfe_data.screening_master"

**Fix:** Ensure your account or service account has `roles/bigquery.dataViewer` on the
`amfe_data` dataset:

```bash
bq show --format=prettyjson ${GOOGLE_CLOUD_PROJECT}:amfe_data
```

### "404 Not found: Dataset amfe_data"

**Fix:** Create the dataset first. See [BigQuery Dataset Creation](#bigquery-dataset-creation).

### "ModuleNotFoundError: No module named 'google.adk'"

**Fix:** Install dependencies:

```bash
uv sync
# or
pip install -e .
```

### "yfinance: No data found for ticker XYZZ"

**Cause:** The ticker does not exist on Yahoo Finance, or the market is closed and no
historical data is available for the requested period.

**Fix:** Verify the ticker symbol is correct. Note that `get_stock_profile` handles this
gracefully and returns an error dict rather than crashing.

### "BigQuery query failed: Unrecognized name: composite_score"

**Cause:** The `screening_master` table has not been populated by the Dataform pipeline yet.

**Fix:** Run the ingest job and Dataform pipeline first, or seed historical data:

```bash
python scripts/seed_historical.py
```

### "Connection refused to localhost:8001"

**Cause:** The research service is not running locally.

**Fix:** Either start it locally:

```bash
adk web research_service/ --port=8001
```

Or update `RESEARCH_SERVICE_URL` in `.env` to point to the Cloud Run deployment.

### ADK web UI shows "No tools available"

**Cause:** The `tools.yaml` file is not being found, or `GOOGLE_CLOUD_PROJECT` is not set.

**Fix:**

```bash
export GOOGLE_CLOUD_PROJECT=amfe-project
adk web amfe_orchestrator/
```

### Cloud Run deployment fails with "permission denied"

**Fix:** Ensure you have the required roles:

```bash
# Check your current roles
gcloud projects get-iam-policy ${GOOGLE_CLOUD_PROJECT} \
  --flatten="bindings[].members" \
  --filter="bindings.members:$(gcloud auth list --format='value(account)')"
```

You need at minimum:
- `roles/run.admin` — Deploy Cloud Run services/jobs
- `roles/iam.serviceAccountUser` — Attach service accounts to services
- `roles/storage.admin` — Push container images

---

## Related Documentation

- [Cloud Run Deployment](../deployment/cloud-run.md) — Production deployment
- [Vertex Agent Engine](../deployment/vertex-agent-engine.md) — Agent Engine deployment
- [MCP Toolbox](./mcp-toolbox.md) — tools.yaml configuration
- [Architecture](../../architecture.md) — Full system design
