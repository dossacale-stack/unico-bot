# strategy_scanner.py - Scanner con logica de AGOTAMIENTO
# =========================================================

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from bybit_api_manager import BybitAPIManager

logger = logging.getLogger("StrategyScanner")


class SignalType(Enum):
    LONG_BREAKOUT = "LONG_BREAKOUT"
    SHORT_BREAKOUT = "SHORT_BREAKOUT"
    LONG_REVERSAL = "LONG_REVERSAL"
    SHORT_REVERSAL = "SHORT_REVERSAL"

    def is_long(self) -> bool:
        return self in {SignalType.LONG_BREAKOUT, SignalType.LONG_REVERSAL}

    def is_short(self) -> bool:
        return self in {SignalType.SHORT_BREAKOUT, SignalType.SHORT_REVERSAL}


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
    timeframe: str = "1h"
    setup: Optional[Any] = None
    confidence: float = 0.0
    risk_level: str = "MEDIO"
    entry_zone_min: float = 0.0
    entry_zone_max: float = 0.0
    entry_reason: str = ""
    auction_type: str = ""
    market_position: str = ""
    bb_position: str = ""
    bb_squeeze: bool = False
    oportunidad: Optional[Any] = None


class MarketScanner:
    EMA_FAST = 55
    EMA_MID = 144
    EMA_SLOW = 233
    BB_PERIOD = 21
    BB_STD = 2.0

    MIN_DISTANCE_PCT = 0.03
    MAX_DISTANCE_PCT = 0.25
    MIN_CONSECUTIVE_CANDLES = 4
    MIN_BB_EXPANSION = 0.15
    MIN_TIME_IN_ZONE = 6
    MIN_EXHAUSTION_SCORE = 0.55
    REJECTION_WICK_RATIO = 1.4

    def __init__(
        self,
        api_manager: BybitAPIManager,
        watchlist: List[str],
        scan_interval: float = 60.0,
        min_score: float = 0.55,
        min_rr: float = 1.0,
        position_pct: float = 0.30,
        db_path: str = "patterns.db",
        signal_cooldown_seconds: int = 300,
        timeframes: List[str] = None,
        modo_aprendizaje=None,
    ):
        self.api = api_manager
        self.watchlist = watchlist
        self.scan_interval = scan_interval
        self.min_score = min_score
        self.min_rr = min_rr
        self.position_pct = position_pct
        self.db_path = db_path
        self.signal_cooldown_seconds = signal_cooldown_seconds
        self.timeframes = timeframes or ["1h"]
        self._signal_cooldown: Dict[str, float] = {}
        self.escaneos_totales = 0
        logger.info(
            f"[Scanner] AGOTAMIENTO | EMA {self.EMA_FAST}/{self.EMA_MID}/{self.EMA_SLOW} "
            f"| BB({self.BB_PERIOD},{self.BB_STD}) | min_score={min_score}"
        )

    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ema_fast"] = df["close"].ewm(span=self.EMA_FAST, adjust=False).mean()
        df["ema_mid"] = df["close"].ewm(span=self.EMA_MID, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=self.EMA_SLOW, adjust=False).mean()

        df["bb_mid"] = df["close"].rolling(self.BB_PERIOD).mean()
        df["bb_std"] = df["close"].rolling(self.BB_PERIOD).std()
        df["bb_upper"] = df["bb_mid"] + self.BB_STD * df["bb_std"]
        df["bb_lower"] = df["bb_mid"] - self.BB_STD * df["bb_std"]
        df["bb_bandwidth"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

        df["tr"] = np.maximum(
            df["high"] - df["low"],
            np.maximum(abs(df["high"] - df["close"].shift(1)),
                       abs(df["low"] - df["close"].shift(1)))
        )
        df["atr"] = df["tr"].rolling(14).mean()

        df["vol_avg"] = df["volume"].rolling(20).mean()
        df["vol_ratio"] = df["volume"] / df["vol_avg"].replace(0, 1e-9)

        return df

    def _detect_ema_stack(self, row: pd.Series, price: float) -> str:
        ef = float(row["ema_fast"])
        em = float(row["ema_mid"])
        es = float(row["ema_slow"])

        if ef > em and em > es and price > ef:
            return "BULLISH"
        if ef < em and em < es and price < ef:
            return "BEARISH"
        return "TANGLED"

    def _exhaustion_score(self, df: pd.DataFrame, direction: str) -> Dict[str, Any]:
        last = df.iloc[-1]
        price = float(last["close"])
        ema_fast = float(last["ema_fast"])

        distance_pct = (price - ema_fast) / ema_fast
        if direction == "UP":
            dist_score = min(max(distance_pct - self.MIN_DISTANCE_PCT, 0) / 0.10, 1.0)
        else:
            dist_score = min(max(-distance_pct - self.MIN_DISTANCE_PCT, 0) / 0.10, 1.0)

        closes = df["close"].values
        consecutive = 0
        for i in range(len(df) - 2, max(len(df) - 20, 0), -1):
            if direction == "UP" and closes[i] > closes[i - 1]:
                consecutive += 1
            elif direction == "DOWN" and closes[i] < closes[i - 1]:
                consecutive += 1
            else:
                break
        consec_score = min(consecutive / 10.0, 1.0)

        bw_now = float(last["bb_bandwidth"])
        bw_10 = float(df["bb_bandwidth"].iloc[-10])
        if bw_10 > 0:
            bb_expansion = (bw_now - bw_10) / bw_10
        else:
            bb_expansion = 0.0
        bb_score = min(max(bb_expansion, 0) / 0.50, 1.0)

        threshold_pct = 0.02 if direction == "UP" else -0.02
        recent = df.tail(30)
        candles_in_zone = 0
        for _, r in recent.iterrows():
            p = float(r["close"])
            ef = float(r["ema_fast"])
            d = (p - ef) / ef
            if direction == "UP" and d > threshold_pct:
                candles_in_zone += 1
            elif direction == "DOWN" and d < threshold_pct:
                candles_in_zone += 1
        time_score = min(candles_in_zone / 20.0, 1.0)

        score = (
            dist_score * 0.35 +
            consec_score * 0.20 +
            bb_score * 0.25 +
            time_score * 0.20
        )

        return {
            "score": score,
            "dist_score": dist_score,
            "consec_score": consec_score,
            "bb_score": bb_score,
            "time_score": time_score,
            "distance_pct": distance_pct,
            "consecutive": consecutive,
            "bb_expansion": bb_expansion,
            "candles_in_zone": candles_in_zone,
        }

    def _detect_rejection_bullish(self, row: pd.Series) -> bool:
        o, c, h, l = float(row["open"]), float(row["close"]), float(row["high"]), float(row["low"])
        body = abs(c - o) or (h - l) * 0.1
        lower_wick = min(o, c) - l
        return lower_wick > body * self.REJECTION_WICK_RATIO and c >= o * 0.998

    def _detect_rejection_bearish(self, row: pd.Series) -> bool:
        o, c, h, l = float(row["open"]), float(row["close"]), float(row["high"]), float(row["low"])
        body = abs(c - o) or (h - l) * 0.1
        upper_wick = h - max(o, c)
        return upper_wick > body * self.REJECTION_WICK_RATIO and c <= o * 1.002

    def _analyze(self, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        if df is None or len(df) < self.EMA_SLOW + 20:
            return None

        df = self._add_indicators(df)
        last = df.iloc[-1]
        prev = df.iloc[-2]

        if pd.isna(last["ema_slow"]) or pd.isna(last["bb_mid"]):
            return None

        price = float(last["close"])
        bb_upper = float(last["bb_upper"])
        bb_lower = float(last["bb_lower"])

        stack = self._detect_ema_stack(last, price)
        if stack == "TANGLED":
            return None

        if stack == "BEARISH":
            ex = self._exhaustion_score(df, "UP")
            if ex["score"] < self.MIN_EXHAUSTION_SCORE:
                return None
            if not (self._detect_rejection_bearish(last) or self._detect_rejection_bearish(prev)):
                return None
            recent_high = float(df["high"].tail(10).max())
            stop_loss = recent_high * 1.003
            take_profit = bb_lower
            risk = abs(price - stop_loss)
            reward = abs(take_profit - price)
            if risk <= 0:
                return None
            rr = reward / risk
            if rr < self.min_rr:
                return None
            return {
                "signal_type": SignalType.SHORT_REVERSAL,
                "score": ex["score"],
                "price": price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "rr": rr,
                "stack": stack,
                "exhaustion": ex,
                "reasons": [
                    f"Pila BAJISTA",
                    f"Distancia EMA55: {ex['distance_pct']*100:+.2f}%",
                    f"Velas seguidas: {ex['consecutive']}",
                    f"BB expandiendo: {ex['bb_expansion']*100:+.1f}%",
                    f"Tiempo en zona: {ex['candles_in_zone']} velas",
                    f"Score agotamiento: {ex['score']:.2f}",
                ],
            }

        if stack == "BULLISH":
            ex = self._exhaustion_score(df, "DOWN")
            if ex["score"] < self.MIN_EXHAUSTION_SCORE:
                return None
            if not (self._detect_rejection_bullish(last) or self._detect_rejection_bullish(prev)):
                return None
            recent_low = float(df["low"].tail(10).min())
            stop_loss = recent_low * 0.997
            take_profit = bb_upper
            risk = abs(price - stop_loss)
            reward = abs(take_profit - price)
            if risk <= 0:
                return None
            rr = reward / risk
            if rr < self.min_rr:
                return None
            return {
                "signal_type": SignalType.LONG_REVERSAL,
                "score": ex["score"],
                "price": price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "rr": rr,
                "stack": stack,
                "exhaustion": ex,
                "reasons": [
                    f"Pila ALCISTA",
                    f"Distancia EMA55: {ex['distance_pct']*100:+.2f}%",
                    f"Velas seguidas: {ex['consecutive']}",
                    f"BB expandiendo: {ex['bb_expansion']*100:+.1f}%",
                    f"Tiempo en zona: {ex['candles_in_zone']} velas",
                    f"Score agotamiento: {ex['score']:.2f}",
                ],
            }

        return None

    async def scan_all(self) -> List[Signal]:
        self.escaneos_totales += 1
        signals: List[Signal] = []
        now = time.time()

        logger.info(f"ESCANEO #{self.escaneos_totales} | {len(self.watchlist)} simbolos")

        for symbol in self.watchlist:
            try:
                if symbol in self._signal_cooldown:
                    if now - self._signal_cooldown[symbol] < self.signal_cooldown_seconds:
                        continue

                df = await self.api.fetch_ohlcv(symbol, timeframe="1h", limit=400)
                if df is None or len(df) < self.EMA_SLOW + 20:
                    continue

                analysis = self._analyze(df)
                if not analysis:
                    continue

                signal = Signal(
                    symbol=symbol,
                    signal_type=analysis["signal_type"],
                    score=analysis["score"],
                    risk_reward=analysis["rr"],
                    stop_loss=analysis["stop_loss"],
                    take_profit=analysis["take_profit"],
                    entry_price=analysis["price"],
                    df=df,
                    pattern_id=None,
                    timeframe="1h",
                    confidence=analysis["score"],
                    entry_reason=" | ".join(analysis["reasons"]),
                    bb_position="UPPER" if analysis["signal_type"].is_short() else "LOWER",
                )
                signals.append(signal)
                self._signal_cooldown[symbol] = now

                logger.info(
                    f"AGOTAMIENTO {analysis['signal_type'].value} {symbol} | "
                    f"Score {analysis['score']:.2f} | RR {analysis['rr']:.2f} | "
                    f"{' | '.join(analysis['reasons'])}"
                )

            except Exception as e:
                logger.debug(f"Error en {symbol}: {e}")
                continue

        if not signals:
            logger.info(f"ESCANEO #{self.escaneos_totales}: Sin agotamientos claros")
        else:
            logger.info(f"ESCANEO #{self.escaneos_totales}: {len(signals)} senales de agotamiento")

        return signals

    def register_trade(self, trade) -> int:
        return 0
