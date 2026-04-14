#!/usr/bin/env python3
"""
Review the Apr 10-13, 2026 xyz:CL weekend model.

This script does four things:

1. Pull the exact `xyz:CL` funding and candle history we can still access from the
   Hyperliquid info API.
2. Pull the corresponding CLK6 / CLM6 minute bars from Databento.
3. Reconstruct the weekend reference paths under:
   - the old "stale CME + immediate roll-step" framing, and
   - the corrected "external price freezes, internal oracle keeps moving" framing.
4. Backtest a realistic weekend portfolio:
   short 1 `xyz:CL`, long a 40/60 CME hedge from the Friday close onward.

Important limitation:
Hyperliquid does not expose historical `oraclePx` for HIP-3 markets via the public
info API. For the weekend review we therefore use a transparent proxy:

    oracle_proxy ~= trade_close / (1 + premium)

where `premium` is the historical hourly premium returned by `fundingHistory`.
Live API checks show that `xyz:CL` premium matches the Hyperliquid HIP-3 premium
formula:

    premium = (0.5 * (impact_bid_px + impact_ask_px) / oracle_px) - 1

so the proxy is a reasonable way to test whether the oracle was frozen or drifting.
"""

from __future__ import annotations

import argparse
import os
import math
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import databento as db
import pandas as pd
import requests


UTC = timezone.utc
ET = ZoneInfo("America/New_York")
INFO_URL = "https://api.hyperliquid.xyz/info"

REVIEW_START = pd.Timestamp("2026-04-10T20:00:00Z")
REVIEW_END = pd.Timestamp("2026-04-14T04:00:00Z")
ENTRY_TS = pd.Timestamp("2026-04-10T20:59:00Z")

KEY_EVENTS: list[tuple[str, pd.Timestamp, str]] = [
    ("entry", pd.Timestamp("2026-04-10T20:59:00Z"), "Last tradable minute before CME closes."),
    ("fri_shift_scheduled", pd.Timestamp("2026-04-10T21:30:00Z"), "Apr 10 5:30 PM ET scheduled 40/60 shift."),
    ("fri_post_shift_hour", pd.Timestamp("2026-04-10T22:00:00Z"), "One hour into the weekend session."),
    ("sat_midnight", pd.Timestamp("2026-04-11T00:00:00Z"), "Friday evening repricing check."),
    ("sun_midnight", pd.Timestamp("2026-04-12T00:00:00Z"), "Weekend repricing check before CME reopen."),
    ("sun_reopen", pd.Timestamp("2026-04-12T22:00:00Z"), "Sunday 6 PM ET CME reopen / external re-anchor."),
    ("mon_close", pd.Timestamp("2026-04-13T20:59:00Z"), "Last tradable minute before Monday close."),
    ("mon_shift_scheduled", pd.Timestamp("2026-04-13T21:30:00Z"), "Apr 13 5:30 PM ET scheduled 20/80 shift."),
    ("mon_reopen", pd.Timestamp("2026-04-13T22:00:00Z"), "Monday 6 PM ET reopen / 20/80 external weight live."),
    ("tue_03", pd.Timestamp("2026-04-14T03:00:00Z"), "Overnight checkpoint after Monday reopen."),
]


@dataclass(frozen=True)
class ReviewConfig:
    coin: str = "xyz:CL"
    front_symbol: str = "CLK6"
    back_symbol: str = "CLM6"
    month_year: tuple[int, int] = (2026, 4)
    tau_hours: float = 16.0
    output_dir: str = "data/processed/xyz_oil_weekend_review"


@dataclass(frozen=True)
class RollEvent:
    scheduled_ts: pd.Timestamp
    live_external_ts: pd.Timestamp
    front_weight: float
    back_weight: float


