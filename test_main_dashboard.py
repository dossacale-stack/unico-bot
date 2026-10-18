"""Configuración, comandos, flujo del bot en DRY_RUN y panel web."""
import base64
import os
import subprocess
import sys
import time
from dataclasses import replace

import pandas as pd
import pytest
from aiohttp.test_utils import TestClient, TestServer

import main as bot_main
import scenarios as SC
import strategy
from config import Config
from conftest import ROOT, FakeExchangeAPI, make_cfg
from dashboard import create_app


# ----------------------------------------------------------------------
# Configuración
# ----------------------------------------------------------------------
def test_bot_mode_and_legacy_variables(monkeypatch):
    monkeypatch.setenv("BOT_MODE", "testnet")
    monkeypatch.setenv("POSITION_PCT", "0.15")
    monkeypatch.setenv("SL_PCT", "0.40")
    monkeypatch.setenv("LEVERAGE", "20")
    monkeypatch.setenv("TP_MULTIPLE", "5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    cfg = Config.from_env()
    assert cfg.mode == "TESTNET" and cfg.margin_pct == 0.15 and cfg.max_loss_pct_of_margin == 0.40
    assert cfg.leverage_cap == 20 and cfg.tp2_r == 5.0
    assert cfg.llm_provider == "anthropic" and cfg.llm_model == "claude-haiku-5-5"
    assert any("BYBIT_API_KEY" in e for e in cfg.validate())


def test_live_requires_explicit_confirmation(monkeypatch):
    monkeypatch.setenv("BOT_MODE", "LIVE")
    monkeypatch.setenv("BYBIT_API_KEY", "k")
    monkeypatch.setenv("BYBIT_API_SECRET", "s")
    assert any("CONFIRM_LIVE" in e for e in Config.from_env().validate())
    monkeypatch.setenv("CONFIRM_LIVE", "SI")
    assert Config.from_env().validate() == []


def test_init_db_command(tmp_path):
    env = {**os.environ, "DB_PATH": str(tmp_path / "x.db"), "PYTHONDONTWRITEBYTECODE": "1"}
    out = subprocess.run([sys.executable, os.path.join(ROOT, "main.py"), "--init-db"], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "WASHI_VERDE" in out.stdout and "Base de datos lista" in out.stdout


# ----------------------------------------------------------------------
# Flujo completo de un escaneo en DRY_RUN (velas de 15 minutos)
# ----------------------------------------------------------------------
SYM = "BTC/USDT:USDT"
STEP_MS = 15 * 60_000


class ScanAPI(FakeExchangeAPI):
    def __init__(self, df):
        super().__init__({SYM: float(df["close"].iloc[-1])})
        self.df = df

    async def fetch_linear_tickers(self):
        last = float(self.df["close"].iloc[-1])
        return [{"symbol": "BTCUSDT", "lastPrice": str(last), "prevPrice24h": str(last / 1.05),
                 "price24hPcnt": "0.05", "prevPrice1h": str(last / 1.01), "turnover24h": "5e8"}]

    async def fetch_ohlcv(self, symbol, timeframe, limit=None):
        return self.df

    async def fetch_ohlcv_range(self, symbol, timeframe, since_ms, until_ms=None, closed_only=True):
        if timeframe == "1m":
            return await super().fetch_ohlcv_range(symbol, timeframe, since_ms, until_ms, closed_only)
        lo = pd.Timestamp(int(since_ms), unit="ms", tz="UTC")
        hi = pd.Timestamp(int(until_ms), unit="ms", tz="UTC") if until_ms else self.df.index[-1]
        return self.df[(self.df.index >= lo) & (self.df.index <= hi)]


def make_bot(tmp_path, monkeypatch, df, **overrides):
    """Bot en DRY_RUN cuya última vela cerrada de 15 minutos es la última de `df`."""
    expected = (int(time.time() * 1000) // STEP_MS) * STEP_MS - STEP_MS
    df = df.copy()
    df.index = pd.date_range(end=pd.Timestamp(expected, unit="ms", tz="UTC"), periods=len(df), freq="15min")
    fake_now = (expected + STEP_MS + 60_000) / 1000
    monkeypatch.setattr(bot_main.time, "time", lambda: fake_now)
    monkeypatch.setattr(bot_main, "last_closed_open_ms", lambda tf, now_ms=None: expected)
    overrides.setdefault("setups", ("ROJA", "AMARILLA", "AZUL"))
    cfg = make_cfg(tmp_path, top_n_24h=5, top_n_1h=5, **overrides)
    bot = bot_main.UnicoBot(cfg)
    api = ScanAPI(df)
    bot.api = bot.broker.api = bot.hist.api = api
    return bot


@pytest.fixture
def scan_bot(tmp_path, monkeypatch):
    return make_bot(tmp_path, monkeypatch, SC.scenario_amarilla(seed=0)), strategy.SHORT


async def test_scan_opens_a_simulated_position_once_per_candle(scan_bot):
    bot, side = scan_bot
    await bot.scan()
    signals = bot.db.recent_signals("DRY_RUN")
    assert len(signals) == 1 and signals[0]["decision"] == "EJECUTADA", signals
    assert signals[0]["setup"] == "AMARILLA" and signals[0]["variant"] == "retroceso"
    positions = bot.db.open_positions("DRY_RUN")
    assert len(positions) == 1 and positions[0]["side"] == side and positions[0]["setup"] == "AMARILLA"
    await bot.scan()                                    # misma vela: no se repite
    assert len(bot.db.recent_signals("DRY_RUN")) == 1 and len(bot.db.open_positions("DRY_RUN")) == 1


async def test_pause_blocks_new_entries_but_records_the_arrow(scan_bot):
    bot, _ = scan_bot
    bot.set_paused(True)
    await bot.scan()
    assert bot.db.open_positions("DRY_RUN") == [] and "pausado" in bot.status["phase"]
    assert bot.db.recent_signals("DRY_RUN")[0]["decision"] == "PAUSADO"


async def test_close_updates_cooldown_and_arrow_memory(scan_bot):
    bot, _ = scan_bot
    row = {"symbol": SYM, "net_pnl": -50.0, "r_multiple": -1.0, "pattern_id": 1, "setup": "AMARILLA",
           "variant": "retroceso", "timeframe": "15m", "signal_id": None, "origin": "BOT"}
    bot._on_close(row)
    assert not bot.guard.check(SYM)[0]
    assert bot.setup_memory.expectancy(SYM, strategy.SETUP_CODES["AMARILLA"], "15m")["muestras"] == 1
    assert bot.setup_memory.expectancy(SYM, 1, "15m")["muestras"] == 1


async def test_memory_blocks_a_losing_arrow_and_follows_it_in_shadow(scan_bot):
    bot, _ = scan_bot
    code = strategy.SETUP_CODES["AMARILLA"]
    for _ in range(bot.cfg.setup_min_samples):
        bot.setup_memory.record("UNIVERSAL", code, "15m", -1.0)
    await bot.scan()
    sig = bot.db.recent_signals("DRY_RUN")[0]
    assert sig["decision"] == "BLOQUEADA_MEMORIA" and sig["shadow_status"] == "PENDING"
    assert bot.db.open_positions("DRY_RUN") == []


async def _open_paper(bot, side, entry, stop):
    from risk_manager import plan_position
    plan, why = plan_position(SYM, side, entry, stop, 10_000.0, bot.rp, max_leverage=50)
    assert plan is not None, why
    t = pd.Timestamp("2026-10-01", tz="UTC")
    sig = strategy.Signal(symbol=SYM, side=side, setup="ROJA" if side == strategy.SHORT else "AZUL",
                          variant="tras_techo" if side == strategy.SHORT else "retroceso", timeframe="15m",
                          trend_timeframe="1h", candle_open_time=t, candle_close_time=t, entry_ref=entry,
                          stop_loss=stop, atr=0.0, score=0.5)
    return await bot.broker.open_position(plan, sig)


async def test_green_warning_closes_a_winning_short(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, monkeypatch, SC.scenario_verde(seed=0))
    last = bot.api.prices[SYM]
    pid = await _open_paper(bot, strategy.SHORT, last * 1.06, last * 1.10)     # corto abierto más arriba: va ganando
    await bot.scan()
    row = bot.db.get_position(pid)
    assert row["status"] == "CLOSED" and row["exit_reason"] == "SALIDA_VERDE" and row["net_pnl"] > 0
    events = bot.db.recent_market_events("DRY_RUN")
    assert events and events[0]["kind"] == "VERDE" and events[0]["action"] == "CIERRE"


async def test_green_warning_keeps_a_losing_short_with_its_stop(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, monkeypatch, SC.scenario_verde(seed=0))
    last = bot.api.prices[SYM]
    pid = await _open_paper(bot, strategy.SHORT, last * 0.98, last * 1.05)     # corto abierto más abajo: va perdiendo
    await bot.scan()
    assert bot.db.get_position(pid)["status"] == "OPEN"
    assert bot.db.recent_market_events("DRY_RUN")[0]["action"] == "MANTENER"


async def test_warnings_work_even_with_entries_paused(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, monkeypatch, SC.scenario_verde(seed=0))
    last = bot.api.prices[SYM]
    pid = await _open_paper(bot, strategy.SHORT, last * 1.06, last * 1.10)
    bot.set_paused(True)
    await bot.scan()
    assert bot.db.get_position(pid)["status"] == "CLOSED"


async def test_warning_exit_can_be_disabled(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, monkeypatch, SC.scenario_verde(seed=0), exit_on_opposite=False)
    last = bot.api.prices[SYM]
    pid = await _open_paper(bot, strategy.SHORT, last * 1.06, last * 1.10)
    await bot.scan()
    assert bot.db.get_position(pid)["status"] == "OPEN"
    assert bot.db.recent_market_events("DRY_RUN")[0]["action"] == "SIN_ACCION"


async def test_shadow_follow_up_feeds_the_memory(scan_bot):
    bot, _ = scan_bot
    df = bot.api.df
    close_iso = (df.index[-60] + pd.Timedelta(minutes=15)).isoformat()
    row = {"id": 0, "symbol": SYM, "side": strategy.SHORT, "timeframe": "15m", "candle_time": close_iso,
           "stop_loss": float(df["high"].iloc[-60]) * 1.01, "decision": "BLOQUEADA_MEMORIA",
           "setup": "AMARILLA", "variant": "retroceso", "pattern_id": None}
    status, r_mult, exit_kind = await bot._shadow_eval(row)
    assert status in ("DONE", "PENDING")
    if status == "DONE":
        assert r_mult is not None and exit_kind


# ----------------------------------------------------------------------
# Panel
# ----------------------------------------------------------------------
def auth(user="karla", password="clave"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


async def test_dashboard_auth_state_and_pause(scan_bot):
    bot, _ = scan_bot
    bot.cfg = replace(bot.cfg, dashboard_password="clave")
    async with TestClient(TestServer(create_app(bot))) as client:
        assert (await client.get("/health")).status == 200
        assert (await client.get("/")).status == 401
        assert (await client.get("/", headers=auth(password="mala"))).status == 401
        page = await client.get("/", headers=auth())
        assert page.status == 200 and "UNICO BOT" in await page.text()
        await bot.scan()
        state = await (await client.get("/api/state", headers=auth())).json()
        assert state["mode"] == "DRY_RUN" and len(state["positions"]) == 1 and state["signals"]
        assert state["signals"][0]["setup"] == "AMARILLA" and state["positions"][0]["setup"] == "AMARILLA"
        assert "por_flecha" in state and "market_events" in state
        forbidden = await client.post("/api/pause", json={"paused": True}, headers=auth())
        assert forbidden.status == 403                      # sin la cabecera anti-CSRF
        ok = await client.post("/api/pause", json={"paused": True}, headers={**auth(), "X-Unico": "1"})
        assert (await ok.json())["paused"] is True and bot.paused


async def test_dashboard_disabled_without_password(scan_bot):
    bot, _ = scan_bot
    async with TestClient(TestServer(create_app(bot))) as client:
        assert (await client.get("/", headers=auth())).status == 503


async def test_startup_retries_until_bybit_answers(scan_bot):
    bot, _ = scan_bot
    calls = {"n": 0}
    original = bot.api.load_markets

    async def flaky(reload=False):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("Bybit no responde")
        return await original(reload)

    bot.api.load_markets = flaky
    bot.retry_delay = 0.01
    bot.running = True
    import asyncio
    bot._stop = asyncio.Event()
    assert await bot._connect() is True and calls["n"] == 3
    assert any("No se pudo conectar" in e["message"] for e in bot.db.recent_events("DRY_RUN"))


async def test_chart_endpoint_draws_the_bot_arrows(scan_bot):
    bot, _ = scan_bot
    bot.cfg = replace(bot.cfg, dashboard_password="clave")
    await bot.scan()
    async with TestClient(TestServer(create_app(bot))) as client:
        resp = await client.get("/api/grafico?simbolo=btc&dias=1", headers=auth())
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        n = len(data["velas"])
        assert n == 96 and len(data["t"]) == n and all(len(data[k]) == n for k in ("ema21", "ema55", "ema144", "ema233",
                                                                                  "bb_sup", "bb_inf"))
        last = [f for f in data["flechas"] if f["k"] == n - 1 and f["entrada"]]
        assert last and last[0]["tipo"] == "AMARILLA" and last[0]["motivos"]
        assert any(d["decision"] == "EJECUTADA" and d["k"] == n - 1 for d in data["decisiones"])
        assert isinstance(data["operaciones"], list)       # la entrada real cae en la vela siguiente (aún abierta)
        for name in ("BTCUSDT", "BTC/USDT:USDT"):
            assert (await client.get(f"/api/grafico?simbolo={name}", headers=auth())).status == 200
        assert (await client.get("/api/grafico?simbolo=NOEXISTE", headers=auth())).status == 404
        assert (await client.get("/api/grafico?simbolo=BTC&hasta=ayer", headers=auth())).status == 400
