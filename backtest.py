"""
backtest.py — Backtest de cartera de UNICO (prueba el MISMO bot que corre en vivo)
==================================================================================
v9 — las flechas de Karla:
  - Usa strategy.py (las 4 flechas), risk_manager.py, setup_memory.py y seed_patterns.py:
    exactamente la lógica del bot. Cada moneda se evalúa con la misma función que en vivo
    (scan_history = evaluate_last_closed vela a vela, sin mirar al futuro).
  - Universo como en vivo: en cada hora, las TOP_N_24H que más subieron en 24 h (con liquidez
    mínima y memoria de WATCHLIST_MEMORY_HOURS) y las TOP_N_1H que más subieron en 1 h.
  - Una sola cartera: un único saldo, máximo de posiciones, cooldown, entradas diarias,
    kill switch y memoria de resultados por flecha (con seguimiento en sombra de las bloqueadas).
  - Salidas: SL, TP1 2R, TP2 5R, break-even a 3R, trailing y — si EXIT_ON_OPPOSITE — cierre en la
    apertura siguiente al aviso contrario (VERDE para cortos, TOPE para largos) si la posición va ganando.
  - Costos: comisión taker en entradas y stops, maker en TP límite, deslizamiento y funding histórico real.
  - Entrada a la apertura de la vela siguiente a la flecha (lo que hace el bot en vivo).
  - Informe por flecha, por motivo de salida, por rango de puntaje y primera/segunda mitad.

Limitaciones (declaradas en el informe):
  - La IA no se simula (no es reproducible ni gratis): se toman todas las flechas válidas.
  - Solo se descargan los contratos que existen hoy (sesgo de supervivencia).
  - Dentro de cada vela se asume primero el SL y después los TP (criterio conservador).
  - La lista de monedas se rehace cada hora (en vivo, cada vela de 15 minutos).

Uso:
  python backtest.py --days 60
  python backtest.py --days 30 --min-score 0.45 --max-positions 3
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import seed_patterns
import strategy
import universe
from config import Config, tf_ms
from historical_levels import levels_point_in_time
from rejection_analyzer import RejectionAnalyzer
from risk_manager import (EntryGuard, KillSwitch, MarketLimits, MemoryStore, PositionState, RiskParams,
                          close_at, funding_cost, opposite_exit, plan_position, process_bar, replan_from_fill)
from setup_memory import SetupMemory

logger = logging.getLogger("Backtest")

WARMUP_BARS = 1000   # igual que el bot en vivo (pide ~1000 velas para que la EMA233 sea estable)
MAX_DAYS = 180       # más días no caben bien en la memoria de Railway
EXIT_REASON = {strategy.VERDE: "SALIDA_VERDE", strategy.TOPE: "SALIDA_TOPE"}


# ======================================================================
# Progreso (lo lee el panel)
# ======================================================================
class Progress:
    def __init__(self, out_dir: Optional[str], params: Dict[str, Any]):
        self.out_dir = out_dir
        self.data = {"state": "running", "phase": "iniciando", "pct": 0.0, "message": "",
                     "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "params": params}
        self._lock = threading.Lock()
        self._write()

    def update(self, phase: str, pct: Optional[float] = None, message: str = "") -> None:
        with self._lock:
            self.data.update(phase=phase, message=message)
            if pct is not None:
                self.data["pct"] = round(float(pct), 1)
            self._write()
        logger.info(f"[{phase}] {message}" if message else f"[{phase}]")

    def finish(self, state: str, message: str = "") -> None:
        with self._lock:
            self.data.update(state=state, pct=100.0 if state == "done" else self.data["pct"], message=message,
                             finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            self._write()

    def _write(self) -> None:
        if not self.out_dir:
            return
        os.makedirs(self.out_dir, exist_ok=True)
        tmp = os.path.join(self.out_dir, "status.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, os.path.join(self.out_dir, "status.json"))


# ======================================================================
# Datos
# ======================================================================
EXCHANGES = ("bybit", "binance")


class BybitData:
    """Descarga velas y funding con caché en disco (data_dir/cache).
    Por defecto de Bybit; con BACKTEST_EXCHANGE=binance usa los perpetuos USDT de ese exchange
    (sirve para comparar o si Bybit no responde desde el servidor). La simulación siempre usa
    las comisiones y reglas de Bybit."""

    def __init__(self, cfg: Config, workers: int = 4, exchange: Optional[str] = None):
        import ccxt  # import local: los tests usan datos sintéticos sin ccxt
        self.cfg = cfg
        self.exchange_id = (exchange or cfg.backtest_exchange or "bybit").lower()
        if self.exchange_id not in EXCHANGES:
            raise ValueError(f"Exchange de datos no soportado: {self.exchange_id} (usa {', '.join(EXCHANGES)})")
        self.cache_dir = os.path.join(cfg.data_dir, "cache", self.exchange_id) if self.exchange_id != "bybit" \
            else os.path.join(cfg.data_dir, "cache")
        self.workers = workers
        self._local = threading.local()
        self._ccxt = ccxt
        base = self._new_exchange()
        base.load_markets()
        self._base = base
        self.markets = {s: m for s, m in base.markets.items()
                        if m.get("linear") and m.get("swap") and m.get("settle") == "USDT" and m.get("active", True)}

    def _new_exchange(self):
        if self.exchange_id == "bybit":
            options = {"defaultType": "swap", "defaultSubType": "linear", "fetchMarkets": {"types": ["linear"]}}
        elif self.exchange_id == "binance":
            options = {"defaultType": "future", "fetchMarkets": {"types": ["linear"]}}
        else:
            options = {"defaultType": "swap"}
        params: Dict[str, Any] = {"enableRateLimit": True, "timeout": 30000, "options": options}
        if self.exchange_id == "bybit":
            params["rateLimit"] = 60
        if self.cfg.proxy:
            params["proxies"] = {"http": self.cfg.proxy, "https": self.cfg.proxy}
        ex = getattr(self._ccxt, self.exchange_id)(params)
        if self.cfg.mode == "TESTNET" and self.exchange_id == "bybit":
            ex.set_sandbox_mode(True)
        return ex

    def _ex(self):
        ex = getattr(self._local, "ex", None)
        if ex is None:
            ex = self._new_exchange()
            ex.set_markets_from_exchange(self._base)
            self._local.ex = ex
        return ex

    def market_ids(self) -> Dict[str, str]:
        # Se usa el nombre tipo Bybit (BASEUSDT) para aplicar las mismas exclusiones en cualquier exchange
        return {s: f"{m.get('base', '')}USDT" if self.exchange_id != "bybit" else m["id"] for s, m in self.markets.items()}

    def market_info(self, symbol: str) -> Dict[str, Any]:
        return self.markets[symbol]

    def _path(self, market_id: str, tf: str) -> str:
        folder = os.path.join(self.cache_dir, tf)
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f"{market_id}.csv.gz")

    def ohlcv(self, symbol: str, tf: str, since_ms: int, until_ms: int) -> pd.DataFrame:
        market_id = self.markets[symbol]["id"]
        path = self._path(market_id, tf)
        meta_path = path + ".json"
        cached = pd.DataFrame()
        covered_from = None
        if os.path.exists(path):
            cached = pd.read_csv(path, index_col=0, parse_dates=True)
            if cached.index.tz is None:
                cached.index = cached.index.tz_localize("UTC")
            if os.path.exists(meta_path):
                with open(meta_path, encoding="utf-8") as fh:
                    covered_from = json.load(fh).get("covered_from")
        step = tf_ms(tf)
        frames = [cached] if not cached.empty else []
        need_head = covered_from is None or since_ms < covered_from
        if need_head:
            head_until = int(cached.index[0].value // 10**6) if not cached.empty else until_ms
            frames.insert(0, self._download(symbol, tf, since_ms, head_until))
            covered_from = since_ms
        if not cached.empty:
            last_ms = int(cached.index[-1].value // 10**6)
            if last_ms + step < until_ms - step:
                frames.append(self._download(symbol, tf, last_ms + step, until_ms))
        frames = [f for f in frames if f is not None and not f.empty]
        if not frames:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.concat(frames)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        closed_cut = pd.Timestamp(((int(time.time() * 1000) // step) * step) - step, unit="ms", tz="UTC")
        df = df[df.index <= closed_cut]
        df.to_csv(path, compression="gzip")
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump({"covered_from": covered_from}, fh)
        lo = pd.Timestamp(since_ms, unit="ms", tz="UTC")
        hi = pd.Timestamp(until_ms, unit="ms", tz="UTC")
        return df[(df.index >= lo) & (df.index <= hi)]

    def _download(self, symbol: str, tf: str, since_ms: int, until_ms: int) -> pd.DataFrame:
        from bybit_api_manager import candles_to_df
        ex = self._ex()
        step = tf_ms(tf)
        frames, cursor = [], int(since_ms)
        while cursor < until_ms:
            window_end = min(cursor + 1000 * step - 1, until_ms)
            for attempt in range(4):
                try:
                    raw = ex.fetch_ohlcv(symbol, tf, cursor, 1000, {"until": window_end})
                    break
                except self._ccxt.NetworkError:
                    time.sleep(2 * (attempt + 1))
            else:
                raise RuntimeError(f"No se pudieron descargar velas de {symbol}")
            df = candles_to_df(raw)
            if not df.empty:
                frames.append(df)
                last = int(df.index[-1].value // 10**6)
                # Cada exchange devuelve un máximo distinto por petición (Bybit/Binance 1000, OKX 100-300):
                # se sigue desde la última vela recibida para no dejar huecos.
                nxt = last + step
                cursor = nxt if nxt > cursor else window_end + 1
            else:
                cursor = window_end + 1   # tramo sin datos (antes del listado): se salta
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames)
        return out[out.index < pd.Timestamp(until_ms, unit="ms", tz="UTC")]

    def ohlcv_many(self, symbols: List[str], tf: str, since_ms: int, until_ms: int,
                   on_progress: Callable[[int, int], None]) -> Dict[str, pd.DataFrame]:
        result: Dict[str, pd.DataFrame] = {}
        done = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self.ohlcv, s, tf, since_ms, until_ms): s for s in symbols}
            for fut in as_completed(futures):
                symbol = futures[fut]
                try:
                    df = fut.result()
                    if not df.empty:
                        result[symbol] = df
                except Exception as exc:
                    logger.warning(f"{symbol}: sin datos ({exc})")
                done += 1
                on_progress(done, len(symbols))
        return result

    def funding(self, symbol: str, start_ms: int, end_ms: int) -> List[Tuple[int, float]]:
        ex = self._ex()
        if self.exchange_id != "bybit":
            out: Dict[int, float] = {}
            since = int(start_ms)
            for _ in range(50):
                rows = ex.fetch_funding_rate_history(symbol, since, 1000)
                for r in rows or []:
                    if r.get("timestamp") and r["timestamp"] <= end_ms:
                        out[int(r["timestamp"])] = float(r.get("fundingRate") or 0.0)
                if not rows or len(rows) < 2 or rows[-1]["timestamp"] >= end_ms:
                    break
                since = int(rows[-1]["timestamp"]) + 1
            return sorted(out.items())
        out: Dict[int, float] = {}
        cursor_end = int(end_ms)
        for _ in range(100):
            resp = ex.public_get_v5_market_funding_history(
                {"category": "linear", "symbol": self.markets[symbol]["id"], "endTime": cursor_end, "limit": 200})
            items = ((resp or {}).get("result") or {}).get("list") or []
            if not items:
                break
            for it in items:
                ts = int(float(it["fundingRateTimestamp"]))
                if ts >= start_ms:
                    out[ts] = float(it["fundingRate"])
            oldest = min(int(float(it["fundingRateTimestamp"])) for it in items)
            if oldest <= start_ms or len(items) < 200:
                break
            cursor_end = oldest - 1
        return sorted(out.items())

    def daily(self, symbol: str) -> pd.DataFrame:
        info = self.markets[symbol].get("info", {}) or {}
        launch = int(float(info.get("launchTime") or 0)) or int((time.time() - 4 * 365 * 86400) * 1000)
        return self.ohlcv(symbol, "1d", launch, int(time.time() * 1000))


# ======================================================================
# Simulación
# ======================================================================
@dataclass
class SymbolData:
    """Lo que el backtest necesita de cada moneda, ya calculado (arrays compactos)."""
    index: pd.DatetimeIndex
    pos: Dict[pd.Timestamp, int]
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    atr: np.ndarray
    entries: Dict[int, strategy.Signal]
    events: Dict[int, List[str]]


def precompute_symbol(symbol: str, df: pd.DataFrame, sp: strategy.StrategyParams, analyzer: RejectionAnalyzer,
                      start: pd.Timestamp, end: pd.Timestamp, templates: List[Dict[str, Any]], min_match: int,
                      htf_df: Optional[pd.DataFrame] = None, htf_tf: str = "1h") -> Optional[SymbolData]:
    """Recorre la moneda vela a vela con la función del bot y guarda flechas y avisos."""
    if df is None or len(df) < strategy.MIN_BARS:
        return None
    frame = strategy.build_frame(df, sp)
    step = strategy.tf_delta(sp.timeframe)
    htf_series = None
    if sp.htf_filter != "off" and htf_df is not None and len(htf_df) >= strategy.MIN_BARS and htf_tf != sp.timeframe:
        htf_series = strategy.align_htf(frame.index, strategy.build_features(htf_df, sp), sp.timeframe, htf_tf)
    scan_from = start - (sp.swing_bars + 2) * step       # un día antes: para recordar el ciclo de flechas
    results = strategy.scan_history(frame, symbol=symbol, analyzer=analyzer, start=scan_from, end=end,
                                    trend_timeframe=htf_tf, htf_fase=htf_series)
    entries: Dict[int, strategy.Signal] = {}
    events: Dict[int, List[str]] = {}
    for i, res in results.items():
        if frame.index[i] < start - step:
            continue
        if res.entry is not None:
            sig = res.entry
            features = seed_patterns.extract_features(frame.df, i)
            template, _hits = seed_patterns.match_pattern(features, sig.side, templates, min_match)
            if template:
                sig.pattern_id, sig.pattern_name = int(template["id"]), template["name"]
            entries[i] = sig
        if res.events:
            events[i] = [ev.kind for ev in res.events]
    a = frame.a
    return SymbolData(index=frame.index, pos={ts: k for k, ts in enumerate(frame.index)},
                      o=a["open"].astype(float).copy(), h=a["high"].astype(float).copy(),
                      l=a["low"].astype(float).copy(), c=a["close"].astype(float).copy(),
                      atr=np.nan_to_num(a["atr"].astype(float), nan=0.0).copy(), entries=entries, events=events)


@dataclass
class SimTrade:
    symbol: str
    side: str
    setup: str
    variant: str
    setup_key: str
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    entry_price: float
    contracts: float
    notional: float
    leverage: int
    stop_loss: float
    tp1: float
    tp2: float
    score: float
    pattern_id: Optional[int]
    pattern: Optional[str]
    risk_usd: float
    state: PositionState
    hist_position_pct: Optional[float] = None
    events: List[Tuple[pd.Timestamp, str, float, float, float, float]] = field(default_factory=list)
    exit_time: Optional[pd.Timestamp] = None
    exit_reason: Optional[str] = None
    funding: float = 0.0
    pending_exit: Optional[str] = None
    shadow: bool = False


def _limits_from_market(market: Dict[str, Any]) -> MarketLimits:
    info = market.get("info", {}) or {}
    lot = info.get("lotSizeFilter", {}) or {}
    price_filter = info.get("priceFilter", {}) or {}
    precision = market.get("precision") or {}
    limits = market.get("limits") or {}
    return MarketLimits(
        qty_step=float(lot.get("qtyStep") or precision.get("amount") or 0),
        min_qty=float(lot.get("minOrderQty") or (limits.get("amount") or {}).get("min") or 0),
        min_notional=float(lot.get("minNotionalValue") or (limits.get("cost") or {}).get("min") or 0),
        tick_size=float(price_filter.get("tickSize") or precision.get("price") or 0),
    )


def _max_leverage(market: Dict[str, Any]) -> int:
    info = market.get("info", {}) or {}
    try:
        lev = (info.get("leverageFilter") or {}).get("maxLeverage") \
            or ((market.get("limits") or {}).get("leverage") or {}).get("max") or 10
        return max(1, int(float(lev)))
    except (TypeError, ValueError):
        return 10


def _closing_kind(side: str) -> str:
    """Aviso que cierra cada lado: la VERDE cierra los cortos, el TOPE cierra los largos."""
    return strategy.VERDE if side == "SHORT" else strategy.TOPE


def simulate(
    cfg: Config,
    data: Dict[str, SymbolData],
    universe_by_hour: Dict[pd.Timestamp, List[str]],
    markets: Dict[str, Dict[str, Any]],
    start: pd.Timestamp,
    end: pd.Timestamp,
    progress: Optional["Progress"] = None,
) -> Tuple[List[SimTrade], pd.DataFrame, Dict[str, int]]:
    rp = RiskParams.from_config(cfg)
    tf = cfg.entry_timeframe
    step = strategy.tf_delta(tf)
    memory = SetupMemory.in_memory(cfg.setup_min_samples, cfg.setup_min_hit_rate)
    store = MemoryStore()
    kill = KillSwitch(store, "BT", cfg.daily_dd_limit, cfg.weekly_dd_limit)
    guard = EntryGuard(store, "BT", cfg.cooldown_minutes, cfg.max_entries_daily)

    counters = {"flechas_en_lista": 0, "abiertas": 0, "sin_hueco": 0, "ya_hay_posicion": 0, "bloqueadas_guard": 0,
                "bloqueadas_memoria": 0, "rechazadas_riesgo": 0, "bloqueadas_kill_switch": 0,
                "kill_switch_velas": 0, "salidas_por_aviso": 0, "avisos_sin_ganancia": 0, "sombra_cerradas": 0}
    cash = float(cfg.paper_balance)
    open_trades: Dict[str, SimTrade] = {}
    shadows: List[SimTrade] = []
    closed: List[SimTrade] = []
    equity_rows: List[Tuple[pd.Timestamp, float, float, int]] = []
    hours = sorted(universe_by_hour)
    hour_index = pd.DatetimeIndex(hours) if hours else pd.DatetimeIndex([], tz="UTC")
    grid = pd.date_range(start.ceil(step), end, freq=step)
    total = len(grid)

    def learn(trade: SimTrade) -> None:
        r_value = trade.state.r_value
        if r_value <= 0:
            return
        r_mult = trade.state.net_pnl / r_value
        code = strategy.SETUP_CODES.get(trade.setup_key)
        if code:
            memory.record(trade.symbol, code, tf, r_mult, name=f"Flecha {trade.setup_key}", shadow=trade.shadow)
        if trade.pattern_id:
            memory.record(trade.symbol, int(trade.pattern_id), tf, r_mult, name=f"Patrón WASHI #{trade.pattern_id}",
                          shadow=trade.shadow)

    def step_trade(trade: SimTrade, sd: SymbolData, i: int, when: pd.Timestamp) -> Tuple[float, bool]:
        """Aplica la vela i a la operación. Devuelve (efectivo que mueve, ¿se cerró?)."""
        moved = 0.0
        if trade.pending_exit:
            reason, trade.pending_exit = trade.pending_exit, None
            ev = opposite_exit(trade.state, float(sd.o[i]), rp, reason)
            if ev is not None:
                trade.events.append((sd.index[i], ev.kind, ev.price, ev.qty, ev.fee, ev.pnl))
                trade.exit_time, trade.exit_reason = sd.index[i], reason
                if not trade.shadow:
                    counters["salidas_por_aviso"] += 1
                return ev.pnl - ev.fee, True
            if not trade.shadow:
                counters["avisos_sin_ganancia"] += 1
        events = process_bar(trade.state, open_=float(sd.o[i]), high=float(sd.h[i]), low=float(sd.l[i]),
                             close=float(sd.c[i]), atr=float(sd.atr[i]), p=rp)
        for ev in events:
            trade.events.append((when, ev.kind, ev.price, ev.qty, ev.fee, ev.pnl))
            moved += ev.pnl - ev.fee
        if trade.state.closed:
            trade.exit_time = when
            trade.exit_reason = next((e.kind for e in reversed(events) if e.kind != "SL_MOVE"), "SL")
            return moved, True
        if cfg.exit_on_opposite and _closing_kind(trade.side) in sd.events.get(i, ()):
            trade.pending_exit = EXIT_REASON[_closing_kind(trade.side)]
        return moved, False

    def new_trade(sig: strategy.Signal, next_open: float, when: pd.Timestamp, equity: float,
                  shadow: bool = False) -> Optional[SimTrade]:
        market = markets.get(sig.symbol, {})
        limits = _limits_from_market(market)
        plan, _why = plan_position(sig.symbol, sig.side, next_open, sig.stop_loss, equity, rp,
                                   max_leverage=_max_leverage(market), limits=None if shadow else limits,
                                   target=sig.target if cfg.exit_at_zone else None)
        if plan is None:
            return None
        fill = next_open * (1 + rp.slippage_pct) if sig.side == "LONG" else next_open * (1 - rp.slippage_pct)
        plan = replan_from_fill(plan, fill, plan.contracts, rp, None if shadow else limits)
        if plan is None:
            return None
        fee = fill * plan.contracts * rp.taker_fee
        trade = SimTrade(
            symbol=sig.symbol, side=sig.side, setup=sig.setup, variant=sig.variant, setup_key=sig.setup_key,
            signal_time=sig.candle_close_time, entry_time=when, entry_price=fill, contracts=plan.contracts,
            notional=plan.notional, leverage=plan.leverage, stop_loss=plan.stop_loss, tp1=plan.tp1_price,
            tp2=plan.tp2_price, score=sig.score, pattern_id=sig.pattern_id, pattern=sig.pattern_name,
            risk_usd=plan.risk_usd, state=PositionState.from_plan(plan, entry_fee=fee), shadow=shadow,
        )
        trade.events.append((when, "ENTRY", fill, plan.contracts, fee, 0.0))
        return trade

    equity = cash
    for n, close_time in enumerate(grid):
        bar_open = close_time - step
        # 1) Posiciones abiertas (y las seguidas en sombra) con la vela que acaba de cerrar
        for symbol in list(open_trades):
            trade = open_trades[symbol]
            sd = data[symbol]
            i = sd.pos.get(bar_open)
            if i is None:
                continue
            moved, done = step_trade(trade, sd, i, close_time)
            cash += moved
            if done:
                closed.append(trade)
                del open_trades[symbol]
                guard.register_close(symbol, close_time.to_pydatetime())
                learn(trade)
        for trade in list(shadows):
            sd = data[trade.symbol]
            i = sd.pos.get(bar_open)
            if i is None:
                continue
            _moved, done = step_trade(trade, sd, i, close_time)
            if done:
                shadows.remove(trade)
                counters["sombra_cerradas"] += 1
                learn(trade)

        # 2) Capital y kill switch
        upnl = 0.0
        for symbol, trade in open_trades.items():
            sd = data[symbol]
            i = sd.pos.get(bar_open)
            if i is not None:
                upnl += trade.state.unrealized(float(sd.c[i]))
        equity = cash + upnl
        equity_rows.append((close_time, equity, cash, len(open_trades)))
        blocked, _ = kill.evaluate(equity, close_time.to_pydatetime())
        if blocked:
            counters["kill_switch_velas"] += 1

        # 3) Flechas de la vela que acaba de cerrar (solo monedas de la lista en ese momento)
        members: List[str] = []
        if len(hour_index):
            pos = hour_index.searchsorted(close_time, side="right") - 1
            members = universe_by_hour[hour_index[pos]] if pos >= 0 else []
        candidates = []
        for symbol in members:
            sd = data.get(symbol)
            if sd is None:
                continue
            i = sd.pos.get(bar_open)
            if i is None or i not in sd.entries or i + 1 >= len(sd.index):
                continue
            candidates.append((sd.entries[i], float(sd.o[i + 1])))
        counters["flechas_en_lista"] += len(candidates)
        candidates.sort(key=lambda c: c[0].score, reverse=True)
        for sig, next_open in candidates:
            if blocked:
                counters["bloqueadas_kill_switch"] += 1
                continue
            if sig.symbol in open_trades:
                counters["ya_hay_posicion"] += 1
                continue
            if len(open_trades) >= cfg.max_positions:
                counters["sin_hueco"] += 1
                continue
            ok, _ = guard.check(sig.symbol, close_time.to_pydatetime())
            if not ok:
                counters["bloqueadas_guard"] += 1
                continue
            verdict = memory.check(sig.symbol, tf, [(sig.setup_code, f"Flecha {sig.setup_key}"),
                                                    (sig.pattern_id, f"Patrón WASHI {sig.pattern_name}")])
            if not verdict["permitido"]:
                counters["bloqueadas_memoria"] += 1
                ghost = new_trade(sig, next_open, close_time, 10_000.0, shadow=True)
                if ghost is not None:
                    shadows.append(ghost)
                continue
            trade = new_trade(sig, next_open, close_time, equity)
            if trade is None:
                counters["rechazadas_riesgo"] += 1
                continue
            cash -= trade.events[0][4]
            open_trades[sig.symbol] = trade
            counters["abiertas"] += 1
            guard.register_entry(sig.symbol, close_time.to_pydatetime())

        if progress and n % max(1, total // 100) == 0:
            progress.update("simulando", 55 + 37 * n / max(1, total), f"{close_time:%Y-%m-%d %H:%M} | "
                            f"{len(closed)} cerradas | capital {equity:,.2f}")

    # Cierre de lo que quede abierto al final de los datos
    last_time = grid[-1] if len(grid) else end
    for symbol, trade in list(open_trades.items()):
        sd = data[symbol]
        k = int(sd.index.searchsorted(last_time - step, side="right")) - 1
        price = float(sd.c[max(0, min(k, len(sd.c) - 1))])
        ev = close_at(trade.state, price, rp, kind="FIN_DATOS")
        trade.events.append((last_time, ev.kind, ev.price, ev.qty, ev.fee, ev.pnl))
        cash += ev.pnl - ev.fee
        trade.exit_time = last_time
        trade.exit_reason = "FIN_DATOS"
        closed.append(trade)
    if len(grid):
        equity_rows.append((last_time, cash, cash, 0))
    equity_df = pd.DataFrame(equity_rows, columns=["time", "equity", "cash", "open_positions"]).set_index("time")
    equity_df = equity_df[~equity_df.index.duplicated(keep="last")]
    return closed, equity_df, counters


def apply_funding(trades: List[SimTrade], funding_rates: Dict[str, List[Tuple[int, float]]],
                  prices: Dict[str, pd.DataFrame]) -> List[Tuple[pd.Timestamp, float]]:
    """Aplica el funding histórico real a cada operación. Devuelve [(momento, costo)]."""
    payments: List[Tuple[pd.Timestamp, float]] = []
    for trade in trades:
        rates = funding_rates.get(trade.symbol) or []
        if not rates or trade.exit_time is None:
            continue
        timeline = [(t, kind, qty) for (t, kind, _p, qty, _f, _pnl) in trade.events if qty]
        frame = prices.get(trade.symbol)
        for ts_ms, rate in rates:
            ts = pd.Timestamp(ts_ms, unit="ms", tz="UTC")
            if not (trade.entry_time < ts <= trade.exit_time):
                continue
            contracts = trade.contracts
            for t, kind, qty in timeline:
                if t <= ts and kind != "ENTRY":
                    contracts -= qty
            if contracts <= 1e-12:
                continue
            mark = trade.entry_price
            if frame is not None and not frame.empty:
                prior = frame[frame.index <= ts]
                if not prior.empty:
                    mark = float(prior["close"].iloc[-1])
            cost = funding_cost(trade.side, contracts, mark, rate)
            trade.funding += cost
            payments.append((ts, cost))
    return payments


# ======================================================================
# Métricas e informe
# ======================================================================
def trades_frame(trades: List[SimTrade]) -> pd.DataFrame:
    rows = []
    for t in trades:
        net = t.state.realized_pnl - t.state.fees - t.funding
        r_value = t.state.r_value
        rows.append({
            "simbolo": t.symbol, "lado": t.side, "flecha": t.setup_key,
            "nombre_flecha": strategy.SETUP_KEY_NAMES.get(t.setup_key, t.setup_key),
            "senal": t.signal_time, "entrada": t.entry_time,
            "salida": t.exit_time, "precio_entrada": t.entry_price, "stop_loss": t.stop_loss,
            "tp1": t.tp1, "tp2": t.tp2, "contratos": t.contracts, "nocional": round(t.notional, 2),
            "apalancamiento": t.leverage, "puntaje": t.score, "patron": t.pattern or "",
            "posicion_rango_historico_pct": t.hist_position_pct,
            "motivo_salida": t.exit_reason, "tp1_ejecutado": t.state.tp1_filled, "tp2_ejecutado": t.state.tp2_filled,
            "pnl_bruto": round(t.state.realized_pnl, 4), "comisiones": round(t.state.fees, 4),
            "funding": round(t.funding, 4), "pnl_neto": round(net, 4),
            "r_multiplo": round(net / r_value, 3) if r_value else None,
            "duracion_h": round((t.exit_time - t.entry_time).total_seconds() / 3600, 2) if t.exit_time is not None else None,
        })
    return pd.DataFrame(rows)


def max_drawdown(equity: pd.Series) -> Tuple[float, Optional[pd.Timestamp], Optional[pd.Timestamp]]:
    if equity.empty:
        return 0.0, None, None
    peak = equity.cummax()
    dd = equity / peak - 1
    trough = dd.idxmin()
    peak_time = equity.loc[:trough].idxmax()
    return float(-dd.min() * 100), peak_time, trough


def _group_stats(df: pd.DataFrame, key: str) -> List[Dict[str, Any]]:
    if df.empty or key not in df:
        return []
    out = []
    for value, g in df.groupby(key, dropna=False):
        out.append({key: str(value), "operaciones": int(len(g)), "acierto_pct": round(float((g["pnl_neto"] > 0).mean() * 100), 1),
                    "pnl_neto": round(float(g["pnl_neto"].sum()), 2), "r_medio": round(float(g["r_multiplo"].mean()), 3)})
    return sorted(out, key=lambda r: r["pnl_neto"], reverse=True)


def summarize(df: pd.DataFrame, equity: pd.DataFrame, initial: float, counters: Dict[str, int],
              params: Dict[str, Any]) -> Dict[str, Any]:
    final = float(equity["equity"].iloc[-1]) if not equity.empty else initial
    dd, dd_peak, dd_trough = max_drawdown(equity["equity"]) if not equity.empty else (0.0, None, None)
    wins = df[df["pnl_neto"] > 0] if not df.empty else df
    losses = df[df["pnl_neto"] <= 0] if not df.empty else df
    gross_win = float(wins["pnl_neto"].sum()) if not df.empty else 0.0
    gross_loss = float(-losses["pnl_neto"].sum()) if not df.empty else 0.0

    def half(part: pd.DataFrame) -> Dict[str, Any]:
        if part.empty:
            return {"operaciones": 0}
        return {"operaciones": int(len(part)), "pnl_neto": round(float(part["pnl_neto"].sum()), 2),
                "acierto_pct": round(float((part["pnl_neto"] > 0).mean() * 100), 1),
                "r_medio": round(float(part["r_multiplo"].mean()), 3)}

    halves = {}
    if not df.empty:
        middle = df["entrada"].min() + (df["entrada"].max() - df["entrada"].min()) / 2
        halves = {"primera_mitad": half(df[df["entrada"] <= middle]), "segunda_mitad": half(df[df["entrada"] > middle])}

    score_bins = pd.cut(df["puntaje"], [0, 0.40, 0.45, 0.50, 0.55, 0.60, 0.70, 1.0]) if not df.empty else None
    by_score = []
    if score_bins is not None:
        df = df.assign(rango_puntaje=score_bins.astype(str))
        by_score = sorted(_group_stats(df, "rango_puntaje"), key=lambda r: r["rango_puntaje"])
    by_setup = []
    for row in _group_stats(df, "flecha"):
        row["nombre"] = strategy.SETUP_KEY_NAMES.get(row["flecha"], row["flecha"])
        by_setup.append(row)

    return {
        "parametros": params,
        "capital_inicial": round(initial, 2),
        "capital_final": round(final, 2),
        "rentabilidad_pct": round((final / initial - 1) * 100, 2) if initial else 0.0,
        "caida_maxima_pct": round(dd, 2),
        "caida_maxima_desde": str(dd_peak) if dd_peak is not None else None,
        "caida_maxima_hasta": str(dd_trough) if dd_trough is not None else None,
        "operaciones": int(len(df)),
        "acierto_pct": round(float((df["pnl_neto"] > 0).mean() * 100), 1) if not df.empty else 0.0,
        "factor_beneficio": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
        "esperanza_usdt": round(float(df["pnl_neto"].mean()), 2) if not df.empty else 0.0,
        "r_medio": round(float(df["r_multiplo"].mean()), 3) if not df.empty else 0.0,
        "ganancia_media": round(float(wins["pnl_neto"].mean()), 2) if len(wins) else 0.0,
        "perdida_media": round(float(losses["pnl_neto"].mean()), 2) if len(losses) else 0.0,
        "comisiones_total": round(float(df["comisiones"].sum()), 2) if not df.empty else 0.0,
        "funding_total": round(float(df["funding"].sum()), 2) if not df.empty else 0.0,
        "duracion_media_h": round(float(df["duracion_h"].mean()), 1) if not df.empty else 0.0,
        "mitades": halves,
        "por_flecha": by_setup,
        "por_lado": _group_stats(df, "lado"),
        "por_motivo_salida": _group_stats(df, "motivo_salida"),
        "por_patron": _group_stats(df, "patron"),
        "por_rango_puntaje": by_score,
        "mejores_simbolos": _group_stats(df, "simbolo")[:10],
        "peores_simbolos": list(reversed(_group_stats(df, "simbolo")[-10:])),
        "contadores": counters,
        "limitaciones": [
            "La IA no se simula: se toman todas las flechas válidas.",
            "Solo se incluyen contratos que cotizan hoy (sesgo de supervivencia).",
            "Dentro de una vela se asume primero el SL y luego los TP (conservador).",
            "Break-even y trailing se actualizan al cierre de cada vela (en vivo, cada minuto).",
            "La lista de monedas se rehace cada hora (en vivo, en cada vela de 15 minutos).",
            "El kill switch del backtest no descuenta funding (se aplica al final).",
            "El resultado pasado no garantiza el futuro: úsalo para comparar ajustes, no como promesa.",
        ],
    }


def write_report(out_dir: str, summary: Dict[str, Any], df: pd.DataFrame, equity: pd.DataFrame) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2, default=str)
    df.to_csv(os.path.join(out_dir, "trades.csv"), index=False)
    equity.to_csv(os.path.join(out_dir, "equity.csv"))
    s = summary
    par = s["parametros"]
    lines = [
        "# Informe de backtest — UNICO BOT v9 (flechas)", "",
        f"Periodo: {par.get('desde')} → {par.get('hasta')} | velas de {par.get('entry_tf')} "
        f"| flechas {', '.join(par.get('flechas') or [])} | MIN_SCORE {par.get('min_score')}", "",
        f"- Capital: {s['capital_inicial']:,.2f} → {s['capital_final']:,.2f} USDT ({s['rentabilidad_pct']:+.2f}%)",
        f"- Caída máxima: {s['caida_maxima_pct']:.2f}%",
        f"- Operaciones: {s['operaciones']} | acierto {s['acierto_pct']}% | factor de beneficio {s['factor_beneficio']}",
        f"- R medio por operación: {s['r_medio']} | esperanza {s['esperanza_usdt']} USDT",
        f"- Comisiones pagadas: {s['comisiones_total']} USDT | funding: {s['funding_total']} USDT", "",
        "## Por flecha",
    ]
    for row in s.get("por_flecha") or []:
        lines.append(f"- {row['nombre']}: {row['operaciones']} operaciones, acierto {row['acierto_pct']}%, "
                     f"R medio {row['r_medio']}, neto {row['pnl_neto']} USDT")
    lines += ["", "## Por motivo de salida"]
    for row in s.get("por_motivo_salida") or []:
        lines.append(f"- {row['motivo_salida']}: {row['operaciones']} operaciones, R medio {row['r_medio']}, "
                     f"neto {row['pnl_neto']} USDT")
    lines += ["", "## Consistencia (primera vs segunda mitad)"]
    for name, half in (s.get("mitades") or {}).items():
        lines.append(f"- {name.replace('_', ' ')}: {half}")
    lines += ["", "## Contadores", "```", json.dumps(s.get("contadores") or {}, ensure_ascii=False, indent=2), "```"]
    lines += ["", "## Limitaciones"] + [f"- {x}" for x in s["limitaciones"]]
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# ======================================================================
# Ejecución completa
# ======================================================================
def run_backtest(cfg: Config, days: int, out_dir: Optional[str], data: Optional[Any] = None,
                 max_symbols: int = 0) -> Dict[str, Any]:
    days = int(max(1, min(days, MAX_DAYS)))
    sp = strategy.StrategyParams.from_config(cfg)
    params = {"dias": days, "min_score": cfg.min_score, "entry_tf": cfg.entry_timeframe,
              "trend_tf": cfg.trend_timeframe, "flechas": list(cfg.setups), "filtro_1h": cfg.htf_filter,
              "salida_en_aviso": cfg.exit_on_opposite, "entrada_en_verde": cfg.entry_on_green,
              "entrada_en_tope": cfg.entry_on_top, "max_posiciones": cfg.max_positions,
              "margen_pct": cfg.margin_pct, "perdida_max_margen": cfg.max_loss_pct_of_margin,
              "taker": cfg.taker_fee, "maker": cfg.maker_fee, "deslizamiento": cfg.slippage_pct,
              "top_24h": cfg.top_n_24h, "top_1h": cfg.top_n_1h, "memoria_lista_h": cfg.watchlist_memory_hours,
              "volumen_min_24h": cfg.min_turnover_24h, "stop_atr": cfg.stop_atr,
              "datos": getattr(data, "exchange_id", "sintéticos") if data is not None else cfg.backtest_exchange}
    progress = Progress(out_dir, params)
    try:
        data = data or BybitData(cfg)
        now_ms = int(time.time() * 1000)
        end_ms = (now_ms // tf_ms("1h")) * tf_ms("1h")
        start_ms = end_ms - days * 86_400_000
        start_ts = pd.Timestamp(start_ms, unit="ms", tz="UTC")
        end_ts = pd.Timestamp(end_ms, unit="ms", tz="UTC")
        params.update(desde=str(start_ts), hasta=str(end_ts))
        eligible = [s for s, mid in data.market_ids().items()
                    if universe.is_eligible_id(mid, cfg.excluded_prefixes)]
        if max_symbols:
            eligible = eligible[:max_symbols]

        # Fase A: velas de 1 h de TODOS los contratos (periodo + memoria + 1 día) para saber cuáles subían más
        memory_h = max(0, int(cfg.watchlist_memory_hours))
        progress.update("descargando 1h", 2, f"{len(eligible)} contratos")
        hourly = data.ohlcv_many(eligible, "1h", start_ms - (memory_h + 26) * tf_ms("1h"), end_ms,
                                 lambda d, t: progress.update("descargando 1h", 2 + 22 * d / max(1, t), f"{d}/{t}"))

        progress.update("calculando la lista de monedas", 25)
        closes = pd.DataFrame({s: df["close"] for s, df in hourly.items() if not df.empty}).sort_index()
        volumes = pd.DataFrame({s: df["volume"] for s, df in hourly.items() if not df.empty}).sort_index()
        closes.index = closes.index + pd.Timedelta(hours=1)          # índice = hora de cierre
        volumes.index = volumes.index + pd.Timedelta(hours=1)
        liquid = universe.turnover_24h_from_hourly(closes, volumes) >= cfg.min_turnover_24h
        membership = universe.membership_from_closes(closes, cfg.top_n_24h, cfg.top_n_1h, memory_h, liquid)
        universe_by_hour = {ts: syms for ts, syms in membership.items() if start_ts < ts <= end_ts}
        needed = sorted({s for syms in universe_by_hour.values() for s in syms})
        del closes, volumes, liquid, membership

        tf, htf = cfg.entry_timeframe, cfg.trend_timeframe
        progress.update(f"descargando {tf}", 28, f"{len(needed)} monedas pasaron por la lista")
        entry_since = start_ms - (WARMUP_BARS + sp.swing_bars + 5) * tf_ms(tf)
        if tf == "1h":
            entry_frames = data.ohlcv_many(needed, "1h", entry_since, end_ms, lambda d, t: None)
        else:
            entry_frames = data.ohlcv_many(needed, tf, entry_since, end_ms,
                                           lambda d, t: progress.update(f"descargando {tf}", 28 + 10 * d / max(1, t)))
        htf_frames: Dict[str, pd.DataFrame] = {}
        if cfg.htf_filter != "off" and htf != tf:
            htf_since = start_ms - (strategy.MIN_BARS + 60) * tf_ms(htf)
            htf_frames = data.ohlcv_many(needed, htf, htf_since, end_ms, lambda d, t: None)
        del hourly

        progress.update("buscando flechas", 40, f"{len(entry_frames)} monedas")
        analyzer = RejectionAnalyzer(db_path=None, min_score=cfg.rejection_min_score)
        templates = [dict(p, id=i + 1, name=seed_patterns.pattern_name(p)) for i, p in enumerate(seed_patterns.PATTERNS)]
        symbol_data: Dict[str, SymbolData] = {}
        for k, symbol in enumerate(sorted(entry_frames)):
            try:
                sd = precompute_symbol(symbol, entry_frames[symbol], sp, analyzer, start_ts, end_ts, templates,
                                       cfg.pattern_min_match, htf_frames.get(symbol), htf)
            except Exception as exc:
                logger.warning(f"{symbol}: no se pudo analizar ({exc})")
                sd = None
            if sd is not None:
                symbol_data[symbol] = sd
            if k % 5 == 0:
                progress.update("buscando flechas", 40 + 15 * k / max(1, len(entry_frames)), symbol)
        htf_frames.clear()

        progress.update("simulando", 55, f"{len(symbol_data)} monedas con datos")
        markets = {s: data.market_info(s) for s in symbol_data}
        trades, equity, counters = simulate(cfg, symbol_data, universe_by_hour, markets, start_ts, end_ts, progress)
        counters["monedas_en_lista"] = len(needed)
        counters["flechas_detectadas"] = sum(len(sd.entries) for sd in symbol_data.values())
        counters["avisos_detectados"] = sum(len(v) for sd in symbol_data.values() for v in sd.events.values())

        progress.update("funding y niveles históricos", 92, f"{len(trades)} operaciones")
        traded = sorted({t.symbol for t in trades})
        rates: Dict[str, List[Tuple[int, float]]] = {}
        for symbol in traded:
            try:
                rates[symbol] = data.funding(symbol, start_ms, end_ms)
            except Exception as exc:
                logger.warning(f"{symbol}: sin funding ({exc})")
        payments = apply_funding(trades, rates, entry_frames)
        if payments and not equity.empty:
            pay = pd.DataFrame(payments, columns=["ts", "cost"]).groupby("ts")["cost"].sum().sort_index()
            cumulative = pay.cumsum().reindex(equity.index, method="ffill").fillna(0.0)
            equity["equity"] = equity["equity"] - cumulative
            equity["cash"] = equity["cash"] - cumulative
        for symbol in traded:
            try:
                daily = data.daily(symbol)
            except Exception:
                continue
            for t in trades:
                if t.symbol == symbol and t.hist_position_pct is None:
                    levels = levels_point_in_time(daily, entry_frames[symbol], t.entry_time, t.entry_price)
                    t.hist_position_pct = levels.range_position_pct if levels else None

        df = trades_frame(trades)
        summary = summarize(df, equity, float(cfg.paper_balance), counters, params)
        if out_dir:
            write_report(out_dir, summary, df, equity)
        progress.finish("done", f"{summary['operaciones']} operaciones | {summary['rentabilidad_pct']:+.2f}% | "
                                f"caída máx {summary['caida_maxima_pct']:.2f}%")
        return summary
    except Exception as exc:
        logger.exception("Backtest fallido")
        progress.finish("error", str(exc))
        raise


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest de cartera de UNICO BOT v9 (flechas)")
    parser.add_argument("--days", type=int, default=60, help=f"días a probar (máximo {MAX_DAYS})")
    parser.add_argument("--min-score", type=float)
    parser.add_argument("--entry-tf")
    parser.add_argument("--trend-tf")
    parser.add_argument("--max-positions", type=int)
    parser.add_argument("--max-symbols", type=int, default=0, help="limitar contratos (pruebas rápidas)")
    parser.add_argument("--exchange", choices=EXCHANGES, help="de dónde bajar las velas (por defecto BACKTEST_EXCHANGE o bybit)")
    parser.add_argument("--out", help="carpeta de resultados (por defecto DATA_DIR/backtests/<fecha>)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", force=True)
    overrides = {k: v for k, v in {
        "MIN_SCORE": args.min_score, "ENTRY_TIMEFRAME": args.entry_tf, "TREND_TIMEFRAME": args.trend_tf,
        "MAX_POSITIONS": args.max_positions, "BACKTEST_EXCHANGE": args.exchange,
    }.items() if v is not None}
    for key, value in overrides.items():
        os.environ[key] = str(value)
    cfg = Config.from_env()
    out_dir = args.out or os.path.join(cfg.data_dir, "backtests", datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    if args.days > MAX_DAYS:
        logger.warning(f"Se piden {args.days} días; se usan {MAX_DAYS} (límite de memoria).")
    summary = run_backtest(cfg, args.days, out_dir, max_symbols=args.max_symbols)
    print(json.dumps({k: summary[k] for k in ("capital_inicial", "capital_final", "rentabilidad_pct",
                                              "caida_maxima_pct", "operaciones", "acierto_pct",
                                              "factor_beneficio", "r_medio")}, ensure_ascii=False, indent=2))
    print(f"Resultados en {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
