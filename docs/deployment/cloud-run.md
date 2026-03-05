# Cloud Run Deployment Guide

> **Project:** `amfe-project` (configurable via `GOOGLE_CLOUD_PROJECT`)
> **Region:** `us-central1` (configurable via `GOOGLE_CLOUD_REGION`)

---

## Services Inventory

StonxAI deploys three distinct workloads on Cloud Run:

| Service | Type | Purpose | Schedule | Port |
|---------|------|---------|----------|------|
| `amfe-ingest-job` | Cloud Run **Job** | Nightly data ingestion (Alpaca, FRED, SEC) | Daily at 16:30 ET via Cloud Scheduler | N/A (batch) |
| `amfe-research-service` | Cloud Run **Service** | A2A deep research agent (Gemini 2.5 Pro) | Always-on (scales to zero) | 8001 |
| MCP Toolbox | Cloud Run **Service** | BigQuery tool sidecar for ADK agents | Always-on | 5000 |

---

## 1. amfe-ingest-job (Cloud Run Job)

### Purpose

Runs daily after market close to fetch fresh data from three external sources and write it
to BigQuery raw tables. After ingestion completes, it triggers the Dataform workflow that
rebuilds `screening_master`.

### Data Flow

```
Cloud Scheduler (16:30 ET)
    │
    ▼
amfe-ingest-job
    ├── Alpaca API  → amfe_data.ohlcv_daily      (OHLCV for 500+ tickers)
    ├── FRED API    → amfe_data.macro_indicators  (VIX, CPI, FEDFUNDS)
    └── SEC EDGAR   → amfe_data.sec_filings       (10-K/10-Q metadata)
    │
    ▼ triggers
Dataform Workflow
    ├── technical_signals.sqlx
    ├── fundamental_scores.sqlx
    └── screening_master.sqlx
```

### Deployment Command

```bash
# Build and deploy the Cloud Run Job
gcloud run jobs deploy amfe-ingest-job \
  --source=./ingest_job/ \
  --project=${GOOGLE_CLOUD_PROJECT} \
  --region=us-central1 \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT},\
ALPACA_API_KEY=${ALPACA_API_KEY},\
ALPACA_SECRET_KEY=${ALPACA_SECRET_KEY},\
ALPACA_BASE_URL=https://paper-api.alpaca.markets,\
FRED_API_KEY=${FRED_API_KEY},\
SEC_EDGAR_USER_AGENT=${SEC_EDGAR_USER_AGENT},\
BQ_DATASET=amfe_data" \
  --service-account=amfe-ingest@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com \
  --memory=2Gi \
  --cpu=2 \
  --task-timeout=30m \
  --max-retries=2
```

### Cloud Scheduler Configuration

```bash
gcloud scheduler jobs create http amfe-daily-ingest \
  --schedule="30 16 * * 1-5" \
  --time-zone="America/New_York" \
  --http-method=POST \
  --uri="https://${GOOGLE_CLOUD_REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${GOOGLE_CLOUD_PROJECT}/jobs/amfe-ingest-job:run" \
  --oauth-service-account-email=amfe-scheduler@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com \
  --location=us-central1
```

| Cron Field | Value | Meaning |
|------------|-------|---------|
| Minute | `30` | At minute 30 |
| Hour | `16` | At 4 PM |
| Day of month | `*` | Every day |
| Month | `*` | Every month |
| Day of week | `1-5` | Monday through Friday |
| **Timezone** | `America/New_York` | Eastern Time (adjusts for DST) |

