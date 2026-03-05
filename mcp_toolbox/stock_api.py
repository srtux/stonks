import google.auth
from google.cloud import bigquery
from typing import Dict, Any, Optional
import os

creds, project_id = google.auth.default()
# Fallback if ADK or local execution didn't set default project appropriately
project_id = project_id or os.getenv("GOOGLE_CLOUD_PROJECT")

# We use the BQ client here securely, isolated from the agent's LLM context.
# The `execute_screen` python function will be exposed to the Agent Engine as a Tool.
bq_client = bigquery.Client(credentials=creds, project=project_id)

def execute_screen(
    sector: Optional[str] = None,
    industry: Optional[str] = None,
    market_cap_min: Optional[float] = None,
    market_cap_max: Optional[float] = None,
    rsi_14_min: Optional[float] = None,
    rsi_14_max: Optional[float] = None,
    macd_histogram_min: Optional[float] = None,
    macd_histogram_max: Optional[float] = None,
    sma_cross_20_50_min: Optional[float] = None,
    sma_cross_20_50_max: Optional[float] = None,
    pe_ratio_min: Optional[float] = None,
    pe_ratio_max: Optional[float] = None,
    revenue_growth_yoy_min: Optional[float] = None,
    hmm_regime: Optional[list[str]] = None,
    composite_score_min: Optional[float] = None,
    signal_label: Optional[str] = None,
    bq_forecast_5d_pct_min: Optional[float] = None,
    limit: int = 20
) -> Dict[str, Any]:
    """
    Executes a stock screen securely against the latest_screening_master view.
    
    Args:
        sector: e.g. 'Technology', 'Healthcare'
        industry: e.g. 'Software - Infrastructure'
        market_cap_min: Minimum market cap in dollars
        market_cap_max: Maximum market cap in dollars
        rsi_14_min: Minimum 14-day RSI (0-100)
        rsi_14_max: Maximum 14-day RSI (0-100)
        macd_histogram_min: Minimum MACD Histogram value
        macd_histogram_max: Maximum MACD Histogram value
        sma_cross_20_50_min: Minimum distance between 20 & 50 SMA (positive = bullish cross)
        sma_cross_20_50_max: Maximum distance between 20 & 50 SMA
        pe_ratio_min: Minimum P/E ratio
        pe_ratio_max: Maximum P/E ratio
        revenue_growth_yoy_min: Minimum YoY revenue growth (e.g. 0.05 for 5%)
        hmm_regime: List of acceptable regimes (e.g. ['BULL_QUIET', 'BULL_VOLATILE'])
        composite_score_min: Minimum composite quant score (-1 to 1)
        signal_label: Specific signal bucket (e.g. 'STRONG_BUY', 'SELL')
        bq_forecast_5d_pct_min: Minimum 5-day forecasted percentage change
        limit: Max results to return (default 20, max 100)
        
    Returns:
        Dictionary with matching tickers and their profile data.
    """
    
    # Base query against the safe View (never the raw table without a date constraint)
    query = "SELECT * FROM `amfe_data.latest_screening_master` WHERE 1=1"
    
    query_params = []
    
    # Dynamically build securely parameterized query
    if sector:
        query += " AND sector = @sector"
        query_params.append(bigquery.ScalarQueryParameter("sector", "STRING", sector))
    if industry:
        query += " AND industry = @industry"
        query_params.append(bigquery.ScalarQueryParameter("industry", "STRING", industry))
        
    if market_cap_min is not None:
        query += " AND market_cap >= @market_cap_min"
        query_params.append(bigquery.ScalarQueryParameter("market_cap_min", "FLOAT64", market_cap_min))
    if market_cap_max is not None:
        query += " AND market_cap <= @market_cap_max"
        query_params.append(bigquery.ScalarQueryParameter("market_cap_max", "FLOAT64", market_cap_max))
        
    if rsi_14_min is not None:
        query += " AND rsi_14 >= @rsi_14_min"
        query_params.append(bigquery.ScalarQueryParameter("rsi_14_min", "FLOAT64", rsi_14_min))
    if rsi_14_max is not None:
        query += " AND rsi_14 <= @rsi_14_max"
        query_params.append(bigquery.ScalarQueryParameter("rsi_14_max", "FLOAT64", rsi_14_max))
        
    if macd_histogram_min is not None:
         query += " AND macd_histogram >= @macd_histogram_min"
         query_params.append(bigquery.ScalarQueryParameter("macd_histogram_min", "FLOAT64", macd_histogram_min))
    if macd_histogram_max is not None:
         query += " AND macd_histogram <= @macd_histogram_max"
         query_params.append(bigquery.ScalarQueryParameter("macd_histogram_max", "FLOAT64", macd_histogram_max))

    if sma_cross_20_50_min is not None:
        query += " AND sma_cross_20_50 >= @sma_cross_20_50_min"
        query_params.append(bigquery.ScalarQueryParameter("sma_cross_20_50_min", "FLOAT64", sma_cross_20_50_min))
    if sma_cross_20_50_max is not None:
        query += " AND sma_cross_20_50 <= @sma_cross_20_50_max"
        query_params.append(bigquery.ScalarQueryParameter("sma_cross_20_50_max", "FLOAT64", sma_cross_20_50_max))
        
    if pe_ratio_min is not None:
        query += " AND pe_ratio >= @pe_ratio_min"
        query_params.append(bigquery.ScalarQueryParameter("pe_ratio_min", "FLOAT64", pe_ratio_min))
    if pe_ratio_max is not None:
        query += " AND pe_ratio <= @pe_ratio_max"
        query_params.append(bigquery.ScalarQueryParameter("pe_ratio_max", "FLOAT64", pe_ratio_max))
        
    if revenue_growth_yoy_min is not None:
        query += " AND revenue_growth_yoy >= @revenue_growth_yoy_min"
        query_params.append(bigquery.ScalarQueryParameter("revenue_growth_yoy_min", "FLOAT64", revenue_growth_yoy_min))
        
    if hmm_regime:
        query += " AND hmm_regime IN UNNEST(@hmm_regime)"
        query_params.append(bigquery.ArrayQueryParameter("hmm_regime", "STRING", hmm_regime))
        
    if composite_score_min is not None:
        query += " AND composite_score >= @composite_score_min"
        query_params.append(bigquery.ScalarQueryParameter("composite_score_min", "FLOAT64", composite_score_min))
        
    if signal_label:
        query += " AND signal_label = @signal_label"
        query_params.append(bigquery.ScalarQueryParameter("signal_label", "STRING", signal_label))
        
    if bq_forecast_5d_pct_min is not None:
        query += " AND bq_forecast_5d_pct >= @bq_forecast_5d_pct_min"
        query_params.append(bigquery.ScalarQueryParameter("bq_forecast_5d_pct_min", "FLOAT64", bq_forecast_5d_pct_min))

    # Ensure limit is sensible
    safe_limit = min(max(1, limit), 100)
    query += " ORDER BY composite_score DESC LIMIT @limit"
    query_params.append(bigquery.ScalarQueryParameter("limit", "INT64", safe_limit))

    job_config = bigquery.QueryJobConfig(query_parameters=query_params)
    
    try:
        results = bq_client.query(query, job_config=job_config).result()
        rows = [dict(row.items()) for row in results]
        
        # Datetime objects need string conversion for JSON serialization back to Agent Engine
        for row in rows:
            if 'date' in row and row['date']:
                 row['date'] = str(row['date'])
            if 'last_updated' in row and row['last_updated']:
                 row['last_updated'] = str(row['last_updated'])
                 
        return {
            "status": "success",
            "matches_found": len(rows),
            "results": rows
        }
    except Exception as e:
        return {
            "status": "error",
            "error_message": str(e)
        }
