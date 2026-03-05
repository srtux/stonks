# Orchestrator Agent

> **Source files:**
> - `/amfe_orchestrator/agent.py` (lines 128-150)
> - `/amfe_orchestrator/__init__.py`

---

## Purpose

The orchestrator is the **root agent** of the StonxAI multi-agent system. It receives every user message, classifies the user's intent, and delegates to the appropriate sub-agent. It never performs financial analysis itself -- it is purely a router and response synthesizer.

---

## Model Choice: `gemini-2.0-flash`

The orchestrator uses `gemini-2.0-flash` because:

1. **Low latency** -- Intent classification is a lightweight text-classification task. A heavier model would add unnecessary latency to every single user interaction.
2. **Cost efficiency** -- Every user message hits the orchestrator first. Using a cheaper model here keeps per-request costs minimal.
3. **Sufficient capability** -- Keyword-based routing and intent classification do not require advanced reasoning; `gemini-2.0-flash` handles this reliably.

---

## Routing Logic

The orchestrator classifies each user message into one of three modes:

### MODE A -- Stock Analysis

**Trigger keywords:** `"analyze"`, `"should I buy/sell"`, `"what do you think about [TICKER]"`, `"recommendation for"`, `"rate [TICKER]"`

**Delegates to:** `stock_analysis_agent`

**Example:** `"Analyze NVDA"` or `"Should I buy AAPL?"` or `"Rate TSLA"`

### MODE B -- Financial Q&A

**Trigger keywords:** `"what is"`, `"how does"`, `"explain"`, `"tell me about"`, current events questions about markets

**Delegates to:** `qa_agent`

**Example:** `"What is a P/E ratio?"` or `"How does RSI work?"`

### MODE C -- Stock Screening

**Trigger keywords:** `"find stocks"`, `"screen for"`, `"which stocks"`, `"show me stocks that"`, `"list stocks with"`

**Delegates to:** `screener_agent`

**Example:** `"Find undervalued tech stocks with momentum"` or `"Screen for oversold value plays"`

---

## Intent Classification Heuristics

The orchestrator uses keyword-matching heuristics embedded in its system instruction (lines 132-143 of `agent.py`). This is not a separate classifier model -- the LLM itself parses the instruction and matches against the described keyword patterns. The classification is "soft" in that the LLM can use context beyond exact keyword matches (e.g., `"What's your take on GOOG?"` maps to MODE A even though it does not contain the word `"analyze"`).

---

## Sub-Agent Delegation Pattern

```
User message
     |
     v
+--------------------+
| root_agent         |
| (amfe_orchestrator)|
+--------------------+
     |
     +--- MODE A ---> stock_analysis_agent
     |
     +--- MODE B ---> qa_agent
     |
     +--- MODE C ---> screener_agent
     |
     +--- (deep dive) ---> research_proxy (RemoteA2aAgent)
```

The orchestrator holds all four sub-agents in its `sub_agents` list:

```python
sub_agents=[
    stock_analysis_agent,
    qa_agent,
    screener_agent,
    research_proxy,
]
```

ADK's `LlmAgent` framework handles the delegation mechanics: the orchestrator's LLM output includes a sub-agent invocation, and ADK routes the conversation to that sub-agent, collects its response, and returns it to the orchestrator for synthesis.

---

## Response Synthesis

After a sub-agent completes its work, the orchestrator synthesizes the response:

- Wraps the sub-agent output into a clean, well-formatted answer.
- Always includes **which agent(s) were used and why** (per the instruction on line 142).
- Does not alter the factual content of the sub-agent's output, but may restructure formatting for readability.

---

## Ambiguity Handling

When the user's intent is unclear (e.g., `"Tell me about AAPL"` -- could be analysis or Q&A), the orchestrator is instructed to **ask ONE clarifying question** rather than guessing. This is specified on line 143:

```python
"If ambiguous, ask ONE clarifying question."
```

This prevents cascading errors from misrouted queries.

---

## Configuration

### Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `RESEARCH_SERVICE_URL` | URL to the Cloud Run research service's A2A endpoint | `""` (empty string) |

The URL is loaded via `os.getenv()` on line 24 and passed to the `RemoteA2aAgent` constructor.

### BigQuery Toolset Setup

```python
bq_toolset = BigQueryToolset(
    config=BigQueryToolConfig(write_mode=WriteMode.ALLOWED),
)
```

- **WriteMode.ALLOWED** is required because the `stock_analysis_agent` needs to INSERT rows into `amfe_data.agent_decisions` for audit logging (Step 5 of its workflow).
- The toolset is shared across `stock_analysis_agent`, `qa_agent`, and `screener_agent`.
- It provides built-in tools: `execute_sql`, `forecast`, `ask_data_insights`, `get_table_info`.

