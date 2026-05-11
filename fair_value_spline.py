#!/usr/bin/env python3
"""
Hyperliquid Oil Perp — Real-Time Fair Value Spline
==================================================

Polls CME futures prices via Databento and Hyperliquid perp data via REST API.
Computes the amortized fair value spline in real time, logs actual vs theoretical
spread, tracks a paper position, and estimates implied tau from observed market
behavior.

Usage:
    uv add databento requests yfinance
    export DATABENTO_API_KEY="your_key_here"
    python fair_value_spline.py

Or for demo mode without API keys:
    python fair_value_spline.py --demo --iterations 1
"""

import argparse
import logging
import math
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
from typing import Deque, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import requests

UTC = timezone.utc
NY_TZ = ZoneInfo("America/New_York")

MONTH_CODES = {
    1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
    7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z",
}
ROLL_FRONT_WEIGHTS = [0.80, 0.60, 0.40, 0.20, 0.00]


def add_months(year: int, month: int, increment: int) -> Tuple[int, int]:
    """Return (year, month) shifted by `increment` calendar months."""
    month_index = (year * 12 + (month - 1)) + increment
    next_year, next_month_zero = divmod(month_index, 12)
    return next_year, next_month_zero + 1


def contract_symbol(root: str, year: int, month: int) -> str:
    """Build Databento-style symbols like CLK6 or CLN6."""
    return f"{root}{MONTH_CODES[month]}{str(year)[-1]}"


def easter_date(year: int) -> date:
    """Return Western Easter Sunday for Gregorian calendar years."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def observed_fixed_holiday(year: int, month: int, day: int) -> date:
    holiday = date(year, month, day)
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    cursor = date(year, month, 1)
    while cursor.weekday() != weekday:
        cursor += timedelta(days=1)
    return cursor + timedelta(days=7 * (occurrence - 1))


def last_weekday(year: int, month: int, weekday: int) -> date:
    next_month_year, next_month = add_months(year, month, 1)
    cursor = date(next_month_year, next_month, 1) - timedelta(days=1)
    while cursor.weekday() != weekday:
        cursor -= timedelta(days=1)
    return cursor


def nymex_full_close_holidays(year: int) -> set[date]:
    """Approximate full NYMEX/CME energy-market holidays relevant to roll counts."""
    return {
        observed_fixed_holiday(year, 1, 1),
        nth_weekday(year, 1, 0, 3),          # Martin Luther King Jr. Day
        nth_weekday(year, 2, 0, 3),          # Presidents Day
        easter_date(year) - timedelta(days=2),
        last_weekday(year, 5, 0),            # Memorial Day
        observed_fixed_holiday(year, 6, 19),
        observed_fixed_holiday(year, 7, 4),
        nth_weekday(year, 9, 0, 1),          # Labor Day
        nth_weekday(year, 11, 3, 4),         # Thanksgiving
        observed_fixed_holiday(year, 12, 25),
    }


def list_business_days(year: int, month: int) -> List[date]:
    """Return exchange business days in the given month for trade.xyz roll timing."""
    days: List[date] = []
    cursor = date(year, month, 1)
    holidays = nymex_full_close_holidays(year)
    while cursor.month == month:
        if cursor.weekday() < 5 and cursor not in holidays:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def parse_iso_datetime(value: str) -> datetime:
    """Parse an ISO timestamp and default naive values to UTC."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class OracleShift:
    """A single oracle weight shift."""

    datetime_utc: datetime
    front_symbol: str
    back_symbol: str
    front_weight: float
    back_weight: float


@dataclass(frozen=True)
class RollWindow:
    """The nearest upcoming roll period that matters for the spline."""

    front_symbol: str
    back_symbol: str
    shifts: List[OracleShift]
    label: str


@dataclass
class RollConfig:
    """Configuration for the oil roll model."""

    hl_asset: str = "xyz:CL"
    tau: float = 16.0  # Funding time constant in hours, not time-to-next-shift.
    symbol_root: str = "CL"
    signal_threshold: float = 0.10
    position_size: float = 1.0
    tau_window: int = 24


class WTIRollScheduleBuilder:
    """
    Build the trade.xyz WTI roll schedule dynamically.

    trade.xyz references the next calendar-month WTI future at the start of each
    month. The protocol roll period spans the 5th through 10th exchange business
    days, implemented as five 20% transitions beginning on the 5th business day
    at 5:30 PM New York time.
    """

    def __init__(self, root_symbol: str = "CL"):
        self.root_symbol = root_symbol

    def _month_start_pair(self, year: int, month: int) -> Tuple[str, str]:
        front_year, front_month = add_months(year, month, 1)
        back_year, back_month = add_months(year, month, 2)
        return (
            contract_symbol(self.root_symbol, front_year, front_month),
            contract_symbol(self.root_symbol, back_year, back_month),
        )

    def build_roll_window(self, year: int, month: int) -> RollWindow:
        front_symbol, back_symbol = self._month_start_pair(year, month)
        business_days = list_business_days(year, month)
        # Five transitions cover the protocol's 5th-10th business-day roll period:
        # after the 9th business-day transition, the 10th business day is fully rolled.
        shift_days = business_days[4:9]
        shifts: List[OracleShift] = []

        for idx, shift_day in enumerate(shift_days):
            local_dt = datetime.combine(shift_day, clock_time(17, 30), tzinfo=NY_TZ)
            shifts.append(
                OracleShift(
                    datetime_utc=local_dt.astimezone(UTC),
                    front_symbol=front_symbol,
                    back_symbol=back_symbol,
                    front_weight=ROLL_FRONT_WEIGHTS[idx],
                    back_weight=1.0 - ROLL_FRONT_WEIGHTS[idx],
                )
            )

        label = f"{front_symbol}->{back_symbol} roll ({year}-{month:02d})"
        return RollWindow(
            front_symbol=front_symbol,
            back_symbol=back_symbol,
            shifts=shifts,
            label=label,
        )

    def upcoming_roll_window(self, now: datetime) -> RollWindow:
        local_now = now.astimezone(NY_TZ)
        current_window = self.build_roll_window(local_now.year, local_now.month)
        if now <= current_window.shifts[-1].datetime_utc:
            return current_window

        next_year, next_month = add_months(local_now.year, local_now.month, 1)
        return self.build_roll_window(next_year, next_month)


