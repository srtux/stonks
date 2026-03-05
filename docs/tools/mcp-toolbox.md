# MCP Toolbox Configuration — BigQuery Tool Exposure

> **Source:** [`/mcp_toolbox/tools.yaml`](../../mcp_toolbox/tools.yaml)
> **Runtime:** Cloud Run sidecar alongside Vertex Agent Engine
> **Provides:** SQL queries, ML forecasts, conversational analytics, schema introspection

---

## Purpose

The MCP Toolbox for Databases is a Google-provided service that exposes BigQuery operations
as callable tools for ADK agents. Instead of embedding BigQuery client code directly in
every agent, the toolbox runs as a sidecar service and presents a standardized tool
interface that agents can call through the `BigQueryToolset` class.

In StonxAI, the MCP Toolbox provides four tools that give agents read access to the
`amfe_data` dataset and limited write access for logging decisions.

---

## tools.yaml Structure

The `tools.yaml` file is the sole configuration for the MCP Toolbox. It defines toolsets
(groups of tools sharing a data source) and individual tools within each toolset.

```yaml
toolsets:
  amfe-bq-toolset:
    type: bigquery
    project: ${GOOGLE_CLOUD_PROJECT}
    dataset: amfe_data
    tools:
      - name: execute_sql
        description: "Execute a read-only SQL query against the AMFE BigQuery dataset"
        type: sql_query
      - name: forecast
        description: "Run BigQuery ML.FORECAST for a ticker's 5-day price prediction using TimesFM"
        type: sql_query
        template: |
          SELECT * FROM ML.FORECAST(
            MODEL `amfe_data.timesfm_model`,
            STRUCT(5 AS horizon, 0.9 AS confidence_level)
          )
          WHERE ticker = @ticker
      - name: ask_data_insights
        description: "Ask a natural language question about the AMFE dataset using BigQuery conversational analytics"
        type: data_insights
      - name: get_table_info
        description: "Get schema and metadata for a table in the amfe_data dataset"
        type: table_info
```

### Syntax Reference

| Field | Level | Description |
|-------|-------|-------------|
| `toolsets` | Root | Top-level map of toolset names to their configuration |
| `type` | Toolset | Data source type. `bigquery` is the primary supported type |
| `project` | Toolset | GCP project ID. Supports `${ENV_VAR}` substitution |
| `dataset` | Toolset | Default BigQuery dataset for all tools in the toolset |
| `tools` | Toolset | List of individual tool definitions |
| `name` | Tool | Unique identifier used in agent tool calls |
| `description` | Tool | Human-readable description shown to the LLM for tool selection |
| `type` | Tool | Tool type: `sql_query`, `data_insights`, or `table_info` |
| `template` | Tool | (Optional) Pre-defined SQL template with `@variable` placeholders |

---

## Tool Definitions

### 1. execute_sql

```yaml
- name: execute_sql
  description: "Execute a read-only SQL query against the AMFE BigQuery dataset"
  type: sql_query
```

**What it does:** Accepts an arbitrary SQL query string from the agent and executes it
against the `amfe_data` dataset. This is the most flexible tool — it can run any valid
BigQuery SQL.

**When agents use it:**
- Ad-hoc queries that do not fit the structured `execute_screen` parameters
- Complex joins or aggregations the agent constructs
- INSERT statements to log decisions to `agent_decisions`
- Queries against tables other than `latest_screening_master`

