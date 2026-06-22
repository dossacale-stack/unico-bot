import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from bybit_api_manager import BybitAPIManager
import seed_patterns

logger = logging.getLogger("MarketScanner")


class SignalType(Enum):
    LONG_BREAKOUT = "LONG_BREAKOUT"
    SHORT_BREAKOUT = "SHORT_BREAKOUT"
    LONG_REVERSAL = "LONG_REVERSAL"
    SHORT_REVERSAL = "SHORT_REVERSAL"

    def is_long(self) -> bool:
        return self in {SignalType.LONG_BREAKOUT, SignalType.LONG_REVERSAL}


@dataclass
class Signal:
    symbol: str
    signal_type: SignalType
    score: float
    risk_reward: float
    stop_loss: float
    take_profit: float
    entry_price: float
    df: Optional[pd.DataFrame] = None
    pattern_id: Optional[int] = None


MATCH_FIELDS = [
    "ema21_vs_ema55",
    "ema55_vs_ema144",
    "ema144_vs_ema233",
    "ema21_slope",
    "ema144_slope",
    "precio_vs_ema21",
    "precio_vs_ema55",
    "precio_vs_ema144",
    "precio_vs_ema233",
    "bb_estado",
    "bb_precio",
    "volumen",
    "patron_vela",
    "fib_zona",
]


