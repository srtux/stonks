"""
Comprehensive tests for amfe_orchestrator/agent.py.

Tests the agent CONFIGURATION and STRUCTURE of the ADK multi-agent system:
root orchestrator, stock_analysis_agent, qa_agent, screener_agent,
research_proxy, BigQuery toolset, and module exports.

The installed google.adk package (Python 3.9) is missing some symbols
(RemoteA2aAgent, BigQueryToolConfig, WriteMode) from their expected
import paths.  We also need to mock mcp_toolbox dependencies (yfinance,
google.auth) that execute at module level.  We patch everything before
importing the module under test.
"""

import sys
import os
import types
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Pre-import patching
# ---------------------------------------------------------------------------

# 1. Ensure BigQueryToolConfig and WriteMode are importable from
#    google.adk.tools.bigquery (they live in .config sub-module).
from google.adk.tools.bigquery.config import BigQueryToolConfig, WriteMode
import google.adk.tools.bigquery as _bq_pkg
if not hasattr(_bq_pkg, "BigQueryToolConfig"):
    _bq_pkg.BigQueryToolConfig = BigQueryToolConfig
if not hasattr(_bq_pkg, "WriteMode"):
    _bq_pkg.WriteMode = WriteMode

# 2. Ensure RemoteA2aAgent is available from google.adk.agents.
from google.adk import agents as _agents_pkg
from google.adk.agents import BaseAgent
_NEED_REMOTE_MOCK = not hasattr(_agents_pkg, "RemoteA2aAgent")
if _NEED_REMOTE_MOCK:

    class _MockRemoteA2aAgent(BaseAgent):
        """Stand-in for RemoteA2aAgent when the SDK lacks it.

        Inherits BaseAgent to pass pydantic validation in LlmAgent.sub_agents.
        """
        url: str = ""
        description: str = ""

        model_config = {"arbitrary_types_allowed": True}

    _agents_pkg.RemoteA2aAgent = _MockRemoteA2aAgent

# 3. Mock yfinance before mcp_toolbox.realtime_quote tries to import it.
if "yfinance" not in sys.modules:
    sys.modules["yfinance"] = MagicMock()

# 4. Set required env vars before the module reads them.
os.environ.setdefault("RESEARCH_SERVICE_URL", "http://localhost:8001")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault("BQ_DATASET", "amfe_data")
os.environ.setdefault("BQ_TABLE", "screening_master")

# 5. Mock BigQueryToolset to accept the `config` kwarg (newer API than installed).
class _MockBigQueryToolset:
    """Stand-in for BigQueryToolset that accepts the config kwarg.

    Must be callable to pass pydantic validation in LlmAgent.tools.
    """

    def __init__(self, config=None, **kwargs):
        self.config = config
        self.name = "bigquery_toolset"

    def __call__(self, *args, **kwargs):
        return self

_bq_pkg.BigQueryToolset = _MockBigQueryToolset

# 6. Patch google.auth.default AND google.cloud.bigquery.Client so
#    mcp_toolbox modules don't fail at import.
_mock_creds = MagicMock()
_auth_patch = patch("google.auth.default", return_value=(_mock_creds, "test-project"))
_bq_client_patch = patch("google.cloud.bigquery.Client")
_auth_patch.start()
_bq_client_patch.start()

# Now safe to import the module under test
import amfe_orchestrator.agent as orch_mod  # noqa: E402

_auth_patch.stop()
_bq_client_patch.stop()

from google.adk.agents import LlmAgent  # noqa: E402

# Grab whatever RemoteA2aAgent is (real or mock) for isinstance checks
_RemoteA2aAgent = _agents_pkg.RemoteA2aAgent


# ===================================================================
# Helpers
# ===================================================================


def _tool_names(tools):
    """Extract human-readable names from a list of tools / toolsets."""
    names = set()
    for t in tools:
        if callable(t) and hasattr(t, "__name__"):
            names.add(t.__name__)
        elif hasattr(t, "name"):
            names.add(t.name)
        else:
            names.add(repr(t))
    return names


# ===================================================================
# Root Agent Tests
# ===================================================================


