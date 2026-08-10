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
    timeframe: str = "3m"


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
        self.timeframes = ["15m", "3m"]
        self.patterns_by_tf = {}
        self._signal_cooldown = {}
        self._load_all_patterns()

        # 🟢 NUEVO: Caché de contexto histórico para no sobrecargar la API
        self._historical_cache = {} 
        self._historical_cache_time = {}

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

    # 🟢 NUEVO: Obtener el contexto histórico con caché
    async def _get_historical_context(self, symbol: str) -> Dict[str, Any]:
        now = time.time()
        # Si ya tenemos datos y no ha pasado más de 1 hora, usamos la caché
        if symbol in self._historical_cache and (now - self._historical_cache_time.get(symbol, 0)) < 3600:
            return self._historical_cache[symbol]

        context = {
            "day_high": 0.0, "day_low": 0.0,
            "week_high": 0.0, "week_low": 0.0,
            "month_high": 0.0, "month_low": 0.0,
            "ath": 0.0, "atl": 0.0,
            "position": "MID_RANGE"
        }

        try:
            # 1. Rango Diario (24h)
            ticker = await self.api._safe_call(
                lambda: self.api.exchange.fetch_ticker(symbol),
                endpoint_type="public"
            )
            context["day_high"] = ticker.get("high", 0.0)
            context["day_low"] = ticker.get("low", 0.0)

            # 2. Rango Semanal (últimas 168 velas de 1h)
            df_week = await self.api.fetch_ohlcv(symbol, timeframe="1h", limit=168)
            if df_week is not None and not df_week.empty:
                context["week_high"] = df_week["high"].max()
                context["week_low"] = df_week["low"].min()

            # 3. Rango Mensual e Histórico (720 velas de 1h ≈ 30 días)
            df_hist = await self.api.fetch_ohlcv(symbol, timeframe="1h", limit=720)
            if df_hist is not None and not df_hist.empty:
                context["month_high"] = df_hist["high"].max()
                context["month_low"] = df_hist["low"].min()
                context["ath"] = df_hist["high"].max()
                context["atl"] = df_hist["low"].min()

            # 4. Clasificar la posición
            if context["ath"] > 0:
                if ticker["last"] >= (context["ath"] * 0.95):
                    context["position"] = "ATH_ZONE"
                elif ticker["last"] <= (context["atl"] * 1.05):
                    context["position"] = "ATL_ZONE"
                elif ticker["last"] >= (context["month_high"] * 0.95):
                    context["position"] = "MONTH_HIGH"
                elif ticker["last"] <= (context["month_low"] * 1.05):
                    context["position"] = "MONTH_LOW"

            # Guardar en caché
            self._historical_cache[symbol] = context
            self._historical_cache_time[symbol] = now

        except Exception as e:
            logger.debug(f"[MarketScanner] Error obteniendo contexto histórico para {symbol}: {e}")

        return context

    async def scan_all(self) -> List[Signal]:
        signals: List[Signal] = []
        now = time.time()

        ticker_map = {}
        try:
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

            daily_pct = ticker_map.get(symbol, 0.0)

            # 🟢 PASO 1: OBTENER CONTEXTO HISTÓRICO (y guardarlo en caché si no lo tenemos)
            historical_context = await self._get_historical_context(symbol)

            # 1. VERIFICAR SI ESTÁ CERCA DE LAS MEDIAS EN M15 (Filtro de vigilancia)
            try:
                df_15m = await self.api.fetch_ohlcv(symbol, timeframe="15m", limit=50)
                if df_15m is None or len(df_15m) < 40:
                    continue
                
                df_15m["ema55"] = df_15m["close"].ewm(span=55, adjust=False).mean()
                df_15m["ema144"] = df_15m["close"].ewm(span=144, adjust=False).mean()
                df_15m["ema233"] = df_15m["close"].ewm(span=233, adjust=False).mean()
                
                price_15m = df_15m["close"].iloc[-1]
                ema55_15m = df_15m["ema55"].iloc[-1]
                ema144_15m = df_15m["ema144"].iloc[-1]
                ema233_15m = df_15m["ema233"].iloc[-1]
                
                distance_pct = abs(price_15m - ema55_15m) / price_15m
                
                # Si la distancia es mayor al 5%, el cruce está lejos. Pasamos al M3 normal.
                if distance_pct > 0.05:
                    continue

                macro_angle = "FLAT"
                if price_15m > ema55_15m and price_15m > ema144_15m and price_15m > ema233_15m:
                    macro_angle = "BULLISH"
                elif price_15m < ema55_15m and price_15m < ema144_15m and price_15m < ema233_15m:
                    macro_angle = "BEARISH"

            except Exception as exc:
                logger.debug(f"[MarketScanner] Error en M15 para {symbol}: {exc}")
                continue

            # 2. ANÁLISIS DE PRECISIÓN EN M3
            try:
                df_3m = await self.api.fetch_ohlcv(symbol, timeframe="3m", limit=80)
                if df_3m is None or len(df_3m) < 40:
                    continue

                behavior = self._describe_behavior_m3(df_3m, daily_pct, macro_angle)
                symbol_code = self._normalize_symbol(symbol)
                
                matches = self._match_patterns(symbol_code, behavior, "3m", macro_angle)

                if not matches:
                    continue

                best = max(matches, key=lambda item: (item["match_ratio"], item["pattern"].get("rb_real", 0.0)))
                signal_type_str = best["pattern"]["signal_type"]
                
                try:
                    signal_type = SignalType(signal_type_str)
                except ValueError:
                    logger.warning(f"[MarketScanner] Tipo inválido '{signal_type_str}' para {symbol}")
                    continue

                # 🟢 PASO 3: FILTRO DE CONTEXTO HISTÓRICO
                # Si el precio está en una zona peligrosa, ajustamos el puntaje.
                position = historical_context.get("position", "MID_RANGE")
                score = best["match_ratio"]

                if position in ["ATH_ZONE", "MONTH_HIGH"] and signal_type.is_long():
                    logger.debug(f"[MarketScanner] ⚠️ {symbol} en zona de máximo ({position}). Bajando score de LONG.")
                    score = score * 0.3  # Reducimos drásticamente el puntaje para que no sea elegida
                elif position in ["ATL_ZONE", "MONTH_LOW"] and not signal_type.is_long():
                    logger.debug(f"[MarketScanner] ⚠️ {symbol} en zona de mínimo ({position}). Bajando score de SHORT.")
                    score = score * 0.3

                if score < self.min_score:
                    continue

                signal = self._build_signal(symbol, df_3m, behavior, best, "3m", score)

                if signal:
                    if self._can_signal(symbol):
                        self._signal_cooldown[symbol] = now
                        signals.append(signal)
                        logger.debug(f"[MarketScanner] Señal {signal.signal_type.value} {symbol} en M3 | Macro: {macro_angle} | Context: {position}")

            except Exception as exc:
                logger.debug(f"[MarketScanner] Error en {symbol} M3: {exc}")

        if not signals:
            logger.info("[MarketScanner] Ninguna señal encontrada en este ciclo.")
        else:
            logger.info(f"[MarketScanner] {len(signals)} señales encontradas")

        return signals

    def _describe_behavior_m3(self, df: pd.DataFrame, daily_pct: float, macro_angle: str) -> Dict[str, Any]:
        df = df.copy()
        df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
        df["ema55"] = df["close"].ewm(span=55, adjust=False).mean()
        df["ema144"] = df["close"].ewm(span=144, adjust=False).mean()
        df["bb_mid"] = df["close"].rolling(20).mean()
        df["bb_std"] = df["close"].rolling(20).std()
        df["bb_upper"] = df["bb_mid"] + 2 * df["bb_std"]
        df["bb_lower"] = df["bb_mid"] - 2 * df["bb_std"]

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

        def bb_price_label(close: float, mid: float, lower: float, upper: float) -> str:
            if close >= upper:
                return "UPPER"
            if close <= lower:
                return "LOWER"
            if close >= mid:
                return "MID_TO_UPPER"
            return "LOWER"

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
            if adx_val > 25:
                return "STRONG"
            elif adx_val > 20:
                return "WEAK"
            else:
                return "RANGE"

        volume_average = float(df["volume"].rolling(20).mean().iloc[-2] or 0.0)
        price = float(current["close"])

        return {
            "precio_vs_ema21": position_label(price, float(current["ema21"])),
            "precio_vs_ema55": position_label(price, float(current["ema55"])),
            "precio_vs_ema144": position_label(price, float(current["ema144"])),
            "ema21_vs_ema55": ema_relation(float(current["ema21"]), float(current["ema55"]),
                                          float(prior["ema21"]), float(prior["ema55"])),
            "ema55_vs_ema144": ema_relation(float(current["ema55"]), float(current["ema144"]),
                                           float(prior["ema55"]), float(prior["ema144"])),
            "bb_precio": bb_price_label(price, float(current["bb_mid"]),
                                       float(current["bb_lower"]), float(current["bb_upper"])),
            "volumen": volume_label(float(current["volume"]), volume_average),
            "patron_vela": candle_pattern(current),
            "entry_price": price,
            "adx_tendencia": adx_tendencia_label(float(current["adx"])),
            "daily_pct_change": daily_pct,
            "macro_angle": macro_angle,
        }

    def _evaluate_golden_rules(self, behavior: Dict[str, Any], macro_angle: str, timeframe: str) -> float:
        score = 0.0
        bb_price = behavior.get("bb_precio", "MID")
        volumen = behavior.get("volumen", "LOW")
        adx_force = behavior.get("adx_tendencia", "RANGE")
        daily_pct = behavior.get("daily_pct_change", 0.0)
        signal_type = behavior.get("signal_type", "UNKNOWN")
        
        precio_vs_ema55 = behavior.get("precio_vs_ema55", "")
        precio_vs_ema144 = behavior.get("precio_vs_ema144", "")
        ema55_vs_ema144 = behavior.get("ema55_vs_ema144", "")

        if macro_angle == "BULLISH" and "SHORT" in str(signal_type):
            return 0.0
        if macro_angle == "BEARISH" and "LONG" in str(signal_type):
            return 0.0

        if adx_force in ["WEAK", "RANGE"]:
            return 0.0

        if daily_pct > 0.05 and "LONG" in str(signal_type):
            return 0.0
        if daily_pct < -0.05 and "SHORT" in str(signal_type):
            return 0.0

        if "LONG" in str(signal_type):
            if ema55_vs_ema144 in ["CROSSING_UP", "ABOVE"]:
                if bb_price == "LOWER" or precio_vs_ema55 in ["TOUCHING", "NEAR"]:
                    if volumen in ["HIGH", "MEDIUM"]:
                        score += 50.0

        elif "SHORT" in str(signal_type):
            if ema55_vs_ema144 in ["CROSSING_DOWN", "BELOW"]:
                if bb_price == "UPPER" or precio_vs_ema55 in ["TOUCHING", "NEAR"]:
                    if volumen in ["HIGH", "MEDIUM"]:
                        score += 50.0

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

            match_fields = [
                "precio_vs_ema21", "precio_vs_ema55", "precio_vs_ema144",
                "ema21_vs_ema55", "ema55_vs_ema144", "bb_precio", "volumen", "patron_vela", "adx_tendencia"
            ]

            for key in match_fields:
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
        score: float
    ) -> Optional[Signal]:
        pattern 