The job runs at 4:30 PM ET, approximately 30 minutes after US market close (4:00 PM ET),
allowing time for end-of-day data to settle.

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GOOGLE_CLOUD_PROJECT` | Yes | GCP project ID |
| `ALPACA_API_KEY` | Yes | Alpaca Markets API key |
| `ALPACA_SECRET_KEY` | Yes | Alpaca Markets secret key |
| `ALPACA_BASE_URL` | Yes | Alpaca endpoint (`paper-api` for testing, `api` for live) |
| `FRED_API_KEY` | Yes | Federal Reserve Economic Data API key |
| `SEC_EDGAR_USER_AGENT` | Yes | SEC EDGAR identification string (`"Name email@example.com"`) |
| `BQ_DATASET` | No | BigQuery dataset name (default: `amfe_data`) |

---

## 2. amfe-research-service (Cloud Run Service)

### Purpose

Hosts the heavyweight research agent (Gemini 2.5 Pro) that performs deep fundamental
analysis. Exposed via the Agent-to-Agent (A2A) protocol on port 8001. Called conditionally
by the orchestrator when:
- Intraday price move exceeds 5%
- User explicitly requests a deep dive
- Signal is borderline (composite_score near 0)

### Deployment Command

```bash
# Using ADK CLI (recommended)
adk deploy cloud_run \
  --project=${GOOGLE_CLOUD_PROJECT} \
  --region=us-central1 \
  --service_name=amfe-research-service \
  research_service/

# Or using gcloud directly
gcloud run deploy amfe-research-service \
  --source=./research_service/ \
  --project=${GOOGLE_CLOUD_PROJECT} \
  --region=us-central1 \
  --port=8001 \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT}" \
  --service-account=amfe-research@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com \
  --memory=2Gi \
  --cpu=2 \
  --min-instances=0 \
  --max-instances=5 \
  --concurrency=10 \
  --allow-unauthenticated
```

### A2A Protocol

The research service exposes its agent card at:
```
https://amfe-research-service-[hash]-uc.a.run.app/.well-known/agent-card.json
```

The orchestrator connects via `RemoteA2aAgent`:
```python
from google.adk.agents import RemoteA2aAgent

research_proxy = RemoteA2aAgent(
    name="research_proxy",
    description="Deep fundamental research...",
    agent_card_url="https://amfe-research-service-[hash]-uc.a.run.app/.well-known/agent-card.json",
)
```

### Tools Available to Research Agent

| Tool | Purpose |
|------|---------|
| `google_search` | Grounding API for real-time news and market data |
| `fetch_sec_filing` | SEC EDGAR API for 10-K/10-Q filing content |
| `score_news_sentiment` | Gemini-based sentiment analysis of news articles |

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GOOGLE_CLOUD_PROJECT` | Yes | GCP project ID |

### Scaling Configuration

| Setting | Value | Rationale |
|---------|-------|-----------|
| `min-instances` | `0` | Scale to zero when idle (cost optimization) |
| `max-instances` | `5` | Cap concurrent research jobs |
| `concurrency` | `10` | Multiple requests per instance (Gemini calls are I/O-bound) |
| `memory` | `2Gi` | Gemini 2.5 Pro responses can be large |
| `cpu` | `2` | Adequate for I/O-bound agent work |

---

## 3. MCP Toolbox (Cloud Run Service)

### Purpose

Runs the MCP Toolbox for Databases as a sidecar to the Agent Engine, providing BigQuery
tool access to all ADK agents. See [MCP Toolbox documentation](../tools/mcp-toolbox.md) for
tool configuration details.

### Deployment Command

```bash
gcloud run deploy mcp-toolbox \
  --image=us-docker.pkg.dev/database-toolbox/toolbox/toolbox:latest \
  --project=${GOOGLE_CLOUD_PROJECT} \
  --region=us-central1 \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT}" \
  --service-account=amfe-agent@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com \
  --port=5000 \
  --memory=512Mi \
  --cpu=1 \
  --min-instances=1 \
  --max-instances=3
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GOOGLE_CLOUD_PROJECT` | Yes | GCP project ID (used in `tools.yaml` substitution) |

---

## IAM Roles Required

Each service account needs specific BigQuery and Cloud Run permissions:

### amfe-ingest@project.iam.gserviceaccount.com (Ingest Job)

| Role | Resource | Purpose |
|------|----------|---------|
| `roles/bigquery.dataEditor` | `amfe_data` dataset | Write to raw tables |
| `roles/dataform.editor` | Dataform repository | Trigger workflow execution |
| `roles/run.invoker` | Self | Allow Cloud Scheduler to invoke |

