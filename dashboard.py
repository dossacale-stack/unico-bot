"""
dashboard.py — Panel web de UNICO BOT (para verlo desde el móvil)
=================================================================
Se sirve desde el mismo proceso del bot en el puerto PORT (Railway lo asigna solo).
Acceso con usuario y contraseña (DASHBOARD_USER / DASHBOARD_PASSWORD).
Sin DASHBOARD_PASSWORD el panel no se activa (solo responde /health).

Muestra: estado, capital y su curva, posiciones con su regla SL → TP2, el GRÁFICO de cualquier moneda
con las flechas que marca el bot (mismos colores que en TradingView: roja, amarilla, verde, azul),
operaciones cerradas con comisiones y funding, resultados por flecha, señales y vetos de la IA
(con su resultado "en sombra"), avisos de agotamiento, y permite pausar/reanudar entradas y lanzar
un backtest.
"""
from __future__ import annotations

import asyncio
import base64
import csv
import hmac
import io
import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from aiohttp import web

import strategy
from config import TF_MINUTES
from database import iso, utcnow
from rejection_analyzer import RejectionAnalyzer

logger = logging.getLogger("Dashboard")

RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}$")
CHART_MAX_DAYS = 7


# ----------------------------------------------------------------------
# Datos para el panel
# ----------------------------------------------------------------------
def _downsample(rows: List[Dict[str, Any]], limit: int = 300) -> List[List[Any]]:
    if len(rows) > limit:
        step = len(rows) / limit
        rows = [rows[int(i * step)] for i in range(limit)] + [rows[-1]]
    return [[r["ts"], round(r["equity"], 2)] for r in rows if r["equity"] is not None]


def backtests_dir(bot) -> str:
    return os.path.join(bot.cfg.data_dir, "backtests")


def list_backtests(bot, limit: int = 5) -> List[Dict[str, Any]]:
    folder = backtests_dir(bot)
    if not os.path.isdir(folder):
        return []
    runs = []
    for name in sorted(os.listdir(folder), reverse=True)[:limit]:
        if not RUN_ID.match(name):
            continue
        path = os.path.join(folder, name)
        item: Dict[str, Any] = {"id": name}
        for fname, key in (("status.json", "status"), ("summary.json", "summary")):
            fpath = os.path.join(path, fname)
            if os.path.exists(fpath):
                try:
                    with open(fpath, encoding="utf-8") as fh:
                        item[key] = json.load(fh)
                except (OSError, json.JSONDecodeError):
                    pass
        if "summary" in item:
            s = item["summary"]
            item["summary"] = {k: s.get(k) for k in (
                "capital_inicial", "capital_final", "rentabilidad_pct", "caida_maxima_pct", "operaciones",
                "acierto_pct", "factor_beneficio", "r_medio", "comisiones_total", "funding_total", "mitades",
                "por_lado", "por_flecha", "por_rango_puntaje", "parametros", "limitaciones")}
        runs.append(item)
    return runs


def _setup_stats(bot) -> List[Dict[str, Any]]:
    """Resultados reales por flecha + lo que dice la memoria (incluye seguimiento en sombra)."""
    db, mode, cfg = bot.db, bot.mode, bot.cfg
    real: Dict[str, List[Dict[str, Any]]] = {}
    for r in db.query("SELECT setup, variant, net_pnl, r_multiple FROM positions "
                      "WHERE mode = ? AND status = 'CLOSED' AND origin = 'BOT'", (mode,)):
        key = strategy.setup_key(r["setup"], r["variant"] or "") if r["setup"] else "SIN_FLECHA"
        real.setdefault(key, []).append(r)
    try:
        memory = {row["code"]: row for row in bot.setup_memory.summary(timeframe=cfg.entry_timeframe)}
    except Exception:  # pragma: no cover
        memory = {}
    out = []
    for key, code in strategy.SETUP_CODES.items():
        rows, mem = real.get(key, []), memory.get(code)
        if not rows and not mem:
            continue
        r_values = [x["r_multiple"] for x in rows if x["r_multiple"] is not None]
        out.append({
            "clave": key, "nombre": strategy.SETUP_KEY_NAMES.get(key, key),
            "operaciones": len(rows),
            "acierto_pct": round(100 * sum(1 for x in rows if (x["net_pnl"] or 0) > 0) / len(rows), 1) if rows else None,
            "r_medio": round(sum(r_values) / len(r_values), 3) if r_values else None,
            "pnl_neto": round(sum(x["net_pnl"] or 0 for x in rows), 2),
            "memoria_n": mem["n"] if mem else 0, "sombra_n": mem["shadow_n"] if mem else 0,
            "memoria_r": mem["r_medio"] if mem else None, "bloqueada": bool(mem and mem["bloqueada"]),
        })
    return out


def build_state(bot) -> Dict[str, Any]:
    db, mode, cfg = bot.db, bot.mode, bot.cfg
    ks = bot.kill_switch.status()
    eq = bot.last_equity or {}
    equity = eq.get("equity")
    day_start, week_start = ks.get("day_start"), ks.get("week_start")

    positions = []
    for row in db.active_positions(mode):
        last = bot.broker.last_prices.get(row["symbol"])
        upnl = None
        if last is not None:
            sign = 1 if row["side"] == "LONG" else -1
            upnl = (last - row["entry_price"]) * row["contracts_open"] * sign
        positions.append({k: row.get(k) for k in (
            "id", "symbol", "side", "status", "origin", "opened_at", "entry_price", "contracts_initial",
            "contracts_open", "leverage", "risk_usd", "sl_initial", "sl_current", "tp1_price", "tp1_filled",
            "tp2_price", "tp2_filled", "be_trigger", "be_set", "trail_active", "fees_paid", "funding_paid",
            "realized_pnl", "setup", "variant")} | {"last_price": last, "upnl": upnl})

    closed_rows = db.query(
        "SELECT net_pnl, fees_paid, funding_paid, r_multiple FROM positions WHERE mode = ? AND status = 'CLOSED'",
        (mode,))
    wins = [r for r in closed_rows if (r["net_pnl"] or 0) > 0]
    r_values = [r["r_multiple"] for r in closed_rows if r["r_multiple"] is not None]
    stats = {
        "operaciones": len(closed_rows),
        "acierto_pct": round(100 * len(wins) / len(closed_rows), 1) if closed_rows else None,
        "pnl_neto": round(sum(r["net_pnl"] or 0 for r in closed_rows), 2),
        "comisiones": round(sum(r["fees_paid"] or 0 for r in closed_rows), 2),
        "funding": round(sum(r["funding_paid"] or 0 for r in closed_rows), 2),
        "r_medio": round(sum(r_values) / len(r_values), 3) if r_values else None,
    }
    trades = [{k: r.get(k) for k in ("id", "symbol", "side", "opened_at", "closed_at", "entry_price",
                                      "exit_price_avg", "exit_reason", "net_pnl", "r_multiple", "fees_paid",
                                      "funding_paid", "exchange_pnl", "origin", "setup", "variant")}
              for r in db.closed_positions(mode, limit=30)]

    signals = [{k: r.get(k) for k in ("id", "ts", "symbol", "side", "setup", "variant", "candle_time", "score",
                                       "pattern_name", "decision", "reason", "llm_decision", "llm_confidence",
                                       "llm_reason", "shadow_status", "shadow_r", "shadow_exit")}
               for r in db.recent_signals(mode, limit=40)]
    llm_rows = db.query(
        "SELECT decision, shadow_status, shadow_r FROM signals WHERE mode = ? AND llm_decision IS NOT NULL", (mode,))
    shadow = [r for r in llm_rows if r["decision"] == "VETO_LLM" and r["shadow_status"] == "DONE" and r["shadow_r"] is not None]
    llm = {
        "proveedor": cfg.llm_provider, "modelo": cfg.llm_model,
        "consultadas": len(llm_rows),
        "aprobadas": sum(1 for r in llm_rows if r["decision"] in ("EJECUTADA", "NO_EJECUTADA")),
        "vetadas": sum(1 for r in llm_rows if r["decision"] == "VETO_LLM"),
        "sombra_evaluadas": len(shadow),
        "sombra_r_total": round(sum(r["shadow_r"] for r in shadow), 2) if shadow else None,
        "sombra_ganadoras": sum(1 for r in shadow if r["shadow_r"] > 0),
    }
    market_events = [{k: r.get(k) for k in ("ts", "symbol", "kind", "candle_time", "price", "exhaustion",
                                             "action", "note")}
                     for r in db.recent_market_events(mode, limit=20)]
    watch = db.get_state(f"{mode}:watchlist", {}) or {}
    return {
        "version": getattr(sys.modules.get("main"), "VERSION", "9"),
        "mode": mode,
        "phase": bot.status.get("phase"),
        "paused": bot.paused,
        "blocked_reason": bot.status.get("blocked_reason"),
        "last_scan": bot.status.get("last_scan"),
        "last_monitor": bot.status.get("last_monitor"),
        "last_error": bot.status.get("last_error"),
        "server_time": utcnow().isoformat(timespec="seconds"),
        "started_at": bot.started_at.isoformat(timespec="seconds"),
        "equity": eq,
        "pnl_dia": round(equity - day_start, 2) if equity is not None and day_start else None,
        "pnl_dia_pct": round((equity / day_start - 1) * 100, 2) if equity and day_start else None,
        "pnl_semana": round(equity - week_start, 2) if equity is not None and week_start else None,
        "pnl_semana_pct": round((equity / week_start - 1) * 100, 2) if equity and week_start else None,
        "kill_switch": ks,
        "limits": {"diaria": cfg.daily_dd_limit, "semanal": cfg.weekly_dd_limit},
        "watchlist": watch.get("symbols", []),
        "watchlist_memoria": watch.get("memoria", []),
        "positions": positions,
        "max_positions": cfg.max_positions,
        "stats": stats,
        "por_flecha": _setup_stats(bot),
        "trades": trades,
        "signals": signals,
        "market_events": market_events,
        "llm": llm,
        "equity_series": _downsample(db.equity_series(mode, utcnow() - timedelta(days=7))),
        "events": db.recent_events(mode, limit=25),
        "backtests": list_backtests(bot),
        "backtest_running": bool(getattr(bot, "_backtest_proc", None) and bot._backtest_proc.returncode is None),
        "config": cfg.public_dict(),
        "api": bot.api.status(),
    }


