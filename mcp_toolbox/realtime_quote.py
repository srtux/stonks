import yfinance as yf
from google.cloud import bigquery
from typing import Dict, Any
import google.auth
import os

creds, project_id = google.auth.default()
project_id = project_id or os.getenv("GOOGLE_CLOUD_PROJECT")
bq_client = bigquery.Client(credentials=creds, project=project_id)

def get_stock_profile(ticker: str) -> Dict[str, Any]:
    """
    Fetches the latest pre-computed batch signals from BigQuery AND
    the real-time intraday quote to provide agents with complete context.
    
    Args:
        ticker: Stock symbol (e.g., 'NVDA', 'AAPL')
        
    Returns:
        Dictionary containing both 'batch_signals' and 'realtime_quote'
    """
    ticker = ticker.upper().strip()
    
    result = {
        "ticker": ticker,
        "batch_signals": None,
        "realtime_quote": None,
        "status": "success"
    }
    
    # 1. Fetch Batch Signals from BQ View
    try:
        query = """
            SELECT * FROM `amfe_data.latest_screening_master`
            WHERE ticker = @ticker
            LIMIT 1
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("ticker", "STRING", ticker)
            ]
        )
        bq_rows = list(bq_client.query(query, job_config=job_config).result())
        
        if bq_rows:
            row_dict = dict(bq_rows[0].items())
            if 'date' in row_dict and row_dict['date']:
                 row_dict['date'] = str(row_dict['date'])
            if 'last_updated' in row_dict and row_dict['last_updated']:
                 row_dict['last_updated'] = str(row_dict['last_updated'])
            result["batch_signals"] = row_dict
        else:
            result["batch_signals"] = {"error": "No batch data found for this ticker."}
    except Exception as e:
        result["batch_signals"] = {"error": f"BQ fetch failed: {str(e)}"}
        result["status"] = "partial_failure"

    # 2. Fetch Real-time data (Lightweight YFinance call)
    try:
        stock = yf.Ticker(ticker)
        # Fast history fetch for just the last 1 day
        hist = stock.history(period="1d")
        
        if not hist.empty:
            current_price = float(hist['Close'].iloc[-1])
            open_price = float(hist['Open'].iloc[-1])
            intraday_pct = ((current_price - open_price) / open_price)
            
            result["realtime_quote"] = {
                "current_price": round(current_price, 2),
                "open_price": round(open_price, 2),
                "intraday_pct_change": round(intraday_pct, 4)
            }
        else:
            result["realtime_quote"] = {"error": "No realtime trade data found today."}
    except Exception as e:
        result["realtime_quote"] = {"error": f"Realtime fetch failed: {str(e)}"}
        result["status"] = "partial_failure"
        
    return result
