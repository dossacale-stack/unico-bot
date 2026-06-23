"""
RiskManager — Módulo de Gestión de Riesgo y Ejecución
======================================================
ÚNICO STRATEGY — Gestión de riesgo profesional

Lógica de capital:
  - Posición:    10% del saldo total (ajustable)
  - SL:          40% de esa posición (= 4% del saldo) → FIJO
  - TP:          10x el riesgo (= 40% del saldo) → FIJO
  - Apalancamiento: DETECTADO AUTOMÁTICAMENTE por símbolo
  - Activos ST:  Reducción de posición a la mitad
  - Kill Switch: si pierde 15% del saldo del día → parar hasta 00:00
  - Cooldown:    15 minutos entre entradas del mismo símbolo
  - Límite diario: 3 entradas por símbolo al día
  - Estructura:  se mantiene para reversos (triple techo/suelo)
  - Reverso:     cierra contrato y abre en dirección contraria
"""

import asyncio
import logging
import sqlite3
import os
import time
from datetime import datetime, timezone, time as dtime
from dataclasses import dataclass, field
from typing import Optional, Dict, Tuple
from enum import Enum
from collections import defaultdict

import pandas as pd
import numpy as np

logger = logging.getLogger("RiskManager")


# ─────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────
class PositionSide(Enum):
    LONG  = "LONG"
    SHORT = "SHORT"
    NONE  = "NONE"

class CloseReason(Enum):
    STOP_LOSS       = "SL"
    TAKE_PROFIT     = "TP"
    STRUCTURE_BREAK = "STRUCTURE"
    REVERSE         = "REVERSE"
    KILL_SWITCH     = "KILL_SWITCH"
    MANUAL          = "MANUAL"

class BotMode(Enum):
    DRY_RUN  = "DRY_RUN"
    LIVE     = "LIVE"
    BACKTEST = "BACKTEST"


# ─────────────────────────────────────────────
#  DATACLASSES
# ─────────────────────────────────────────────
@dataclass
class CapitalState:
    total_balance:     float = 0.0
    available:         float = 0.0
    day_start_balance: float = 0.0
    day_pnl:           float = 0.0
    day_pnl_pct:       float = 0.0
    open_positions:    int   = 0
    kill_switch_active: bool = False
    kill_switch_until:  str  = ""

    @property
    def drawdown_pct(self) -> float:
        if self.day_start_balance <= 0:
            return 0.0
        return ((self.day_start_balance - self.total_balance)
                / self.day_start_balance * 100)


@dataclass
class PositionSize:
    symbol:           str
    side:             PositionSide
    position_usd:     float
    risk_usd:         float
    entry_price:      float
    stop_loss:        float
    take_profit:      float
    contracts:        float
    leverage:         int
    sl_distance_pct:  float
    max_loss_usd:     float
    is_st_asset:      bool = False


@dataclass
class OpenPosition:
    id:               str
    symbol:           str
    side:             PositionSide
    entry_price:      float
    current_price:    float
    stop_loss:        float
    take_profit:      float
    contracts:        float
    leverage:         int
    position_usd:     float
    risk_usd:         float
    pattern_id:       Optional[int] = None
    open_time:        str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    unrealized_pnl:   float = 0.0
    unrealized_pnl_pct: float = 0.0
    highest_price:    float = 0.0
    lowest_price:     float = 0.0
    is_st_asset:      bool = False
    score:            float = 0.0  # Para aprendizaje
    signal_type:      Optional[str] = None  # Para aprendizaje
    arrow_color:      Optional[str] = None  # Para aprendizaje