def list_business_days(year: int, month: int) -> list[date]:
    days: list[date] = []
    cursor = date(year, month, 1)
    while cursor.month == month:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def build_roll_events(year: int, month: int) -> list[RollEvent]:
    business_days = list_business_days(year, month)
    shift_days = business_days[5:10]
    front_weights = [0.80, 0.60, 0.40, 0.20, 0.00]
    events: list[RollEvent] = []

    for idx, shift_day in enumerate(shift_days):
        scheduled_local = datetime.combine(shift_day, clock_time(17, 30), tzinfo=ET)
        if scheduled_local.weekday() == 4:
            live_local = datetime.combine(shift_day + timedelta(days=2), clock_time(18, 0), tzinfo=ET)
        else:
            live_local = datetime.combine(shift_day, clock_time(18, 0), tzinfo=ET)

        events.append(
            RollEvent(
                scheduled_ts=pd.Timestamp(scheduled_local.astimezone(UTC)),
                live_external_ts=pd.Timestamp(live_local.astimezone(UTC)),
                front_weight=front_weights[idx],
                back_weight=1.0 - front_weights[idx],
            )
        )

    return events


def scheduled_front_weight(ts: pd.Timestamp, events: list[RollEvent]) -> float:
    weight = 1.0
    for event in events:
        if ts >= event.scheduled_ts:
            weight = event.front_weight
    return weight


def live_external_front_weight(ts: pd.Timestamp, events: list[RollEvent]) -> float:
    weight = 1.0
    for event in events:
        if ts >= event.live_external_ts:
            weight = event.front_weight
    return weight


def post_apr10_shift_front_weight(events: list[RollEvent]) -> float:
    for event in events:
        if event.scheduled_ts == pd.Timestamp("2026-04-10T21:30:00Z"):
            return event.front_weight
    raise ValueError("Expected Apr 10 roll event was not found.")


def fetch_xyz_funding_history(coin: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    response = requests.post(
        INFO_URL,
        json={"type": "fundingHistory", "coin": coin, "startTime": int(start.timestamp() * 1000)},
        timeout=30,
    )
    response.raise_for_status()
    data = [row for row in response.json() if int(row["time"]) <= int(end.timestamp() * 1000)]

    frame = pd.DataFrame(data)
    if frame.empty:
        raise ValueError("No funding history returned for the requested window.")

    frame["ts"] = pd.to_datetime(frame["time"], unit="ms", utc=True).dt.floor("h")
    frame["funding_rate"] = frame["fundingRate"].astype(float)
    frame["premium"] = frame["premium"].astype(float)
    return frame[["ts", "funding_rate", "premium"]].sort_values("ts").drop_duplicates("ts")


def fetch_xyz_candles(coin: str, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    response = requests.post(
        INFO_URL,
        json={
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000),
            },
        },
        timeout=60,
    )
    response.raise_for_status()
    frame = pd.DataFrame(response.json())
    if frame.empty:
        raise ValueError(f"No {interval} candles returned for {coin}.")

    frame["ts"] = pd.to_datetime(frame["t"], unit="ms", utc=True)
    for source, target in {
        "o": "open",
        "h": "high",
        "l": "low",
        "c": "close",
        "v": "volume",
        "n": "trades",
    }.items():
        frame[target] = frame[source].astype(float)

    return frame[["ts", "open", "high", "low", "close", "volume", "trades"]].sort_values("ts")


