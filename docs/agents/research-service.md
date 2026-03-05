# Research Service (A2A Deep-Dive Agent)

> **Source files:**
> - `/research_service/agent.py`
> - `/research_service/__init__.py`

---

## Purpose

The research service is a **heavyweight deep-dive fundamental analysis agent** that runs on Cloud Run as a standalone service. It is called conditionally -- not on every request -- when the orchestrator or its sub-agents determine that pre-computed quantitative signals are insufficient and a deeper fundamental analysis is needed.

It performs SEC filing analysis, news sentiment scoring, and real-time web search to produce a comprehensive research report.

---

## Model Choice: `gemini-2.5-pro`

The research service uses `gemini-2.5-pro` (line 198) instead of `gemini-2.0-flash` because:

1. **Complex reasoning** -- Synthesizing SEC filings, news sentiment, and market context into a coherent bull/bear thesis requires stronger analytical capabilities.
2. **Long context** -- SEC filings can be large (the tool sends up to 15,000 characters of raw filing text). `gemini-2.5-pro` handles long contexts more reliably.
3. **Quality over speed** -- This agent runs asynchronously and conditionally. Users have already received an initial response from the stock analysis agent; the research report is a follow-up that can tolerate higher latency.
4. **Cost justification** -- By only calling this agent conditionally (borderline signals, explicit deep-dive requests), the higher per-token cost of `gemini-2.5-pro` is contained.

---

## A2A Protocol: `to_a2a()` and Cloud Run Deployment

### How `to_a2a()` Works

The research agent is exposed as an Agent-to-Agent (A2A) service on line 224:

```python
app = research_agent.to_a2a()
```

This call converts the `LlmAgent` into an ASGI web application that:

1. Exposes a `.well-known/agent-card.json` endpoint describing the agent's capabilities.
2. Accepts incoming A2A messages (JSON-RPC over HTTP).
3. Routes them to the `research_agent` LLM for processing.
4. Returns the agent's response in A2A protocol format.

### Cloud Run Deployment

The service runs on Cloud Run, listening on port 8001:

```python
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
```

Deploy command:

```bash
adk deploy cloud_run \
  --project=$PROJECT_ID \
  --region=us-central1 \
  --service_name=amfe-research-service \
  research_service/
```

### How the Orchestrator Connects

In the orchestrator (`/amfe_orchestrator/agent.py`, lines 115-123):

```python
research_proxy = RemoteA2aAgent(
    name="research_proxy",
    url=RESEARCH_SERVICE_URL,
    description="Deep-research agent running on Cloud Run...",
)
```

The `RESEARCH_SERVICE_URL` environment variable points to:
```
https://amfe-research-service-[hash]-uc.a.run.app
```

The `RemoteA2aAgent` automatically discovers the agent card at `/.well-known/agent-card.json` and communicates via the A2A protocol.

---

## Three Custom Tools

### 1. `fetch_sec_filing`

**Source:** Lines 50-133

**Purpose:** Fetches the latest SEC filing (10-K or 10-Q) for a ticker and produces a Gemini-summarized extract of key sections.

#### Implementation Steps

```
Step 1: _resolve_cik(ticker)
    |  Downloads SEC company_tickers.json
    |  Matches ticker -> CIK (Central Index Key)
    |  Zero-pads CIK to 10 digits
    v
Step 2: Fetch submissions
    |  GET https://data.sec.gov/submissions/CIK{cik}.json
    |  Parse recent filings list
    v
Step 3: Find latest filing of requested type
    |  Iterate through forms[] to find first match
    |  Extract filing_date, accessionNumber, primaryDocument
    v
Step 4: Fetch filing document
    |  GET https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}
    |  Take first 15,000 characters of raw text
    v
Step 5: Gemini summarization
    |  Send to gemini-2.5-pro with prompt:
    |  "Extract: 1) Business overview, 2) Risk factors,
    |   3) Financial highlights (revenue, net income, EPS)"
    v
Return: {ticker, form_type, filing_date, accession_number,
         document_url, key_sections}
```

#### Function Signature

```python
def fetch_sec_filing(ticker: str, filing_type: str = "10-K") -> dict[str, Any]:
```

#### Parameters

- `ticker` -- Stock symbol (e.g., `"AAPL"`)
- `filing_type` -- `"10-K"` (annual) or `"10-Q"` (quarterly). Defaults to `"10-K"`.

#### Return Value

```python
{
    "ticker": "AAPL",
    "form_type": "10-K",
    "filing_date": "2025-11-01",
    "accession_number": "0000320193...",
    "document_url": "https://www.sec.gov/Archives/edgar/data/...",
    "key_sections": "Business Overview: Apple designs, manufactures..."
}
```

#### Error Handling

- If ticker cannot be resolved to CIK: `{"error": "Could not resolve CIK for ticker 'XYZ'."}`
- If no filing of requested type found: `{"error": "No 10-K filing found for XYZ."}`
- If document fetch or summarization fails: returns metadata with fallback message in `key_sections`.
- Unsupported filing type: `{"error": "Unsupported filing type: 8-K. Use '10-K' or '10-Q'."}`

