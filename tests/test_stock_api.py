"""
Comprehensive tests for mcp_toolbox/stock_api.py -- execute_screen function.

Every parameter and code path is exercised.  BigQuery interactions are fully
mocked so that no network calls occur during the test run.
"""

import datetime
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# We must patch the module-level google.auth.default and bigquery.Client
# *before* the module is imported, because stock_api.py calls them at import
# time (lines 6-12).
# ---------------------------------------------------------------------------

_mock_creds = MagicMock()
_mock_bq_client = MagicMock()


def _setup_module_patches():
    """Return context-manager patches that must be active when stock_api is imported."""
    auth_patch = patch("google.auth.default", return_value=(_mock_creds, "test-project"))
    client_patch = patch("google.cloud.bigquery.Client", return_value=_mock_bq_client)
    return auth_patch, client_patch


_auth_patch, _client_patch = _setup_module_patches()
_auth_patch.start()
_client_patch.start()

# Now it is safe to import the module under test.
from mcp_toolbox.stock_api import execute_screen  # noqa: E402

# We keep patches alive for the entire test session; they will be torn down
# at process exit.  This is intentional -- the module-level objects have
# already been bound.

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bq_row(data: dict) -> MagicMock:
    """Create a MagicMock that behaves like a BigQuery Row."""
    row = MagicMock()
    row.items.return_value = data.items()
    return row


def _setup_query_return(rows: list[dict]):
    """Configure the mock BQ client to return the given rows from .query().result()."""
    mock_result = MagicMock()
    mock_result.__iter__ = lambda self: iter([_make_bq_row(r) for r in rows])
    _mock_bq_client.query.return_value.result.return_value = mock_result


def _get_last_query_and_params():
    """Return (query_string, query_params_list) from the most recent bq_client.query call."""
    args, kwargs = _mock_bq_client.query.call_args
    query_string = args[0]
    job_config = kwargs.get("job_config") or args[1]
    params = job_config.query_parameters
    return query_string, params


# ---------------------------------------------------------------------------
# Tests -- individual filters
# ---------------------------------------------------------------------------