class HyperliquidClient:
    """Fetch mark price, oracle price, and funding from Hyperliquid."""

    BASE_URL = "https://api.hyperliquid.xyz/info"

    def __init__(self, asset_name: str = "xyz:CL"):
        self.asset_name = asset_name
        self.dex_name = asset_name.split(":")[0] if ":" in asset_name else None
        self.coin_name = asset_name.split(":")[1] if ":" in asset_name else asset_name

    def _make_request(self, payload: dict):
        response = requests.post(self.BASE_URL, json=payload, timeout=10)
        response.raise_for_status()
        return response.json()

    def _search_meta_payload(self, meta_data) -> Optional[dict]:
        preferred_names = {self.asset_name, self.coin_name}

        if isinstance(meta_data, list) and len(meta_data) >= 2 and isinstance(meta_data[0], dict):
            candidate_pairs = [(meta_data[0], meta_data[1])]
        else:
            candidate_pairs = []
            if isinstance(meta_data, list):
                for dex_idx in range(0, len(meta_data), 2):
                    meta = meta_data[dex_idx] if dex_idx < len(meta_data) else None
                    ctxs = meta_data[dex_idx + 1] if dex_idx + 1 < len(meta_data) else []
                    candidate_pairs.append((meta, ctxs))

        for meta, ctxs in candidate_pairs:
            if not meta or "universe" not in meta:
                continue

            for asset_idx, asset in enumerate(meta["universe"]):
                name = asset.get("name", "")
                if name not in preferred_names or asset_idx >= len(ctxs):
                    continue
                ctx = ctxs[asset_idx]
                impact_prices = ctx.get("impactPxs") or [0, 0]
                return {
                    "markPx": float(ctx.get("markPx", 0)),
                    "oraclePx": float(ctx.get("oraclePx", 0)),
                    "funding": float(ctx.get("funding", 0)),
                    "premium": float(ctx.get("premium", 0)),
                    "midPx": float(ctx.get("midPx", 0)),
                    "openInterest": float(ctx.get("openInterest", 0)),
                    "impactBid": float(impact_prices[0]),
                    "impactAsk": float(impact_prices[1]),
                }
        return None

    def get_perp_data(self) -> Optional[dict]:
        """
        Return current mark, oracle, premium, funding, and impact prices.

        For HIP-3 markets like `xyz:CL`, `metaAndAssetCtxs` contains the context
        data we need.
        """
        try:
            if self.dex_name:
                direct = self._search_meta_payload(
                    self._make_request({"type": "metaAndAssetCtxs", "dex": self.dex_name})
                )
                if direct is not None:
                    return direct

            all_mids = self._make_request({"type": "allMids"})
            searched = self._search_meta_payload(self._make_request({"type": "metaAndAssetCtxs"}))
            if searched is not None:
                return searched

            mid = all_mids.get(self.asset_name, all_mids.get(self.coin_name))
            if mid is not None:
                mid_float = float(mid)
                return {
                    "markPx": mid_float,
                    "oraclePx": 0.0,
                    "funding": 0.0,
                    "premium": 0.0,
                    "midPx": mid_float,
                    "openInterest": 0.0,
                    "impactBid": mid_float,
                    "impactAsk": mid_float,
                }
            return None
        except Exception as exc:
            logging.error("Hyperliquid API error: %s", exc)
            return None


class DatabentoClient:
    """Fetch near-real-time CME futures prices via Databento Historical."""

    def __init__(self, api_key: Optional[str] = None, snapshot_delay_minutes: int = 45):
        self.api_key = api_key or os.environ.get("DATABENTO_API_KEY")
        self._db = None
        self.snapshot_delay_minutes = snapshot_delay_minutes

    def _get_client(self):
        if self._db is None:
            try:
                import databento as db
            except ImportError:
                logging.warning("databento package not installed.")
                return None

            if not self.api_key:
                logging.warning("DATABENTO_API_KEY not set.")
                return None

            self._db = db.Historical(self.api_key)

        return self._db

    def get_futures_prices(self, symbols: Sequence[str]) -> Optional[Dict[str, float]]:
        """
        Fetch the latest traded price for each requested CME symbol.

        Returns a mapping like {"CLK6": 95.97, "CLM6": 87.53}.
        """
        client = self._get_client()
        if client is None:
            return None

        try:
            end = datetime.now(UTC) - timedelta(minutes=self.snapshot_delay_minutes)
            start = end - timedelta(hours=2)
            data = client.timeseries.get_range(
                dataset="GLBX.MDP3",
                symbols=list(symbols),
                schema="trades",
                start=start.isoformat(),
                end=end.isoformat(),
                limit=20000,
            )

            df = data.to_df().reset_index()
            prices: Dict[str, float] = {}
            for symbol in symbols:
                symbol_rows = df[df["symbol"] == symbol]
                if len(symbol_rows) == 0:
                    continue
                prices[symbol] = float(symbol_rows.iloc[-1]["price"])

            return prices if len(prices) == len(symbols) else None
        except Exception as exc:
            logging.error("Databento API error: %s", exc)
            return None


