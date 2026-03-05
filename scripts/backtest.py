"""
backtest.py — Walk-forward backtesting of agent decisions.

For each decision in amfe_data.agent_decisions, looks up the actual price
movement over the following 5 trading days in amfe_data.ohlcv_daily and
computes accuracy / return metrics.

Usage:
    python scripts/backtest.py
"""

from __future__ import annotations

import os

import pandas as pd
from dotenv import load_dotenv
from google.cloud import bigquery

load_dotenv()

PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
DATASET = os.environ.get("BQ_DATASET", "amfe_data")

bq_client = bigquery.Client(project=PROJECT)

# ── Queries ───────────────────────────────────────────────────────────────────

DECISIONS_QUERY = f"""
SELECT
    decision_id,
    ticker,
    action AS signal_label,
    agent_mode,
    reasoning,
    timestamp AS decision_timestamp,
    confidence_score AS confidence
FROM `{PROJECT}.{DATASET}.agent_decisions`
ORDER BY decision_timestamp
"""

OHLCV_QUERY = f"""
SELECT
    ticker,
    date AS trade_date,
    close
FROM `{PROJECT}.{DATASET}.ohlcv_daily`
ORDER BY ticker, timestamp
"""

# ── Data loading ──────────────────────────────────────────────────────────────


def load_decisions() -> pd.DataFrame:
    """Load agent decisions from BigQuery."""
    df = bq_client.query(DECISIONS_QUERY).to_dataframe()
    df["decision_timestamp"] = pd.to_datetime(df["decision_timestamp"])
    return df


def load_ohlcv() -> pd.DataFrame:
    """Load daily OHLCV closes from BigQuery."""
    df = bq_client.query(OHLCV_QUERY).to_dataframe()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


# ── Analysis ──────────────────────────────────────────────────────────────────

FORWARD_DAYS = 5


def compute_forward_returns(decisions: pd.DataFrame, ohlcv: pd.DataFrame) -> pd.DataFrame:
    """
    For each decision, find the closing price on the decision date and
    the closing price 5 trading days later, then compute the forward return.
    """
    results = []

    for ticker, group in ohlcv.groupby("ticker"):
        group = group.sort_values("trade_date").reset_index(drop=True)
        ticker_decisions = decisions[decisions["ticker"] == ticker]

        for _, dec in ticker_decisions.iterrows():
            dec_date = dec["decision_timestamp"].normalize()

            # Find the closest trading day on or after the decision date
            mask_entry = group["trade_date"] >= dec_date
            if mask_entry.sum() == 0:
                continue
            entry_idx = group.loc[mask_entry].index[0]
            entry_price = group.loc[entry_idx, "close"]

            # Forward exit: FORWARD_DAYS trading days later
            exit_idx = entry_idx + FORWARD_DAYS
            if exit_idx >= len(group):
                continue
            exit_price = group.loc[exit_idx, "close"]

            forward_return = (exit_price - entry_price) / entry_price

            results.append(
                {
                    "decision_id": dec["decision_id"],
                    "ticker": ticker,
                    "signal_label": dec["signal_label"],
                    "agent_mode": dec["agent_mode"],
                    "hmm_regime": dec["hmm_regime"],
                    "confidence": dec.get("confidence"),
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "forward_return": forward_return,
                }
            )

    return pd.DataFrame(results)


def print_summary(df: pd.DataFrame) -> None:
    """Print formatted summary tables grouped by key dimensions."""
    if df.empty:
        print("No results to summarize.")
        return

    # Tag whether price moved in the direction of the signal
    df["is_buy"] = df["signal_label"].str.upper() == "BUY"
    df["is_sell"] = df["signal_label"].str.upper() == "SELL"
    df["hit"] = (
        (df["is_buy"] & (df["forward_return"] > 0))
        | (df["is_sell"] & (df["forward_return"] < 0))
    )

    buys = df[df["is_buy"]]
    sells = df[df["is_sell"]]

    # ── Overall metrics ───────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("OVERALL BACKTEST METRICS")
    print("=" * 70)
    print(f"  Total decisions evaluated : {len(df)}")
    print(f"  BUY decisions             : {len(buys)}")
    print(f"  SELL decisions            : {len(sells)}")
    if len(buys) > 0:
        hit_rate = buys["hit"].mean() * 100
        avg_ret = buys["forward_return"].mean() * 100
        print(f"  BUY hit rate              : {hit_rate:.1f}%")
        print(f"  Avg return on BUYs        : {avg_ret:+.2f}%")
    if len(sells) > 0:
        sell_hit = sells["hit"].mean() * 100
        avg_sell_ret = sells["forward_return"].mean() * 100
        print(f"  SELL hit rate              : {sell_hit:.1f}%")
        print(f"  Avg return on SELLs       : {avg_sell_ret:+.2f}%")

    # ── Grouped summaries ─────────────────────────────────────────────
    groupings = {
        "agent_mode": "AGENT MODE",
        "hmm_regime": "HMM REGIME",
        "signal_label": "SIGNAL LABEL",
    }

    for col, title in groupings.items():
        if col not in df.columns or df[col].isna().all():
            continue

        print(f"\n{'─' * 70}")
        print(f"BY {title}")
        print("─" * 70)

        summary = (
            df.groupby(col)
            .agg(
                count=("forward_return", "size"),
                hit_rate=("hit", "mean"),
                avg_return=("forward_return", "mean"),
                median_return=("forward_return", "median"),
                std_return=("forward_return", "std"),
            )
            .reset_index()
        )
        summary["hit_rate"] = (summary["hit_rate"] * 100).round(1).astype(str) + "%"
        summary["avg_return"] = (summary["avg_return"] * 100).round(2).astype(str) + "%"
        summary["median_return"] = (summary["median_return"] * 100).round(2).astype(str) + "%"
        summary["std_return"] = (summary["std_return"] * 100).round(2).astype(str) + "%"

        print(summary.to_string(index=False))


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    print("Loading agent decisions from BigQuery...")
    decisions = load_decisions()
    print(f"  {len(decisions)} decisions loaded.")

    print("Loading OHLCV data from BigQuery...")
    ohlcv = load_ohlcv()
    print(f"  {len(ohlcv)} price rows loaded.")

    print("Computing 5-day forward returns...")
    results = compute_forward_returns(decisions, ohlcv)
    print(f"  {len(results)} decisions matched to price data.")

    print_summary(results)


if __name__ == "__main__":
    main()