class TestExecuteScreenFilters:
    """Each test verifies that a single filter parameter adds the correct
    WHERE clause and ScalarQueryParameter / ArrayQueryParameter."""

    def setup_method(self):
        _mock_bq_client.reset_mock()
        _setup_query_return([])

    # -- no filters ----------------------------------------------------------

    def test_execute_screen_no_filters(self):
        """Calling with no params should produce the base query with no extra WHERE clauses."""
        result = execute_screen()
        query, params = _get_last_query_and_params()
        assert "WHERE 1=1" in query
        # Only the LIMIT param should be present
        param_names = [p.name for p in params]
        assert param_names == ["limit"]

    # -- sector --------------------------------------------------------------

    def test_execute_screen_sector_filter(self):
        """Sector filter adds 'AND sector = @sector' with a ScalarQueryParameter."""
        execute_screen(sector="Technology")
        query, params = _get_last_query_and_params()
        assert "AND sector = @sector" in query
        sector_param = next(p for p in params if p.name == "sector")
        assert sector_param.value == "Technology"
        assert sector_param.type_ == "STRING"

    # -- industry ------------------------------------------------------------

    def test_execute_screen_industry_filter(self):
        """Industry filter adds 'AND industry = @industry'."""
        execute_screen(industry="Semiconductors")
        query, params = _get_last_query_and_params()
        assert "AND industry = @industry" in query
        industry_param = next(p for p in params if p.name == "industry")
        assert industry_param.value == "Semiconductors"

    # -- market_cap ----------------------------------------------------------

    def test_execute_screen_market_cap_min(self):
        """market_cap_min adds >= clause."""
        execute_screen(market_cap_min=1e9)
        query, params = _get_last_query_and_params()
        assert "AND market_cap >= @market_cap_min" in query
        p = next(p for p in params if p.name == "market_cap_min")
        assert p.value == 1e9

    def test_execute_screen_market_cap_max(self):
        """market_cap_max adds <= clause."""
        execute_screen(market_cap_max=5e10)
        query, params = _get_last_query_and_params()
        assert "AND market_cap <= @market_cap_max" in query
        p = next(p for p in params if p.name == "market_cap_max")
        assert p.value == 5e10

    def test_execute_screen_market_cap_range(self):
        """Both min and max market_cap params produce two clauses."""
        execute_screen(market_cap_min=1e9, market_cap_max=5e10)
        query, params = _get_last_query_and_params()
        assert "AND market_cap >= @market_cap_min" in query
        assert "AND market_cap <= @market_cap_max" in query
        param_names = [p.name for p in params]
        assert "market_cap_min" in param_names
        assert "market_cap_max" in param_names

    # -- rsi_14 --------------------------------------------------------------

    def test_execute_screen_rsi_14_min(self):
        """rsi_14_min adds >= clause."""
        execute_screen(rsi_14_min=30.0)
        query, params = _get_last_query_and_params()
        assert "AND rsi_14 >= @rsi_14_min" in query

    def test_execute_screen_rsi_14_max(self):
        """rsi_14_max adds <= clause."""
        execute_screen(rsi_14_max=70.0)
        query, params = _get_last_query_and_params()
        assert "AND rsi_14 <= @rsi_14_max" in query

    def test_execute_screen_rsi_14_range(self):
        """Both rsi_14 bounds are included."""
        execute_screen(rsi_14_min=30.0, rsi_14_max=70.0)
        query, _ = _get_last_query_and_params()
        assert "AND rsi_14 >= @rsi_14_min" in query
        assert "AND rsi_14 <= @rsi_14_max" in query

    # -- macd_histogram ------------------------------------------------------

    def test_execute_screen_macd_histogram_min(self):
        """macd_histogram_min adds >= clause."""
        execute_screen(macd_histogram_min=0.5)
        query, _ = _get_last_query_and_params()
        assert "AND macd_histogram >= @macd_histogram_min" in query

    def test_execute_screen_macd_histogram_max(self):
        """macd_histogram_max adds <= clause."""
        execute_screen(macd_histogram_max=2.0)
        query, _ = _get_last_query_and_params()
        assert "AND macd_histogram <= @macd_histogram_max" in query

    # -- sma_cross_20_50 -----------------------------------------------------

    def test_execute_screen_sma_cross_min(self):
        """sma_cross_20_50_min adds >= clause."""
        execute_screen(sma_cross_20_50_min=1.0)
        query, _ = _get_last_query_and_params()
        assert "AND sma_cross_20_50 >= @sma_cross_20_50_min" in query

    def test_execute_screen_sma_cross_max(self):
        """sma_cross_20_50_max adds <= clause."""
        execute_screen(sma_cross_20_50_max=5.0)
        query, _ = _get_last_query_and_params()
        assert "AND sma_cross_20_50 <= @sma_cross_20_50_max" in query

    # -- pe_ratio ------------------------------------------------------------

    def test_execute_screen_pe_ratio_min(self):
        """pe_ratio_min adds >= clause."""
        execute_screen(pe_ratio_min=10.0)
        query, _ = _get_last_query_and_params()
        assert "AND pe_ratio >= @pe_ratio_min" in query

    def test_execute_screen_pe_ratio_max(self):
        """pe_ratio_max adds <= clause."""
        execute_screen(pe_ratio_max=50.0)
        query, _ = _get_last_query_and_params()
        assert "AND pe_ratio <= @pe_ratio_max" in query

    # -- revenue_growth_yoy --------------------------------------------------

    def test_execute_screen_revenue_growth_yoy(self):
        """revenue_growth_yoy_min adds >= clause."""
        execute_screen(revenue_growth_yoy_min=0.05)
        query, params = _get_last_query_and_params()
        assert "AND revenue_growth_yoy >= @revenue_growth_yoy_min" in query
        p = next(p for p in params if p.name == "revenue_growth_yoy_min")
        assert p.value == 0.05

    # -- hmm_regime ----------------------------------------------------------

    def test_execute_screen_hmm_regime_single(self):
        """Single hmm_regime value uses IN UNNEST with ArrayQueryParameter."""
        execute_screen(hmm_regime=["BULL_QUIET"])
        query, params = _get_last_query_and_params()
        assert "AND hmm_regime IN UNNEST(@hmm_regime)" in query
        p = next(p for p in params if p.name == "hmm_regime")
        assert p.values == ["BULL_QUIET"]

    def test_execute_screen_hmm_regime_multiple(self):
        """Multiple hmm_regime values are passed as array."""
        execute_screen(hmm_regime=["BULL_QUIET", "BULL_VOLATILE"])
        query, params = _get_last_query_and_params()
        assert "AND hmm_regime IN UNNEST(@hmm_regime)" in query
        p = next(p for p in params if p.name == "hmm_regime")
        assert p.values == ["BULL_QUIET", "BULL_VOLATILE"]

    # -- composite_score -----------------------------------------------------

    def test_execute_screen_composite_score_min(self):
        """composite_score_min adds >= clause."""
        execute_screen(composite_score_min=0.5)
        query, _ = _get_last_query_and_params()
        assert "AND composite_score >= @composite_score_min" in query

    # -- signal_label --------------------------------------------------------

    def test_execute_screen_signal_label(self):
        """signal_label adds = clause with ScalarQueryParameter."""
        execute_screen(signal_label="STRONG_BUY")
        query, params = _get_last_query_and_params()
        assert "AND signal_label = @signal_label" in query
        p = next(p for p in params if p.name == "signal_label")
        assert p.value == "STRONG_BUY"

    # -- bq_forecast_5d_pct --------------------------------------------------

    def test_execute_screen_bq_forecast_min(self):
        """bq_forecast_5d_pct_min adds >= clause."""
        execute_screen(bq_forecast_5d_pct_min=0.01)
        query, _ = _get_last_query_and_params()
        assert "AND bq_forecast_5d_pct >= @bq_forecast_5d_pct_min" in query


