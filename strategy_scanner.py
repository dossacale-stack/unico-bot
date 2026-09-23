# strategy_scanner.py - Lógica EMA Fibonacci 55/144/233 + BB 21,2
# =================================================================
# Basado en estructura real de mercado:
#   1. Pila de EMAs 55 > 144 > 233 (Fibonacci) define dirección
#   2. Bollinger Bands 21,2 definen zona de entrada
#   3. Rechazo en la banda opuesta a la pila
#   4. Confirmación con vela de rechazo

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
    """
    Scanner basado en EMA Fibonacci (55/144/233) + Bollinger Bands (21,2).

    Reglas:
    1. Pila alcista (55>144>233, precio arriba) + pullback a BB media/inferior
       + rechazo → LONG
    2. Pila bajista (55<144<233, precio abajo) + rebote a BB media/superior
       + rechazo → SHORT
    3. Pila entrelazada → NO OPERAR
    """

    # Parámetros de la estrategia
    EMA_FAST = 55
    EMA_MID = 144
    EMA_SLOW = 233
    BB_PERIOD = 21
    BB_STD = 2.0
    REJECTION_WICK_RATIO = 1.5   # mecha debe ser 1.5x el cuerpo
    MIN_TOUCH_DISTANCE = 0.005   # precio debe estar a 0.5% del BB o EMA

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
        self.timeframes = timeframes or ["15m"]
        self._signal_cooldown: Dict[str, float] = {}
        self.escaneos_totales = 0
        logger.info(
            f"[Scanner] EMA Fibonacci {self.EMA_FAST}/{self.EMA_MID}/{self.EMA_SLOW} "
            f"+ BB({self.BB_PERIOD},{self.BB_STD}) | min_score={min_score}"
        )

    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        # EMAs Fibonacci
        df["ema_fast"] = df["close"].ewm(span=self.EMA_FAST, adjust=False).mean()
        df["ema_mid"] = df["close"].ewm(span=self.EMA_MID, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=self.EMA_SLOW, adjust=False).mean()

        # Bollinger Bands 21, 2
        df["bb_mid"] = df["close"].rolling(self.BB_PERIOD).mean()
        df["bb_std"] = df["close"].rolling(self.BB_PERIOD).std()
        df["bb_upper"] = df["bb_mid"] + self.BB_STD * df["bb_std"]
        df["bb_lower"] = df["bb_mid"] - self.BB_STD * df["bb_std"]

        # ATR para SL dinámico
        df["tr"] = np.maximum(
            df["high"] - df["low"],
            np.maximum(abs(df["high"] - df["close"].shift(1)),
                       abs(df["low"] - df["close"].shift(1)))
        )
        df["atr"] = df["tr"].rolling(14).mean()

        # Volumen relativo
        df["vol_avg"] = df["volume"].rolling(20).mean()
        df["vol_ratio"] = df["volume"] / df["vol_avg"].replace(0, 1e-9)

        return df

    def _detect_ema_stack(self, row: pd.Series, price: float) -> str:
        """
        Detecta la pila de EMAs.
        Retorna: BULLISH, BEARISH, o TANGLED
        """
        ef = float(row["ema_fast"])
        em = float(row["ema_mid"])
        es = float(row["ema_slow"])

        # Pila alcista: ema_fast > ema_mid > ema_slow Y precio arriba de la fast
        if ef > em and em > es and price > ef:
            return "BULLISH"
        # Pila bajista: ema_fast < ema_mid < ema_slow Y precio abajo de la fast
        if ef < em and em < es and price < ef:
            return "BEARISH"
        return "TANGLED"

    def _detect_rejection_bullish(self, row: pd.Series) -> bool:
        """
        Vela de rechazo alcista:
        - Cuerpo pequeño o verde
        - Mecha inferior larga (> 1.5x cuerpo)
        - Cierra arriba del mínimo
        """
        o = float(row["open"])
        c = float(row["close"])
        h = float(row["high"])
        l = float(row["low"])

        body = abs(c - o)
        if body < 1e-9:
            body = (h - l) * 0.1  # mínimo

        lower_wick = min(o, c) - l
        # Mecha inferior larga Y cuerpo pequeño
        if lower_wick > body * self.REJECTION_WICK_RATIO and c >= o * 0.998:
            return True
        return False

    def _detect_rejection_bearish(self, row: pd.Series) -> bool:
        """
        Vela de rechazo bajista:
        - Mecha superior larga (> 1.5x cuerpo)
        - Cierra abajo del máximo
        """
        o = float(row["open"])
        c = float(row["close"])
        h = float(row["high"])
        l = float(row["low"])

        body = abs(c - o)
        if body < 1e-9:
            body = (h - l) * 0.1

        upper_wick = h - max(o, c)
        if upper_wick > body * self.REJECTION_WICK_RATIO and c <= o * 1.002:
            return True
        return False

    def _find_recent_swing(self, df: pd.DataFrame, lookback: int = 20) -> Dict[str, float]:
        """Encuentra swing high y swing low recientes."""
        recent = df.tail(lookback)
        return {
            "swing_high": float(recent["high"].max()),
            "swing_low": float(recent["low"].min()),
        }

    def _analyze(self, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        Lógica central: detecta setup según tu estrategia.
        """
        if df is None or len(df) < self.EMA_SLOW + 20:
            return None

        df = self._add_indicators(df)
        last = df.iloc[-1]
        prev = df.iloc[-2]

        if pd.isna(last["ema_slow"]) or pd.isna(last["bb_mid"]) or pd.isna(last["atr"]):
            return None

        price = float(last["close"])
        ema_fast = float(last["ema_fast"])
        ema_mid = float(last["ema_mid"])
        ema_slow = float(last["ema_slow"])
        bb_upper = float(last["bb_upper"])
        bb_mid = float(last["bb_mid"])
        bb_lower = float(last["bb_lower"])
        atr = float(last["atr"])
        vol_ratio = float(last["vol_ratio"])

        # 1. Detectar pila de EMAs
        stack = self._detect_ema_stack(last, price)
        if stack == "TANGLED":
            return None  # No operar

        score = 0.0
        reasons = []
        signal_type = None

        # Distancia al BB (como % del precio)
        dist_upper = abs(price - bb_upper) / price if price > 0 else 1
        dist_lower = abs(price - bb_lower) / price if price > 0 else 1
        dist_mid = abs(price - bb_mid) / price if price > 0 else 1

        # ══════ PILA ALCISTA → buscar LONG en pullback ══════
        if stack == "BULLISH":
            # Precio debe estar cerca de BB media o inferior (pullback)
            if dist_mid < 0.01 or dist_lower < 0.01:
                score += 0.35
                reasons.append(f"Pullback a BB {'media' if dist_mid < dist_lower else 'inferior'}")

                # ¿EMA fast o mid actúa como soporte?
                ema_touch = min(
                    abs(price - ema_fast) / price,
                    abs(price - ema_mid) / price
                )
                if ema_touch < self.MIN_TOUCH_DISTANCE:
                    score += 0.20
                    reasons.append(f"Toque EMA soporte")

                # Vela de rechazo alcista
                if self._detect_rejection_bullish(last) or self._detect_rejection_bullish(prev):
                    score += 0.25
                    reasons.append("Vela de rechazo alcista")

                # Volumen confirma
                if vol_ratio > 1.2:
                    score += 0.10
                    reasons.append(f"Volumen {vol_ratio:.1f}x")

                # Precio cerca del BB inferior = mejor entrada
                if dist_lower < 0.005:
                    score += 0.10
                    reasons.append("En BB inferior")

                if score >= self.min_score:
                    signal_type = SignalType.LONG_REVERSAL

        # ══════ PILA BAJISTA → buscar SHORT en rebote ══════
        elif stack == "BEARISH":
            # Precio debe estar cerca de BB media o superior (rebote)
            if dist_mid < 0.01 or dist_upper < 0.01:
                score += 0.35
                reasons.append(f"Rebote a BB {'media' if dist_mid < dist_upper else 'superior'}")

                ema_touch = min(
                    abs(price - ema_fast) / price,
                    abs(price - ema_mid) / price
                )
                if ema_touch < self.MIN_TOUCH_DISTANCE:
                    score += 0.20
                    reasons.append(f"Toque EMA resistencia")

                if self._detect_rejection_bearish(last) or self._detect_rejection_bearish(prev):
                    score += 0.25
                    reasons.append("Vela de rechazo bajista")

                if vol_ratio > 1.2:
                    score += 0.10
                    reasons.append(f"Volumen {vol_ratio:.1f}x")

                if dist_upper < 0.005:
                    score += 0.10
                    reasons.append("En BB superior")

                if score >= self.min_score:
                    signal_type = SignalType.SHORT_REVERSAL

        if signal_type is None:
            return None

        # Calcular SL y TP
        swing = self._find_recent_swing(df, lookback=20)

        if signal_type.is_long():
            # SL debajo del swing low reciente o EMA mid, lo que esté más cerca
            sl_candidates = [swing["swing_low"], ema_mid * 0.998]
            stop_loss = max(sl_candidates)  # el más cercano al precio
            # TP en BB superior o swing high
            take_profit = bb_upper
        else:
            # SL arriba del swing high reciente o EMA mid
            sl_candidates = [swing["swing_high"], ema_mid * 1.002]
            stop_loss = min(sl_candidates)
            take_profit = bb_lower

        # Validar SL (no muy lejos)
        risk = abs(price - stop_loss)
        if risk / price > 0.05:  # SL > 5% → descartar
            return None

        reward = abs(take_profit - price)
        rr = reward / risk if risk > 0 else 0

        # Reducir score si RR es malo
        if rr < self.min_rr:
            return None

        # Bonus por RR alto
        if rr > 3:
            score += 0.05
            reasons.append(f"R:R {rr:.1f}")

        return {
            "signal_type": signal_type,
            "score": min(score, 1.0),
            "price": price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "rr": rr,
            "atr": atr,
            "stack": stack,
            "reasons": reasons,
            "bb_position": (
                "UPPER" if dist_upper < dist_lower else "LOWER"
                if dist_lower < 0.01 else "MID"
            ),
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

                # Necesitamos muchas velas para EMA 233
                df = await self.api.fetch_ohlcv(symbol, timeframe="15m", limit=300)
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
                    timeframe="15m",
                    confidence=analysis["score"],
                    entry_reason=" | ".join(analysis["reasons"]),
                    bb_position=analysis.get("bb_position", ""),
                )
                signals.append(signal)
                self._signal_cooldown[symbol] = now

                logger.info(
                    f"SENAL {analysis['signal_type'].value} {symbol} | "
                    f"Score {analysis['score']:.2f} | RR {analysis['rr']:.2f} | "
                    f"Stack {analysis['stack']} | {' | '.join(analysis['reasons'])}"
                )

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