#### SEC EDGAR Compliance

The tool uses a `User-Agent` header (line 32-36) as required by SEC EDGAR's fair access policy:

```python
_SEC_HEADERS = {
    "User-Agent": os.getenv(
        "SEC_EDGAR_USER_AGENT", "StonxAI/1.0 (contact@example.com)"
    ),
    "Accept": "application/json",
}
```

---

### 2. `score_news_sentiment`

**Source:** Lines 141-189

**Purpose:** Takes a list of news headlines and produces an aggregate sentiment score using Gemini as a sentiment classifier.

#### Function Signature

```python
def score_news_sentiment(headlines: list[str]) -> dict[str, Any]:
```

#### Prompt Engineering

The tool numbers each headline and sends a structured prompt to `gemini-2.5-pro`:

```
You are a financial sentiment analyst. Score the overall market sentiment
of the following news headlines on a scale from -1.0 (very bearish) to
1.0 (very bullish). Also give each headline an individual score.

Headlines:
1. NVDA reports record revenue, beats estimates
2. Fed signals potential rate hike amid inflation concerns
3. NVIDIA announces new AI chip partnership

Respond in strict JSON with this schema:
{
  "overall_score": <float between -1 and 1>,
  "label": "<VERY_BEARISH|BEARISH|NEUTRAL|BULLISH|VERY_BULLISH>",
  "per_headline": [
    {"headline": "<text>", "score": <float>}
  ]
}
Return ONLY the JSON, no markdown fences.
```

#### Scoring Scale

| Score Range | Label |
|---|---|
| -1.0 to -0.6 | `VERY_BEARISH` |
| -0.6 to -0.2 | `BEARISH` |
| -0.2 to 0.2 | `NEUTRAL` |
| 0.2 to 0.6 | `BULLISH` |
| 0.6 to 1.0 | `VERY_BULLISH` |

#### Return Value (Success)

```python
{
    "overall_score": 0.45,
    "label": "BULLISH",
    "per_headline": [
        {"headline": "NVDA reports record revenue...", "score": 0.8},
        {"headline": "Fed signals potential rate hike...", "score": -0.3},
        {"headline": "NVIDIA announces new AI chip...", "score": 0.6}
    ]
}
```

#### Fallback Handling

Three fallback paths (lines 180-189):

1. **JSON parse failure** -- If Gemini returns non-JSON text, wraps it in a neutral default:
   ```python
   {
       "overall_score": 0.0,
       "label": "NEUTRAL",
       "raw_response": "<model output>",
       "note": "Model response was not valid JSON; returning neutral default."
   }
   ```

2. **Empty headlines** -- Returns `{"error": "No headlines provided."}`.

3. **General exception** -- Returns `{"error": "Sentiment scoring failed: <details>"}`.

---

### 3. `google_search`

**Source:** Imported from `google.adk.tools` (line 15)

**Purpose:** Real-time web search using Google's Grounding API. Provides current news, analyst commentary, and market information.

This is a built-in ADK tool, not a custom implementation. It is used by the research agent as the first step in the research workflow to gather recent news headlines before scoring sentiment.

---

## Research Workflow

When invoked, the research agent follows this sequence (defined in its instruction, lines 205-211):

```
Step 1: google_search
    |  Search for recent news, earnings, analyst opinions
    |  Collect headlines and key information
    v
Step 2: fetch_sec_filing
    |  Fetch latest 10-K (or 10-Q)
    |  Get Gemini-summarized key sections
    v
Step 3: score_news_sentiment
    |  Score the headlines from Step 1
    |  Get overall sentiment and per-headline breakdown
    v
Step 4: Synthesize
    |  Combine all inputs into structured output:
    |  fundamental_signal, bull_thesis, bear_thesis, risk_flags
    v
Return comprehensive research report
```

### Sequence Diagram

```
Orchestrator    research_proxy    Research Agent    google_search    SEC EDGAR    Gemini 2.5-pro
     |               |                |                 |               |              |
     |--A2A request->|                |                 |               |              |
     |               |--forward------>|                 |               |              |
     |               |                |                 |               |              |
     |               |                |--search-------->|               |              |
     |               |                |<--news results--|               |              |
     |               |                |                 |               |              |
     |               |                |--fetch filing------------------->|              |
     |               |                |                 |               |--doc-------->|
     |               |                |                 |               |              |
     |               |                |                 |               |  summarize   |
     |               |                |                 |               |<--summary----|
     |               |                |<--filing data + summary---------|              |
     |               |                |                 |               |              |
     |               |                |--score sentiment(headlines)-------------------->|
     |               |                |<--sentiment scores-----------------------------|
     |               |                |                 |               |              |
     |               |                |  [synthesize all inputs]        |              |
     |               |                |                 |               |              |
     |               |<--report-------|                 |               |              |
     |<--A2A response-|               |                 |               |              |
```

---

## Output Format

The research agent produces a structured report with four components:

