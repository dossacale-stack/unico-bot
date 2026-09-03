# strategy_scanner.py - Strategy Scanner WASHI RADAR UNIFICADO
# ============================================================
# REEMPLAZA COMPLETAMENTE al antiguo strategy_scanner.py
# Integra TODA la funcionalidad de WASHI RADAR
# 
# CARACTERÍSTICAS:
#   - Escanea TODOS los activos de la watchlist
#   - Identifica oportunidades (LISTO, CALENTANDO, FRESCO, FRIO)
#   - Prioriza activos automáticamente
#   - Usa Bollinger Bands + EMAs + Subasta + Cruces
#   - Aprende de niveles históricos y trampas
#   - Genera señales de trading con confianza

import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from bybit_api_manager import BybitAPIManager
from learning_engine import LearningEngine, TradeRecord, ModoAprendizaje
import seed_patterns

logger = logging.getLogger("StrategyScanner")


# ═══════════════════════════════════════════════════════════════
#  ENUMS Y DATACLASSES
# ═══════════════════════════════════════════════════════════════

class SignalType(Enum):
    LONG_BREAKOUT = "LONG_BREAKOUT"
    SHORT_BREAKOUT = "SHORT_BREAKOUT"
    LONG_REVERSAL = "LONG_REVERSAL"
    SHORT_REVERSAL = "SHORT_REVERSAL"

    def is_long(self) -> bool:
        return self in {SignalType.LONG_BREAKOUT, SignalType.LONG_REVERSAL}
    
    def is_short(self) -> bool:
        return self in {SignalType.SHORT_BREAKOUT, SignalType.SHORT_REVERSAL}


class EstadoOportunidad(Enum):
    """Estado de oportunidad de un activo (WASHI RADAR)"""
    LISTO = "LISTO"                # Señal activa ahora
    CALENTANDO = "CALENTANDO"      # Se acerca a zona de entrada
    FRESCO = "FRESCO"              # Acaba de dar señal (cooldown)
    FRIO = "FRIO"                  # Lejos de oportunidad
    ESPERANDO = "ESPERANDO"        # En zona de espera


@dataclass
class OportunidadActivo:
    """Estado de oportunidad de un activo (WASHI RADAR)"""
    simbolo: str
    estado: EstadoOportunidad
    confianza: float
    tiempo_estimado: str
    precio_actual: float
    zona_entrada_min: float
    zona_entrada_max: float
    distancia_porcentaje: float
    tendencia: str
    volumen_relativo: float
    nivel_clave: str
    ultima_actualizacion: datetime
    prioridad: int = 0
    detalle: Dict[str, Any] = field(default_factory=dict)
    signal: Optional['Signal'] = None


@dataclass
class Signal:
    """Señal de trading"""
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
    oportunidad: Optional[OportunidadActivo] = None


# ═══════════════════════════════════════════════════════════════
#  STRATEGY SCANNER - VERSIÓN WASHI RADAR
# ═══════════════════════════════════════════════════════════════