# ---------------------------------------------------------------------------
# Tests -- LIMIT behaviour
# ---------------------------------------------------------------------------


class TestExecuteScreenLimit:
    """Verify the limit parameter is clamped and defaults correctly."""

    def setup_method(self):
        _mock_bq_client.reset_mock()
        _setup_query_return([])

    def test_execute_screen_limit_default(self):
        """Default limit should be 20."""
        execute_screen()
        _, params = _get_last_query_and_params()
        limit_param = next(p for p in params if p.name == "limit")
        assert limit_param.value == 20

    def test_execute_screen_limit_custom(self):
        """Custom limit value is passed through."""
        execute_screen(limit=50)
        _, params = _get_last_query_and_params()
        limit_param = next(p for p in params if p.name == "limit")
        assert limit_param.value == 50

    def test_execute_screen_limit_clamped_high(self):
        """Limit > 100 is clamped to 100."""
        execute_screen(limit=500)
        _, params = _get_last_query_and_params()
        limit_param = next(p for p in params if p.name == "limit")
        assert limit_param.value == 100

    def test_execute_screen_limit_clamped_low(self):
        """Limit < 1 is clamped to 1."""
        execute_screen(limit=-5)
        _, params = _get_last_query_and_params()
        limit_param = next(p for p in params if p.name == "limit")
        assert limit_param.value == 1


# ---------------------------------------------------------------------------
# Tests -- ORDER BY
# ---------------------------------------------------------------------------


class TestExecuteScreenOrderBy:
    """Verify results are ordered by composite_score DESC."""

    def setup_method(self):
        _mock_bq_client.reset_mock()
        _setup_query_return([])

    def test_execute_screen_order_by(self):
        """Query must include ORDER BY composite_score DESC."""
        execute_screen()
        query, _ = _get_last_query_and_params()
        assert "ORDER BY composite_score DESC" in query


# ---------------------------------------------------------------------------
# Tests -- all filters at once
# ---------------------------------------------------------------------------


