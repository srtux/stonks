# Q&A Agent

> **Source files:**
> - `/amfe_orchestrator/agent.py` (lines 65-85)

---

## Purpose

The Q&A agent serves as the **financial education and general knowledge** interface of the StonxAI system. It answers broad financial questions, provides data-grounded answers about market conditions, and can route to the research service for deep company analysis. It is invoked as MODE B by the orchestrator.

---

## Model: `gemini-2.0-flash`

Sufficient for educational explanations and tool routing. The heavier `gemini-2.5-pro` is reserved for the research service, which handles the truly complex analytical tasks.

---

## Three Response Modes

The Q&A agent operates in three distinct modes depending on the nature of the question:

```
User Question
     |
     +-- Educational? -------> Mode 1: Direct Knowledge (no tool calls)
     |
     +-- Specific stock/event? -> Mode 2: google_search grounding
     |
     +-- AMFE data patterns? --> Mode 3: ask_data_insights (BigQuery)
     |
     +-- Deep research? ------> Route to research_proxy
```

### Mode 1: Direct Knowledge (Educational Questions)

**When used:** Questions about financial concepts, definitions, explanations of metrics or strategies.

**Tool calls:** None. The LLM answers from its training knowledge.

**Example queries and expected behavior:**

| Query | Behavior |
|---|---|
| "What is a P/E ratio?" | Direct explanation of price-to-earnings ratio, how to interpret it, typical ranges by sector |
| "How does RSI work?" | Explanation of Relative Strength Index, overbought/oversold thresholds (70/30), calculation method |
| "Explain DCF analysis" | Walkthrough of discounted cash flow methodology, inputs, limitations |
| "What's the difference between SMA and EMA?" | Comparison of simple vs. exponential moving averages |
| "How do Bollinger Bands work?" | Explanation of upper/lower bands, standard deviation basis, squeeze patterns |

**Example interaction:**

```
User: "What is a P/E ratio?"

Agent (no tool calls):
"The Price-to-Earnings (P/E) ratio measures how much investors are
willing to pay per dollar of earnings. It is calculated as:

  P/E = Stock Price / Earnings Per Share (EPS)

A P/E of 20 means investors pay $20 for every $1 of earnings.

Interpretation:
- Low P/E (< 15): May indicate undervaluation or low growth expectations
- High P/E (> 30): May indicate overvaluation or high growth expectations
- Compare within sector: Tech stocks typically have higher P/E than utilities

In the AMFE system, P/E ratios are pre-computed in the screening_master
table from SEC filing data."
```

### Mode 2: `google_search` Grounding (Current Events / Specific Stocks)

**When used:** Questions about what is happening right now with specific stocks, recent earnings, market events, or anything requiring current information.

**Tool calls:** `google_search` (Google Grounding API)

**Example queries and expected behavior:**

| Query | Behavior |
|---|---|
| "What happened to NVDA today?" | Search for recent NVDA news, price action, analyst commentary |
| "Latest Tesla earnings results" | Search for most recent TSLA earnings report, revenue/EPS figures |
| "Why is the market down today?" | Search for current market news, identify catalysts |
| "What did the Fed say yesterday?" | Search for recent Federal Reserve statements |

**Example interaction:**

```
User: "What happened to NVDA today?"

Agent:
1. Calls google_search("NVDA stock news today")
2. Grounds response in search results

"Based on current reports, NVIDIA (NVDA) is [up/down X%] today.
The move appears driven by [catalyst from search results].
[Additional context from search results.]

Want me to run a full analysis on NVDA for a BUY/HOLD/SELL
recommendation?"
```

### Mode 3: `ask_data_insights` (AMFE Data Patterns)

**When used:** Questions about patterns, aggregations, or statistics within the AMFE BigQuery dataset.

**Tool calls:** `ask_data_insights` (BigQuery Conversational Analytics API)

**Example queries and expected behavior:**

| Query | Behavior |
|---|---|
| "Which sectors are in bull regimes?" | Queries screening_master for regime distribution by sector |
| "Average RSI across S&P 500 today" | Computes mean RSI from latest_screening_master |
| "How many stocks have STRONG_BUY signals?" | Counts signal_label distribution |
| "What's the sector breakdown of oversold stocks?" | Filters rsi_14 < 30 and groups by sector |

**Example interaction:**

