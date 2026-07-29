import logging
import os
import re
import sqlite3
import time
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
    timeframe: str = "15m"


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
    "adx_tendencia",
    "daily_pct_change",      # 🟢 NUEVO: Cambio % 24h
    "ema_touch_count",       # 🟢 NUEVO: Conteo de toques a la EMA
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
        signal_cooldown_seconds: int = 60,
        timeframes: List[str] = None,
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
        self.patterns_by_tf = {}
        self._signal_cooldown = {}
        self._load_all_patterns()

    def _normalize_symbol(self, symbol: str) -> str:
        return re.sub(r"[^\w]", "", symbol).upper()

    def _load_all_patterns(self) -> None:
        if not os.path.exists(self.db_path):
            logger.warning("[MarketScanner] No se encontró DB de patrones. Usando seed_patterns.")
            for tf in self.timeframes:
                self.patterns_by_tf[tf] = [p.copy() for p in seed_patterns.PATTERNS if p.get("timeframe") == tf]
            return

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                for tf in self.timeframes:
                    rows = conn.execute("""
                        SELECT * FROM patterns
                        WHERE timeframe = ? AND resultado != 'EVITAR'
                    """, (tf,)).fetchall()
                    self.patterns_by_tf[tf] = [dict(row) for row in rows]
                    logger.info(f"[MarketScanner] Cargados {len(self.patterns_by_tf[tf])} patrones para {tf}")
        except Exception as exc:
            logger.warning(f"[MarketScanner] Error cargando patrones: {exc}. Usando seed_patterns.")
            for tf in self.timeframes:
                self.patterns_by_tf[tf] = [p.copy() for p in seed_patterns.PATTERNS if p.get("timeframe") == tf]

    def _can_signal(self, symbol: str) -> bool:
        now = time.time()
        if symbol in self._signal_cooldown:
            elapsed = now - self._signal_cooldown[symbol]
            if elapsed < self.signal_cooldown_seconds:
                return False
        return True

    async def scan_all(self) -> List[Signal]:
        signals: List[Signal] = []
        now = time.time()

        # 🟢 NUEVO: Obtener datos de cambio porcentual 24h de toda la watchlist en una sola llamada
        ticker_map = {}
        try:
            # Usamos el método HTTP directamente o del manager para obtener tickers
            session = self.api.exchange
            response = session.get_tickers(category="linear")
            tickers = response["result"]["list"]
            for t in tickers:
                ticker_map[t["symbol"]] = float(t.get("price24hPcnt", 0.0))
        except Exception as e:
            logger.debug(f"[MarketScanner] No se pudo obtener tickers 24h, usando 0.0: {e}")

        for symbol in self.watchlist:
            if symbol in self._signal_cooldown:
                if now - self._signal_cooldown[symbol] < 300:
                    continue

            # Obtener el cambio diario para este símbolo
            daily_pct = ticker_map.get(symbol, 0.0)

            # 1. OBTENER TENDENCIA MACRO (1 HORA)
            macro_angle = "FLAT"
            try:
                df_1h = await self.api.fetch_ohlcv(symbol, timeframe="1h", limit=100)
                if df_1h is not None and len(df_1h) > 40:
                    df_1h["ema144"] = df_1h["close"].ewm(span=144, adjust=False).mean()
                    df_1h["ema233"] = df_1h["close"].ewm(span=233, adjust=False).mean()
                    
                    ema144_slope_1h = df_1h["ema144"].iloc[-1] - df_1h["ema144"].iloc[-5]
                    ema233_slope_1h = df_1h["ema233"].iloc[-1] - df_1h["ema233"].iloc[-5]
                    
                    if ema144_slope_1h > 0 and ema233_slope_1h > 0:
                        macro_angle = "BULLISH"
                    elif ema144_slope_1h < 0 and ema233_slope_1h < 0:
                        macro_angle = "BEARISH"
            except Exception as exc:
                logger.debug(f"[MarketScanner] No se pudo obtener macro 1h para {symbol}: {exc}")

            # 2. ANALIZAR TIMEFRAMES
            for timeframe in self.timeframes:
                try:
                    df = await self.api.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
                    if df is None or len(df) < 40:
                        continue

                    # Pasar el cambio diario y el marco temporal a la descripción
                    behavior = self._describe_behavior(df, daily_pct, timeframe)
                    symbol_code = self._normalize_symbol(symbol)
                    
                    matches = self._match_patterns(symbol_code, behavior, timeframe, macro_angle)

                    if not matches:
                        continue

                    best = max(matches, key=lambda item: (item["match_ratio"], item["pattern"].get("rb_real", 0.0)))
                    signal = self._build_signal(symbol, df, behavior, best, timeframe)

                    if signal and signal.score >= self.min_score:
                        if self._can_signal(symbol):
                            self._signal_cooldown[symbol] = now
                            signals.append(signal)
                            logger.debug(f"[MarketScanner] Señal {symbol} {timeframe} Score: {signal.score:.2f}")

                except Exception as exc:
                    logger.debug(f"[MarketScanner] Error en {symbol} {timeframe}: {exc}")

        if not signals:
            logger.info("[MarketScanner] Ninguna señal encontrada en este ciclo.")
        else:
            logger.info(f"[MarketScanner] {len(signals)} señales encontradas")

        return signals

    # 🟢 NUEVO: Cuenta los toques a la EMA 55 después del último cruce
    def _count_touches_after_cross(self, df: pd.DataFrame, lookback: int = 50) -> int:
        """
        Cuenta cuántas veces el precio ha estado NEAR o TOUCHING la EMA55
        después del último cruce alcista de la EMA55 sobre la EMA144.
        Si el precio ya tocó la EMA más de 4 veces, es una señal de agotamiento.
        """
        try:
            df = df.copy()
            df['ema55'] = df['close'].ewm(span=55, adjust=False).mean()
            df['ema144'] = df['close'].ewm(span=144, adjust=False).mean()
            
            # Detectar cruces alcistas en las últimas 50 velas
            df['cross_bull'] = (df['ema55'] > df['ema144']) & (df['ema55'].shift(1) <= df['ema144'].shift(1))
            cross_idx = df[df['cross_bull']].index
            
            if len(cross_idx) == 0:
                return 0
            
            # Tomar el último cruce
            last_cross_idx = cross_idx[-1]
            df_after_cross = df.loc[last_cross_idx:]
            
            # Contar las veces que el precio estuvo cerca de la EMA55 (menos del 0.8% de distancia)
            touch_count = 0
            for idx, row in df_after_cross.iterrows():
                diff = abs(float(row['close']) - float(row['ema55']))
                pct = diff / float(row['close'])
                if pct < 0.008: # Equivalente a NEAR o TOUCHING
                    touch_count += 1
            
            return touch_count
            
        except Exception as e:
            logger.debug(f"[MarketScanner] Error contando toques a EMA: {e}")
            return 999  # Si hay error, ignoramos la señal

    def _describe_behavior(self, df: pd.DataFrame, daily_pct: float, timeframe: str) -> Dict[str, Any]:
        df = df.copy()
        df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
        df["ema55"] = df["close"].ewm(span=55, adjust=False).mean()
        df["ema144"] = df["close"].ewm(span=144, adjust=False).mean()
        df["ema233"] = df["close"].ewm(span=233, adjust=False).mean()
        df["bb_mid"] = df["close"].rolling(20).mean()
        df["bb_std"] = df["close"].rolling(20).std()
        df["bb_upper"] = df["bb_mid"] + 2 * df["bb_std"]
        df["bb_lower"] = df["bb_mid"] - 2 * df["bb_std"]

        # 🟢 NUEVO: Cálculo de ADX
        df["tr"] = np.maximum(
            df["high"] - df["low"],
            np.maximum(
                abs(df["high"] - df["close"].shift(1)),
                abs(df["low"] - df["close"].shift(1))
            )
        )
        df["atr"] = df["tr"].rolling(14).mean()
        df["up"] = df["high"] - df["high"].shift(1)
        df["down"] = df["low"].shift(1) - df["low"]
        df["+dm"] = np.where((df["up"] > df["down"]) & (df["up"] > 0), df["up"], 0.0)
        df["-dm"] = np.where((df["down"] > df["up"]) & (df["down"] > 0), df["down"], 0.0)
        df["+di"] = 100 * (df["+dm"].rolling(14).mean() / df["atr"])
        df["-di"] = 100 * (df["-dm"].rolling(14).mean() / df["atr"])
        df["dx"] = 100 * abs(df["+di"] - df["-di"]) / (df["+di"] + df["-di"])
        df["adx"] = df["dx"].rolling(14).mean()

        current = df.iloc[-1]
        prior = df.iloc[-2]

        # 🟢 NUEVO: Calcular toques a la EMA después del cruce
        ema_touch_count = self._count_touches_after_cross(df)

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
            if close >= mid:
                return "MID_TO_UPPER"
            return "LOWER"

        def bb_state_label(mid: float, upper: float, lower: float, recent: pd.DataFrame) -> str:
            width = (upper - lower) / max(mid, 1e-6)
            previous_width = ((recent["bb_upper"] - recent["bb_lower"]) / recent["bb_mid"].replace(0, np.nan)).iloc[-5:-1].mean()
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

        def adx_tendencia_label(adx_val: float) -> str:
            if adx_val > 30:
                return "STRONG"
            elif adx_val > 20:
                return "WEAK"
            else:
                return "RANGE"

        volume_average = float(df["volume"].rolling(20).mean().iloc[-2] or 0.0)
        price = float(current["close"])

        return {
            "ema21_vs_ema55": ema_relation(float(current["ema21"]), float(current["ema55"]),
                                          float(prior["ema21"]), float(prior["ema55"])),
            "ema55_vs_ema144": ema_relation(float(current["ema55"]), float(current["ema144"]),
                                           float(prior["ema55"]), float(prior["ema144"])),
            "ema144_vs_ema233": ema_relation(float(current["ema144"]), float(current["ema233"]),
                                            float(prior["ema144"]), float(prior["ema233"])),
            "ema21_slope": slope_label(df["ema21"]),
            "ema144_slope": slope_label(df["ema144"]),
            "precio_vs_ema21": position_label(price, float(current["ema21"])),
            "precio_vs_ema55": position_label(price, float(current["ema55"])),
            "precio_vs_ema144": position_label(price, float(current["ema144"])),
            "precio_vs_ema233": position_label(price, float(current["ema233"])),
            "bb_estado": bb_state_label(float(current["bb_mid"]), float(current["bb_upper"]),
                                       float(current["bb_lower"]), df.iloc[-10:]),
            "bb_precio": bb_price_label(price, float(current["bb_mid"]),
                                       float(current["bb_lower"]), float(current["bb_upper"])),
            "volumen": volume_label(float(current["volume"]), volume_average),
            "patron_vela": candle_pattern(current),
            "fib_zona": fib_zone(price),
            "entry_price": price,
            "adx_tendencia": adx_tendencia_label(float(current["adx"])),
            "daily_pct_change": daily_pct,      # 🟢 NUEVO: Pasar el cambio diario
            "ema_touch_count": ema_touch_count, # 🟢 NUEVO: Pasar el conteo de toques
        }

    def _evaluate_golden_rules(self, behavior: Dict[str, Any], macro_angle: str, timeframe: str) -> float:
        score = 0.0
        bb_price = behavior.get("bb_precio", "MID")
        ema144_slope = behavior.get("ema144_slope", "FLAT")
        volumen = behavior.get("volumen", "LOW")
        precio_vs_ema144 = behavior.get("precio_vs_ema144", "")
        adx_force = behavior.get("adx_tendencia", "RANGE")
        daily_pct = behavior.get("daily_pct_change", 0.0)      # 🟢 NUEVO
        ema_touch_count = behavior.get("ema_touch_count", 0)   # 🟢 NUEVO
        signal_type = behavior.get("signal_type", "UNKNOWN")

        # 🛑 FILTRO 1: Si el activo está extremadamente sobrecomprado (>30% en el día), no comprar.
        if daily_pct > 0.30 and "LONG" in str(signal_type):
            return 0.0
        if daily_pct < -0.30 and "SHORT" in str(signal_type):
            return 0.0

        # 🛑 FILTRO 2: Si el precio ha tocado la EMA 55 más de 4 veces después del último cruce, es una trampa.
        if ema_touch_count > 4:
            # Penalizar fuertemente si está en la zona de compra/venta
            if precio_vs_ema144 in ["ABOVE", "BELOW"]:
                return 0.0
            else:
                # Si el precio está cerca de la EMA pero ya la tocó varias veces, seguir penalizando
                score -= 30.0

        # ⭐ REGLA DE ORO: CAZA DE ROMPIMIENTO REAL (Día 5)
        if adx_force == "STRONG" and macro_angle == "BULLISH" and precio_vs_ema144 == "ABOVE":
            if volumen in ["HIGH", "MEDIUM"]:
                score += 30.0

        # --- REGLA 1: COMPRA DE BAJO RIESGO (BANDA INFERIOR) ---
        if macro_angle == "BULLISH":
            if bb_price == "LOWER":
                score += 25.0

        # --- REGLA 2: VENTA DE CONTINUACIÓN (BANDA SUPERIOR O MEDIA EN BAJISTA) ---
        if macro_angle == "BEARISH":
            if bb_price in ["UPPER", "MID_TO_UPPER"]:
                score += 20.0

        # --- REGLA 3: COMPRA EN M3 (ANCLAJE A EMA55) ---
        if timeframe == "3m" and ema144_slope == "UP":
            if behavior.get("precio_vs_ema55") in ["TOUCHING", "NEAR"]:
                score += 25.0

        # --- REGLA 4: RECHAZO EN BANDA SUPERIOR (TRAMPA DE TECHO) ---
        if bb_price == "UPPER":
            candle = behavior.get("patron_vela", "NEUTRAL")
            if candle in ["REJECTION", "STRONG_RED"]:
                score += 15.0

        # --- REGLA 5: ANCLAJE MACRO (Filtro de seguridad) ---
        if bb_price == "MID" and macro_angle == "FLAT":
            score -= 20.0

        return max(0.0, score)

    def _match_patterns(self, symbol_code: str, behavior: Dict[str, Any], timeframe: str, macro_angle: str) -> List[Dict[str, Any]]:
        matches = []
        patterns = self.patterns_by_tf.get(timeframe, [])
        
        golden_score = self._evaluate_golden_rules(behavior, macro_angle, timeframe)
        
        if golden_score < 15.0:
            return matches

        for pattern in patterns:
            if pattern.get("symbol") not in {symbol_code, "UNIVERSAL"}:
                continue

            if pattern.get("timeframe") != timeframe:
                continue

            signal_type = pattern.get("signal_type", "")

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
            
            final_score = match_ratio + (golden_score / 100.0) * 0.5
            final_score = min(final_score, 1.0)

            # 🚨 FILTRO ANTI-TRAMPAS
            if "BREAKOUT" in signal_type:
                if behavior.get("adx_tendencia") in ["WEAK", "RANGE"]:
                    continue
                if final_score < 0.40:
                    continue
            else:
                if final_score < 0.40:
                    continue

            matches.append({
                "pattern": pattern,
                "match_ratio": final_score,
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
        timeframe: str,
    ) -> Optional[Signal]:
        pattern = matched["pattern"]
        signal_type_str = pattern["signal_type"]

        if signal_type_str == "NO_SIGNAL":
            return None

        try:
            signal_type = SignalType(signal_type_str)
        except ValueError:
            logger.warning(f"[MarketScanner] Tipo inválido '{signal_type_str}' para {symbol}")
            return None

        entry_price = float(behavior["entry_price"])
        score = float(matched["match_ratio"])

        return Signal(
            symbol=symbol,
            signal_type=signal_type,
            score=score,
            risk_reward=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            entry_price=entry_price,
            df=df,
            pattern_id=pattern.get("id"),
            timeframe=timeframe,
        )
