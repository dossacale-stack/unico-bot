"""
RiskManager — Módulo de Gestión de Riesgo y Ejecución
======================================================
ÚNICO STRATEGY — Gestión de riesgo profesional

Lógica de capital (MODIFICADA):
  - Posición:    10% del saldo total
  - SL:          40% de esa posición (= 4% del saldo) → NUEVO: se usa SIEMPRE
  - TP:          10x el riesgo (= 40% del saldo) → NUEVO: fijo
  - Kill Switch: si pierde 15% del saldo del día → parar hasta 00:00
  - Estructura:  se mantiene para reversos (triple techo/suelo)
  - Reverso:     cierra contrato y abre en dirección contraria

El SL y TP ya no se calculan por estructura (EMA/swing),
sino que se usan los valores fijos basados en el riesgo.
"""

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone, time as dtime
from dataclasses import dataclass, field
from typing import Optional, Dict, Tuple
from enum import Enum

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
    STRUCTURE_BREAK = "STRUCTURE"   # EMA cruzó en contra (solo para reversos)
    REVERSE         = "REVERSE"     # Trampa detectada — revertir
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
    """Estado del capital en tiempo real."""
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
    """Tamaño de posición calculado."""
    symbol:           str
    side:             PositionSide
    position_usd:     float   # 10% del saldo
    risk_usd:         float   # 40% de position_usd = 4% saldo
    entry_price:      float
    stop_loss:        float
    take_profit:      float
    contracts:        float   # cantidad de contratos
    leverage:         int
    sl_distance_pct:  float
    max_loss_usd:     float


@dataclass
class OpenPosition:
    """Posición abierta en seguimiento."""
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
    highest_price:    float = 0.0   # para trailing
    lowest_price:     float = 0.0