**Security note:** Despite the name "read-only" in the description, this tool can execute
write operations when `WriteMode.ALLOWED` is set in the Python configuration (see
[BigQueryToolset Configuration](#bigquerytoolset-python-configuration) below). The
description guides the LLM's behavior, but the actual permission is controlled at the
Python level.

### 2. forecast

```yaml
- name: forecast
  description: "Run BigQuery ML.FORECAST for a ticker's 5-day price prediction using TimesFM"
  type: sql_query
  template: |
    SELECT * FROM ML.FORECAST(
      MODEL `amfe_data.timesfm_model`,
      STRUCT(5 AS horizon, 0.9 AS confidence_level)
    )
    WHERE ticker = @ticker
```

**What it does:** Runs BigQuery's built-in `ML.FORECAST` function using the pre-trained
TimesFM (Time Series Foundation Model) to predict the next 5 trading days of price movement.

**Template variables:**
- `@ticker` — The stock symbol to forecast. The agent provides this value when calling the
  tool.

**ML.FORECAST parameters:**
- `horizon: 5` — Predict 5 time steps (trading days) ahead
- `confidence_level: 0.9` — Return 90% confidence interval bounds

**When agents use it:** Step 2 of the Stock Analysis Agent flow, after `get_stock_profile`.
The forecast complements the batch signals by providing a forward-looking prediction.

**Output columns:** `forecast_timestamp`, `forecast_value` (predicted close price),
`prediction_interval_lower_bound`, `prediction_interval_upper_bound`, `ticker`.

### 3. ask_data_insights

```yaml
- name: ask_data_insights
  description: "Ask a natural language question about the AMFE dataset using BigQuery conversational analytics"
  type: data_insights
```

**What it does:** Passes a natural-language question to BigQuery's Conversational Analytics
API, which automatically generates and executes SQL to answer the question. This is BigQuery's
built-in NL-to-SQL capability.

**When agents use it:**
- Ambiguous user queries where the agent is not confident about SQL translation
- Broad analytical questions: "Which sectors have the most stocks in bull regime?"
- Pattern discovery: "What is the average RSI across the S&P 500 today?"
- The Q&A Agent (Mode B) uses this as a fallback for data-specific questions

**Why it exists alongside execute_sql:** `execute_sql` requires the agent to write correct
SQL. `ask_data_insights` lets the agent pass the user's question directly and relies on
BigQuery's own NL understanding. This provides a safety net for queries the LLM might
formulate incorrectly.

### 4. get_table_info

```yaml
- name: get_table_info
  description: "Get schema and metadata for a table in the amfe_data dataset"
  type: table_info
```

**What it does:** Returns the schema (column names, types, descriptions) and metadata
(row count, size, partitioning info) for a specified table in the dataset.

**When agents use it:**
- When the agent needs to verify column names before writing SQL
- When a user asks "what data do you have?" or "what columns are in the screening table?"
- During debugging or exploratory analysis

---

## BigQueryToolset Python Configuration

In the agent code (`agent.py`), the toolbox is connected via the `BigQueryToolset` class:

```python
from google.adk.tools.bigquery import BigQueryToolset, BigQueryToolConfig, WriteMode
import google.auth

creds, _ = google.auth.default()
bq_toolset = BigQueryToolset(
    bigquery_tool_config=BigQueryToolConfig(
        write_mode=WriteMode.ALLOWED
    )
)
```

### Why WriteMode.ALLOWED

The default write mode is `WriteMode.BLOCKED`, which prevents any INSERT, UPDATE, or DELETE
operations. StonxAI sets `WriteMode.ALLOWED` because:

1. **Decision logging:** The Stock Analysis Agent must INSERT rows into
   `amfe_data.agent_decisions` after every analysis to create an audit trail.
2. **Backtest dependency:** The `backtest.py` script relies on the `agent_decisions` table
   being populated with agent outputs.

**Security mitigation:** While write mode is allowed at the toolset level, the agent
instructions explicitly limit writes to the `agent_decisions` table only. The BigQuery
IAM role on the service account should also be scoped to allow writes only to
`agent_decisions`, not to `screening_master` or `ohlcv_daily`.

---

## Cloud Run Sidecar Deployment

The MCP Toolbox runs as a Cloud Run service alongside the Agent Engine deployment:

```
┌─────────────────────────────────────────────┐
│  Vertex AI Agent Engine                      │
│  ┌───────────────────────────────────────┐  │
│  │  AMFE Orchestrator (ADK agents)       │  │
│  │  Calls tools via BigQueryToolset      │──│──► BigQuery API
│  └───────────────────────────────────────┘  │
│  ┌───────────────────────────────────────┐  │
│  │  MCP Toolbox (sidecar)                │  │
│  │  Serves tools.yaml configuration      │  │
│  │  Handles BQ auth + query execution    │  │
│  └───────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
```

### How the Sidecar Works

1. The MCP Toolbox process reads `tools.yaml` at startup
2. It exposes the defined tools as an MCP-compatible tool server
3. The ADK agent's `BigQueryToolset` connects to the sidecar's local endpoint
4. When an agent calls a tool (e.g., `forecast`), the sidecar:
   - Resolves template variables (e.g., `@ticker`)
   - Executes the query against BigQuery using the service account credentials
   - Returns the results to the agent

### Deployment

The toolbox is deployed alongside the agent service. When using `adk deploy agent_engine`,
the sidecar is configured automatically. For Cloud Run manual deployment:

```bash
# The MCP Toolbox image is provided by Google
gcloud run deploy mcp-toolbox \
  --image=us-docker.pkg.dev/database-toolbox/toolbox/toolbox:latest \
  --region=us-central1 \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID}" \
  --service-account=amfe-agent@${PROJECT_ID}.iam.gserviceaccount.com \
  --port=5000
```

The `tools.yaml` file is mounted or embedded in the container configuration.

---

## Tool Type Reference

| Type | Input | Output | Use Case |
|------|-------|--------|----------|
| `sql_query` | SQL string or template variables | Query result rows | Structured data retrieval, writes |
| `data_insights` | Natural language question | NL answer + generated SQL | Ambiguous analytical questions |
| `table_info` | Table name | Schema + metadata | Exploration, debugging |

---

## Environment Variable Substitution

The `tools.yaml` file supports `${ENV_VAR}` syntax for environment variables:

```yaml
project: ${GOOGLE_CLOUD_PROJECT}
```

This is resolved at toolbox startup time, not at query time. The environment variable must
be set in the Cloud Run service configuration or the local shell environment.

---

## Related Documentation

- [Stock Screening API](./stock-api.md) — Python-based tool that queries BQ directly
- [Realtime Quote Tool](./realtime-quote.md) — Python-based tool combining BQ + yfinance
- [Cloud Run Deployment](../deployment/cloud-run.md) — Deploying the sidecar
- [Vertex Agent Engine](../deployment/vertex-agent-engine.md) — Connecting toolset to agents
- [Environment Setup](./environment-setup.md) — Setting `GOOGLE_CLOUD_PROJECT`
