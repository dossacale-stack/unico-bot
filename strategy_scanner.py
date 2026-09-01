
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
    timeframe: str = "3m"
    setup: Optional[Any] = None  # Para WASHI setup
    confidence: float = 0.0
    risk_level: str = "MEDIO"
    entry_zone_min: float = 0.0
    entry_zone_max: float = 0.0
    entry_reason: str = ""
    auction_type: str = ""
    market_position: str = ""
    bb_position: str = ""
    bb_squeeze: bool = False


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
        self.timeframes = ["15m", "3m", "1m"]
        self.patterns_by_tf = {}
        self._signal_cooldown = {}
        self._load_all_patterns()

        # Cachés
        self._historical_cache = {} 
        self._historical_cache_time = {}
        self._bb_cache = {}
        
        # 📊 Bollinger Config
        self.bb_period = 20
        self.bb_std = 2

    def _normalize_symbol(self, symbol: str) -> str:
        return re.sub(r"[^\w]", "", symbol).upper()

    def _load_all_patterns(self) -> None:
        if not os.path.exists(self.db_path):
            logger.warning("[MarketScanner] No se encontró DB. Usando seed_patterns.")
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

    # ═══════════════════════════════════════════════════════════
    # 🟢 BOLLINGER BANDS - CÁLCULO Y ANÁLISIS
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
        
        # Detectar Squeeze (bandas comprimidas)
        avg_bandwidth = bb['bandwidth'].tail(20).mean()
        is_squeeze = bandwidth < avg_bandwidth * 0.7
        is_expanding = bandwidth > avg_bandwidth * 1.3
        
        # Posición del precio
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
            'price_vs_upper': price / upper if upper > 0 else 0,
            'price_vs_lower': price / lower if lower > 0 else 0,
        }

    # ═══════════════════════════════════════════════════════════
    # 🟢 CONTEXTO HISTÓRICO COMPLETO (TODAS LAS VELAS)
    # ═══════════════════════════════════════════════════════════

    async def _get_historical_context(self, symbol: str) -> Dict[str, Any]:
        """Obtiene contexto histórico con TODAS las velas disponibles"""
        now = time.time()
        if symbol in self._historical_cache and (now - self._historical_cache_time.get(symbol, 0)) < 3600:
            return self._historical_cache[symbol]

        context = {
            "day_high": 0.0, "day_low": 0.0,
            "week_high": 0.0, "week_low": 0.0,
            "month_high": 0.0, "month_low": 0.0,
            "ath": 0.0, "atl": 0.0,
            "position": "MID_RANGE",
            "all_time_high": 0.0,
            "all_time_low": 0.0,
            "volume_profile": {},
            "support_levels": [],
            "resistance_levels": []
        }

        try:
            # 1. Ticker actual
            ticker = await self.api._safe_call(
                lambda: self.api.exchange.fetch_ticker(symbol),
                endpoint_type="public"
            )
            context["current_price"] = ticker.get("last", 0)
            context["day_high"] = ticker.get("high", 0)
            context["day_low"] = ticker.get("low", 0)

            # 2. TODOS los datos históricos disponibles (máximo 1500 velas de 1h)
            df_hist = await self.api.fetch_ohlcv(symbol, timeframe="1h", limit=1500)
            if df_hist is not None and not df_hist.empty:
                context["ath"] = df_hist["high"].max()
                context["atl"] = df_hist["low"].min()
                context["all_time_high"] = df_hist["high"].max()
                context["all_time_low"] = df_hist["low"].min()
                
                # Niveles de soporte/resistencia históricos
                context["support_levels"] = self._find_support_levels(df_hist)
                context["resistance_levels"] = self._find_resistance_levels(df_hist)
                context["volume_profile"] = self._calculate_volume_profile(df_hist)

            # 3. Rango semanal (168 velas)
            df_week = await self.api.fetch_ohlcv(symbol, timeframe="1h", limit=168)
            if df_week is not None and not df_week.empty:
                context["week_high"] = df_week["high"].max()
                context["week_low"] = df_week["low"].min()

            # 4. Rango mensual (720 velas)
            df_month = await self.api.fetch_ohlcv(symbol, timeframe="1h", limit=720)
            if df_month is not None and not df_month.empty:
                context["month_high"] = df_month["high"].max()
                context["month_low"] = df_month["low"].min()

            # 5. Clasificar posición
            price = context.get("current_price", 0)
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

            # Guardar en caché
            self._historical_cache[symbol] = context
            self._historical_cache_time[symbol] = now

        except Exception as e:
            logger.debug(f"[MarketScanner] Error en contexto histórico para {symbol}: {e}")

        return context

    def _find_support_levels(self, df: pd.DataFrame, n: int = 5) -> List[float]:
        """Encuentra niveles de soporte históricos"""
        lows = df['low'].values
        levels = []
        window = 20
        
        for i in range(window, len(lows) - window):
            if lows[i] == min(lows[i-window:i+window]):
                levels.append(lows[i])
        
        # Ordenar y tomar los más fuertes
        levels.sort()
        return levels[:n] if levels else []

    def _find_resistance_levels(self, df: pd.DataFrame, n: int = 5) -> List[float]:
        """Encuentra niveles de resistencia históricos"""
        highs = df['high'].values
        levels = []
        window = 20
        
        for i in range(window, len(highs) - window):
            if highs[i] == max(highs[i-window:i+window]):
                levels.append(highs[i])
        
        levels.sort(reverse=True)
        return levels[:n] if levels else []

    def _calculate_volume_profile(self, df: pd.DataFrame) -> Dict[str, float]:
        """Calcula perfil de volumen"""
        if df.empty:
            return {}
        
        price_range = df['high'].max() - df['low'].min()
        if price_range == 0:
            return {}
        
        bins = 10
        bin_size = price_range / bins
        volume_profile = {}
        
        for i in range(bins):
            low = df['low'].min() + (i * bin_size)
            high = low + bin_size
            mask = (df['close'] >= low) & (df['close'] <= high)
            volume_profile[f"{low:.4f}-{high:.4f}"] = df.loc[mask, 'volume'].sum()
        
        return volume_profile

    # ═══════════════════════════════════════════════════════════
    # 🟢 ANÁLISIS DE SUBASTA (COMPRADORES VS VENDEDORES)
    # ═══════════════════════════════════════════════════════════

    def _analyze_auction(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Analiza la subasta: compradores vs vendedores"""
        close = df['close']
        open_price = df['open']
        volume = df['volume']
        
        buy_volume = df[close > open_price]['volume'].sum()
        sell_volume = df[close < open_price]['volume'].sum()
        
        total_volume = buy_volume + sell_volume
        if total_volume == 0:
            return {'type': 'BALANCED', 'buy_ratio': 0.5, 'sell_ratio': 0.5}
        
        buy_ratio = buy_volume / total_volume
        sell_ratio = sell_volume / total_volume
        
        # VWAP
        vwap = (df['close'] * df['volume']).sum() / df['volume'].sum() if df['volume'].sum() > 0 else close.iloc[-1]
        price = close.iloc[-1]
        price_vs_vwap = (price - vwap) / vwap if vwap > 0 else 0
        
        # Delta acumulado
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
    # 🟢 DETECCIÓN DE CRUCES DE EMAs
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
        
        # EMA21 vs EMA55
        if len(ema21) > 2:
            prev_21 = ema21.iloc[-2]
            curr_21 = ema21.iloc[-1]
            prev_55 = ema55.iloc[-2]
            curr_55 = ema55.iloc[-1]
            
            if prev_21 <= prev_55 and curr_21 > curr_55:
                crossovers['ema21_55'] = 'CROSSING_UP'
                crossovers['ema21_55_reason'] = 'EMA21 cruzó por encima de EMA55 (Golden Cross)'
            elif prev_21 >= prev_55 and curr_21 < curr_55:
                crossovers['ema21_55'] = 'CROSSING_DOWN'
                crossovers['ema21_55_reason'] = 'EMA21 cruzó por debajo de EMA55 (Death Cross)'
            else:
                crossovers['ema21_55'] = 'NO_CROSS'
        
        # EMA55 vs EMA144
        if len(ema55) > 2:
            prev_55 = ema55.iloc[-2]
            curr_55 = ema55.iloc[-1]
            prev_144 = ema144.iloc[-2]
            curr_144 = ema144.iloc[-1]
            
            if prev_55 <= prev_144 and curr_55 > curr_144:
                crossovers['ema55_144'] = 'CROSSING_UP'
                crossovers['ema55_144_reason'] = 'EMA55 cruzó por encima de EMA144'
            elif prev_55 >= prev_144 and curr_55 < curr_144:
                crossovers['ema55_144'] = 'CROSSING_DOWN'
                crossovers['ema55_144_reason'] = 'EMA55 cruzó por debajo de EMA144'
            else:
                crossovers['ema55_144'] = 'NO_CROSS'
        
        return crossovers

    # ═══════════════════════════════════════════════════════════
    # 🟢 SCAN PRINCIPAL
    # ═══════════════════════════════════════════════════════════

    async def scan_all(self) -> List[Signal]:
        """Escanea todos los símbolos con Bollinger + WASHI"""
        signals: List[Signal] = []
        now = time.time()

        # Obtener tickers 24h
        ticker_map = {}
        try:
            session = self.api.exchange
            response = session.get_tickers(category="linear")
            tickers = response["result"]["list"]
            for t in tickers:
                ticker_map[t["symbol"]] = float(t.get("price24hPcnt", 0.0))
        except Exception as e:
            logger.debug(f"[MarketScanner] No se pudo obtener tickers: {e}")

        for symbol in self.watchlist:
            try:
                # Cooldown
                if symbol in self._signal_cooldown:
                    if now - self._signal_cooldown[symbol] < 300:
                        continue

                daily_pct = ticker_map.get(symbol, 0.0)
                historical_context = await self._get_historical_context(symbol)

                # ──────────────────────────────────────────────
                # 1. FILTRO M15 (MACRO)
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
                
                # Umbral relajado para capturar más oportunidades
                if distance_pct > 0.12:
                    logger.debug(f"[MarketScanner] {symbol}: M15 lejos de EMA55 ({distance_pct:.2%})")
                    continue

                macro_angle = "FLAT"
                if price_15m > ema55_15m and price_15m > df_15m["ema144"].iloc[-1] and price_15m > df_15m["ema233"].iloc[-1]:
                    macro_angle = "BULLISH"
                elif price_15m < ema55_15m and price_15m < df_15m["ema144"].iloc[-1] and price_15m < df_15m["ema233"].iloc[-1]:
                    macro_angle = "BEARISH"

                # ──────────────────────────────────────────────
                # 2. ANÁLISIS M3 (MICRO) - CON BOLLINGER
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
                # 3. MATCH CON PATRONES (con Bollinger)
                # ──────────────────────────────────────────────
                matches = self._match_patterns_with_bb(
                    symbol_code, behavior, "3m", macro_angle, bb_analysis
                )

                if not matches:
                    logger.debug(f"[MarketScanner] {symbol}: No hay matches")
                    continue

                best = max(matches, key=lambda item: item["match_ratio"])
                signal_type_str = best["pattern"]["signal_type"]
                
                try:
                    signal_type = SignalType(signal_type_str)
                except ValueError:
                    logger.warning(f"[MarketScanner] Tipo inválido '{signal_type_str}'")
                    continue

                # ──────────────────────────────────────────────
                # 4. FILTRO DE CONTEXTO HISTÓRICO
                # ──────────────────────────────────────────────
                position = historical_context.get("position", "MID_RANGE")
                score = best["match_ratio"]

                # Penalización por zonas peligrosas
                if position in ["ATH_ZONE", "MONTH_HIGH"] and signal_type.is_long():
                    logger.debug(f"[MarketScanner] ⚠️ {symbol} en zona de máximo. Penalizando LONG.")
                    score = score * 0.3
                elif position in ["ATL_ZONE", "MONTH_LOW"] and signal_type.is_short():
                    logger.debug(f"[MarketScanner] ⚠️ {symbol} en zona de mínimo. Penalizando SHORT.")
                    score = score * 0.3

                # Penalización por Bollinger en zona contraria
                bb_pos = bb_analysis['position']
                if signal_type.is_long() and bb_pos in ["ABOVE_UPPER", "AT_UPPER"]:
                    score = score * 0.7
                elif signal_type.is_short() and bb_pos in ["BELOW_LOWER", "AT_LOWER"]:
                    score = score * 0.7

                if score < self.min_score:
                    logger.debug(f"[MarketScanner] {symbol}: Score {score:.2f} < {self.min_score}")
                    continue

                # ──────────────────────────────────────────────
                # 5. CONSTRUIR SEÑAL
                # ──────────────────────────────────────────────
                signal = self._build_signal_enhanced(
                    symbol, df_3m, behavior, best, "3m", score,
                    bb_analysis, auction, historical_context
                )

                if signal:
                    self._signal_cooldown[symbol] = now
                    signals.append(signal)
                    logger.info(f"✅ {signal.signal_type.value} {symbol} | "
                              f"Score: {score:.2f} | BB: {bb_pos} | "
                              f"Macro: {macro_angle} | Context: {position}")

            except Exception as e:
                logger.debug(f"[MarketScanner] Error en {symbol}: {e}")
                continue

        if not signals:
            logger.info("[MarketScanner] Ninguna señal encontrada")
        else:
            logger.info(f"[MarketScanner] {len(signals)} señales encontradas")

        return signals

    # ═══════════════════════════════════════════════════════════
    # 🟢 MATCH CON PATRONES CON BOLLINGER
    # ═══════════════════════════════════════════════════════════

    def _match_patterns_with_bb(
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
        
        golden_score = self._evaluate_golden_rules_enhanced(behavior, macro_angle, timeframe, bb_analysis)
        
        if golden_score < 8.0:
            return matches

        for pattern in patterns:
            if pattern.get("symbol") not in {symbol_code, "UNIVERSAL"}:
                continue

            if pattern.get("timeframe") != timeframe:
                continue

            score = 0
            total = 0

            # Campos base
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

            # Campos adicionales de Bollinger
            bb_pattern = pattern.get("bb_estado")
            if bb_pattern and bb_pattern != "N/A":
                total += 1
                bb_actual = "SQUEEZE" if bb_analysis['is_squeeze'] else "EXPANDING" if bb_analysis['is_expanding'] else "NORMAL"
                if bb_actual == bb_pattern:
                    score += 1

            if total == 0:
                continue

            match_ratio = score / total
            final_score = match_ratio + (golden_score / 100.0) * 0.4
            final_score = min(final_score, 1.0)

            if final_score < 0.30:
                continue

            matches.append({
                "pattern": pattern,
                "match_ratio": final_score,
                "score": score,
                "total": total,
            })

        return matches

    def _evaluate_golden_rules_enhanced(
        self, 
        behavior: Dict[str, Any], 
        macro_angle: str, 
        timeframe: str,
        bb_analysis: Dict[str, Any]
    ) -> float:
        """Evalúa reglas de oro con Bollinger"""
        score = 0.0
        bb_pos = bb_analysis.get('position', 'MID')
        volumen = behavior.get("volumen", "LOW")
        adx_force = behavior.get("adx_tendencia", "RANGE")
        daily_pct = behavior.get("daily_pct_change", 0.0)
        signal_type = behavior.get("signal_type", "UNKNOWN")
        auction_type = behavior.get("auction_type", "BALANCED")
        is_squeeze = bb_analysis.get('is_squeeze', False)
        
        # Macro alignment
        if macro_angle == "BULLISH" and "SHORT" in str(signal_type):
            return 0.0
        if macro_angle == "BEARISH" and "LONG" in str(signal_type):
            return 0.0

        # ADX - reducido requisito
        if adx_force in ["RANGE"]:
            return 0.0

        # Daily change
        if daily_pct > 0.05 and "LONG" in str(signal_type):
            return 0.0
        if daily_pct < -0.05 and "SHORT" in str(signal_type):
            return 0.0

        # Reglas LONG
        if "LONG" in str(signal_type):
            # Bollinger en zona de compra
            if bb_pos in ["AT_LOWER", "BELOW_LOWER", "BELOW_MIDDLE"]:
                score += 25
            elif bb_pos in ["SQUEEZE"]:
                score += 20
            
            # EMA alignment
            ema55_144 = behavior.get("ema55_vs_ema144", "")
            if ema55_144 in ["CROSSING_UP", "ABOVE"]:
                score += 20
            
            # Volumen
            if volumen in ["HIGH", "MEDIUM"]:
                score += 15
            
            # Subasta
            if auction_type == "BUYERS_IN_CONTROL":
                score += 15

        # Reglas SHORT
        elif "SHORT" in str(signal_type):
            # Bollinger en zona de venta
            if bb_pos in ["AT_UPPER", "ABOVE_UPPER", "ABOVE_MIDDLE"]:
                score += 25
            elif bb_pos in ["SQUEEZE"]:
                score += 20
            
            # EMA alignment
            ema55_144 = behavior.get("ema55_vs_ema144", "")
            if ema55_144 in ["CROSSING_DOWN", "BELOW"]:
                score += 20
            
            # Volumen
            if volumen in ["HIGH", "MEDIUM"]:
                score += 15
            
            # Subasta
            if auction_type == "SELLERS_IN_CONTROL":
                score += 15

        return max(0.0, score)

    # ═══════════════════════════════════════════════════════════
    # 🟢 DESCRIPCIÓN DE COMPORTAMIENTO M3
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
    # 🟢 CONSTRUCCIÓN DE SEÑAL MEJORADA
    # ═══════════════════════════════════════════════════════════

    def _build_signal_enhanced(
        self,
        symbol: str,
        df: pd.DataFrame,
        behavior: Dict[str, Any],
        matched: Dict[str, Any],
        timeframe: str,
        score: float,
        bb_analysis: Dict[str, Any],
        auction: Dict[str, Any],
        historical_context: Dict[str, Any]
    ) -> Optional[Signal]:
        """Construye señal mejorada con Bollinger y WASHI"""
        try:
            pattern = matched["pattern"]
            signal_type_str = pattern.get("signal_type", "")
            
            try:
                signal_type = SignalType(signal_type_str)
            except ValueError:
                logger.warning(f"[MarketScanner] Tipo inválido: {signal_type_str}")
                return None
            
            current_price = df["close"].iloc[-1]
            atr = behavior.get("atr", current_price * 0.01)
            
            # Calcular SL/TP basado en ATR y Bollinger
            bb_upper = bb_analysis['upper']
            bb_lower = bb_analysis['lower']
            bb_middle = bb_analysis['middle']
            is_squeeze = bb_analysis['is_squeeze']
            
            if signal_type.is_long():
                # SL cerca de BB Lower o ATR
                if is_squeeze:
                    stop_loss = current_price - (atr * 1.2)
                else:
                    stop_loss = current_price - (atr * 1.5)
                # TP en BB Upper o 2x ATR
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
            
            # Determinar nivel de riesgo
            bb_pos = bb_analysis['position']
            if bb_pos in ["AT_LOWER", "BELOW_LOWER"] and signal_type.is_long():
                risk_level = "BAJO"
            elif bb_pos in ["AT_UPPER", "ABOVE_UPPER"] and signal_type.is_short():
                risk_level = "BAJO"
            elif is_squeeze:
                risk_level = "MEDIO"
            else:
                risk_level = "MEDIO"
            
            # Crear señal
            signal = Signal(
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
                bb_squeeze=is_squeeze
            )
            
            return signal
            
        except Exception as e:
            logger.error(f"[MarketScanner] Error construyendo señal para {symbol}: {e}")
            return None

    def _build_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        behavior: Dict[str, Any],
        matched: Dict[str, Any],
        timeframe: str,
        score: float
    ) -> Optional[Signal]:
        """Mantiene compatibilidad con el código existente"""
        return self._build_signal_enhanced(
            symbol, df, behavior, matched, timeframe, score,
            {'position': 'MID', 'upper': 0, 'lower': 0, 'middle': 0, 'is_squeeze': False},
            {'type': 'BALANCED'},
            {'position': 'MID_RANGE'}
        )