# ─────────────────────────────────────────────
#  KILL SWITCH
# ─────────────────────────────────────────────
class KillSwitch:
    DAILY_DRAWDOWN_LIMIT  = 15.0
    WEEKLY_DRAWDOWN_LIMIT = 30.0

    def __init__(self):
        self.active            = False
        self.reason            = ""
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
                logger.info("[KillSwitch] Reset automático a las 00:00 UTC.")

        if self.active:
            return True, self.reason

        if self.day_start_balance > 0:
            daily_dd = ((self.day_start_balance - current_balance)
                        / self.day_start_balance * 100)
            if daily_dd >= self.DAILY_DRAWDOWN_LIMIT:
                self._activate(
                    f"Drawdown diario {daily_dd:.1f}% >= {self.DAILY_DRAWDOWN_LIMIT}%"
                )
                return True, self.reason

        if self.week_start_balance > 0:
            weekly_dd = ((self.week_start_balance - current_balance)
                         / self.week_start_balance * 100)
            if weekly_dd >= self.WEEKLY_DRAWDOWN_LIMIT:
                self._activate(
                    f"Drawdown semanal {weekly_dd:.1f}% >= {self.WEEKLY_DRAWDOWN_LIMIT}%",
                    days=2
                )
                return True, self.reason

        return False, ""

    def _activate(self, reason: str, days: int = 0):
        self.active  = True
        self.reason  = reason
        now          = datetime.now(timezone.utc)

        if days > 0:
            from datetime import timedelta
            self._reset_time = now + timedelta(days=days)
        else:
            from datetime import timedelta
            self._reset_time = datetime.combine(
                now.date() + timedelta(days=1),
                dtime(0, 0, 0),
                tzinfo=timezone.utc
            )

        logger.critical(
            f"[KillSwitch] 🛑 ACTIVADO: {reason} | "
            f"Reset: {self._reset_time.isoformat()}"
        )


