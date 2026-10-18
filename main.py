"""
main.py — UNICO BOT v10 (orquestador)
====================================
Qué hace en cada vela cerrada de 15 minutos:
  1. Rehace la lista de monedas: las que más suben en 24 h y en 1 h, y las que estuvieron en el
     top de 24 h en las últimas WATCHLIST_MEMORY_HOURS (96 h) para seguir su ciclo completo.
  2. Evalúa cada moneda UNA vez por vela con strategy.py (la misma función que el backtest):
       flechas de entrada → ROJA, AMARILLA (ventas) y AZUL (compras)
       avisos             → VERDE (agotamiento de ventas) y TOPE (agotamiento de compras)
  3. Avisos: si hay un corto abierto en esa moneda y aparece la VERDE (o un largo y aparece el TOPE)
     y la posición va ganando, la cierra a mercado (EXIT_ON_OPPOSITE). Funciona aunque las entradas
     estén pausadas o bloqueadas por el kill switch.
  4. Entradas: pausa, kill switch, máximo de posiciones, cooldown, entradas diarias, memoria de
     resultados de la flecha, tamaño/costos y segunda opinión de la IA.
  5. Abre con el SL en el exchange y TP parciales (2R y 5R); cada MONITOR_INTERVAL segundos concilia
     con Bybit, mueve break-even y trailing y registra cierres y comisiones.

Comandos:
  python main.py              → arranca el bot (modo según BOT_MODE) y el panel web
  python main.py --check      → comprueba configuración y conexión con Bybit
  python main.py --init-db    → crea la base de datos e inserta los patrones WASHI
  python main.py --status     → resumen rápido en consola
  python main.py --backtest --days 60   → backtest de cartera (ver backtest.py)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal as os_signal
import sys
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

import ccxt
import pandas as pd

import seed_patterns
import strategy
import universe
from bybit_api_manager import BybitAPIManager, DataUnavailable, last_closed_open_ms
from config import TF_MINUTES, Config
from database import Database, iso, utcnow
from historical_levels import HistoricalLevels
from llm_filter import LLMFilter
from order_executor import ExchangeBroker, PaperBroker
from rejection_analyzer import RejectionAnalyzer
from risk_manager import (EntryGuard, KillSwitch, PositionState, RiskParams, close_at, in_profit,
                          opposite_exit, plan_position, process_bar)
from setup_memory import SetupMemory

VERSION = "10.0"
logger = logging.getLogger("UNICO")

EVENT_NAMES = {strategy.VERDE: "VERDE (agotamiento de ventas)", strategy.TOPE: "TOPE (agotamiento de compras)"}
EXIT_REASON = {strategy.VERDE: "SALIDA_VERDE", strategy.TOPE: "SALIDA_TOPE"}


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
    for noisy in ("ccxt", "urllib3", "httpx", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def init_database(cfg: Config) -> Database:
    db = Database(cfg.db_path)
    with db._lock:
        seed_patterns.insert_patterns(db.conn, seed_patterns.PATTERNS)
    SetupMemory(cfg.db_path, cfg.setup_min_samples, cfg.setup_min_hit_rate)
    RejectionAnalyzer(db_path=cfg.db_path)
    return db


class UnicoBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.mode = cfg.mode
        self.db = init_database(cfg)
        with self.db._lock:
            self.templates = seed_patterns.load_templates(self.db.conn)
        self.api = BybitAPIManager(cfg)
        self.rp = RiskParams.from_config(cfg)
        self.sp = strategy.StrategyParams.from_config(cfg)
        self.analyzer = RejectionAnalyzer(db_path=cfg.db_path, min_score=cfg.rejection_min_score)
        self.llm = LLMFilter(cfg)
        self.setup_memory = SetupMemory(cfg.db_path, cfg.setup_min_samples, cfg.setup_min_hit_rate)
        self.hist = HistoricalLevels(self.api)
        self.kill_switch = KillSwitch(self.db, self.mode, cfg.daily_dd_limit, cfg.weekly_dd_limit)
        self.guard = EntryGuard(self.db, self.mode, cfg.cooldown_minutes, cfg.max_entries_daily)
        self.watch_memory = universe.WatchlistMemory(self.db, self.mode, cfg.watchlist_memory_hours)
        broker_cls = PaperBroker if self.mode == "DRY_RUN" else ExchangeBroker
        self.broker = broker_cls(self.api, self.db, cfg, self.rp, on_close=self._on_close)

        self.watchlist: List[str] = []
        self._watchlist_at = 0.0
        self._tickers: Dict[str, universe.TickerRow] = {}
        self._evaluated: Dict[str, int] = {}
        self._features: Dict[str, pd.DataFrame] = {}
        self._htf_ctx: Dict[str, Dict[str, Any]] = {}
        self.running = False
        self._stop: Optional[asyncio.Event] = None
        self.started_at = utcnow()
        self.last_equity: Dict[str, float] = {}
        self.retry_delay = 10.0
        self.status: Dict[str, Any] = {"phase": "iniciando", "last_scan": None, "last_monitor": None,
                                       "scans": 0, "last_error": "", "blocked_reason": ""}

    # ------------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return bool(self.db.get_state(f"{self.mode}:paused", False))

    def set_paused(self, value: bool) -> None:
        self.db.set_state(f"{self.mode}:paused", bool(value))
        self.db.add_event(self.mode, "INFO", "Entradas PAUSADAS desde el panel." if value
                          else "Entradas REANUDADAS desde el panel.")

    def _record_memory(self, symbol: str, setup: Optional[str], variant: Optional[str], pattern_id: Optional[int],
                       timeframe: Optional[str], r_multiple: Optional[float], shadow: bool = False) -> None:
        if r_multiple is None:
            return
        tf = timeframe or self.cfg.entry_timeframe
        if setup:
            key = strategy.setup_key(setup, variant or "")
            code = strategy.SETUP_CODES.get(key)
            if code:
                self.setup_memory.record(symbol, code, tf, r_multiple, name=f"Flecha {key}", shadow=shadow)
        if pattern_id:
            self.setup_memory.record(symbol, int(pattern_id), tf, r_multiple, name=f"Patrón WASHI #{pattern_id}",
                                     shadow=shadow)

    def _on_close(self, row: Dict[str, Any]) -> None:
        pnl = float(row.get("net_pnl") or 0.0)
        self.guard.register_close(row["symbol"])
        if row.get("origin", "BOT") == "BOT":
            self._record_memory(row["symbol"], row.get("setup"), row.get("variant"), row.get("pattern_id"),
                                row.get("timeframe"), row.get("r_multiple"))
        if row.get("signal_id"):
            sig = self.db.query("SELECT rejection_event_id FROM signals WHERE id = ?", (row["signal_id"],))
            if sig and sig[0]["rejection_event_id"]:
                self.analyzer.record_outcome(sig[0]["rejection_event_id"], "WIN" if pnl > 0 else "LOSS", pnl)

    # ------------------------------------------------------------------
    # Universo
    # ------------------------------------------------------------------
    async def refresh_watchlist(self, force: bool = False) -> None:
        fresh = time.time() - self._watchlist_at < self.cfg.watchlist_refresh_min * 60
        if self.watchlist and fresh and not force:
            return
        raw = await self.api.fetch_linear_tickers()
        rows = universe.parse_tickers(raw, self.api.markets_by_id, self.cfg.excluded_prefixes)
        self._tickers = {r.symbol: r for r in rows}
        top24, top1h = universe.top_lists(rows, self.cfg.top_n_24h, self.cfg.top_n_1h, self.cfg.min_turnover_24h)
        if self.cfg.watchlist_memory_hours > 0:
            full = self.watch_memory.update(top24, top1h)
        else:
            full = universe.select_top(rows, self.cfg.top_n_24h, self.cfg.top_n_1h, self.cfg.min_turnover_24h)
        self.watchlist = [s for s in full if s in self._tickers]      # las que ya no cotizan se quitan
        remembered = [s for s in self.watchlist if s not in top24 and s not in top1h]
        self._watchlist_at = time.time()
        self.db.set_state(f"{self.mode}:watchlist", {"updated": iso(), "symbols": self.watchlist, "top24": top24,
                                                     "top1h": top1h, "memoria": remembered})
        logger.info(f"[Universo] {len(self.watchlist)} monedas ({len(remembered)} por memoria): "
                    f"{', '.join(s.split('/')[0] for s in self.watchlist)}")

    # ------------------------------------------------------------------
    # Escaneo
    # ------------------------------------------------------------------
    async def update_equity(self) -> Dict[str, float]:
        eq = await self.broker.equity()
        self.last_equity = eq
        self.db.add_equity(self.mode, eq["equity"], eq["balance"], eq["upnl"],
                           len(self.db.active_positions(self.mode)))
        return eq

    async def scan(self) -> None:
        tf = self.cfg.entry_timeframe
        expected = last_closed_open_ms(tf)
        candle_close_ms = expected + TF_MINUTES[tf] * 60_000
        # La lista de monedas se rehace al cerrar cada vela (igual que en el backtest) y cada WATCHLIST_REFRESH_MIN
        await self.refresh_watchlist(force=self._watchlist_at * 1000 < candle_close_ms)
        eq = await self.update_equity()
        blocked, reason = self.kill_switch.evaluate(eq["equity"])
        self.status["blocked_reason"] = reason if blocked else ""
        self.status["scans"] += 1
        self.status["last_scan"] = iso()
        max_age = min(self.cfg.signal_max_age_min, TF_MINUTES[tf] / 2) * 60_000
        if time.time() * 1000 - candle_close_ms > max_age:
            self.status["phase"] = "esperando el cierre de la próxima vela"
            return

        self.status["phase"] = "escaneando"
        own_open = [row["symbol"] for row in self.db.open_positions(self.mode) if row["origin"] == "BOT"]
        symbols = list(dict.fromkeys(self.watchlist + own_open))   # también las monedas con posición (para los avisos)
        entries: List[strategy.Signal] = []
        for symbol in symbols:
            if self._evaluated.get(symbol) == expected:
                continue
            try:
                result = await self._evaluate_symbol(symbol, expected)
            except (DataUnavailable, ccxt.NetworkError) as exc:
                logger.info(f"[Escaneo] {symbol}: sin datos ({exc})")
                continue
            except Exception as exc:
                logger.warning(f"[Escaneo] {symbol}: error {exc}")
                continue
            if result is None:
                continue
            for event in result.events:
                try:
                    await self._handle_market_event(event)
                except Exception as exc:
                    logger.exception(f"[Aviso] {symbol}: {exc}")
                    self.db.add_event(self.mode, "ERROR", f"Error procesando el aviso {event.kind} de {symbol}: {exc}")
            if result.entry is not None and symbol in self.watchlist:
                entries.append(result.entry)

        gate: Optional[Tuple[str, str]] = None
        if self.paused:
            gate = ("PAUSADO", "entradas pausadas desde el panel")
        elif blocked:
            gate = ("KILL_SWITCH", reason)
        entries.sort(key=lambda s: s.score, reverse=True)
        for sig in entries:
            try:
                opened = await self._process_signal(sig, eq, gate)
            except Exception as exc:
                logger.exception(f"[Señal] {sig.symbol}: {exc}")
                if sig.signal_id:
                    self.db.update_signal(sig.signal_id, decision="ERROR", reason=str(exc)[:300])
                self.db.add_event(self.mode, "ERROR", f"Error procesando la señal de {sig.symbol}: {exc}")
                continue
            if opened:
                eq = self.last_equity if self.mode == "DRY_RUN" else await self.update_equity()
        if self.paused:
            self.status["phase"] = "pausado (entradas detenidas desde el panel; los avisos siguen activos)"
        elif blocked:
            self.status["phase"] = f"kill switch: {reason}"
        else:
            self.status["phase"] = "vigilando"

    async def _evaluate_symbol(self, symbol: str, expected_open_ms: int) -> Optional[strategy.BarResult]:
        tf, htf = self.cfg.entry_timeframe, self.cfg.trend_timeframe
        entry_df = await self.api.fetch_ohlcv(symbol, tf)
        last_open_ms = int(entry_df.index[-1].value // 10**6)
        self._evaluated[symbol] = last_open_ms
        if last_open_ms != expected_open_ms or len(entry_df) < strategy.MIN_BARS:
            return None
        htf_df = None
        if self.cfg.htf_filter != "off" and htf != tf:
            htf_df = await self.api.fetch_ohlcv(symbol, htf)
        result, frame = strategy.evaluate_last_closed(
            entry_df, symbol=symbol, p=self.sp, analyzer=self.analyzer, htf_df=htf_df, htf_timeframe=htf,
            log_rejection=True,
        )
        sig = result.entry
        if sig is None:
            return result
        if htf_df is None and htf != tf:       # contexto de 1 h para la IA y el panel
            try:
                htf_df = await self.api.fetch_ohlcv(symbol, htf)
            except (DataUnavailable, ccxt.NetworkError):
                htf_df = None
        self._htf_ctx[symbol] = strategy.htf_context(htf_df, self.sp)
        features = seed_patterns.extract_features(frame.df, -1)
        template, _hits = seed_patterns.match_pattern(features, sig.side, self.templates, self.cfg.pattern_min_match)
        sig.pattern_features = features
        if template:
            sig.pattern_id, sig.pattern_name = int(template["id"]), template["name"]
        self._features[symbol] = frame.df.tail(30)
        return result

    async def _handle_market_event(self, event: strategy.MarketEvent) -> None:
        """Aviso VERDE / TOPE: se guarda y, si hay una posición del lado contrario ganando, se cierra."""
        event_id, new = self.db.record_market_event(
            mode=self.mode, symbol=event.symbol, timeframe=event.timeframe,
            candle_time=iso(event.candle_close_time.to_pydatetime()), kind=event.kind, price=event.price,
            close=event.close, exhaustion=event.exhaustion, reasons=event.reasons, action="AVISO",
        )
        if not new:
            return
        name = EVENT_NAMES.get(event.kind, event.kind)
        row = self.db.open_position_for(self.mode, event.symbol)
        if row is None or row["origin"] != "BOT" or row["side"] != event.closes_side:
            self.db.add_event(self.mode, "INFO", f"Aviso {name} en {event.symbol} a {event.price:.6g}.")
            return
        side_txt = "corto" if row["side"] == strategy.SHORT else "largo"
        if not self.cfg.exit_on_opposite:
            self.db.update_market_event(event_id, action="SIN_ACCION", note="EXIT_ON_OPPOSITE desactivado")
            self.db.add_event(self.mode, "INFO", f"Aviso {name} en {event.symbol}: el {side_txt} sigue (salida "
                                                 "por aviso desactivada).")
            return
        try:
            price = (await self.api.fetch_ticker(event.symbol))["last"]
        except (DataUnavailable, ccxt.NetworkError) as exc:
            self.db.update_market_event(event_id, action="ERROR", note=f"sin precio ({exc}); sigue con su SL")
            return
        if not in_profit(row["side"], row["entry_price"], price, self.rp.slippage_pct):
            self.db.update_market_event(event_id, action="MANTENER",
                                        note=f"el {side_txt} no va ganando ({price:.6g} frente a la entrada "
                                             f"{row['entry_price']:.6g}); sigue con su SL")
            return
        done = await self.broker.close_now(row, EXIT_REASON[event.kind], note=f"Aviso {name}")
        self.db.update_market_event(event_id, action="CIERRE" if done else "SIN_CIERRE",
                                    note=f"posición #{row['id']} ({side_txt}) a ~{price:.6g}")
        self.db.add_event(self.mode, "INFO", f"Aviso {name} en {event.symbol}: "
                                             f"{'se cierra' if done else 'no se pudo cerrar'} el {side_txt} que iba ganando.")

    async def _process_signal(self, sig: strategy.Signal, eq: Dict[str, float],
                              gate: Optional[Tuple[str, str]] = None) -> bool:
        """Decide si la flecha se opera. Devuelve True si se abrió la posición."""
        components = {**sig.components, "motivos": sig.reasons, "contexto_1h": self._htf_ctx.get(sig.symbol) or {}}
        signal_id, new = self.db.record_signal(
            mode=self.mode, symbol=sig.symbol, side=sig.side, timeframe=sig.timeframe,
            candle_time=iso(sig.candle_close_time.to_pydatetime()), setup=sig.setup, variant=sig.variant,
            score=sig.score, components=components, rejection=sig.rejection,
            rejection_event_id=sig.rejection.get("event_id"), pattern_id=sig.pattern_id,
            pattern_name=sig.pattern_name, entry_ref=sig.entry_ref, stop_loss=sig.stop_loss, target=sig.target,
            atr=sig.atr,
            decision="EVALUANDO",
        )
        if not new:
            return False
        sig.signal_id = signal_id

        if gate is not None:
            self.db.update_signal(signal_id, decision=gate[0], reason=gate[1])
            return False
        active = self.db.active_positions(self.mode)
        if any(row["symbol"] == sig.symbol for row in active):
            self.db.update_signal(signal_id, decision="YA_HAY_POSICION", reason="ya hay una posición en esa moneda")
            return False
        if len(active) >= self.cfg.max_positions:
            self.db.update_signal(signal_id, decision="MAX_POSICIONES",
                                  reason=f"ya hay {len(active)} posiciones (máximo {self.cfg.max_positions})")
            return False
        ok, reason = self.guard.check(sig.symbol)
        if not ok:
            self.db.update_signal(signal_id, decision="BLOQUEADA", reason=reason)
            return False
        verdict = self.setup_memory.check(sig.symbol, sig.timeframe, [
            (sig.setup_code, f"Flecha {sig.setup_key}"),
            (sig.pattern_id, f"Patrón WASHI {sig.pattern_name}"),
        ])
        if not verdict["permitido"]:
            # Se sigue en sombra: si la flecha vuelve a funcionar, la memoria la desbloquea sola
            self.db.update_signal(signal_id, decision="BLOQUEADA_MEMORIA", reason=verdict["razon"],
                                  shadow_status="PENDING")
            return False

        ticker = await self.api.fetch_ticker(sig.symbol)
        price = ticker["last"]
        plan, why = plan_position(
            sig.symbol, sig.side, price, sig.stop_loss, eq["equity"], self.rp,
            max_leverage=self.api.max_leverage(sig.symbol), is_st=self.api.is_st(sig.symbol),
            limits=self.api.limits(sig.symbol), target=sig.target if self.cfg.exit_at_zone else None,
        )
        if plan is None:
            self.db.update_signal(signal_id, decision="RECHAZADA_RIESGO", reason=why)
            return False

        hist = await self.hist.get(sig.symbol, price)
        if hist:
            sig.hist = hist.to_dict()
            self.db.update_signal(signal_id, hist=sig.hist)

        context = {"price": price, "min_score": self.cfg.min_score, "plan": plan.to_dict(),
                   "funding_rate": ticker["funding_rate"], "hist": sig.hist, "htf": self._htf_ctx.get(sig.symbol),
                   "memoria": verdict["razon"], "candles": self._features.get(sig.symbol)}
        decision = await self.llm.evaluate(sig, context)
        self.db.update_signal(
            signal_id, llm_provider=decision.provider or decision.source, llm_decision=decision.decision,
            llm_confidence=decision.confidence, llm_reason=decision.reason, llm_latency_ms=decision.latency_ms,
        )
        if not decision.allow:
            vetoed_by_model = decision.source == "llm"
            self.db.update_signal(signal_id, decision="VETO_LLM" if vetoed_by_model else "BLOQUEADA_LLM_ERROR",
                                  reason=decision.reason, shadow_status="PENDING" if vetoed_by_model else None)
            logger.info(f"[IA] Vetada {sig.symbol} {sig.setup_key}: {decision.reason}")
            return False

        position_id = await self.broker.open_position(plan, sig, meta={
            "next_funding_ms": ticker["next_funding_ms"], "reasons": sig.reasons, "flecha": sig.label})
        if position_id:
            self.guard.register_entry(sig.symbol)
            self.db.update_signal(signal_id, decision="EJECUTADA", position_id=position_id,
                                  reason=f"riesgo {plan.risk_usd:.2f} USDT | {decision.reason}")
            return True
        self.db.update_signal(signal_id, decision="NO_EJECUTADA", reason="ver eventos del panel")
        return False

    # ------------------------------------------------------------------
    # Gestión de posiciones y trabajos periódicos
    # ------------------------------------------------------------------
    async def monitor(self) -> None:
        await self.broker.manage()
        self.status["last_monitor"] = iso()

    async def shadow_job(self) -> None:
        """Sigue las señales vetadas por la IA o por la memoria: ¿qué habría pasado si se hubieran tomado?"""
        rows = self.db.pending_shadow_signals(self.mode, older_than=utcnow() - timedelta(hours=1))
        for row in rows:
            try:
                status, r_mult, exit_kind = await self._shadow_eval(row)
            except (DataUnavailable, ccxt.NetworkError):
                continue
            self.db.update_signal(row["id"], shadow_status=status, shadow_r=r_mult, shadow_exit=exit_kind)
            if status == "DONE" and row["decision"] == "BLOQUEADA_MEMORIA":
                self._record_memory(row["symbol"], row.get("setup"), row.get("variant"), row.get("pattern_id"),
                                    row["timeframe"], r_mult, shadow=True)

    async def _shadow_eval(self, row: Dict[str, Any]):
        """Simula la operación con las mismas reglas del backtest (SL, TP, break-even, trailing y avisos)."""
        tf = row["timeframe"]
        step = TF_MINUTES[tf] * 60_000
        close_ms = int(pd.Timestamp(row["candle_time"]).value // 10**6)
        warmup = strategy.MIN_BARS + self.sp.swing_bars + 10
        df = await self.api.fetch_ohlcv_range(row["symbol"], tf, since_ms=close_ms - warmup * step)
        if df.empty:
            return "PENDING", None, None
        frame = strategy.build_frame(df, self.sp)
        close_ts = pd.Timestamp(close_ms, unit="ms", tz="UTC")
        entry_pos = int(frame.index.searchsorted(close_ts))
        if entry_pos >= len(frame):
            return "PENDING", None, None
        a = frame.a
        entry = float(a["open"][entry_pos])
        target = row.get("target") if self.cfg.exit_at_zone else None
        plan, why = plan_position(row["symbol"], row["side"], entry, row["stop_loss"], 10_000.0, self.rp,
                                  target=target)
        if plan is None:
            return "DESCARTADA", None, why[:60]
        state = PositionState.from_plan(plan, entry_fee=entry * plan.contracts * self.rp.taker_fee)
        closing = {}
        if self.cfg.exit_on_opposite:
            kind_for_side = strategy.VERDE if row["side"] == strategy.SHORT else strategy.TOPE
            history = strategy.scan_history(frame, symbol=row["symbol"], analyzer=RejectionAnalyzer(db_path=None),
                                            start=frame.index[entry_pos])
            closing = {i: EXIT_REASON[kind_for_side] for i, res in history.items()
                       if any(ev.kind == kind_for_side for ev in res.events)}
        exit_kind, pending = None, None
        for k in range(entry_pos, len(frame)):
            if pending is not None:
                ev = opposite_exit(state, float(a["open"][k]), self.rp, pending)
                pending = None
                if ev is not None:
                    exit_kind = ev.kind
                    break
            atr = float(a["atr"][k]) if pd.notna(a["atr"][k]) else 0.0
            events = process_bar(state, open_=float(a["open"][k]), high=float(a["high"][k]), low=float(a["low"][k]),
                                 close=float(a["close"][k]), atr=atr, p=self.rp)
            if state.closed:
                exit_kind = next((e.kind for e in reversed(events) if e.kind != "SL_MOVE"), "SL")
                break
            pending = closing.get(k)
        if not state.closed:
            if utcnow() - close_ts.to_pydatetime() < timedelta(days=7):
                return "PENDING", None, None
            close_at(state, float(a["close"][-1]), self.rp, kind="ABIERTA_7D")
            exit_kind = "ABIERTA_7D"
        r_value = state.r_value
        return "DONE", round(state.net_pnl / r_value, 3) if r_value else None, exit_kind

    async def reload_markets(self) -> None:
        await self.api.load_markets(reload=True)

    # ------------------------------------------------------------------
    async def _loop(self, name: str, fn, interval: float) -> None:
        while self.running:
            started = time.monotonic()
            try:
                await fn()
            except Exception as exc:
                logger.exception(f"[{name}] {exc}")
                self.status["last_error"] = f"{name}: {exc}"
                self.db.add_event(self.mode, "ERROR", f"{name}: {exc}")
            elapsed = time.monotonic() - started
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(1.0, interval - elapsed))
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        if self.running:
            logger.info("Apagando UNICO BOT de forma ordenada...")
        self.running = False
        if self._stop is not None:
            self._stop.set()

    async def run(self) -> None:
        errors = self.cfg.validate()
        if errors:
            for err in errors:
                logger.critical(f"[Config] {err}")
            raise SystemExit(2)
        self._stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (os_signal.SIGINT, os_signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop)
            except (NotImplementedError, RuntimeError):  # Windows
                pass

        cfg = self.cfg
        logger.info("=" * 64)
        logger.info(f"UNICO BOT v{VERSION} | modo {self.mode} | velas {cfg.entry_timeframe} "
                    f"(contexto {cfg.trend_timeframe}) | flechas {', '.join(cfg.setups)}")
        logger.info(f"Avisos: salida en VERDE/TOPE {'activada' if cfg.exit_on_opposite else 'desactivada'} | "
                    f"filtro 1h {cfg.htf_filter} | MIN_SCORE {cfg.min_score} | IA {cfg.llm_provider}")
        logger.info(f"Riesgo: margen {cfg.margin_pct:.0%}, pérdida máx {cfg.max_loss_pct_of_margin:.0%} "
                    f"del margen ({cfg.max_loss_pct_of_balance:.1%} del capital) | máx {cfg.max_positions} posiciones")
        logger.info("=" * 64)
        self.db.add_event(self.mode, "INFO", f"UNICO BOT v{VERSION} arrancando en modo {self.mode}.")

        runner = None
        if cfg.dashboard_enabled:
            from dashboard import start_dashboard
            runner = await start_dashboard(self)

        self.running = True
        if not await self._connect():
            return await self._shutdown(runner, [])
        tasks = [
            asyncio.create_task(self._loop("escaneo", self.scan, cfg.scan_interval)),
            asyncio.create_task(self._loop("monitor", self.monitor, cfg.monitor_interval)),
            asyncio.create_task(self._loop("sombra", self.shadow_job, 3600)),
            asyncio.create_task(self._loop("mercados", self.reload_markets, 6 * 3600)),
        ]
        await self._stop.wait()
        await self._shutdown(runner, tasks)

    async def _connect(self) -> bool:
        """Conecta con Bybit reintentando: si Bybit no responde, el panel sigue activo y lo muestra."""
        delay = self.retry_delay
        while self.running:
            try:
                self.status["phase"] = "conectando con Bybit"
                await self.api.load_markets()
                await self.broker.startup()
                self.status["phase"] = "vigilando"
                return True
            except Exception as exc:
                self.status["phase"] = f"sin conexión con Bybit, reintento en {int(delay)} s"
                self.status["last_error"] = str(exc)
                logger.error(f"[Arranque] No se pudo conectar con Bybit: {exc}")
                self.db.add_event(self.mode, "ERROR", f"No se pudo conectar con Bybit: {exc}")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 300.0)
        return False

    async def _shutdown(self, runner, tasks) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if runner is not None:
            await runner.cleanup()
        await self.api.close()
        self.db.add_event(self.mode, "INFO", "Bot detenido. Las posiciones abiertas siguen con su SL/TP en Bybit.")
        self.db.close()


# ----------------------------------------------------------------------
# Comandos de consola
# ----------------------------------------------------------------------
def cmd_init_db(cfg: Config) -> None:
    db = init_database(cfg)
    with db._lock:
        seed_patterns.print_summary(db.conn)
    print(f"\nBase de datos lista en {cfg.db_path}")


def cmd_status(cfg: Config) -> None:
    db = init_database(cfg)
    print(f"Modo: {cfg.mode}")
    for row in db.active_positions(cfg.mode):
        flecha = f" [{row['setup']}]" if row.get("setup") else ""
        print(f"  ABIERTA {row['symbol']} {row['side']}{flecha} @ {row['entry_price']} SL {row['sl_current']} "
              f"({row['contracts_open']} contratos)")
    closed = db.closed_positions(cfg.mode, limit=10)
    total = sum((r["net_pnl"] or 0) for r in db.query(
        "SELECT net_pnl FROM positions WHERE mode = ? AND status = 'CLOSED'", (cfg.mode,)))
    print(f"Últimas cerradas ({len(closed)}), PnL neto acumulado {total:+.2f} USDT")
    for row in closed:
        print(f"  {row['closed_at']} {row['symbol']} {row['side']} {row.get('setup') or ''} "
              f"{row['exit_reason']} {(row['net_pnl'] or 0):+.2f}")
    events = db.recent_market_events(cfg.mode, limit=10)
    if events:
        print("Últimos avisos:")
        for ev in events:
            print(f"  {ev['candle_time']} {ev['symbol']} {ev['kind']} a {ev['price']:.6g} → {ev['action']}")


async def cmd_check(cfg: Config) -> int:
    print(f"Comprobando configuración (modo {cfg.mode})...")
    errors = cfg.validate()
    for err in errors:
        print(f"  ✗ {err}")
    if errors:
        return 2
    api = BybitAPIManager(cfg)
    try:
        await api.load_markets()
        print(f"  ✓ Mercados cargados: {len(api.markets_by_id)} perpetuos USDT")
        ticker = await api.fetch_ticker("BTC/USDT:USDT")
        print(f"  ✓ Precio BTC: {ticker['last']}")
        if cfg.uses_exchange:
            print(f"  ✓ Cuenta: {await api.detect_account()}")
            eq = await api.get_equity()
            print(f"  ✓ Capital: {eq['equity']:.2f} USDT (disponible {eq['available']:.2f})")
            positions = await api.fetch_open_positions()
            print(f"  ✓ Posiciones abiertas en Bybit: {len(positions)}")
        print(f"  ✓ Flechas activas: {', '.join(cfg.setups)} | salida en avisos: {cfg.exit_on_opposite}")
        print(f"  ✓ IA: {cfg.llm_provider} {cfg.llm_model or ''}")
        print(f"  ✓ Panel: {'activado' if cfg.dashboard_enabled and cfg.dashboard_password else 'SIN CONTRASEÑA (desactivado)'}")
        return 0
    except Exception as exc:
        print(f"  ✗ Error conectando con Bybit: {exc}")
        return 1
    finally:
        await api.close()


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"UNICO BOT v{VERSION}")
    parser.add_argument("--check", action="store_true", help="comprobar configuración y conexión")
    parser.add_argument("--init-db", action="store_true", help="crear la base de datos y los patrones")
    parser.add_argument("--status", action="store_true", help="resumen en consola")
    parser.add_argument("--backtest", action="store_true", help="ejecutar el backtest de cartera")
    parser.add_argument("--days", type=int, default=60, help="días de backtest (máximo 180)")
    parser.add_argument("--dry-run", action="store_true", help="(compatibilidad) fuerza BOT_MODE=DRY_RUN")
    parser.add_argument("--live", action="store_true", help="(compatibilidad) usa BOT_MODE=LIVE; exige CONFIRM_LIVE=SI")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    import os
    if args.dry_run:
        os.environ["BOT_MODE"] = "DRY_RUN"
    elif args.live:
        os.environ["BOT_MODE"] = "LIVE"
    cfg = Config.from_env()
    setup_logging(cfg.log_level)
    if args.dry_run:
        logger.warning("Se forzó BOT_MODE=DRY_RUN con el parámetro --dry-run. "
                       "Si quieres usar BOT_MODE, quita --dry-run del comando de inicio en Railway.")

    if args.init_db:
        cmd_init_db(cfg)
        return 0
    if args.status:
        cmd_status(cfg)
        return 0
    if args.check:
        return asyncio.run(cmd_check(cfg))
    if args.backtest:
        import backtest
        return backtest.main(["--days", str(args.days)])

    bot = UnicoBot(cfg)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Interrupción manual.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
