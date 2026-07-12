"""
RiskManager — Gestión de Riesgo UNIFICADA (M15 + M3)
=========================================================
- SL: 4% del capital (40% de la posición)
- TP: 20% del capital (5x SL)
- Leverage: 10x
- R:R: 5:1
- TODOS los valores en USDT
"""

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone, time as dtime
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
    TAKE_PROFIT = "TP"
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
    position_usd: float
    risk_usd: float
    entry_price: float
    stop_loss: float
    take_profit: float
    contracts: float
    leverage: int
    sl_distance_pct: float
    max_loss_usd: float
    is_st_asset: bool = False
    timeframe: str = "15m"


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
    leverage: int
    position_usd: float
    risk_usd: float
    pattern_id: Optional[int] = None
    timeframe: str = "15m"
    open_time: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    is_st_asset: bool = False
    score: float = 0.0
    signal_type: Optional[str] = None
    arrow_color: Optional[str] = None


class KillSwitch:
    DAILY_DRAWDOWN_LIMIT = 15.0
    WEEKLY_DRAWDOWN_LIMIT = 30.0

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
                logger.info("[KillSwitch] Reset automático a las 00:00 UTC.")

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
            from datetime import timedelta
            self._reset_time = now + timedelta(days=days)
        else:
            from datetime import timedelta
            self._reset_time = datetime.combine(now.date() + timedelta(days=1), dtime(0, 0, 0), tzinfo=timezone.utc)

        logger.critical(f"[KillSwitch] 🛑 ACTIVADO: {reason} | Reset: {self._reset_time.isoformat()}")


