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
    timeframe: str = "15m"
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
    def __init__(
        self,
        api_manager: BybitAPIManager,
        watchlist: List[str],
        scan_interval: float = 20.0,
        min_score: float = 0.15,
        min_rr: float = 0.8,
        position_pct: float = 0.30,
        db_path: str = "patterns.db",
        signal_cooldown_seconds: int = 60,
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
        self.timeframes = timeframes or ["15m", "3m"]
        self._signal_cooldown: Dict[str, float] = {}
        self.escaneos_totales = 0
        logger.info(f"[Scanner] Iniciado | min_score={min_score} | min_rr={min_rr}")

    def _calc_rsi(self, prices: pd.Series, period: int = 14) -> pd.Series:
        delta = prices.diff()
        gain = delta.where(delta > 0, 0.0).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
        rs = gain / loss.replace(0, 1e-9)
        return 100 - (100 / (1 + rs))

    def _calc_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["rsi"] = self._calc_rsi(df["close"], 14)
        df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
        df["ema55"] = df["close"].ewm(span=55, adjust=False).mean()
        df["bb_mid"] = df["close"].rolling(20).mean()
        df["bb_std"] = df["close"].rolling(20).std()
        df["bb_upper"] = df["bb_mid"] + 2 * df["bb_std"]
        df["bb_lower"] = df["bb_mid"] - 2 * df["bb_std"]
        df["tr"] = np.maximum(
            df["high"] - df["low"],
            np.maximum(abs(df["high"] - df["close"].shift(1)),
                       abs(df["low"] - df["close"].shift(1)))
        )
        df["atr"] = df["tr"].rolling(14).mean()
        df["vol_avg"] = df["volume"].rolling(20).mean()
        df["vol_ratio"] = df["volume"] / df["vol_avg"].replace(0, 1e-9)
        return df

    def _analyze(self, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        if df is None or len(df) < 60:
            return None

        df = self._calc_indicators(df)
        last = df.iloc[-1]
        prev = df.iloc[-2]

        if pd.isna(last["rsi"]) or pd.isna(last["atr"]):
            return None

        price = float(last["close"])
        rsi = float(last["rsi"])
        atr = float(last["atr"])
        ema21 = float(last["ema21"])
        ema55 = float(last["ema55"])
        bb_upper = float(last["bb_upper"])
        bb_lower = float(last["bb_lower"])
        vol_ratio = float(last["vol_ratio"])
        prev_ema21 = float(prev["ema21"])
        prev_ema55 = float(prev["ema55"])

        score = 0.0
        signal_type = None
        reasons = []

        if rsi < 35 and price <= bb_lower * 1.01:
            signal_type = SignalType.LONG_REVERSAL
            score += 0.4
            reasons.append(f"RSI oversold {rsi:.1f}")
        elif rsi > 65 and price >= bb_upper * 0.99:
            signal_type = SignalType.SHORT_REVERSAL
            score += 0.4
            reasons.append(f"RSI overbought {rsi:.1f}")
        elif prev_ema21 <= prev_ema55 and ema21 > ema55:
            signal_type = SignalType.LONG_BREAKOUT
            score += 0.35
            reasons.append("Cruce EMA21/EMA55 alcista")
        elif prev_ema21 >= prev_ema55 and ema21 < ema55:
            signal_type = SignalType.SHORT_BREAKOUT
            score += 0.35
            reasons.append("Cruce EMA21/EMA55 bajista")

        if signal_type is None:
            return None

        if vol_ratio > 1.3:
            score += 0.2
            reasons.append(f"Volumen {vol_ratio:.2f}x")

        atr_pct = atr / price if price > 0 else 0
        if 0.002 < atr_pct < 0.05:
            score += 0.1
            reasons.append(f"ATR OK {atr_pct*100:.2f}%")

        return {
            "signal_type": signal_type,
            "score": min(score, 1.0),
            "price": price,
            "atr": atr,
            "rsi": rsi,
            "reasons": reasons,
        }

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

                df = await self.api.fetch_ohlcv(symbol, timeframe="15m", limit=120)
                if df is None or len(df) < 60:
                    continue

                analysis = self._analyze(df)
                if not analysis:
                    continue
                if analysis["score"] < self.min_score:
                    continue

                price = analysis["price"]
                atr = analysis["atr"]
                st = analysis["signal_type"]

                if st.is_long():
                    stop_loss = price - (atr * 1.5)
                    take_profit = price + (atr * 3.0)
                else:
                    stop_loss = price + (atr * 1.5)
                    take_profit = price - (atr * 3.0)

                risk = abs(price - stop_loss)
                reward = abs(take_profit - price)
                rr = reward / risk if risk > 0 else 0

                if rr < self.min_rr:
                    continue

                signal = Signal(
                    symbol=symbol,
                    signal_type=st,
                    score=analysis["score"],
                    risk_reward=rr,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    entry_price=price,
                    df=df,
                    pattern_id=None,
                    timeframe="15m",
                    confidence=analysis["score"],
                    entry_reason=" | ".join(analysis["reasons"]),
                )
                signals.append(signal)
                self._signal_cooldown[symbol] = now
                logger.info(f"SENAL {st.value} {symbol} | Score {analysis['score']:.2f} | RR {rr:.2f}")

            except Exception as e:
                logger.debug(f"Error en {symbol}: {e}")
                continue

        if not signals:
            logger.info(f"ESCANEO #{self.escaneos_totales}: Sin senales")
        else:
            logger.info(f"ESCANEO #{self.escaneos_totales}: {len(signals)} senales")

        return signals

    def register_trade(self, trade) -> int:
        return 0