class StrategyScanner:
    """
    Strategy Scanner WASHI RADAR UNIFICADO.
    Escanea, prioriza y genera señales con aprendizaje continuo.
    """
    
    def __init__(
        self,
        api_manager: BybitAPIManager,
        watchlist: List[str],
        scan_interval: float = 30.0,
        min_score: float = 0.35,
        min_rr: float = 1.5,
        position_pct: float = 0.02,
        db_path: str = "patterns.db",
        signal_cooldown_seconds: int = 60,
        timeframes: List[str] = None,
        modo_aprendizaje: ModoAprendizaje = ModoAprendizaje.ACTIVO,
    ):
        self.api = api_manager
        self.watchlist = watchlist
        self.scan_interval = scan_interval
        self.min_score = min_score
        self.min_rr = min_rr
        self.position_pct = position_pct
        self.db_path = db_path
        self.signal_cooldown_seconds = signal_cooldown_seconds
        self.timeframes = timeframes or ["15m", "3m", "1m"]
        
        # ═══════════════════════════════════════════════════════
        #  WASHI RADAR - ESTRUCTURAS
        # ═══════════════════════════════════════════════════════
        self.oportunidades: Dict[str, OportunidadActivo] = {}
        self.ultimo_escaneo = None
        self.escaneos_totales = 0
        
        # ═══════════════════════════════════════════════════════
        #  APRENDIZAJE WASHI
        # ═══════════════════════════════════════════════════════
        self.learning_engine = LearningEngine(db_path=db_path, modo=modo_aprendizaje)
        
        # ═══════════════════════════════════════════════════════
        #  CACHÉS Y ESTADOS
        # ═══════════════════════════════════════════════════════
        self.patterns_by_tf = {}
        self._signal_cooldown = {}
        self._historical_cache = {}
        self._historical_cache_time = {}
        self._bb_cache = {}
        
        # ═══════════════════════════════════════════════════════
        #  CONFIGURACIÓN BOLLINGER
        # ═══════════════════════════════════════════════════════
        self.bb_period = 20
        self.bb_std = 2
        
        # ═══════════════════════════════════════════════════════
        #  INICIALIZACIÓN
        # ═══════════════════════════════════════════════════════
        self._load_all_patterns()
        
        logger.info(f"📡 STRATEGY SCANNER WASHI RADAR UNIFICADO")
        logger.info(f"   Watchlist: {len(self.watchlist)} activos")
        logger.info(f"   Scan interval: {self.scan_interval}s")
        logger.info(f"   Mínimo score: {self.min_score}")
        logger.info(f"   Modo aprendizaje: {modo_aprendizaje.value}")
        logger.info(f"   Timeframes: {self.timeframes}")

    # ═══════════════════════════════════════════════════════════
    # 1. INICIALIZACIÓN Y APRENDIZAJE
    # ═══════════════════════════════════════════════════════════

    def _load_all_patterns(self) -> None:
        """Carga patrones desde DB o seed_patterns"""
        if not os.path.exists(self.db_path):
            logger.warning("[StrategyScanner] No se encontró DB. Usando seed_patterns.")
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
                    logger.info(f"[StrategyScanner] Cargados {len(self.patterns_by_tf[tf])} patrones para {tf}")
        except Exception as exc:
            logger.warning(f"[StrategyScanner] Error cargando patrones: {exc}. Usando seed_patterns.")
            for tf in self.timeframes:
                self.patterns_by_tf[tf] = [p.copy() for p in seed_patterns.PATTERNS if p.get("timeframe") == tf]

    async def _aprender_watchlist(self):
        """Aprende de todos los símbolos en la watchlist"""
        logger.info("🧠 WASHI aprendiendo de la watchlist...")
        
        for symbol in self.watchlist:
            try:
                # Obtener datos históricos completos
                df = await self.api.fetch_ohlcv(symbol, timeframe="1h", limit=1500)
                if df is not None and len(df) > 100:
                    await self.learning_engine.aprender_simbolo(symbol, df, self.api)
            except Exception as e:
                logger.debug(f"[StrategyScanner] Error aprendiendo {symbol}: {e}")
        
        logger.info("✅ Aprendizaje de watchlist completado")

    # ═══════════════════════════════════════════════════════════
    # 2. MÉTODOS AUXILIARES
    # ═══════════════════════════════════════════════════════════

    def _normalize_symbol(self, symbol: str) -> str:
        return re.sub(r"[^\w]", "", symbol).upper()

    def _can_signal(self, symbol: str) -> bool:
        now = time.time()
        if symbol in self._signal_cooldown:
            elapsed = now - self._signal_cooldown[symbol]
            if elapsed < self.signal_cooldown_seconds:
                return False
        return True

    # ═══════════════════════════════════════════════════════════
    # 3. BOLLINGER BANDS
    # ═══════════════════════════════════════════════════════════

    def _calculate_bollinger_bands(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Calcula Bandas de Bollinger"""
        mid = df['close'].rolling(window=self.bb_period).mean()
        std = df['close'].rolling(window=self.bb_period).std()
        upper = mid + (self.bb_std * std)
        lower = mid - (self.bb_std * std)
        bandwidth = (upper - lower) / mid
        
        return {
            'upper': upper,
            'middle': mid,
            'lower': lower,
            'bandwidth': bandwidth,
            'std': std
        }
    
    def _analyze_bb_position(self, df: pd.DataFrame, price: float) -> Dict[str, Any]:
        """Analiza posición del precio vs Bollinger Bands"""
        bb = self._calculate_bollinger_bands(df)
        
        upper = bb['upper'].iloc[-1]
        middle = bb['middle'].iloc[-1]
        lower = bb['lower'].iloc[-1]
        bandwidth = bb['bandwidth'].iloc[-1]
        
        avg_bandwidth = bb['bandwidth'].tail(20).mean()
        is_squeeze = bandwidth < avg_bandwidth * 0.7
        is_expanding = bandwidth > avg_bandwidth * 1.3
        
        if price > upper:
            position = "ABOVE_UPPER"
        elif price >= upper * 0.995:
            position = "AT_UPPER"
        elif price > middle and price < upper:
            position = "ABOVE_MIDDLE"
        elif abs(price - middle) / price < 0.005:
            position = "AT_MIDDLE"
        elif price < middle and price > lower:
            position = "BELOW_MIDDLE"
        elif price <= lower * 1.005:
            position = "AT_LOWER"
        elif price < lower:
            position = "BELOW_LOWER"
        else:
            position = "AT_MIDDLE"
        
        if is_squeeze and position not in ["AT_UPPER", "AT_LOWER"]:
            position = "SQUEEZE"
        elif is_expanding:
            position = "EXPANDING"
        
        return {
            'position': position,
            'upper': upper,
            'middle': middle,
            'lower': lower,
            'bandwidth': bandwidth,
            'is_squeeze': is_squeeze,
            'is_expanding': is_expanding,
            'pct_to_upper': (upper - price) / price * 100,
            'pct_to_lower': (price - lower) / price * 100,
        }

    # ═══════════════════════════════════════════════════════════
    # 4. CONTEXTO HISTÓRICO COMPLETO
    # ═══════════════════════════════════════════════════════════

    async def _get_historical_context(self, symbol: str) -> Dict[str, Any]:
        """Obtiene contexto histórico completo"""
        now = time.time()
        if symbol in self._historical_cache and (now - self._historical_cache_time.get(symbol, 0)) < 3600:
            return self._historical_cache[symbol]

        context = {
            "day_high": 0.0, "day_low": 0.0,
            "week_high": 0.0, "week_low": 0.0,
            "month_high": 0.0, "month_low": 0.0,
            "ath": 0.0, "atl": 0.0,
            "position": "MID_RANGE",
            "current_price": 0.0
        }

        try:
            ticker = await self.api._safe_call(
                lambda: self.api.exchange.fetch_ticker(symbol),
                endpoint_type="public"
            )
            context["current_price"] = ticker.get("last", 0)
            context["day_high"] = ticker.get("high", 0)
            context["day_low"] = ticker.get("low", 0)

            df_hist = await self.api.fetch_ohlcv(symbol, timeframe="1h", limit=720)
            if df_hist is not None and not df_hist.empty:
                context["ath"] = df_hist["high"].max()
                context["atl"] = df_hist["low"].min()
                context["month_high"] = df_hist["high"].max()
                context["month_low"] = df_hist["low"].min()

            price = context["current_price"]
            ath = context["ath"]
            atl = context["atl"]
            
            if ath > 0 and price >= (ath * 0.95):
                context["position"] = "ATH_ZONE"
            elif atl > 0 and price <= (atl * 1.05):
                context["position"] = "ATL_ZONE"
            elif context["month_high"] > 0 and price >= (context["month_high"] * 0.95):
                context["position"] = "MONTH_HIGH"
            elif context["month_low"] > 0 and price <= (context["month_low"] * 1.05):
                context["position"] = "MONTH_LOW"

            self._historical_cache[symbol] = context
            self._historical_cache_time[symbol] = now

        except Exception as e:
            logger.debug(f"[StrategyScanner] Error en contexto histórico para {symbol}: {e}")

        return context

    # ═══════════════════════════════════════════════════════════
    # 5. ANÁLISIS DE SUBASTA
    # ═══════════════════════════════════════════════════════════

    def _analyze_auction(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Analiza la subasta: compradores vs vendedores"""
        close = df['close']
        open_price = df['open']
        
        buy_volume = df[close > open_price]['volume'].sum()
        sell_volume = df[close < open_price]['volume'].sum()
        
        total_volume = buy_volume + sell_volume
        if total_volume == 0:
            return {'type': 'BALANCED', 'buy_ratio': 0.5, 'sell_ratio': 0.5}
        
        buy_ratio = buy_volume / total_volume
        sell_ratio = sell_volume / total_volume
        
        vwap = (df['close'] * df['volume']).sum() / df['volume'].sum() if df['volume'].sum() > 0 else close.iloc[-1]
        price = close.iloc[-1]
        price_vs_vwap = (price - vwap) / vwap if vwap > 0 else 0
        
        delta = (df['volume'] * np.where(df['close'] > df['open'], 1, -1)).sum()
        
        if buy_ratio > 0.52 and price_vs_vwap > 0 and delta > 0:
            auction_type = "BUYERS_IN_CONTROL"
        elif sell_ratio > 0.52 and price_vs_vwap < 0 and delta < 0:
            auction_type = "SELLERS_IN_CONTROL"
        else:
            auction_type = "BALANCED"
        
        return {
            'type': auction_type,
            'buy_ratio': buy_ratio,
            'sell_ratio': sell_ratio,
            'vwap': vwap,
            'price_vs_vwap': price_vs_vwap,
            'delta': delta
        }

    # ═══════════════════════════════════════════════════════════
    # 6. DETECCIÓN DE CRUCES
    # ═══════════════════════════════════════════════════════════

    def _detect_crossovers(self, df: pd.DataFrame) -> Dict[str, str]:
        """Detecta cruces entre EMAs"""
        if len(df) < 3:
            return {}
        
        ema21 = df['close'].ewm(span=21, adjust=False).mean()
        ema55 = df['close'].ewm(span=55, adjust=False).mean()
        ema144 = df['close'].ewm(span=144, adjust=False).mean()
        ema233 = df['close'].ewm(span=233, adjust=False).mean()
        
        crossovers = {}
        
        if len(ema21) > 2:
            prev_21 = ema21.iloc[-2]
            curr_21 = ema21.iloc[-1]
            prev_55 = ema55.iloc[-2]
            curr_55 = ema55.iloc[-1]
            
            if prev_21 <= prev_55 and curr_21 > curr_55:
                crossovers['ema21_55'] = 'CROSSING_UP'
            elif prev_21 >= prev_55 and curr_21 < curr_55:
                crossovers['ema21_55'] = 'CROSSING_DOWN'
            else:
                crossovers['ema21_55'] = 'NO_CROSS'
        
        if len(ema55) > 2:
            prev_55 = ema55.iloc[-2]
            curr_55 = ema55.iloc[-1]
            prev_144 = ema144.iloc[-2]
            curr_144 = ema144.iloc[-1]
            
            if prev_55 <= prev_144 and curr_55 > curr_144:
                crossovers['ema55_144'] = 'CROSSING_UP'
            elif prev_55 >= prev_144 and curr_55 < curr_144:
                crossovers['ema55_144'] = 'CROSSING_DOWN'
            else:
                crossovers['ema55_144'] = 'NO_CROSS'
        
        return crossovers

    # ═══════════════════════════════════════════════════════════
    # 7. DESCRIPCIÓN DE COMPORTAMIENTO M3
    # ═══════════════════════════════════════════════════════════

    def _describe_behavior_m3(self, df: pd.DataFrame, daily_pct: float, macro_angle: str) -> Dict[str, Any]:
        """Describe el comportamiento en M3"""
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

        def position_label(price: float, target: float) -> str:
            if target == 0 or price == 0:
                return "N/A"
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
            return "MID_TO_LOWER"

        def volume_label(volume: float, average: float) -> str:
            if average == 0:
                return "LOW"
            ratio = volume / average
            if ratio >= 2.0:
                return "HIGH"
            if ratio >= 1.2:
                return "MEDIUM"
            if ratio < 0.35:
                return "VERY_LOW"
            return "LOW"

        def candle_pattern(row: pd.Series) -> str:
            body = abs(row["close"] - row["open"])
            if body == 0:
                return "NEUTRAL"
            upper_wick = float(row["high"] - max(row["close"], row["open"]))
            lower_wick = float(min(row["close"], row["open"]) - row["low"])
            if row["close"] > row["open"] and body > upper_wick * 2:
                return "STRONG_GREEN"
            if row["close"] < row["open"] and body > lower_wick * 2:
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

        price = float(current["close"])
        volume_average = float(df["volume"].rolling(20).mean().iloc[-2] or 0.0)

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
            "atr": float(current["atr"]) if not pd.isna(current["atr"]) else price * 0.01,
        }

    # ═══════════════════════════════════════════════════════════
    # 8. MATCH CON PATRONES (MODIFICADO)
    # ═══════════════════════════════════════════════════════════

    def _match_patterns(
        self, 
        symbol_code: str, 
        behavior: Dict[str, Any], 
        timeframe: str, 
        macro_angle: str,
        bb_analysis: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Compara comportamiento con patrones incluyendo Bollinger"""
        matches = []
        patterns = self.patterns_by_tf.get(timeframe, [])
        
        golden_score = self._evaluate_golden_rules(behavior, macro_angle, timeframe, bb_analysis)
        
        # 🔥 MODIFICADO: Reducido el umbral de 8.0 a 5.0
        if golden_score < 5.0:
            logger.debug(f"[DEBUG] {symbol_code} {timeframe}: Golden Score ({golden_score}) demasiado bajo.")
            return matches

        for pattern in patterns:
            if pattern.get("symbol") not in {symbol_code, "UNIVERSAL"}:
                continue

            if pattern.get("timeframe") != timeframe:
                continue

            score = 0
            total = 0

            base_fields = [
                "precio_vs_ema21", "precio_vs_ema55", "precio_vs_ema144",
                "ema21_vs_ema55", "ema55_vs_ema144", "bb_precio", 
                "volumen", "patron_vela", "adx_tendencia"
            ]

            for key in base_fields:
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
            final_score = match_ratio + (golden_score / 100.0) * 0.4
            final_score = min(final_score, 1.0)

            if final_score < 0.30:
                continue

            # 🔥 MODIFICADO: Añadido print de depuración
            logger.debug(f"[DEBUG] Patrón {pattern.get('id')} en {symbol_code}: Score {score}/{total}, Final: {final_score:.2f}")

            matches.append({
                "pattern": pattern,
                "match_ratio": final_score,
                "score": score,
                "total": total,
            })

        return matches

    def _evaluate_golden_rules(
        self, 
        behavior: Dict[str, Any], 
        macro_angle: str, 
        timeframe: str,
        bb_analysis: Dict[str, Any]
    ) -> float:
        """Evalúa reglas de oro con Bollinger (MODIFICADO)"""
        score = 0.0
        bb_pos = bb_analysis.get('position', 'MID')
        volumen = behavior.get("volumen", "LOW")
        adx_force = behavior.get("adx_tendencia", "RANGE")
        daily_pct = behavior.get("daily_pct_change", 0.0)
        signal_type = behavior.get("signal_type", "UNKNOWN")
        is_squeeze = bb_analysis.get('is_squeeze', False)
        
        if macro_angle == "BULLISH" and "SHORT" in str(signal_type):
            return 0.0
        if macro_angle == "BEARISH" and "LONG" in str(signal_type):
            return 0.0

        # 🔥 MODIFICADO: Eliminado el filtro estricto de RANGE
        # if adx_force in ["RANGE"]:
        #     return 0.0

        if daily_pct > 0.05 and "LONG" in str(signal_type):
            return 0.0
        if daily_pct < -0.05 and "SHORT" in str(signal_type):
            return 0.0

        if "LONG" in str(signal_type):
            if bb_pos in ["AT_LOWER", "BELOW_LOWER", "BELOW_MIDDLE"]:
                score += 25
            elif bb_pos in ["SQUEEZE"]:
                score += 20
            
            ema55_144 = behavior.get("ema55_vs_ema144", "")
            if ema55_144 in ["CROSSING_UP", "ABOVE"]:
                score += 20
            
            if volumen in ["HIGH", "MEDIUM"]:
                score += 15

        elif "SHORT" in str(signal_type):
            if bb_pos in ["AT_UPPER", "ABOVE_UPPER", "ABOVE_MIDDLE"]:
                score += 25
            elif bb_pos in ["SQUEEZE"]:
                score += 20
            
            ema55_144 = behavior.get("ema55_vs_ema144", "")
            if ema55_144 in ["CROSSING_DOWN", "BELOW"]:
                score += 20
            
            if volumen in ["HIGH", "MEDIUM"]:
                score += 15

        # 🔥 MODIFICADO: Puntaje base bajo si no hay tendencia para no descartar todo
        if adx_force == "RANGE":
            score += 10

        return max(0.0, score)

    # ═══════════════════════════════════════════════════════════
    # 9. CONSTRUCCIÓN DE SEÑAL
    # ═══════════════════════════════════════════════════════════

    def _build_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        behavior: Dict[str, Any],
        matched: Dict[str, Any],
        timeframe: str,
        score: float,
        bb_analysis: Dict[str, Any],
        auction: Dict[str, Any],
        historical_context: Dict[str, Any],
        oportunidad: Optional[OportunidadActivo] = None
    ) -> Optional[Signal]:
        """Construye señal de trading"""
        try:
            pattern = matched["pattern"]
            signal_type_str = pattern.get("signal_type", "")
            
            try:
                signal_type = SignalType(signal_type_str)
            except ValueError:
                logger.warning(f"[StrategyScanner] Tipo inválido: {signal_type_str}")
                return None
            
            current_price = df["close"].iloc[-1]
            atr = behavior.get("atr", current_price * 0.01)
            
            bb_upper = bb_analysis['upper']
            bb_lower = bb_analysis['lower']
            is_squeeze = bb_analysis['is_squeeze']
            
            if signal_type.is_long():
                if is_squeeze:
                    stop_loss = current_price - (atr * 1.2)
                else:
                    stop_loss = current_price - (atr * 1.5)
                take_profit = current_price + (atr * 3.0)
                entry_zone_min = max(bb_lower, current_price * 0.995)
                entry_zone_max = current_price * 1.005
            else:
                if is_squeeze:
                    stop_loss = current_price + (atr * 1.2)
                else:
                    stop_loss = current_price + (atr * 1.5)
                take_profit = current_price - (atr * 3.0)
                entry_zone_min = current_price * 0.995
                entry_zone_max = min(bb_upper, current_price * 1.005)
            
            risk = abs(current_price - stop_loss)
            reward = abs(take_profit - current_price)
            risk_reward = reward / risk if risk > 0 else 0
            
            bb_pos = bb_analysis['position']
            if bb_pos in ["AT_LOWER", "BELOW_LOWER"] and signal_type.is_long():
                risk_level = "BAJO"
            elif bb_pos in ["AT_UPPER", "ABOVE_UPPER"] and signal_type.is_short():
                risk_level = "BAJO"
            elif is_squeeze:
                risk_level = "MEDIO"
            else:
                risk_level = "MEDIO"
            
            return Signal(
                symbol=symbol,
                signal_type=signal_type,
                score=score,
                risk_reward=risk_reward,
                stop_loss=stop_loss,
                take_profit=take_profit,
                entry_price=current_price,
                df=df.copy(),
                pattern_id=pattern.get("id"),
                timeframe=timeframe,
                confidence=score,
                risk_level=risk_level,
                entry_zone_min=entry_zone_min,
                entry_zone_max=entry_zone_max,
                entry_reason=pattern.get("notas", ""),
                auction_type=auction.get('type', 'BALANCED'),
                market_position=historical_context.get("position", "MID_RANGE"),
                bb_position=bb_analysis.get('position', 'MID'),
                bb_squeeze=is_squeeze,
                oportunidad=oportunidad
            )
            
        except Exception as e:
            logger.error(f"[StrategyScanner] Error construyendo señal para {symbol}: {e}")
            return None

    # ═══════════════════════════════════════════════════════════
    # 10. ESCANEO PRINCIPAL (WASHI RADAR + SEÑALES) (MODIFICADO)
    # ═══════════════════════════════════════════════════════════

    async def scan_all(self) -> List[Signal]:
        """
        ESCANEO COMPLETO - WASHI RADAR + SEÑALES DE TRADING
        Esta es la función principal que usa el bot.
        """
        self.escaneos_totales += 1
        signals: List[Signal] = []
        now = time.time()
        self.ultimo_escaneo = datetime.now()

        logger.info(f"📡 ESCANEO #{self.escaneos_totales} - {self.ultimo_escaneo.strftime('%H:%M:%S')}")

        # Obtener tickers 24h
        ticker_map = {}
        try:
            session = self.api.exchange
            response = session.get_tickers(category="linear")
            tickers = response["result"]["list"]
            for t in tickers:
                ticker_map[t["symbol"]] = float(t.get("price24hPcnt", 0.0))
        except Exception as e:
            logger.debug(f"[StrategyScanner] No se pudo obtener tickers: {e}")

        # ========================================================
        #  A. ESCANEAR CADA SÍMBOLO
        # ========================================================
        
        for symbol in self.watchlist:
            try:
                # Cooldown
                if symbol in self._signal_cooldown:
                    if now - self._signal_cooldown[symbol] < 300:
                        continue

                daily_pct = ticker_map.get(symbol, 0.0)
                historical_context = await self._get_historical_context(symbol)

                # ──────────────────────────────────────────────
                #  A1. FILTRO M15 (MACRO)
                # ──────────────────────────────────────────────
                df_15m = await self.api.fetch_ohlcv(symbol, timeframe="15m", limit=80)
                if df_15m is None or len(df_15m) < 60:
                    continue
                
                df_15m["ema55"] = df_15m["close"].ewm(span=55, adjust=False).mean()
                df_15m["ema144"] = df_15m["close"].ewm(span=144, adjust=False).mean()
                df_15m["ema233"] = df_15m["close"].ewm(span=233, adjust=False).mean()
                
                price_15m = df_15m["close"].iloc[-1]
                ema55_15m = df_15m["ema55"].iloc[-1]
                
                distance_pct = abs(price_15m - ema55_15m) / price_15m if price_15m > 0 else 0.1
                
                # 🔥 MODIFICADO: Relajado de 0.12 a 0.30 para no perder activos en movimiento
                if distance_pct > 0.30:
                    logger.debug(f"[StrategyScanner] {symbol}: M15 lejos de EMA55 ({distance_pct:.2%})")
                    continue

                macro_angle = "FLAT"
                if price_15m > ema55_15m and price_15m > df_15m["ema144"].iloc[-1] and price_15m > df_15m["ema233"].iloc[-1]:
                    macro_angle = "BULLISH"
                elif price_15m < ema55_15m and price_15m < df_15m["ema144"].iloc[-1] and price_15m < df_15m["ema233"].iloc[-1]:
                    macro_angle = "BEARISH"

                # ──────────────────────────────────────────────
                #  A2. ANÁLISIS M3 (MICRO) - CON BOLLINGER
                # ──────────────────────────────────────────────
                df_3m = await self.api.fetch_ohlcv(symbol, timeframe="3m", limit=120)
                if df_3m is None or len(df_3m) < 60:
                    continue

                price = df_3m["close"].iloc[-1]
                
                # Bollinger Bands
                bb_analysis = self._analyze_bb_position(df_3m, price)
                
                # Subasta
                auction = self._analyze_auction(df_3m)
                
                # Cruces
                crossovers = self._detect_crossovers(df_3m)
                
                # Comportamiento M3
                behavior = self._describe_behavior_m3(df_3m, daily_pct, macro_angle)
                behavior['bb_position'] = bb_analysis['position']
                behavior['bb_is_squeeze'] = bb_analysis['is_squeeze']
                behavior['bb_is_expanding'] = bb_analysis['is_expanding']
                behavior['auction_type'] = auction['type']
                behavior['crossovers'] = crossovers
                
                symbol_code = self._normalize_symbol(symbol)
                
                # ──────────────────────────────────────────────
                #  A3. MATCH CON PATRONES
                # ──────────────────────────────────────────────
                matches = self._match_patterns(
                    symbol_code, behavior, "3m", macro_angle, bb_analysis
                )

                if not matches:
                    logger.debug(f"[StrategyScanner] {symbol}: No hay matches")
                    continue

                best = max(matches, key=lambda item: item["match_ratio"])
                signal_type_str = best["pattern"]["signal_type"]
                
                try:
                    signal_type = SignalType(signal_type_str)
                except ValueError:
                    logger.warning(f"[StrategyScanner] Tipo inválido '{signal_type_str}'")
                    continue

                # ──────────────────────────────────────────────
                #  A4. FILTRO DE CONTEXTO HISTÓRICO
                # ──────────────────────────────────────────────
                position = historical_context.get("position", "MID_RANGE")
                score = best["match_ratio"]

                if position in ["ATH_ZONE", "MONTH_HIGH"] and signal_type.is_long():
                    score = score * 0.3
                elif position in ["ATL_ZONE", "MONTH_LOW"] and signal_type.is_short():
                    score = score * 0.3

                bb_pos = bb_analysis['position']
                if signal_type.is_long() and bb_pos in ["ABOVE_UPPER", "AT_UPPER"]:
                    score = score * 0.7
                elif signal_type.is_short() and bb_pos in ["BELOW_LOWER", "AT_LOWER"]:
                    score = score * 0.7

                # ====================================================
                #  B. WASHI RADAR - DETERMINAR ESTADO DE OPORTUNIDAD
                # ====================================================
                
                # 1. ¿Hay señal activa?
                tiene_senal = score >= self.min_score
                
                # 2. ¿Está calentando?
                esta_calentando = self._esta_calentando(symbol, price, bb_analysis, historical_context)
                
                # 3. ¿Distancia a zona de entrada?
                distancia = 0.0
                if signal_type.is_long():
                    distancia = (price - bb_analysis['lower']) / price * 100 if bb_analysis['lower'] > 0 else 100
                else:
                    distancia = (bb_analysis['upper'] - price) / price * 100 if price > 0 else 100
                
                # 4. Determinar estado
                if tiene_senal and score >= self.min_score:
                    estado = EstadoOportunidad.LISTO
                    confianza = score
                    tiempo = "AHORA"
                elif esta_calentando:
                    estado = EstadoOportunidad.CALENTANDO
                    confianza = 0.4 + (score * 0.3)
                    if distancia < 1:
                        tiempo = "30 min - 1 hora"
                    elif distancia < 2:
                        tiempo = "1 - 3 horas"
                    else:
                        tiempo = "3 - 6 horas"
                elif symbol in self._signal_cooldown:
                    estado = EstadoOportunidad.FRESCO
                    confianza = 0.3
                    tiempo = "En cooldown"
                else:
                    estado = EstadoOportunidad.FRIO
                    confianza = 0.1
                    tiempo = "> 6 horas"
                
                # 5. Crear oportunidad
                zona_min = bb_analysis['lower'] if signal_type.is_long() else bb_analysis['upper'] * 0.99
                zona_max = bb_analysis['upper'] if signal_type.is_long() else bb_analysis['upper'] * 1.01
                
                oportunidad = OportunidadActivo(
                    simbolo=symbol,
                    estado=estado,
                    confianza=confianza,
                    tiempo_estimado=tiempo,
                    precio_actual=price,
                    zona_entrada_min=zona_min,
                    zona_entrada_max=zona_max,
                    distancia_porcentaje=distancia,
                    tendencia=macro_angle,
                    volumen_relativo=1.0,
                    nivel_clave="SOPORTE" if signal_type.is_long() else "RESISTENCIA",
                    ultima_actualizacion=self.ultimo_escaneo,
                    prioridad=self._calcular_prioridad(estado, confianza),
                    detalle={
                        'bb_pos': bb_pos,
                        'auction': auction['type'],
                        'macro_angle': macro_angle,
                        'score': score
                    }
                )
                
                # Guardar oportunidad
                self.oportunidades[symbol] = oportunidad

                # ──────────────────────────────────────────────
                #  A5. CONSTRUIR SEÑAL (SOLO SI ESTÁ LISTO)
                # ──────────────────────────────────────────────
                if score < self.min_score:
                    logger.debug(f"[StrategyScanner] {symbol}: Score {score:.2f} < {self.min_score}")
                    continue

                signal = self._build_signal(
                    symbol, df_3m, behavior, best, "3m", score,
                    bb_analysis, auction, historical_context, oportunidad
                )

                if signal:
                    self._signal_cooldown[symbol] = now
                    signals.append(signal)
                    logger.info(f"✅ {signal.signal_type.value} {symbol} | "
                              f"Score: {score:.2f} | BB: {bb_pos} | "
                              f"Estado: {estado.value} | Macro: {macro_angle}")

            except Exception as e:
                logger.debug(f"[StrategyScanner] Error en {symbol}: {e}")
                continue

        # ========================================================
        #  C. PRIORIZAR OPORTUNIDADES
        # ========================================================
        
        self._priorizar_oportunidades()
        
        # Log de radar
        listos = self.obtener_activos_listos()
        calentando = [o for o in self.oportunidades.values() if o.estado == EstadoOportunidad.CALENTANDO]
        
        if listos:
            logger.info(f"🎯 {len(listos)} ACTIVOS LISTOS:")
            for o in listos:
                logger.info(f"   ✅ {o.simbolo} | Conf: {o.confianza:.1%} | {o.nivel_clave}")
        
        if calentando:
            logger.info(f"🔥 {len(calentando)} ACTIVOS CALENTANDO:")
            for o in calentando[:3]:
                logger.info(f"   🟡 {o.simbolo} | Dist: {o.distancia_porcentaje:.1f}% | {o.tiempo_estimado}")

        if not signals:
            logger.info("[StrategyScanner] Ninguna señal encontrada")
        else:
            logger.info(f"[StrategyScanner] {len(signals)} señales generadas")

        return signals

    def _esta_calentando(self, symbol: str, price: float, bb_analysis: Dict, historical_context: Dict) -> bool:
        """Determina si un activo está calentando"""
        bb_pos = bb_analysis.get('position', 'MID')
        
        # Cerca de BB Lower (para longs) o BB Upper (para shorts)
        if bb_pos in ["AT_LOWER", "BELOW_MIDDLE"]:
            return True
        if bb_pos in ["AT_UPPER", "ABOVE_MIDDLE"]:
            return True
        
        # Cerca de soportes/resistencias históricas
        for nivel in self.learning_engine._niveles_cerca(symbol, price):
            if nivel['distancia'] < 2 and nivel['fuerza'] > 50:
                return True
        
        return False

    def _calcular_prioridad(self, estado: EstadoOportunidad, confianza: float) -> int:
        """Calcula prioridad (0-10)"""
        prioridad = 0
        
        if estado == EstadoOportunidad.LISTO:
            prioridad += 10
        elif estado == EstadoOportunidad.CALENTANDO:
            prioridad += 5
        
        if confianza > 0.7:
            prioridad += 2
        elif confianza > 0.5:
            prioridad += 1
        
        return min(prioridad, 10)

    def _priorizar_oportunidades(self):
        """Prioriza todas las oportunidades"""
        for oport in self.oportunidades.values():
            oport.prioridad = self._calcular_prioridad(oport.estado, oport.confianza)

    # ═══════════════════════════════════════════════════════════
    # 11. MÉTODOS PARA RADAR
    # ═══════════════════════════════════════════════════════════

    def obtener_activos_listos(self) -> List[OportunidadActivo]:
        """Obtiene activos LISTOS para operar"""
        return [o for o in self.oportunidades.values() if o.estado == EstadoOportunidad.LISTO]

    def obtener_activos_calentando(self) -> List[OportunidadActivo]:
        """Obtiene activos CALENTANDO"""
        return [o for o in self.oportunidades.values() if o.estado == EstadoOportunidad.CALENTANDO]

    def obtener_top_prioridades(self, n: int = 5) -> List[OportunidadActivo]:
        """Obtiene los n activos con mayor prioridad"""
        ordenados = sorted(self.oportunidades.values(), key=lambda x: x.prioridad, reverse=True)
        return ordenados[:n]

    def generar_reporte_radar(self) -> str:
        """Genera reporte completo del radar"""
        reporte = []
        reporte.append("=" * 60)
        reporte.append(f"📡 RADAR WASHI - {self.ultimo_escaneo.strftime('%Y-%m-%d %H:%M:%S') if self.ultimo_escaneo else 'Sin datos'}")
        reporte.append("=" * 60)
        
        listos = self.obtener_activos_listos()
        if listos:
            reporte.append("\n🟢 ACTIVOS LISTOS:")
            for o in listos:
                reporte.append(f"   ✅ {o.simbolo} | Conf: {o.confianza:.1%} | {o.nivel_clave}")
                reporte.append(f"      Entry: ${o.zona_entrada_min:.4f} - ${o.zona_entrada_max:.4f}")
        
        calentando = self.obtener_activos_calentando()
        if calentando:
            reporte.append("\n🟡 ACTIVOS CALENTANDO:")
            for o in calentando[:5]:
                reporte.append(f"   🔥 {o.simbolo} | Dist: {o.distancia_porcentaje:.1f}% | {o.tiempo_estimado}")
        
        reporte.append("\n📊 TOP 5 PRIORIDADES:")
        for i, o in enumerate(self.obtener_top_prioridades(5), 1):
            emoji = "🟢" if o.estado == EstadoOportunidad.LISTO else "🟡" if o.estado == EstadoOportunidad.CALENTANDO else "⚪"
            reporte.append(f"   {i}. {emoji} {o.simbolo} | Prioridad: {o.prioridad} | {o.estado.value}")
        
        reporte.append("\n" + "=" * 60)
        return "\n".join(reporte)

    # ═══════════════════════════════════════════════════════════
    # 12. REGISTRAR TRADE (APRENDIZAJE)
    # ═══════════════════════════════════════════════════════════

    def register_trade(self, trade: TradeRecord) -> int:
        """Registra un trade en el motor de aprendizaje"""
        return self.learning_engine.register_trade(trade)

    def get_learning_report(self, symbol: Optional[str] = None) -> Dict:
        """Obtiene reporte de aprendizaje"""
        return self.learning_engine.get_report(symbol)

    # ═══════════════════════════════════════════════════════════
    # 13. ESTADO
    # ═══════════════════════════════════════════════════════════

    def get_status(self) -> Dict:
        """Obtiene estado completo del scanner"""
        return {
            'escaneos_totales': self.escaneos_totales,
            'ultimo_escaneo': self.ultimo_escaneo.isoformat() if self.ultimo_escaneo else None,
            'activos_escaneados': len(self.oportunidades),
            'activos_listos': len(self.obtener_activos_listos()),
            'activos_calentando': len(self.obtener_activos_calentando()),
            'cooldowns_activos': len(self._signal_cooldown),
            'aprendizaje': self.learning_engine.get_report(),
            'watchlist': self.watchlist
        }
