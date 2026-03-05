"""
A2A Research Service — heavyweight deep-dive agent for StonxAI.

Deployed on Cloud Run, exposed via to_a2a() on port 8001.
Called conditionally by the orchestrator for borderline signals.
"""

import json
import os
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.adk.agents import LlmAgent
from google.adk.tools import google_search
import requests

load_dotenv()

# ---------------------------------------------------------------------------
# Gemini client for sentiment scoring
# ---------------------------------------------------------------------------
_gemini_client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

# ---------------------------------------------------------------------------
# Tool: fetch_sec_filing
# ---------------------------------------------------------------------------

_SEC_EDGAR_BASE = "https://efts.sec.gov/LATEST/search-index"
_SEC_SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
_SEC_HEADERS = {
    "User-Agent": os.getenv(
        "SEC_EDGAR_USER_AGENT", "StonxAI/1.0 (contact@example.com)"
    ),
    "Accept": "application/json",
}


def _resolve_cik(ticker: str) -> str | None:
    """Resolve a ticker symbol to a zero-padded CIK via SEC EDGAR company tickers."""
    url = "https://www.sec.gov/files/company_tickers.json"
    resp = requests.get(url, headers=_SEC_HEADERS, timeout=15)
    resp.raise_for_status()
    for entry in resp.json().values():
        if entry.get("ticker", "").upper() == ticker.upper():
            return str(entry["cik_str"]).zfill(10)
    return None


def fetch_sec_filing(ticker: str, filing_type: str = "10-K") -> dict[str, Any]:
    """Fetch the latest SEC filing metadata and key excerpts for a ticker.

    Args:
        ticker: Stock ticker symbol (e.g. "AAPL").
        filing_type: Type of filing — "10-K" (annual) or "10-Q" (quarterly).

    Returns:
        A dict with filing_date, form_type, and key_sections summary.
    """
    filing_type = filing_type.upper()
    if filing_type not in ("10-K", "10-Q"):
        return {"error": f"Unsupported filing type: {filing_type}. Use '10-K' or '10-Q'."}

    # Step 1 — resolve ticker to CIK
    cik = _resolve_cik(ticker)
    if cik is None:
        return {"error": f"Could not resolve CIK for ticker '{ticker}'."}

    # Step 2 — pull recent filings from the SEC submissions endpoint
    submissions_url = f"{_SEC_SUBMISSIONS_BASE}/CIK{cik}.json"
    try:
        resp = requests.get(submissions_url, headers=_SEC_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        return {"error": f"Failed to fetch SEC submissions: {exc}"}

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accession_numbers = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    # Step 3 — find the latest filing of the requested type
    filing_date = None
    accession = None
    primary_doc = None
    for i, form in enumerate(forms):
        if form == filing_type:
            filing_date = dates[i]
            accession = accession_numbers[i].replace("-", "")
            primary_doc = primary_docs[i]
            break

    if filing_date is None:
        return {"error": f"No {filing_type} filing found for {ticker}."}

    # Step 4 — fetch the filing document and extract a summary excerpt
    doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/{accession}/{primary_doc}"
    key_sections_summary = ""
    try:
        doc_resp = requests.get(doc_url, headers=_SEC_HEADERS, timeout=30)
        doc_resp.raise_for_status()
        # Take a manageable slice of the document text for summarisation
        raw_text = doc_resp.text[:15_000]

        # Use Gemini to pull out key sections
        prompt = (
            f"The following is the beginning of a SEC {filing_type} filing for {ticker}. "
            "Extract and briefly summarize the key sections: "
            "1) Business overview, 2) Risk factors, 3) Financial highlights "
            "(revenue, net income, EPS if available). Be concise.\n\n"
            f"{raw_text}"
        )
        summary_resp = _gemini_client.models.generate_content(
            model="gemini-2.5-pro",
            contents=prompt,
        )
        key_sections_summary = summary_resp.text
    except Exception:
        key_sections_summary = (
            "Could not retrieve or summarize filing document. "
            "The metadata is still available above."
        )

    return {
        "ticker": ticker.upper(),
        "form_type": filing_type,
        "filing_date": filing_date,
        "accession_number": accession,
        "document_url": doc_url,
        "key_sections": key_sections_summary,
    }


# ---------------------------------------------------------------------------
# Tool: score_news_sentiment
# ---------------------------------------------------------------------------


def score_news_sentiment(headlines: list[str]) -> dict[str, Any]:
    """Score the overall sentiment of a list of news headlines or snippets.

    Uses Gemini to produce a sentiment score from -1 (very bearish)
    to 1 (very bullish).

    Args:
        headlines: A list of news headline strings or short snippets.

    Returns:
        A dict with overall_score (-1 to 1), label, and per-headline details.
    """
    if not headlines:
        return {"error": "No headlines provided."}

    numbered = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(headlines))
    prompt = (
        "You are a financial sentiment analyst. Score the overall market sentiment "
        "of the following news headlines on a scale from -1.0 (very bearish) to "
        "1.0 (very bullish). Also give each headline an individual score.\n\n"
        f"Headlines:\n{numbered}\n\n"
        "Respond in strict JSON with this schema:\n"
        '{\n'
        '  "overall_score": <float between -1 and 1>,\n'
        '  "label": "<VERY_BEARISH|BEARISH|NEUTRAL|BULLISH|VERY_BULLISH>",\n'
        '  "per_headline": [\n'
        '    {"headline": "<text>", "score": <float>}\n'
        '  ]\n'
        '}\n'
        "Return ONLY the JSON, no markdown fences."
    )

    try:
        resp = _gemini_client.models.generate_content(
            model="gemini-2.5-pro",
            contents=prompt,
        )
        result = json.loads(resp.text)
        return result
    except json.JSONDecodeError:
        # If the model returned non-JSON, wrap it
        return {
            "overall_score": 0.0,
            "label": "NEUTRAL",
            "raw_response": resp.text,
            "note": "Model response was not valid JSON; returning neutral default.",
        }
    except Exception as exc:
        return {"error": f"Sentiment scoring failed: {exc}"}


# ---------------------------------------------------------------------------
# Research Agent
# ---------------------------------------------------------------------------

research_agent = LlmAgent(
    name="research_agent",
    model="gemini-2.5-pro",
    description=(
        "Deep fundamental research agent. Performs thorough analysis using "
        "SEC filings, real-time news search, and sentiment scoring. "
        "Called conditionally for borderline signals or deep-dive requests."
    ),
    instruction=(
        "You are a deep research analyst. When called, perform thorough "
        "fundamental analysis: "
        "1) Search for recent news and earnings, "
        "2) Fetch the latest SEC filing, "
        "3) Score news sentiment, "
        "4) Synthesize into: fundamental_signal (BULLISH/BEARISH/NEUTRAL), "
        "bull_thesis, bear_thesis, risk_flags."
    ),
    tools=[
        google_search,
        fetch_sec_filing,
        score_news_sentiment,
    ],
)

# ---------------------------------------------------------------------------
# A2A exposure for Cloud Run deployment
# ---------------------------------------------------------------------------

app = research_agent.to_a2a()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