class TestRootAgent:
    """Tests for the root orchestrator agent configuration."""

    def test_root_agent_exists(self):
        """root_agent is defined and is an LlmAgent instance."""
        assert orch_mod.root_agent is not None
        assert isinstance(orch_mod.root_agent, LlmAgent)

    def test_root_agent_name(self):
        """root_agent name is 'amfe_orchestrator'."""
        assert orch_mod.root_agent.name == "amfe_orchestrator"

    def test_root_agent_model(self):
        """root_agent model is 'gemini-2.0-flash'."""
        assert orch_mod.root_agent.model == "gemini-2.0-flash"

    def test_root_agent_has_sub_agents(self):
        """root_agent has exactly 4 sub_agents."""
        assert len(orch_mod.root_agent.sub_agents) == 4

    def test_root_agent_sub_agent_names(self):
        """sub_agents have the expected names."""
        names = {sa.name for sa in orch_mod.root_agent.sub_agents}
        expected = {
            "stock_analysis_agent",
            "qa_agent",
            "screener_agent",
            "research_proxy",
        }
        assert names == expected

    def test_root_agent_instruction_contains_mode_a(self):
        """Instruction mentions STOCK ANALYSIS (MODE A)."""
        assert "STOCK ANALYSIS" in orch_mod.root_agent.instruction

    def test_root_agent_instruction_contains_mode_b(self):
        """Instruction mentions Q&A (MODE B)."""
        assert "Q&A" in orch_mod.root_agent.instruction

    def test_root_agent_instruction_contains_mode_c(self):
        """Instruction mentions SCREENING (MODE C)."""
        assert "SCREENING" in orch_mod.root_agent.instruction


# ===================================================================
# Stock Analysis Agent Tests
# ===================================================================


class TestStockAnalysisAgent:
    """Tests for the stock_analysis_agent sub-agent."""

    def test_stock_analysis_agent_name(self):
        """Name is 'stock_analysis_agent'."""
        assert orch_mod.stock_analysis_agent.name == "stock_analysis_agent"

    def test_stock_analysis_agent_model(self):
        """Model is 'gemini-2.0-flash'."""
        assert orch_mod.stock_analysis_agent.model == "gemini-2.0-flash"

    def test_stock_analysis_agent_has_tools(self):
        """Tools list is not empty."""
        assert len(orch_mod.stock_analysis_agent.tools) > 0

    def test_stock_analysis_agent_has_get_stock_profile(self):
        """get_stock_profile is among the tools."""
        tool_names = _tool_names(orch_mod.stock_analysis_agent.tools)
        assert "get_stock_profile" in tool_names

    def test_stock_analysis_agent_has_bq_toolset(self):
        """The BigQuery toolset is included in the tools list."""
        assert orch_mod.bq_toolset in orch_mod.stock_analysis_agent.tools

    def test_stock_analysis_agent_instruction_step1(self):
        """Instruction contains reference to get_stock_profile (STEP 1)."""
        assert "get_stock_profile" in orch_mod.stock_analysis_agent.instruction

    def test_stock_analysis_agent_instruction_step2(self):
        """Instruction contains reference to forecast (STEP 2)."""
        assert "forecast" in orch_mod.stock_analysis_agent.instruction

    def test_stock_analysis_agent_instruction_step5(self):
        """Instruction contains reference to agent_decisions (STEP 5)."""
        assert "agent_decisions" in orch_mod.stock_analysis_agent.instruction

    def test_stock_analysis_agent_instruction_discrepancy(self):
        """Instruction mentions the 5% threshold for discrepancy flagging."""
        assert "5%" in orch_mod.stock_analysis_agent.instruction


# ===================================================================
# QA Agent Tests
# ===================================================================