# ─────────────────────────────────────────────
#  CALCULADORA DE POSICIÓN
# ─────────────────────────────────────────────
class PositionCalculator:
    POSITION_PCT = 0.10
    SL_MAX_PCT   = 0.40
    TP_MULTIPLE  = 10.0
    ST_REDUCTION = 0.50

    @classmethod
    def calculate(
        cls,
        symbol:          str,
        side:            PositionSide,
        entry_price:     float,
        sl_structural:   float,
        tp_structural:   float,
        balance:         float,
        leverage:        int = 10,
        max_leverage:    Optional[int] = None,
        is_st_asset:     bool = False,
    ) -> PositionSize:
        # Si es ST, reducir posición a la mitad
        position_pct = cls.POSITION_PCT
        if is_st_asset:
            position_pct = cls.POSITION_PCT * cls.ST_REDUCTION
            logger.warning(
                f"[PositionCalc] {symbol} es ST, posición reducida al "
                f"{position_pct*100:.0f}% del capital"
            )
        
        # Si se pasa max_leverage, limitar el apalancamiento
        if max_leverage and leverage > max_leverage:
            logger.warning(
                f"[PositionCalc] Apalancamiento {leverage}x > máximo {max_leverage}x "
                f"para {symbol}. Usando {max_leverage}x."
            )
            leverage = max_leverage
        
        # 1. Posición: % del saldo (ajustado por ST)
        position_usd = balance * position_pct
        
        # 2. Riesgo máximo: 40% de la posición
        max_loss_usd = position_usd * cls.SL_MAX_PCT
        
        # 3. SL en precio (fijo, basado en riesgo)
        sl_distance_pct = cls.SL_MAX_PCT / leverage
        
        if side == PositionSide.LONG:
            stop_loss = entry_price * (1 - sl_distance_pct)
            take_profit = entry_price * (1 + cls.SL_MAX_PCT * cls.TP_MULTIPLE / leverage)
        else:  # SHORT
            stop_loss = entry_price * (1 + sl_distance_pct)
            take_profit = entry_price * (1 - cls.SL_MAX_PCT * cls.TP_MULTIPLE / leverage)
        
        # 4. Contratos
        position_with_leverage = position_usd * leverage
        contracts = position_with_leverage / entry_price
        
        # 5. Riesgo en USD (confirmación)
        risk_usd = position_usd * sl_distance_pct
        
        logger.info(
            f"[PositionCalc] {symbol} {side.value} | "
            f"Posición: ${position_usd:.2f} | "
            f"Riesgo: ${risk_usd:.2f} ({risk_usd/balance*100:.1f}% capital) | "
            f"SL: {stop_loss:.4f} ({sl_distance_pct*100:.1f}%) | "
            f"TP: {take_profit:.4f} | "
            f"Leverage: {leverage}x | "
            f"Contratos: {contracts:.4f}"
        )

        return PositionSize(
            symbol          = symbol,
            side            = side,
            position_usd    = round(position_usd, 2),
            risk_usd        = round(risk_usd, 2),
            entry_price     = entry_price,
            stop_loss       = stop_loss,
            take_profit     = take_profit,
            contracts       = round(contracts, 4),
            leverage        = leverage,
            sl_distance_pct = round(sl_distance_pct * 100, 3),
            max_loss_usd    = round(max_loss_usd, 2),
            is_st_asset     = is_st_asset,
        )

    @staticmethod
    def _prepare_structural_df(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
        df["ema55"] = df["close"].ewm(span=55, adjust=False).mean()
        df["ema144"] = df["close"].ewm(span=144, adjust=False).mean()
        df["ema233"] = df["close"].ewm(span=233, adjust=False).mean()
        df["bb_mid"] = df["close"].rolling(20).mean()
        df["bb_std"] = df["close"].rolling(20).std()
        df["bb_upper"] = df["bb_mid"] + 2 * df["bb_std"]
        df["bb_lower"] = df["bb_mid"] - 2 * df["bb_std"]
        df["vol_avg"] = df["volume"].rolling(20).mean()
        df["vol_ratio"] = df["volume"] / df["vol_avg"].replace(0, 1e-9)
        return df.dropna()


# ─────────────────────────────────────────────
#  MOTOR DE CIERRE POR ESTRUCTURA
# ─────────────────────────────────────────────
class StructureExitEngine:
    TRIPLE_LOOKBACK = 20

    @classmethod
    def evaluate_exit(
        cls,
        position: OpenPosition,
        df: pd.DataFrame,
    ) -> Tuple[bool, CloseReason, str]:
        last = df.iloc[-1]
        prev = df.iloc[-2]

        ema21 = float(last["ema21"])
        ema55 = float(last["ema55"])
        ema21_prev = float(prev["ema21"])
        ema55_prev = float(prev["ema55"])

        if position.side == PositionSide.LONG:
            cross_down = (ema21 < ema55) and (ema21_prev >= ema55_prev)
            if cross_down:
                is_trap = cls._detect_triple_top(df)
                if is_trap:
                    return True, CloseReason.REVERSE, (
                        "Triple techo detectado — revertir a SHORT"
                    )

        else:  # SHORT
            cross_up = (ema21 > ema55) and (ema21_prev <= ema55_prev)
            if cross_up:
                is_trap = cls._detect_triple_bottom(df)
                if is_trap:
                    return True, CloseReason.REVERSE, (
                        "Triple suelo detectado — revertir a LONG"
                    )

        return False, CloseReason.MANUAL, ""

    @classmethod
    def _detect_triple_top(cls, df: pd.DataFrame) -> bool:
        recent = df.tail(cls.TRIPLE_LOOKBACK)
        highs  = recent["high"].values
        last   = df.iloc[-1]

        peaks = []
        for i in range(1, len(highs) - 1):
            if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
                peaks.append(highs[i])

        if len(peaks) < 2:
            return False

        max_peak = max(peaks)
        similar  = [p for p in peaks if abs(p - max_peak) / max_peak < 0.03]

        if len(similar) < 2:
            return False

        last_high  = float(last["high"])
        last_close = float(last["close"])
        fake_break = last_high > max_peak and last_close < max_peak

        vol_ratio = float(last["vol_ratio"]) if "vol_ratio" in last else 1.0
        high_vol  = vol_ratio >= 1.5

        return fake_break and high_vol

    @classmethod
    def _detect_triple_bottom(cls, df: pd.DataFrame) -> bool:
        recent = df.tail(cls.TRIPLE_LOOKBACK)
        lows   = recent["low"].values
        last   = df.iloc[-1]

        valleys = []
        for i in range(1, len(lows) - 1):
            if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
                valleys.append(lows[i])

        if len(valleys) < 2:
            return False

        min_valley = min(valleys)
        similar    = [v for v in valleys
                      if abs(v - min_valley) / min_valley < 0.03]

        if len(similar) < 2:
            return False

        last_low   = float(last["low"])
        last_close = float(last["close"])
        fake_break = last_low < min_valley and last_close > min_valley

        vol_ratio = float(last["vol_ratio"]) if "vol_ratio" in last else 1.0
        high_vol  = vol_ratio >= 1.5

        return fake_break and high_vol


# ─────────────────────────────────────────────
#  RISK MANAGER PRINCIPAL (MEJORADO)
# ─────────────────────────────────────────────
class RiskManager:
    def __init__(
        self,
        api_manager,
        mode:          BotMode = BotMode.DRY_RUN,
        leverage:      int     = 10,
        db_path:       str     = "patterns.db",
        max_positions: int     = 3,
        position_pct:  float   = 0.10,
        sl_pct:        float   = 0.40,
        tp_multiple:   float   = 10.0,
        st_reduction:  float   = 0.50,
        cooldown_minutes: int = 15,      # NUEVO: cooldown entre entradas del mismo símbolo
        max_entries_daily: int = 3,      # NUEVO: límite diario por símbolo
    ):
        self.api           = api_manager
        self.mode          = mode
        self.leverage      = leverage
        self.db_path       = db_path
        self.max_positions = max_positions
        self.position_pct  = position_pct
        self.sl_pct        = sl_pct
        self.tp_multiple   = tp_multiple
        self.st_reduction  = st_reduction
        self.cooldown_minutes = cooldown_minutes
        self.max_entries_daily = max_entries_daily
        self.kill_switch   = KillSwitch()
        self.positions:    Dict[str, OpenPosition] = {}
        self._capital:     CapitalState = CapitalState()

        # NUEVO: Cooldown por símbolo (post-cierre)
        self._symbol_cooldown: Dict[str, float] = {}
        # NUEVO: Límite diario por símbolo
        self._symbol_daily_entries: Dict[str, int] = defaultdict(int)
        self._last_entry_date: Dict[str, str] = {}

        # Actualizar constantes del PositionCalculator
        PositionCalculator.POSITION_PCT = position_pct
        PositionCalculator.SL_MAX_PCT = sl_pct
        PositionCalculator.TP_MULTIPLE = tp_multiple
        PositionCalculator.ST_REDUCTION = st_reduction

        logger.info(
            f"[RiskManager] Iniciado | "
            f"Modo: {mode.value} | "
            f"Leverage: {leverage}x | "
            f"Max posiciones: {max_positions} | "
            f"Posición: {position_pct*100:.0f}% | "
            f"SL: {sl_pct*100:.0f}% de posición ({sl_pct*position_pct*100:.1f}% capital) | "
            f"TP: {tp_multiple}x riesgo | "
            f"ST reducción: {st_reduction*100:.0f}% | "
            f"Cooldown: {cooldown_minutes}min | "
            f"Máx entradas/día: {max_entries_daily}"
        )

    # ──────────────────────────────────────────
    #  ACTUALIZAR CAPITAL
    # ──────────────────────────────────────────
    async def update_capital(self) -> CapitalState:
        if self.mode == BotMode.DRY_RUN:
            return self._capital

        try:
            balance = await self.api.fetch_balance()
            total   = balance["total"]

            if self._capital.day_start_balance == 0:
                self._capital.day_start_balance = total
                self.kill_switch.set_day_start(total)
                logger.info(f"[Capital] Saldo inicial del día: ${total:.2f}")

            self._capital.total_balance = total
            self._capital.available     = balance["free"]
            self._capital.day_pnl       = total - self._capital.day_start_balance
            self._capital.day_pnl_pct   = (
                self._capital.day_pnl / self._capital.day_start_balance * 100
                if self._capital.day_start_balance > 0 else 0
            )
            self._capital.open_positions = len(self.positions)

            return self._capital

        except Exception as e:
            logger.error(f"[Capital] Error actualizando balance: {e}")
            return self._capital

    def set_initial_balance(self, balance: float):
        self._capital.total_balance     = balance
        self._capital.available         = balance
        self._capital.day_start_balance = balance
        self.kill_switch.set_day_start(balance)
        logger.info(f"[Capital] Balance DRY_RUN: ${balance:.2f}")

    # ──────────────────────────────────────────
    #  EVALUAR NUEVA ENTRADA (MEJORADO)
    # ──────────────────────────────────────────
    async def evaluate_entry(
        self,
        signal,
        df: pd.DataFrame,
        sl_structural: float,
        tp_structural: float,
    ) -> Optional[PositionSize]:
        # ── Kill Switch ──
        stopped, reason = self.kill_switch.check(
            self._capital.total_balance
        )
        if stopped:
            logger.warning(f"[RiskManager] Kill Switch activo: {reason}")
            return None

        # ── Máximo de posiciones ──
        if len(self.positions) >= self.max_positions:
            logger.debug(
                f"[RiskManager] Máximo de posiciones alcanzado "
                f"({self.max_positions})"
            )
            return None

        # ── Ya hay posición en este símbolo ──
        if signal.symbol in self.positions:
            logger.debug(
                f"[RiskManager] Ya hay posición abierta en {signal.symbol}"
            )
            return None

        # ── NUEVO: Verificar cooldown post-cierre ──
        if signal.symbol in self._symbol_cooldown:
            elapsed = time.time() - self._symbol_cooldown[signal.symbol]
            cooldown_seconds = self.cooldown_minutes * 60
            if elapsed < cooldown_seconds:
                remaining = (cooldown_seconds - elapsed) / 60
                logger.debug(
                    f"[RiskManager] {signal.symbol} en cooldown. "
                    f"Restan {remaining:.1f} min para nueva entrada."
                )
                return None
            else:
                # Limpiar cooldown expirado
                del self._symbol_cooldown[signal.symbol]

        # ── NUEVO: Verificar límite diario por símbolo ──
        today = datetime.now(timezone.utc).date().isoformat()
        if self._last_entry_date.get(signal.symbol) != today:
            self._symbol_daily_entries[signal.symbol] = 0
            self._last_entry_date[signal.symbol] = today

        if self._symbol_daily_entries[signal.symbol] >= self.max_entries_daily:
            logger.warning(
                f"[RiskManager] {signal.symbol} alcanzó el límite diario de "
                f"{self.max_entries_daily} entradas. Hoy no se opera más."
            )
            return None

        # ── Detectar apalancamiento máximo del mercado ──
        try:
            max_leverage = await self.api.get_max_leverage(signal.symbol)
            is_st = await self.api.is_st_asset(signal.symbol)
        except Exception as e:
            logger.warning(f"[RiskManager] Error detectando apalancamiento para {signal.symbol}: {e}")
            max_leverage = self.leverage
            is_st = False

        # ── Calcular apalancamiento efectivo ──
        effective_leverage = min(self.leverage, max_leverage)
        
        if effective_leverage < self.leverage:
            logger.info(
                f"[RiskManager] {signal.symbol} apalancamiento limitado: "
                f"{effective_leverage}x (máx mercado: {max_leverage}x)"
            )

        # ── Calcular posición ──
        from market_scanner import SignalType
        side = (PositionSide.LONG
                if signal.signal_type in (
                    SignalType.LONG_BREAKOUT,
                    SignalType.LONG_REVERSAL
                )
                else PositionSide.SHORT)

        position_size = PositionCalculator.calculate(
            symbol        = signal.symbol,
            side          = side,
            entry_price   = signal.entry_price,
            sl_structural = 0.0,
            tp_structural = 0.0,
            balance       = self._capital.total_balance,
            leverage      = effective_leverage,
            max_leverage  = max_leverage,
            is_st_asset   = is_st,
        )

        # Registrar que vamos a usar una entrada diaria
        self._symbol_daily_entries[signal.symbol] += 1

        logger.info(
            f"[RiskManager] ✅ Entrada aprobada: "
            f"{signal.symbol} {side.value} | "
            f"${position_size.position_usd:.2f} | "
            f"Riesgo: ${position_size.risk_usd:.2f} | "
            f"SL: {position_size.stop_loss:.4f} | "
            f"TP: {position_size.take_profit:.4f} | "
            f"Entradas hoy: {self._symbol_daily_entries[signal.symbol]}/{self.max_entries_daily}"
        )

        return position_size

    # ──────────────────────────────────────────
    #  REGISTRAR POSICIÓN
    # ──────────────────────────────────────────
    def register_position(
        self,
        order_id:      str,
        position_size: PositionSize,
        pattern_id:    Optional[int] = None,
        signal_type:   Optional[str] = None,
        arrow_color:   Optional[str] = None,
        score:         float = 0.0,
    ) -> OpenPosition:
        pos = OpenPosition(
            id           = order_id,
            symbol       = position_size.symbol,
            side         = position_size.side,
            entry_price  = position_size.entry_price,
            current_price= position_size.entry_price,
            stop_loss    = position_size.stop_loss,
            take_profit  = position_size.take_profit,
            contracts    = position_size.contracts,
            leverage     = position_size.leverage,
            position_usd = position_size.position_usd,
            risk_usd     = position_size.risk_usd,
            pattern_id   = pattern_id,
            highest_price= position_size.entry_price,
            lowest_price = position_size.entry_price,
            is_st_asset  = position_size.is_st_asset,
            score        = score,
            signal_type  = signal_type,
            arrow_color  = arrow_color,
        )
        self.positions[position_size.symbol] = pos
        logger.info(
            f"[RiskManager] 📝 Posición registrada: "
            f"{pos.symbol} {pos.side.value} @ {pos.entry_price}"
            f"{' (ST)' if pos.is_st_asset else ''}"
        )
        return pos

    # ──────────────────────────────────────────
    #  MONITOREAR POSICIONES
    # ──────────────────────────────────────────
    async def monitor_positions(
        self,
        dfs: Dict[str, pd.DataFrame],
    ) -> Dict[str, Tuple[bool, CloseReason, str]]:
        results = {}

        for symbol, position in list(self.positions.items()):
            if symbol not in dfs:
                continue

            df   = dfs[symbol]
            df   = PositionCalculator._prepare_structural_df(df)
            if df.empty or len(df) < 10:
                logger.warning(f"[Monitor] Datos insuficientes para {symbol}, salto.")
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

            position.unrealized_pnl_pct = pnl_pct * 100 * position.leverage
            position.unrealized_pnl     = (
                position.position_usd * pnl_pct * position.leverage
            )

            # ── 1. Evaluar SL y TP fijos ──
            if position.side == PositionSide.LONG:
                if current <= position.stop_loss:
                    results[symbol] = (True, CloseReason.STOP_LOSS, 
                                       f"SL fijo tocado {current:.4f}")
                    continue
                if current >= position.take_profit:
                    results[symbol] = (True, CloseReason.TAKE_PROFIT, 
                                       f"TP fijo tocado {current:.4f}")
                    continue
            else:  # SHORT
                if current >= position.stop_loss:
                    results[symbol] = (True, CloseReason.STOP_LOSS, 
                                       f"SL fijo tocado {current:.4f}")
                    continue
                if current <= position.take_profit:
                    results[symbol] = (True, CloseReason.TAKE_PROFIT, 
                                       f"TP fijo tocado {current:.4f}")
                    continue

            # ── 2. Evaluar reverso por estructura ──
            should_close, reason, notes = StructureExitEngine.evaluate_exit(
                position, df
            )

            if should_close:
                results[symbol] = (True, reason, notes)
                logger.info(
                    f"[Monitor] 🔔 {symbol} — cerrar por {reason.value}: {notes}"
                )

        return results

    # ──────────────────────────────────────────
    #  CERRAR POSICIÓN (MEJORADO)
    # ──────────────────────────────────────────
    async def close_position(
        self,
        symbol:       str,
        reason:       CloseReason,
        current_price: float,
    ) -> Optional[dict]:
        if symbol not in self.positions:
            return None

        pos = self.positions[symbol]

        if pos.side == PositionSide.LONG:
            pnl_pct = (current_price - pos.entry_price) / pos.entry_price
        else:
            pnl_pct = (pos.entry_price - current_price) / pos.entry_price

        pnl_usd = pos.position_usd * pnl_pct * pos.leverage

        open_dt    = datetime.fromisoformat(pos.open_time)
        now_dt     = datetime.now(timezone.utc)
        duration_m = (now_dt - open_dt).total_seconds() / 60

        logger.info(
            f"[RiskManager] {'🟢' if pnl_usd > 0 else '🔴'} "
            f"Cerrando {symbol} {pos.side.value} | "
            f"PnL: ${pnl_usd:.2f} ({pnl_pct*100:.1f}%) | "
            f"Razón: {reason.value} | "
            f"Duración: {duration_m:.0f}min"
        )

        # ── NUEVO: Registrar cooldown post-cierre ──
        self._symbol_cooldown[symbol] = time.time()
        logger.info(
            f"[RiskManager] {symbol} en cooldown por {self.cooldown_minutes} minutos "
            f"(cerrado por {reason.value})"
        )

        if pos.pattern_id:
            self._update_pattern_result(
                pattern_id   = pos.pattern_id,
                exit_price   = current_price,
                exit_reason  = reason.value,
                pnl_percent  = pnl_pct * 100,
                pnl_usd      = pnl_usd,
                duration_min = duration_m,
            )

        self._capital.total_balance += pnl_usd
        self._capital.available     += pos.position_usd + pnl_usd
        self._capital.day_pnl       += pnl_usd

        del self.positions[symbol]

        result = {
            "symbol":       symbol,
            "side":         pos.side.value,
            "entry":        pos.entry_price,
            "exit":         current_price,
            "pnl_usd":      round(pnl_usd, 2),
            "pnl_pct":      round(pnl_pct * 100, 2),
            "reason":       reason.value,
            "duration_min": round(duration_m, 0),
            "reverse":      reason == CloseReason.REVERSE,
            "pattern_id":   pos.pattern_id,
            "score":        pos.score,
            "signal_type":  pos.signal_type,
            "arrow_color":  pos.arrow_color,
        }

        if reason == CloseReason.REVERSE:
            reverse_side = (
                PositionSide.SHORT
                if pos.side == PositionSide.LONG
                else PositionSide.LONG
            )
            result["reverse_side"]  = reverse_side.value
            result["reverse_price"] = current_price
            logger.info(
                f"[RiskManager] 🔄 REVERSO: "
                f"abrir {reverse_side.value} en {symbol} @ {current_price}"
            )

        return result

    # ──────────────────────────────────────────
    #  ACTUALIZAR BD
    # ──────────────────────────────────────────
    def _update_pattern_result(
        self,
        pattern_id:   int,
        exit_price:   float,
        exit_reason:  str,
        pnl_percent:  float,
        pnl_usd:      float,
        duration_min: float,
    ):
        trade_result = "WIN" if pnl_usd > 0 else "LOSS"
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    UPDATE patterns SET
                        exit_price   = ?,
                        exit_reason  = ?,
                        pnl_percent  = ?,
                        pnl_usd      = ?,
                        duration_min = ?,
                        trade_result = ?
                    WHERE id = ?
                """, (
                    exit_price, exit_reason, pnl_percent,
                    pnl_usd, duration_min, trade_result, pattern_id
                ))
                conn.commit()
        except Exception as e:
            logger.error(f"[BD] Error actualizando patrón {pattern_id}: {e}")

    # ──────────────────────────────────────────
    #  ESTADO DEL SISTEMA
    # ──────────────────────────────────────────
    def get_status(self) -> dict:
        return {
            "mode":              self.mode.value,
            "balance":           self._capital.total_balance,
            "available":         self._capital.available,
            "day_start":         self._capital.day_start_balance,
            "day_pnl":           self._capital.day_pnl,
            "day_pnl_pct":       self._capital.day_pnl_pct,
            "drawdown_pct":      self._capital.drawdown_pct,
            "kill_switch":       self.kill_switch.active,
            "kill_switch_reason":self.kill_switch.reason,
            "open_positions":    len(self.positions),
            "cooldown_symbols":  list(self._symbol_cooldown.keys()),
            "daily_entries":     dict(self._symbol_daily_entries),
            "positions": {
                sym: {
                    "side":        pos.side.value,
                    "entry":       pos.entry_price,
                    "current":     pos.current_price,
                    "pnl_usd":     pos.unrealized_pnl,
                    "pnl_pct":     pos.unrealized_pnl_pct,
                    "sl":          pos.stop_loss,
                    "tp":          pos.take_profit,
                    "is_st":       pos.is_st_asset,
                }
                for sym, pos in self.positions.items()
            },
        }


async def main():
    print("\n🔥 RiskManager v3.0 — Con cooldown y límite diario")
    print("─" * 60)
    print("  🛡️  Cooldown: 15 minutos entre entradas del mismo símbolo")
    print("  📊  Límite diario: 3 entradas por símbolo")
    print("  🔒  SL fijo: 4% del capital")
    print("  🚀  TP fijo: 40% del capital")
    print("─" * 60)

if __name__ == "__main__":
    asyncio.run(main())