```
User: "Which sectors have the most stocks in bull regime right now?"

Agent:
1. Calls ask_data_insights(
     "Which sectors have the most stocks in bull regime
      in the latest_screening_master?"
   )
2. BigQuery Conversational Analytics translates to SQL and returns results

"Based on the latest AMFE data:

1. Technology: 87 stocks in BULL_QUIET, 12 in BULL_VOLATILE
2. Healthcare: 54 stocks in BULL_QUIET, 8 in BULL_VOLATILE
3. Consumer Discretionary: 41 stocks in BULL_QUIET, 15 in BULL_VOLATILE
...

Technology leads with the most stocks in bullish regimes. Want me
to screen for the strongest tech stocks in bull regime?"
```

---

## Routing to `research_proxy`

The Q&A agent can route queries to the research proxy (A2A Research Service on Cloud Run) for deep company analysis. This is specified in the instruction (lines 78-79):

```
For very deep company research requests: Route to research_proxy
for a comprehensive fundamental analysis.
```

**Trigger conditions:**

- User explicitly asks for "deep research" or "comprehensive analysis" of a company.
- Question requires SEC filing analysis that goes beyond what `google_search` can provide.
- User asks about a company's fundamental health, bull/bear thesis, or risk factors.

**Example triggers:**

| Query | Action |
|---|---|
| "Do a deep dive on MSFT's fundamentals" | Route to research_proxy |
| "What are the risk factors for TSLA?" | Route to research_proxy |
| "Analyze Amazon's latest 10-K" | Route to research_proxy |

See [Research Service](./research-service.md) for details on what the proxy returns.

---

## Tools

| Tool | Source | Purpose |
|---|---|---|
| `google_search` | `google.adk.tools.google_search` | Real-time web search grounding |
| `bq_toolset` (includes `ask_data_insights`) | `BigQueryToolset` | Query AMFE data patterns |

The Q&A agent does **not** have `execute_screen` or `get_stock_profile` -- those belong to the screener and stock analysis agents respectively. If a user's question turns into a screening or analysis request, the orchestrator should reclassify and reroute.

---

## Decision Tree: Which Mode to Use

```
Is the question about a financial concept or methodology?
  |
  +-- YES --> Mode 1: Direct Knowledge
  |
  +-- NO
       |
       Is the question about current events, news, or specific stock movements?
         |
         +-- YES --> Mode 2: google_search
         |
         +-- NO
              |
              Is the question about patterns/statistics in the AMFE dataset?
                |
                +-- YES --> Mode 3: ask_data_insights
                |
                +-- NO
                     |
                     Is it a request for deep fundamental research on a company?
                       |
                       +-- YES --> Route to research_proxy
                       |
                       +-- NO --> Answer with best judgment or ask for clarification
```

---

## Sequence Diagram: Q&A Flow (Mode 2 -- google_search)

```
User            Orchestrator      QA Agent        google_search
 |                   |                |                |
 |--"What happened"->|                |                |
 |   "to NVDA?"      |                |                |
 |                   |--MODE B------->|                |
 |                   |                |                |
 |                   |                |--search------->|
 |                   |                |  "NVDA news"   |
 |                   |                |<--results------|
 |                   |                |                |
 |                   |                |--synthesize    |
 |                   |                |  grounded answer|
 |                   |                |                |
 |                   |<--answer-------|                |
 |<--formatted-------|                |                |
```

---

## Sequence Diagram: Q&A Flow (Mode 3 -- ask_data_insights)

```
User            Orchestrator      QA Agent       ask_data_insights     BigQuery
 |                   |                |                |                  |
 |--"Which sectors"->|                |                |                  |
 |   "in bull?"      |                |                |                  |
 |                   |--MODE B------->|                |                  |
 |                   |                |                |                  |
 |                   |                |--question----->|                  |
 |                   |                |                |--NL-to-SQL------>|
 |                   |                |                |<--query results--|
 |                   |                |<--insights-----|                  |
 |                   |                |                |                  |
 |                   |                |--format answer |                  |
 |                   |<--answer-------|                |                  |
 |<--formatted-------|                |                |                  |
```

---

## Cross-References

- [Orchestrator](./orchestrator.md) -- Parent agent that routes MODE B here
- [Research Service](./research-service.md) -- Called for deep company research
- [Stock Analysis Agent](./stock-analysis.md) -- Handles ticker-specific recommendations (not Q&A)
- [Screener Agent](./screener.md) -- Handles stock screening requests (not Q&A)
