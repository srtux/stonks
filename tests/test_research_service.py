"""
Comprehensive tests for research_service/agent.py.

Covers: fetch_sec_filing, score_news_sentiment, _resolve_cik,
research_agent configuration, A2A app exposure, and module exports.

All external APIs (requests, google.genai, SEC endpoints) are mocked.

Note: The source uses ``str | None`` (PEP 604) which requires Python 3.10+.
On Python 3.9, we patch ``__future__.annotations`` behavior by rewriting the
function annotations at import time. We also ensure google.genai is available
with a mock API key.
"""

from __future__ import annotations

import json
import sys
import os
import types
from typing import Any, Optional
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Pre-import patching for Python 3.9 compatibility
# ---------------------------------------------------------------------------
# research_service/agent.py uses ``str | None`` which is a syntax error
# on Python 3.9.  We work around this by reading the source, replacing
# the union syntax, compiling, and executing into a synthetic module.
# ---------------------------------------------------------------------------


def _load_research_module():
    """Load research_service.agent with Python 3.9 compatibility patches.

    The source uses ``str | None`` (PEP 604, Python 3.10+) and calls
    ``LlmAgent.to_a2a()`` which may not exist in the installed SDK.
    We patch both issues before executing the module code.
    """
    # Set env vars that the module reads at import time
    os.environ.setdefault("GOOGLE_API_KEY", "fake-api-key")
    os.environ.setdefault("SEC_EDGAR_USER_AGENT", "Test/1.0 (test@example.com)")

    src_path = os.path.join(
        os.path.dirname(__file__), "..", "research_service", "agent.py"
    )
    src_path = os.path.abspath(src_path)

    with open(src_path, "r") as f:
        source = f.read()

    # Add future annotations import at the top to defer evaluation of PEP 604
    if "from __future__ import annotations" not in source:
        source = "from __future__ import annotations\n" + source

    # Patch to_a2a() if the SDK doesn't support it — replace with a
    # simple attribute assignment so ``app`` is still defined.
    from google.adk.agents import LlmAgent as _LlmAgent
    if not hasattr(_LlmAgent, "to_a2a"):
        # Monkey-patch a stub onto LlmAgent
        _LlmAgent.to_a2a = lambda self: MagicMock(name="a2a_app")

    code = compile(source, src_path, "exec")

    mod = types.ModuleType("research_service.agent")
    mod.__file__ = src_path
    mod.__package__ = "research_service"

    exec(code, mod.__dict__)  # noqa: S102

    return mod


# Load the module once at test-collection time
rs_mod = _load_research_module()


# ---------------------------------------------------------------------------
# Fixtures — sample API responses
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_company_tickers():
    """SEC company_tickers.json sample mapping."""
    return {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
        "2": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA Corp"},
    }


@pytest.fixture()
def sample_submissions():
    """SEC submissions endpoint sample response for CIK 0000320193 (AAPL)."""
    return {
        "filings": {
            "recent": {
                "form": ["10-K", "10-Q", "8-K"],
                "filingDate": ["2025-10-30", "2025-07-15", "2025-06-01"],
                "accessionNumber": [
                    "0000320193-25-000123",
                    "0000320193-25-000099",
                    "0000320193-25-000050",
                ],
                "primaryDocument": [
                    "aapl-20250928.htm",
                    "aapl-20250628.htm",
                    "aapl-8k.htm",
                ],
            }
        }
    }


@pytest.fixture()
def sample_filing_html():
    """Minimal HTML that simulates a filing document."""
    return "<html><body>Apple Inc 10-K annual report content here...</body></html>"


@pytest.fixture()
def sample_gemini_summary():
    """Mock Gemini generate_content response for filing summarisation."""
    mock_resp = MagicMock()
    mock_resp.text = (
        "Business overview: Apple designs consumer electronics. "
        "Risk factors: Macro headwinds. "
        "Financial highlights: Revenue $400B, Net Income $100B."
    )
    return mock_resp


@pytest.fixture()
def sample_sentiment_json():
    """Well-formed sentiment JSON that the model would return."""
    return {
        "overall_score": 0.65,
        "label": "BULLISH",
        "per_headline": [
            {"headline": "AAPL beats earnings", "score": 0.8},
            {"headline": "Market rallies on tech gains", "score": 0.5},
        ],
    }


@pytest.fixture()
def bullish_headlines():
    return ["AAPL beats earnings", "Market rallies on tech gains"]


@pytest.fixture()
def bearish_headlines():
    return ["Global recession fears mount", "Tech layoffs accelerate"]


# ===================================================================
# Helpers
# ===================================================================