# ----------------------------------------------------------------------
# Gráfico con las flechas
# ----------------------------------------------------------------------
def resolve_symbol(bot, text: Optional[str]) -> Optional[str]:
    """Acepta 'LAB', 'labusdt' o 'LAB/USDT:USDT'. Devuelve el símbolo de ccxt o None."""
    t = re.sub(r"\s+", "", (text or "")).upper()
    if not t or len(t) > 40:
        return None
    by_id = bot.api.markets_by_id or {}
    if "/" in t:
        return next((m["symbol"] for m in by_id.values() if str(m.get("symbol", "")).upper() == t), None)
    market = by_id.get(t if t.endswith("USDT") else t + "USDT")
    return market["symbol"] if market else None


def _num(value: Any, digits: int = 6) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return float(f"{f:.{digits}g}") if math.isfinite(f) else None


def _series(values: np.ndarray, first: int) -> List[Optional[float]]:
    return [_num(v) for v in values[first:]]


async def build_chart(bot, symbol: str, days: float, until: Optional[datetime] = None) -> Dict[str, Any]:
    """Velas, EMAs, bandas y las flechas que marca la estrategia (la misma función que el bot y el backtest)."""
    cfg, sp = bot.cfg, bot.sp
    tf, htf = cfg.entry_timeframe, cfg.trend_timeframe
    step_ms = TF_MINUTES[tf] * 60_000
    show = max(8, int(round(days * 24 * 60 / TF_MINUTES[tf])))
    warm = strategy.MIN_BARS + sp.swing_bars + 10
    now_ms = int(time.time() * 1000)
    end_ms = min(int(until.timestamp() * 1000), now_ms) if until else now_ms
    df = await bot.api.fetch_ohlcv_range(symbol, tf, since_ms=end_ms - (show + warm) * step_ms, until_ms=end_ms)
    if df.empty:
        raise ValueError("Bybit no devolvió velas para ese periodo.")
    frame = strategy.build_frame(df, sp)
    n = len(frame)
    first = max(0, n - show)
    htf_series = None
    if cfg.htf_filter != "off" and htf != tf:
        htf_ms = TF_MINUTES[htf] * 60_000
        hdf = await bot.api.fetch_ohlcv_range(symbol, htf, since_ms=end_ms - (show * step_ms) - (strategy.MIN_BARS + 30) * htf_ms,
                                              until_ms=end_ms)
        if len(hdf) >= strategy.MIN_BARS:
            htf_series = strategy.align_htf(frame.index, strategy.build_features(hdf, sp), tf, htf)
    analyzer = RejectionAnalyzer(db_path=None, min_score=cfg.rejection_min_score)
    results = strategy.scan_history(frame, symbol=symbol, analyzer=analyzer,
                                    start=frame.index[max(0, first - sp.swing_bars - 1)],
                                    trend_timeframe=htf, htf_fase=htf_series)
    a, idx = frame.a, frame.index
    arrows: List[Dict[str, Any]] = []
    for i in sorted(results):
        if i < first:
            continue
        res = results[i]
        if res.entry is not None:
            sig = res.entry
            arrows.append({"k": i - first, "tipo": sig.setup, "variante": sig.variant, "lado": sig.side,
                           "nombre": sig.label, "entrada": True,
                           "precio": _num(a["high"][i] if sig.side == strategy.SHORT else a["low"][i]),
                           "cierre": _num(sig.entry_ref), "stop": _num(sig.stop_loss), "puntaje": round(sig.score, 2),
                           "motivos": sig.reasons})
        for ev in res.events:
            arrows.append({"k": i - first, "tipo": ev.kind, "variante": "", "entrada": False,
                           "lado": strategy.LONG if ev.kind == strategy.VERDE else strategy.SHORT,
                           "nombre": f"{ev.kind} · {strategy.SETUP_NAMES[ev.kind]} (aviso)",
                           "precio": _num(ev.price), "cierre": _num(ev.close), "agotamiento": ev.exhaustion,
                           "motivos": ev.reasons})

    window_open = idx[first].to_pydatetime()
    step = strategy.tf_delta(tf)

    def k_of(close_iso: Optional[str]) -> Optional[int]:
        if not close_iso:
            return None
        ts = pd.Timestamp(close_iso)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        pos = int(idx.searchsorted(ts - step))
        return pos - first if first <= pos < n else None

    decisions = []
    for row in bot.db.signals_between(bot.mode, symbol, window_open + step, idx[-1].to_pydatetime() + step):
        k = k_of(row["candle_time"])
        if k is not None:
            decisions.append({"k": k, "tipo": row["setup"], "lado": row["side"], "decision": row["decision"],
                              "razon": row["reason"]})
    def k_at(value: Optional[str]) -> Optional[int]:
        """Vela (dentro de la ventana) en la que ocurrió algo; None si queda fuera."""
        if not value:
            return None
        ts = pd.Timestamp(value)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        if ts < idx[first] or ts >= idx[-1] + step:
            return None
        return int(idx.searchsorted(ts, side="right")) - 1 - first

    trades = []
    for row in bot.db.query("SELECT id, side, setup, entry_price, opened_at, closed_at, exit_price_avg, exit_reason, "
                            "net_pnl, r_multiple FROM positions WHERE mode = ? AND symbol = ? AND status IN "
                            "('OPEN', 'CLOSED') AND (closed_at IS NULL OR closed_at >= ?) AND opened_at <= ? "
                            "ORDER BY id",
                            (bot.mode, symbol, iso(window_open), iso((idx[-1] + step).to_pydatetime()))):
        trades.append({"id": row["id"], "lado": row["side"], "tipo": row["setup"],
                       "k_entrada": k_at(row["opened_at"]), "entrada": _num(row["entry_price"]),
                       "k_salida": k_at(row["closed_at"]), "salida": _num(row["exit_price_avg"]),
                       "motivo": row["exit_reason"], "neto": _num(row["net_pnl"], 4), "r": _num(row["r_multiple"], 3)})

    return {
        "simbolo": symbol, "tf": tf, "dias": days,
        "t": [int(ts.value // 10**6) for ts in idx[first:]],
        "velas": [[_num(a["open"][i]), _num(a["high"][i]), _num(a["low"][i]), _num(a["close"][i])] for i in range(first, n)],
        "ema21": _series(a["ema21"], first), "ema55": _series(a["ema_fast"], first),
        "ema144": _series(a["ema_mid"], first), "ema233": _series(a["ema_slow"], first),
        "bb_sup": _series(a["bb_upper"], first), "bb_inf": _series(a["bb_lower"], first),
        "flechas": arrows, "decisiones": decisions, "operaciones": trades,
        "fase": strategy.FASE_NAMES[int(a["fase"][-1])],
        "fib": _fib_lines(a, sp),
    }


def _fib_lines(a: Dict[str, np.ndarray], sp) -> Optional[Dict[str, Any]]:
    """Niveles de Fibonacci del último gran tramo (los que vería el bot en la próxima vela)."""
    hi, lo, up = a["fib_hi"][-1], a["fib_lo"][-1], int(a["fib_up"][-1])
    if not sp.fib_levels or not (np.isfinite(hi) and np.isfinite(lo)) or up < 0 or hi <= lo:
        return None
    rng = hi - lo
    levels = [{"r": r, "precio": _num(hi - r * rng if up == 1 else lo + r * rng)} for r in sp.fib_levels]
    return {"tramo": "subida" if up == 1 else "bajada", "maximo": _num(hi), "minimo": _num(lo), "niveles": levels}


# ----------------------------------------------------------------------
# Servidor
# ----------------------------------------------------------------------
def _authorized(request: web.Request, user: str, password: str) -> bool:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
    except Exception:
        return False
    given_user, _, given_pass = decoded.partition(":")
    return hmac.compare_digest(given_user, user) and hmac.compare_digest(given_pass, password)


def create_app(bot) -> web.Application:
    cfg = bot.cfg

    @web.middleware
    async def auth(request: web.Request, handler):
        if request.path == "/health":
            return await handler(request)
        if not cfg.dashboard_password:
            return web.Response(status=503, text="Panel desactivado: define DASHBOARD_PASSWORD en Railway.")
        if not _authorized(request, cfg.dashboard_user, cfg.dashboard_password):
            return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="UNICO BOT"'},
                                text="Usuario o contraseña incorrectos.")
        if request.method == "POST" and request.headers.get("X-Unico") != "1":
            return web.Response(status=403, text="Petición no permitida.")
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def health(_request):
        return web.json_response({"ok": True, "mode": bot.mode, "phase": bot.status.get("phase")})

    async def index(_request):
        return web.Response(text=PAGE, content_type="text/html", charset="utf-8")

    async def state(_request):
        return web.json_response(build_state(bot), dumps=lambda o: json.dumps(o, default=str))

    async def pause(request):
        body = await request.json()
        bot.set_paused(bool(body.get("paused")))
        return web.json_response({"paused": bot.paused})

    async def chart(request):
        symbol = resolve_symbol(bot, request.query.get("simbolo"))
        if symbol is None:
            if not bot.api.markets_by_id:
                return web.json_response({"error": "El bot aún no se ha conectado con Bybit."}, status=503)
            return web.json_response({"error": "No encuentro esa moneda entre los perpetuos USDT de Bybit."}, status=404)
        try:
            days = float(request.query.get("dias") or 2)
        except ValueError:
            days = 2.0
        days = max(0.5, min(days, CHART_MAX_DAYS))
        until = None
        if request.query.get("hasta"):
            try:
                until = datetime.strptime(request.query["hasta"], "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
            except ValueError:
                return web.json_response({"error": "Fecha no válida (usa AAAA-MM-DD)."}, status=400)
        try:
            data = await build_chart(bot, symbol, days, until)
        except Exception as exc:
            logger.warning(f"[Panel] Gráfico de {symbol}: {exc}")
            return web.json_response({"error": f"No se pudo preparar el gráfico: {exc}"}, status=502)
        return web.json_response(data, dumps=lambda o: json.dumps(o, default=str))

    async def start_backtest(request):
        proc = getattr(bot, "_backtest_proc", None)
        if proc is not None and proc.returncode is None:
            return web.json_response({"error": "Ya hay un backtest en marcha."}, status=409)
        body = await request.json()
        days = max(7, min(int(body.get("days") or 60), 180))
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out_dir = os.path.join(backtests_dir(bot), run_id)
        os.makedirs(out_dir, exist_ok=True)
        args = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest.py"),
                "--days", str(days), "--out", out_dir]
        if body.get("min_score") not in (None, ""):
            args += ["--min-score", str(float(body["min_score"]))]
        if body.get("entry_tf"):
            args += ["--entry-tf", str(body["entry_tf"])]
        if body.get("exchange") in ("bybit", "binance"):
            args += ["--exchange", body["exchange"]]
        log = open(os.path.join(out_dir, "log.txt"), "w", encoding="utf-8")
        bot._backtest_proc = await asyncio.create_subprocess_exec(*args, stdout=log, stderr=asyncio.subprocess.STDOUT)
        bot.db.add_event(bot.mode, "INFO", f"Backtest de {days} días lanzado desde el panel ({run_id}).")
        return web.json_response({"id": run_id})

    async def backtest_file(request):
        run_id, name = request.match_info["run"], request.match_info["name"]
        if not RUN_ID.match(run_id) or name not in ("trades.csv", "equity.csv", "report.md", "summary.json", "log.txt"):
            raise web.HTTPNotFound()
        path = os.path.join(backtests_dir(bot), run_id, name)
        if not os.path.exists(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{run_id}-{name}"'})

    async def trades_csv(_request):
        rows = bot.db.query("SELECT * FROM positions WHERE mode = ? AND status = 'CLOSED' ORDER BY closed_at", (bot.mode,))
        buf = io.StringIO()
        if rows:
            writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return web.Response(text=buf.getvalue(), content_type="text/csv", charset="utf-8",
                            headers={"Content-Disposition": f'attachment; filename="operaciones-{bot.mode}.csv"'})

    app = web.Application(middlewares=[auth])
    app.router.add_get("/health", health)
    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/pause", pause)
    app.router.add_get("/api/grafico", chart)
    app.router.add_post("/api/backtest", start_backtest)
    app.router.add_get("/api/backtest/{run}/{name}", backtest_file)
    app.router.add_get("/api/operaciones.csv", trades_csv)
    return app


async def start_dashboard(bot) -> web.AppRunner:
    app = create_app(bot)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=bot.cfg.port)
    await site.start()
    if bot.cfg.dashboard_password:
        logger.info(f"[Panel] Disponible en el puerto {bot.cfg.port} (usuario {bot.cfg.dashboard_user}).")
    else:
        logger.warning("[Panel] Sin DASHBOARD_PASSWORD: solo /health está activo.")
    return runner


# ----------------------------------------------------------------------
# Página (HTML + CSS + JS en un solo archivo, sin dependencias)
# ----------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#EDF0F3" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#10161C" media="(prefers-color-scheme: dark)">
<title>UNICO BOT</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700&family=Instrument+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --papel:#EDF0F3; --tinta:#17202A; --suave:#56616D; --linea:#CBD3DB; --fondo-fila:#E3E8ED;
  --verde:#1C7F52; --roja:#B9372F; --dorada:#8A6514; --dorada-fondo:#F3E6C4;
  --grafico:#FFFFFF; --rejilla:#E6EAEE;
  --c-roja:#F23645; --c-amarilla:#FF9800; --c-verde:#43A047; --c-azul:#089981; --c-tope:#787B86;
  --display:"Bricolage Grotesque", "Trebuchet MS", sans-serif;
  --texto:"Instrument Sans", "Segoe UI", Roboto, sans-serif;
}
@media (prefers-color-scheme: dark){
  :root{ --papel:#10161C; --tinta:#E7ECF1; --suave:#9AA5B0; --linea:#2B3540; --fondo-fila:#18212A;
         --verde:#47C18B; --roja:#EF6E66; --dorada:#E2B657; --dorada-fondo:#3A3018;
         --grafico:#131722; --rejilla:#1F2533; }
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--papel);color:var(--tinta);font:16px/1.5 var(--texto);
     font-variant-numeric:tabular-nums;padding:0 16px 48px;max-width:760px;margin-inline:auto}