class YahooFinanceClient:
    """Fetch delayed CME snapshots from Yahoo Finance contract pages."""

    MONTH_PATTERN = re.compile(r"^([A-Z]+)([FGHJKMNQUVXZ])(\d)$")

    def _to_yahoo_symbol(self, symbol: str) -> str:
        match = self.MONTH_PATTERN.match(symbol)
        if not match:
            raise ValueError(f"Unsupported futures symbol format: {symbol}")

        root, month_code, year_digit_text = match.groups()
        year_digit = int(year_digit_text)
        current_year = datetime.now(UTC).year
        candidate_year = (current_year // 10) * 10 + year_digit
        if candidate_year < current_year - 5:
            candidate_year += 10
        elif candidate_year > current_year + 5:
            candidate_year -= 10
        return f"{root}{month_code}{str(candidate_year)[-2:]}.NYM"

    def get_futures_prices(self, symbols: Sequence[str]) -> Optional[Dict[str, float]]:
        try:
            import yfinance as yf
        except ImportError:
            logging.warning("yfinance package not installed.")
            return None

        prices: Dict[str, float] = {}
        for symbol in symbols:
            yahoo_symbol = self._to_yahoo_symbol(symbol)
            ticker = yf.Ticker(yahoo_symbol)
            history = ticker.history(period="2d", interval="1m")
            if history.empty:
                fast_info = getattr(ticker, "fast_info", None)
                if not fast_info:
                    return None
                last_price = fast_info.get("lastPrice")
                if last_price is None:
                    return None
                prices[symbol] = float(last_price)
            else:
                prices[symbol] = float(history.iloc[-1]["Close"])

        return prices if len(prices) == len(symbols) else None


class AutoFuturesClient:
    """Try Databento first, then fall back to Yahoo Finance snapshots."""

    def __init__(self):
        self.sources = [
            ("databento", DatabentoClient()),
            ("yahoo", YahooFinanceClient()),
        ]

    def get_futures_prices(self, symbols: Sequence[str]) -> Optional[Dict[str, float]]:
        for source_name, source in self.sources:
            prices = source.get_futures_prices(symbols)
            if prices is not None:
                if source_name != "databento":
                    logging.info("Using %s fallback for CME snapshot.", source_name)
                return prices
        return None


class ManualPriceSource:
    """Manual/demo source for testing without API keys."""

    def __init__(self, f1=95.70, f2=87.00, mark=94.30, oracle=95.10, funding=-0.000537):
        self.f1 = f1
        self.f2 = f2
        self.mark = mark
        self.oracle = oracle
        self.funding = funding

    def get_futures_prices(self, symbols: Sequence[str]) -> Dict[str, float]:
        front_symbol, back_symbol = list(symbols)
        return {
            front_symbol: self.f1,
            back_symbol: self.f2,
        }

    def get_perp_data(self) -> dict:
        premium = (self.mark - self.oracle) / self.oracle if self.oracle else 0.0
        return {
            "markPx": self.mark,
            "oraclePx": self.oracle,
            "funding": self.funding,
            "premium": premium,
            "midPx": self.mark,
            "openInterest": 1000,
            "impactBid": self.mark - 0.05,
            "impactAsk": self.mark + 0.05,
        }

# ============================================================
# FAIR VALUE SPLINE ENGINE
# ============================================================

class FairValueSpline:
    """
    Compute the amortized roll component of the Hyperliquid mark-oracle spread.

    The model uses the nearest relevant trade.xyz roll window. Before the month's
    final shift, that is the current month's roll. Immediately after the month's
    final shift, it automatically rolls forward to the next month's schedule so the
    spline can still be evaluated even when the next shift is weeks away.

    This model deliberately treats the observed spread as:

        total_spread = baseline_spread + roll_spread

    where `baseline_spread` is the non-roll oil perp basis and `roll_spread` is
    the incremental premium required to compensate the known future oracle shifts.
    The exponential curve applies to `roll_spread`, not to the total spread.

    Important: the current implementation recomputes future shift jumps from the
    latest observed front/back CME prices each time it is called. That means fair
    value updates immediately when the live calendar spread changes, but it does
    not forecast an explicit future path for calendar convergence unless you add a
    separate shift-by-shift spread forecast.
    """

    def __init__(self, config: RollConfig):
        self.config = config
        self.tau = config.tau
        self.schedule_builder = WTIRollScheduleBuilder(config.symbol_root)

    def roll_window(self, now: datetime) -> RollWindow:
        return self.schedule_builder.upcoming_roll_window(now)

    def current_front_weight(self, now: datetime) -> float:
        weight = 1.0
        window = self.roll_window(now)
        for shift in window.shifts:
            if now >= shift.datetime_utc:
                weight = shift.front_weight
        return weight

    def current_symbols(self, now: datetime) -> Tuple[str, str]:
        window = self.roll_window(now)
        return window.front_symbol, window.back_symbol

    def oracle_price(self, front_price: float, back_price: float, now: datetime) -> float:
        weight = self.current_front_weight(now)
        return weight * front_price + (1.0 - weight) * back_price

    @staticmethod
    def shift_size(front_price: float, back_price: float) -> float:
        return 0.20 * (front_price - back_price)

    def _remaining_shift_hours(self, now: datetime) -> List[float]:
        remaining: List[float] = []
        for shift in self.roll_window(now).shifts:
            if shift.datetime_utc > now:
                remaining.append((shift.datetime_utc - now).total_seconds() / 3600.0)
        return remaining

    def compute_fair_spread_with_tau(
        self,
        front_price: float,
        back_price: float,
        now: datetime,
        tau: float,
    ) -> float:
        """
        Return the roll component only, excluding baseline perp basis.

        All remaining shift jumps are based on the *current* front/back spread
        snapshot passed into this call.
        """
        if tau <= 0:
            return 0.0

        remaining = self._remaining_shift_hours(now)
        if not remaining:
            return 0.0

        shift_size = self.shift_size(front_price, back_price)
        spread = 0.0

        for idx in range(len(remaining) - 1, -1, -1):
            spread_pre_shift = spread + shift_size
            if idx == 0:
                duration = remaining[0]
            else:
                duration = remaining[idx] - remaining[idx - 1]
            spread = spread_pre_shift * math.exp(-duration / tau)

        return spread

    def compute_fair_spread(self, front_price: float, back_price: float, now: datetime) -> float:
        return self.compute_fair_spread_with_tau(front_price, back_price, now, self.tau)

    def compute_total_fair_spread(
        self,
        front_price: float,
        back_price: float,
        now: datetime,
        baseline_spread: float = 0.0,
        tau: Optional[float] = None,
    ) -> float:
        roll_spread = self.compute_fair_spread_with_tau(
            front_price,
            back_price,
            now,
            tau if tau is not None else self.tau,
        )
        return baseline_spread + roll_spread

    def compute_fair_mark(
        self,
        front_price: float,
        back_price: float,
        now: datetime,
        baseline_spread: float = 0.0,
        tau: Optional[float] = None,
    ) -> float:
        oracle = self.oracle_price(front_price, back_price, now)
        fair_spread = self.compute_total_fair_spread(
            front_price,
            back_price,
            now,
            baseline_spread=baseline_spread,
            tau=tau,
        )
        return oracle - fair_spread

    def compute_calendar_sensitivity(
        self,
        front_price: float,
        back_price: float,
        now: datetime,
    ) -> float:
        calendar = front_price - back_price
        if abs(calendar) < 1e-9:
            return 0.0
        return self.compute_fair_spread(front_price, back_price, now) / calendar

    @staticmethod
    def implied_hourly_funding(oracle: float, spread: float) -> float:
        if oracle <= 0:
            return 0.0001 / 16
        premium = -spread / oracle
        clamped = max(-0.0005, min(0.0005, 0.0001 - premium))
        return (1.0 / 16.0) * (premium + clamped)

    def solve_point_implied_tau(
        self,
        observed_roll_spread: float,
        front_price: float,
        back_price: float,
        now: datetime,
        tau_bounds: Tuple[float, float] = (4.0, 72.0),
    ) -> Optional[float]:
        if observed_roll_spread <= 0:
            return None

        low, high = tau_bounds
        low_value = self.compute_fair_spread_with_tau(front_price, back_price, now, low)
        high_value = self.compute_fair_spread_with_tau(front_price, back_price, now, high)

        if observed_roll_spread < low_value or observed_roll_spread > high_value:
            return None

        for _ in range(80):
            mid = 0.5 * (low + high)
            mid_value = self.compute_fair_spread_with_tau(front_price, back_price, now, mid)
            if mid_value < observed_roll_spread:
                low = mid
            else:
                high = mid
        return 0.5 * (low + high)

    def compute_full_curve(
        self,
        front_price: float,
        back_price: float,
        now: datetime,
        baseline_spread: float = 0.0,
        tau: Optional[float] = None,
        hours_ahead: float = 24.0,
    ) -> List[dict]:
        fit_tau = tau if tau is not None else self.tau
        curve: List[dict] = []
        for hour_offset in range(0, int(hours_ahead) + 1):
            at = now + timedelta(hours=hour_offset)
            oracle = self.oracle_price(front_price, back_price, at)
            roll_spread = self.compute_fair_spread_with_tau(front_price, back_price, at, fit_tau)
            fair_spread = baseline_spread + roll_spread
            fair_mark = oracle - fair_spread
            curve.append(
                {
                    "time": at,
                    "oracle": oracle,
                    "fair_mark": fair_mark,
                    "baseline_spread": baseline_spread,
                    "roll_spread": roll_spread,
                    "fair_spread": fair_spread,
                    "funding_ann_pct": self.implied_hourly_funding(oracle, fair_spread) * 8760 * 100,
                }
            )

        return curve


@dataclass
class MarketSample:
    timestamp: datetime
    front_price: float
    back_price: float
    observed_spread: float


@dataclass
class TauFit:
    tau: float
    baseline_spread: float
    rmse: float


class TauEstimator:
    """
    Estimate tau from observed spread samples.

    Tau and baseline are only jointly identified once the sample window spans at
    least one roll reset. Before that, the estimator only uses the configured tau
    prior and backs out a residual baseline spread.
    """

    def __init__(
        self,
        spline: FairValueSpline,
        max_samples: int = 24,
        min_samples: int = 6,
        min_fit_span_hours: float = 18.0,
    ):
        self.spline = spline
        self.max_samples = max_samples
        self.min_samples = min_samples
        self.min_fit_span_hours = min_fit_span_hours

    def _recent_samples(self, samples: Sequence[MarketSample]) -> List[MarketSample]:
        return [sample for sample in samples if sample.observed_spread > 0][-self.max_samples:]

    def estimate_baseline(self, samples: Sequence[MarketSample], tau: float) -> Optional[float]:
        recent = self._recent_samples(samples)
        if len(recent) < self.min_samples:
            return None

        residuals = []
        for sample in recent:
            roll_prediction = self.spline.compute_fair_spread_with_tau(
                sample.front_price,
                sample.back_price,
                sample.timestamp,
                tau,
            )
            residuals.append(sample.observed_spread - roll_prediction)

        return sum(residuals) / len(residuals)

    def _spans_shift(self, samples: Sequence[MarketSample]) -> bool:
        if not samples:
            return False
        start = samples[0].timestamp
        end = samples[-1].timestamp
        if (end - start).total_seconds() / 3600.0 < self.min_fit_span_hours:
            return False

        window = self.spline.roll_window(end)
        return any(start <= shift.datetime_utc <= end for shift in window.shifts)

    def fit(self, samples: Sequence[MarketSample]) -> Optional[TauFit]:
        recent = self._recent_samples(samples)
        if len(recent) < self.min_samples or not self._spans_shift(recent):
            return None

        best: Optional[TauFit] = None

        for tau in [step / 10.0 for step in range(60, 721)]:
            predictions: List[float] = []
            for sample in recent:
                predictions.append(
                    self.spline.compute_fair_spread_with_tau(
                        sample.front_price,
                        sample.back_price,
                        sample.timestamp,
                        tau,
                    )
                )

            baseline_spread = sum(
                sample.observed_spread - predicted
                for sample, predicted in zip(recent, predictions)
            ) / len(recent)
            errors: List[float] = []
            for sample, predicted in zip(recent, predictions):
                total_prediction = baseline_spread + predicted
                errors.append((sample.observed_spread - total_prediction) ** 2)

            rmse = math.sqrt(sum(errors) / len(errors))
            if best is None or rmse < best.rmse:
                best = TauFit(tau=tau, baseline_spread=baseline_spread, rmse=rmse)

        return best

# ============================================================
# TRADING SIGNAL GENERATOR
# ============================================================

@dataclass
class Signal:
    timestamp: datetime
    front_symbol: str
    back_symbol: str
    oracle: float
    market_mark: float
    fair_mark: float
    market_spread: float
    impact_spread: float
    baseline_spread: float
    roll_spread: float
    fair_spread: float
    edge: float
    funding_rate: float
    action: str
    strength: float
    point_implied_tau: Optional[float]
    rolling_implied_tau: Optional[float]
    rolling_baseline_spread: Optional[float]
    calendar_sensitivity: float


class SignalGenerator:
    """Generate buy/sell/hold recommendations from fair value deviations."""

    def __init__(self, threshold: float = 0.10):
        self.threshold = threshold
        self.history: List[Signal] = []

    def evaluate(
        self,
        now: datetime,
        front_symbol: str,
        back_symbol: str,
        oracle: float,
        market_mark: float,
        fair_mark: float,
        impact_spread: float,
        baseline_spread: float,
        roll_spread: float,
        funding_rate: float,
        point_tau: Optional[float],
        rolling_tau: Optional[float],
        rolling_baseline_spread: Optional[float],
        calendar_sensitivity: float,
    ) -> Signal:
        market_spread = oracle - market_mark
        fair_spread = oracle - fair_mark
        edge = market_spread - fair_spread

        # If market spread is narrower than fair, the perp is rich vs fair and
        # the trade is to sell/short the perp against the CME hedge.
        if edge < -self.threshold:
            action = "SELL"
            strength = min(1.0, abs(edge) / (3.0 * self.threshold))
        elif edge > self.threshold:
            action = "BUY"
            strength = min(1.0, edge / (3.0 * self.threshold))
        else:
            action = "HOLD"
            strength = 0.0

        signal = Signal(
            timestamp=now,
            front_symbol=front_symbol,
            back_symbol=back_symbol,
            oracle=oracle,
            market_mark=market_mark,
            fair_mark=fair_mark,
            market_spread=market_spread,
            impact_spread=impact_spread,
            baseline_spread=baseline_spread,
            roll_spread=roll_spread,
            fair_spread=fair_spread,
            edge=edge,
            funding_rate=funding_rate,
            action=action,
            strength=strength,
            point_implied_tau=point_tau,
            rolling_implied_tau=rolling_tau,
            rolling_baseline_spread=rolling_baseline_spread,
            calendar_sensitivity=calendar_sensitivity,
        )
        self.history.append(signal)
        return signal


class HedgeCalculator:
    """Compute the CME hedge matching the current trade.xyz oracle weights."""

    def __init__(self, spline: FairValueSpline, position_size: float = 1.0):
        self.spline = spline
        self.position_size = position_size

    def current_hedge(self, now: datetime) -> dict:
        window = self.spline.roll_window(now)
        front_weight = self.spline.current_front_weight(now)
        front_units = front_weight * self.position_size
        back_units = (1.0 - front_weight) * self.position_size
        return {
            "front_symbol": window.front_symbol,
            "back_symbol": window.back_symbol,
            "front_units": front_units,
            "back_units": back_units,
            "front_weight": front_weight,
        }

    def next_rebalance(self, now: datetime) -> Optional[dict]:
        window = self.spline.roll_window(now)
        current = self.current_hedge(now)
        for shift in window.shifts:
            if shift.datetime_utc > now:
                future_front = shift.front_weight * self.position_size
                future_back = shift.back_weight * self.position_size
                return {
                    "time": shift.datetime_utc,
                    "hours_until": (shift.datetime_utc - now).total_seconds() / 3600.0,
                    "sell_front": current["front_units"] - future_front,
                    "buy_back": future_back - current["back_units"],
                }
        return None


@dataclass
class PositionSnapshot:
    side: int
    label: str
    entry_time: Optional[datetime]
    entry_spread: float
    spread_pnl: float
    funding_pnl: float
    open_pnl: float
    realized_pnl: float
    total_pnl: float


class PaperTrader:
    """
    Track a simple signal-following paper position on the hedged spread.

    `SELL` means short perp + long CME hedge. `BUY` means the reverse.
    Funding is accrued continuously from the latest observed hourly rate as an
    approximation between polls.
    """

    def __init__(self, position_size: float = 1.0):
        self.position_size = position_size
        self.side = 0
        self.entry_time: Optional[datetime] = None
        self.entry_spread = 0.0
        self.funding_pnl = 0.0
        self.realized_pnl = 0.0
        self.last_timestamp: Optional[datetime] = None

    def _accrue_funding(self, timestamp: datetime, oracle: float, funding_rate: float) -> None:
        if self.side == 0 or self.last_timestamp is None:
            return
        elapsed_hours = max(0.0, (timestamp - self.last_timestamp).total_seconds() / 3600.0)
        self.funding_pnl += -self.side * funding_rate * oracle * elapsed_hours * self.position_size

    def _spread_pnl(self, current_spread: float) -> float:
        if self.side == 0:
            return 0.0
        return self.side * (current_spread - self.entry_spread) * self.position_size

    def update(self, signal: Signal) -> PositionSnapshot:
        self._accrue_funding(signal.timestamp, signal.oracle, signal.funding_rate)

        desired_side = self.side
        if signal.action == "SELL":
            desired_side = -1
        elif signal.action == "BUY":
            desired_side = 1

        if self.side == 0 and desired_side != 0:
            self.side = desired_side
            self.entry_time = signal.timestamp
            self.entry_spread = signal.market_spread
            self.funding_pnl = 0.0
        elif self.side != 0 and desired_side not in (0, self.side):
            self.realized_pnl += self._spread_pnl(signal.market_spread) + self.funding_pnl
            self.side = desired_side
            self.entry_time = signal.timestamp
            self.entry_spread = signal.market_spread
            self.funding_pnl = 0.0

        spread_pnl = self._spread_pnl(signal.market_spread)
        open_pnl = spread_pnl + self.funding_pnl if self.side != 0 else 0.0
        total_pnl = self.realized_pnl + open_pnl

        self.last_timestamp = signal.timestamp
        label = "FLAT"
        if self.side == -1:
            label = "SHORT PERP / LONG CME"
        elif self.side == 1:
            label = "LONG PERP / SHORT CME"

        return PositionSnapshot(
            side=self.side,
            label=label,
            entry_time=self.entry_time,
            entry_spread=self.entry_spread,
            spread_pnl=spread_pnl,
            funding_pnl=self.funding_pnl,
            open_pnl=open_pnl,
            realized_pnl=self.realized_pnl,
            total_pnl=total_pnl,
        )


def print_dashboard(
    spline: FairValueSpline,
    signal: Signal,
    hedge: dict,
    next_rebalance: Optional[dict],
    front_price: float,
    back_price: float,
    window: RollWindow,
    position: PositionSnapshot,
    curve_hours: int,
) -> None:
    now_str = signal.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
    print("\033[2J\033[H", end="")
    print("=" * 88)
    print(f"  HL OIL PERP FAIR VALUE MONITOR  |  {window.label}  |  {now_str}")
    print("=" * 88)

    print("\n  CME FUTURES")
    print(
        f"    {signal.front_symbol}: ${front_price:>8.2f}    "
        f"{signal.back_symbol}: ${back_price:>8.2f}    "
        f"Calendar: ${front_price - back_price:>6.2f}"
    )

    print("\n  HYPERLIQUID PERP")
    print(f"    Mark:          ${signal.market_mark:>8.3f}    Oracle: ${signal.oracle:>8.3f}")
    print(f"    Mark spread:   ${signal.market_spread:>+.4f}")
    print(f"    Impact spread: ${signal.impact_spread:>+.4f}")
    print(f"    Baseline:      ${signal.baseline_spread:>+.4f}")
    print(f"    Roll spread:   ${signal.roll_spread:>+.4f}")
    print(f"    Fair spread:   ${signal.fair_spread:>+.4f}")
    print(f"    Edge:          ${signal.edge:>+.4f}")
    print(f"    Funding (ann): {signal.funding_rate * 8760 * 100:>+8.1f}%")

    point_tau = f"{signal.point_implied_tau:.2f}h" if signal.point_implied_tau else "n/a"
    rolling_tau = f"{signal.rolling_implied_tau:.2f}h" if signal.rolling_implied_tau else "n/a"
    rolling_baseline = (
        f"${signal.rolling_baseline_spread:+.4f}"
        if signal.rolling_baseline_spread is not None
        else "n/a"
    )
    print("\n  MODEL DIAGNOSTICS")
    print(f"    Point implied τ:   {point_tau}")
    print(f"    Rolling implied τ: {rolling_tau}")
    print(f"    Rolling baseline:  {rolling_baseline}")
    print(f"    dSpread / dCal:    {signal.calendar_sensitivity:.4f} $/$")

    color = "\033[92m" if signal.action == "SELL" else ("\033[91m" if signal.action == "BUY" else "\033[93m")
    reset = "\033[0m"
    print(f"\n  SIGNAL: {color}{signal.action}{reset}  (strength: {signal.strength:.2f})")

    print(f"\n  CME HEDGE ({hedge['front_weight'] * 100:>5.1f}% front)")
    print(
        f"    Long {hedge['front_units']:.2f} {hedge['front_symbol']} + "
        f"{hedge['back_units']:.2f} {hedge['back_symbol']}"
    )
    if next_rebalance:
        print(
            f"    Next rebalance in {next_rebalance['hours_until']:.1f}h: "
            f"sell {next_rebalance['sell_front']:.2f} {hedge['front_symbol']}, "
            f"buy {next_rebalance['buy_back']:.2f} {hedge['back_symbol']}"
        )

    print("\n  PAPER POSITION")
    print(f"    State:         {position.label}")
    if position.entry_time:
        print(f"    Entry spread:  ${position.entry_spread:+.4f}")
    print(f"    Spread PnL:    ${position.spread_pnl:+.4f}")
    print(f"    Funding PnL:   ${position.funding_pnl:+.4f}")
    print(f"    Open PnL:      ${position.open_pnl:+.4f}")
    print(f"    Realized PnL:  ${position.realized_pnl:+.4f}")
    print(f"    Total PnL:     ${position.total_pnl:+.4f}")

    print(f"\n  FAIR VALUE CURVE (next {curve_hours}h)")
    print(f"  {'Hours':>6}  {'Oracle':>8}  {'FairMark':>9}  {'Roll':>8}  {'Total':>8}  {'FundAnn':>10}")
    curve = spline.compute_full_curve(
        front_price,
        back_price,
        signal.timestamp,
        baseline_spread=signal.baseline_spread,
        tau=signal.rolling_implied_tau,
        hours_ahead=curve_hours,
    )
    display_points = curve[::2] if len(curve) > 12 else curve
    for point in display_points:
        hours = int((point['time'] - signal.timestamp).total_seconds() / 3600)
        print(
            f"  {hours:>5}h  {point['oracle']:>8.2f}  ${point['fair_mark']:>8.3f}  "
            f"${point['roll_spread']:>7.4f}  ${point['fair_spread']:>7.4f}  {point['funding_ann_pct']:>+10.1f}%"
        )

    print("\n  Press Ctrl+C to exit")


def open_csv_log(path: str):
    handle = open(path, "w", buffering=1)
    handle.write(
        "timestamp,front_symbol,back_symbol,front_price,back_price,oracle,mark,"
        "impact_spread,baseline_spread,roll_spread,fair_mark,market_spread,"
        "fair_spread,edge,funding_rate,point_implied_tau,rolling_implied_tau,"
        "rolling_baseline_spread,calendar_sensitivity,action,strength,"
        "position_state,entry_spread,spread_pnl,funding_pnl,total_pnl\n"
    )
    return handle


def main() -> None:
    parser = argparse.ArgumentParser(description="HL Oil Perp Fair Value Monitor")
    parser.add_argument("--demo", action="store_true", help="Run in demo mode with static prices.")
    parser.add_argument("--as-of", type=str, default=None, help="Optional ISO timestamp for model evaluation.")
    parser.add_argument(
        "--fit-tau",
        action="store_true",
        help="Opt in to fitting tau from a window that spans at least one realized shift reset.",
    )
    parser.add_argument(
        "--quote-source",
        choices=["auto", "databento", "yahoo"],
        default="auto",
        help="Snapshot source for CME front/back quotes.",
    )
    parser.add_argument("--baseline-spread", type=float, default=0.0, help="Fallback non-roll baseline spread in dollars.")
    parser.add_argument("--f1", type=float, default=95.70, help="Front-month CME price for demo mode.")
    parser.add_argument("--f2", type=float, default=87.00, help="Back-month CME price for demo mode.")
    parser.add_argument("--mark", type=float, default=94.30, help="HL mark price for demo mode.")
    parser.add_argument("--oracle", type=float, default=95.10, help="HL oracle price for demo mode.")
    parser.add_argument(
        "--tau",
        type=float,
        default=16.0,
        help="Funding time constant in hours; this is a structural decay/growth parameter, not time until the next shift.",
    )
    parser.add_argument("--interval", type=int, default=30, help="Poll interval in seconds.")
    parser.add_argument("--threshold", type=float, default=0.10, help="Signal threshold in dollars.")
    parser.add_argument("--curve-hours", type=int, default=24, help="Curve horizon to display.")
    parser.add_argument("--iterations", type=int, default=0, help="Number of loop iterations; 0 means run forever.")
    parser.add_argument("--tau-window", type=int, default=24, help="Recent samples used for rolling tau fit.")
    parser.add_argument("--position-size", type=float, default=1.0, help="Paper position size in HL units.")
    parser.add_argument("--log", type=str, default=None, help="Optional CSV log file path.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler()],
    )

    config = RollConfig(
        tau=args.tau,
        signal_threshold=args.threshold,
        position_size=args.position_size,
        tau_window=args.tau_window,
    )
    spline = FairValueSpline(config)
    signal_generator = SignalGenerator(threshold=args.threshold)
    hedge_calculator = HedgeCalculator(spline, position_size=args.position_size)
    tau_estimator = TauEstimator(spline, max_samples=args.tau_window)
    paper_trader = PaperTrader(position_size=args.position_size)
    history: Deque[MarketSample] = deque(maxlen=max(args.tau_window * 4, 64))

    if args.demo:
        cme_source = ManualPriceSource(
            f1=args.f1,
            f2=args.f2,
            mark=args.mark,
            oracle=args.oracle,
        )
        hl_source = cme_source
    else:
        if args.quote_source == "databento":
            cme_source = DatabentoClient()
        elif args.quote_source == "yahoo":
            cme_source = YahooFinanceClient()
        else:
            cme_source = AutoFuturesClient()
        hl_source = HyperliquidClient(config.hl_asset)

    fixed_now = parse_iso_datetime(args.as_of) if args.as_of else None
    if fixed_now is not None and args.iterations == 0:
        args.iterations = 1

    log_handle = open_csv_log(args.log) if args.log else None

    print("Starting fair value monitor...")
    print(
        f"  τ = {args.tau:.2f}h, threshold = ${args.threshold:.2f}, "
        f"interval = {args.interval}s, fit_tau = {args.fit_tau}, quote_source = {args.quote_source}"
    )

    iterations = 0
    try:
        while True:
            now = fixed_now if fixed_now is not None else datetime.now(UTC)
            window = spline.roll_window(now)
            front_symbol, back_symbol = window.front_symbol, window.back_symbol

            prices = cme_source.get_futures_prices([front_symbol, back_symbol])
            hl_data = hl_source.get_perp_data()

            if prices is None or hl_data is None:
                logging.warning("Failed to fetch market data; retrying...")
                time.sleep(5)
                continue

            if front_symbol not in prices or back_symbol not in prices:
                logging.warning("Incomplete CME strip for %s / %s; retrying...", front_symbol, back_symbol)
                time.sleep(5)
                continue

            front_price = prices[front_symbol]
            back_price = prices[back_symbol]
            market_mark = hl_data["markPx"]
            oracle = hl_data["oraclePx"] or spline.oracle_price(front_price, back_price, now)
            funding_rate = hl_data["funding"]
            impact_spread = -hl_data["premium"] * oracle if oracle else 0.0

            observed_spread_for_tau = impact_spread if impact_spread > 0 else (oracle - market_mark)

            history.append(
                MarketSample(
                    timestamp=now,
                    front_price=front_price,
                    back_price=back_price,
                    observed_spread=observed_spread_for_tau,
                )
            )
            rolling_fit = tau_estimator.fit(list(history)) if args.fit_tau else None
            rolling_tau = rolling_fit.tau if rolling_fit else None
            effective_tau = rolling_tau if rolling_tau is not None else args.tau
            if rolling_fit is not None:
                baseline_spread = rolling_fit.baseline_spread
            else:
                estimated_baseline = tau_estimator.estimate_baseline(list(history), effective_tau)
                baseline_spread = estimated_baseline if estimated_baseline is not None else args.baseline_spread
            roll_spread = spline.compute_fair_spread_with_tau(front_price, back_price, now, effective_tau)
            fair_spread = baseline_spread + roll_spread
            fair_mark = oracle - fair_spread
            observed_roll_spread = observed_spread_for_tau - baseline_spread
            point_tau = spline.solve_point_implied_tau(
                observed_roll_spread,
                front_price,
                back_price,
                now,
            )
            calendar_sensitivity = spline.compute_calendar_sensitivity(front_price, back_price, now)

            signal = signal_generator.evaluate(
                now=now,
                front_symbol=front_symbol,
                back_symbol=back_symbol,
                oracle=oracle,
                market_mark=market_mark,
                fair_mark=fair_mark,
                impact_spread=impact_spread,
                baseline_spread=baseline_spread,
                roll_spread=roll_spread,
                funding_rate=funding_rate,
                point_tau=point_tau,
                rolling_tau=rolling_tau,
                rolling_baseline_spread=baseline_spread,
                calendar_sensitivity=calendar_sensitivity,
            )

            hedge = hedge_calculator.current_hedge(now)
            next_rebalance = hedge_calculator.next_rebalance(now)
            position = paper_trader.update(signal)

            print_dashboard(
                spline=spline,
                signal=signal,
                hedge=hedge,
                next_rebalance=next_rebalance,
                front_price=front_price,
                back_price=back_price,
                window=window,
                position=position,
                curve_hours=args.curve_hours,
            )

            if log_handle:
                point_tau_str = "" if point_tau is None else f"{point_tau:.6f}"
                rolling_tau_str = "" if rolling_tau is None else f"{rolling_tau:.6f}"
                rolling_baseline_str = f"{baseline_spread:.6f}"
                log_handle.write(
                    f"{now.isoformat()},{front_symbol},{back_symbol},{front_price:.8f},{back_price:.8f},"
                    f"{oracle:.8f},{market_mark:.8f},{impact_spread:.8f},{signal.baseline_spread:.8f},"
                    f"{signal.roll_spread:.8f},{fair_mark:.8f},{signal.market_spread:.8f},"
                    f"{signal.fair_spread:.8f},{signal.edge:.8f},{funding_rate:.10f},"
                    f"{point_tau_str},{rolling_tau_str},{rolling_baseline_str},"
                    f"{calendar_sensitivity:.8f},{signal.action},{signal.strength:.6f},"
                    f"{position.label},{position.entry_spread:.8f},{position.spread_pnl:.8f},"
                    f"{position.funding_pnl:.8f},{position.total_pnl:.8f}\n"
                )

            iterations += 1
            if args.iterations and iterations >= args.iterations:
                break

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        if log_handle:
            log_handle.close()


if __name__ == "__main__":
    main()