### amfe-research@project.iam.gserviceaccount.com (Research Service)

| Role | Resource | Purpose |
|------|----------|---------|
| `roles/bigquery.dataViewer` | `amfe_data` dataset | Read screening data |
| `roles/aiplatform.user` | Project | Gemini API access |

### amfe-agent@project.iam.gserviceaccount.com (MCP Toolbox + Agent Engine)

| Role | Resource | Purpose |
|------|----------|---------|
| `roles/bigquery.dataViewer` | `amfe_data` dataset | Read all tables |
| `roles/bigquery.dataEditor` | `amfe_data.agent_decisions` table | Write decision logs |
| `roles/bigquery.jobUser` | Project | Execute queries |
| `roles/aiplatform.user` | Project | Gemini API access |
| `roles/run.invoker` | Research service | Call A2A endpoint |

### amfe-scheduler@project.iam.gserviceaccount.com (Cloud Scheduler)

| Role | Resource | Purpose |
|------|----------|---------|
| `roles/run.invoker` | Ingest job | Trigger job execution |

---

## Health Checks and Monitoring

### Cloud Run Service Health

Cloud Run provides built-in health checks. For custom health endpoints:

```python
# In research_service/agent.py
@app.route("/health")
def health():
    return {"status": "healthy"}, 200
```

Configure in Cloud Run:
```bash
gcloud run services update amfe-research-service \
  --startup-probe-path=/health \
  --startup-probe-period=10 \
  --liveness-probe-path=/health \
  --liveness-probe-period=30
```

### Monitoring Recommendations

| Metric | Alert Threshold | Action |
|--------|----------------|--------|
| Ingest job failure | Any failure | Check API key expiration, rate limits |
| Research service latency | p95 > 30s | Check Gemini API quotas |
| MCP Toolbox error rate | > 5% | Check BigQuery permissions, dataset availability |
| Ingest job duration | > 25 min | Check network, consider parallelization |
| Cloud Scheduler missed | Any miss | Verify scheduler service account permissions |

### Logging

All services log to Cloud Logging under the project. Filter by service:

```
resource.type="cloud_run_revision"
resource.labels.service_name="amfe-research-service"
```

For the ingest job:
```
resource.type="cloud_run_job"
resource.labels.job_name="amfe-ingest-job"
```

---

## Cost Optimization

### Scale-to-Zero Configuration

| Service | Min Instances | Cost When Idle |
|---------|---------------|----------------|
| `amfe-ingest-job` | N/A (job) | $0 — only billed per execution |
| `amfe-research-service` | 0 | $0 — scales to zero when not called |
| MCP Toolbox | 1 | ~$5-10/month — kept warm for agent responsiveness |

### Key Cost Drivers

1. **BigQuery** — Queries against `screening_master` are cheap (clustered, single partition).
   The ML.FORECAST calls are the most expensive BQ operation.
2. **Gemini 2.5 Pro** — The research service uses the most expensive model. Conditional
   invocation (only when needed) keeps costs controlled.
3. **Cloud Run** — With scale-to-zero, idle costs are near zero. The ingest job runs once
   per day for ~5-10 minutes.
4. **Cloud Scheduler** — Free tier covers the single daily trigger.

### Estimated Monthly Cost (Low Usage)

| Component | Estimated Cost |
|-----------|---------------|
| BigQuery storage + queries | $5-15 |
| Cloud Run (ingest job, ~5 min/day) | $1-3 |
| Cloud Run (research, scale-to-zero) | $0-10 |
| Cloud Run (MCP toolbox, 1 min instance) | $5-10 |
| Gemini API (agent calls) | $5-20 |
| Cloud Scheduler | $0 |
| **Total** | **$16-58/month** |

---

## Related Documentation

- [Vertex Agent Engine](./vertex-agent-engine.md) — Deploying the orchestrator
- [MCP Toolbox Configuration](../tools/mcp-toolbox.md) — tools.yaml reference
- [Environment Setup](../tools/environment-setup.md) — All environment variables
- [Architecture](../../architecture.md) — Full system design
