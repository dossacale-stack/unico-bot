# main.py - UNICO STRATEGY v7.0 - TODO INTEGRADO
# =============================================
# Este archivo contiene TODO: el bot, el scanner con lógica de agotamiento,
# el debug, y la gestión de riesgo. Un solo archivo para evitar problemas
# de sincronización entre GitHub y Railway.

import argparse
import asyncio
import logging
import os
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from pybit.unified_trading import HTTP

from bybit_api_manager import BybitAPIManager
from order_executor import OrderExecutor
from risk_manager import BotMode, CloseReason, RiskManager
import seed_patterns

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("UNICO")


# ═══════════════════════════════════════════════════════════
# SIGNAL TYPE
# ═══════════════════════════════════════════════════════════
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


# ═══════════════════════════════════════════════════════════
# SCANNER CON LÓGICA DE AGOTAMIENTO
# ═══════════════════════════════════════════════════════════
class MarketScanner:
    EMA_FAST = 55
    EMA_MID = 144
    EMA_SLOW = 233
    BB_PERIOD = 21
    BB_STD = 2.0

    MIN_DISTANCE_PCT = 0.01
    MAX_DISTANCE_PCT = 0.30
    MIN_CONSECUTIVE_CANDLES = 2
    MIN_BB_EXPANSION = 0.02
    MIN_TIME_IN_ZONE = 2
    MIN_EXHAUSTION_SCORE = 0.20
    REJECTION_WICK_RATIO = 1.0

    DEBUG_MODE = True

    def __init__(
        self,
        api_manager: BybitAPIManager,
        watchlist: List[str],
        scan_interval: float = 60.0,
        min_score: float = 0.20,
        min_rr: float = 0.8,
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
            f"[Scanner] AGOTAMIENTO v2 | EMA {self.EMA_FAST}/{self.EMA_MID}/{self.EMA_SLOW} "
            f"| BB({self.BB_PERIOD},{self.BB_STD}) | min_score={min_score} | DEBUG={self.DEBUG_MODE}"
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
            dist_score = min(max(distance_pct - self.MIN_DISTANCE_PCT, 0) / 0.08, 1.0)
        else:
            dist_score = min(max(-distance_pct - self.MIN_DISTANCE_PCT, 0) / 0.08, 1.0)

        closes = df["close"].values
        consecutive = 0
        for i in range(len(df) - 2, max(len(df) - 20, 0), -1):
            if direction == "UP" and closes[i] > closes[i - 1]:
                consecutive += 1
            elif direction == "DOWN" and closes[i] < closes[i - 1]:
                consecutive += 1
            else:
                break
        consec_score = min(consecutive / 8.0, 1.0)

        bw_now = float(last["bb_bandwidth"])
        bw_10 = float(df["bb_bandwidth"].iloc[-10])
        if bw_10 > 0:
            bb_expansion = (bw_now - bw_10) / bw_10
        else:
            bb_expansion = 0.0
        bb_score = min(max(bb_expansion, 0) / 0.30, 1.0)

        threshold_pct = 0.015 if direction == "UP" else -0.015
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
        time_score = min(candles_in_zone / 15.0, 1.0)

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
        return lower_wick > body * self.REJECTION_WICK_RATIO and c >= o * 0.995

    def _detect_rejection_bearish(self, row: pd.Series) -> bool:
        o, c, h, l = float(row["open"]), float(row["close"]), float(row["high"]), float(row["low"])
        body = abs(c - o) or (h - l) * 0.1
        upper_wick = h - max(o, c)
        return upper_wick > body * self.REJECTION_WICK_RATIO and c <= o * 1.005

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

        if self.DEBUG_MODE:
            if stack == "BEARISH":
                ex_dbg = self._exhaustion_score(df, "UP")
                has_rej = self._detect_rejection_bearish(last) or self._detect_rejection_bearish(prev)
                logger.info(
                    f"[DEBUG] BEARISH | Score {ex_dbg['score']:.2f} (min {self.MIN_EXHAUSTION_SCORE}) | "
                    f"Dist {ex_dbg['distance_pct']*100:+.2f}% | Consec {ex_dbg['consecutive']} | "
                    f"BB {ex_dbg['bb_expansion']*100:+.1f}% | Zona {ex_dbg['candles_in_zone']} | "
                    f"Rechazo: {'SI' if has_rej else 'NO'}"
                )
            elif stack == "BULLISH":
                ex_dbg = self._exhaustion_score(df, "DOWN")
                has_rej = self._detect_rejection_bullish(last) or self._detect_rejection_bullish(prev)
                logger.info(
                    f"[DEBUG] BULLISH | Score {ex_dbg['score']:.2f} (min {self.MIN_EXHAUSTION_SCORE}) | "
                    f"Dist {ex_dbg['distance_pct']*100:+.2f}% | Consec {ex_dbg['consecutive']} | "
                    f"BB {ex_dbg['bb_expansion']*100:+.1f}% | Zona {ex_dbg['candles_in_zone']} | "
                    f"Rechazo: {'SI' if has_rej else 'NO'}"
                )

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


# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════
CONFIG: Dict[str, Any] = {
    "API_KEY": os.getenv("BYBIT_API_KEY", ""),
    "API_SECRET": os.getenv("BYBIT_API_SECRET", ""),
    "MODE": "DRY_RUN",
    "SCANNER_ENABLED": True,
    "SCAN_INTERVAL": float(os.getenv("SCAN_INTERVAL", "60.0")),
    "MIN_SCORE": 0.20,
    "MIN_RR": 0.8,
    "TIMEFRAMES": ["1h"],
    "MAX_POSITIONS": int(os.getenv("MAX_POSITIONS", "2")),
    "POSITION_PCT": float(os.getenv("POSITION_PCT", "0.30")),
    "SL_PCT": float(os.getenv("SL_PCT", "0.15")),
    "TP_MULTIPLE": float(os.getenv("TP_MULTIPLE", "5.0")),
    "LEVERAGE": int(os.getenv("LEVERAGE", "10")),
    "COOLDOWN_MINUTES": int(os.getenv("COOLDOWN_MINUTES", "60")),
    "MAX_ENTRIES_DAILY": int(os.getenv("MAX_ENTRIES_DAILY", "5")),
    "DB_PATH": os.getenv("DB_PATH", "patterns.db"),
    "WATCHLIST": [],
    "TRAILING_ACTIVATED": True,
}

FALLBACK_WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT", "MATICUSDT"
]