```python
{
    "fundamental_signal": "BULLISH",      # BULLISH | BEARISH | NEUTRAL
    "bull_thesis": "Revenue growth of 28% YoY driven by cloud adoption. "
                   "AI infrastructure spending is a multi-year tailwind...",
    "bear_thesis": "Massive AI capex ($50B+) may pressure margins. "
                   "Regulatory antitrust scrutiny is increasing...",
    "risk_flags": [
        "Antitrust regulatory risk in EU and US",
        "Customer concentration in top 5 enterprise accounts",
        "AI spending cyclicality risk"
    ]
}
```

| Field | Type | Description |
|---|---|---|
| `fundamental_signal` | string | Overall fundamental assessment |
| `bull_thesis` | string | Best-case narrative with supporting evidence |
| `bear_thesis` | string | Worst-case narrative with supporting evidence |
| `risk_flags` | list[str] | Specific risk factors identified |

---

## Deployment Architecture

```
Cloud Run Service: amfe-research-service
+-----------------------------------------------+
|  Container                                      |
|  +-----------------------------------------+    |
|  | uvicorn (ASGI server)                   |    |
|  | host: 0.0.0.0, port: 8001              |    |
|  |                                         |    |
|  | research_agent.to_a2a() -> ASGI app     |    |
|  |                                         |    |
|  | Endpoints:                              |    |
|  | /.well-known/agent-card.json            |    |
|  | /  (A2A JSON-RPC)                       |    |
|  +-----------------------------------------+    |
|                                                 |
|  External calls:                                |
|  -> Google Grounding API (google_search)        |
|  -> SEC EDGAR APIs (fetch_sec_filing)           |
|  -> Gemini API (sentiment scoring, filing       |
|     summarization)                              |
+-----------------------------------------------+
```

### Scaling Behavior

- Cloud Run scales to zero when idle (no cost when not in use).
- Scales up automatically when the orchestrator sends A2A requests.
- Cold start includes loading the Gemini client and environment variables.

---

## Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `GOOGLE_API_KEY` | Gemini API key for `genai.Client` (used by sentiment scoring and filing summarization) | Required, no default |
| `SEC_EDGAR_USER_AGENT` | User-Agent header for SEC EDGAR API compliance | `"StonxAI/1.0 (contact@example.com)"` |

Both are loaded via `dotenv` on line 19:

```python
load_dotenv()
```

---

## When This Service Is Called

The research service is **not called on every request**. It is invoked conditionally:

1. **From `stock_analysis_agent` (Step 4):**
   - When intraday price contradicts batch signal by >5% (discrepancy detected).
   - When the user explicitly requests a deep dive.
   - When `composite_score` is borderline between signal labels.

2. **From `qa_agent`:**
   - When the user asks for deep company research (e.g., "Analyze Amazon's latest 10-K").
   - When the question requires SEC filing analysis.

3. **Directly from the orchestrator:**
   - Listed as a sub-agent, so the orchestrator can theoretically route directly to it, though the instruction does not describe a specific trigger for this.

---

## Cost Considerations

| Factor | Impact |
|---|---|
| Model cost | `gemini-2.5-pro` is significantly more expensive per token than `gemini-2.0-flash` |
| Double Gemini calls | `fetch_sec_filing` calls Gemini for summarization, `score_news_sentiment` calls Gemini for scoring -- two separate API calls per research request |
| SEC document size | Up to 15,000 characters sent to Gemini for summarization (a large input token count) |
| Cloud Run costs | Scales to zero when idle, so cost is purely per-invocation |
| Conditional invocation | By only calling this service when needed (borderline signals, explicit requests), the system avoids the cost of running `gemini-2.5-pro` on every analysis request |

### Cost Mitigation Strategies

- The `gemini-2.0-flash` agents handle the majority of requests cheaply.
- The research service is only triggered when the lightweight analysis is insufficient.
- Filing text is truncated to 15,000 characters to limit input tokens.
- Cloud Run scales to zero, so idle time is free.

---

## Module Structure

### `__init__.py`

```python
from .agent import research_agent
```

Exports `research_agent` for deployment tooling.

### `agent.py` Layout

| Lines | Section |
|---|---|
| 1-6 | Module docstring |
| 8-17 | Imports |
| 19 | `load_dotenv()` |
| 23 | Gemini client initialization |
| 29-36 | SEC EDGAR constants and headers |
| 39-47 | `_resolve_cik()` helper |
| 50-133 | `fetch_sec_filing()` tool |
| 141-189 | `score_news_sentiment()` tool |
| 196-218 | `research_agent` LlmAgent definition |
| 224 | `app = research_agent.to_a2a()` |
| 226-229 | `__main__` uvicorn entrypoint |

---

## Cross-References

- [Orchestrator](./orchestrator.md) -- Parent system that routes to this service via `research_proxy`
- [Stock Analysis Agent](./stock-analysis.md) -- Primary caller (Step 4 deep dive)
- [Q&A Agent](./qa-agent.md) -- Secondary caller (deep research requests)
- [Screener Agent](./screener.md) -- Does not call the research service