### Additional Tool Imports

```python
from mcp_toolbox.realtime_quote import get_stock_profile
from mcp_toolbox.stock_api import execute_screen
```

These Python functions are imported and injected as tools into the appropriate sub-agents.

---

## Module Structure

### `__init__.py`

```python
from .agent import root_agent
```

Exports only `root_agent`. This is the entry point that Vertex AI Agent Engine and `adk deploy` look for -- the module-level `root_agent` variable.

### `agent.py` Layout

| Lines | Section |
|---|---|
| 1-7 | Module docstring |
| 9-17 | Imports (ADK, BigQuery, tools) |
| 19-24 | Environment variable loading |
| 29-31 | BigQuery toolset initialization |
| 36-60 | `stock_analysis_agent` definition |
| 65-85 | `qa_agent` definition |
| 90-110 | `screener_agent` definition |
| 115-123 | `research_proxy` (RemoteA2aAgent) definition |
| 128-150 | `root_agent` definition |

---

## Deployment to Vertex AI Agent Engine

The orchestrator is deployed as a managed agent on Vertex AI Agent Engine:

```bash
adk deploy agent_engine \
  --project=$PROJECT_ID \
  --region=us-central1 \
  --display_name="AMFE Orchestrator" \
  --staging_bucket="gs://amfe-staging" \
  amfe_orchestrator/
```

This command packages the entire `amfe_orchestrator/` directory (including all sub-agents defined within `agent.py`) and deploys them as a single managed agent.

---

## Session Management

Vertex AI Agent Engine provides **managed sessions**:

- Conversation history persists across turns without any custom session storage code.
- Each user session maintains its own conversation context, so the orchestrator (and sub-agents) can reference earlier messages in the same session.
- Session IDs are tracked in the `agent_decisions` table (`session_id` column) for audit correlation.

---

## Code Walkthrough: `root_agent` Definition

```python
# Line 128-150
root_agent = LlmAgent(
    name="amfe_orchestrator",                    # ADK agent name
    model="gemini-2.0-flash",                    # lightweight model for routing
    instruction=(                                 # system prompt with routing rules
        "You are AMFE, an AI-powered financial analysis system. "
        "Classify every user message into one of these modes and route "
        "accordingly:\n\n"
        "MODE A -- STOCK ANALYSIS (analyze, should I buy/sell, "
        "recommendation for): Route to stock_analysis_agent.\n\n"
        "MODE B -- FINANCIAL Q&A (what is, how does, explain): "
        "Route to qa_agent.\n\n"
        "MODE C -- STOCK SCREENING (find stocks, screen for, which stocks): "
        "Route to screener_agent.\n\n"
        "After routing, synthesize the sub-agent's response. "
        "If ambiguous, ask ONE clarifying question."
    ),
    sub_agents=[                                  # all available sub-agents
        stock_analysis_agent,
        qa_agent,
        screener_agent,
        research_proxy,
    ],
)
```

Key observations:

- The `root_agent` has **no tools of its own** -- it delegates all tool usage to sub-agents.
- `research_proxy` is listed as a sub-agent but is not mentioned in the routing instruction. It can be invoked by sub-agents (particularly `qa_agent` and `stock_analysis_agent`) rather than directly by the orchestrator.
- The `name="amfe_orchestrator"` is the identity used in Agent Engine logs and session tracking.

---

## Sequence Diagram: Full Request Flow

```
User                 Orchestrator           Sub-Agent              Tools
 |                       |                      |                    |
 |-- "Analyze NVDA" ---->|                      |                    |
 |                       |                      |                    |
 |                       |-- classify intent -->|                    |
 |                       |   (MODE A detected)  |                    |
 |                       |                      |                    |
 |                       |-- delegate --------->|                    |
 |                       |   stock_analysis     |                    |
 |                       |                      |-- get_stock_profile |
 |                       |                      |<-- batch + quote --|
 |                       |                      |-- forecast ------->|
 |                       |                      |<-- 5d prediction --|
 |                       |                      |-- execute_sql ---->|
 |                       |                      |   (INSERT decision)|
 |                       |                      |<-- logged ---------|
 |                       |                      |                    |
 |                       |<-- recommendation ---|                    |
 |                       |                      |                    |
 |                       |-- synthesize ------->|                    |
 |<-- formatted answer --|                      |                    |
```

---

## Cross-References

- [Stock Analysis Agent](./stock-analysis.md) -- MODE A sub-agent
- [Q&A Agent](./qa-agent.md) -- MODE B sub-agent
- [Screener Agent](./screener.md) -- MODE C sub-agent
- [Research Service](./research-service.md) -- A2A deep-dive agent (Cloud Run)
