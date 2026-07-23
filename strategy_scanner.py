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
        self.timeframes = timeframes or ["15m"]
        self.patterns_by_tf = {}
        self._signal_cooldown = {}
        self._load_all_patterns()
        
        # 🧠 MEMORIA PARA EL CONTADOR DE TOQUES A LA EMA55
        self.ema55_touch_counter = {}

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

        for symbol in self.watchlist:
            if symbol in self._signal_cooldown:
                if now - self._signal_cooldown[symbol] < 300:
                    continue

            # 1. OBTENER TENDENCIA MACRO (1 HORA)
            macro_direction = "NEUTRAL"
            try:
                df_1h = await self.api.fetch_ohlcv(symbol, timeframe="1h", limit=100)
                if df_1h is not None and len(df_1h) > 40:
                    df_1h["ema55"] = df_1h["close"].ewm(span=55, adjust=False).mean()
                    df_1h["ema144"] = df_1h["close"].ewm(span=144, adjust=False).mean()
                    current_ema55 = df_1h["ema55"].iloc[-1]
                    current_ema144 = df_1h["ema144"].iloc[-1]
                    
                    if current_ema55 > current_ema144:
                        macro_direction = "BULLISH"
                    elif current_ema55 < current_ema144:
                        macro_direction = "BEARISH"
            except Exception as exc:
                logger.debug(f"[MarketScanner] No se pudo obtener macro 1h para {symbol}: {exc}")

            # 2. ANALIZAR TIMEFRAME 15M
            for timeframe in self.timeframes:
                try:
                    df = await self.api.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
                    if df is None or len(df) < 40:
                        continue

                    behavior = self._describe_behavior(df)
                    symbol_code = self._normalize_symbol(symbol)
                    
                    matches = self._match_patterns(symbol_code, behavior, timeframe, macro_direction)

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

        volume_average = float(df["volume"].rolling(20).mean().iloc[-2] or 0.0)
        price = float(current["close"])

        prev_candle = candle_pattern(prior)
        prev_vol_ratio = float(prior["volume"]) / max(volume_average, 1e-9)
        previous_breakout = "NO"
        
        if prev_candle in ["STRONG_GREEN", "REJECTION", "GREEN"] and prev_vol_ratio >= 1.8:
            previous_breakout = "YES"

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
            "previous_breakout": previous_breakout,
        }

    def _match_patterns(self, symbol_code: str, behavior: Dict[str, Any], timeframe: str, macro_direction: str) -> List[Dict[str, Any]]:
        matches = []
        patterns = self.patterns_by_tf.get(timeframe, [])

        prev_breakout = behavior.get("previous_breakout", "NO")

        # Variables de comportamiento
        precio_vs_ema55 = behavior.get("precio_vs_ema55", "")
        ema55_vs_ema144 = behavior.get("ema55_vs_ema144", "")
        ema144_vs_ema233 = behavior.get("ema144_vs_ema233", "")
        bb_state = behavior.get("bb_estado", "CONTRACTING")
        bb_precio = behavior.get("bb_precio", "MID")

        # 🧠 ACTUALIZAR CONTADOR DE TOQUES A LA EMA55
        if precio_vs_ema55 in ["TOUCHING", "NEAR"]:
            if symbol_code not in self.ema55_touch_counter:
                self.ema55_touch_counter[symbol_code] = 0
            self.ema55_touch_counter[symbol_code] += 1
            logger.debug(f"[MarketScanner] 🧠 {symbol_code} tocó la EMA55. Contador: {self.ema55_touch_counter[symbol_code]}")

        current_touches = self.ema55_touch_counter.get(symbol_code, 0)

        for pattern in patterns:
            if pattern.get("symbol") not in {symbol_code, "UNIVERSAL"}:
                continue

            if pattern.get("timeframe") != timeframe:
                continue

            signal_type = pattern.get("signal_type", "")

            # 🛡️ REGLA 1: FILTRO MACRO OBLIGATORIO (144 vs 233)
            if signal_type in ["LONG_BREAKOUT", "LONG_REVERSAL"] and ema144_vs_ema233 != "ABOVE":
                continue
            if signal_type in ["SHORT_BREAKOUT", "SHORT_REVERSAL"] and ema144_vs_ema233 != "BELOW":
                continue
            if macro_direction == "NEUTRAL":
                continue

            # 🛡️ REGLA 2: EVITAR FALSOS EN PICO ANTERIOR
            if prev_breakout == "YES":
                continue

            # 🛡️ REGLA 3: SISTEMA DE PRIORIDAD POR TOQUES A LA EMA55
            if current_touches > 4:
                precio_vs_ema144 = behavior.get("precio_vs_ema144", "")
                precio_vs_ema233 = behavior.get("precio_vs_ema233", "")
                
                if signal_type in ["LONG_BREAKOUT", "LONG_REVERSAL"]:
                    if precio_vs_ema144 not in ["TOUCHING", "NEAR"] and precio_vs_ema233 not in ["TOUCHING", "NEAR"]:
                        continue
                if signal_type in ["SHORT_BREAKOUT", "SHORT_REVERSAL"]:
                    if precio_vs_ema144 not in ["TOUCHING", "NEAR"] and precio_vs_ema233 not in ["TOUCHING", "NEAR"]:
                        continue

            # 🛡️ REGLA 4: EVITAR SQUEEZES (Bandas de Bollinger apretadas)
            if bb_state in ["MAX_SQUEEZE", "SQUEEZE"]:
                continue

            # Calcular puntaje base del patrón
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

            # 🟢 BONIFICACIONES
            # 1. Bonificación por toque temprano (Prioridad a la EMA55 en los primeros 3 toques)
            if current_touches <= 3 and precio_vs_ema55 == "TOUCHING":
                match_ratio += 0.20

            # 2. Bonificación por Fuerza Macrof (Cuando la 144 está muy por encima de la 233)
            ema144_val = float(behavior.get("ema144_vs_ema233", {}))
            ema233_val = float(behavior.get("ema144_vs_ema233", {}))
            if ema144_val != 0 and ema233_val != 0:
                distance_pct = abs(ema144_val - ema233_val) / ema233_val
                if distance_pct > 0.015:
                    match_ratio += 0.15
                    logger.debug(f"[MarketScanner] 🔥 Bonificación por Fuerza Macrof en {symbol_code}")

            # 🤖 IA DE PATRONES: Evaluar si el patrón actual tiene éxito histórico en la BD
            ai_bonus = self._ai_evaluate_pattern(symbol_code, behavior, signal_type, timeframe)
            if ai_bonus is not None:
                match_ratio += ai_bonus
                logger.debug(f"[MarketScanner] 🤖 IA evaluó {symbol_code} con bonificación de +{ai_bonus:.2f}")

            if match_ratio >= 0.40:
                matches.append({
                    "pattern": pattern,
                    "match_ratio": match_ratio,
                    "score": score,
                    "total": total,
                })

        return matches

    # 🤖 NUEVO SISTEMA DE INTELIGENCIA ARTIFICIAL BASADO EN BASE DE DATOS
    def _ai_evaluate_pattern(self, symbol_code: str, behavior: Dict[str, Any], signal_type: str, timeframe: str) -> Optional[float]:
        try:
            # Conectar a la base de datos
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                
                # Construir una consulta SQL para buscar patrones parecidos al actual (excluyendo el que se está evaluando)
                # Buscamos patrones que hayan tenido el mismo signal_type y timeframe
                query = """
                    SELECT resultado, COUNT(*) as count 
                    FROM patterns 
                    WHERE signal_type = ? 
                    AND timeframe = ?
                    AND ema55_vs_ema144 = ?
                    AND ema144_vs_ema233 = ?
                    AND bb_precio = ?
                    GROUP BY resultado
                    ORDER BY count DESC
                    LIMIT 5
                """
                
                params = (
                    signal_type,
                    timeframe,
                    behavior.get("ema55_vs_ema144", "FLAT"),
                    behavior.get("ema144_vs_ema233", "FLAT"),
                    behavior.get("bb_precio", "MID"),
                )
                
                cursor = conn.execute(query, params)
                rows = cursor.fetchall()
                
                if not rows:
                    return None
                
                # Si encontramos patrones parecidos, calculamos la ratio de éxito
                win_count = 0
                loss_count = 0
                
                for row in rows:
                    if row['resultado'] == 'WIN':
                        win_count = row['count']
                    elif row['resultado'] == 'LOSS':
                        loss_count = row['count']
                
                total = win_count + loss_count
                if total == 0:
                    return None
                
                # Si el porcentaje de éxito es mayor al 60%, damos una bonificación
                success_rate = win_count / total
                if success_rate > 0.60:
                    return 0.10  # Bonificación del 10%
                elif success_rate < 0.40:
                    return -0.10  # Penalización del 10% si tiene mal historial
                
                return None
                
        except Exception as e:
            logger.debug(f"[MarketScanner] Error en IA de patrones para {symbol_code}: {e}")
            return None

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