# ─────────────────────────────────────────────
#  KILL SWITCH
# ─────────────────────────────────────────────
class KillSwitch:
    """
    Para el bot si el drawdown diario supera el 15%.
    Bloquea hasta las 00:00 UTC del día siguiente.
    También detecta drawdown semanal como segundo nivel.
    """

    DAILY_DRAWDOWN_LIMIT  = 15.0   # % del saldo del día
    WEEKLY_DRAWDOWN_LIMIT = 30.0   # % del saldo del lunes

    def __init__(self):
        self.active            = False
        self.reason            = ""
        self.day_start_balance = 0.0
        self.week_start_balance = 0.0
        self._reset_time: Optional[datetime] = None

    def set_day_start(self, balance: float):
        self.day_start_balance = balance
        now = datetime.now(timezone.utc)
        # Resetear saldo semanal los lunes
        if now.weekday() == 0:
            self.week_start_balance = balance
        elif self.week_start_balance == 0:
            self.week_start_balance = balance

    def check(self, current_balance: float) -> Tuple[bool, str]:
        """
        Retorna (debe_parar, razón).
        Llama esto antes de cada nueva operación.
        """
        # Verificar si ya pasó el reset
        if self.active and self._reset_time:
            now = datetime.now(timezone.utc)
            if now >= self._reset_time:
                self.active = False
                self.reason = ""
                logger.info("[KillSwitch] Reset automático a las 00:00 UTC.")

        if self.active:
            return True, self.reason

        # Drawdown diario
        if self.day_start_balance > 0:
            daily_dd = ((self.day_start_balance - current_balance)
                        / self.day_start_balance * 100)
            if daily_dd >= self.DAILY_DRAWDOWN_LIMIT:
                self._activate(
                    f"Drawdown diario {daily_dd:.1f}% >= {self.DAILY_DRAWDOWN_LIMIT}%"
                )
                return True, self.reason

        # Drawdown semanal
        if self.week_start_balance > 0:
            weekly_dd = ((self.week_start_balance - current_balance)
                         / self.week_start_balance * 100)
            if weekly_dd >= self.WEEKLY_DRAWDOWN_LIMIT:
                self._activate(
                    f"Drawdown semanal {weekly_dd:.1f}% >= {self.WEEKLY_DRAWDOWN_LIMIT}%",
                    days=2  # Pausa 2 días si es semanal
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
            # Reset a las 00:00 UTC del día siguiente
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
#  CALCULADORA DE POSICIÓN (MODIFICADA)
# ─────────────────────────────────────────────
class PositionCalculator:
    """
    Calcula tamaño de posición basado en:
    - 10% del saldo como posición
    - 40% de esa posición como SL máximo = 4% del saldo
    - TP fijo = 10x el riesgo = 40% del saldo
    
    ⚠️ SL y TP se calculan SIEMPRE con esta lógica fija,
    independientemente de la estructura (EMA/swing).
    """

    POSITION_PCT = 0.10   # 10% del saldo
    SL_MAX_PCT   = 0.40   # 40% de la posición = 4% del saldo
    TP_MULTIPLE  = 10.0   # 10x el riesgo → TP = 40% del saldo

    @classmethod
    def calculate(
        cls,
        symbol:          str,
        side:            PositionSide,
        entry_price:     float,
        sl_structural:   float,   # ⚠️ Ya NO se usa para SL
        tp_structural:   float,   # ⚠️ Ya NO se usa para TP
        balance:         float,
        leverage:        int = 10,  # MODIFICADO: default 10x
    ) -> PositionSize:
        """
        Calcula el tamaño de posición con SL y TP fijos.
        sl_structural y tp_structural se ignoran.
        """
        # 1. Posición: 10% del saldo
        position_usd = balance * cls.POSITION_PCT
        
        # 2. Riesgo máximo: 40% de la posición = 4% del saldo
        max_loss_usd = position_usd * cls.SL_MAX_PCT
        
        # 3. SL en precio (fijo, basado en riesgo)
        #    Distancia = (max_loss_usd / position_usd) / leverage
        #    = 0.40 / leverage
        sl_distance_pct = cls.SL_MAX_PCT / leverage  # 0.40/10 = 4% en precio
        
        if side == PositionSide.LONG:
            stop_loss = entry_price * (1 - sl_distance_pct)
            take_profit = entry_price * (1 + cls.SL_MAX_PCT * cls.TP_MULTIPLE / leverage)
            # Para LONG: TP = entry * (1 + 0.40*10/10) = entry * 1.40 → 40% arriba
        else:  # SHORT
            stop_loss = entry_price * (1 + sl_distance_pct)
            take_profit = entry_price * (1 - cls.SL_MAX_PCT * cls.TP_MULTIPLE / leverage)
            # Para SHORT: TP = entry * (1 - 0.40*10/10) = entry * 0.60 → 40% abajo
        
        # 4. Contratos
        position_with_leverage = position_usd * leverage
        contracts = position_with_leverage / entry_price
        
        # 5. Riesgo en USD (confirmación)
        risk_usd = position_usd * sl_distance_pct
        
        logger.info(
            f"[PositionCalc] {symbol} {side.value} | "
            f"Posición: ${position_usd:.2f} | "
            f"Riesgo: ${risk_usd:.2f} (4% del capital) | "
            f"SL: {stop_loss:.4f} ({sl_distance_pct*100:.1f}%) | "
            f"TP: {take_profit:.4f} | "
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
        )

    @staticmethod
    def _prepare_structural_df(df: pd.DataFrame) -> pd.DataFrame:
        """Enriquece el DataFrame con indicadores para la evaluación de cierre."""
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
    """
    Decide cuándo cerrar o revertir basándose en
    el comportamiento de las EMAs — no en precio fijo.
    
    ⚠️ MODIFICADO: Ahora solo evalúa REVERSOS (trampas),
    el SL y TP fijos se evalúan en RiskManager.monitor_positions()
    """

    # Cuántas velas atrás buscar el triple techo/suelo
    TRIPLE_LOOKBACK = 20

    @classmethod
    def evaluate_exit(
        cls,
        position: OpenPosition,
        df: pd.DataFrame,
    ) -> Tuple[bool, CloseReason, str]:
        """
        Evalúa si se debe cerrar o revertir la posición.
        ⚠️ SOLO evalúa REVERSOS (trampas).
        El SL y TP fijos NO se evalúan aquí.
        """
        last = df.iloc[-1]
        prev = df.iloc[-2]

        # ⚠️ SL y TP fijos se evalúan en monitor_positions()
        # Aquí solo evaluamos reversos por estructura

        # ── Verificar reverso por estructura ──
        ema21 = float(last["ema21"])
        ema55 = float(last["ema55"])
        ema21_prev = float(prev["ema21"])
        ema55_prev = float(prev["ema55"])

        if position.side == PositionSide.LONG:
            # EMA21 cruzó hacia abajo de EMA55
            cross_down = (ema21 < ema55) and (ema21_prev >= ema55_prev)
            if cross_down:
                is_trap = cls._detect_triple_top(df)
                if is_trap:
                    return True, CloseReason.REVERSE, (
                        "Triple techo detectado — revertir a SHORT"
                    )
                # Si no es trampa, NO cerramos por estructura
                # (el SL fijo se encargará)

        else:  # SHORT
            # EMA21 cruzó hacia arriba de EMA55
            cross_up = (ema21 > ema55) and (ema21_prev <= ema55_prev)
            if cross_up:
                is_trap = cls._detect_triple_bottom(df)
                if is_trap:
                    return True, CloseReason.REVERSE, (
                        "Triple suelo detectado — revertir a LONG"
                    )
                # Si no es trampa, NO cerramos por estructura

        return False, CloseReason.MANUAL, ""

    @classmethod
    def _detect_triple_top(cls, df: pd.DataFrame) -> bool:
        """
        Detecta triple techo + breakout falso:
        - 3 máximos similares en las últimas N velas
        - Última vela rompió el máximo pero cerró debajo
        - Volumen alto en esa vela
        """
        recent = df.tail(cls.TRIPLE_LOOKBACK)
        highs  = recent["high"].values
        last   = df.iloc[-1]

        # Encontrar máximos locales
        peaks = []
        for i in range(1, len(highs) - 1):
            if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
                peaks.append(highs[i])

        if len(peaks) < 2:
            return False

        # Verificar si los picos son similares (dentro del 3%)
        max_peak = max(peaks)
        similar  = [p for p in peaks if abs(p - max_peak) / max_peak < 0.03]

        if len(similar) < 2:
            return False

        # Verificar breakout falso en última vela
        last_high  = float(last["high"])
        last_close = float(last["close"])
        fake_break = last_high > max_peak and last_close < max_peak

        # Volumen alto en la trampa
        vol_ratio = float(last["vol_ratio"]) if "vol_ratio" in last else 1.0
        high_vol  = vol_ratio >= 1.5

        return fake_break and high_vol

    @classmethod
    def _detect_triple_bottom(cls, df: pd.DataFrame) -> bool:
        """Detecta triple suelo + breakout falso bajista."""
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
#  RISK MANAGER PRINCIPAL (MODIFICADO)
# ─────────────────────────────────────────────
class RiskManager:
    """
    Módulo principal de gestión de riesgo.
    Coordina: KillSwitch + PositionCalculator + StructureExitEngine
    """

    def __init__(
        self,
        api_manager,
        mode:          BotMode = BotMode.DRY_RUN,
        leverage:      int     = 10,   # MODIFICADO: default 10x
        db_path:       str     = "patterns.db",
        max_positions: int     = 3,
        position_pct:  float   = 0.10,   # NUEVO: permite configurar
        sl_pct:        float   = 0.40,   # NUEVO: permite configurar
        tp_multiple:   float   = 10.0,   # NUEVO: permite configurar
    ):
        self.api           = api_manager
        self.mode          = mode
        self.leverage      = leverage
        self.db_path       = db_path
        self.max_positions = max_positions
        self.position_pct  = position_pct
        self.sl_pct        = sl_pct
        self.tp_multiple   = tp_multiple
        self.kill_switch   = KillSwitch()
        self.positions:    Dict[str, OpenPosition] = {}
        self._capital:     CapitalState = CapitalState()

        # Actualizar constantes del PositionCalculator
        PositionCalculator.POSITION_PCT = position_pct
        PositionCalculator.SL_MAX_PCT = sl_pct
        PositionCalculator.TP_MULTIPLE = tp_multiple

        logger.info(
            f"[RiskManager] Iniciado | "
            f"Modo: {mode.value} | "
            f"Leverage: {leverage}x | "
            f"Max posiciones: {max_positions} | "
            f"Posición: {position_pct*100:.0f}% | "
            f"SL: {sl_pct*100:.0f}% de posición ({sl_pct*position_pct*100:.1f}% capital) | "
            f"TP: {tp_multiple}x riesgo"
        )

    # ──────────────────────────────────────────
    #  ACTUALIZAR CAPITAL
    # ──────────────────────────────────────────
    async def update_capital(self) -> CapitalState:
        """Sincroniza el estado del capital con Bybit."""
        if self.mode == BotMode.DRY_RUN:
            return self._capital

        try:
            balance = await self.api.fetch_balance()
            total   = balance["total"]

            # Primer arranque del día
            if self._capital.day_start_balance == 0:
                self._capital.day_start_balance = total
                self.kill_switch.set_day_start(total)
                logger.info(
                    f"[Capital] Saldo inicial del día: ${total:.2f}"
                )

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
        """Para DRY_RUN — establece balance inicial manualmente."""
        self._capital.total_balance     = balance
        self._capital.available         = balance
        self._capital.day_start_balance = balance
        self.kill_switch.set_day_start(balance)
        logger.info(f"[Capital] Balance DRY_RUN: ${balance:.2f}")

    # ──────────────────────────────────────────
    #  EVALUAR NUEVA ENTRADA (MODIFICADO)
    # ──────────────────────────────────────────
    async def evaluate_entry(
        self,
        signal,           # Signal del MarketScanner
        df: pd.DataFrame,
        sl_structural: float,   # ⚠️ YA NO SE USA
        tp_structural: float,   # ⚠️ YA NO SE USA
    ) -> Optional[PositionSize]:
        """
        Evalúa si se puede abrir una nueva posición.
        ⚠️ sl_structural y tp_structural se ignoran.
        """
        # ── Kill Switch ──
        stopped, reason = self.kill_switch.check(
            self._capital.total_balance
        )
        if stopped:
            logger.warning(f"[RiskManager] Kill Switch activo: {reason}")
            return None

        # ── Máximo de posiciones simultáneas ──
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

        # ── Calcular tamaño con SL/TP fijos ──
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
            sl_structural = 0.0,   # Ignorado
            tp_structural = 0.0,   # Ignorado
            balance       = self._capital.total_balance,
            leverage      = self.leverage,
        )

        logger.info(
            f"[RiskManager] ✅ Entrada aprobada: "
            f"{signal.symbol} {side.value} | "
            f"${position_size.position_usd:.2f} | "
            f"Riesgo: ${position_size.risk_usd:.2f} | "
            f"SL: {position_size.stop_loss:.4f} | "
            f"TP: {position_size.take_profit:.4f}"
        )

        return position_size

    # ──────────────────────────────────────────
    #  REGISTRAR POSICIÓN ABIERTA
    # ──────────────────────────────────────────
    def register_position(
        self,
        order_id:      str,
        position_size: PositionSize,
        pattern_id:    Optional[int] = None,
    ) -> OpenPosition:
        """Registra una posición abierta para seguimiento."""
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
        )
        self.positions[position_size.symbol] = pos
        logger.info(
            f"[RiskManager] 📝 Posición registrada: "
            f"{pos.symbol} {pos.side.value} @ {pos.entry_price}"
        )
        return pos

    # ──────────────────────────────────────────
    #  MONITOREAR POSICIONES ABIERTAS (MODIFICADO)
    # ──────────────────────────────────────────
    async def monitor_positions(
        self,
        dfs: Dict[str, pd.DataFrame],
    ) -> Dict[str, Tuple[bool, CloseReason, str]]:
        """
        Revisa todas las posiciones abiertas.
        ⚠️ AHORA evalúa SL y TP FIJOS además de reversos.
        """
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

            # Actualizar precio actual y PnL
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

            # ── 1. Evaluar SL y TP fijos (NUEVO) ──
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

            # ── 2. Evaluar reverso por estructura (mantenido) ──
            should_close, reason, notes = StructureExitEngine.evaluate_exit(
                position, df
            )

            if should_close:
                results[symbol] = (True, reason, notes)
                logger.info(
                    f"[Monitor] 🔔 {symbol} — cerrar por {reason.value}: {notes}"
                )
            else:
                results[symbol] = (False, CloseReason.MANUAL, "")

        return results

    # ──────────────────────────────────────────
    #  CERRAR / REVERTIR POSICIÓN
    # ──────────────────────────────────────────
    async def close_position(
        self,
        symbol:       str,
        reason:       CloseReason,
        current_price: float,
    ) -> Optional[dict]:
        """
        Cierra una posición y actualiza la BD de patrones.
        Si reason == REVERSE, abre en dirección contraria.
        """
        if symbol not in self.positions:
            return None

        pos = self.positions[symbol]

        # Calcular PnL
        if pos.side == PositionSide.LONG:
            pnl_pct = (current_price - pos.entry_price) / pos.entry_price
        else:
            pnl_pct = (pos.entry_price - current_price) / pos.entry_price

        pnl_usd = pos.position_usd * pnl_pct * pos.leverage

        # Duración
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

        # Actualizar BD de patrones
        if pos.pattern_id:
            self._update_pattern_result(
                pattern_id   = pos.pattern_id,
                exit_price   = current_price,
                exit_reason  = reason.value,
                pnl_percent  = pnl_pct * 100,
                pnl_usd      = pnl_usd,
                duration_min = duration_m,
            )

        # Actualizar capital
        self._capital.total_balance += pnl_usd
        self._capital.available     += pos.position_usd + pnl_usd
        self._capital.day_pnl       += pnl_usd

        # Remover posición
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
        }

        # Si es reverso — señal para abrir en dirección contraria
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
    #  ACTUALIZAR BD DE PATRONES
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
        """Registra el resultado real de una operación en la BD."""
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
        """Estado completo para el dashboard."""
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
            "positions": {
                sym: {
                    "side":        pos.side.value,
                    "entry":       pos.entry_price,
                    "current":     pos.current_price,
                    "pnl_usd":     pos.unrealized_pnl,
                    "pnl_pct":     pos.unrealized_pnl_pct,
                    "sl":          pos.stop_loss,
                    "tp":          pos.take_profit,
                }
                for sym, pos in self.positions.items()
            },
        }


