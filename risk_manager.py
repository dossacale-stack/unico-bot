"""
RiskManager — Gestión de Riesgo con Break-Even y TP Escalonado
===============================================================
CAMBIOS vs versión anterior:
- Break-even automático a 3x riesgo
- TP escalonado (25% / 25% / 50% con trailing)
- Trailing por ATR
- Consulta SetupMemory antes de aprobar entrada

FIX v7.1:
- ✅ Sizing: 15% del balance como margen (antes 30%)
- ✅ Pérdida máxima: 40% del margen (antes 15% fijo de SL)
- ✅ SL estructural: usa el stop_loss de la señal, no uno inventado
- ✅ Apalancamiento dinámico: usa el máximo del activo (tope 20x)
- ✅ Fix bug LONG/SHORT: comparación robusta de tipos de señal
- ✅ Limpieza de símbolos con sufijo :USDT antes de llamar a Bybit nativo
"""

import asyncio
import logging
import sqlite3
import time
import json
import os
from datetime import datetime, timezone, time as dtime, timedelta
from dataclasses import dataclass, field
from typing import Optional, Dict, Tuple
from enum import Enum
from collections import defaultdict

import pandas as pd
import numpy as np

logger = logging.getLogger("RiskManager")


class PositionSide(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class CloseReason(Enum):
    STOP_LOSS = "SL"
    TAKE_PROFIT_1 = "TP1"
    TAKE_PROFIT_2 = "TP2"
    TRAILING = "TRAILING"
    BREAK_EVEN = "BREAK_EVEN"
    STRUCTURE_BREAK = "STRUCTURE"
    REVERSE = "REVERSE"
    KILL_SWITCH = "KILL_SWITCH"
    MANUAL = "MANUAL"


class BotMode(Enum):
    DRY_RUN = "DRY_RUN"
    LIVE = "LIVE"
    BACKTEST = "BACKTEST"


@dataclass
class CapitalState:
    total_balance: float = 0.0
    available: float = 0.0
    day_start_balance: float = 0.0
    day_pnl: float = 0.0
    day_pnl_pct: float = 0.0
    open_positions: int = 0
    kill_switch_active: bool = False
    kill_switch_until: str = ""


@dataclass
class PositionSize:
    symbol: str
    side: PositionSide
    position_usd: float      # margen comprometido
    risk_usd: float          # pérdida máxima esperada
    entry_price: float
    stop_loss: float
    take_profit: float
    contracts: float
    leverage: int
    sl_distance_pct: float
    max_loss_usd: float
    is_st_asset: bool = False
    timeframe: str = "1h"


@dataclass
class OpenPosition:
    id: str
    symbol: str
    side: PositionSide
    entry_price: float
    current_price: float
    stop_loss: float
    take_profit: float
    contracts: float
    original_contracts: float
    leverage: int
    position_usd: float
    risk_usd: float
    pattern_id: Optional[int] = None
    timeframe: str = "1h"
    open_time: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    is_st_asset: bool = False
    score: float = 0.0
    signal_type: Optional[str] = None
    arrow_color: Optional[str] = None
    tp1_hit: bool = False
    tp2_hit: bool = False
    break_even_set: bool = False
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    atr_at_entry: float = 0.0


class KillSwitch:
    DAILY_DRAWDOWN_LIMIT = 15.0
    WEEKLY_DRAWDOWN_LIMIT = 25.0

    def __init__(self):
        self.active = False
        self.reason = ""
        self.day_start_balance = 0.0
        self.week_start_balance = 0.0
        self._reset_time: Optional[datetime] = None

    def set_day_start(self, balance: float):
        self.day_start_balance = balance
        now = datetime.now(timezone.utc)
        if now.weekday() == 0:
            self.week_start_balance = balance
        elif self.week_start_balance == 0:
            self.week_start_balance = balance

    def check(self, current_balance: float) -> Tuple[bool, str]:
        if self.active and self._reset_time:
            now = datetime.now(timezone.utc)
            if now >= self._reset_time:
                self.active = False
                self.reason = ""
                logger.info("[KillSwitch] Reset automático.")

        if self.active:
            return True, self.reason

        if self.day_start_balance > 0:
            daily_dd = ((self.day_start_balance - current_balance) / self.day_start_balance * 100)
            if daily_dd >= self.DAILY_DRAWDOWN_LIMIT:
                self._activate(f"Drawdown diario {daily_dd:.1f}% >= {self.DAILY_DRAWDOWN_LIMIT}%")
                return True, self.reason

        if self.week_start_balance > 0:
            weekly_dd = ((self.week_start_balance - current_balance) / self.week_start_balance * 100)
            if weekly_dd >= self.WEEKLY_DRAWDOWN_LIMIT:
                self._activate(f"Drawdown semanal {weekly_dd:.1f}% >= {self.WEEKLY_DRAWDOWN_LIMIT}%", days=2)
                return True, self.reason

        return False, ""

    def _activate(self, reason: str, days: int = 0):
        self.active = True
        self.reason = reason
        now = datetime.now(timezone.utc)
        if days > 0:
            self._reset_time = now + timedelta(days=days)
        else:
            self._reset_time = datetime.combine(now.date() + timedelta(days=1), dtime(0, 0, 0), tzinfo=timezone.utc)
        logger.critical(f"[KillSwitch] 🛑 ACTIVADO: {reason} | Reset: {self._reset_time.isoformat()}")


class PositionCalculator:
    """
    ✅ FIX v7.1: Nueva lógica de sizing.

    Parámetros:
    - Margen por operación: 15% del balance.
    - Pérdida máxima: 40% del margen comprometido.
    - Apalancamiento: dinámico según el activo, tope 20x.
    - SL: se usa el stop_loss estructural que viene de la señal.
    - Si el stop_loss está más lejos que el riesgo permitido, se reduce el tamaño.
    """
    MARGIN_PCT = 0.15
    MAX_LOSS_PCT_OF_MARGIN = 0.40
    LEVERAGE_CAP = 20
    MIN_LEVERAGE = 3
    ST_REDUCTION = 0.50

    TP1_MULTIPLE = 2.0
    TP2_MULTIPLE = 5.0
    BREAK_EVEN_AT_R = 3.0

    TIMEFRAME_CONFIG = {
        "15m": {"margin_pct": 0.15, "leverage": 10},
        "3m":  {"margin_pct": 0.12, "leverage": 10},
        "1m":  {"margin_pct": 0.10, "leverage": 10},
        "1h":  {"margin_pct": 0.15, "leverage": 10},
    }

    @classmethod
    def get_config(cls, timeframe: str):
        return cls.TIMEFRAME_CONFIG.get(timeframe, cls.TIMEFRAME_CONFIG["1h"])

    @classmethod
    def calculate(
        cls,
        symbol: str,
        side: PositionSide,
        entry_price: float,
        stop_loss_price: float,
        balance: float,
        atr: float = 0.0,
        max_leverage: Optional[int] = None,
        is_st_asset: bool = False,
        timeframe: str = "1h",
    ) -> Optional[PositionSize]:
        config = cls.get_config(timeframe)
        margin_pct = config["margin_pct"]

        if is_st_asset:
            margin_pct *= cls.ST_REDUCTION

        margin_usd = balance * margin_pct
        max_loss_usd = margin_usd * cls.MAX_LOSS_PCT_OF_MARGIN

        # Distancia al SL (estructural)
        if entry_price <= 0 or stop_loss_price <= 0:
            logger.warning(f"[PositionCalc] {symbol}: entry o SL inválidos")
            return None

        sl_distance_pct = abs(entry_price - stop_loss_price) / entry_price
        if sl_distance_pct < 0.001:  # SL demasiado cerca (<0.1%)
            logger.warning(f"[PositionCalc] {symbol}: SL demasiado cerca ({sl_distance_pct*100:.3f}%)")
            return None

        # Apalancamiento dinámico
        leverage = cls.LEVERAGE_CAP
        if max_leverage is not None and max_leverage > 0:
            leverage = min(max_leverage, cls.LEVERAGE_CAP)
        leverage = max(leverage, cls.MIN_LEVERAGE)

        # Notional máximo permitido por riesgo y por margen
        notional_from_risk = max_loss_usd / sl_distance_pct
        notional_from_margin = margin_usd * leverage
        notional = min(notional_from_risk, notional_from_margin)

        contracts = notional / entry_price
        risk_usd = notional * sl_distance_pct
        effective_leverage = notional / margin_usd if margin_usd > 0 else 0

        # TP basados en múltiplos de la distancia al SL (risk distance)
        risk_distance = abs(entry_price - stop_loss_price)
        if side == PositionSide.LONG:
            tp1 = entry_price + risk_distance * cls.TP1_MULTIPLE
            tp2 = entry_price + risk_distance * cls.TP2_MULTIPLE
        else:
            tp1 = entry_price - risk_distance * cls.TP1_MULTIPLE
            tp2 = entry_price - risk_distance * cls.TP2_MULTIPLE

        logger.info(
            f"[PositionCalc] {symbol} {side.value} {timeframe} | "
            f"Margen: {margin_usd:.2f} | Notional: {notional:.2f} | "
            f"Riesgo: {risk_usd:.2f} ({sl_distance_pct*100:.2f}%) | "
            f"SL: {stop_loss_price:.6f} | TP1: {tp1:.6f} | TP2: {tp2:.6f} | "
            f"Lev: {effective_leverage:.1f}x (cap {leverage}x) | "
            f"Contratos: {contracts:.4f}"
        )

        return PositionSize(
            symbol=symbol,
            side=side,
            position_usd=round(margin_usd, 2),
            risk_usd=round(risk_usd, 2),
            entry_price=entry_price,
            stop_loss=stop_loss_price,
            take_profit=tp2,
            contracts=round(contracts, 6),
            leverage=leverage,
            sl_distance_pct=round(sl_distance_pct * 100, 3),
            max_loss_usd=round(max_loss_usd, 2),
            is_st_asset=is_st_asset,
            timeframe=timeframe,
        )

    @staticmethod
    def _prepare_structural_df(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
        df["ema55"] = df["close"].ewm(span=55, adjust=False).mean()
        df["ema144"] = df["close"].ewm(span=144, adjust=False).mean()
        df["tr"] = np.maximum(df["high"] - df["low"],
                              np.maximum(abs(df["high"] - df["close"].shift(1)),
                                         abs(df["low"] - df["close"].shift(1))))
        df["atr"] = df["tr"].rolling(14).mean()
        df["vol_avg"] = df["volume"].rolling(20).mean()
        df["vol_ratio"] = df["volume"] / df["vol_avg"].replace(0, 1e-9)
        return df.dropna()


class StructureExitEngine:
    TRIPLE_LOOKBACK = 20

    @classmethod
    def evaluate_exit(cls, position: OpenPosition, df: pd.DataFrame, df_1h: Optional[pd.DataFrame] = None) -> Tuple[bool, CloseReason, str]:
        if position.side == PositionSide.LONG:
            if cls._detect_triple_top(df):
                return True, CloseReason.REVERSE, "Triple techo detectado"
        else:
            if cls._detect_triple_bottom(df):
                return True, CloseReason.REVERSE, "Triple suelo detectado"
        return False, CloseReason.MANUAL, ""

    @classmethod
    def _detect_triple_top(cls, df: pd.DataFrame) -> bool:
        recent = df.tail(cls.TRIPLE_LOOKBACK)
        highs = recent["high"].values
        last = df.iloc[-1]
        peaks = []
        for i in range(1, len(highs) - 1):
            if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
                peaks.append(highs[i])
        if len(peaks) < 2:
            return False
        max_peak = max(peaks)
        similar = [p for p in peaks if abs(p - max_peak) / max_peak < 0.03]
        if len(similar) < 2:
            return False
        last_high = float(last["high"])
        last_close = float(last["close"])
        fake_break = last_high > max_peak and last_close < max_peak
        vol_ratio = float(last["vol_ratio"]) if "vol_ratio" in last else 1.0
        return fake_break and vol_ratio >= 1.5

    @classmethod
    def _detect_triple_bottom(cls, df: pd.DataFrame) -> bool:
        recent = df.tail(cls.TRIPLE_LOOKBACK)
        lows = recent["low"].values
        last = df.iloc[-1]
        valleys = []
        for i in range(1, len(lows) - 1):
            if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
                valleys.append(lows[i])
        if len(valleys) < 2:
            return False
        min_valley = min(valleys)
        similar = [v for v in valleys if abs(v - min_valley) / min_valley < 0.03]
        if len(similar) < 2:
            return False
        last_low = float(last["low"])
        last_close = float(last["close"])
        fake_break = last_low < min_valley and last_close > min_valley
        vol_ratio = float(last["vol_ratio"]) if "vol_ratio" in last else 1.0
        return fake_break and vol_ratio >= 1.5


class RiskManager:
    def __init__(
        self,
        api_manager,
        mode: BotMode = BotMode.DRY_RUN,
        leverage: int = 10,
        db_path: str = "patterns.db",
        max_positions: int = 3,
        position_pct: float = 0.15,   # ✅ FIX: antes 0.30
        sl_pct: float = 0.40,          # ✅ FIX: antes 0.15 (era % fijo, ahora % del margen)
        tp_multiple: float = 5.0,
        st_reduction: float = 0.50,
        cooldown_minutes: int = 5,
        max_entries_daily: int = 20,
    ):
        self.api = api_manager
        self.mode = mode
        self.leverage = leverage
        self.db_path = db_path
        self.max_positions = max_positions
        self.position_pct = position_pct
        self.sl_pct = sl_pct
        self.tp_multiple = tp_multiple
        self.st_reduction = st_reduction
        self.cooldown_minutes = cooldown_minutes
        self.max_entries_daily = max_entries_daily
        self.kill_switch = KillSwitch()
        self.positions: Dict[str, OpenPosition] = {}
        self._capital = CapitalState()
        self._symbol_cooldown: Dict[str, float] = {}
        self._symbol_daily_entries: Dict[str, int] = defaultdict(int)
        self._last_entry_date: Dict[str, str] = {}

        try:
            from setup_memory import SetupMemory
            self.setup_memory = SetupMemory(db_path=db_path)
        except ImportError:
            logger.warning("[RiskManager] setup_memory.py no encontrado. Memoria de setups desactivada.")
            self.setup_memory = None

        self.cooldown_file = "cooldowns.json"
        self._load_cooldowns()

        logger.info(
            f"[RiskManager] Iniciado v7.1 | Modo: {mode.value} | "
            f"Max pos: {max_positions} | Margen: {position_pct*100:.0f}% | "
            f"Pérdida máx: {sl_pct*100:.0f}% del margen | "
            f"Cooldown: {cooldown_minutes}min | Lev cap: {PositionCalculator.LEVERAGE_CAP}x"
        )

    def _load_cooldowns(self):
        if os.path.exists(self.cooldown_file):
            try:
                with open(self.cooldown_file, 'r') as f:
                    data = json.load(f)
                    self._symbol_cooldown = {k: float(v) for k, v in data.items()}
            except Exception as e:
                logger.warning(f"[RiskManager] Error cargando cooldowns: {e}")

    def _save_cooldowns(self):
        try:
            with open(self.cooldown_file, 'w') as f:
                json.dump(self._symbol_cooldown, f, indent=2)
        except Exception as e:
            logger.error(f"[RiskManager] Error guardando cooldowns: {e}")

    async def update_capital(self) -> CapitalState:
        if self.mode == BotMode.DRY_RUN:
            return self._capital

        try:
            balance = await self.api.fetch_balance()
            total = balance["total"]

            if self._capital.day_start_balance == 0:
                self._capital.day_start_balance = total
                self.kill_switch.set_day_start(total)

            self._capital.total_balance = total
            self._capital.available = balance["free"]
            self._capital.day_pnl = total - self._capital.day_start_balance
            self._capital.day_pnl_pct = (self._capital.day_pnl / self._capital.day_start_balance * 100) if self._capital.day_start_balance > 0 else 0
            self._capital.open_positions = len(self.positions)
            return self._capital
        except Exception as e:
            logger.error(f"[Capital] Error: {e}")
            return self._capital

    def set_initial_balance(self, balance: float):
        self._capital.total_balance = balance
        self._capital.available = balance
        self._capital.day_start_balance = balance
        self.kill_switch.set_day_start(balance)
        logger.info(f"[Capital] Balance DRY_RUN: {balance:.2f} USDT")

    async def evaluate_entry(self, signal, df: pd.DataFrame) -> Optional[PositionSize]:
        stopped, reason = self.kill_switch.check(self._capital.total_balance)
        if stopped:
            logger.warning(f"[RiskManager] Kill Switch activo: {reason}")
            return None

        if len(self.positions) >= self.max_positions:
            return None

        if signal.symbol in self.positions:
            return None

        if signal.symbol in self._symbol_cooldown:
            elapsed = time.time() - self._symbol_cooldown[signal.symbol]
            if elapsed < self.cooldown_minutes * 60:
                return None
            else:
                del self._symbol_cooldown[signal.symbol]
                self._save_cooldowns()

        today = datetime.now(timezone.utc).date().isoformat()
        if self._last_entry_date.get(signal.symbol) != today:
            self._symbol_daily_entries[signal.symbol] = 0
            self._last_entry_date[signal.symbol] = today

        if self._symbol_daily_entries[signal.symbol] >= self.max_entries_daily:
            return None

        if self.setup_memory and signal.pattern_id:
            exp = self.setup_memory.get_setup_expectancy(
                symbol=signal.symbol,
                pattern_id=signal.pattern_id,
                timeframe=signal.timeframe
            )
            if not exp["permitido"]:
                logger.info(f"[RiskManager] ⛔ Setup rechazado {signal.symbol}: {exp['razon']}")
                return None
            logger.info(f"[RiskManager] ✅ Setup OK {signal.symbol}: {exp['razon']}")

        timeframe = getattr(signal, 'timeframe', '1h')

        # Obtener apalancamiento máximo del activo
        try:
            max_leverage = await self.api.get_max_leverage(signal.symbol)
            is_st = await self.api.is_st_asset(signal.symbol)
        except Exception as e:
            logger.debug(f"[RiskManager] Error detectando leverage/ST para {signal.symbol}: {e}")
            max_leverage = self.leverage
            is_st = False

        # ✅ FIX v7.1: Detección robusta de LONG/SHORT sin depender del enum
        sig_type_str = str(getattr(signal.signal_type, "value", signal.signal_type)).upper()
        is_long = ("LONG" in sig_type_str)
        side = PositionSide.LONG if is_long else PositionSide.SHORT

        # SL estructural de la señal
        stop_loss_price = float(getattr(signal, "stop_loss", 0.0))
        entry_price = float(signal.entry_price)

        if stop_loss_price <= 0:
            logger.warning(f"[RiskManager] {signal.symbol}: señal sin stop_loss válido. Descartada.")
            return None

        # Validar dirección del SL (para LONG, SL < entry; para SHORT, SL > entry)
        if side == PositionSide.LONG and stop_loss_price >= entry_price:
            logger.warning(f"[RiskManager] {signal.symbol}: SL inválido para LONG ({stop_loss_price} >= {entry_price}). Descartada.")
            return None
        if side == PositionSide.SHORT and stop_loss_price <= entry_price:
            logger.warning(f"[RiskManager] {signal.symbol}: SL inválido para SHORT ({stop_loss_price} <= {entry_price}). Descartada.")
            return None

        atr = 0.0
        if df is not None and len(df) > 14:
            df_prep = PositionCalculator._prepare_structural_df(df)
            if not df_prep.empty:
                atr = float(df_prep["atr"].iloc[-1])

        position_size = PositionCalculator.calculate(
            symbol=signal.symbol,
            side=side,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            balance=self._capital.total_balance,
            atr=atr,
            max_leverage=max_leverage,
            is_st_asset=is_st,
            timeframe=timeframe,
        )

        if position_size is None:
            return None

        self._symbol_daily_entries[signal.symbol] += 1
        return position_size

    def register_position(self, order_id: str, position_size: PositionSize,
                          pattern_id: Optional[int] = None,
                          signal_type: Optional[str] = None,
                          arrow_color: Optional[str] = None,
                          score: float = 0.0,
                          atr: float = 0.0) -> OpenPosition:

        # TP1 y TP2 calculados a partir de la distancia real al SL estructural
        risk_distance = abs(position_size.entry_price - position_size.stop_loss)
        if position_size.side == PositionSide.LONG:
            tp1 = position_size.entry_price + (risk_distance * PositionCalculator.TP1_MULTIPLE)
            tp2 = position_size.entry_price + (risk_distance * PositionCalculator.TP2_MULTIPLE)
        else:
            tp1 = position_size.entry_price - (risk_distance * PositionCalculator.TP1_MULTIPLE)
            tp2 = position_size.entry_price - (risk_distance * PositionCalculator.TP2_MULTIPLE)

        pos = OpenPosition(
            id=order_id,
            symbol=position_size.symbol,
            side=position_size.side,
            entry_price=position_size.entry_price,
            current_price=position_size.entry_price,
            stop_loss=position_size.stop_loss,
            take_profit=tp2,
            contracts=position_size.contracts,
            original_contracts=position_size.contracts,
            leverage=position_size.leverage,
            position_usd=position_size.position_usd,
            risk_usd=position_size.risk_usd,
            pattern_id=pattern_id,
            timeframe=position_size.timeframe,
            highest_price=position_size.entry_price,
            lowest_price=position_size.entry_price,
            is_st_asset=position_size.is_st_asset,
            score=score,
            signal_type=signal_type,
            arrow_color=arrow_color,
            tp1_price=tp1,
            tp2_price=tp2,
            atr_at_entry=atr,
        )
        self.positions[position_size.symbol] = pos
        logger.info(f"[RiskManager] 📝 Posición registrada: {pos.symbol} {pos.side.value} | TP1: {tp1:.6f} TP2: {tp2:.6f}")
        return pos

    async def monitor_positions(self, dfs: Dict[str, pd.DataFrame]) -> Dict[str, Tuple[bool, CloseReason, str, float]]:
        results = {}

        for symbol, position in list(self.positions.items()):
            if symbol not in dfs:
                continue

            df = dfs[symbol]
            df = PositionCalculator._prepare_structural_df(df)
            if df.empty or len(df) < 10:
                continue

            last = df.iloc[-1]
            current = float(last["close"])
            position.current_price = current

            if position.side == PositionSide.LONG:
                pnl_pct = (current - position.entry_price) / position.entry_price
                position.highest_price = max(position.highest_price, current)
            else:
                pnl_pct = (position.entry_price - current) / position.entry_price
                position.lowest_price = min(position.lowest_price, current)

            position.unrealized_pnl_pct = pnl_pct * 100
            position.unrealized_pnl = position.position_usd * pnl_pct * position.leverage

            # SL
            if position.side == PositionSide.LONG:
                if current <= position.stop_loss:
                    results[symbol] = (True, CloseReason.STOP_LOSS, f"SL {current:.6f}", 1.0)
                    continue
            else:
                if current >= position.stop_loss:
                    results[symbol] = (True, CloseReason.STOP_LOSS, f"SL {current:.6f}", 1.0)
                    continue

            # TP1 (parcial 25%)
            if not position.tp1_hit:
                if position.side == PositionSide.LONG and current >= position.tp1_price:
                    results[symbol] = (True, CloseReason.TAKE_PROFIT_1, f"TP1 {current:.6f}", 0.25)
                    continue
                elif position.side == PositionSide.SHORT and current <= position.tp1_price:
                    results[symbol] = (True, CloseReason.TAKE_PROFIT_1, f"TP1 {current:.6f}", 0.25)
                    continue

            # Break-even a 3R
            if not position.break_even_set:
                risk_distance = abs(position.entry_price - position.stop_loss)
                if position.side == PositionSide.LONG:
                    trigger = position.entry_price + (risk_distance * PositionCalculator.BREAK_EVEN_AT_R)
                    if current >= trigger:
                        position.stop_loss = position.entry_price * 1.001
                        position.break_even_set = True
                        logger.info(f"[RiskManager] 🔒 Break-even {symbol} @ {position.stop_loss:.6f}")
                else:
                    trigger = position.entry_price - (risk_distance * PositionCalculator.BREAK_EVEN_AT_R)
                    if current <= trigger:
                        position.stop_loss = position.entry_price * 0.999
                        position.break_even_set = True
                        logger.info(f"[RiskManager] 🔒 Break-even {symbol} @ {position.stop_loss:.6f}")

            # TP2 (parcial 25%)
            if position.tp1_hit and not position.tp2_hit:
                if position.side == PositionSide.LONG and current >= position.tp2_price:
                    results[symbol] = (True, CloseReason.TAKE_PROFIT_2, f"TP2 {current:.6f}", 0.25)
                    continue
                elif position.side == PositionSide.SHORT and current <= position.tp2_price:
                    results[symbol] = (True, CloseReason.TAKE_PROFIT_2, f"TP2 {current:.6f}", 0.25)
                    continue

            # Trailing por ATR
            if position.tp1_hit and position.atr_at_entry > 0:
                atr = float(last["atr"]) if "atr" in last and not pd.isna(last["atr"]) else position.atr_at_entry
                callback = atr * 2.5
                if position.side == PositionSide.LONG:
                    new_sl = position.highest_price - callback
                    if new_sl > position.stop_loss:
                        position.stop_loss = new_sl
                else:
                    new_sl = position.lowest_price + callback
                    if new_sl < position.stop_loss:
                        position.stop_loss = new_sl

        return results

    async def close_position(self, symbol: str, reason: CloseReason, current_price: float, partial_pct: float = 1.0) -> Optional[dict]:
        if symbol not in self.positions:
            return None

        pos = self.positions[symbol]

        if pos.side == PositionSide.LONG:
            pnl_pct = (current_price - pos.entry_price) / pos.entry_price
        else:
            pnl_pct = (pos.entry_price - current_price) / pos.entry_price

        # ✅ FIX v7.1: PnL calculado sobre el margen comprometido (no sobre el notional)
        portion_margin = pos.position_usd * partial_pct
        pnl_usd = portion_margin * pnl_pct * pos.leverage

        open_dt = datetime.fromisoformat(pos.open_time)
        now_dt = datetime.now(timezone.utc)
        duration_m = (now_dt - open_dt).total_seconds() / 60

        logger.info(
            f"[RiskManager] {'🟢' if pnl_usd > 0 else '🔴'} "
            f"{'Parcial' if partial_pct < 1.0 else 'Total'} {symbol} {pos.side.value} | "
            f"PnL: {pnl_usd:.2f} USDT ({pnl_pct*100:.2f}%) | Razón: {reason.value}"
        )

        if partial_pct >= 1.0 and self.setup_memory and pos.pattern_id:
            self.setup_memory.update(
                symbol=symbol,
                pattern_id=pos.pattern_id,
                timeframe=pos.timeframe,
                pnl_usd=pnl_usd
            )

        if pos.pattern_id:
            self._update_pattern_result(
                pattern_id=pos.pattern_id,
                exit_price=current_price,
                exit_reason=reason.value,
                pnl_percent=pnl_pct * 100,
                pnl_usd=pnl_usd,
                duration_min=duration_m,
            )

        self._capital.total_balance += pnl_usd
        self._capital.available += portion_margin + pnl_usd

        if partial_pct >= 1.0:
            self._symbol_cooldown[symbol] = time.time()
            self._save_cooldowns()
            del self.positions[symbol]
        else:
            pos.contracts -= (pos.original_contracts * partial_pct)
            pos.position_usd -= portion_margin
            if reason == CloseReason.TAKE_PROFIT_1:
                pos.tp1_hit = True
            elif reason == CloseReason.TAKE_PROFIT_2:
                pos.tp2_hit = True

        return {
            "symbol": symbol,
            "side": pos.side.value,
            "entry": pos.entry_price,
            "exit": current_price,
            "pnl_usd": round(pnl_usd, 2),
            "pnl_pct": round(pnl_pct * 100, 2),
            "reason": reason.value,
            "partial_pct": partial_pct,
            "duration_min": round(duration_m, 0),
        }

    def _update_pattern_result(self, pattern_id: int, exit_price: float, exit_reason: str, pnl_percent: float, pnl_usd: float, duration_min: float):
        trade_result = "WIN" if pnl_usd > 0 else "LOSS"
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    UPDATE patterns SET
                        exit_price=?, exit_reason=?, pnl_percent=?,
                        pnl_usd=?, duration_min=?, trade_result=?
                    WHERE id=?
                """, (exit_price, exit_reason, pnl_percent, pnl_usd, duration_min, trade_result, pattern_id))
                conn.commit()
        except Exception as e:
            logger.error(f"[BD] Error actualizando patrón {pattern_id}: {e}")

    def get_status(self) -> dict:
        return {
            "mode": self.mode.value,
            "balance": self._capital.total_balance,
            "available": self._capital.available,
            "day_pnl": self._capital.day_pnl,
            "kill_switch": self.kill_switch.active,
            "open_positions": len(self.positions),
            "positions": {
                sym: {
                    "side": pos.side.value,
                    "entry": pos.entry_price,
                    "current": pos.current_price,
                    "pnl_usd": pos.unrealized_pnl,
                    "sl": pos.stop_loss,
                    "tp1": pos.tp1_price,
                    "tp2": pos.tp2_price,
                    "tp1_hit": pos.tp1_hit,
                    "tp2_hit": pos.tp2_hit,
                    "break_even": pos.break_even_set,
                }
                for sym, pos in self.positions.items()
            },
        }