# ═══════════════════════════════════════════════════════════
# TRAILING STOP MANAGER
# ═══════════════════════════════════════════════════════════
class TrailingStopManager:
    def __init__(self, api_manager, callback_atr_mult: float = 2.5, is_active: bool = True):
        self.api = api_manager
        self.callback_atr_mult = callback_atr_mult
        self.is_active = is_active
        self._tracked_positions = {}

    async def manage(self, symbol: str, side: str, atr: float = 0.0) -> None:
        if not self.is_active or atr <= 0:
            return
        try:
            response = await self.api.get_positions(symbol=symbol)
            if isinstance(response, dict) and "list" in response:
                positions = response["list"]
            elif isinstance(response, list):
                positions = response
            else:
                return
            if not positions:
                return
            pos = positions[0]
            if float(pos.get("size", 0)) == 0:
                return
        except Exception as e:
            logger.debug(f"[TrailingStop] {symbol}: {e}")


# ═══════════════════════════════════════════════════════════
# BOT PRINCIPAL
# ═══════════════════════════════════════════════════════════
class UnicoBot:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.mode = BotMode.DRY_RUN

        self.api = BybitAPIManager(
            api_key=config["API_KEY"],
            api_secret=config["API_SECRET"],
            sandbox=False
        )
        self.rm = RiskManager(
            api_manager=self.api,
            mode=self.mode,
            db_path=config["DB_PATH"],
            max_positions=config["MAX_POSITIONS"],
            position_pct=config.get("POSITION_PCT", 0.30),
            sl_pct=config.get("SL_PCT", 0.15),
            tp_multiple=config.get("TP_MULTIPLE", 5.0),
            leverage=config.get("LEVERAGE", 10),
            cooldown_minutes=config.get("COOLDOWN_MINUTES", 60),
            max_entries_daily=config.get("MAX_ENTRIES_DAILY", 5)
        )
        self.scanner = MarketScanner(
            api_manager=self.api,
            watchlist=[],
            scan_interval=config["SCAN_INTERVAL"],
            min_score=config["MIN_SCORE"],
            min_rr=config["MIN_RR"],
            position_pct=config["POSITION_PCT"],
            db_path=config["DB_PATH"],
            signal_cooldown_seconds=300,
            timeframes=config.get("TIMEFRAMES", ["1h"])
        )
        self.executor = OrderExecutor(api_manager=self.api, mode=self.mode)
        self.trailing_stop = TrailingStopManager(api_manager=self.api, is_active=True)

        self.running = False
        self.stats = {
            "cycles": 0, "signals": 0, "opened": 0, "closed": 0,
            "tp1_hits": 0, "tp2_hits": 0,
            "started_at": datetime.now(timezone.utc).isoformat()
        }

        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    async def generate_dynamic_watchlist(self) -> List[str]:
        def to_ccxt_symbol(bybit_symbol: str) -> str:
            if bybit_symbol.endswith('USDT') and not bybit_symbol.endswith('USDC'):
                return f"{bybit_symbol[:-4]}/USDT:USDT"
            return bybit_symbol
        try:
            session = HTTP(testnet=False)
            response = session.get_tickers(category="linear")
            tickers = response["result"]["list"]

            valid_tickers = []
            for t in tickers:
                sym = t.get("symbol", "")
                if not sym.endswith("USDT"):
                    continue
                if "USDC" in sym:
                    continue
                try:
                    if float(t.get("lastPrice", 0)) <= 0:
                        continue
                except (ValueError, TypeError):
                    continue
                valid_tickers.append(t)

            sorted_24h = sorted(valid_tickers, key=lambda x: float(x.get("price24hPcnt", 0)), reverse=True)
            top_24h = [to_ccxt_symbol(t["symbol"]) for t in sorted_24h[:15]]

            sorted_1h = sorted(valid_tickers, key=lambda x: float(x.get("price1hPcnt", 0)), reverse=True)
            top_1h = [to_ccxt_symbol(t["symbol"]) for t in sorted_1h[:15]]

            final_watchlist = list(set(top_24h + top_1h))
            logger.info(f"Watchlist: {len(final_watchlist)} simbolos validos")

            if not final_watchlist:
                return [to_ccxt_symbol(s) for s in FALLBACK_WATCHLIST]
            return final_watchlist
        except Exception as e:
            logger.error(f"Error watchlist: {e}")
            return [to_ccxt_symbol(s) for s in FALLBACK_WATCHLIST]

    async def initialize(self) -> None:
        logger.info("=" * 60)
        logger.info("UNICO STRATEGY v7.0 - TODO INTEGRADO + AGOTAMIENTO")
        logger.info(f"MODO: {self.mode.value}")
        logger.info("=" * 60)
        self.config["WATCHLIST"] = await self.generate_dynamic_watchlist()
        self.scanner.watchlist = self.config["WATCHLIST"]
        self.rm.set_initial_balance(10000.0)

    async def run(self) -> None:
        await self.initialize()
        self.running = True
        while self.running:
            try:
                await self._cycle()
            except Exception as exc:
                logger.exception(f"Error en ciclo: {exc}")
                await asyncio.sleep(5)
        await self.shutdown()

    async def _cycle(self) -> None:
        self.stats["cycles"] += 1
        capital = await self.rm.update_capital()
        stopped, reason = self.rm.kill_switch.check(capital.total_balance)
        if stopped:
            logger.critical(f"Kill Switch: {reason}")
            self.running = False
            return

        if self.config["SCANNER_ENABLED"] and len(self.rm.positions) < self.config["MAX_POSITIONS"]:
            if self.stats["cycles"] % 15 == 0:
                self.config["WATCHLIST"] = await self.generate_dynamic_watchlist()
                self.scanner.watchlist = self.config["WATCHLIST"]

            signals = await self.scanner.scan_all()
            self.stats["signals"] += len(signals)

            if signals:
                signals.sort(key=lambda s: s.score, reverse=True)
                available_slots = self.config["MAX_POSITIONS"] - len(self.rm.positions)
                for sig in signals[:available_slots]:
                    await self._process_signal(sig)

        if self.rm.positions:
            dfs = {}
            for symbol in list(self.rm.positions.keys()):
                pos = self.rm.positions[symbol]
                tf = getattr(pos, 'timeframe', '1h')
                try:
                    dfs[symbol] = await asyncio.wait_for(
                        self.api.fetch_ohlcv(symbol, timeframe=tf, limit=100),
                        timeout=15.0
                    )
                except asyncio.TimeoutError:
                    continue

            closes = await self.rm.monitor_positions(dfs)
            for symbol, (should_close, reason, notes, partial_pct) in closes.items():
                if not should_close:
                    continue
                pos = self.rm.positions.get(symbol)
                if not pos:
                    continue
                close_side = "sell" if pos.side.value == "LONG" else "buy"
                contracts_to_close = pos.original_contracts * partial_pct if partial_pct < 1.0 else pos.contracts
                result = await self.executor.close_position(
                    symbol=symbol, side=close_side, contracts=contracts_to_close,
                    current_price=pos.current_price, reason=reason
                )
                if result is None or result.get("is_ghost"):
                    await self.rm.close_position(symbol, CloseReason.MANUAL, pos.current_price, 1.0)
                    continue
                await self.rm.close_position(symbol, reason, pos.current_price, partial_pct)
                if reason == CloseReason.TAKE_PROFIT_1:
                    self.stats["tp1_hits"] += 1
                elif reason == CloseReason.TAKE_PROFIT_2:
                    self.stats["tp2_hits"] += 1

        self._log_status(capital)
        await asyncio.sleep(self.config["SCAN_INTERVAL"])

    async def _process_signal(self, signal: Signal) -> None:
        try:
            df = await asyncio.wait_for(
                self.api.fetch_ohlcv(signal.symbol, timeframe=signal.timeframe, limit=100),
                timeout=15.0
            )
        except asyncio.TimeoutError:
            return

        position_size = await self.rm.evaluate_entry(signal=signal, df=df)
        if not position_size:
            return

        open_side = "buy" if signal.signal_type.is_long() else "sell"
        order = await self.executor.open_position(
            symbol=signal.symbol, side=open_side, position_size=position_size,
            stop_loss=position_size.stop_loss, take_profit=position_size.take_profit,
            leverage=position_size.leverage
        )
        if not order:
            logger.error(f"Error abriendo {signal.symbol}")
            return

        atr = 0.0
        if df is not None and len(df) > 14:
            from risk_manager import PositionCalculator
            df_prep = PositionCalculator._prepare_structural_df(df)
            if not df_prep.empty:
                atr = float(df_prep["atr"].iloc[-1])

        self.rm.register_position(
            order_id=order["id"], position_size=position_size,
            pattern_id=signal.pattern_id, signal_type=signal.signal_type.value,
            arrow_color=None, score=signal.score, atr=atr
        )
        self.stats["opened"] += 1
        logger.info(f"POSICION ABIERTA {signal.symbol} {open_side}")

    def _log_status(self, capital: Any) -> None:
        logger.info(
            f"Ciclo {self.stats['cycles']} | Balance: {capital.total_balance:.2f} | "
            f"Pos: {len(self.rm.positions)} | Senales: {self.stats['signals']} | "
            f"TP1: {self.stats['tp1_hits']} | TP2: {self.stats['tp2_hits']}"
        )

    def _handle_shutdown(self, signum: int, frame: Any) -> None:
        self.running = False

    async def shutdown(self) -> None:
        if self.api:
            await self.api.close()


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="UNICO STRATEGY Bot")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--init-db", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if args.init_db:
        with sqlite3.connect(CONFIG['DB_PATH']) as conn:
            seed_patterns.init_db(conn)
            seed_patterns.insert_patterns(conn, seed_patterns.PATTERNS)
            seed_patterns.print_summary(conn)
        return
    if args.status:
        return

    bot = UnicoBot(CONFIG)
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Interrupcion manual.")
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    print("UNICO STRATEGY v7.0 - INICIANDO EN DRY_RUN")
    asyncio.run(main())