# ─────────────────────────────────────────────
#  EJEMPLO DRY RUN
# ─────────────────────────────────────────────
async def main():
    """
    Prueba del RiskManager.
    El capital se lee SIEMPRE de la cuenta real — nunca hardcodeado.
    En DRY_RUN usa el balance real de Bybit Testnet.
    En LIVE usa el balance real de Bybit.
    """
    # Importar el APIManager real
    # from bybit_api_manager import BybitAPIManager
    # api = BybitAPIManager(
    #     api_key    = "TU_API_KEY",
    #     api_secret = "TU_API_SECRET",
    #     sandbox    = True,   # True = Testnet | False = Live
    # )

    # ── Solo para test sin API ──
    class MockAPI:
        async def fetch_balance(self):
            # En producción esto viene de Bybit real
            # Aquí simula la llamada sin valor fijo
            raise NotImplementedError(
                "Conecta el APIManager real. "
                "El capital nunca se hardcodea."
            )

    rm = RiskManager(
        api_manager   = MockAPI(),
        mode          = BotMode.DRY_RUN,
        leverage      = 10,   # MODIFICADO: 10x
        max_positions = 3,
        position_pct  = 0.10,
        sl_pct        = 0.40,
        tp_multiple   = 10.0,
    )

    # Capital real desde la API
    try:
        capital = await rm.update_capital()
        balance = capital.total_balance
    except NotImplementedError as e:
        print(f"\n⚠️  {e}")
        print("   Conecta tu APIManager para ver el balance real.\n")
        return

    status = rm.get_status()
    print("\n═══ RISK MANAGER — Estado Inicial ═══")
    print(f"  Balance real:   ${balance:.2f}")
    print(f"  Disponible:     ${status['available']:.2f}")
    print(f"  Kill Switch:    {'ACTIVO' if status['kill_switch'] else 'OK'}")
    print(f"  Posiciones:     {status['open_positions']}")
    print(f"  Modo:           {status['mode']}")
    print(f"  Por operación:  ${balance * 0.10:.2f} (10% del balance real)")
    print(f"  Riesgo máx:     ${balance * 0.04:.2f} (4% del balance real)")
    print(f"  SL en precio:   {0.40/10*100:.1f}% desde entrada")
    print(f"  TP en precio:   {0.40*10/10*100:.1f}% desde entrada")
    print("═" * 40)


if __name__ == "__main__":
    asyncio.run(main())