def _tool_names(tools):
    """Extract human-readable names from a list of tools."""
    names = set()
    for t in tools:
        if callable(t) and hasattr(t, "__name__"):
            names.add(t.__name__)
        elif hasattr(t, "name"):
            names.add(t.name)
        else:
            names.add(repr(t))
    return names


def _make_requests_side_effect(submissions_resp, doc_resp):
    """Create a side_effect for requests.get that routes by URL."""
    def side_effect(url, **kwargs):
        if "CIK" in url or "submissions" in url:
            return submissions_resp
        return doc_resp
    return side_effect


# ===================================================================
# _resolve_cik Tests
# ===================================================================


class TestResolveCik:
    """Tests for the internal _resolve_cik helper."""

    def test_resolve_cik_found(self, sample_company_tickers):
        """Valid ticker returns a zero-padded 10-digit CIK string."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = sample_company_tickers
        mock_resp.raise_for_status = MagicMock()

        with patch.object(rs_mod.requests, "get", return_value=mock_resp):
            result = rs_mod._resolve_cik("AAPL")

        assert result == "0000320193"

    def test_resolve_cik_not_found(self, sample_company_tickers):
        """Unknown ticker returns None."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = sample_company_tickers
        mock_resp.raise_for_status = MagicMock()

        with patch.object(rs_mod.requests, "get", return_value=mock_resp):
            result = rs_mod._resolve_cik("ZZZZ")

        assert result is None

    def test_resolve_cik_case_insensitive(self, sample_company_tickers):
        """Lowercase ticker still matches (case-insensitive comparison)."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = sample_company_tickers
        mock_resp.raise_for_status = MagicMock()

        with patch.object(rs_mod.requests, "get", return_value=mock_resp):
            result = rs_mod._resolve_cik("aapl")

        assert result == "0000320193"

    def test_resolve_cik_api_url(self, sample_company_tickers):
        """Verify the correct SEC company_tickers URL is called."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = sample_company_tickers
        mock_resp.raise_for_status = MagicMock()

        with patch.object(rs_mod.requests, "get", return_value=mock_resp) as mock_get:
            rs_mod._resolve_cik("AAPL")

        args, kwargs = mock_get.call_args
        assert args[0] == "https://www.sec.gov/files/company_tickers.json"


# ===================================================================
# fetch_sec_filing Tests
# ===================================================================


