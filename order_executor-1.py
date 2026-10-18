"""
order_executor.py — Ejecución de órdenes (v9)
=============================================
Dos "brokers" con la misma interfaz:

PaperBroker (DRY_RUN)
  Simula con precios reales de Bybit: entrada con deslizamiento, comisiones taker/maker,
  funding cada vez que Bybit liquida, SL/TP revisados con velas de 1 minuto (detecta mechas).
  El PnL es contratos × diferencia de precio − costos (la v7.3 lo inflaba).

ExchangeBroker (DEMO, TESTNET, LIVE)
  - Entrada a mercado con el SL ADJUNTO. Si Bybit la rechaza, no se abre nada (nunca sin SL).
  - Confirma la posición con fetch_positions (formato de símbolo correcto).
  - Si por algún motivo la posición quedara sin SL, lo pone; si no puede, la cierra.
  - TP1 y TP2 como órdenes límite reduce-only (se ejecutan aunque el bot esté caído).
  - Break-even y trailing se envían a Bybit con trading-stop.
  - Conciliación en cada ciclo y al arrancar: nunca borra una posición sin confirmar con Bybit;
    si Bybit la cerró (SL/TP), registra el PnL real (closed-pnl) en la base de datos;
    si encuentra posiciones que no abrió el bot, las muestra sin tocarlas.

v9: cada posición guarda su flecha (setup) y variante, y close_now() cierra a mercado lo que quede
cuando aparece el aviso contrario (VERDE cierra cortos, TOPE cierra largos) — ver main.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import ccxt
import pandas as pd

import strategy
from bybit_api_manager import BybitAPIManager, DataUnavailable
from config import Config
from database import Database, iso
from risk_manager import (
    LONG, SHORT, PositionPlan, PositionState, RiskParams, _better, close_at, funding_cost,
    price_pnl, process_bar, replan_from_fill, round_tick, stop_reason, update_protection,
)

logger = logging.getLogger("OrderExecutor")

EXIT_KINDS = ("TP1", "TP2", "SL", "BREAK_EVEN", "TRAILING", "CLOSE", "MANUAL", "CERRADA_EN_BYBIT",
              "SIN_SL", "ENTRADA_TRAS_SL", "SALIDA_VERDE", "SALIDA_TOPE")
OnClose = Callable[[Dict[str, Any]], Any]


def now_ms() -> int:
    return int(time.time() * 1000)


def ceil_minute(ms: int) -> int:
    return ((ms + 59_999) // 60_000) * 60_000


def parse_iso_ms(value: Optional[str]) -> int:
    if not value:
        return now_ms()
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def _meta(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(row.get("meta") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def exit_price_avg(fills: List[Dict[str, Any]]) -> Optional[float]:
    exits = [f for f in fills if f["kind"] in EXIT_KINDS and (f["qty"] or 0) > 0 and f["price"]]
    qty = sum(f["qty"] for f in exits)
    if qty <= 0:
        return None
    return sum(f["price"] * f["qty"] for f in exits) / qty


def plan_from_row(row: Dict[str, Any]) -> PositionPlan:
    return PositionPlan(
        symbol=row["symbol"], side=row["side"], entry_price=row["entry_price"], stop_loss=row["sl_initial"],
        contracts=row["contracts_initial"], notional=row["contracts_initial"] * row["entry_price"],
        margin_usd=row["margin_usd"] or 0.0, leverage=row["leverage"] or 1, max_loss_usd=0.0,
        risk_usd=row["risk_usd"] or 0.0,
        price_risk_usd=row["contracts_initial"] * abs(row["entry_price"] - row["sl_initial"]),
        sl_distance_pct=abs(row["entry_price"] - row["sl_initial"]) / row["entry_price"], fee_to_risk=0.0,
        tp1_price=row["tp1_price"] or 0.0, tp1_qty=row["tp1_qty"] or 0.0,
        tp2_price=row["tp2_price"] or 0.0, tp2_qty=row["tp2_qty"] or 0.0,
        be_trigger=row["be_trigger"] or 0.0, be_price=row["be_price"] or row["entry_price"],
    )


class _BaseBroker:
    def __init__(self, api: BybitAPIManager, db: Database, cfg: Config, rp: RiskParams,
                 on_close: Optional[OnClose] = None):
        self.api = api
        self.db = db
        self.cfg = cfg
        self.rp = rp
        self.mode = cfg.mode
        self.on_close = on_close
        self.last_prices: Dict[str, float] = {}
        self._atr_cache: Dict[str, tuple] = {}
        self._funding_seen: Dict[str, float] = {}

    # --------------------------------------------------------------
    async def _atr(self, symbol: str) -> float:
        cached = self._atr_cache.get(symbol)
        if cached and time.time() - cached[1] < 300:
            return cached[0]
        df = await self.api.fetch_ohlcv(symbol, self.cfg.entry_timeframe, limit=200)
        ind = strategy.add_indicators(df)
        atr = float(ind["atr"].iloc[-1]) if len(ind) and pd.notna(ind["atr"].iloc[-1]) else 0.0
        self._atr_cache[symbol] = (atr, time.time())
        return atr

    async def _minute_bars(self, symbol: str, since_ms: Optional[int]) -> pd.DataFrame:
        if not since_ms:
            return pd.DataFrame()
        last_closed = (now_ms() // 60_000) * 60_000 - 60_000
        if since_ms > last_closed:
            return pd.DataFrame()
        df = await self.api.fetch_ohlcv_range(symbol, "1m", since_ms=int(since_ms))
        if df.empty:
            return df
        return df[df.index >= pd.Timestamp(int(since_ms), unit="ms", tz="UTC")]

    def _record(self, position_id: int, events, ts: Optional[str] = None) -> None:
        for ev in events:
            self.db.add_fill(position_id, ev.kind, ev.price, ev.qty, ev.fee, ev.pnl, ev.note, ts=ts)

    def _finalize(self, row: Dict[str, Any], state: PositionState, exit_reason: str,
                  exchange_pnl: Optional[float] = None) -> None:
        fills = self.db.fills_for(row["id"])
        net = exchange_pnl if exchange_pnl is not None else state.net_pnl
        r_value = state.r_value
        r_mult = net / r_value if r_value > 0 else None
        self.db.update_position(
            row["id"], **state.db_fields(), status="CLOSED", closed_at=iso(),
            exit_price_avg=exit_price_avg(fills), exit_reason=exit_reason,
            net_pnl=round(net, 6), r_multiple=round(r_mult, 4) if r_mult is not None else None,
            exchange_pnl=exchange_pnl,
        )
        r_text = f" ({r_mult:+.2f}R)" if r_mult is not None else ""
        self.db.add_event(self.mode, "INFO",
                          f"Cerrada {row['symbol']} {row['side']}: {exit_reason} | PnL neto {net:+.2f} USDT{r_text}")
        if self.on_close:
            try:
                self.on_close(self.db.get_position(row["id"]))
            except Exception as exc:  # pragma: no cover
                logger.exception(f"[Executor] Error en on_close: {exc}")

    def _note_funding_rate(self, symbol: str, rate: float) -> None:
        self._funding_seen[symbol] = rate


# ======================================================================
# DRY_RUN
# ======================================================================
class PaperBroker(_BaseBroker):
    """Simulador realista con precios en vivo de Bybit."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.balance_key = f"{self.mode}:paper_balance"
        if self.db.get_state(self.balance_key) is None:
            self.db.set_state(self.balance_key, float(self.cfg.paper_balance))
            self.db.set_state(f"{self.mode}:paper_initial", float(self.cfg.paper_balance))

    def balance(self) -> float:
        return float(self.db.get_state(self.balance_key, self.cfg.paper_balance))

    def _add_cash(self, delta: float) -> None:
        if delta:
            self.db.set_state(self.balance_key, self.balance() + delta)

    async def startup(self) -> None:
        n = len(self.db.open_positions(self.mode))
        self.db.add_event(self.mode, "INFO", f"Simulación (DRY_RUN) iniciada. Saldo {self.balance():.2f} USDT, "
                                             f"{n} posiciones simuladas abiertas.")

    async def equity(self) -> Dict[str, float]:
        balance = self.balance()
        upnl = 0.0
        for row in self.db.open_positions(self.mode):
            price = self.last_prices.get(row["symbol"])
            if price is None:
                try:
                    price = (await self.api.fetch_ticker(row["symbol"]))["last"]
                    self.last_prices[row["symbol"]] = price
                except Exception:
                    price = row["entry_price"]
            upnl += price_pnl(row["side"], row["entry_price"], price, row["contracts_open"])
        return {"equity": balance + upnl, "balance": balance, "upnl": upnl, "available": balance}

    async def open_position(self, plan: PositionPlan, signal, meta: Optional[Dict[str, Any]] = None) -> Optional[int]:
        meta = dict(meta or {})
        slip = self.rp.slippage_pct
        fill = plan.entry_price * (1 + slip) if plan.side == LONG else plan.entry_price * (1 - slip)
        limits = self.api.limits(plan.symbol)
        new_plan = replan_from_fill(plan, fill, plan.contracts, self.rp, limits)
        if new_plan is None:
            self.db.add_event(self.mode, "WARNING", f"{plan.symbol}: el precio ya pasó el SL; no se entra.")
            return None
        fee = fill * new_plan.contracts * self.rp.taker_fee
        pid = self.db.insert_position(
            mode=self.mode, symbol=plan.symbol, side=plan.side, status="OPEN", origin="BOT",
            timeframe=signal.timeframe, entry_price=fill, contracts_initial=new_plan.contracts,
            contracts_open=new_plan.contracts, leverage=new_plan.leverage, margin_usd=new_plan.margin_usd,
            risk_usd=new_plan.risk_usd, sl_initial=new_plan.stop_loss, sl_current=new_plan.stop_loss,
            tp1_price=new_plan.tp1_price, tp1_qty=new_plan.tp1_qty, tp2_price=new_plan.tp2_price,
            tp2_qty=new_plan.tp2_qty, be_trigger=new_plan.be_trigger, be_price=new_plan.be_price,
            highest=fill, lowest=fill, atr_entry=signal.atr, fees_paid=fee, signal_id=signal.signal_id,
            pattern_id=signal.pattern_id, setup=getattr(signal, "setup", None),
            variant=getattr(signal, "variant", None), score=signal.score, last_check_ms=ceil_minute(now_ms()),
            next_funding_ms=meta.get("next_funding_ms"), meta=meta,
        )
        self.db.add_fill(pid, "ENTRY", fill, new_plan.contracts, fee, 0.0, "entrada simulada a mercado")
        self._add_cash(-fee)
        self.db.add_event(self.mode, "INFO",
                          f"[SIM] Abierta {plan.symbol} {plan.side} @ {fill:.6g} | SL {new_plan.stop_loss:.6g} | "
                          f"TP1 {new_plan.tp1_price:.6g} | TP2 {new_plan.tp2_price:.6g} | riesgo {new_plan.risk_usd:.2f} USDT")
        return pid

    async def manage(self) -> None:
        for row in self.db.open_positions(self.mode):
            try:
                await self._manage_one(row)
            except (DataUnavailable, ccxt.NetworkError, ccxt.ExchangeError) as exc:
                logger.warning(f"[SIM] {row['symbol']}: sin datos este ciclo ({exc}). No se toca la posición.")

    async def _manage_one(self, row: Dict[str, Any]) -> None:
        symbol = row["symbol"]
        state = PositionState.from_db(row)
        before_pnl, before_fees = state.realized_pnl, state.fees
        atr = await self._atr(symbol)
        last_check = row["last_check_ms"]
        bars = await self._minute_bars(symbol, last_check)
        for ts, bar in bars.iterrows():
            events = process_bar(state, open_=bar["open"], high=bar["high"], low=bar["low"],
                                 close=bar["close"], atr=atr, p=self.rp)
            bar_close = ts + pd.Timedelta(minutes=1)
            self._record(row["id"], events, ts=iso(bar_close.to_pydatetime()))
            last_check = int(bar_close.value // 10**6)
            if state.closed:
                break

        next_funding = row["next_funding_ms"]
        if not state.closed:
            ticker = await self.api.fetch_ticker(symbol)
            last = ticker["last"]
            self.last_prices[symbol] = last
            self._record(row["id"], process_bar(state, open_=last, high=last, low=last, close=last, atr=atr, p=self.rp))
            if not state.closed and next_funding and now_ms() >= next_funding:
                rate = self._funding_seen.get(symbol, ticker["funding_rate"])
                cost = funding_cost(state.side, state.contracts_open, ticker["mark"], rate)
                state.funding += cost
                self._add_cash(-cost)
                self.db.add_fill(row["id"], "FUNDING", ticker["mark"], state.contracts_open, 0.0, -cost,
                                 f"tasa {rate:.4%}")
                next_funding = ticker["next_funding_ms"] if ticker["next_funding_ms"] > next_funding \
                    else next_funding + 8 * 3_600_000
            elif not next_funding:
                next_funding = ticker["next_funding_ms"] or None
            self._note_funding_rate(symbol, ticker["funding_rate"])

        self._add_cash((state.realized_pnl - before_pnl) - (state.fees - before_fees))
        self.db.update_position(row["id"], **state.db_fields(), last_check_ms=last_check, next_funding_ms=next_funding)
        if state.closed:
            fills = self.db.fills_for(row["id"])
            reason = next((f["kind"] for f in reversed(fills) if f["kind"] in EXIT_KINDS), "CLOSE")
            self._finalize(self.db.get_position(row["id"]), state, reason)

    async def close_now(self, row: Dict[str, Any], reason: str, note: str = "") -> bool:
        """Cierra a mercado lo que quede (aviso contrario). Devuelve True si la cerró el bot."""
        await self._manage_one(row)          # primero lo ocurrido hasta ahora (SL/TP con velas de 1 minuto)
        row = self.db.get_position(row["id"])
        if row is None or row["status"] != "OPEN":
            return False
        state = PositionState.from_db(row)
        before_pnl, before_fees = state.realized_pnl, state.fees
        price = (await self.api.fetch_ticker(row["symbol"]))["last"]
        event = close_at(state, price, self.rp, kind=reason)
        event.note = note or "cierre a mercado simulado"
        self._record(row["id"], [event])
        self._add_cash((state.realized_pnl - before_pnl) - (state.fees - before_fees))
        self.db.update_position(row["id"], **state.db_fields())
        self._finalize(self.db.get_position(row["id"]), state, reason)
        return True


# ======================================================================
# DEMO / TESTNET / LIVE
# ======================================================================
class ExchangeBroker(_BaseBroker):
    PENDING_TIMEOUT_MS = 3 * 60_000

    def _safe_qty(self, symbol: str, qty: float) -> float:
        """Cantidad ajustada al paso del exchange; 0 si no llega al mínimo (nunca lanza error)."""
        if qty <= 0:
            return 0.0
        try:
            return self.api.amount_to_precision(symbol, qty)
        except Exception:
            return 0.0

    async def startup(self) -> None:
        account = await self.api.detect_account()
        await self.api.ensure_one_way_mode()
        self.db.add_event(self.mode, "INFO", f"Conectado a Bybit ({self.mode}), cuenta {account}.")
        await self.manage()
        n = len(self.db.open_positions(self.mode))
        self.db.add_event(self.mode, "INFO", f"Conciliación al arrancar: {n} posiciones abiertas registradas.")

    async def equity(self) -> Dict[str, float]:
        eq = await self.api.get_equity()
        return {"equity": eq["equity"], "balance": eq["wallet"], "upnl": eq["upnl"], "available": eq["available"]}

    # --------------------------------------------------------------
    async def open_position(self, plan: PositionPlan, signal, meta: Optional[Dict[str, Any]] = None) -> Optional[int]:
        symbol, side = plan.symbol, plan.side
        try:
            live = await self.api.fetch_open_positions()
        except Exception as exc:
            self.db.add_event(self.mode, "WARNING", f"{symbol}: no se pudieron leer las posiciones ({exc}); no se entra.")
            return None
        if any(p["symbol"] == symbol for p in live):
            self.db.add_event(self.mode, "WARNING", f"{symbol}: ya hay una posición en Bybit; no se abre otra.")
            return None
        limits = self.api.limits(symbol)
        qty = self._safe_qty(symbol, plan.contracts)
        if qty <= 0 or qty < limits.min_qty:
            self.db.add_event(self.mode, "WARNING", f"{symbol}: cantidad {qty} por debajo del mínimo; no se entra.")
            return None
        try:
            await self.api.set_leverage(symbol, plan.leverage)
        except Exception as exc:
            self.db.add_event(self.mode, "WARNING", f"{symbol}: no se pudo fijar {plan.leverage}x ({exc}); "
                                                    "se usa el apalancamiento que tenga la cuenta.")
        client_id = f"unico-{uuid.uuid4().hex[:24]}"
        meta = {**(meta or {}), "plan": plan.to_dict()}
        pid = self.db.insert_position(
            mode=self.mode, symbol=symbol, side=side, status="PENDING", origin="BOT",
            timeframe=signal.timeframe, entry_price=plan.entry_price, contracts_initial=qty, contracts_open=qty,
            leverage=plan.leverage, margin_usd=plan.margin_usd, risk_usd=plan.risk_usd,
            sl_initial=plan.stop_loss, sl_current=plan.stop_loss, tp1_price=plan.tp1_price, tp1_qty=plan.tp1_qty,
            tp2_price=plan.tp2_price, tp2_qty=plan.tp2_qty, be_trigger=plan.be_trigger, be_price=plan.be_price,
            highest=plan.entry_price, lowest=plan.entry_price, atr_entry=signal.atr, signal_id=signal.signal_id,
            pattern_id=signal.pattern_id, setup=getattr(signal, "setup", None),
            variant=getattr(signal, "variant", None), score=signal.score, client_order_id=client_id, meta=meta,
        )
        try:
            await self.api.create_entry_order(symbol, side, qty, plan.stop_loss, client_id)
        except ccxt.NetworkError as exc:
            self.db.add_event(self.mode, "WARNING", f"{symbol}: no hubo respuesta al enviar la orden ({exc}). "
                                                    "Se comprobará en el próximo ciclo.")
            return None
        except ccxt.ExchangeError as exc:
            self.db.update_position(pid, status="CANCELLED", closed_at=iso(), exit_reason="RECHAZADA")
            self.db.add_event(self.mode, "ERROR", f"Bybit rechazó la entrada en {symbol}; no se abrió nada: {exc}")
            return None

        position = await self._wait_position(symbol, side)
        if position is None:
            self.db.add_event(self.mode, "WARNING", f"{symbol}: orden enviada pero la posición aún no aparece. "
                                                    "Se revisará en el próximo ciclo.")
            return None
        return await self._confirm_open(self.db.get_position(pid), position, plan)

    async def _wait_position(self, symbol: str, side: str, attempts: int = 8) -> Optional[Dict[str, Any]]:
        for i in range(attempts):
            await asyncio.sleep(0.5 + 0.25 * i)
            for p in await self.api.fetch_open_positions():
                if p["symbol"] == symbol and p["side"] == side:
                    return p
        return None

    async def _confirm_open(self, row: Dict[str, Any], position: Dict[str, Any], plan: PositionPlan) -> Optional[int]:
        pid, symbol, side = row["id"], plan.symbol, plan.side
        meta = _meta(row)
        limits = self.api.limits(symbol)
        fill, contracts = position["entry_price"], position["contracts"]
        position_idx = position.get("position_idx", 0)
        meta["position_idx"] = position_idx

        if not position.get("stop_loss"):
            try:
                await self.api.set_trading_stop(symbol, stop_loss=plan.stop_loss, position_idx=position_idx)
                self.db.add_event(self.mode, "WARNING", f"{symbol}: la posición llegó sin SL y se le puso.")
            except Exception as exc:
                self.db.add_event(self.mode, "CRITICAL", f"{symbol}: no se pudo poner el SL ({exc}). "
                                                         "Se cierra la posición por seguridad.")
                await self._emergency_close(row, contracts, "SIN_SL", meta)
                return None

        new_plan = replan_from_fill(plan, fill, contracts, self.rp, limits)
        if new_plan is None:
            self.db.add_event(self.mode, "WARNING", f"{symbol}: el precio de entrada ya superó el SL; se cierra.")
            await self._emergency_close(row, contracts, "ENTRADA_TRAS_SL", meta)
            return None

        tp_orders: Dict[str, Optional[str]] = {}
        for n in (1, 2):
            qty_n = self._safe_qty(symbol, getattr(new_plan, f"tp{n}_qty"))
            if qty_n <= 0 or qty_n < limits.min_qty:
                continue
            try:
                order = await self.api.create_reduce_only_limit(
                    symbol, side, qty_n, getattr(new_plan, f"tp{n}_price"), f"{row['client_order_id']}-tp{n}")
                tp_orders[f"tp{n}"] = order.get("id")
            except Exception as exc:
                self.db.add_event(self.mode, "WARNING", f"{symbol}: no se pudo colocar el TP{n} ({exc}). "
                                                        "El SL sigue activo; se reintentará.")
        meta["tp_orders"] = tp_orders
        fee_est = fill * contracts * self.rp.taker_fee
        self.db.update_position(
            pid, status="OPEN", opened_at=iso(), entry_price=fill, contracts_initial=contracts,
            contracts_open=contracts, risk_usd=new_plan.risk_usd, tp1_price=new_plan.tp1_price,
            tp1_qty=new_plan.tp1_qty, tp2_price=new_plan.tp2_price, tp2_qty=new_plan.tp2_qty,
            be_trigger=new_plan.be_trigger, be_price=new_plan.be_price, highest=fill, lowest=fill,
            fees_paid=fee_est, last_check_ms=ceil_minute(now_ms()), meta=meta,
        )
        self.db.add_fill(pid, "ENTRY", fill, contracts, fee_est, 0.0, "precio real de Bybit")
        self.db.add_event(self.mode, "INFO",
                          f"Abierta {symbol} {side} @ {fill:.6g} | SL {plan.stop_loss:.6g} | "
                          f"TP1 {new_plan.tp1_price:.6g} | TP2 {new_plan.tp2_price:.6g}")
        return pid

    async def _emergency_close(self, row: Dict[str, Any], contracts: float, reason: str,
                               meta: Dict[str, Any]) -> None:
        meta = {**meta, "forced_reason": reason}
        self.db.update_position(row["id"], status="OPEN", meta=meta)
        try:
            await self.api.close_market(row["symbol"], row["side"], contracts)
        except Exception as exc:
            self.db.add_event(self.mode, "CRITICAL", f"¡No se pudo cerrar {row['symbol']}! Revísalo en Bybit: {exc}")

    async def close_now(self, row: Dict[str, Any], reason: str, note: str = "") -> bool:
        """Cierra a mercado lo que quede (aviso contrario). El cierre se registra al confirmarlo Bybit."""
        symbol = row["symbol"]
        try:
            live = await self.api.fetch_open_positions()
        except Exception as exc:
            self.db.add_event(self.mode, "WARNING", f"{symbol}: no se pudo leer la posición para cerrarla ({exc}). "
                                                    "Sigue protegida por su SL; se reintentará.")
            return False
        position = next((p for p in live if p["symbol"] == symbol and p["side"] == row["side"]), None)
        if position is None:      # ya la cerró Bybit (SL o TP): se registra como tal
            await self._finalize_from_exchange(row)
            return False
        meta = {**_meta(row), "forced_reason": reason}
        self.db.update_position(row["id"], meta=meta)
        try:
            await self.api.close_market(symbol, row["side"], position["contracts"])
        except Exception as exc:
            meta.pop("forced_reason", None)
            self.db.update_position(row["id"], meta=meta)
            self.db.add_event(self.mode, "ERROR", f"{symbol}: no se pudo cerrar por {reason} ({exc}). "
                                                  "Sigue protegida por su SL.")
            return False
        self.db.add_event(self.mode, "INFO", f"{symbol}: cierre a mercado enviado ({reason}). {note}".strip())
        return True

    # --------------------------------------------------------------
    async def manage(self) -> None:
        try:
            live = await self.api.fetch_open_positions()
        except Exception as exc:
            logger.warning(f"[Bybit] No se pudieron leer las posiciones ({exc}). Ciclo sin cambios.")
            return
        live_by_symbol = {p["symbol"]: p for p in live}
        handled: set = set()
        for row in self.db.active_positions(self.mode):
            symbol = row["symbol"]
            position = live_by_symbol.get(symbol)
            try:
                if row["status"] == "PENDING":
                    handled.add(symbol)
                    await self._resolve_pending(row, position)
                    continue
                if position is None or position["side"] != row["side"]:
                    await self._finalize_from_exchange(row)
                    continue
                handled.add(symbol)
                if row["origin"] == "EXTERNAL":
                    self.db.update_position(row["id"], contracts_open=position["contracts"],
                                            sl_current=position.get("stop_loss") or 0.0)
                    self.last_prices[symbol] = position["mark_price"]
                    continue
                await self._sync_open(row, position)
            except (DataUnavailable, ccxt.NetworkError) as exc:
                logger.warning(f"[Bybit] {symbol}: sin datos este ciclo ({exc}).")
            except Exception as exc:
                logger.exception(f"[Bybit] Error gestionando {symbol}: {exc}")
                self.db.add_event(self.mode, "ERROR", f"Error gestionando {symbol}: {exc}")

        for symbol, position in live_by_symbol.items():
            if symbol not in handled and self.db.open_position_for(self.mode, symbol) is None:
                self._adopt_external(position)

    async def _resolve_pending(self, row: Dict[str, Any], position: Optional[Dict[str, Any]]) -> None:
        if position is not None and position["side"] == row["side"]:
            await self._confirm_open(row, position, plan_from_row(row))
            return
        if now_ms() - parse_iso_ms(row["opened_at"]) > self.PENDING_TIMEOUT_MS:
            self.db.update_position(row["id"], status="CANCELLED", closed_at=iso(), exit_reason="NO_EJECUTADA")
            self.db.add_event(self.mode, "WARNING", f"{row['symbol']}: la orden de entrada no llegó a ejecutarse.")

    def _adopt_external(self, position: Dict[str, Any]) -> None:
        self.db.insert_position(
            mode=self.mode, symbol=position["symbol"], side=position["side"], status="OPEN", origin="EXTERNAL",
            entry_price=position["entry_price"], contracts_initial=position["contracts"],
            contracts_open=position["contracts"], leverage=int(position.get("leverage") or 0),
            sl_initial=position.get("stop_loss") or 0.0, sl_current=position.get("stop_loss") or 0.0,
            highest=position["entry_price"], lowest=position["entry_price"],
            meta={"nota": "posición encontrada en Bybit que el bot no abrió"},
        )
        warning = "" if position.get("stop_loss") else " ¡NO TIENE STOP-LOSS!"
        self.db.add_event(self.mode, "WARNING",
                          f"Posición en Bybit que no abrió el bot: {position['symbol']} {position['side']} "
                          f"{position['contracts']}. Se muestra pero no se toca.{warning}")

    async def _sync_open(self, row: Dict[str, Any], position: Dict[str, Any]) -> None:
        symbol = row["symbol"]
        state = PositionState.from_db(row)
        meta = _meta(row)
        tp_orders = meta.get("tp_orders") or {}
        limits = self.api.limits(symbol)

        # 1) Reducciones: TP ejecutados en Bybit o cierres manuales parciales
        if position["contracts"] < state.contracts_open - 1e-12:
            reduced = state.contracts_open - position["contracts"]
            open_ids = {o.get("id") for o in await self.api.fetch_open_orders(symbol)}
            for n in (1, 2):
                if reduced <= 1e-12:
                    break
                order_id = tp_orders.get(f"tp{n}")
                if getattr(state, f"tp{n}_filled") or not order_id or order_id in open_ids:
                    continue
                qty = min(getattr(state, f"tp{n}_qty"), reduced)
                price = getattr(state, f"tp{n}_price")
                fee = price * qty * self.rp.maker_fee
                pnl = price_pnl(state.side, state.entry_price, price, qty)
                state.realized_pnl += pnl
                state.fees += fee
                setattr(state, f"tp{n}_filled", True)
                reduced -= qty
                self.db.add_fill(row["id"], f"TP{n}", price, qty, fee, pnl, "ejecutado en Bybit")
            if reduced > 1e-9:
                price = position["mark_price"]
                fee = price * reduced * self.rp.taker_fee
                pnl = price_pnl(state.side, state.entry_price, price, reduced)
                state.realized_pnl += pnl
                state.fees += fee
                self.db.add_fill(row["id"], "MANUAL", price, reduced, fee, pnl, "reducción no hecha por el bot")
            state.contracts_open = position["contracts"]
        elif position["contracts"] > state.contracts_open + 1e-9:
            self.db.add_event(self.mode, "WARNING", f"{symbol}: la posición creció fuera del bot "
                                                    f"({state.contracts_open} → {position['contracts']}).")
            state.contracts_open = position["contracts"]

        # 2) Extremos de precio (velas de 1 minuto + último precio) → break-even y trailing
        atr = await self._atr(symbol)
        last_check = row["last_check_ms"]
        moves = []
        for ts, bar in (await self._minute_bars(symbol, last_check)).iterrows():
            moves += update_protection(state, high=bar["high"], low=bar["low"], atr=atr, p=self.rp)
            last_check = int((ts + pd.Timedelta(minutes=1)).value // 10**6)
        ticker = await self.api.fetch_ticker(symbol)
        last = ticker["last"]
        self.last_prices[symbol] = last
        moves += update_protection(state, high=last, low=last, atr=atr, p=self.rp)

        # 3) Enviar el nuevo SL a Bybit (nunca afloja un SL que ya sea mejor)
        exchange_sl = position.get("stop_loss")
        target = round_tick(state.sl, limits.tick_size)
        must_set = exchange_sl is None or (abs(exchange_sl - target) > limits.tick_size / 2
                                           and _better(state.side, target, exchange_sl))
        if must_set:
            beyond = (state.side == LONG and target >= last) or (state.side == SHORT and target <= last)
            if beyond:
                self.db.add_event(self.mode, "INFO", f"{symbol}: el precio ya cruzó el nuevo SL; se cierra a mercado.")
                self.db.update_position(row["id"], **state.db_fields(), last_check_ms=last_check)
                await self._emergency_close(self.db.get_position(row["id"]), state.contracts_open,
                                            stop_reason(state), meta)
                return
            else:
                await self.api.set_trading_stop(symbol, stop_loss=target, position_idx=meta.get("position_idx", 0))
                note = moves[-1].note if moves else "restaurado"
                self.db.add_fill(row["id"], "SL_MOVE", target, 0.0, 0.0, 0.0, note)
                if exchange_sl is None:
                    self.db.add_event(self.mode, "WARNING", f"{symbol}: Bybit no mostraba SL; se volvió a poner.")
        elif exchange_sl is not None and _better(state.side, exchange_sl, state.sl):
            state.sl = exchange_sl  # alguien lo ajustó a mejor en Bybit: se respeta

        # 4) Volver a colocar TP que falten (si se cancelaron o fallaron)
        if any(not getattr(state, f"tp{n}_filled") and getattr(state, f"tp{n}_qty") > 0 for n in (1, 2)):
            open_ids = {o.get("id") for o in await self.api.fetch_open_orders(symbol)}
            for n in (1, 2):
                if getattr(state, f"tp{n}_filled") or getattr(state, f"tp{n}_qty") <= 0:
                    continue
                if tp_orders.get(f"tp{n}") in open_ids:
                    continue
                price = getattr(state, f"tp{n}_price")
                passed = (state.side == LONG and last >= price) or (state.side == SHORT and last <= price)
                qty_n = self._safe_qty(symbol, min(getattr(state, f"tp{n}_qty"), state.contracts_open))
                # Si la orden existía y la posición ya se redujo, pudo haberse ejecutado: no se duplica.
                maybe_filled = bool(tp_orders.get(f"tp{n}")) and position["contracts"] < state.contracts_initial - 1e-12
                if passed or qty_n <= 0 or qty_n < limits.min_qty or maybe_filled:
                    continue
                try:
                    order = await self.api.create_reduce_only_limit(symbol, state.side, qty_n, price,
                                                                     f"unico-{uuid.uuid4().hex[:20]}-t{n}")
                    tp_orders[f"tp{n}"] = order.get("id")
                except Exception as exc:
                    logger.warning(f"[Bybit] {symbol}: TP{n} sigue sin colocarse ({exc}).")
            meta["tp_orders"] = tp_orders

        self.db.update_position(row["id"], **state.db_fields(), last_check_ms=last_check, meta=meta)

    async def _finalize_from_exchange(self, row: Dict[str, Any]) -> None:
        """La posición ya no está en Bybit: registra cómo y a qué precio se cerró (PnL real de Bybit)."""
        symbol = row["symbol"]
        state = PositionState.from_db(row)
        meta = _meta(row)
        opened_ms = parse_iso_ms(row["opened_at"])
        exchange_pnl: Optional[float] = None
        try:
            records = await self.api.fetch_closed_pnl(symbol, opened_ms - 60_000)
            records = [r for r in records
                       if int(float(r.get("updatedTime") or r.get("createdTime") or 0)) >= opened_ms - 60_000]
        except Exception as exc:
            logger.warning(f"[Bybit] {symbol}: no se pudo leer el PnL cerrado ({exc}); se estima.")
            records = []
        records.sort(key=lambda r: int(float(r.get("updatedTime") or r.get("createdTime") or 0)))

        if records:
            exchange_pnl = sum(float(r.get("closedPnl") or 0) for r in records)
            fees = sum(float(r.get("openFee") or 0) + float(r.get("closeFee") or 0) for r in records)
            # Lo que ya registró el bot (TP vistos en ciclos anteriores) no se cuenta dos veces
            known = sum(f["qty"] or 0 for f in self.db.fills_for(row["id"]) if f["kind"] in EXIT_KINDS)
            last_kind = None
            for rec in records:
                qty = float(rec.get("closedSize") or rec.get("qty") or 0)
                price = float(rec.get("avgExitPrice") or 0)
                if known >= qty - 1e-12:
                    known -= qty
                    continue
                qty, known = qty - known, 0.0
                qty = min(qty, state.contracts_open)
                if qty <= 0 or price <= 0:
                    continue
                kind = self._classify_exit(state, price)
                if kind in ("TP1", "TP2") and getattr(state, f"{kind.lower()}_filled"):
                    kind = "CERRADA_EN_BYBIT"
                if kind in ("TP1", "TP2"):
                    setattr(state, f"{kind.lower()}_filled", True)
                pnl = price_pnl(state.side, state.entry_price, price, qty)
                state.realized_pnl += pnl
                state.contracts_open = max(0.0, state.contracts_open - qty)
                self.db.add_fill(row["id"], kind, price, qty, 0.0, pnl, "cierre detectado en Bybit")
                last_kind = kind
            if fees > 0:
                state.fees = fees
            reason = meta.get("forced_reason") or last_kind
        else:
            reason = meta.get("forced_reason")

        if state.contracts_open > 0:   # sin registros de Bybit: se estima con el último precio conocido
            price = self.last_prices.get(symbol) or state.sl
            kind = reason or self._classify_exit(state, price)
            qty = state.contracts_open
            pnl = price_pnl(state.side, state.entry_price, price, qty)
            fee = price * qty * self.rp.taker_fee
            state.realized_pnl += pnl
            state.fees += fee
            state.contracts_open = 0.0
            self.db.add_fill(row["id"], kind, price, qty, fee, pnl, "cierre estimado (Bybit no devolvió el detalle)")
            reason = kind
        for order_id in (meta.get("tp_orders") or {}).values():
            if order_id:
                try:
                    await self.api.cancel_order(order_id, symbol)
                except Exception:
                    pass
        self._finalize(row, state, reason or "CERRADA_EN_BYBIT", exchange_pnl=exchange_pnl)

    @staticmethod
    def _classify_exit(state: PositionState, price: float) -> str:
        tolerance = 0.0025
        if state.sl and abs(price - state.sl) / state.sl <= tolerance:
            return stop_reason(state)
        for n in (2, 1):
            target = getattr(state, f"tp{n}_price")
            if target and abs(price - target) / target <= tolerance:
                return f"TP{n}"
        return "CERRADA_EN_BYBIT"