class PositionCalculator:
    # ═══════════════════════════════════════════════════════════════
    #  CONFIGURACIÓN UNIFICADA (M15 + M3)
    #  SL: 4% del capital | TP: 20% del capital | Leverage: 10x
    #  TODOS los valores en USDT
    # ═══════════════════════════════════════════════════════════════
    
    POSITION_PCT = 0.10      # 10% del capital
    SL_MAX_PCT = 0.40        # 40% de la posición = 4% capital
    TP_MULTIPLE = 5.0        # 5x SL = 20% capital
    LEVERAGE = 10            # 10x
    ST_REDUCTION = 0.50      # 50% para activos ST

    TIMEFRAME_CONFIG = {
        "15m": {
            "position_pct": 0.10,
            "sl_pct": 0.40,
            "tp_multiple": 5.0,
            "leverage": 10,
        },
        "3m": {
            "position_pct": 0.10,
            "sl_pct": 0.40,
            "tp_multiple": 5.0,
            "leverage": 10,
        },
    }

    @classmethod
    def get_config(cls, timeframe: str):
        return cls.TIMEFRAME_CONFIG.get(timeframe, cls.TIMEFRAME_CONFIG["15m"])

    @classmethod
    def calculate(
        cls,
        symbol: str,
        side: PositionSide,
        entry_price: float,
        sl_structural: float,
        tp_structural: float,
        balance: float,
        leverage: Optional[int] = None,
        max_leverage: Optional[int] = None,
        is_st_asset: bool = False,
        timeframe: str = "15m",
    ) -> PositionSize:
        config = cls.get_config(timeframe)

        position_pct = config["position_pct"]
        sl_pct = config["sl_pct"]
        tp_multiple = config["tp_multiple"]
        leverage = leverage or config["leverage"]

        if is_st_asset:
            position_pct = position_pct * cls.ST_REDUCTION
            logger.warning(f"[PositionCalc] {symbol} es ST, posición reducida")

        if max_leverage and leverage > max_leverage:
            logger.warning(f"[PositionCalc] Leverage {leverage}x > {max_leverage}x, usando {max_leverage}x")
            leverage = max_leverage

        position_usd = balance * position_pct
        max_loss_usd = position_usd * sl_pct
        sl_distance_pct = sl_pct / leverage

        if side == PositionSide.LONG:
            stop_loss = entry_price * (1 - sl_distance_pct)
            take_profit = entry_price * (1 + sl_pct * tp_multiple / leverage)
        else:
            stop_loss = entry_price * (1 + sl_distance_pct)
            take_profit = entry_price * (1 - sl_pct * tp_multiple / leverage)

        contracts = (position_usd * leverage) / entry_price
        risk_usd = position_usd * sl_distance_pct

        logger.info(
            f"[PositionCalc] {symbol} {side.value} {timeframe} | "
            f"Posición: {position_usd:.2f} USDT | Riesgo: {risk_usd:.2f} USDT | "
            f"SL: {stop_loss:.4f} ({sl_distance_pct*100:.1f}%) | "
            f"TP: {take_profit:.4f} ({sl_pct*tp_multiple/leverage*100:.1f}%) | "
            f"Leverage: {leverage}x | R:R: {tp_multiple:.1f}:1"
        )

        return PositionSize(
            symbol=symbol,
            side=side,
            position_usd=round(position_usd, 2),
            risk_usd=round(risk_usd, 2),
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            contracts=round(contracts, 4),
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
        df["ema233"] = df["close"].ewm(span=233, adjust=False).mean()
        df["bb_mid"] = df["close"].rolling(20).mean()
        df["bb_std"] = df["close"].rolling(20).std()
        df["bb_upper"] = df["bb_mid"] + 2 * df["bb_std"]
        df["bb_lower"] = df["bb_mid"] - 2 * df["bb_std"]
        df["vol_avg"] = df["volume"].rolling(20).mean()
        df["vol_ratio"] = df["volume"] / df["vol_avg"].replace(0, 1e-9)
        return df.dropna()


class StructureExitEngine:
    TRIPLE_LOOKBACK = 20

    @classmethod
    def evaluate_exit(cls, position: OpenPosition, df: pd.DataFrame) -> Tuple[bool, CloseReason, str]:
        last = df.iloc[-1]
        prev = df.iloc[-2]

        ema21 = float(last["ema21"])
        ema55 = float(last["ema55"])
        ema21_prev = float(prev["ema21"])
        ema55_prev = float(prev["ema55"])

        if position.side == PositionSide.LONG:
            cross_down = (ema21 < ema55) and (ema21_prev >= ema55_prev)
            if cross_down:
                if cls._detect_triple_top(df):
                    return True, CloseReason.REVERSE, "Triple techo detectado — revertir a SHORT"
        else:
            cross_up = (ema21 > ema55) and (ema21_prev <= ema55_prev)
            if cross_up:
                if cls._detect_triple_bottom(df):
                    return True, CloseReason.REVERSE, "Triple suelo detectado — revertir a LONG"

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
        position_pct: float = 0.10,
        sl_pct: float = 0.40,
        tp_multiple: float = 5.0,
        st_reduction: float = 0.50,
        cooldown_minutes: int = 15,
        max_entries_daily: int = 3,
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

        logger.info(
            f"[RiskManager] Iniciado | Modo: {mode.value} | "
            f"Leverage: {leverage}x | Max posiciones: {max_positions} | "
            f"Posición: {position_pct*100:.0f}% | SL: {sl_pct*100:.0f}% de posición ({sl_pct*position_pct*100:.1f}% capital) | "
            f"TP: {tp_multiple}x SL ({sl_pct*tp_multiple*position_pct*100:.1f}% capital) | "
            f"R:R: {tp_multiple:.1f}:1 | Cooldown: {cooldown_minutes}min"
        )

    async def update_capital(self) -> CapitalState:
        if self.mode == BotMode.DRY_RUN:
            return self._capital

        try:
            balance = await self.api.fetch_balance()
            total = balance["total"]

            if self._capital.day_start_balance == 0:
                self._capital.day_start_balance = total
                self.kill_switch.set_day_start(total)
                logger.info(f"[Capital] Saldo inicial del día: {total:.2f} USDT")

            self._capital.total_balance = total
            self._capital.available = balance["free"]
            self._capital.day_pnl = total - self._capital.day_start_balance
            self._capital.day_pnl_pct = (self._capital.day_pnl / self._capital.day_start_balance * 100) if self._capital.day_start_balance > 0 else 0
            self._capital.open_positions = len(self.positions)

            return self._capital

        except Exception as e:
            logger.error(f"[Capital] Error actualizando balance: {e}")
            return self._capital

    def set_initial_balance(self, balance: float):
        self._capital.total_balance = balance
        self._capital.available = balance
        self._capital.day_start_balance = balance
        self.kill_switch.set_day_start(balance)
        logger.info(f"[Capital] Balance DRY_RUN: {balance:.2f} USDT")

    async def evaluate_entry(
        self,
        signal,
        df: pd.DataFrame,
        sl_structural: float,
        tp_structural: float,
    ) -> Optional[PositionSize]:
        stopped, reason = self.kill_switch.check(self._capital.total_balance)
        if stopped:
            logger.warning(f"[RiskManager] Kill Switch activo: {reason}")
            return None

        if len(self.positions) >= self.max_positions:
            logger.debug(f"[RiskManager] Máximo de posiciones alcanzado ({self.max_positions})")
            return None

        if signal.symbol in self.positions:
            logger.debug(f"[RiskManager] Ya hay posición en {signal.symbol}")
            return None

        if signal.symbol in self._symbol_cooldown:
            elapsed = time.time() - self._symbol_cooldown[signal.symbol]
            if elapsed < self.cooldown_minutes * 60:
                return None
            else:
                del self._symbol_cooldown[signal.symbol]

        today = datetime.now(timezone.utc).date().isoformat()
        if self._last_entry_date.get(signal.symbol) != today:
            self._symbol_daily_entries[signal.symbol] = 0
            self._last_entry_date[signal.symbol] = today

        if self._symbol_daily_entries[signal.symbol] >= self.max_entries_daily:
            logger.warning(f"[RiskManager] {signal.symbol} límite diario alcanzado")
            return None

        timeframe = getattr(signal, 'timeframe', '15m')

        try:
            max_leverage = await self.api.get_max_leverage(signal.symbol)
            is_st = await self.api.is_st_asset(signal.symbol)
        except Exception as e:
            logger.warning(f"[RiskManager] Error detectando leverage para {signal.symbol}: {e}")
            max_leverage = self.leverage
            is_st = False

        tf_config = PositionCalculator.get_config(timeframe)
        effective_leverage = min(tf_config["leverage"], max_leverage)

        # ✅ CAMBIO APLICADO AQUÍ: Ahora importa desde strategy_scanner
        from strategy_scanner import SignalType
        side = PositionSide.LONG if signal.signal_type in (SignalType.LONG_BREAKOUT, SignalType.LONG_REVERSAL) else PositionSide.SHORT

        position_size = PositionCalculator.calculate(
            symbol=signal.symbol,
            side=side,
            entry_price=signal.entry_price,
            sl_structural=0.0,
            tp_structural=0.0,
            balance=self._capital.total_balance,
            leverage=effective_leverage,
            max_leverage=max_leverage,
            is_st_asset=is_st,
            timeframe=timeframe,
        )

        self._symbol_daily_entries[signal.symbol] += 1

        logger.info(
            f"[RiskManager] ✅ Entrada aprobada: {signal.symbol} {side.value} {timeframe} | "
            f"{position_size.position_usd:.2f} USDT | Riesgo: {position_size.risk_usd:.2f} USDT"
        )

        return position_size

    def register_position(
        self,
        order_id: str,
        position_size: PositionSize,
        pattern_id: Optional[int] = None,
        signal_type: Optional[str] = None,
        arrow_color: Optional[str] = None,
        score: float = 0.0,
    ) -> OpenPosition:
        pos = OpenPosition(
            id=order_id,
            symbol=position_size.symbol,
            side=position_size.side,
            entry_price=position_size.entry_price,
            current_price=position_size.entry_price,
            stop_loss=position_size.stop_loss,
            take_profit=position_size.take_profit,
            contracts=position_size.contracts,
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
        )
        self.positions[position_size.symbol] = pos
        logger.info(f"[RiskManager] 📝 Posición registrada: {pos.symbol} {pos.side.value} {pos.timeframe}")
        return pos

    async def monitor_positions(self, dfs: Dict[str, pd.DataFrame]) -> Dict[str, Tuple[bool, CloseReason, str]]:
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

            position.unrealized_pnl_pct = pnl_pct * 100 * position.leverage
            position.unrealized_pnl = position.position_usd * pnl_pct * position.leverage

            # SL/TP fijos
            if position.side == PositionSide.LONG:
                if current <= position.stop_loss:
                    results[symbol] = (True, CloseReason.STOP_LOSS, f"SL tocado {current:.4f}")
                    continue
                if current >= position.take_profit:
                    results[symbol] = (True, CloseReason.TAKE_PROFIT, f"TP tocado {current:.4f}")
                    continue
            else:
                if current >= position.stop_loss:
                    results[symbol] = (True, CloseReason.STOP_LOSS, f"SL tocado {current:.4f}")
                    continue
                if current <= position.take_profit:
                    results[symbol] = (True, CloseReason.TAKE_PROFIT, f"TP tocado {current:.4f}")
                    continue

            # Reverso por estructura
            should_close, reason, notes = StructureExitEngine.evaluate_exit(position, df)
            if should_close:
                results[symbol] = (True, reason, notes)

        return results

    async def close_position(self, symbol: str, reason: CloseReason, current_price: float) -> Optional[dict]:
        if symbol not in self.positions:
            return None

        pos = self.positions[symbol]

        if pos.side == PositionSide.LONG:
            pnl_pct = (current_price - pos.entry_price) / pos.entry_price
        else:
            pnl_pct = (pos.entry_price - current_price) / pos.entry_price

        pnl_usd = pos.position_usd * pnl_pct * pos.leverage

        open_dt = datetime.fromisoformat(pos.open_time)
        now_dt = datetime.now(timezone.utc)
        duration_m = (now_dt - open_dt).total_seconds() / 60

        # ✅ CORREGIDO: Mostrar USDT en lugar de $
        logger.info(
            f"[RiskManager] {'🟢' if pnl_usd > 0 else '🔴'} "
            f"Cerrando {symbol} {pos.side.value} | PnL: {pnl_usd:.2f} USDT ({pnl_pct*100:.1f}%) | "
            f"Razón: {reason.value} | Duración: {duration_m:.0f}min"
        )

        self._symbol_cooldown[symbol] = time.time()

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
        self._capital.available += pos.position_usd + pnl_usd
        self._capital.day_pnl += pnl_usd

        del self.positions[symbol]

        result = {
            "symbol": symbol,
            "side": pos.side.value,
            "entry": pos.entry_price,
            "exit": current_price,
            "pnl_usd": round(pnl_usd, 2),
            "pnl_pct": round(pnl_pct * 100, 2),
            "reason": reason.value,
            "duration_min": round(duration_m, 0),
            "reverse": reason == CloseReason.REVERSE,
            "timeframe": pos.timeframe,
        }

        if reason == CloseReason.REVERSE:
            reverse_side = PositionSide.SHORT if pos.side == PositionSide.LONG else PositionSide.LONG
            result["reverse_side"] = reverse_side.value
            result["reverse_price"] = current_price
            logger.info(f"[RiskManager] 🔄 REVERSO: abrir {reverse_side.value} en {symbol} @ {current_price}")

        return result

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
            "day_start": self._capital.day_start_balance,
            "day_pnl": self._capital.day_pnl,
            "day_pnl_pct": self._capital.day_pnl_pct,
            "drawdown_pct": self._capital.drawdown_pct if hasattr(self._capital, 'drawdown_pct') else 0.0,
            "kill_switch": self.kill_switch.active,
            "kill_switch_reason": self.kill_switch.reason,
            "open_positions": len(self.positions),
            "positions": {
                sym: {
                    "side": pos.side.value,
                    "entry": pos.entry_price,
                    "current": pos.current_price,
                    "pnl_usd": pos.unrealized_pnl,
                    "pnl_pct": pos.unrealized_pnl_pct,
                    "sl": pos.stop_loss,
                    "tp": pos.take_profit,
                    "timeframe": pos.timeframe,
                    "is_st": pos.is_st_asset,
                }
                for sym, pos in self.positions.items()
            },
        }
