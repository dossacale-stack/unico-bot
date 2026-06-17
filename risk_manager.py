"""
RiskManager — Módulo de Gestión de Riesgo y Ejecución
======================================================
ÚNICO STRATEGY — Gestión de riesgo profesional

Lógica de capital:
  - Posición:    10% del saldo total
  - SL:          40% de esa posición (= 4% del saldo)
  - Kill Switch: si pierde 15% del saldo del día → parar hasta 00:00
  - TP:          por estructura — no por ratio fijo
  - Reverso:     cierra contrato y abre en dirección contraria

Cierre por comportamiento de EMAs, no por precio:
  - Mantener mientras cascada de EMAs activa
  - Cerrar cuando EMA21 cruza en dirección contraria
  - Revertir cuando aparece trampa (triple techo / breakout falso)
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
    STRUCTURE_BREAK = "STRUCTURE"   # EMA cruzó en contra
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
            tomorrow = now.date().__class__(
                now.year, now.month, now.day
            )
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
    """
    Calcula tamaño de posición basado en:
    - 10% del saldo como posición
    - 40% de esa posición como SL máximo
    - SL por estructura (EMA o swing) cuando es más corto
    """

    POSITION_PCT = 0.10   # 10% del saldo
    SL_MAX_PCT   = 0.40   # 40% de la posición = 4% del saldo

    @classmethod
    def calculate(
        cls,
        symbol:          str,
        side:            PositionSide,
        entry_price:     float,
        sl_structural:   float,   # SL por estructura (EMA/swing)
        tp_structural:   float,   # TP por estructura
        balance:         float,
        leverage:        int = 20,
    ) -> PositionSize:
        """
        Calcula el tamaño de posición respetando los límites de riesgo.
        """
        position_usd = balance * cls.POSITION_PCT
        max_loss_usd = position_usd * cls.SL_MAX_PCT  # 4% del saldo

        # Distancia al SL estructural
        if side == PositionSide.LONG:
            sl_distance = entry_price - sl_structural
        else:
            sl_distance = sl_structural - entry_price

        sl_distance_pct = sl_distance / entry_price

        # Verificar si el SL estructural excede el límite
        sl_cost = position_usd * sl_distance_pct
        if sl_cost > max_loss_usd:
            # Ajustar posición para que el riesgo no exceda el límite
            position_usd = max_loss_usd / sl_distance_pct
            logger.warning(
                f"[PositionCalc] SL estructural muy amplio. "
                f"Posición reducida a ${position_usd:.2f}"
            )

        # Contratos
        position_with_leverage = position_usd * leverage
        contracts = position_with_leverage / entry_price

        risk_usd = position_usd * sl_distance_pct

        logger.info(
            f"[PositionCalc] {symbol} {side.value} | "
            f"Posición: ${position_usd:.2f} | "
            f"Riesgo: ${risk_usd:.2f} | "
            f"SL dist: {sl_distance_pct*100:.2f}% | "
            f"Contratos: {contracts:.4f}"
        )

        return PositionSize(
            symbol          = symbol,
            side            = side,
            position_usd    = round(position_usd, 2),
            risk_usd        = round(risk_usd, 2),
            entry_price     = entry_price,
            stop_loss       = sl_structural,
            take_profit     = tp_structural,
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

    Reglas de cierre LONG:
      - EMA21 cruza hacia abajo de EMA55 → cerrar
      - Triple techo + breakout falso → REVERTIR a short

    Reglas de cierre SHORT:
      - EMA21 cruza hacia arriba de EMA55 → cerrar
      - Triple suelo + breakout falso → REVERTIR a long
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
        Retorna (debe_cerrar, razón, notas).
        """
        last = df.iloc[-1]
        prev = df.iloc[-2]

        # ── Verificar SL hard ──
        if position.side == PositionSide.LONG:
            if float(last["low"]) <= position.stop_loss:
                return True, CloseReason.STOP_LOSS, "SL tocado"
        else:
            if float(last["high"]) >= position.stop_loss:
                return True, CloseReason.STOP_LOSS, "SL tocado"

        # ── Cierre por estructura — cruce de EMA21 ──
        ema21 = float(last["ema21"])
        ema55 = float(last["ema55"])
        ema21_prev = float(prev["ema21"])
        ema55_prev = float(prev["ema55"])

        if position.side == PositionSide.LONG:
            # EMA21 cruzó hacia abajo de EMA55
            cross_down = (ema21 < ema55) and (ema21_prev >= ema55_prev)
            if cross_down:
                # Verificar si es trampa (triple techo) o cierre real
                is_trap = cls._detect_triple_top(df)
                if is_trap:
                    return True, CloseReason.REVERSE, (
                        "Triple techo detectado — revertir a SHORT"
                    )
                return True, CloseReason.STRUCTURE_BREAK, (
                    "EMA21 cruzó bajo EMA55 — tendencia alcista terminada"
                )

        else:  # SHORT
            # EMA21 cruzó hacia arriba de EMA55
            cross_up = (ema21 > ema55) and (ema21_prev <= ema55_prev)
            if cross_up:
                is_trap = cls._detect_triple_bottom(df)
                if is_trap:
                    return True, CloseReason.REVERSE, (
                        "Triple suelo detectado — revertir a LONG"
                    )
                return True, CloseReason.STRUCTURE_BREAK, (
                    "EMA21 cruzó sobre EMA55 — tendencia bajista terminada"
                )

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
#  RISK MANAGER PRINCIPAL
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
        leverage:      int     = 20,
        db_path:       str     = "patterns.db",
        max_positions: int     = 3,   # máximo simultáneos
    ):
        self.api           = api_manager
        self.mode          = mode
        self.leverage      = leverage
        self.db_path       = db_path
        self.max_positions = max_positions
        self.kill_switch   = KillSwitch()
        self.positions:    Dict[str, OpenPosition] = {}
        self._capital:     CapitalState = CapitalState()

        logger.info(
            f"[RiskManager] Iniciado | "
            f"Modo: {mode.value} | "
            f"Leverage: {leverage}x | "
            f"Max posiciones: {max_positions}"
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
    #  EVALUAR NUEVA ENTRADA
    # ──────────────────────────────────────────
    async def evaluate_entry(
        self,
        signal,           # Signal del MarketScanner
        df: pd.DataFrame,
        sl_structural: float,
        tp_structural: float,
    ) -> Optional[PositionSize]:
        """
        Evalúa si se puede abrir una nueva posición.
        Retorna PositionSize o None si no se puede.
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

        # ── Calcular tamaño ──
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
            sl_structural = sl_structural,
            tp_structural = tp_structural,
            balance       = self._capital.total_balance,
            leverage      = self.leverage,
        )

        logger.info(
            f"[RiskManager] ✅ Entrada aprobada: "
            f"{signal.symbol} {side.value} | "
            f"${position_size.position_usd:.2f} | "
            f"Riesgo: ${position_size.risk_usd:.2f}"
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
    #  MONITOREAR POSICIONES ABIERTAS
    # ──────────────────────────────────────────
    async def monitor_positions(
        self,
        dfs: Dict[str, pd.DataFrame],
    ) -> Dict[str, Tuple[bool, CloseReason, str]]:
        """
        Revisa todas las posiciones abiertas.
        Retorna dict de {symbol: (debe_cerrar, razón, notas)}
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

            # Evaluar si cerrar
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
        leverage      = 20,
        max_positions = 3,
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
    print("═" * 40)


if __name__ == "__main__":
    asyncio.run(main())