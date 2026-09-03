# strategy_scanner.py - Strategy Scanner WASHI RADAR UNIFICADO
# ============================================================
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
    LISTO = "LISTO"
    CALENTANDO = "CALENTANDO"
    FRESCO = "FRESCO"
    FRIO = "FRIO"
    ESPERANDO = "ESPERANDO"

@dataclass
class OportunidadActivo:
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

# 🔥 NOMBRE CORREGIDO: MarketScanner para coincidir con main.py
class MarketScanner:
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
        
        self.oportunidades: Dict[str, OportunidadActivo] = {}
        self.ultimo_escaneo = None
        self.escaneos_totales = 0
        
        self.learning_engine = LearningEngine(db_path=db_path, modo=modo_aprendizaje)
        self.patterns_by_tf = {}
        self._signal_cooldown = {}
        self._historical_cache = {}
        self._historical_cache_time = {}
        self._bb_cache = {}
        self.bb_period = 20
        self.bb_std = 2
        
        self._load_all_patterns()

    def _load_all_patterns(self) -> None:
        if not os.path.exists(self.db_path):
            for tf in self.timeframes:
                self.patterns_by_tf[tf] = [p.copy() for p in seed_patterns.PATTERNS if p.get("timeframe") == tf]
            return
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                for tf in self.timeframes:
                    rows = conn.execute("SELECT * FROM patterns WHERE timeframe = ? AND resultado != 'EVITAR'", (tf,)).fetchall()
                    self.patterns_by_tf[tf] = [dict(row) for row in rows]
        except Exception as exc:
            for tf in self.timeframes:
                self.patterns_by_tf[tf] = [p.copy() for p in seed_patterns.PATTERNS if p.get("timeframe") == tf]

    def _normalize_symbol(self, symbol: str) -> str:
        return re.sub(r"[^\w]", "", symbol).upper()

    def _calculate_bollinger_bands(self, df: pd.DataFrame) -> Dict[str, Any]:
        mid = df['close'].rolling(window=self.bb_period).mean()
        std = df['close'].rolling(window=self.bb_period).std()
        upper = mid + (self.bb_std * std)
        lower = mid - (self.bb_std * std)
        return {'upper': upper, 'middle': mid, 'lower': lower, 'bandwidth': (upper - lower) / mid, 'std': std}
    
    def _analyze_bb_position(self, df: pd.DataFrame, price: float) -> Dict[str, Any]:
        bb = self._calculate_bollinger_bands(df)
        upper = bb['upper'].iloc[-1]
        middle = bb['middle'].iloc[-1]
        lower = bb['lower'].iloc[-1]
        bandwidth = bb['bandwidth'].iloc[-1]
        avg_bandwidth = bb['bandwidth'].tail(20).mean()
        is_squeeze = bandwidth < avg_bandwidth * 0.7
        is_expanding = bandwidth > avg_bandwidth * 1.3
        
        if price > upper: position = "ABOVE_UPPER"
        elif price >= upper * 0.995: position = "AT_UPPER"
        elif price > middle and price < upper: position = "ABOVE_MIDDLE"
        elif abs(price - middle) / price < 0.005: position = "AT_MIDDLE"
        elif price < middle and price > lower: position = "BELOW_MIDDLE"
        elif price <= lower * 1.005: position = "AT_LOWER"
        elif price < lower: position = "BELOW_LOWER"
        else: position = "AT_MIDDLE"
        
        if is_squeeze and position not in ["AT_UPPER", "AT_LOWER"]: position = "SQUEEZE"
        elif is_expanding: position = "EXPANDING"
        
        return {'position': position, 'upper': upper, 'middle': middle, 'lower': lower, 'bandwidth': bandwidth, 'is_squeeze': is_squeeze, 'is_expanding': is_expanding}

    def _analyze_auction(self, df: pd.DataFrame) -> Dict[str, Any]:
        close = df['close']; open_price = df['open']
        buy_volume = df[close > open_price]['volume'].sum()
        sell_volume = df[close < open_price]['volume'].sum()
        total_volume = buy_volume + sell_volume
        if total_volume == 0: return {'type': 'BALANCED', 'buy_ratio': 0.5, 'sell_ratio': 0.5}
        buy_ratio = buy_volume / total_volume; sell_ratio = sell_volume / total_volume
        vwap = (df['close'] * df['volume']).sum() / df['volume'].sum() if df['volume'].sum() > 0 else close.iloc[-1]
        price = close.iloc[-1]
        price_vs_vwap = (price - vwap) / vwap if vwap > 0 else 0
        delta = (df['volume'] * np.where(df['close'] > df['open'], 1, -1)).sum()
        if buy_ratio > 0.52 and price_vs_vwap > 0 and delta > 0: auction_type = "BUYERS_IN_CONTROL"
        elif sell_ratio > 0.52 and price_vs_vwap < 0 and delta < 0: auction_type = "SELLERS_IN_CONTROL"
        else: auction_type = "BALANCED"
        return {'type': auction_type, 'buy_ratio': buy_ratio, 'sell_ratio': sell_ratio, 'vwap': vwap, 'price_vs_vwap': price_vs_vwap, 'delta': delta}

    def _detect_crossovers(self, df: pd.DataFrame) -> Dict[str, str]:
        if len(df) < 3: return {}
        ema21 = df['close'].ewm(span=21, adjust=False).mean()
        ema55 = df['close'].ewm(span=55, adjust=False).mean()
        ema144 = df['close'].ewm(span=144, adjust=False).mean()
        ema233 = df['close'].ewm(span=233, adjust=False).mean()
        crossovers = {}
        if len(ema21) > 2:
            prev_21 = ema21.iloc[-2]; curr_21 = ema21.iloc[-1]; prev_55 = ema55.iloc[-2]; curr_55 = ema55.iloc[-1]
            if prev_21 <= prev_55 and curr_21 > curr_55: crossovers['ema21_55'] = 'CROSSING_UP'
            elif prev_21 >= prev_55 and curr_21 < curr_55: crossovers['ema21_55'] = 'CROSSING_DOWN'
            else: crossovers['ema21_55'] = 'NO_CROSS'
        if len(ema55) > 2:
            prev_55 = ema55.iloc[-2]; curr_55 = ema55.iloc[-1]; prev_144 = ema144.iloc[-2]; curr_144 = ema144.iloc[-1]
            if prev_55 <= prev_144 and curr_55 > curr_144: crossovers['ema55_144'] = 'CROSSING_UP'
            elif prev_55 >= prev_144 and curr_55 < curr_144: crossovers['ema55_144'] = 'CROSSING_DOWN'
            else: crossovers['ema55_144'] = 'NO_CROSS'
        return crossovers

    def _describe_behavior_m3(self, df: pd.DataFrame, daily_pct: float, macro_angle: str) -> Dict[str, Any]:
        df = df.copy()
        df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
        df["ema55"] = df["close"].ewm(span=55, adjust=False).mean()
        df["ema144"] = df["close"].ewm(span=144, adjust=False).mean()
        df["bb_mid"] = df["close"].rolling(20).mean()
        df["bb_std"] = df["close"].rolling(20).std()
        df["bb_upper"] = df["bb_mid"] + 2 * df["bb_std"]
        df["bb_lower"] = df["bb_mid"] - 2 * df["bb_std"]
        df["tr"] = np.maximum(df["high"] - df["low"], np.maximum(abs(df["high"] - df["close"].shift(1)), abs(df["low"] - df["close"].shift(1))))
        df["atr"] = df["tr"].rolling(14).mean()
        df["up"] = df["high"] - df["high"].shift(1)
        df["down"] = df["low"].shift(1) - df["low"]
        df["+dm"] = np.where((df["up"] > df["down"]) & (df["up"] > 0), df["up"], 0.0)
        df["-dm"] = np.where((df["down"] > df["up"]) & (df["down"] > 0), df["down"], 0.0)
        df["+di"] = 100 * (df["+dm"].rolling(14).mean() / df["atr"])
        df["-di"] = 100 * (df["-dm"].rolling(14).mean() / df["atr"])
        df["dx"] = 100 * abs(df["+di"] - df["-di"]) / (df["+di"] + df["-di"])
        df["adx"] = df["dx"].rolling(14).mean()
        current = df.iloc[-1]; prior = df.iloc[-2]

        def position_label(price: float, target: float) -> str:
            if target == 0 or price == 0: return "N/A"
            diff = price - target; pct = abs(diff / max(price, 1e-6))
            if pct < 0.002: return "TOUCHING"
            if pct < 0.008: return "NEAR"
            return "ABOVE" if diff > 0 else "BELOW"

        def ema_relation(fast: float, slow: float, prev_fast: float, prev_slow: float) -> str:
            if fast > slow and prev_fast <= prev_slow: return "CROSSING_UP"
            if fast < slow and prev_fast >= prev_slow: return "CROSSING_DOWN"
            if abs(fast - slow) / max(slow, 1e-6) < 0.0015: return "FLAT"
            return "ABOVE" if fast > slow else "BELOW"

        def bb_price_label(close: float, mid: float, lower: float, upper: float) -> str:
            if close >= upper: return "UPPER"
            if close <= lower: return "LOWER"
            if close >= mid: return "MID_TO_UPPER"
            return "MID_TO_LOWER"

        def volume_label(volume: float, average: float) -> str:
            if average == 0: return "LOW"
            ratio = volume / average
            if ratio >= 2.0: return "HIGH"
            if ratio >= 1.2: return "MEDIUM"
            if ratio < 0.35: return "VERY_LOW"
            return "LOW"

        def candle_pattern(row: pd.Series) -> str:
            body = abs(row["close"] - row["open"])
            if body == 0: return "NEUTRAL"
            upper_wick = float(row["high"] - max(row["close"], row["open"]))
            lower_wick = float(min(row["close"], row["open"]) - row["low"])
            if row["close"] > row["open"] and body > upper_wick * 2: return "STRONG_GREEN"
            if row["close"] < row["open"] and body > lower_wick * 2: return "STRONG_RED"
            if upper_wick > body * 1.5 and row["close"] < row["open"]: return "REJECTION"
            if lower_wick > body * 1.5 and row["close"] > row["open"]: return "HAMMER"
            return "NEUTRAL"

        def adx_tendencia_label(adx_val: float) -> str:
            if adx_val > 25: return "STRONG"
            elif adx_val > 20: return "WEAK"
            else: return "RANGE"

        price = float(current["close"])
        volume_average = float(df["volume"].rolling(20).mean().iloc[-2] or 0.0)

        return {
            "precio_vs_ema21": position_label(price, float(current["ema21"])),
            "precio_vs_ema55": position_label(price, float(current["ema55"])),
            "precio_vs_ema144": position_label(price, float(current["ema144"])),
            "ema21_vs_ema55": ema_relation(float(current["ema21"]), float(current["ema55"]), float(prior["ema21"]), float(prior["ema55"])),
            "ema55_vs_ema144": ema_relation(float(current["ema55"]), float(current["ema144"]), float(prior["ema55"]), float(prior["ema144"])),
            "bb_precio": bb_price_label(price, float(current["bb_mid"]), float(current["bb_lower"]), float(current["bb_upper"])),
            "volumen": volume_label(float(current["volume"]), volume_average),
            "patron_vela": candle_pattern(current),
            "entry_price": price,
            "adx_tendencia": adx_tendencia_label(float(current["adx"])),
            "daily_pct_change": daily_pct,
            "macro_angle": macro_angle,
            "atr": float(current["atr"]) if not pd.isna(current["atr"]) else price * 0.01,
        }

    def _match_patterns(self, symbol_code: str, behavior: Dict[str, Any], timeframe: str, macro_angle: str, bb_analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
        matches = []
        patterns = self.patterns_by_tf.get(timeframe, [])
        golden_score = self._evaluate_golden_rules(behavior, macro_angle, timeframe, bb_analysis)
        # 🔥 ELIMINADO EL FILTRO ESTRICTO: if golden_score < 5.0: return matches
        
        for pattern in patterns:
            if pattern.get("symbol") not in {symbol_code, "UNIVERSAL"}: continue
            if pattern.get("timeframe") != timeframe: continue
            score = 0; total = 0
            base_fields = ["precio_vs_ema21", "precio_vs_ema55", "precio_vs_ema144", "ema21_vs_ema55", "ema55_vs_ema144", "bb_precio", "volumen", "patron_vela", "adx_tendencia"]
            for key in base_fields:
                expected = pattern.get(key); actual = behavior.get(key)
                if expected is None or expected == "N/A" or actual is None: continue
                total += 1
                if str(expected).upper() == str(actual).upper(): score += 1
            if total == 0: continue
            match_ratio = score / total
            final_score = match_ratio + (golden_score / 100.0) * 0.4
            final_score = min(final_score, 1.0)
            if final_score < 0.30: continue
            matches.append({"pattern": pattern, "match_ratio": final_score, "score": score, "total": total})
        return matches

    def _evaluate_golden_rules(self, behavior: Dict[str, Any], macro_angle: str, timeframe: str, bb_analysis: Dict[str, Any]) -> float:
        score = 0.0
        bb_pos = bb_analysis.get('position', 'MID')
        volumen = behavior.get("volumen", "LOW")
        adx_force = behavior.get("adx_tendencia", "RANGE")
        daily_pct = behavior.get("daily_pct_change", 0.0)
        is_squeeze = bb_analysis.get('is_squeeze', False)
        
        # 🔥 CORRECCIÓN CRÍTICA: Inferir la dirección según la posición en Bollinger
        if bb_pos in ["AT_LOWER", "BELOW_LOWER", "BELOW_MIDDLE"]:
            signal_type = "LONG"
        elif bb_pos in ["AT_UPPER", "ABOVE_UPPER", "ABOVE_MIDDLE"]:
            signal_type = "SHORT"
        else:
            signal_type = "UNKNOWN"
        
        if macro_angle == "BULLISH" and "SHORT" in str(signal_type): return 0.0
        if macro_angle == "BEARISH" and "LONG" in str(signal_type): return 0.0
        if daily_pct > 0.05 and "LONG" in str(signal_type): return 0.0
        if daily_pct < -0.05 and "SHORT" in str(signal_type): return 0.0

        if "LONG" in str(signal_type):
            if bb_pos in ["AT_LOWER", "BELOW_LOWER", "BELOW_MIDDLE"]: score += 25
            elif bb_pos in ["SQUEEZE"]: score += 20
            if behavior.get("ema55_vs_ema144", "") in ["CROSSING_UP", "ABOVE"]: score += 20
            if volumen in ["HIGH", "MEDIUM"]: score += 15

        elif "SHORT" in str(signal_type):
            if bb_pos in ["AT_UPPER", "ABOVE_UPPER", "ABOVE_MIDDLE"]: score += 25
            elif bb_pos in ["SQUEEZE"]: score += 20
            if behavior.get("ema55_vs_ema144", "") in ["CROSSING_DOWN", "BELOW"]: score += 20
            if volumen in ["HIGH", "MEDIUM"]: score += 15

        # 🔥 Puntaje base bajo si no hay tendencia para no descartar todo
        if adx_force == "RANGE": score += 10

        return max(0.0, score)

    def _build_signal(self, symbol: str, df: pd.DataFrame, behavior: Dict[str, Any], matched: Dict[str, Any], timeframe: str, score: float, bb_analysis: Dict[str, Any], auction: Dict[str, Any], historical_context: Dict[str, Any], oportunidad: Optional[OportunidadActivo] = None) -> Optional[Signal]:
        try:
            pattern = matched["pattern"]
            try:
                signal_type = SignalType(pattern.get("signal_type", ""))
            except ValueError:
                return None
            current_price = df["close"].iloc[-1]
            atr = behavior.get("atr", current_price * 0.01)
            bb_upper = bb_analysis['upper']; bb_lower = bb_analysis['lower']
            is_squeeze = bb_analysis['is_squeeze']
            if signal_type.is_long():
                stop_loss = current_price - (atr * 1.2 if is_squeeze else atr * 1.5)
                take_profit = current_price + (atr * 3.0)
                entry_zone_min = max(bb_lower, current_price * 0.995)
                entry_zone_max = current_price * 1.005
            else:
                stop_loss = current_price + (atr * 1.2 if is_squeeze else atr * 1.5)
                take_profit = current_price - (atr * 3.0)
                entry_zone_min = current_price * 0.995
                entry_zone_max = min(bb_upper, current_price * 1.005)
            risk = abs(current_price - stop_loss)
            reward = abs(take_profit - current_price)
            risk_reward = reward / risk if risk > 0 else 0
            return Signal(
                symbol=symbol, signal_type=signal_type, score=score, risk_reward=risk_reward,
                stop_loss=stop_loss, take_profit=take_profit, entry_price=current_price,
                df=df.copy(), pattern_id=pattern.get("id"), timeframe=timeframe,
                confidence=score, risk_level="MEDIO", entry_zone_min=entry_zone_min,
                entry_zone_max=entry_zone_max, entry_reason=pattern.get("notas", ""),
                auction_type=auction.get('type', 'BALANCED'),
                market_position=historical_context.get("position", "MID_RANGE"),
                bb_position=bb_analysis.get('position', 'MID'), bb_squeeze=is_squeeze,
                oportunidad=oportunidad
            )
        except Exception as e:
            logger.error(f"Error construyendo señal: {e}")
            return None

    async def _get_historical_context(self, symbol: str) -> Dict[str, Any]:
        context = {"day_high": 0.0, "day_low": 0.0, "week_high": 0.0, "week_low": 0.0, "month_high": 0.0, "month_low": 0.0, "ath": 0.0, "atl": 0.0, "position": "MID_RANGE", "current_price": 0.0}
        try:
            ticker = await self.api._safe_call(lambda: self.api.exchange.fetch_ticker(symbol), endpoint_type="public")
            context["current_price"] = ticker.get("last", 0)
            df_hist = await self.api.fetch_ohlcv(symbol, timeframe="1h", limit=720)
            if df_hist is not None and not df_hist.empty:
                context["ath"] = df_hist["high"].max(); context["atl"] = df_hist["low"].min()
                context["month_high"] = df_hist["high"].max(); context["month_low"] = df_hist["low"].min()
            price = context["current_price"]; ath = context["ath"]; atl = context["atl"]
            if ath > 0 and price >= (ath * 0.95): context["position"] = "ATH_ZONE"
            elif atl > 0 and price <= (atl * 1.05): context["position"] = "ATL_ZONE"
            elif context["month_high"] > 0 and price >= (context["month_high"] * 0.95): context["position"] = "MONTH_HIGH"
            elif context["month_low"] > 0 and price <= (context["month_low"] * 1.05): context["position"] = "MONTH_LOW"
        except Exception as e:
            logger.debug(f"Error en contexto histórico: {e}")
        return context

    async def scan_all(self) -> List[Signal]:
        self.escaneos_totales += 1
        signals = []
        now = time.time()
        self.ultimo_escaneo = datetime.now()
        logger.info(f"📡 ESCANEO #{self.escaneos_totales}")

        ticker_map = {}
        try:
            response = self.api.exchange.get_tickers(category="linear")
            tickers = response["result"]["list"]
            for t in tickers:
                ticker_map[t["symbol"]] = float(t.get("price24hPcnt", 0.0))
        except Exception:
            pass

        for symbol in self.watchlist:
            try:
                if symbol in self._signal_cooldown:
                    if now - self._signal_cooldown[symbol] < 300: continue
                daily_pct = ticker_map.get(symbol, 0.0)
                historical_context = await self._get_historical_context(symbol)
                
                df_15m = await self.api.fetch_ohlcv(symbol, timeframe="15m", limit=80)
                if df_15m is None or len(df_15m) < 60: continue
                df_15m["ema55"] = df_15m["close"].ewm(span=55, adjust=False).mean()
                df_15m["ema144"] = df_15m["close"].ewm(span=144, adjust=False).mean()
                df_15m["ema233"] = df_15m["close"].ewm(span=233, adjust=False).mean()
                price_15m = df_15m["close"].iloc[-1]
                ema55_15m = df_15m["ema55"].iloc[-1]
                distance_pct = abs(price_15m - ema55_15m) / price_15m if price_15m > 0 else 0.1
                if distance_pct > 0.30: continue
                macro_angle = "FLAT"
                if price_15m > ema55_15m and price_15m > df_15m["ema144"].iloc[-1] and price_15m > df_15m["ema233"].iloc[-1]: macro_angle = "BULLISH"
                elif price_15m < ema55_15m and price_15m < df_15m["ema144"].iloc[-1] and price_15m < df_15m["ema233"].iloc[-1]: macro_angle = "BEARISH"
                
                df_3m = await self.api.fetch_ohlcv(symbol, timeframe="3m", limit=120)
                if df_3m is None or len(df_3m) < 60: continue
                price = df_3m["close"].iloc[-1]
                bb_analysis = self._analyze_bb_position(df_3m, price)
                auction = self._analyze_auction(df_3m)
                behavior = self._describe_behavior_m3(df_3m, daily_pct, macro_angle)
                behavior['bb_position'] = bb_analysis['position']
                behavior['bb_is_squeeze'] = bb_analysis['is_squeeze']
                behavior['bb_is_expanding'] = bb_analysis['is_expanding']
                behavior['auction_type'] = auction['type']
                symbol_code = self._normalize_symbol(symbol)
                matches = self._match_patterns(symbol_code, behavior, "3m", macro_angle, bb_analysis)
                if not matches: 
                    # 🔥 PRINT DE DEPURACIÓN
                    logger.debug(f"[DEBUG] {symbol} {symbol_code}: Sin matches, Score: N/A")
                    continue
                best = max(matches, key=lambda item: item["match_ratio"])
                try:
                    signal_type = SignalType(best["pattern"]["signal_type"])
                except ValueError:
                    continue
                position = historical_context.get("position", "MID_RANGE")
                score = best["match_ratio"]
                if position in ["ATH_ZONE", "MONTH_HIGH"] and signal_type.is_long(): score = score * 0.3
                elif position in ["ATL_ZONE", "MONTH_LOW"] and signal_type.is_short(): score = score * 0.3
                bb_pos = bb_analysis['position']
                if signal_type.is_long() and bb_pos in ["ABOVE_UPPER", "AT_UPPER"]: score = score * 0.7
                elif signal_type.is_short() and bb_pos in ["BELOW_LOWER", "AT_LOWER"]: score = score * 0.7
                
                # 🔥 PRINT DE DEPURACIÓN PARA VER EL SCORE
                logger.debug(f"[DEBUG] {symbol} {symbol_code}: Match encontrado, Score: {score:.2f}")
                
                if score < self.min_score: continue
                signal = self._build_signal(symbol, df_3m, behavior, best, "3m", score, bb_analysis, auction, historical_context)
                if signal:
                    self._signal_cooldown[symbol] = now
                    signals.append(signal)
                    logger.info(f"✅ {signal.signal_type.value} {symbol} | Score: {score:.2f} | BB: {bb_pos}")
            except Exception as e:
                logger.debug(f"Error en {symbol}: {e}")
                continue
        if not signals:
            logger.info("[StrategyScanner] Ninguna señal encontrada")
        else:
            logger.info(f"[StrategyScanner] {len(signals)} señales generadas")
        return signals

    def register_trade(self, trade: TradeRecord) -> int:
        return self.learning_engine.register_trade(trade)