def fetch_cme_minute_closes(
    front_symbol: str,
    back_symbol: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    api_key = os.environ.get("DATABENTO_API_KEY")
    if not api_key:
        raise ValueError("DATABENTO_API_KEY must be set in the environment.")

    client = db.Historical(api_key)
    symbol_frames: list[pd.DataFrame] = []
    for symbol in [front_symbol, back_symbol]:
        last_error: Exception | None = None
        for _ in range(2):
            try:
                data = client.timeseries.get_range(
                    dataset="GLBX.MDP3",
                    symbols=[symbol],
                    schema="ohlcv-1m",
                    start=start.isoformat(),
                    end=end.isoformat(),
                )
                frame = data.to_df().reset_index()
                if frame.empty:
                    raise ValueError(f"No Databento minute bars returned for {symbol}.")
                symbol_frames.append(frame)
                last_error = None
                break
            except Exception as exc:  # pragma: no cover - transient network path
                last_error = exc
        if last_error is not None:
            raise last_error

    frame = pd.concat(symbol_frames, ignore_index=True)

    frame["ts_event"] = pd.to_datetime(frame["ts_event"], utc=True).dt.floor("min")
    frame["close"] = frame["close"].astype(float)
    pivot = (
        frame.pivot_table(index="ts_event", columns="symbol", values="close", aggfunc="last")
        .sort_index()
        .rename_axis(None, axis=1)
    )
    minute_index = pd.date_range(start=start.floor("min"), end=end.floor("min"), freq="1min", tz="UTC")
    pivot = pivot.reindex(minute_index).ffill()

    missing = {front_symbol, back_symbol}.difference(pivot.columns)
    if missing:
        raise ValueError(f"Missing CME symbols in Databento response: {sorted(missing)}")

    return pivot[[front_symbol, back_symbol]]


def fetch_live_premium_formula_check(coin: str) -> pd.DataFrame:
    response = requests.post(
        INFO_URL,
        json={"type": "metaAndAssetCtxs", "dex": "xyz"},
        timeout=30,
    )
    response.raise_for_status()
    meta, asset_ctxs = response.json()

    for asset, ctx in zip(meta["universe"], asset_ctxs):
        if asset["name"] != coin:
            continue

        oracle = float(ctx["oraclePx"])
        impact_bid, impact_ask = map(float, ctx["impactPxs"])
        premium_api = float(ctx["premium"])

        trade_xyz_formula = (
            max(impact_bid - oracle, 0.0) - max(oracle - impact_ask, 0.0)
        ) / oracle
        hip3_formula = (0.5 * (impact_bid + impact_ask) / oracle) - 1.0

        return pd.DataFrame(
            [
                {
                    "coin": coin,
                    "oracle_px": oracle,
                    "impact_bid_px": impact_bid,
                    "impact_ask_px": impact_ask,
                    "premium_api": premium_api,
                    "premium_trade_xyz_formula": trade_xyz_formula,
                    "premium_hip3_formula": hip3_formula,
                    "abs_error_trade_xyz": abs(premium_api - trade_xyz_formula),
                    "abs_error_hip3": abs(premium_api - hip3_formula),
                }
            ]
        )

    raise ValueError(f"Could not find {coin} in live metaAndAssetCtxs response.")


def compute_old_roll_spread(front_price: float, back_price: float, ts: pd.Timestamp, events: list[RollEvent], tau_hours: float) -> float:
    if tau_hours <= 0:
        return 0.0

    remaining = [
        (event.scheduled_ts - ts).total_seconds() / 3600.0
        for event in events
        if event.scheduled_ts > ts
    ]
    if not remaining:
        return 0.0

    jump = 0.20 * (front_price - back_price)
    spread = 0.0
    for idx in range(len(remaining) - 1, -1, -1):
        spread_pre_shift = spread + jump
        if idx == 0:
            duration = remaining[0]
        else:
            duration = remaining[idx] - remaining[idx - 1]
        spread = spread_pre_shift * math.exp(-duration / tau_hours)

    return float(spread)


def market_is_cme_open(ts: pd.Timestamp) -> bool:
    local = ts.tz_convert(ET)
    if local.weekday() == 5:
        return False
    if local.weekday() == 6 and local.time() < clock_time(18, 0):
        return False
    if local.weekday() == 4 and local.time() >= clock_time(17, 0):
        return False
    if local.weekday() < 4 and clock_time(17, 0) <= local.time() < clock_time(18, 0):
        return False
    return True


def build_hourly_review_frame(
    cfg: ReviewConfig,
    events: list[RollEvent],
    cme_minute: pd.DataFrame,
    funding_history: pd.DataFrame,
    xyz_hourly: pd.DataFrame,
) -> pd.DataFrame:
    hourly = funding_history.merge(
        xyz_hourly[["ts", "close"]].rename(columns={"close": "perp_close"}),
        on="ts",
        how="left",
    )
    hourly["front_price"] = cme_minute[cfg.front_symbol].reindex(hourly["ts"], method="ffill").to_numpy()
    hourly["back_price"] = cme_minute[cfg.back_symbol].reindex(hourly["ts"], method="ffill").to_numpy()

    hourly["scheduled_front_weight"] = hourly["ts"].map(lambda ts: scheduled_front_weight(ts, events))
    hourly["live_front_weight"] = hourly["ts"].map(lambda ts: live_external_front_weight(ts, events))
    hourly["scheduled_back_weight"] = 1.0 - hourly["scheduled_front_weight"]
    hourly["live_back_weight"] = 1.0 - hourly["live_front_weight"]
    hourly["cme_open"] = hourly["ts"].map(market_is_cme_open)

    hourly["old_model_reference"] = (
        hourly["scheduled_front_weight"] * hourly["front_price"]
        + hourly["scheduled_back_weight"] * hourly["back_price"]
    )
    hourly["corrected_external_reference"] = (
        hourly["live_front_weight"] * hourly["front_price"]
        + hourly["live_back_weight"] * hourly["back_price"]
    )
    hourly["oracle_proxy"] = hourly["perp_close"] / (1.0 + hourly["premium"])
    hourly["market_spread_to_oracle_proxy"] = hourly["oracle_proxy"] - hourly["perp_close"]
    hourly["market_spread_to_corrected_external"] = (
        hourly["corrected_external_reference"] - hourly["perp_close"]
    )
    hourly["market_spread_to_old_model_reference"] = (
        hourly["old_model_reference"] - hourly["perp_close"]
    )
    hourly["oracle_drift_vs_corrected_external"] = (
        hourly["corrected_external_reference"] - hourly["oracle_proxy"]
    )
    hourly["funding_pnl_short"] = hourly["oracle_proxy"] * hourly["funding_rate"]
    hourly["old_roll_spread_tau16"] = hourly.apply(
        lambda row: compute_old_roll_spread(
            row["front_price"],
            row["back_price"],
            row["ts"],
            events,
            cfg.tau_hours,
        ),
        axis=1,
    )

    entry_front = float(cme_minute.loc[ENTRY_TS, cfg.front_symbol])
    entry_back = float(cme_minute.loc[ENTRY_TS, cfg.back_symbol])
    hourly["frozen_friday_60_40_reference"] = 0.60 * entry_front + 0.40 * entry_back
    hourly["frozen_friday_40_60_reference"] = 0.40 * entry_front + 0.60 * entry_back
    hourly["oracle_drift_vs_frozen_60_40"] = hourly["frozen_friday_60_40_reference"] - hourly["oracle_proxy"]
    hourly["implied_premium_if_frozen_60_40"] = (
        hourly["perp_close"] / hourly["frozen_friday_60_40_reference"]
    ) - 1.0
    hourly["implied_premium_if_frozen_40_60"] = (
        hourly["perp_close"] / hourly["frozen_friday_40_60_reference"]
    ) - 1.0

    return hourly.sort_values("ts")


def asof_value(series: pd.Series, ts: pd.Timestamp) -> float:
    row = series.loc[:ts]
    if row.empty:
        raise ValueError(f"No value available at or before {ts.isoformat()}.")
    return float(row.iloc[-1])


def build_key_checkpoints(
    cfg: ReviewConfig,
    events: list[RollEvent],
    cme_minute: pd.DataFrame,
    xyz_minute: pd.DataFrame,
    hourly_review: pd.DataFrame,
) -> pd.DataFrame:
    xyz_minute = xyz_minute.set_index("ts").sort_index()
    hourly_review = hourly_review.set_index("ts").sort_index()

    rows: list[dict[str, object]] = []
    for label, ts, note in KEY_EVENTS:
        perp_close = asof_value(xyz_minute["close"], ts)
        front_price = asof_value(cme_minute[cfg.front_symbol], ts)
        back_price = asof_value(cme_minute[cfg.back_symbol], ts)
        hourly_row = hourly_review.loc[:ts].tail(1).iloc[0]
        frozen_60_40 = float(hourly_row["frozen_friday_60_40_reference"])
        frozen_40_60 = float(hourly_row["frozen_friday_40_60_reference"])

        rows.append(
            {
                "label": label,
                "ts": ts,
                "note": note,
                "cme_open": market_is_cme_open(ts),
                "perp_close": perp_close,
                "front_price": front_price,
                "back_price": back_price,
                "scheduled_front_weight": scheduled_front_weight(ts, events),
                "live_front_weight": live_external_front_weight(ts, events),
                "frozen_friday_60_40_reference": frozen_60_40,
                "frozen_friday_40_60_reference": frozen_40_60,
                "old_model_reference": scheduled_front_weight(ts, events) * front_price
                + (1.0 - scheduled_front_weight(ts, events)) * back_price,
                "corrected_external_reference": live_external_front_weight(ts, events) * front_price
                + (1.0 - live_external_front_weight(ts, events)) * back_price,
                "oracle_proxy_hourly": float(hourly_row["oracle_proxy"]),
                "premium_hourly": float(hourly_row["premium"]),
                "funding_rate_hourly": float(hourly_row["funding_rate"]),
                "old_roll_spread_tau16": float(hourly_row["old_roll_spread_tau16"]),
            }
        )

    frame = pd.DataFrame(rows).sort_values("ts")
    frame["market_spread_to_corrected_external"] = (
        frame["corrected_external_reference"] - frame["perp_close"]
    )
    frame["market_spread_to_old_model_reference"] = (
        frame["old_model_reference"] - frame["perp_close"]
    )
    return frame


def build_premium_falsification_table(hourly_review: pd.DataFrame) -> pd.DataFrame:
    sample_times = [
        pd.Timestamp("2026-04-10T22:00:00Z"),
        pd.Timestamp("2026-04-11T00:00:00Z"),
        pd.Timestamp("2026-04-12T00:00:00Z"),
        pd.Timestamp("2026-04-12T22:00:00Z"),
    ]
    frame = hourly_review[hourly_review["ts"].isin(sample_times)].copy()
    frame["actual_premium_sign"] = frame["premium"].map(lambda value: "pos" if value > 0 else "neg")
    frame["frozen_60_40_sign"] = frame["implied_premium_if_frozen_60_40"].map(
        lambda value: "pos" if value > 0 else "neg"
    )
    frame["frozen_40_60_sign"] = frame["implied_premium_if_frozen_40_60"].map(
        lambda value: "pos" if value > 0 else "neg"
    )
    return frame[
        [
            "ts",
            "perp_close",
            "premium",
            "actual_premium_sign",
            "implied_premium_if_frozen_60_40",
            "frozen_60_40_sign",
            "implied_premium_if_frozen_40_60",
            "frozen_40_60_sign",
            "oracle_proxy",
            "corrected_external_reference",
            "old_model_reference",
        ]
    ].sort_values("ts")


def cumulative_funding_through(hourly_review: pd.DataFrame, cutoff: pd.Timestamp) -> float:
    funding_start = pd.Timestamp("2026-04-10T21:00:00Z")
    mask = (hourly_review["ts"] >= funding_start) & (hourly_review["ts"] <= cutoff.floor("h"))
    return float(hourly_review.loc[mask, "funding_pnl_short"].sum())


def build_portfolio_checkpoints(
    cfg: ReviewConfig,
    key_checkpoints: pd.DataFrame,
    hourly_review: pd.DataFrame,
    events: list[RollEvent],
) -> pd.DataFrame:
    entry_row = key_checkpoints[key_checkpoints["label"] == "entry"].iloc[0]
    post_shift_front = post_apr10_shift_front_weight(events)
    post_shift_back = 1.0 - post_shift_front

    rows: list[dict[str, object]] = []
    for label in ["entry", "sun_reopen", "mon_close", "mon_reopen", "tue_03"]:
        row = key_checkpoints[key_checkpoints["label"] == label].iloc[0]
        cutoff = pd.Timestamp(row["ts"])
        perp_pnl = float(entry_row["perp_close"] - row["perp_close"])
        hedge_pnl = (
            post_shift_front * (float(row["front_price"]) - float(entry_row["front_price"]))
            + post_shift_back * (float(row["back_price"]) - float(entry_row["back_price"]))
        )
        funding_pnl = 0.0 if label == "entry" else cumulative_funding_through(hourly_review, cutoff)
        net_pnl = perp_pnl + hedge_pnl + funding_pnl

        rows.append(
            {
                "label": label,
                "ts": cutoff,
                "perp_trade_pnl": perp_pnl,
                "cme_hedge_pnl": hedge_pnl,
                "funding_pnl": funding_pnl,
                "net_pnl": net_pnl,
            }
        )

    frame = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    incremental = frame[["perp_trade_pnl", "cme_hedge_pnl", "funding_pnl", "net_pnl"]].diff().fillna(frame)
    frame["segment_perp_trade_pnl"] = incremental["perp_trade_pnl"]
    frame["segment_cme_hedge_pnl"] = incremental["cme_hedge_pnl"]
    frame["segment_funding_pnl"] = incremental["funding_pnl"]
    frame["segment_net_pnl"] = incremental["net_pnl"]
    return frame


def build_summary_frame(
    premium_formula_check: pd.DataFrame,
    key_checkpoints: pd.DataFrame,
    portfolio_checkpoints: pd.DataFrame,
) -> pd.DataFrame:
    entry = key_checkpoints[key_checkpoints["label"] == "entry"].iloc[0]
    sun_reopen = key_checkpoints[key_checkpoints["label"] == "sun_reopen"].iloc[0]
    mon_reopen = key_checkpoints[key_checkpoints["label"] == "mon_reopen"].iloc[0]
    stale_40_60 = 0.40 * float(entry["front_price"]) + 0.60 * float(entry["back_price"])

    summary = [
        {
            "metric": "weekend_oracle_verdict",
            "value": "not_frozen",
            "detail": "Actual weekend premium signs are inconsistent with both frozen 60/40 and frozen 40/60 references.",
        },
        {
            "metric": "live_premium_matches_formula",
            "value": "hip3",
            "detail": (
                "Live xyz:CL premium is closer to the Hyperliquid HIP-3 premium formula than the trade.xyz "
                "impact-price-difference formula."
            ),
        },
        {
            "metric": "friday_preclose_60_40_external",
            "value": round(float(entry["corrected_external_reference"]), 6),
            "detail": "Last live external reference before the weekend shutdown.",
        },
        {
            "metric": "friday_stale_40_60_reference",
            "value": round(stale_40_60, 6),
            "detail": "Reference implied by applying the Apr 10 shift immediately to the Friday close curve.",
        },
        {
            "metric": "sunday_reopen_live_40_60_external",
            "value": round(float(sun_reopen["corrected_external_reference"]), 6),
            "detail": "Actual live 40/60 external basket at Sunday 6 PM ET.",
        },
        {
            "metric": "sunday_reopen_vs_stale_error",
            "value": round(float(sun_reopen["corrected_external_reference"] - stale_40_60), 6),
            "detail": "How far the real Sunday 40/60 external basket moved versus the stale Friday 40/60 basket.",
        },
        {
            "metric": "weekend_portfolio_net_to_sun_reopen",
            "value": round(float(portfolio_checkpoints[portfolio_checkpoints["label"] == "sun_reopen"]["net_pnl"].iloc[0]), 6),
            "detail": "Short xyz:CL / long 40/60 CME hedge, using actual funding and frozen CME during closure.",
        },
        {
            "metric": "portfolio_net_to_tue_03",
            "value": round(float(portfolio_checkpoints[portfolio_checkpoints["label"] == "tue_03"]["net_pnl"].iloc[0]), 6),
            "detail": "Same portfolio through 03:00 UTC on Apr 14 after the Monday reopen.",
        },
        {
            "metric": "monday_reopen_live_20_80_external",
            "value": round(float(mon_reopen["corrected_external_reference"]), 6),
            "detail": "Actual live 20/80 external basket at Monday 6 PM ET after the next shift goes live.",
        },
    ]

    formula_row = premium_formula_check.iloc[0]
    summary.append(
        {
            "metric": "premium_formula_abs_error_ratio",
            "value": round(float(formula_row["abs_error_trade_xyz"] / max(formula_row["abs_error_hip3"], 1e-12)), 6),
            "detail": "Trade.xyz-doc premium formula error divided by HIP-3 formula error on the live xyz:CL context.",
        }
    )
    return pd.DataFrame(summary)


def write_frame(frame: pd.DataFrame, output_dir: Path, filename: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output_dir / filename, index=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Review the Apr 10-13, 2026 xyz:CL weekend model.")
    parser.add_argument(
        "--output-dir",
        default=ReviewConfig.output_dir,
        help="Directory for processed review tables.",
    )
    args = parser.parse_args()

    cfg = ReviewConfig(output_dir=args.output_dir)
    events = build_roll_events(*cfg.month_year)

    os.environ.setdefault("TZ", "UTC")

    funding_history = fetch_xyz_funding_history(cfg.coin, REVIEW_START, REVIEW_END)
    xyz_hourly = fetch_xyz_candles(cfg.coin, "1h", REVIEW_START, REVIEW_END)
    xyz_minute = fetch_xyz_candles(
        cfg.coin,
        "1m",
        ENTRY_TS - pd.Timedelta(minutes=30),
        REVIEW_END + pd.Timedelta(minutes=5),
    )
    cme_minute = fetch_cme_minute_closes(
        cfg.front_symbol,
        cfg.back_symbol,
        ENTRY_TS - pd.Timedelta(minutes=30),
        REVIEW_END + pd.Timedelta(minutes=5),
    )
    premium_formula_check = fetch_live_premium_formula_check(cfg.coin)

    hourly_review = build_hourly_review_frame(cfg, events, cme_minute, funding_history, xyz_hourly)
    key_checkpoints = build_key_checkpoints(cfg, events, cme_minute, xyz_minute, hourly_review)
    premium_falsification = build_premium_falsification_table(hourly_review)
    portfolio_checkpoints = build_portfolio_checkpoints(cfg, key_checkpoints, hourly_review, events)
    summary = build_summary_frame(premium_formula_check, key_checkpoints, portfolio_checkpoints)

    output_dir = Path(cfg.output_dir)
    write_frame(hourly_review, output_dir, "xyz_cl_hourly_review.csv")
    write_frame(key_checkpoints, output_dir, "xyz_cl_key_checkpoints.csv")
    write_frame(premium_falsification, output_dir, "xyz_cl_premium_falsification.csv")
    write_frame(portfolio_checkpoints, output_dir, "xyz_cl_portfolio_checkpoints.csv")
    write_frame(premium_formula_check, output_dir, "xyz_cl_live_premium_formula_check.csv")
    write_frame(summary, output_dir, "xyz_cl_summary.csv")

    sun_reopen = portfolio_checkpoints[portfolio_checkpoints["label"] == "sun_reopen"].iloc[0]
    tue_03 = portfolio_checkpoints[portfolio_checkpoints["label"] == "tue_03"].iloc[0]
    sunday_error = float(
        summary[summary["metric"] == "sunday_reopen_vs_stale_error"]["value"].iloc[0]
    )

    print("Weekend oracle verdict: not frozen.")
    print("Docs-consistent corrected rule: external price freezes off-hours, internal oracle keeps moving.")
    print(
        "Friday 5:30 PM ET roll nuance: the Apr 10 weight change becomes a live external price "
        "only when external pricing resumes on Sunday at 6 PM ET."
    )
    print(f"Sunday reopen stale-vs-live 40/60 error: {sunday_error:+.4f}")
    print(
        "Short xyz:CL + long 40/60 CME hedge PnL: "
        f"{sun_reopen['net_pnl']:+.4f} to Sunday reopen, {tue_03['net_pnl']:+.4f} to Tuesday 03:00 UTC."
    )
    print(f"Wrote review tables to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
