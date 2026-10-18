"""Base de datos (migración desde la v8) y memoria de resultados por flecha."""
import sqlite3

import pytest

import database
from database import Database
from setup_memory import SetupMemory, UNIVERSAL


def v8_schema() -> str:
    """El esquema de la v8: el actual sin las columnas nuevas ni la tabla de avisos."""
    lines = [l for l in database.SCHEMA.splitlines() if l.strip() not in ("setup             TEXT,",
                                                                          "variant           TEXT,",
                                                                          "setup               TEXT,",
                                                                          "variant             TEXT,",
                                                                          "target              REAL,")]
    text = "\n".join(lines)
    return text[: text.index("CREATE TABLE IF NOT EXISTS market_events")]


def test_v8_database_is_migrated_without_losing_data(tmp_path):
    path = tmp_path / "v8.db"
    con = sqlite3.connect(path)
    con.executescript(v8_schema())
    con.execute("INSERT INTO positions (mode, symbol, side, status, opened_at, entry_price, contracts_initial, "
                "contracts_open) VALUES ('LIVE', 'LAB/USDT:USDT', 'SHORT', 'OPEN', '2026-10-01T00:00:00+00:00', 1, 1, 1)")
    con.commit()
    assert "setup" not in {r[1] for r in con.execute("PRAGMA table_info(positions)")}
    con.close()

    db = Database(str(path))
    assert {"setup", "variant"} <= {r[1] for r in db.conn.execute("PRAGMA table_info(positions)")}
    assert {"setup", "variant"} <= {r[1] for r in db.conn.execute("PRAGMA table_info(signals)")}
    assert db.open_positions("LIVE")[0]["symbol"] == "LAB/USDT:USDT"           # los datos siguen ahí
    pid = db.insert_position(mode="LIVE", symbol="SKY/USDT:USDT", side="LONG", entry_price=1, contracts_initial=1,
                             contracts_open=1, setup="AZUL", variant="tras_verde")
    assert db.get_position(pid)["variant"] == "tras_verde"
    Database(str(path))                                                         # arrancar dos veces no rompe nada


def test_market_events_are_recorded_once_per_candle(tmp_path):
    db = Database(str(tmp_path / "x.db"))
    args = dict(mode="DRY_RUN", symbol="LAB/USDT:USDT", timeframe="15m", candle_time="2026-10-09T12:15:00+00:00",
                kind="VERDE", price=8.5, close=8.7, exhaustion=0.6, reasons=["mínimo de 12 h"], action="AVISO")
    first, new = db.record_market_event(**args)
    again, new2 = db.record_market_event(**args)
    assert new and not new2 and first == again
    db.update_market_event(first, action="CIERRE", note="posición #1")
    row = db.recent_market_events("DRY_RUN")[0]
    assert row["action"] == "CIERRE" and row["kind"] == "VERDE"
    with pytest.raises(ValueError):
        db.record_market_event(**{**args, "inventado": 1})


def test_memory_needs_enough_samples_and_a_real_loss_to_block():
    mem = SetupMemory.in_memory(min_samples=20)
    for _ in range(19):
        mem.record("LAB", 1002, "15m", -1.0)
    assert mem.expectancy("LAB", 1002, "15m")["permitido"]                     # 19 < 20: aún no decide
    mem.record("LAB", 1002, "15m", -1.0)
    verdict = mem.expectancy("LAB", 1002, "15m")
    assert not verdict["permitido"] and verdict["muestras"] == 20


def test_memory_does_not_block_a_normal_losing_streak():
    """Con 2R/5R se pierde más veces de las que se gana: una mala racha no debe bloquear la flecha."""
    mem = SetupMemory.in_memory(min_samples=20)
    for r in [-1.0] * 15 + [2.5] * 5:                                          # 25 % de aciertos: media -0,125R
        mem.record("SKY", 1004, "15m", r)
    assert mem.expectancy("SKY", 1004, "15m")["permitido"]                      # -0,125 + error estándar > 0: racha
    clear = SetupMemory.in_memory(min_samples=20)
    for r in [-1.0] * 16 + [2.0] * 4:                                          # media -0,4R: pierde de verdad
        clear.record("SKY", 1004, "15m", r)
    assert not clear.expectancy("SKY", 1004, "15m")["permitido"]


def test_memory_universal_fallback_and_shadow_recovery():
    mem = SetupMemory.in_memory(min_samples=20)
    for _ in range(30):
        mem.record("OTRA", 1003, "15m", -1.0)
    verdict = mem.check("NUEVA", "15m", [(1003, "Flecha AZUL_RETROCESO"), (None, "sin patrón")])
    assert not verdict["permitido"] and "AZUL_RETROCESO" in verdict["razon"]   # usa el dato UNIVERSAL
    for _ in range(60):
        mem.record("OTRA", 1003, "15m", 2.0, shadow=True)                       # en sombra vuelve a funcionar
    assert mem.check("NUEVA", "15m", [(1003, "Flecha AZUL_RETROCESO")])["permitido"]
    summary = {row["code"]: row for row in mem.summary(UNIVERSAL, "15m")}
    assert summary[1003]["shadow_n"] == 60 and summary[1003]["n"] == 90