h1,h2{font-family:var(--display);font-weight:700;line-height:1.1;margin:0}
h2{font-size:1.35rem;margin:40px 0 12px}
button,input,select{font:inherit;color:inherit}
:focus-visible{outline:3px solid var(--dorada);outline-offset:2px}
.top{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-start;gap:12px 16px;padding-top:20px}
.top > div{flex:1 1 220px}
.estado{display:flex;gap:10px;align-items:baseline}
.luz{width:12px;height:12px;border-radius:50%;background:var(--verde);flex:none;transform:translateY(1px)}
.luz.pausa{background:var(--dorada)} .luz.error{background:var(--roja)}
.frase{font-family:var(--display);font-size:1.45rem;font-weight:700;line-height:1.15}
.modo{color:var(--suave);font-size:.92rem;margin-top:4px}
.btn{border:1.5px solid var(--tinta);background:transparent;border-radius:999px;padding:8px 16px;
     font-weight:600;cursor:pointer;white-space:nowrap}
.btn:active{transform:translateY(1px)}
.btn.lleno{background:var(--tinta);color:var(--papel)}
.aviso{margin-top:16px;padding:12px 14px;border-left:4px solid var(--dorada);background:var(--dorada-fondo);border-radius:4px}
.capital{margin-top:28px}
.rotulo{margin:0 0 4px;font-family:var(--texto);font-size:1rem;font-weight:500;color:var(--suave)}
.cifra{font-family:var(--display);font-size:clamp(2.4rem,11vw,3.6rem);font-weight:700;letter-spacing:-.02em;line-height:1}
.cifra small{font-size:.38em;font-weight:500;letter-spacing:0;color:var(--suave);margin-left:6px}
.variaciones{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:12px}
.variaciones div{display:flex;flex-direction:column;font-size:.9rem;color:var(--suave)}
.variaciones b{font-weight:600;font-size:1rem}
.pos{color:var(--verde)} .neg{color:var(--roja)}
svg.curva{display:block;width:100%;height:150px;margin-top:14px}
.vacio{color:var(--suave);padding:12px 0}
.posicion{padding:16px 0;border-top:1px solid var(--linea)}
.posicion:last-child{border-bottom:1px solid var(--linea)}
.pos-cab{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.simbolo{font-family:var(--display);font-weight:700;font-size:1.15rem}
.lado{font-weight:600;margin-left:6px}
.lado.LONG{color:var(--verde)} .lado.SHORT{color:var(--roja)}
.detalle{color:var(--suave);font-size:.88rem;margin-top:2px}
svg.regla{display:block;width:100%;height:auto;margin-top:8px;overflow:visible}
.tabla{width:100%;border-collapse:collapse;font-size:.92rem}
.tabla th{text-align:left;font-weight:500;color:var(--suave);padding:6px 6px 6px 0;border-bottom:1px solid var(--linea)}
.tabla td{padding:9px 6px 9px 0;border-bottom:1px solid var(--linea);vertical-align:top}
.tabla td.num,.tabla th.num{text-align:right;padding-left:8px}
.tabla td:last-child,.tabla th:last-child{padding-right:0}
.senal{padding:12px 0;border-bottom:1px solid var(--linea)}
.senal-cab{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.senal time{color:var(--suave);font-size:.85rem}
.motivo{color:var(--suave);font-size:.85rem}
.etiqueta{display:inline-block;border-radius:4px;padding:0 6px;font-size:.8rem;font-weight:600;border:1px solid currentColor}
.cifras{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:14px 20px}
.cifras div span{display:block;color:var(--suave);font-size:.85rem}
.cifras div strong{font-family:var(--display);font-size:1.3rem}
form.bt{display:flex;gap:10px;flex-wrap:wrap;align-items:end;margin-bottom:14px}
form.bt label{display:flex;flex-direction:column;font-size:.85rem;color:var(--suave);gap:4px}
form.bt input,form.bt select{background:transparent;border:1.5px solid var(--linea);border-radius:8px;padding:7px 10px;width:110px}
.barra{height:6px;background:var(--linea);border-radius:3px;overflow:hidden;margin:6px 0 4px}
.barra i{display:block;height:100%;background:var(--tinta)}
details{margin-top:8px} summary{cursor:pointer;font-weight:600}
pre{white-space:pre-wrap;font-size:.8rem;color:var(--suave)}
.evento{padding:8px 0;border-bottom:1px solid var(--linea);font-size:.9rem}
.evento time{color:var(--suave);font-size:.8rem;display:block}
.evento.ERROR,.evento.CRITICAL{color:var(--roja)} .evento.WARNING{color:var(--dorada)}
a{color:inherit}
.marca{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;flex:none;transform:translateY(1px)}
.marca.ROJA{background:var(--c-roja)} .marca.AMARILLA{background:var(--c-amarilla)} .marca.VERDE{background:var(--c-verde)}
.marca.AZUL{background:var(--c-azul)} .marca.DESINTERES{background:var(--c-roja);border-radius:2px} .marca.INTERES{background:var(--c-azul);border-radius:2px} .marca.TOPE{background:transparent;border:2px solid var(--c-tope)}
.g-marco{border:1px solid var(--linea);border-radius:8px;overflow:hidden;background:var(--grafico);margin-top:6px}
.g-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;overscroll-behavior-x:contain}
.g-scroll svg{display:block}
.g-scroll .fl{cursor:pointer}
.g-leyenda{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:10px;font-size:.85rem;color:var(--suave)}
.g-leyenda span{display:inline-flex;align-items:center}
.g-detalle{margin-top:12px;padding:12px 14px;border-left:4px solid var(--linea);background:var(--fondo-fila);border-radius:4px;font-size:.92rem}
.g-detalle ul{margin:6px 0 0;padding-left:18px} .g-detalle li{margin:2px 0}
form.bt input[name=simbolo]{width:120px;text-transform:uppercase}
form.bt input[type=date]{width:150px}
@media (prefers-reduced-motion:no-preference){ .luz{animation:latido 2.4s ease-in-out infinite} }
@keyframes latido{50%{opacity:.35}}
@media (prefers-reduced-motion:reduce){ .luz{animation:none} }
</style>
</head>
<body>
<header class="top">
  <div>
    <div class="estado"><span class="luz" id="luz"></span><span class="frase" id="frase">Conectando con el bot…</span></div>
    <div class="modo" id="modo"></div>
  </div>
  <button class="btn" id="pausa" type="button" hidden>Pausar entradas</button>
</header>
<div id="avisos"></div>

<section class="capital" aria-labelledby="t-capital">
  <h2 id="t-capital" class="rotulo">Capital</h2>
  <div class="cifra" id="cifra">—</div>
  <div class="variaciones" id="variaciones"></div>
  <svg class="curva" id="curva" role="img" aria-label="Evolución del capital en los últimos 7 días"></svg>
</section>

<section aria-labelledby="t-pos">
  <h2 id="t-pos">Posiciones abiertas</h2>
  <div id="posiciones"></div>
</section>

<section aria-labelledby="t-graf">
  <h2 id="t-graf">Gráfico con las flechas</h2>
  <form class="bt" id="form-g">
    <label>Moneda <input name="simbolo" id="g-simbolo" list="g-lista" placeholder="LAB" autocomplete="off" autocapitalize="characters" required></label>
    <datalist id="g-lista"></datalist>
    <label>Días <select name="dias"><option>1</option><option selected>2</option><option>3</option><option>5</option><option>7</option></select></label>
    <label>Hasta el día <input name="hasta" type="date"></label>
    <button class="btn lleno" type="submit">Ver gráfico</button>
  </form>
  <p class="detalle" id="g-estado">Elige una moneda para ver, en velas de 15 minutos, dónde vendería por desinterés o compraría por interés el bot. Toca una flecha para ver por qué la marcó.</p>
  <div class="g-marco" id="g-marco" hidden><div class="g-scroll" id="g-scroll"></div></div>
  <div class="g-leyenda" id="g-leyenda" hidden>
    <span><i class="marca" style="background:#9B6BDF;border-radius:2px;height:3px"></i>Morada EMA55</span>
    <span><i class="marca" style="background:#6FA8DC;border-radius:2px;height:3px"></i>Azul EMA144</span>
    <span><i class="marca" style="background:#F5D90A;border-radius:2px;height:3px"></i>Amarilla EMA233</span>
    <span><i class="marca DESINTERES"></i>Venta por desinterés</span>
    <span><i class="marca INTERES"></i>Compra por interés</span>
    <span><i class="marca ROJA"></i>Roja (v9)</span>
    <span><i class="marca AMARILLA"></i>Amarilla (v9)</span>
    <span><i class="marca VERDE"></i>Verde · agotamiento de ventas</span>
    <span><i class="marca AZUL"></i>Azul (v9)</span>
    <span><i class="marca TOPE"></i>Tope · agotamiento de compras</span>
  </div>
  <div class="g-detalle" id="g-detalle" hidden></div>
</section>

<section aria-labelledby="t-op">
  <h2 id="t-op">Operaciones cerradas</h2>
  <div class="cifras" id="estadisticas"></div>
  <div style="overflow-x:auto;margin-top:14px"><table class="tabla" id="operaciones"></table></div>
  <p><a href="/api/operaciones.csv">Descargar todas las operaciones (CSV)</a></p>
  <h3 class="rotulo" style="margin-top:22px">Resultados por flecha</h3>
  <div style="overflow-x:auto"><table class="tabla" id="por-flecha"></table></div>
</section>

<section aria-labelledby="t-sen">
  <h2 id="t-sen">Señales recientes</h2>
  <p class="detalle" id="llm"></p>
  <div id="senales"></div>
  <h3 class="rotulo" style="margin-top:22px">Avisos de agotamiento (verde y tope)</h3>
  <div id="agotamiento"></div>
</section>

<section aria-labelledby="t-bt">
  <h2 id="t-bt">Backtest</h2>
  <form class="bt" id="form-bt">
    <label>Días <input name="days" type="number" min="7" max="180" value="60"></label>
    <label>Puntaje mínimo <input name="min_score" type="number" step="0.01" min="0" max="1" placeholder="0.40"></label>
    <label>Velas <select name="entry_tf"><option value="">igual que el bot</option><option>15m</option><option>1h</option></select></label>
    <label>Datos de <select name="exchange"><option value="">Bybit</option><option value="binance">Binance</option></select></label>
    <button class="btn lleno" type="submit" id="lanzar">Lanzar backtest</button>
  </form>
  <div id="backtests"></div>
</section>

<section aria-labelledby="t-ev">
  <h2 id="t-ev">Avisos del bot</h2>
  <div id="eventos"></div>
</section>

<section aria-labelledby="t-cfg">
  <h2 id="t-cfg">Configuración</h2>
  <details><summary>Ver parámetros en uso</summary><pre id="config"></pre></details>
</section>

<script>
const $ = (id) => document.getElementById(id);
const nf = (v, d = 2) => v == null || isNaN(v) ? "—" : Number(v).toLocaleString("es-ES", {minimumFractionDigits: d, maximumFractionDigits: d});
const precio = (v) => v == null ? "—" : Number(v).toLocaleString("es-ES", {maximumSignificantDigits: 6});
const signo = (v, d = 2) => v == null ? "—" : (v > 0 ? "+" : "") + nf(v, d);
const clase = (v) => v > 0 ? "pos" : v < 0 ? "neg" : "";
const hora = (iso) => iso ? new Date(iso).toLocaleString("es-ES", {day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit"}) : "—";
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const MODOS = {DRY_RUN: "Simulación con precios reales de Bybit", DEMO: "Cuenta demo de Bybit (dinero virtual)",
               TESTNET: "Red de pruebas de Bybit", LIVE: "Dinero real en Bybit"};
const DECISIONES = {EJECUTADA: "Ejecutada", VETO_LLM: "Vetada por la IA", BLOQUEADA: "Bloqueada (cooldown o límite diario)",
  BLOQUEADA_MEMORIA: "Bloqueada: esta flecha viene perdiendo", BLOQUEADA_PATRON: "Patrón perdedor",
  RECHAZADA_RIESGO: "Rechazada por riesgo", NO_EJECUTADA: "No ejecutada", BLOQUEADA_LLM_ERROR: "Bloqueada (error de IA)",
  EVALUANDO: "Evaluando", PAUSADO: "Vista con las entradas en pausa", KILL_SWITCH: "Vista con el kill switch activo",
  YA_HAY_POSICION: "Ya había una posición en esa moneda", MAX_POSICIONES: "Máximo de posiciones alcanzado", ERROR: "Error"};
const SALIDAS = {SL: "Stop-loss", TP1: "TP1", TP2: "TP2", TRAILING: "Trailing", BREAK_EVEN: "Break-even",
  CERRADA_EN_BYBIT: "Cerrada en Bybit", MANUAL: "Manual", SIN_SL: "Cierre por seguridad", ENTRADA_TRAS_SL: "Cierre por seguridad",
  SALIDA_VERDE: "Aviso verde", SALIDA_TOPE: "Aviso tope", FIN_DATOS: "Fin de los datos"};
const FLECHAS = {ROJA: "Roja", AMARILLA: "Amarilla", VERDE: "Verde", AZUL: "Azul", TOPE: "Tope",
  DESINTERES: "Venta por desinterés", INTERES: "Compra por interés"};
const VARIANTES = {tras_techo: "tras el techo", retroceso: "en retroceso", tras_verde: "tras la verde", agresiva: "agresiva",
  morada: "en la morada", azul: "en la azul", amarilla: "en la amarilla"};
const ACCIONES = {AVISO: "Solo aviso", CIERRE: "Cerró la posición que iba ganando", MANTENER: "La posición no iba ganando: sigue con su SL",
  SIN_ACCION: "Salida por aviso desactivada", SIN_CIERRE: "No se pudo cerrar", ERROR: "Sin precio: sigue con su SL"};
const COLOR = {ROJA: "#F23645", AMARILLA: "#FF9800", VERDE: "#43A047", AZUL: "#089981", TOPE: "#787B86",
  DESINTERES: "#F23645", INTERES: "#089981"};
const flecha = (setup, variante) => setup ? `<i class="marca ${esc(setup)}"></i>${FLECHAS[setup] || esc(setup)}${variante && VARIANTES[variante] && ["AZUL", "DESINTERES", "INTERES"].includes(setup) ? " " + VARIANTES[variante] : ""}` : "";
let graficoPedido = false;

async function api(path, body) {
  const opts = body ? {method: "POST", headers: {"Content-Type": "application/json", "X-Unico": "1"}, body: JSON.stringify(body)} : {};
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function curva(svg, serie) {
  if (!serie || serie.length < 2) { svg.innerHTML = ""; return; }
  const w = svg.clientWidth || 600, h = 150, pad = 6, banda = 18;
  const vals = serie.map((p) => p[1]);
  const min = Math.min(...vals), max = Math.max(...vals), rango = max - min || 1;
  const x = (i) => pad + (w - 2 * pad) * i / (serie.length - 1);
  const y = (v) => h - banda - (h - 2 * banda) * (v - min) / rango;
  const d = serie.map((p, i) => (i ? "L" : "M") + x(i).toFixed(1) + " " + y(p[1]).toFixed(1)).join(" ");
  const subio = vals[vals.length - 1] >= vals[0];
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.innerHTML = `<path d="${d}" fill="none" stroke="var(${subio ? "--verde" : "--roja"})" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${x(serie.length - 1)}" cy="${y(vals[vals.length - 1])}" r="4.5" fill="var(--tinta)"/>
    <text x="${pad}" y="${h - 2}" font-size="11" fill="var(--suave)">${nf(min)}</text>
    <text x="${pad}" y="12" font-size="11" fill="var(--suave)">${nf(max)}</text>`;
}

function regla(p) {
  // De izquierda a derecha siempre: stop-loss → objetivos (hacia la derecha = ganancia)
  const sl = p.sl_current, tp2 = p.tp2_price, total = (tp2 - sl);
  if (!sl || !tp2 || !total) return "";
  const W = 340, x0 = 14, x1 = W - 14;
  const px = (v) => x0 + (x1 - x0) * Math.max(-0.04, Math.min(1.04, (v - sl) / total));
  const marcas = [
    {v: sl, color: "--roja", txt: p.be_set ? "SL (BE)" : "SL", on: true},
    {v: p.entry_price, color: "--tinta", txt: "Entrada", on: true},
    {v: p.tp1_price, color: "--verde", txt: "TP1", on: p.tp1_filled},
    {v: p.be_trigger, color: "--dorada", txt: "3R", on: p.be_set},
    {v: tp2, color: "--verde", txt: "TP2", on: p.tp2_filled},
  ].filter((m) => m.v != null).map((m) => ({...m, x: px(m.v)})).sort((a, b) => a.x - b.x);
  const ultimaFila = [-1e9, -1e9];
  for (const m of marcas) {               // etiquetas abajo; si chocan, arriba
    m.fila = m.x - ultimaFila[0] >= 44 ? 0 : (m.x - ultimaFila[1] >= 44 ? 1 : 0);
    ultimaFila[m.fila] = m.x;
  }
  const dibujo = marcas.map((m) => `<line x1="${m.x}" x2="${m.x}" y1="20" y2="36" stroke="var(${m.color})" stroke-width="2.5"/>
    <text x="${Math.max(24, Math.min(W - 24, m.x))}" y="${m.fila ? 12 : 52}" text-anchor="middle" font-size="11" font-weight="${m.on ? 600 : 400}" fill="var(${m.on ? m.color : "--suave"})">${m.txt}</text>`).join("");
  const punto = p.last_price == null ? "" :
    `<circle cx="${px(p.last_price)}" cy="28" r="6.5" fill="var(${(p.upnl ?? 0) >= 0 ? "--verde" : "--roja"})" stroke="var(--papel)" stroke-width="2"/>`;
  return `<svg class="regla" viewBox="0 0 ${W} 58" role="img" aria-label="Precio actual entre el stop-loss y el objetivo final">
    <line x1="${x0}" x2="${x1}" y1="28" y2="28" stroke="var(--linea)" stroke-width="4" stroke-linecap="round"/>${dibujo}${punto}</svg>`;
}

function pintar(s) {
  const luz = $("luz");
  luz.className = "luz" + (s.last_error && !s.last_scan ? " error" : (s.paused || s.blocked_reason) ? " pausa" : "");
  const abiertas = s.positions.length;
  $("frase").textContent = s.paused ? "Entradas en pausa" : s.blocked_reason ? "Kill switch activo" :
    `Vigilando ${s.watchlist.length} monedas`;
  const memo = (s.watchlist_memoria || []).length;
  const fase = s.phase && !["vigilando", "escaneando"].includes(s.phase) ? ` Ahora: ${s.phase}.` : "";
  const abiertasTxt = abiertas ? `${abiertas} ${abiertas === 1 ? "posición abierta" : "posiciones abiertas"}. ` : "Sin posiciones abiertas. ";
  $("modo").textContent = `${abiertasTxt}${MODOS[s.mode] || s.mode}.${fase} Último escaneo ${hora(s.last_scan)}.` +
    (memo ? ` ${memo} de ellas siguen en la lista porque subieron fuerte en los últimos días.` : "");
  const b = $("pausa"); b.hidden = false;
  b.textContent = s.paused ? "Reanudar entradas" : "Pausar entradas";
  b.onclick = async () => { b.disabled = true; try { await api("/api/pause", {paused: !s.paused}); await cargar(); } finally { b.disabled = false; } };

  const avisos = [];
  if (s.mode === "LIVE") avisos.push("Modo real: las órdenes usan tu dinero.");
  if (s.blocked_reason) avisos.push(`${esc(s.blocked_reason)}. No abrirá nuevas operaciones hasta el reinicio del periodo; las abiertas siguen con su SL y TP.`);
  if (s.api && s.api.consecutive_failures > 3) avisos.push(`Bybit no responde (${s.api.consecutive_failures} fallos seguidos). El bot no opera sin datos.`);
  $("avisos").innerHTML = avisos.map((a) => `<div class="aviso">${a}</div>`).join("");

  const eq = s.equity || {};
  $("cifra").innerHTML = `${nf(eq.equity)}<small>USDT</small>`;
  $("variaciones").innerHTML = `<div>Hoy<b class="${clase(s.pnl_dia)}">${signo(s.pnl_dia)}</b>${signo(s.pnl_dia_pct)} %</div>
    <div>Semana<b class="${clase(s.pnl_semana)}">${signo(s.pnl_semana)}</b>${signo(s.pnl_semana_pct)} %</div>
    <div>Sin realizar<b class="${clase(eq.upnl)}">${signo(eq.upnl)}</b>USDT</div>`;
  curva($("curva"), s.equity_series);

  $("posiciones").innerHTML = abiertas ? s.positions.map((p) => `
    <article class="posicion">
      <div class="pos-cab"><div><span class="simbolo">${esc(p.symbol.split(":")[0].replace("/", ""))}</span>
        <span class="lado ${p.side}">${p.side === "LONG" ? "Long" : "Short"}</span>
        ${p.setup ? `<span class="detalle" style="margin-left:6px">${flecha(p.setup, p.variant)}</span>` : ""}
        ${p.origin === "EXTERNAL" ? '<span class="etiqueta" style="color:var(--dorada)">no la abrió el bot</span>' : ""}
        ${p.status === "PENDING" ? '<span class="etiqueta">confirmando</span>' : ""}</div>
        <strong class="${clase(p.upnl)}">${signo(p.upnl)} USDT</strong></div>
      ${regla(p)}
      <div class="detalle">Entrada ${precio(p.entry_price)}, SL ${precio(p.sl_current)}, TP1 ${precio(p.tp1_price)}, TP2 ${precio(p.tp2_price)}.
        ${p.leverage ? p.leverage + "x" : ""}, riesgo ${nf(p.risk_usd)} USDT, abierta ${hora(p.opened_at)}.
        ${p.trail_active ? "Trailing activo." : p.be_set ? "Stop en break-even." : ""}</div>
    </article>`).join("") : `<p class="vacio">Sin posiciones. El bot entra solo cuando una moneda de la lista marca una flecha roja, amarilla o azul.</p>`;

  const st = s.stats;
  $("estadisticas").innerHTML = `
    <div><span>Operaciones</span><strong>${st.operaciones}</strong></div>
    <div><span>Acierto</span><strong>${st.acierto_pct == null ? "—" : nf(st.acierto_pct, 1) + " %"}</strong></div>
    <div><span>Resultado neto</span><strong class="${clase(st.pnl_neto)}">${signo(st.pnl_neto)}</strong></div>
    <div><span>R medio</span><strong>${st.r_medio == null ? "—" : signo(st.r_medio)}</strong></div>
    <div><span>Comisiones</span><strong>${nf(st.comisiones)}</strong></div>
    <div><span>Funding</span><strong>${nf(st.funding)}</strong></div>`;
  $("operaciones").innerHTML = s.trades.length ? `<thead><tr><th>Cierre</th><th>Moneda</th><th>Salida</th><th class="num">R</th><th class="num">Neto</th></tr></thead><tbody>` +
    s.trades.map((t) => `<tr><td>${hora(t.closed_at)}</td><td>${esc(t.symbol.split("/")[0])} <span class="lado ${t.side}">${t.side === "LONG" ? "L" : "S"}</span>${t.setup ? `<br><span class="motivo">${flecha(t.setup, t.variant)}</span>` : ""}</td>
      <td class="motivo">${SALIDAS[t.exit_reason] || esc(t.exit_reason)}</td><td class="num">${t.r_multiple == null ? "—" : signo(t.r_multiple)}</td>
      <td class="num ${clase(t.net_pnl)}">${signo(t.net_pnl)}</td></tr>`).join("") + "</tbody>" :
    `<tbody><tr><td class="vacio">Todavía no hay operaciones cerradas.</td></tr></tbody>`;
  const pf = s.por_flecha || [];
  $("por-flecha").innerHTML = pf.length ? `<thead><tr><th>Flecha</th><th class="num">Operaciones</th><th class="num">Acierto</th><th class="num">R medio</th><th class="num">Neto</th></tr></thead><tbody>` +
    pf.map((f) => `<tr><td><i class="marca ${esc(f.clave.split("_")[0])}"></i>${esc(f.nombre)}${f.bloqueada ? ' <span class="etiqueta" style="color:var(--roja)">bloqueada</span>' : ""}
      ${f.sombra_n ? `<br><span class="motivo">${f.sombra_n} seguidas en sombra; memoria ${f.memoria_n} resultados, ${signo(f.memoria_r)} R de media</span>` : ""}</td>
      <td class="num">${f.operaciones}</td><td class="num">${f.acierto_pct == null ? "—" : nf(f.acierto_pct, 1) + " %"}</td>
      <td class="num">${f.r_medio == null ? "—" : signo(f.r_medio)}</td><td class="num ${clase(f.pnl_neto)}">${signo(f.pnl_neto)}</td></tr>`).join("") + "</tbody>" :
    `<tbody><tr><td class="vacio">Aún no hay resultados por flecha.</td></tr></tbody>`;

  const l = s.llm;
  $("llm").textContent = l.proveedor === "none" ? "Filtro de IA desactivado: decide la estrategia sola." :
    `IA ${l.proveedor} (${l.modelo}): ${l.consultadas} consultas, ${l.vetadas} vetadas.` +
    (l.sombra_evaluadas ? ` Si se hubieran tomado las ${l.sombra_evaluadas} vetadas ya resueltas: ${signo(l.sombra_r_total)} R (${l.sombra_ganadoras} ganadoras).` : "");
  $("senales").innerHTML = s.signals.length ? s.signals.map((g) => {
    const extra = g.shadow_status === "DONE" && g.shadow_r != null ? ` En sombra habría dado ${signo(g.shadow_r)} R.` : "";
    const motivo = esc(g.llm_reason || g.reason || "");
    return `<div class="senal"><div class="senal-cab"><span>${flecha(g.setup, g.variant)} <strong>${esc(g.symbol.split("/")[0])}</strong>
      <span class="lado ${g.side}">${g.side === "LONG" ? "Long" : "Short"}</span>, puntaje ${nf(g.score)}</span><time>${hora(g.ts)}</time></div>
      <div>${DECISIONES[g.decision] || esc(g.decision)}${g.pattern_name ? `, patrón ${esc(g.pattern_name)}` : ""}</div>
      ${motivo || extra ? `<div class="motivo">${motivo}${motivo && !motivo.endsWith(".") ? "." : ""}${extra}</div>` : ""}</div>`;
  }).join("") : `<p class="vacio">Aún no hay flechas. Se evalúan al cerrar cada vela de 15 minutos.</p>`;
  $("agotamiento").innerHTML = (s.market_events || []).length ? s.market_events.map((e) => `<div class="senal">
      <div class="senal-cab"><span>${flecha(e.kind)} <strong>${esc(e.symbol.split("/")[0])}</strong> a ${precio(e.price)}</span><time>${hora(e.candle_time)}</time></div>
      <div class="motivo">${ACCIONES[e.action] || esc(e.action)}${e.note ? ". " + esc(e.note) : ""}</div></div>`).join("") :
    `<p class="vacio">Sin avisos todavía.</p>`;
  const lista = [...new Set([...s.positions.map((p) => p.symbol), ...s.watchlist])];
  $("g-lista").innerHTML = lista.map((x) => `<option value="${esc(x.split("/")[0])}">`).join("");
  if (!$("g-simbolo").value && lista.length && !graficoPedido) $("g-simbolo").value = lista[0].split("/")[0];

  $("lanzar").disabled = s.backtest_running;
  $("lanzar").textContent = s.backtest_running ? "Backtest en marcha…" : "Lanzar backtest";
  $("backtests").innerHTML = s.backtests.length ? s.backtests.map((b) => {
    const st = b.status || {}, r = b.summary;
    if (!r) return `<div class="posicion"><strong>${b.id}</strong> ${st.state === "error" ? `<span class="neg">Error: ${esc(st.message)}</span>` :
      `<div class="barra"><i style="width:${st.pct || 0}%"></i></div><span class="detalle">${esc(st.phase)} ${esc(st.message)}</span>`}</div>`;
    const m = r.mitades || {};
    return `<div class="posicion"><div class="pos-cab"><strong>${b.id}</strong><span class="detalle">${r.parametros?.dias} días, ${r.parametros?.entry_tf}, datos de ${esc(r.parametros?.datos || "bybit")}</span></div>
      <div class="cifras" style="margin-top:10px">
        <div><span>Rentabilidad</span><strong class="${clase(r.rentabilidad_pct)}">${signo(r.rentabilidad_pct)} %</strong></div>
        <div><span>Caída máxima</span><strong>${nf(r.caida_maxima_pct)} %</strong></div>
        <div><span>Operaciones</span><strong>${r.operaciones}</strong></div>
        <div><span>Acierto</span><strong>${nf(r.acierto_pct, 1)} %</strong></div>
        <div><span>Factor de beneficio</span><strong>${r.factor_beneficio == null ? "—" : nf(r.factor_beneficio)}</strong></div>
        <div><span>R medio</span><strong>${signo(r.r_medio, 3)}</strong></div></div>
      <p class="detalle">Primera mitad: ${m.primera_mitad?.operaciones ?? 0} operaciones, ${signo(m.primera_mitad?.pnl_neto)} USDT. Segunda mitad: ${m.segunda_mitad?.operaciones ?? 0}, ${signo(m.segunda_mitad?.pnl_neto)} USDT.
        Comisiones ${nf(r.comisiones_total)}, funding ${nf(r.funding_total)}.</p>
      ${(r.por_flecha || []).length ? `<p class="detalle">${r.por_flecha.map((f) => `<i class="marca ${esc(String(f.flecha).split("_")[0])}"></i>${esc(f.nombre || f.flecha)}: ${f.operaciones} op., ${nf(f.acierto_pct, 1)} % acierto, ${signo(f.r_medio, 2)} R`).join("<br>")}</p>` : ""}
      <p><a href="/api/backtest/${b.id}/trades.csv">Operaciones (CSV)</a>, <a href="/api/backtest/${b.id}/report.md">informe</a></p></div>`;
  }).join("") : `<p class="vacio">Todavía no has lanzado ningún backtest. Prueba con 60 días.</p>`;

  $("eventos").innerHTML = s.events.map((e) => `<div class="evento ${e.level}"><time>${hora(e.ts)}</time>${esc(e.message)}</div>`).join("") ||
    `<p class="vacio">Sin avisos.</p>`;
  $("config").textContent = JSON.stringify(s.config, null, 2);
}

// ---------------- Gráfico con las flechas ----------------
let datosGrafico = null;
function puntas(x, y, haciaAbajo) {        // flecha de bloque como las de TradingView; (x, y) = punta
  const s = haciaAbajo ? -1 : 1;
  return [[0, 0], [7, 7], [3, 7], [3, 17], [-3, 17], [-3, 7], [-7, 7]]
    .map(([dx, dy]) => `${(x + dx).toFixed(1)},${(y + s * dy).toFixed(1)}`).join(" ");
}
function tramo(arr, x, y) {
  let d = "", pen = false;
  arr.forEach((v, k) => { if (v == null) { pen = false; return; } d += (pen ? "L" : "M") + x(k).toFixed(1) + " " + y(v).toFixed(1); pen = true; });
  return d;
}
const G = {paso: 7, izq: 6, der: 66, H: 380, arriba: 30, abajo: 34};
let reescala = null;
function dibujarGrafico(d) {
  datosGrafico = d;
  const caja = $("g-scroll");
  const W = Math.max(G.izq + d.t.length * G.paso + G.der, caja.clientWidth || 320);
  caja.innerHTML = `<svg width="${W}" height="${G.H}"></svg>`;
  caja.scrollLeft = caja.scrollWidth;              // empieza en lo más reciente, como TradingView
  pintarGrafico();
  caja.onscroll = () => { clearTimeout(reescala); reescala = setTimeout(pintarGrafico, 140); };
}
function pintarGrafico() {
  const d = datosGrafico, caja = $("g-scroll");
  if (!d) return;
  const n = d.t.length, {paso, izq, der, H, arriba, abajo} = G;
  const W = Math.max(izq + n * paso + der, caja.clientWidth || 320);
  const vista0 = caja.scrollLeft, vistaW = caja.clientWidth || 320;
  // Escala de precios según las velas VISIBLES (al deslizar se reajusta, como en TradingView)
  const k0 = Math.max(0, Math.floor((vista0 - izq) / paso)), k1 = Math.min(n - 1, Math.ceil((vista0 + vistaW - der - izq) / paso));
  let lo = Infinity, hi = -Infinity;
  for (let k = k0; k <= k1; k++) {
    const c = d.velas[k];
    if (c[2] != null) lo = Math.min(lo, c[2]);
    if (c[1] != null) hi = Math.max(hi, c[1]);
    for (const s of ["ema55", "ema144"]) { const v = d[s][k]; if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }
  }
  if (!isFinite(lo) || !isFinite(hi)) return;
  const margen = (hi - lo) * 0.08 || Math.abs(hi) * 0.01 || 1;
  lo -= margen; hi += margen;
  const y = (v) => arriba + (hi - v) / (hi - lo) * (H - arriba - abajo);
  const x = (k) => izq + k * paso + paso / 2;
  const partes = [`<defs><clipPath id="g-zona"><rect x="0" y="${arriba - 24}" width="${W}" height="${H - arriba - abajo + 48}"/></clipPath></defs>`];
  const xEje = Math.min(W, vista0 + vistaW) - der;  // los precios van pegados al borde derecho visible
  for (let j = 0; j <= 5; j++) {
    const v = lo + (hi - lo) * j / 5, yy = y(v).toFixed(1);
    partes.push(`<line x1="0" x2="${W}" y1="${yy}" y2="${yy}" stroke="var(--rejilla)" stroke-width="1"/>`);
  }
  let diaAnterior = null;
  d.t.forEach((t, k) => {
    const f = new Date(t), dia = f.toDateString();
    if (diaAnterior !== null && dia !== diaAnterior) {
      partes.push(`<line x1="${x(k) - paso / 2}" x2="${x(k) - paso / 2}" y1="${arriba - 12}" y2="${H - abajo}" stroke="#2962FF" stroke-opacity=".55" stroke-dasharray="4 4"/>`,
        `<text x="${x(k)}" y="${H - 12}" font-size="11" font-weight="600" fill="var(--tinta)" text-anchor="middle">${f.toLocaleDateString("es-ES", {day: "2-digit", month: "short"})}</text>`);
    } else if (f.getMinutes() === 0 && f.getHours() % 6 === 0 && k > 2 && k < n - 2) {
      partes.push(`<text x="${x(k)}" y="${H - 12}" font-size="11" fill="var(--suave)" text-anchor="middle">${String(f.getHours()).padStart(2, "0")}:00</text>`);
    }
    diaAnterior = dia;
  });
  const dibujo = [];
  const validos = d.bb_sup.map((v, k) => (v != null && d.bb_inf[k] != null) ? k : -1).filter((k) => k >= 0);
  if (validos.length > 1) {
    const ida = validos.map((k) => `${x(k).toFixed(1)} ${y(d.bb_sup[k]).toFixed(1)}`).join(" L");
    const vuelta = validos.slice().reverse().map((k) => `${x(k).toFixed(1)} ${y(d.bb_inf[k]).toFixed(1)}`).join(" L");
    dibujo.push(`<path d="M${ida} L${vuelta} Z" fill="#2962FF" fill-opacity=".03" stroke="none"/>`);
  }
  // Colores de Karla: morada = EMA55, azul = EMA144, amarilla = EMA233 (las bandas, tenues)
  for (const [k, color, w] of [["bb_sup", "#F23645", 0.6], ["bb_inf", "#43A047", 0.6], ["ema233", "#F5D90A", 1.6],
                               ["ema144", "#6FA8DC", 1.6], ["ema55", "#9B6BDF", 1.6]]) {
    dibujo.push(`<path d="${tramo(d[k], x, y)}" fill="none" stroke="${color}" stroke-width="${w}" stroke-linejoin="round"/>`);
  }
  d.velas.forEach((c, k) => {
    const [o, h, l, cl] = c;
    if (o == null || cl == null) return;
    const color = cl >= o ? "#089981" : "#F23645", xx = x(k).toFixed(1);
    const y1 = y(Math.max(o, cl)), y2 = y(Math.min(o, cl));
    dibujo.push(`<line x1="${xx}" x2="${xx}" y1="${y(h).toFixed(1)}" y2="${y(l).toFixed(1)}" stroke="${color}" stroke-width="1"/>`,
      `<rect x="${(x(k) - paso * 0.36).toFixed(1)}" y="${y1.toFixed(1)}" width="${(paso * 0.72).toFixed(1)}" height="${Math.max(1, y2 - y1).toFixed(1)}" fill="${color}"/>`);
  });
  for (const op of d.operaciones || []) {
    const color = op.neto == null ? "var(--tinta)" : op.neto >= 0 ? "#089981" : "#F23645";
    if (op.k_entrada != null && op.k_salida != null && op.salida != null)
      dibujo.push(`<line x1="${x(op.k_entrada)}" y1="${y(op.entrada)}" x2="${x(op.k_salida)}" y2="${y(op.salida)}" stroke="${color}" stroke-width="1.5" stroke-dasharray="3 3"/>`);
    if (op.k_entrada != null) dibujo.push(`<circle cx="${x(op.k_entrada)}" cy="${y(op.entrada)}" r="4" fill="var(--grafico)" stroke="var(--tinta)" stroke-width="2"/>`);
    if (op.k_salida != null && op.salida != null) dibujo.push(`<rect x="${x(op.k_salida) - 4}" y="${y(op.salida) - 4}" width="8" height="8" fill="${color}"/>`);
  }
  if (d.fib) {
    for (const nv of d.fib.niveles) {
      if (nv.precio == null || nv.precio < lo || nv.precio > hi) continue;
      const yy = y(nv.precio).toFixed(1);
      dibujo.push(`<line x1="0" x2="${W}" y1="${yy}" y2="${yy}" stroke="#B39DDB" stroke-width="1" stroke-dasharray="6 4"/>`,
        `<text x="${Math.max(4, xEje - 44)}" y="${Number(yy) - 3}" font-size="10" fill="#9575CD">${nf(nv.r * 100, 1)}%</text>`);
    }
  }
  partes.push(`<g clip-path="url(#g-zona)">${dibujo.join("")}</g>`);
  const ocupadas = {};
  d.flechas.forEach((f, j) => {
    const c = d.velas[f.k];
    if (!c) return;
    const haciaAbajo = f.lado === "SHORT";
    const clave = f.k + (haciaAbajo ? "a" : "b");
    const piso = ocupadas[clave] = (ocupadas[clave] || 0) + 1;
    const base = haciaAbajo ? Math.max(y(c[1]), arriba - 4) - 6 : Math.min(y(c[2]), H - abajo + 4) + 6;
    const yy = haciaAbajo ? base - (piso - 1) * 21 : base + (piso - 1) * 21;
    const color = COLOR[f.tipo] || "#787B86";
    partes.push(`<g class="fl" data-j="${j}" role="button" aria-label="${esc(f.nombre)}"><rect x="${x(f.k) - 11}" y="${haciaAbajo ? yy - 21 : yy}" width="22" height="22" fill="transparent"/>
      <polygon points="${puntas(x(f.k), yy, haciaAbajo)}" fill="${f.tipo === "TOPE" ? "none" : color}" stroke="${color}" stroke-width="${f.tipo === "TOPE" ? 1.6 : 0}" stroke-linejoin="round"/></g>`);
  });
  partes.push(`<rect x="${xEje}" y="0" width="${der}" height="${H - abajo + 6}" fill="var(--grafico)" fill-opacity=".85"/>`);
  for (let j = 0; j <= 5; j++) {
    const v = lo + (hi - lo) * j / 5;
    partes.push(`<text x="${xEje + 6}" y="${(y(v) + 4).toFixed(1)}" font-size="11" fill="var(--suave)">${precio(v)}</text>`);
  }
  const ultimo = d.velas[n - 1];
  if (ultimo && ultimo[3] != null && n - 1 >= k0 && n - 1 <= k1 + 1) {
    const yy = y(ultimo[3]), color = ultimo[3] >= ultimo[0] ? "#089981" : "#F23645";
    partes.push(`<rect x="${xEje + 2}" y="${(yy - 9).toFixed(1)}" width="${der - 4}" height="18" rx="3" fill="${color}"/>
      <text x="${xEje + 6}" y="${(yy + 4).toFixed(1)}" font-size="11" font-weight="600" fill="#fff">${precio(ultimo[3])}</text>`);
  }
  const izquierda = caja.scrollLeft;
  caja.innerHTML = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Velas de ${esc(d.simbolo)} con las flechas del bot">${partes.join("")}</svg>`;
  caja.scrollLeft = izquierda;
  caja.querySelectorAll(".fl").forEach((g) => g.addEventListener("click", () => detalleFlecha(Number(g.dataset.j))));
}
function detalleFlecha(j) {
  const d = datosGrafico, f = d && d.flechas[j];
  if (!f) return;
  const dec = (d.decisiones || []).find((x) => x.k === f.k && x.lado === f.lado);
  const box = $("g-detalle");
  box.hidden = false;
  box.style.borderLeftColor = COLOR[f.tipo] || "var(--linea)";
  const datos = f.entrada ? `Cierre de la vela ${precio(f.cierre)}, stop ${precio(f.stop)}, puntaje ${nf(f.puntaje)}.`
                          : `Aviso en ${precio(f.precio)}, agotamiento ${nf(f.agotamiento)}.`;
  const bot = dec ? `Lo que hizo el bot en vivo: ${DECISIONES[dec.decision] || esc(dec.decision)}${dec.razon ? " (" + esc(dec.razon) + ")" : ""}.`
                  : (f.entrada ? "El bot no la registró en vivo (la moneda no estaba en su lista, el bot estaba apagado o es anterior a esta versión)." : "");
  box.innerHTML = `<strong style="color:${COLOR[f.tipo] || "inherit"}">${esc(f.nombre)}</strong> · vela de ${hora(new Date(d.t[f.k]).toISOString())}
    <div class="motivo">${datos}</div><ul>${(f.motivos || []).map((m) => `<li>${esc(m)}</li>`).join("")}</ul>
    ${bot ? `<div class="motivo">${bot}</div>` : ""}`;
}
async function verGrafico(simbolo, dias, hasta) {
  graficoPedido = true;
  $("g-estado").textContent = `Cargando ${simbolo.toUpperCase()}…`;
  try {
    const q = new URLSearchParams({simbolo, dias: String(dias || 2)});
    if (hasta) q.set("hasta", hasta);
    const d = await api("/api/grafico?" + q.toString());
    $("g-marco").hidden = false; $("g-leyenda").hidden = false; $("g-detalle").hidden = true;
    dibujarGrafico(d);
    const entradas = d.flechas.filter((f) => f.entrada).length, avisos = d.flechas.length - entradas;
    $("g-estado").textContent = `${d.simbolo.split(":")[0]}, velas de ${d.tf}, ${d.dias} ${Number(d.dias) === 1 ? "día" : "días"}: ` +
      `${entradas} ${entradas === 1 ? "flecha" : "flechas"} de entrada y ${avisos} ${avisos === 1 ? "aviso" : "avisos"}. ` +
      `Fase EMA55/EMA144 en la última vela: ${d.fase.toLowerCase()}. Círculo = entrada real del bot, cuadrado = salida.`;
  } catch (e) {
    let msg = e.message;
    try { msg = JSON.parse(msg).error || msg; } catch (_) {}
    $("g-estado").textContent = "No se pudo cargar el gráfico: " + String(msg).slice(0, 160);
  }
}
$("form-g").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  verGrafico(String(f.get("simbolo") || "").trim(), f.get("dias"), f.get("hasta"));
});

async function cargar() {
  try { pintar(await api("/api/state")); }
  catch (e) { $("frase").textContent = "No hay conexión con el bot"; $("luz").className = "luz error"; $("modo").textContent = e.message.slice(0, 160); }
}
$("form-bt").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  try { await api("/api/backtest", {days: Number(f.get("days")), min_score: f.get("min_score"), entry_tf: f.get("entry_tf"), exchange: f.get("exchange")}); await cargar(); }
  catch (e) { alert("No se pudo lanzar el backtest: " + e.message); }
});
cargar();
setInterval(cargar, 15000);
</script>
</body>
</html>
"""
