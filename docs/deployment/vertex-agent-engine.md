# Vertex AI Agent Engine Deployment Guide

> **Target:** AMFE Orchestrator (root agent + 3 sub-agents)
> **Model:** Gemini 2.0 Flash
> **Region:** us-central1

---

## What Agent Engine Provides

Vertex AI Agent Engine is a managed runtime for ADK agents that handles infrastructure
concerns the agents themselves should not have to deal with:

| Capability | Description |
|------------|-------------|
| **Managed Sessions** | Conversation history persists across turns without custom database code. Each user session maintains state automatically. |
| **Authentication** | Service account management, OAuth token refresh, and API key handling are built in. |
| **Auto-scaling** | Scales agent instances up/down based on request load with no configuration. |
| **Memory Bank** | Long-term user context storage beyond session boundaries — remembers user preferences, past analyses, and portfolio context across separate conversations. |
| **Monitoring** | Built-in request logging, latency tracking, and error reporting in Cloud Console. |
| **Versioning** | Deploy new agent versions without downtime; rollback to previous versions. |

---

## Deployment Command

The primary deployment method uses the ADK CLI:

```bash
adk deploy agent_engine \
  --project=${GOOGLE_CLOUD_PROJECT} \
  --region=us-central1 \
  --display_name="AMFE Orchestrator" \
  --staging_bucket="gs://amfe-staging" \
  amfe_orchestrator/
```

### Command Breakdown

| Flag | Value | Purpose |
|------|-------|---------|
| `--project` | `${GOOGLE_CLOUD_PROJECT}` | Target GCP project |
| `--region` | `us-central1` | Deployment region (must match BigQuery dataset region) |
| `--display_name` | `"AMFE Orchestrator"` | Human-readable name in Cloud Console |
| `--staging_bucket` | `gs://amfe-staging` | GCS bucket for staging agent artifacts |
| (positional) | `amfe_orchestrator/` | Directory containing `agent.py` and `__init__.py` |

### What the Command Does

1. Packages the `amfe_orchestrator/` directory (agent code, dependencies)
2. Uploads the package to `gs://amfe-staging`
3. Creates or updates the Agent Engine deployment
4. Configures the runtime with the project's service account
5. Exposes the agent via a managed endpoint

---

## Staging Bucket Setup

The staging bucket must exist before the first deployment:

```bash
# Create the bucket
gsutil mb -l us-central1 gs://amfe-staging

# Set lifecycle to auto-delete old staging artifacts (optional, cost optimization)
cat > /tmp/lifecycle.json << 'EOF'
{
  "rule": [
    {
      "action": {"type": "Delete"},
      "condition": {"age": 30}
    }
  ]
}
EOF
gsutil lifecycle set /tmp/lifecycle.json gs://amfe-staging
```

---

## Session Management

### Conversation History Persistence

Agent Engine automatically persists conversation turns within a session. When a user sends
multiple messages, the full history is available to the agent:

```
Turn 1: "Analyze NVDA"
  → Agent calls get_stock_profile, forecast, returns BUY recommendation

Turn 2: "What about AMD compared to that?"
  → Agent has full context of the NVDA analysis from Turn 1
  → Can compare AMD signals against NVDA without re-fetching
```

Sessions are identified by a `session_id` that the client application provides or that
Agent Engine auto-generates. Sessions persist until explicitly deleted or until they expire
(configurable TTL).

### Session Data Stored

| Data | Retention | Purpose |
|------|-----------|---------|
| User messages | Session lifetime | Context for multi-turn conversations |
| Agent responses | Session lifetime | Reference for follow-up questions |
| Tool call results | Session lifetime | Avoid redundant API calls |
| Agent routing decisions | Session lifetime | Audit trail |

---

## Memory Bank for Long-Term Context

Memory Bank extends beyond individual sessions to provide cross-session memory:

```
Session A (Monday): "I'm focused on tech stocks, risk-tolerant"
Session B (Wednesday): "Screen for stocks"
  → Memory Bank provides: user prefers Technology sector, high risk tolerance
  → Screener Agent automatically filters for higher-volatility tech stocks
```

### Memory Bank Use Cases in StonxAI

| Context Type | Example | How Agent Uses It |
|-------------|---------|-------------------|
| Sector preference | "I only care about tech and healthcare" | Pre-filters screening results |
| Risk tolerance | "I'm conservative" | Weights HOLD recommendations higher |
| Portfolio holdings | "I own NVDA and AAPL" | Avoids recommending stocks already held |
| Analysis history | Previously analyzed TSLA as SELL | References past analysis for consistency |

---

## Agent Engine vs Cloud Run: Decision Guide

| Criterion | Vertex Agent Engine | Cloud Run |
|-----------|-------------------|-----------|
| **Session management** | Built-in, managed | Must implement yourself (Redis, Firestore) |
| **Deployment** | `adk deploy agent_engine` (one command) | Dockerfile + `gcloud run deploy` |
| **Scaling** | Automatic, opaque | Configurable (min/max instances, concurrency) |
| **Cost model** | Per-request pricing | Per-instance-second pricing |
| **Custom ports** | Not configurable | Full control |
| **A2A protocol** | Supported as client (RemoteA2aAgent) | Supported as server (`to_a2a()`) |
| **Memory Bank** | Built-in | Not available |
| **Best for** | User-facing orchestrators with conversation | Backend services, A2A endpoints, batch jobs |

### StonxAI's Decision

| Component | Deployment Target | Rationale |
|-----------|------------------|-----------|
| AMFE Orchestrator | **Agent Engine** | Needs sessions, Memory Bank, managed scaling |
| Research Service | **Cloud Run** | Needs custom port (8001), serves as A2A endpoint |
| Ingest Job | **Cloud Run Job** | Batch workload, runs once/day |
| MCP Toolbox | **Cloud Run** | Sidecar service, stateless |

