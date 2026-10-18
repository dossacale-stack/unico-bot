"""
risk_manager.py — Tamaño de posición, costos, salidas y kill switch (v8)
=========================================================================
Compartido por el bot (DRY_RUN, DEMO, TESTNET, LIVE) y por el backtest.

Tamaño (lo que pidió Karla):
  - Margen por operación: MARGIN_PCT del capital (15%).
  - Pérdida máxima si toca el SL: MAX_LOSS_PCT_OF_MARGIN del margen (40%) = 6% del capital,
    AHORA incluyendo comisiones y deslizamiento (antes se ignoraban).
  - Apalancamiento: el máximo del activo con tope LEVERAGE_CAP (20x), mínimo MIN_LEVERAGE.

Salidas (iguales en el bot y en el backtest):
  - SL en el exchange desde el primer segundo (orden de entrada con SL adjunto).
  - TP1: TP1_PCT (25%) a TP1_R (2R)   → orden límite reduce-only (comisión maker).
  - TP2: TP2_PCT (25%) a TP2_R (5R)   → orden límite reduce-only.
  - Break-even al llegar a BE_R (3R): SL a entrada ± BE_OFFSET_PCT (cubre comisiones).
  - Trailing tras TP1: SL = máximo alcanzado − TRAIL_ATR_MULT × ATR (en SHORT, al revés).
  - El 50% restante sale por trailing o por el SL.

Kill switch: usa el capital real (equity), se reinicia cada día y cada semana (UTC),
se guarda en la base de datos y NO apaga el bot: solo bloquea nuevas entradas.
Las posiciones abiertas siguen protegidas por su SL y TP.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

LONG, SHORT = "LONG", "SHORT"


# ---------------------------------------------------------------------------
# Parámetros
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RiskParams:
    margin_pct: float = 0.15
    max_loss_pct_of_margin: float = 0.40
    leverage_cap: int = 20
    min_leverage: int = 3
    st_reduction: float = 0.50
    min_sl_pct: float = 0.001
    max_fee_to_risk: float = 0.20
    taker_fee: float = 0.00055
    maker_fee: float = 0.0002
    slippage_pct: float = 0.0005
    tp1_r: float = 2.0
    tp1_pct: float = 0.25
    tp2_r: float = 5.0
    tp2_pct: float = 0.25
    be_r: float = 3.0
    be_offset_pct: float = 0.0015
    trail_atr_mult: float = 2.5

    @classmethod
    def from_config(cls, cfg) -> "RiskParams":
        names = {f.name for f in fields(cls)}
        return cls(**{name: getattr(cfg, name) for name in names})


@dataclass(frozen=True)
class MarketLimits:
    qty_step: float = 0.0
    min_qty: float = 0.0
    min_notional: float = 0.0
    tick_size: float = 0.0


def floor_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(math.floor(value / step + 1e-9) * step, decimals + 2)


def round_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    decimals = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return round(round(price / tick) * tick, decimals + 2)


# ---------------------------------------------------------------------------
# Tamaño de posición
# ---------------------------------------------------------------------------
@dataclass
class PositionPlan:
    symbol: str
    side: str
    entry_price: float
    stop_loss: float
    contracts: float
    notional: float
    margin_usd: float
    leverage: int
    max_loss_usd: float          # lo máximo que se acepta perder (40% del margen)
    risk_usd: float              # pérdida esperada si toca el SL, con comisiones y deslizamiento
    price_risk_usd: float        # contratos × distancia al SL (1R en dinero)
    sl_distance_pct: float
    fee_to_risk: float
    tp1_price: float
    tp1_qty: float
    tp2_price: float
    tp2_qty: float
    be_trigger: float
    be_price: float
    is_st: bool = False
    target: Optional[float] = None   # zona: se cierra TODO ahí (TP1 = 100 %, sin TP2 ni break-even)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def plan_position(
    symbol: str,
    side: str,
    entry_price: float,
    stop_loss: float,
    balance: float,
    p: RiskParams,
    *,
    max_leverage: Optional[int] = None,
    is_st: bool = False,
    limits: Optional[MarketLimits] = None,
    target: Optional[float] = None,
) -> Tuple[Optional[PositionPlan], str]:
    """Calcula la posición. Devuelve (plan, "OK") o (None, motivo en español)."""
    if entry_price <= 0 or stop_loss <= 0:
        return None, "precio de entrada o SL inválido"
    if side == LONG and stop_loss >= entry_price:
        return None, f"SL ({stop_loss:.6g}) por encima de la entrada en LONG"
    if side == SHORT and stop_loss <= entry_price:
        return None, f"SL ({stop_loss:.6g}) por debajo de la entrada en SHORT"
    if balance <= 0:
        return None, "capital insuficiente"
    if target is not None and ((side == LONG and target <= entry_price) or (side == SHORT and target >= entry_price)):
        return None, f"el precio ya llegó a la zona objetivo ({target:.6g})"

    sl_dist = abs(entry_price - stop_loss) / entry_price
    if sl_dist < p.min_sl_pct:
        return None, f"SL demasiado cerca ({sl_dist:.3%})"
    fee_to_risk = (2 * p.taker_fee) / sl_dist
    if fee_to_risk > p.max_fee_to_risk:
        return None, f"las comisiones se comen el {fee_to_risk:.0%} del riesgo (máximo {p.max_fee_to_risk:.0%})"

    margin = balance * p.margin_pct * (p.st_reduction if is_st else 1.0)
    max_loss = margin * p.max_loss_pct_of_margin

    asset_max = int(max_leverage) if max_leverage and max_leverage > 0 else p.leverage_cap
    leverage = min(p.leverage_cap, asset_max)
    if is_st:
        leverage = min(leverage, 5)
    leverage = max(1, min(max(leverage, p.min_leverage), asset_max))

    loss_per_notional = sl_dist + 2 * p.taker_fee + 2 * p.slippage_pct
    notional = min(max_loss / loss_per_notional, margin * leverage)
    contracts = notional / entry_price
    if limits is not None:
        contracts = floor_step(contracts, limits.qty_step)
        if contracts <= 0 or contracts < limits.min_qty:
            return None, f"tamaño ({contracts:g}) por debajo del mínimo del exchange ({limits.min_qty:g})"
        if limits.min_notional and contracts * entry_price < limits.min_notional:
            return None, f"valor de la orden por debajo del mínimo ({limits.min_notional:g} USDT)"
    if contracts <= 0:
        return None, "tamaño nulo"
    notional = contracts * entry_price

    r_dist = abs(entry_price - stop_loss)
    sign = 1 if side == LONG else -1
    step = limits.qty_step if limits else 0.0
    min_qty = limits.min_qty if limits else 0.0

    def _part(pct: float) -> float:
        qty = floor_step(contracts * pct, step)
        return qty if qty >= max(min_qty, 1e-12) else 0.0

    tp1_qty = _part(p.tp1_pct)
    tp2_qty = _part(p.tp2_pct)
    if tp1_qty + tp2_qty > contracts:
        tp2_qty = max(0.0, floor_step(contracts - tp1_qty, step))

    plan = PositionPlan(
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        stop_loss=stop_loss,
        contracts=contracts,
        notional=notional,
        margin_usd=margin,
        leverage=leverage,
        max_loss_usd=max_loss,
        risk_usd=notional * loss_per_notional,
        price_risk_usd=contracts * r_dist,
        sl_distance_pct=sl_dist,
        fee_to_risk=fee_to_risk,
        tp1_price=entry_price + sign * r_dist * p.tp1_r,
        tp1_qty=tp1_qty,
        tp2_price=entry_price + sign * r_dist * p.tp2_r,
        tp2_qty=tp2_qty,
        be_trigger=entry_price + sign * r_dist * p.be_r,
        be_price=entry_price * (1 + sign * p.be_offset_pct),
        is_st=is_st,
    )
    if target is not None:
        plan.target = float(target)
        plan = _to_zone(plan)
    return plan, "OK"


def _to_zone(plan: PositionPlan) -> PositionPlan:
    """Salida en la zona: todo en un solo objetivo, sin TP2 ni break-even."""
    return PositionPlan(**{**plan.to_dict(), "tp1_price": plan.target, "tp1_qty": plan.contracts,
                           "tp2_price": 0.0, "tp2_qty": 0.0, "be_trigger": 0.0})


def replan_from_fill(plan: PositionPlan, fill_price: float, contracts: float, p: RiskParams,
                     limits: Optional[MarketLimits] = None) -> Optional[PositionPlan]:
    """Recalcula TP/BE con el precio real de entrada (el SL estructural no se mueve)."""
    side = plan.side
    if (side == LONG and plan.stop_loss >= fill_price) or (side == SHORT and plan.stop_loss <= fill_price):
        return None
    if plan.target is not None and ((side == LONG and plan.target <= fill_price)
                                    or (side == SHORT and plan.target >= fill_price)):
        return None
    r_dist = abs(fill_price - plan.stop_loss)
    sign = 1 if side == LONG else -1
    step = limits.qty_step if limits else 0.0
    min_qty = limits.min_qty if limits else 0.0

    def _part(pct: float) -> float:
        qty = floor_step(contracts * pct, step)
        return qty if qty >= max(min_qty, 1e-12) else 0.0

    tp1_qty, tp2_qty = _part(p.tp1_pct), _part(p.tp2_pct)
    if tp1_qty + tp2_qty > contracts:
        tp2_qty = max(0.0, floor_step(contracts - tp1_qty, step))
    sl_dist = r_dist / fill_price
    loss_per_notional = sl_dist + 2 * p.taker_fee + 2 * p.slippage_pct
    return PositionPlan(
        **{**plan.to_dict(),
           "entry_price": fill_price,
           "contracts": contracts,
           "notional": contracts * fill_price,
           "risk_usd": contracts * fill_price * loss_per_notional,
           "price_risk_usd": contracts * r_dist,
           "sl_distance_pct": sl_dist,
           "fee_to_risk": (2 * p.taker_fee) / sl_dist if sl_dist else 0.0,
           "tp1_price": fill_price + sign * r_dist * p.tp1_r,
           "tp1_qty": tp1_qty,
           "tp2_price": fill_price + sign * r_dist * p.tp2_r,
           "tp2_qty": tp2_qty,
           "be_trigger": fill_price + sign * r_dist * p.be_r,
           "be_price": fill_price * (1 + sign * p.be_offset_pct)}
    ) if plan.target is None else _to_zone(PositionPlan(**{**plan.to_dict(), "entry_price": fill_price,
                                                             "contracts": contracts,
                                                             "notional": contracts * fill_price,
                                                             "risk_usd": contracts * fill_price * loss_per_notional,
                                                             "price_risk_usd": contracts * r_dist,
                                                             "sl_distance_pct": sl_dist,
                                                             "fee_to_risk": (2 * p.taker_fee) / sl_dist if sl_dist else 0.0,
                                                             "be_price": fill_price * (1 + sign * p.be_offset_pct)}))


# ---------------------------------------------------------------------------
# Gestión de salidas (motor común para simulación y backtest)
# ---------------------------------------------------------------------------
@dataclass
class ExitEvent:
    kind: str          # TP1, TP2, SL, BREAK_EVEN, TRAILING, SL_MOVE, CLOSE, FUNDING
    price: float
    qty: float
    fee: float
    pnl: float         # PnL bruto de precio de este fill (sin comisiones)
    note: str = ""


@dataclass
class PositionState:
    side: str
    entry_price: float
    contracts_initial: float
    contracts_open: float
    sl: float
    sl_initial: float
    tp1_price: float
    tp1_qty: float
    tp2_price: float
    tp2_qty: float
    be_trigger: float
    be_price: float
    tp1_filled: bool = False
    tp2_filled: bool = False
    be_set: bool = False
    trail_active: bool = False
    highest: float = 0.0
    lowest: float = 0.0
    realized_pnl: float = 0.0
    fees: float = 0.0
    funding: float = 0.0

    @classmethod
    def from_plan(cls, plan: PositionPlan, entry_fee: float = 0.0) -> "PositionState":
        return cls(
            side=plan.side, entry_price=plan.entry_price, contracts_initial=plan.contracts,
            contracts_open=plan.contracts, sl=plan.stop_loss, sl_initial=plan.stop_loss,
            tp1_price=plan.tp1_price, tp1_qty=plan.tp1_qty, tp2_price=plan.tp2_price, tp2_qty=plan.tp2_qty,
            be_trigger=plan.be_trigger, be_price=plan.be_price,
            highest=plan.entry_price, lowest=plan.entry_price, fees=entry_fee,
        )

    @classmethod
    def from_db(cls, row: Dict[str, Any]) -> "PositionState":
        return cls(
            side=row["side"], entry_price=row["entry_price"], contracts_initial=row["contracts_initial"],
            contracts_open=row["contracts_open"], sl=row["sl_current"], sl_initial=row["sl_initial"],
            tp1_price=row["tp1_price"] or 0.0, tp1_qty=row["tp1_qty"] or 0.0,
            tp2_price=row["tp2_price"] or 0.0, tp2_qty=row["tp2_qty"] or 0.0,
            be_trigger=row["be_trigger"] or 0.0, be_price=row["be_price"] or row["entry_price"],
            tp1_filled=bool(row["tp1_filled"]), tp2_filled=bool(row["tp2_filled"]),
            be_set=bool(row["be_set"]), trail_active=bool(row["trail_active"]),
            highest=row["highest"] or row["entry_price"], lowest=row["lowest"] or row["entry_price"],
            realized_pnl=row["realized_pnl"] or 0.0, fees=row["fees_paid"] or 0.0,
            funding=row["funding_paid"] or 0.0,
        )

    def db_fields(self) -> Dict[str, Any]:
        return {
            "contracts_open": self.contracts_open, "sl_current": self.sl,
            "tp1_filled": int(self.tp1_filled), "tp2_filled": int(self.tp2_filled),
            "be_set": int(self.be_set), "trail_active": int(self.trail_active),
            "highest": self.highest, "lowest": self.lowest,
            "realized_pnl": self.realized_pnl, "fees_paid": self.fees, "funding_paid": self.funding,
        }

    @property
    def is_long(self) -> bool:
        return self.side == LONG

    @property
    def closed(self) -> bool:
        return self.contracts_open <= 1e-12

    def unrealized(self, price: float) -> float:
        return price_pnl(self.side, self.entry_price, price, self.contracts_open)

    @property
    def net_pnl(self) -> float:
        return self.realized_pnl - self.fees - self.funding

    @property
    def r_value(self) -> float:
        return self.contracts_initial * abs(self.entry_price - self.sl_initial)


def price_pnl(side: str, entry: float, price: float, qty: float) -> float:
    return (price - entry) * qty if side == LONG else (entry - price) * qty


def stop_reason(state: PositionState) -> str:
    if state.trail_active:
        return "TRAILING"
    if state.be_set:
        return "BREAK_EVEN"
    return "SL"


def _better(side: str, new_sl: float, old_sl: float) -> bool:
    return new_sl > old_sl if side == LONG else new_sl < old_sl


def process_bar(state: PositionState, *, open_: float, high: float, low: float, close: float,
                atr: float, p: RiskParams) -> List[ExitEvent]:
    """
    Aplica una vela a la posición, en orden conservador:
      1) SL vigente al inicio de la vela (si la vela abre más allá del SL, sale en la apertura).
      2) TP1 y TP2 (órdenes límite).
      3) Break-even y trailing: se calculan con esta vela y valen desde la siguiente.
    """
    events = apply_exits(state, open_=open_, high=high, low=low, p=p)
    if not state.closed:
        events.extend(update_protection(state, high=high, low=low, atr=atr, p=p))
    return events


def apply_exits(state: PositionState, *, open_: float, high: float, low: float, p: RiskParams) -> List[ExitEvent]:
    """Pasos 1 y 2 de process_bar: stop y tomas parciales (lo que en real ejecuta el exchange)."""
    events: List[ExitEvent] = []
    if state.closed:
        return events
    long = state.is_long

    if (long and low <= state.sl) or (not long and high >= state.sl):
        base = min(open_, state.sl) if long else max(open_, state.sl)
        price = base * (1 - p.slippage_pct) if long else base * (1 + p.slippage_pct)
        qty = state.contracts_open
        fee = price * qty * p.taker_fee
        pnl = price_pnl(state.side, state.entry_price, price, qty)
        events.append(ExitEvent(stop_reason(state), price, qty, fee, pnl))
        state.realized_pnl += pnl
        state.fees += fee
        state.contracts_open = 0.0
        return events

    for name in ("tp1", "tp2"):
        filled = getattr(state, f"{name}_filled")
        qty = min(getattr(state, f"{name}_qty"), state.contracts_open)
        target = getattr(state, f"{name}_price")
        if filled or qty <= 0 or target <= 0:
            continue
        if (long and high >= target) or (not long and low <= target):
            fee = target * qty * p.maker_fee
            pnl = price_pnl(state.side, state.entry_price, target, qty)
            events.append(ExitEvent(name.upper(), target, qty, fee, pnl))
            state.realized_pnl += pnl
            state.fees += fee
            state.contracts_open -= qty
            setattr(state, f"{name}_filled", True)
    if state.contracts_open <= 1e-12:
        state.contracts_open = 0.0
    return events


def update_protection(state: PositionState, *, high: float, low: float, atr: float,
                      p: RiskParams) -> List[ExitEvent]:
    """Paso 3: actualiza extremos, break-even y trailing. Devuelve los movimientos de SL."""
    events: List[ExitEvent] = []
    if state.closed:
        return events
    long = state.is_long
    state.highest = max(state.highest, high)
    state.lowest = min(state.lowest, low)

    if not state.be_set and state.be_trigger > 0:
        if (long and high >= state.be_trigger) or (not long and low <= state.be_trigger):
            state.be_set = True
            if _better(state.side, state.be_price, state.sl):
                state.sl = state.be_price
                events.append(ExitEvent("SL_MOVE", state.sl, 0.0, 0.0, 0.0, "break-even"))

    if state.tp1_filled and atr and atr > 0 and p.trail_atr_mult > 0:
        candidate = state.highest - p.trail_atr_mult * atr if long else state.lowest + p.trail_atr_mult * atr
        if _better(state.side, candidate, state.sl):
            state.sl = candidate
            state.trail_active = True
            events.append(ExitEvent("SL_MOVE", state.sl, 0.0, 0.0, 0.0, "trailing"))
    return events


def close_at(state: PositionState, price: float, p: RiskParams, kind: str = "CLOSE",
             taker: bool = True) -> ExitEvent:
    """Cierra a mercado lo que quede (fin de datos, cierre manual)."""
    qty = state.contracts_open
    fill = price * (1 - p.slippage_pct) if state.is_long else price * (1 + p.slippage_pct)
    fee = fill * qty * (p.taker_fee if taker else p.maker_fee)
    pnl = price_pnl(state.side, state.entry_price, fill, qty)
    state.realized_pnl += pnl
    state.fees += fee
    state.contracts_open = 0.0
    return ExitEvent(kind, fill, qty, fee, pnl)


def in_profit(side: str, entry: float, price: float, slippage: float = 0.0) -> bool:
    """¿Cerrar ahora a mercado (con su deslizamiento) daría ganancia de precio frente a la entrada?"""
    fill = price * (1 - slippage) if side == LONG else price * (1 + slippage)
    return price_pnl(side, entry, fill, 1.0) > 0


def opposite_exit(state: PositionState, price: float, p: RiskParams, kind: str) -> Optional[ExitEvent]:
    """Aviso contrario (VERDE para cortos, TOPE para largos): cierra lo que quede si va ganando.
    Se usa igual en el bot, en el backtest (apertura de la vela siguiente) y en el seguimiento en sombra."""
    if state.closed or not in_profit(state.side, state.entry_price, price, p.slippage_pct):
        return None
    return close_at(state, price, p, kind=kind)


def funding_cost(side: str, contracts: float, mark_price: float, rate: float) -> float:
    """Costo de funding (positivo = se paga). Con tasa positiva pagan los LONG y cobran los SHORT."""
    notional = contracts * mark_price
    return notional * rate if side == LONG else -notional * rate


# ---------------------------------------------------------------------------
# Estado persistente: kill switch, cooldowns, entradas diarias
# ---------------------------------------------------------------------------
class MemoryStore:
    """Almacén en memoria con la misma interfaz que Database (lo usa el backtest)."""

    def __init__(self):
        self._data: Dict[str, Any] = {}

    def get_state(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set_state(self, key: str, value: Any) -> None:
        self._data[key] = value


def _week_id(now: datetime) -> str:
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


class KillSwitch:
    def __init__(self, store, mode: str, daily_limit: float, weekly_limit: float):
        self.store = store
        self.key = f"{mode}:kill_switch"
        self.daily_limit = daily_limit
        self.weekly_limit = weekly_limit

    def evaluate(self, equity: float, now: Optional[datetime] = None) -> Tuple[bool, str]:
        now = now or datetime.now(timezone.utc)
        s = dict(self.store.get_state(self.key, {}) or {})
        day, week = now.strftime("%Y-%m-%d"), _week_id(now)
        if s.get("day") != day:
            s.update(day=day, day_start=equity)
        if s.get("week") != week:
            s.update(week=week, week_start=equity)
        day_start = float(s.get("day_start") or equity)
        week_start = float(s.get("week_start") or equity)
        daily_dd = (day_start - equity) / day_start * 100 if day_start > 0 else 0.0
        weekly_dd = (week_start - equity) / week_start * 100 if week_start > 0 else 0.0
        s.update(daily_dd=round(daily_dd, 2), weekly_dd=round(weekly_dd, 2), equity=equity)

        blocked_until = s.get("blocked_until")
        if blocked_until and now < datetime.fromisoformat(blocked_until):
            self.store.set_state(self.key, s)
            return True, s.get("reason", "Kill switch activo")

        s.update(blocked_until=None, reason="")
        if weekly_dd >= self.weekly_limit:
            monday = (now + timedelta(days=7 - now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            s.update(blocked_until=monday.isoformat(),
                     reason=f"Pérdida semanal {weekly_dd:.1f}% ≥ {self.weekly_limit}%")
        elif daily_dd >= self.daily_limit:
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            s.update(blocked_until=tomorrow.isoformat(),
                     reason=f"Pérdida diaria {daily_dd:.1f}% ≥ {self.daily_limit}%")
        self.store.set_state(self.key, s)
        return bool(s.get("blocked_until")), s.get("reason", "")

    def status(self) -> Dict[str, Any]:
        return dict(self.store.get_state(self.key, {}) or {})


class EntryGuard:
    """Cooldown por símbolo tras cerrar y máximo de entradas diarias por símbolo (persistentes)."""

    def __init__(self, store, mode: str, cooldown_minutes: int, max_entries_daily: int):
        self.store = store
        self.mode = mode
        self.cooldown = timedelta(minutes=cooldown_minutes)
        self.max_entries_daily = max_entries_daily

    def _cooldowns(self) -> Dict[str, str]:
        return dict(self.store.get_state(f"{self.mode}:cooldowns", {}) or {})

    def _entries(self, now: datetime) -> Dict[str, Any]:
        data = dict(self.store.get_state(f"{self.mode}:entries", {}) or {})
        today = now.strftime("%Y-%m-%d")
        if data.get("date") != today:
            data = {"date": today, "counts": {}}
        return data

    def check(self, symbol: str, now: Optional[datetime] = None) -> Tuple[bool, str]:
        now = now or datetime.now(timezone.utc)
        last_close = self._cooldowns().get(symbol)
        if last_close and now - datetime.fromisoformat(last_close) < self.cooldown:
            remaining = self.cooldown - (now - datetime.fromisoformat(last_close))
            return False, f"cooldown ({int(remaining.total_seconds() // 60)} min restantes)"
        count = self._entries(now)["counts"].get(symbol, 0)
        if count >= self.max_entries_daily:
            return False, f"máximo de {self.max_entries_daily} entradas diarias en {symbol}"
        return True, "OK"

    def register_entry(self, symbol: str, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        data = self._entries(now)
        data["counts"][symbol] = data["counts"].get(symbol, 0) + 1
        self.store.set_state(f"{self.mode}:entries", data)

    def register_close(self, symbol: str, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        cooldowns = self._cooldowns()
        cooldowns[symbol] = now.isoformat()
        cutoff = now - self.cooldown * 2
        cooldowns = {k: v for k, v in cooldowns.items() if datetime.fromisoformat(v) >= cutoff}
        self.store.set_state(f"{self.mode}:cooldowns", cooldowns)