class MarketScanner:
    def __init__(
        self,
        api_manager: BybitAPIManager,
        watchlist: List[str],
        scan_interval: float,
        min_score: float,
        min_rr: float,
        position_pct: float,
        db_path: str = "patterns.db",
    ):
        self.api = api_manager
        self.watchlist = watchlist
        self.scan_interval = scan_interval
        self.min_score = min_score
        self.min_rr = min_rr
        self.position_pct = position_pct
        self.db_path = db_path
        self.patterns = self._load_patterns()

    def _normalize_symbol(self, symbol: str) -> str:
        return re.sub(r"[^\w]", "", symbol).upper()

    def _load_patterns(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.db_path):
            logger.warning(
                "[MarketScanner] No se encontró DB de patrones. Usando patterns en memoria."
            )
            return [p.copy() for p in seed_patterns.PATTERNS]

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM patterns WHERE resultado != 'EVITAR'"
                ).fetchall()
                return [dict(row) for row in rows]
        except Exception as exc:
            logger.warning(
                f"[MarketScanner] No se pudo cargar DB de patrones: {exc}. "
                "Usando seed_patterns en memoria."
            )
            return [p.copy() for p in seed_patterns.PATTERNS]

    async def scan_all(self) -> List[Signal]:
        signals: List[Signal] = []
        for symbol in self.watchlist:
            try:
                df = await self.api.fetch_ohlcv(symbol, timeframe="15m", limit=100)
                if df is None or len(df) < 40:
                    continue

                behavior = self._describe_behavior(df)
                symbol_code = self._normalize_symbol(symbol)
                matches = self._match_patterns(symbol_code, behavior)

                if not matches:
                    continue

                best = max(
                    matches,
                    key=lambda item: (
                        item["match_ratio"],
                        item["pattern"].get("rb_real", 0.0),
                    ),
                )
                signal = self._build_signal(symbol, df, behavior, best)
                if signal and signal.score >= self.min_score and signal.risk_reward >= self.min_rr:
                    signals.append(signal)

            except Exception as exc:
                logger.warning(f"[MarketScanner] Error en {symbol}: {exc}")

        if not signals:
            logger.info("[MarketScanner] Ninguna señal encontrada en este ciclo.")
        return signals

    def _describe_behavior(self, df: pd.DataFrame) -> Dict[str, Any]:
        df = df.copy()
        df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
        df["ema55"] = df["close"].ewm(span=55, adjust=False).mean()
        df["ema144"] = df["close"].ewm(span=144, adjust=False).mean()
        df["ema233"] = df["close"].ewm(span=233, adjust=False).mean()
        df["bb_mid"] = df["close"].rolling(20).mean()
        df["bb_std"] = df["close"].rolling(20).std()
        df["bb_upper"] = df["bb_mid"] + 2 * df["bb_std"]
        df["bb_lower"] = df["bb_mid"] - 2 * df["bb_std"]

        current = df.iloc[-1]
        prior = df.iloc[-2]

        def slope_label(series: pd.Series) -> str:
            delta = series.iloc[-1] - series.iloc[-4]
            if abs(delta) / max(series.iloc[-1], 1e-6) < 0.002:
                return "FLAT"
            return "UP" if delta > 0 else "DOWN"

        def position_label(price: float, target: float) -> str:
            diff = price - target
            pct = abs(diff / max(price, 1e-6))
            if pct < 0.002:
                return "TOUCHING"
            if pct < 0.008:
                return "NEAR"
            return "ABOVE" if diff > 0 else "BELOW"

        def ema_relation(fast: float, slow: float, prev_fast: float, prev_slow: float) -> str:
            if fast > slow and prev_fast <= prev_slow:
                return "CROSSING_UP"
            if fast < slow and prev_fast >= prev_slow:
                return "CROSSING_DOWN"
            if abs(fast - slow) / max(slow, 1e-6) < 0.0015:
                return "FLAT"
            return "ABOVE" if fast > slow else "BELOW"

        def fib_zone(close: float) -> str:
            window = df.iloc[-40:-5]
            if len(window) < 10:
                return "N/A"
            swing_high = float(window["high"].max())
            swing_low = float(window["low"].min())
            if swing_high <= swing_low or close <= swing_low:
                return "N/A"
            ratio = (close - swing_low) / max(swing_high - swing_low, 1e-6)
            if ratio < 0.382:
                return "0.0_0.382"
            if ratio < 0.5:
                return "0.382_0.500"
            if ratio < 0.618:
                return "0.500_0.618"
            if ratio < 0.786:
                return "0.618_0.786"
            if ratio <= 1.0:
                return "0.786_1.000"
            return "1.000_PLUS"

        def bb_price_label(close: float, mid: float, lower: float, upper: float) -> str:
            if close >= upper:
                return "UPPER"
            if close <= lower:
                return "LOWER"
            distance = abs(close - mid) / max(mid, 1e-6)
            if distance < 0.03:
                return "MID"
            if close > mid:
                return "MID_TO_UPPER"
            return "LOWER"

        def bb_state_label(mid: float, upper: float, lower: float, recent: pd.DataFrame) -> str:
            width = (upper - lower) / max(mid, 1e-6)
            previous_width = (
                (recent["bb_upper"] - recent["bb_lower"]) /
                recent["bb_mid"].replace(0, np.nan)
            ).iloc[-5:-1].mean()
            if width < 0.025:
                return "MAX_SQUEEZE"
            if width < 0.045:
                return "SQUEEZE"
            if width > 0.080:
                return "EXPANDING"
            return "CONTRACTING"

        def volume_label(volume: float, average: float) -> str:
            if volume >= average * 2.0:
                return "HIGH"
            if volume >= average * 1.2:
                return "MEDIUM"
            if volume < average * 0.35:
                return "VERY_LOW"
            return "LOW"

        def candle_pattern(row: pd.Series) -> str:
            body = abs(row["close"] - row["open"])
            upper_wick = float(row["high"] - max(row["close"], row["open"]))
            lower_wick = float(min(row["close"], row["open"]) - row["low"])
            if body > 0 and row["close"] > row["open"] and body > upper_wick * 2:
                return "STRONG_GREEN"
            if body > 0 and row["close"] < row["open"] and body > lower_wick * 2:
                return "STRONG_RED"
            if upper_wick > body * 1.5 and row["close"] < row["open"]:
                return "REJECTION"
            if lower_wick > body * 1.5 and row["close"] > row["open"]:
                return "HAMMER"
            return "NEUTRAL"

        volume_average = float(df["volume"].rolling(20).mean().iloc[-2] or 0.0)
        price = float(current["close"])

        return {
            "ema21_vs_ema55": ema_relation(
                float(current["ema21"]), float(current["ema55"]),
                float(prior["ema21"]), float(prior["ema55"]),
            ),
            "ema55_vs_ema144": ema_relation(
                float(current["ema55"]), float(current["ema144"]),
                float(prior["ema55"]), float(prior["ema144"]),
            ),
            "ema144_vs_ema233": ema_relation(
                float(current["ema144"]), float(current["ema233"]),
                float(prior["ema144"]), float(prior["ema233"]),
            ),
            "ema21_slope": slope_label(df["ema21"]),
            "ema144_slope": slope_label(df["ema144"]),
            "precio_vs_ema21": position_label(price, float(current["ema21"])),
            "precio_vs_ema55": position_label(price, float(current["ema55"])),
            "precio_vs_ema144": position_label(price, float(current["ema144"])),
            "precio_vs_ema233": position_label(price, float(current["ema233"])),
            "bb_estado": bb_state_label(
                float(current["bb_mid"]),
                float(current["bb_upper"]),
                float(current["bb_lower"]),
                df.iloc[-10:],
            ),
            "bb_precio": bb_price_label(
                price,
                float(current["bb_mid"]),
                float(current["bb_lower"]),
                float(current["bb_upper"]),
            ),
            "volumen": volume_label(float(current["volume"]), volume_average),
            "patron_vela": candle_pattern(current),
            "fib_zona": fib_zone(price),
            "entry_price": price,
        }

    def _match_patterns(self, symbol_code: str, behavior: Dict[str, Any]) -> List[Dict[str, Any]]:
        matches: List[Dict[str, Any]] = []

        for pattern in self.patterns:
            if pattern.get("symbol") not in {symbol_code, "UNIVERSAL"}:
                continue

            if pattern.get("timeframe", "15m") != "15m":
                continue

            score = 0
            total = 0

            for key in MATCH_FIELDS:
                expected = pattern.get(key)
                actual = behavior.get(key)
                if expected is None or expected == "N/A":
                    continue
                if actual is None:
                    continue

                total += 1
                if str(expected).upper() == str(actual).upper():
                    score += 1

            if total == 0:
                continue

            match_ratio = score / total
            if match_ratio >= 0.60:
                matches.append({
                    "pattern": pattern,
                    "match_ratio": match_ratio,
                    "score": score,
                    "total": total,
                })

        return matches

    def _build_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        behavior: Dict[str, Any],
        matched: Dict[str, Any],
    ) -> Optional[Signal]:
        pattern = matched["pattern"]
        signal_type_str = pattern["signal_type"]

        # ============================================================
        # 🔥 CORRECCIÓN: Validar que el tipo de señal sea válido
        # ============================================================
        try:
            signal_type = SignalType(signal_type_str)
        except ValueError:
            logger.warning(
                f"[MarketScanner] Tipo de señal inválido '{signal_type_str}' en patrón "
                f"para {symbol}. Se omite."
            )
            return None

        entry_price = float(behavior["entry_price"])
        stop_loss = self._calculate_stop_loss(df, signal_type)
        take_profit = self._calculate_take_profit(
            entry_price, stop_loss, pattern.get("rb_real", 0.0), signal_type
        )
        if stop_loss is None or take_profit is None:
            return None

        if entry_price == stop_loss:
            return None

        reward = abs(take_profit - entry_price)
        risk = abs(entry_price - stop_loss)
        risk_reward = reward / max(risk, 1e-6)
        score = float(matched["match_ratio"])

        logger.debug(
            f"[MarketScanner] Signal candidate {symbol} {signal_type.value} | "
            f"match_ratio={matched['match_ratio']:.2f} | rb_real={pattern.get('rb_real', 0.0)} | "
            f"score={score:.2f} | rr={risk_reward:.2f}"
        )

        return Signal(
            symbol=symbol,
            signal_type=signal_type,
            score=score,
            risk_reward=float(risk_reward),
            stop_loss=float(stop_loss),
            take_profit=float(take_profit),
            entry_price=entry_price,
            df=df,
            pattern_id=pattern.get("id"),
        )

    def _calculate_stop_loss(self, df: pd.DataFrame, signal_type: SignalType) -> Optional[float]:
        if signal_type.is_long():
            return float(df["low"].iloc[-5:].min())
        return float(df["high"].iloc[-5:].max())

    def _calculate_take_profit(
        self,
        entry_price: float,
        stop_loss: float,
        rb_real: float,
        signal_type: SignalType,
    ) -> Optional[float]:
        if entry_price == stop_loss:
            return None

        base_distance = abs(entry_price - stop_loss)
        ratio = 3.0
        if rb_real and rb_real > 0:
            ratio = max(3.0, min(6.0, float(rb_real) / 10.0))

        if signal_type.is_long():
            return entry_price + base_distance * ratio
        return entry_price - base_distance * ratio