class TestQAAgent:
    """Tests for the qa_agent sub-agent."""

    def test_qa_agent_name(self):
        """Name is 'qa_agent'."""
        assert orch_mod.qa_agent.name == "qa_agent"

    def test_qa_agent_model(self):
        """Model is 'gemini-2.0-flash'."""
        assert orch_mod.qa_agent.model == "gemini-2.0-flash"

    def test_qa_agent_has_google_search(self):
        """google_search is among the tools."""
        tool_names = _tool_names(orch_mod.qa_agent.tools)
        assert "google_search" in tool_names

    def test_qa_agent_has_bq_toolset(self):
        """The BigQuery toolset is included in the tools list."""
        assert orch_mod.bq_toolset in orch_mod.qa_agent.tools

    def test_qa_agent_instruction_educational(self):
        """Instruction mentions educational questions."""
        assert "educational" in orch_mod.qa_agent.instruction.lower()

    def test_qa_agent_instruction_google_search(self):
        """Instruction mentions google_search tool."""
        assert "google_search" in orch_mod.qa_agent.instruction

    def test_qa_agent_instruction_data_insights(self):
        """Instruction mentions ask_data_insights."""
        assert "ask_data_insights" in orch_mod.qa_agent.instruction

    def test_qa_agent_instruction_research_proxy(self):
        """Instruction mentions research_proxy for deep research routing."""
        assert "research_proxy" in orch_mod.qa_agent.instruction


# ===================================================================
# Screener Agent Tests
# ===================================================================


class TestScreenerAgent:
    """Tests for the screener_agent sub-agent."""

    def test_screener_agent_name(self):
        """Name is 'screener_agent'."""
        assert orch_mod.screener_agent.name == "screener_agent"

    def test_screener_agent_model(self):
        """Model is 'gemini-2.0-flash'."""
        assert orch_mod.screener_agent.model == "gemini-2.0-flash"

    def test_screener_agent_has_execute_screen(self):
        """execute_screen is among the tools."""
        tool_names = _tool_names(orch_mod.screener_agent.tools)
        assert "execute_screen" in tool_names

    def test_screener_agent_has_bq_toolset(self):
        """The BigQuery toolset is included in the tools list."""
        assert orch_mod.bq_toolset in orch_mod.screener_agent.tools

    def test_screener_agent_instruction_filter_keys(self):
        """Instruction mentions all expected filter keys."""
        instruction = orch_mod.screener_agent.instruction
        expected_keys = [
            "sector",
            "rsi_14",
            "macd_histogram",
            "sma_cross_20_50",
            "pe_ratio",
            "revenue_growth_yoy",
            "hmm_regime",
            "composite_score",
            "signal_label",
            "bq_forecast_5d_pct",
        ]
        for key in expected_keys:
            assert key in instruction, f"Filter key '{key}' not found in screener instruction"

    def test_screener_agent_instruction_summarize(self):
        """Instruction mentions 'top 5 tickers' in the summarization step."""
        assert "top 5" in orch_mod.screener_agent.instruction.lower()


# ===================================================================
# Research Proxy Tests
# ===================================================================


class TestResearchProxy:
    """Tests for the research_proxy RemoteA2aAgent."""

    def test_research_proxy_is_remote_agent(self):
        """research_proxy is a RemoteA2aAgent instance."""
        assert isinstance(orch_mod.research_proxy, _RemoteA2aAgent)

    def test_research_proxy_name(self):
        """Name is 'research_proxy'."""
        assert orch_mod.research_proxy.name == "research_proxy"

    def test_research_proxy_has_description(self):
        """Description is not empty."""
        assert orch_mod.research_proxy.description
        assert len(orch_mod.research_proxy.description) > 0

    def test_research_proxy_url_from_env(self):
        """URL comes from RESEARCH_SERVICE_URL env var."""
        assert orch_mod.RESEARCH_SERVICE_URL != ""


# ===================================================================
# BigQuery Toolset Tests
# ===================================================================


class TestBigQueryToolset:
    """Tests for the shared BigQuery toolset."""

    def test_bq_toolset_exists(self):
        """bq_toolset is defined and not None."""
        assert orch_mod.bq_toolset is not None

    def test_bq_toolset_write_mode(self):
        """WriteMode is ALLOWED on the BQ config."""
        config = orch_mod.bq_toolset.config
        assert config.write_mode == WriteMode.ALLOWED


# ===================================================================
# Module / __init__ Tests
# ===================================================================


class TestModuleExports:
    """Tests for the amfe_orchestrator package __init__.py exports."""

    def test_init_exports_root_agent(self):
        """__init__.py exports root_agent at the package level."""
        import amfe_orchestrator
        assert hasattr(amfe_orchestrator, "root_agent")
        assert amfe_orchestrator.root_agent.name == "amfe_orchestrator"