class TestExecuteScreenAllFilters:
    """Verify that all filters can be combined in a single call."""

    def setup_method(self):
        _mock_bq_client.reset_mock()
        _setup_query_return([])

    def test_execute_screen_all_filters(self):
        """Every filter parameter supplied at once should produce the correct query."""
        execute_screen(
            sector="Technology",
            industry="Semiconductors",
            market_cap_min=1e9,
            market_cap_max=5e12,
            rsi_14_min=30.0,
            rsi_14_max=70.0,
            macd_histogram_min=0.0,
            macd_histogram_max=3.0,
            sma_cross_20_50_min=-2.0,
            sma_cross_20_50_max=10.0,
            pe_ratio_min=5.0,
            pe_ratio_max=100.0,
            revenue_growth_yoy_min=0.1,
            hmm_regime=["BULL_QUIET", "BULL_VOLATILE"],
            composite_score_min=0.5,
            signal_label="STRONG_BUY",
            bq_forecast_5d_pct_min=0.02,
            limit=25,
        )
        query, params = _get_last_query_and_params()

        expected_clauses = [
            "AND sector = @sector",
            "AND industry = @industry",
            "AND market_cap >= @market_cap_min",
            "AND market_cap <= @market_cap_max",
            "AND rsi_14 >= @rsi_14_min",
            "AND rsi_14 <= @rsi_14_max",
            "AND macd_histogram >= @macd_histogram_min",
            "AND macd_histogram <= @macd_histogram_max",
            "AND sma_cross_20_50 >= @sma_cross_20_50_min",
            "AND sma_cross_20_50 <= @sma_cross_20_50_max",
            "AND pe_ratio >= @pe_ratio_min",
            "AND pe_ratio <= @pe_ratio_max",
            "AND revenue_growth_yoy >= @revenue_growth_yoy_min",
            "AND hmm_regime IN UNNEST(@hmm_regime)",
            "AND composite_score >= @composite_score_min",
            "AND signal_label = @signal_label",
            "AND bq_forecast_5d_pct >= @bq_forecast_5d_pct_min",
            "ORDER BY composite_score DESC LIMIT @limit",
        ]
        for clause in expected_clauses:
            assert clause in query, f"Missing clause: {clause}"

        # 17 filter params + 1 limit = 18 total
        assert len(params) == 18


# ---------------------------------------------------------------------------
# Tests -- response format and serialisation
# ---------------------------------------------------------------------------


class TestExecuteScreenResponse:
    """Verify response structure, serialisation, and error handling."""

    def setup_method(self):
        _mock_bq_client.reset_mock()

    def test_execute_screen_success_response(self, sample_screening_row):
        """Successful call returns {status, matches_found, results}."""
        _setup_query_return([sample_screening_row])
        result = execute_screen()
        assert result["status"] == "success"
        assert result["matches_found"] == 1
        assert isinstance(result["results"], list)
        assert len(result["results"]) == 1

    def test_execute_screen_empty_results(self):
        """When no rows match, matches_found is 0 and results is empty list."""
        _setup_query_return([])
        result = execute_screen()
        assert result["status"] == "success"
        assert result["matches_found"] == 0
        assert result["results"] == []

    def test_execute_screen_date_serialization(self):
        """date objects in rows are converted to strings."""
        row = {"ticker": "AAPL", "date": datetime.date(2026, 3, 4), "last_updated": None}
        _setup_query_return([row])
        result = execute_screen()
        assert result["results"][0]["date"] == "2026-03-04"

    def test_execute_screen_last_updated_serialization(self):
        """datetime (last_updated) objects are converted to strings."""
        row = {
            "ticker": "AAPL",
            "date": None,
            "last_updated": datetime.datetime(2026, 3, 4, 14, 30, 0),
        }
        _setup_query_return([row])
        result = execute_screen()
        assert result["results"][0]["last_updated"] == "2026-03-04 14:30:00"

    def test_execute_screen_bq_error(self):
        """BigQuery exception is caught and returns an error response."""
        _mock_bq_client.query.side_effect = Exception("BQ timeout")
        result = execute_screen()
        assert result["status"] == "error"
        assert "BQ timeout" in result["error_message"]
        # Clean up side_effect for subsequent tests
        _mock_bq_client.query.side_effect = None