---

## Connecting to MCP Toolbox Sidecar

The Agent Engine deployment automatically connects to the MCP Toolbox when configured via
`BigQueryToolset` in the agent code:

```python
from google.adk.tools.bigquery import BigQueryToolset, BigQueryToolConfig, WriteMode

bq_toolset = BigQueryToolset(
    bigquery_tool_config=BigQueryToolConfig(
        write_mode=WriteMode.ALLOWED
    )
)
```

The toolset reads the `tools.yaml` configuration and connects to the toolbox sidecar. In
Agent Engine, this connection is managed internally — no explicit URL configuration is
needed.

For local development, the toolbox runs locally and connects directly to BigQuery:

```bash
# Local testing with adk web
adk web amfe_orchestrator/
```

---

## Connecting to A2A Research Service

The orchestrator connects to the Cloud Run-hosted research service via `RemoteA2aAgent`:

```python
from google.adk.agents import RemoteA2aAgent

research_proxy = RemoteA2aAgent(
    name="research_proxy",
    description="Deep fundamental research: SEC filings + news analysis + web search. "
                "Call for borderline signals or user requests for deep company analysis.",
    agent_card_url="https://amfe-research-service-[hash]-uc.a.run.app/.well-known/agent-card.json",
)
```

### Getting the Research Service URL

After deploying the research service to Cloud Run:

```bash
# Get the service URL
RESEARCH_URL=$(gcloud run services describe amfe-research-service \
  --project=${GOOGLE_CLOUD_PROJECT} \
  --region=us-central1 \
  --format='value(status.url)')

echo "Agent card URL: ${RESEARCH_URL}/.well-known/agent-card.json"
```

Update the `agent_card_url` in `amfe_orchestrator/agent.py` with the actual URL before
deploying to Agent Engine.

### Authentication Between Services

The Agent Engine's service account must have `roles/run.invoker` on the research service
to call it. This is configured in IAM:

```bash
gcloud run services add-iam-policy-binding amfe-research-service \
  --project=${GOOGLE_CLOUD_PROJECT} \
  --region=us-central1 \
  --member="serviceAccount:amfe-agent@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

---

## Production Checklist

### Pre-Deployment

- [ ] BigQuery `amfe_data` dataset exists with all tables and views created
- [ ] `screening_master` has recent data (run ingest job + Dataform first)
- [ ] Staging bucket `gs://amfe-staging` created in the correct region
- [ ] Service account `amfe-agent@project.iam.gserviceaccount.com` created
- [ ] IAM roles assigned (see [Cloud Run IAM section](./cloud-run.md#iam-roles-required))
- [ ] Research service deployed and URL obtained
- [ ] `agent_card_url` updated in `agent.py` with actual research service URL
- [ ] `tools.yaml` tested locally with `adk web`

### Deployment

- [ ] Run `adk deploy agent_engine` with correct flags
- [ ] Verify deployment succeeded in Cloud Console > Vertex AI > Agent Engine
- [ ] Note the deployed agent endpoint URL

### Post-Deployment Verification

- [ ] **Mode A test:** Send "Analyze NVDA" — verify `get_stock_profile` and `forecast` are called
- [ ] **Mode B test:** Send "What is RSI?" — verify direct knowledge answer (no tool calls)
- [ ] **Mode B test:** Send "Which sectors are bullish?" — verify `ask_data_insights` is called
- [ ] **Mode C test:** Send "Find momentum tech stocks" — verify `execute_screen` is called
- [ ] **Session test:** Send two related messages — verify context persists
- [ ] **Decision logging:** Check `agent_decisions` table for new rows after analysis
- [ ] **Research test:** Trigger a deep dive — verify A2A call to research service succeeds
- [ ] **Error handling:** Send an invalid ticker — verify graceful error message

### Monitoring Setup

- [ ] Set up Cloud Monitoring alerts for:
  - Agent Engine error rate > 5%
  - Agent Engine p95 latency > 30s
  - BigQuery query failures
  - Research service invocation failures
- [ ] Verify logs appear in Cloud Logging
- [ ] Set up weekly cost report for the project

---

## Updating the Deployment

To deploy a new version:

```bash
# Re-run the deploy command — it updates in place
adk deploy agent_engine \
  --project=${GOOGLE_CLOUD_PROJECT} \
  --region=us-central1 \
  --display_name="AMFE Orchestrator" \
  --staging_bucket="gs://amfe-staging" \
  amfe_orchestrator/
```

Agent Engine handles zero-downtime deployments automatically. The previous version continues
serving requests until the new version is ready.

---

## Local Development and Testing

Before deploying to Agent Engine, test locally:

```bash
# Start the agent locally with the ADK web UI
adk web amfe_orchestrator/

# This starts:
# - The orchestrator and all sub-agents
# - A local MCP Toolbox connection to BigQuery
# - A web UI at http://localhost:8000 for interactive testing
```

For testing without BigQuery access, mock the toolset:

```bash
# Set environment variables
export GOOGLE_CLOUD_PROJECT=amfe-project
export RESEARCH_SERVICE_URL=http://localhost:8001

# Start the agent
adk web amfe_orchestrator/
```

---

## Related Documentation

- [Cloud Run Deployment](./cloud-run.md) — Research service, ingest job, MCP Toolbox
- [MCP Toolbox Configuration](../tools/mcp-toolbox.md) — BigQuery tool definitions
- [Environment Setup](../tools/environment-setup.md) — All environment variables
- [Architecture](../../architecture.md) — Full system design
