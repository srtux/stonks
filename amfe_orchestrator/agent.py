"""
AMFE Orchestrator — ADK multi-agent system for AI-powered financial analysis.

Defines the root orchestrator and its three sub-agents (stock_analysis, qa,
screener) plus a remote research proxy that connects to the Cloud Run
deep-research service.
"""

import os
from dotenv import load_dotenv

from google.adk.agents import LlmAgent, RemoteA2aAgent
from google.adk.tools.bigquery import BigQueryToolset, BigQueryToolConfig, WriteMode
from google.adk.tools import google_search

from mcp_toolbox.realtime_quote import get_stock_profile
from mcp_toolbox.stock_api import execute_screen

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
load_dotenv()

RESEARCH_SERVICE_URL = os.getenv("RESEARCH_SERVICE_URL", "")

# ---------------------------------------------------------------------------
# Shared toolset — BigQuery with write access for logging agent decisions
# ---------------------------------------------------------------------------
bq_toolset = BigQueryToolset(
    config=BigQueryToolConfig(write_mode=WriteMode.ALLOWED),
)

# ---------------------------------------------------------------------------
# Sub-agent 1: Stock Analysis
# ---------------------------------------------------------------------------
stock_analysis_agent = LlmAgent(
    name="stock_analysis_agent",
    model="gemini-2.0-flash",
    instruction=(
        "You are a stock analyst for the AMFE system. "
        "Given a ticker symbol, ALWAYS follow these steps in order:\n\n"
        "STEP 1: Call get_stock_profile to fetch the latest batch signals and "
        "real-time quote.\n\n"
        "STEP 2: Call forecast to get the 5-day price outlook from TimesFM.\n\n"
        "STEP 3: Interpret the data using the signal_label from the batch "
        "signals. Ensure you also check the real-time intraday percentage. "
        "If the intraday move wildly contradicts the batch signal (e.g., "
        "> 5% down when batch is STRONG_BUY), flag this discrepancy heavily.\n\n"
        "STEP 4: If there is a major discrepancy, or if the user requested a "
        "deep dive, offer to trigger the asynchronous report generation.\n\n"
        "STEP 5: Log the decision via execute_sql INSERT INTO "
        "amfe_data.agent_decisions.\n\n"
        "Output a structured recommendation with: action, confidence_score, "
        "key_factors, regime_context, and a 2-3 sentence rationale."
    ),
    tools=[
        get_stock_profile,
        bq_toolset,
    ],
)

# ---------------------------------------------------------------------------
# Sub-agent 2: Financial Q&A
# ---------------------------------------------------------------------------
qa_agent = LlmAgent(
    name="qa_agent",
    model="gemini-2.0-flash",
    instruction=(
        "You are a financial educator for the AMFE system.\n\n"
        "For educational questions (what is P/E ratio, how does RSI work, "
        "explain DCF): Answer directly from knowledge.\n\n"
        "For questions about specific stocks (what happened to NVDA today, "
        "latest earnings): Call google_search to ground your answer in "
        "current information.\n\n"
        "For questions about patterns in the AMFE data (which sectors are in "
        "bull regimes, average RSI across S&P 500 today): Call "
        "ask_data_insights with the question directly.\n\n"
        "For very deep company research requests: Route to research_proxy "
        "for a comprehensive fundamental analysis."
    ),
    tools=[
        google_search,
        bq_toolset,
    ],
)

# ---------------------------------------------------------------------------
# Sub-agent 3: Stock Screener
# ---------------------------------------------------------------------------
screener_agent = LlmAgent(
    name="screener_agent",
    model="gemini-2.0-flash",
    instruction=(
        "You are a stock screener for the AMFE system. You translate natural "
        "language screening criteria into structural queries via the "
        "execute_screen tool.\n\n"
        "Available keys you can filter on: sector, industry, "
        "market_cap_min/max, rsi_14_min/max, macd_histogram_min/max, "
        "sma_cross_20_50_min/max, pe_ratio_min/max, revenue_growth_yoy_min, "
        "hmm_regime, composite_score_min, signal_label, "
        "bq_forecast_5d_pct_min.\n\n"
        "After getting results, summarize: total matches found, top 5 "
        "tickers, common characteristics, and recommended next step "
        "(e.g. 'run full analysis on AAPL')."
    ),
    tools=[
        execute_screen,
        bq_toolset,
    ],
)

# ---------------------------------------------------------------------------
# Sub-agent 4: Research Proxy (remote A2A agent on Cloud Run)
# ---------------------------------------------------------------------------
research_proxy = RemoteA2aAgent(
    name="research_proxy",
    url=RESEARCH_SERVICE_URL,
    description=(
        "Deep-research agent running on Cloud Run. Generates comprehensive "
        "fundamental analysis reports for individual companies. Use this when "
        "the user asks for an in-depth or deep-dive research report."
    ),
)

# ---------------------------------------------------------------------------
# Root Orchestrator
# ---------------------------------------------------------------------------
root_agent = LlmAgent(
    name="amfe_orchestrator",
    model="gemini-2.0-flash",
    instruction=(
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
    sub_agents=[
        stock_analysis_agent,
        qa_agent,
        screener_agent,
        research_proxy,
    ],
)