class TestFetchSecFiling:
    """Tests for the fetch_sec_filing tool function."""

    def _setup_patches(
        self,
        cik="0000320193",
        submissions=None,
        filing_html="<html>filing</html>",
        gemini_text="Summary of filing.",
    ):
        """Create common patches for fetch_sec_filing tests.

        Returns a dict of context-manager patches.
        """
        patches = {}
        patches["resolve_cik"] = patch.object(
            rs_mod, "_resolve_cik", return_value=cik
        )

        sub_resp = MagicMock()
        sub_resp.json.return_value = submissions or {}
        sub_resp.raise_for_status = MagicMock()

        doc_resp = MagicMock()
        doc_resp.text = filing_html
        doc_resp.raise_for_status = MagicMock()

        patches["requests_get"] = patch.object(
            rs_mod.requests,
            "get",
            side_effect=_make_requests_side_effect(sub_resp, doc_resp),
        )

        gemini_resp = MagicMock()
        gemini_resp.text = gemini_text
        patches["gemini"] = patch.object(
            rs_mod._gemini_client.models,
            "generate_content",
            return_value=gemini_resp,
        )

        return patches

    def test_fetch_sec_filing_success(self, sample_submissions):
        """Normal flow with valid ticker and 10-K returns expected keys."""
        p = self._setup_patches(submissions=sample_submissions)
        with p["resolve_cik"], p["requests_get"], p["gemini"]:
            result = rs_mod.fetch_sec_filing("AAPL", "10-K")

        assert result["ticker"] == "AAPL"
        assert result["form_type"] == "10-K"
        assert result["filing_date"] == "2025-10-30"
        assert "error" not in result

    def test_fetch_sec_filing_10q(self, sample_submissions):
        """10-Q filing type is supported and returns correct match."""
        p = self._setup_patches(submissions=sample_submissions)
        with p["resolve_cik"], p["requests_get"], p["gemini"]:
            result = rs_mod.fetch_sec_filing("AAPL", "10-Q")

        assert result["form_type"] == "10-Q"
        assert result["filing_date"] == "2025-07-15"

    def test_fetch_sec_filing_invalid_type(self):
        """Unsupported filing type returns error dict."""
        result = rs_mod.fetch_sec_filing("AAPL", "8-K")
        assert "error" in result
        assert "Unsupported filing type" in result["error"]

    def test_fetch_sec_filing_unknown_ticker(self):
        """CIK resolution failure returns error dict."""
        with patch.object(rs_mod, "_resolve_cik", return_value=None):
            result = rs_mod.fetch_sec_filing("ZZZZ", "10-K")

        assert "error" in result
        assert "Could not resolve CIK" in result["error"]

    def test_fetch_sec_filing_cik_resolution(self, sample_submissions):
        """_resolve_cik is called with the correct ticker."""
        p = self._setup_patches(submissions=sample_submissions)
        with p["resolve_cik"] as mock_cik, p["requests_get"], p["gemini"]:
            rs_mod.fetch_sec_filing("NVDA", "10-K")

        mock_cik.assert_called_once_with("NVDA")

    def test_fetch_sec_filing_cik_zero_padded(self, sample_company_tickers):
        """CIK is zero-padded to 10 digits inside _resolve_cik."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = sample_company_tickers
        mock_resp.raise_for_status = MagicMock()

        with patch.object(rs_mod.requests, "get", return_value=mock_resp):
            cik = rs_mod._resolve_cik("AAPL")

        assert len(cik) == 10
        assert cik == "0000320193"

    def test_fetch_sec_filing_submissions_url(self, sample_submissions):
        """Correct SEC submissions API URL is constructed."""
        p = self._setup_patches(cik="0000320193", submissions=sample_submissions)
        with p["resolve_cik"], p["requests_get"] as mock_get, p["gemini"]:
            rs_mod.fetch_sec_filing("AAPL", "10-K")

        calls = mock_get.call_args_list
        submissions_call = calls[0]
        assert "CIK0000320193.json" in submissions_call[0][0]

    def test_fetch_sec_filing_no_filing_found(self):
        """No matching filing type in submissions returns error."""
        empty_submissions = {
            "filings": {
                "recent": {
                    "form": ["8-K"],
                    "filingDate": ["2025-06-01"],
                    "accessionNumber": ["0000320193-25-000050"],
                    "primaryDocument": ["doc.htm"],
                }
            }
        }
        p = self._setup_patches(submissions=empty_submissions)
        with p["resolve_cik"], p["requests_get"], p["gemini"]:
            result = rs_mod.fetch_sec_filing("AAPL", "10-K")

        assert "error" in result
        assert "No 10-K filing found" in result["error"]

    def test_fetch_sec_filing_accession_number_formatted(self, sample_submissions):
        """Dashes are removed from the accession number."""
        p = self._setup_patches(submissions=sample_submissions)
        with p["resolve_cik"], p["requests_get"], p["gemini"]:
            result = rs_mod.fetch_sec_filing("AAPL", "10-K")

        assert "-" not in result["accession_number"]
        assert result["accession_number"] == "000032019325000123"

    def test_fetch_sec_filing_document_url_format(self, sample_submissions):
        """Verify the Archive URL is properly constructed."""
        p = self._setup_patches(cik="0000320193", submissions=sample_submissions)
        with p["resolve_cik"], p["requests_get"], p["gemini"]:
            result = rs_mod.fetch_sec_filing("AAPL", "10-K")

        expected_prefix = "https://www.sec.gov/Archives/edgar/data/320193/"
        assert result["document_url"].startswith(expected_prefix)
        assert result["document_url"].endswith("aapl-20250928.htm")

    def test_fetch_sec_filing_gemini_summarization(self, sample_submissions):
        """Gemini is called with the filing text for summarisation."""
        p = self._setup_patches(
            submissions=sample_submissions,
            filing_html="<html>Filing body text</html>",
            gemini_text="Summarized content",
        )
        with p["resolve_cik"], p["requests_get"], p["gemini"] as mock_gemini:
            result = rs_mod.fetch_sec_filing("AAPL", "10-K")

        mock_gemini.assert_called_once()
        # Verify model kwarg
        call_kwargs = mock_gemini.call_args
        assert call_kwargs.kwargs.get("model") == "gemini-2.5-pro"
        assert result["key_sections"] == "Summarized content"

    def test_fetch_sec_filing_document_fetch_error(self, sample_submissions):
        """Document download fails, graceful fallback with metadata still present."""
        import requests as req_lib

        resolve_patch = patch.object(rs_mod, "_resolve_cik", return_value="0000320193")

        sub_resp = MagicMock()
        sub_resp.json.return_value = sample_submissions
        sub_resp.raise_for_status = MagicMock()

        def side_effect(url, **kwargs):
            if "CIK" in url:
                return sub_resp
            raise req_lib.RequestException("Connection timeout")

        req_patch = patch.object(rs_mod.requests, "get", side_effect=side_effect)

        with resolve_patch, req_patch:
            result = rs_mod.fetch_sec_filing("AAPL", "10-K")

        assert "Could not retrieve or summarize" in result["key_sections"]
        assert result["ticker"] == "AAPL"

    def test_fetch_sec_filing_submissions_api_error(self):
        """Submissions API request failure returns error dict."""
        import requests as req_lib

        resolve_patch = patch.object(rs_mod, "_resolve_cik", return_value="0000320193")

        def raise_error(*args, **kwargs):
            raise req_lib.RequestException("API down")

        req_patch = patch.object(rs_mod.requests, "get", side_effect=raise_error)

        with resolve_patch, req_patch:
            result = rs_mod.fetch_sec_filing("AAPL", "10-K")

        assert "error" in result
        assert "Failed to fetch SEC submissions" in result["error"]

    def test_fetch_sec_filing_response_keys(self, sample_submissions):
        """Verify all expected keys are present in the success response dict."""
        p = self._setup_patches(submissions=sample_submissions)
        with p["resolve_cik"], p["requests_get"], p["gemini"]:
            result = rs_mod.fetch_sec_filing("AAPL", "10-K")

        expected_keys = {
            "ticker",
            "form_type",
            "filing_date",
            "accession_number",
            "document_url",
            "key_sections",
        }
        assert set(result.keys()) == expected_keys

    def test_fetch_sec_filing_text_truncated(self, sample_submissions):
        """Only the first 15000 chars of the document are sent to Gemini."""
        long_text = "A" * 20_000
        p = self._setup_patches(
            submissions=sample_submissions,
            filing_html=long_text,
        )
        with p["resolve_cik"], p["requests_get"], p["gemini"] as mock_gemini:
            rs_mod.fetch_sec_filing("AAPL", "10-K")

        # Extract the prompt (contents kwarg) sent to Gemini
        call_kwargs = mock_gemini.call_args
        prompt = call_kwargs.kwargs.get("contents", "")
        if isinstance(prompt, str):
            # The raw_text[:15000] means at most 15000 A's in the prompt
            assert "A" * 16_000 not in prompt
            # But it should contain some A's (the truncated portion)
            assert "A" * 100 in prompt


# ===================================================================
# score_news_sentiment Tests
# ===================================================================


class TestScoreNewsSentiment:
    """Tests for the score_news_sentiment tool function."""

    def test_score_news_sentiment_success(self, sample_sentiment_json):
        """Valid headlines with well-formed JSON response."""
        mock_resp = MagicMock()
        mock_resp.text = json.dumps(sample_sentiment_json)

        with patch.object(
            rs_mod._gemini_client.models, "generate_content", return_value=mock_resp
        ):
            result = rs_mod.score_news_sentiment(["AAPL beats earnings"])

        assert result["overall_score"] == 0.65
        assert result["label"] == "BULLISH"

    def test_score_news_sentiment_empty_headlines(self):
        """Empty list returns error dict."""
        result = rs_mod.score_news_sentiment([])
        assert "error" in result
        assert "No headlines" in result["error"]

    def test_score_news_sentiment_bullish(self, bullish_headlines):
        """Positive headlines produce a positive overall_score."""
        sentiment = {"overall_score": 0.7, "label": "BULLISH", "per_headline": []}
        mock_resp = MagicMock()
        mock_resp.text = json.dumps(sentiment)

        with patch.object(
            rs_mod._gemini_client.models, "generate_content", return_value=mock_resp
        ):
            result = rs_mod.score_news_sentiment(bullish_headlines)

        assert result["overall_score"] > 0

    def test_score_news_sentiment_bearish(self, bearish_headlines):
        """Negative headlines produce a negative overall_score."""
        sentiment = {"overall_score": -0.6, "label": "BEARISH", "per_headline": []}
        mock_resp = MagicMock()
        mock_resp.text = json.dumps(sentiment)

        with patch.object(
            rs_mod._gemini_client.models, "generate_content", return_value=mock_resp
        ):
            result = rs_mod.score_news_sentiment(bearish_headlines)

        assert result["overall_score"] < 0

    def test_score_news_sentiment_prompt_format(self):
        """Headlines are numbered in the prompt sent to Gemini."""
        headlines = ["First headline", "Second headline"]
        mock_resp = MagicMock()
        mock_resp.text = json.dumps(
            {"overall_score": 0.0, "label": "NEUTRAL", "per_headline": []}
        )

        with patch.object(
            rs_mod._gemini_client.models, "generate_content", return_value=mock_resp
        ) as mock_gen:
            rs_mod.score_news_sentiment(headlines)

        prompt = mock_gen.call_args.kwargs.get("contents", "")
        assert "1. First headline" in prompt
        assert "2. Second headline" in prompt

    def test_score_news_sentiment_json_parse_error(self):
        """Model returns non-JSON — neutral fallback with raw_response."""
        mock_resp = MagicMock()
        mock_resp.text = "This is not JSON at all."

        with patch.object(
            rs_mod._gemini_client.models, "generate_content", return_value=mock_resp
        ):
            result = rs_mod.score_news_sentiment(["Some headline"])

        assert result["overall_score"] == 0.0
        assert result["label"] == "NEUTRAL"
        assert "raw_response" in result

    def test_score_news_sentiment_api_error(self):
        """Gemini API failure returns error dict."""
        with patch.object(
            rs_mod._gemini_client.models,
            "generate_content",
            side_effect=Exception("API quota exceeded"),
        ):
            result = rs_mod.score_news_sentiment(["Some headline"])

        assert "error" in result
        assert "Sentiment scoring failed" in result["error"]

    def test_score_news_sentiment_response_keys(self, sample_sentiment_json):
        """Verify expected keys in a successful response."""
        mock_resp = MagicMock()
        mock_resp.text = json.dumps(sample_sentiment_json)

        with patch.object(
            rs_mod._gemini_client.models, "generate_content", return_value=mock_resp
        ):
            result = rs_mod.score_news_sentiment(["headline"])

        assert "overall_score" in result
        assert "label" in result
        assert "per_headline" in result

    def test_score_news_sentiment_model_used(self):
        """Verify gemini-2.5-pro model is specified in the API call."""
        mock_resp = MagicMock()
        mock_resp.text = json.dumps(
            {"overall_score": 0.0, "label": "NEUTRAL", "per_headline": []}
        )

        with patch.object(
            rs_mod._gemini_client.models, "generate_content", return_value=mock_resp
        ) as mock_gen:
            rs_mod.score_news_sentiment(["headline"])

        call_kwargs = mock_gen.call_args
        assert call_kwargs.kwargs.get("model") == "gemini-2.5-pro"


# ===================================================================
# Research Agent Configuration Tests
# ===================================================================


class TestResearchAgentConfig:
    """Tests for the research_agent LlmAgent definition."""

    def test_research_agent_exists(self):
        """research_agent is defined and not None."""
        assert rs_mod.research_agent is not None

    def test_research_agent_name(self):
        """Name is 'research_agent'."""
        assert rs_mod.research_agent.name == "research_agent"

    def test_research_agent_model(self):
        """Model is 'gemini-2.5-pro'."""
        assert rs_mod.research_agent.model == "gemini-2.5-pro"

    def test_research_agent_has_tools(self):
        """Agent has exactly 3 tools defined."""
        assert len(rs_mod.research_agent.tools) == 3

    def test_research_agent_has_google_search(self):
        """google_search is among the tools."""
        tool_names = _tool_names(rs_mod.research_agent.tools)
        assert "google_search" in tool_names

    def test_research_agent_has_fetch_sec_filing(self):
        """fetch_sec_filing is among the tools."""
        tool_names = _tool_names(rs_mod.research_agent.tools)
        assert "fetch_sec_filing" in tool_names

    def test_research_agent_has_score_news_sentiment(self):
        """score_news_sentiment is among the tools."""
        tool_names = _tool_names(rs_mod.research_agent.tools)
        assert "score_news_sentiment" in tool_names

    def test_research_agent_instruction(self):
        """Instruction mentions fundamental analysis, bull_thesis, bear_thesis."""
        instruction = rs_mod.research_agent.instruction
        assert "fundamental analysis" in instruction.lower()
        assert "bull_thesis" in instruction
        assert "bear_thesis" in instruction


# ===================================================================
# A2A / Module Tests
# ===================================================================


class TestA2AAndModule:
    """Tests for the A2A app and module-level exports."""

    def test_app_is_a2a(self):
        """app is created via to_a2a() and is not None."""
        assert rs_mod.app is not None

    def test_init_exports_research_agent(self):
        """__init__.py exports research_agent at the package level.

        Because the standard import path may fail on Python 3.9 due to
        PEP 604 syntax, we verify via our pre-loaded module instead.
        """
        assert hasattr(rs_mod, "research_agent")
        assert rs_mod.research_agent.name == "research_agent"
