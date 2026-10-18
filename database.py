"""
database.py — Base de datos SQLite de UNICO BOT
================================================
Guarda todo lo que el bot necesita recordar entre reinicios:
  - posiciones (abiertas y cerradas) con su SL/TP, comisiones y resultado
  - fills (cada entrada, toma parcial, cierre o movimiento de SL)
  - señales evaluadas (aprobadas, vetadas y por qué), con su flecha (setup) y variante
  - avisos de mercado v9: flecha VERDE (agotamiento de ventas) y TOPE (agotamiento de compras)
  - estado (kill switch, cooldowns, pausa, saldo simulado, memoria de la lista de monedas)
  - curva de capital y eventos para el panel

Las bases creadas con la v8 se actualizan solas al arrancar (se añaden las columnas nuevas).

En Railway, DB_PATH debe apuntar a un Volume (por ejemplo /data/unico.db);
si no, la base se borra con cada despliegue.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    mode              TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    status            TEXT NOT NULL,
    origin            TEXT NOT NULL DEFAULT 'BOT',
    opened_at         TEXT NOT NULL,
    closed_at         TEXT,
    timeframe         TEXT,
    entry_price       REAL NOT NULL,
    contracts_initial REAL NOT NULL,
    contracts_open    REAL NOT NULL,
    leverage          INTEGER,
    margin_usd        REAL,
    risk_usd          REAL,
    sl_initial        REAL,
    sl_current        REAL,
    tp1_price         REAL,
    tp1_qty           REAL DEFAULT 0,
    tp1_filled        INTEGER DEFAULT 0,
    tp2_price         REAL,
    tp2_qty           REAL DEFAULT 0,
    tp2_filled        INTEGER DEFAULT 0,
    be_trigger        REAL,
    be_price          REAL,
    be_set            INTEGER DEFAULT 0,
    trail_active      INTEGER DEFAULT 0,
    highest           REAL,
    lowest            REAL,
    atr_entry         REAL,
    fees_paid         REAL DEFAULT 0,
    funding_paid      REAL DEFAULT 0,
    realized_pnl      REAL DEFAULT 0,
    exit_price_avg    REAL,
    exit_reason       TEXT,
    net_pnl           REAL,
    r_multiple        REAL,
    exchange_pnl      REAL,
    signal_id         INTEGER,
    pattern_id        INTEGER,
    setup             TEXT,
    variant           TEXT,
    score             REAL,
    client_order_id   TEXT,
    last_check_ms     INTEGER,
    next_funding_ms   INTEGER,
    meta              TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_mode_status ON positions(mode, status);

CREATE TABLE IF NOT EXISTS fills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    price       REAL,
    qty         REAL,
    fee         REAL DEFAULT 0,
    pnl         REAL DEFAULT 0,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_fills_position ON fills(position_id);

CREATE TABLE IF NOT EXISTS signals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT NOT NULL,
    mode                TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL,
    timeframe           TEXT NOT NULL,
    candle_time         TEXT NOT NULL,
    setup               TEXT,
    variant             TEXT,
    score               REAL,
    components          TEXT,
    rejection           TEXT,
    rejection_event_id  INTEGER,
    pattern_id          INTEGER,
    pattern_name        TEXT,
    hist                TEXT,
    entry_ref           REAL,
    stop_loss           REAL,
    target              REAL,
    atr                 REAL,
    decision            TEXT,
    reason              TEXT,
    llm_provider        TEXT,
    llm_decision        TEXT,
    llm_confidence      REAL,
    llm_reason          TEXT,
    llm_latency_ms      INTEGER,
    position_id         INTEGER,
    shadow_status       TEXT,
    shadow_r            REAL,
    shadow_exit         TEXT,
    UNIQUE(mode, symbol, timeframe, candle_time, side)
);
CREATE INDEX IF NOT EXISTS idx_signals_mode_ts ON signals(mode, ts);

CREATE TABLE IF NOT EXISTS state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS equity (
    ts             TEXT NOT NULL,
    mode           TEXT NOT NULL,
    equity         REAL,
    balance        REAL,
    upnl           REAL,
    open_positions INTEGER
);
CREATE INDEX IF NOT EXISTS idx_equity_mode_ts ON equity(mode, ts);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    mode    TEXT,
    level   TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    mode        TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    candle_time TEXT NOT NULL,
    kind        TEXT NOT NULL,
    price       REAL,
    close       REAL,
    exhaustion  REAL,
    reasons     TEXT,
    action      TEXT,
    note        TEXT,
    UNIQUE(mode, symbol, timeframe, candle_time, kind)
);
CREATE INDEX IF NOT EXISTS idx_market_events_mode_ts ON market_events(mode, ts);
"""

# Columnas añadidas después de la v8: (tabla, columna, tipo). Se agregan con ALTER TABLE si faltan.
MIGRATIONS = [
    ("positions", "setup", "TEXT"),
    ("positions", "variant", "TEXT"),
    ("signals", "setup", "TEXT"),
    ("signals", "variant", "TEXT"),
    ("signals", "target", "REAL"),
]

POSITION_COLUMNS = {
    "mode", "symbol", "side", "status", "origin", "opened_at", "closed_at", "timeframe",
    "entry_price", "contracts_initial", "contracts_open", "leverage", "margin_usd", "risk_usd",
    "sl_initial", "sl_current", "tp1_price", "tp1_qty", "tp1_filled", "tp2_price", "tp2_qty",
    "tp2_filled", "be_trigger", "be_price", "be_set", "trail_active", "highest", "lowest",
    "atr_entry", "fees_paid", "funding_paid", "realized_pnl", "exit_price_avg", "exit_reason",
    "net_pnl", "r_multiple", "exchange_pnl", "signal_id", "pattern_id", "setup", "variant", "score",
    "client_order_id", "last_check_ms", "next_funding_ms", "meta",
}

SIGNAL_COLUMNS = {
    "ts", "mode", "symbol", "side", "timeframe", "candle_time", "setup", "variant", "score", "components",
    "rejection", "rejection_event_id", "pattern_id", "pattern_name", "hist", "entry_ref",
    "stop_loss", "target", "atr", "decision", "reason", "llm_provider", "llm_decision", "llm_confidence",
    "llm_reason", "llm_latency_ms", "position_id", "shadow_status", "shadow_r", "shadow_exit",
}

MARKET_EVENT_COLUMNS = {
    "ts", "mode", "symbol", "timeframe", "candle_time", "kind", "price", "close", "exhaustion",
    "reasons", "action", "note",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


class Database:
    """Conexión única (con candado) compartida por el bot y el panel."""

    def __init__(self, path: str):
        self.path = path
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            if path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()

    def _migrate(self) -> None:
        """Añade a una base antigua (v8) las columnas nuevas sin tocar los datos."""
        for table, column, kind in MIGRATIONS:
            existing = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    # ------------------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------------
    # Estado clave/valor (kill switch, cooldowns, saldo simulado, pausa)
    # ------------------------------------------------------------------
    def get_state(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM state WHERE key = ?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except (TypeError, json.JSONDecodeError):
            return default

    def set_state(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value, default=str), iso()),
        )

    # ------------------------------------------------------------------
    # Posiciones
    # ------------------------------------------------------------------
    def insert_position(self, **fields: Any) -> int:
        unknown = set(fields) - POSITION_COLUMNS
        if unknown:
            raise ValueError(f"Columnas desconocidas en positions: {unknown}")
        fields.setdefault("status", "OPEN")
        fields.setdefault("opened_at", iso())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.execute(
            f"INSERT INTO positions ({cols}) VALUES ({marks})",
            [_json(v) for v in fields.values()],
        )
        return int(cur.lastrowid)

    def update_position(self, position_id: int, **fields: Any) -> None:
        if not fields:
            return
        unknown = set(fields) - POSITION_COLUMNS
        if unknown:
            raise ValueError(f"Columnas desconocidas en positions: {unknown}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(
            f"UPDATE positions SET {sets} WHERE id = ?",
            [_json(v) for v in fields.values()] + [position_id],
        )

    def get_position(self, position_id: int) -> Optional[Dict[str, Any]]:
        rows = self.query("SELECT * FROM positions WHERE id = ?", (position_id,))
        return rows[0] if rows else None

    def open_positions(self, mode: str) -> List[Dict[str, Any]]:
        return self.query(
            "SELECT * FROM positions WHERE mode = ? AND status = 'OPEN' ORDER BY id", (mode,)
        )

    def active_positions(self, mode: str) -> List[Dict[str, Any]]:
        """Abiertas o pendientes de confirmar en el exchange (cuentan para MAX_POSITIONS)."""
        return self.query(
            "SELECT * FROM positions WHERE mode = ? AND status IN ('OPEN', 'PENDING') ORDER BY id", (mode,)
        )

    def open_position_for(self, mode: str, symbol: str) -> Optional[Dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM positions WHERE mode = ? AND symbol = ? AND status = 'OPEN' ORDER BY id DESC LIMIT 1",
            (mode, symbol),
        )
        return rows[0] if rows else None

    def closed_positions(self, mode: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self.query(
            "SELECT * FROM positions WHERE mode = ? AND status = 'CLOSED' ORDER BY closed_at DESC LIMIT ?",
            (mode, limit),
        )

    def add_fill(self, position_id: int, kind: str, price: float | None, qty: float | None,
                 fee: float = 0.0, pnl: float = 0.0, note: str = "", ts: Optional[str] = None) -> None:
        self.execute(
            "INSERT INTO fills (position_id, ts, kind, price, qty, fee, pnl, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (position_id, ts or iso(), kind, price, qty, fee, pnl, note),
        )

    def fills_for(self, position_id: int) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM fills WHERE position_id = ? ORDER BY id", (position_id,))

    # ------------------------------------------------------------------
    # Señales
    # ------------------------------------------------------------------
    def record_signal(self, **fields: Any) -> tuple[int, bool]:
        """Inserta una señal. Devuelve (id, es_nueva). Una vela solo genera una señal."""
        unknown = set(fields) - SIGNAL_COLUMNS
        if unknown:
            raise ValueError(f"Columnas desconocidas en signals: {unknown}")
        fields.setdefault("ts", iso())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._lock:
            cur = self.conn.execute(
                f"INSERT OR IGNORE INTO signals ({cols}) VALUES ({marks})",
                [_json(v) for v in fields.values()],
            )
            self.conn.commit()
            if cur.rowcount:
                return int(cur.lastrowid), True
            row = self.conn.execute(
                "SELECT id FROM signals WHERE mode = ? AND symbol = ? AND timeframe = ? AND candle_time = ? AND side = ?",
                (fields["mode"], fields["symbol"], fields["timeframe"], fields["candle_time"], fields["side"]),
            ).fetchone()
            return int(row["id"]), False

    def update_signal(self, signal_id: int, **fields: Any) -> None:
        if not fields:
            return
        unknown = set(fields) - SIGNAL_COLUMNS
        if unknown:
            raise ValueError(f"Columnas desconocidas en signals: {unknown}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE signals SET {sets} WHERE id = ?", [_json(v) for v in fields.values()] + [signal_id])

    def recent_signals(self, mode: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM signals WHERE mode = ? ORDER BY id DESC LIMIT ?", (mode, limit))

    def pending_shadow_signals(self, mode: str, older_than: datetime, limit: int = 50) -> List[Dict[str, Any]]:
        return self.query(
            "SELECT * FROM signals WHERE mode = ? AND decision IN ('VETO_LLM', 'BLOQUEADA_MEMORIA') "
            "AND (shadow_status IS NULL OR shadow_status = 'PENDING') AND ts <= ? ORDER BY id LIMIT ?",
            (mode, iso(older_than), limit),
        )

    def signals_between(self, mode: str, symbol: str, since: datetime,
                        until: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Señales de una moneda en un rango de velas (para dibujarlas en el gráfico)."""
        until_iso = iso(until) if until else "9999"
        return self.query(
            "SELECT id, side, setup, variant, candle_time, score, decision, reason, entry_ref, stop_loss, position_id "
            "FROM signals WHERE mode = ? AND symbol = ? AND candle_time >= ? AND candle_time <= ? ORDER BY candle_time",
            (mode, symbol, iso(since), until_iso),
        )

    # ------------------------------------------------------------------
    # Avisos de mercado (flecha VERDE y TOPE)
    # ------------------------------------------------------------------
    def record_market_event(self, **fields: Any) -> tuple[int, bool]:
        """Guarda un aviso. Devuelve (id, es_nuevo). Una vela solo genera un aviso de cada tipo."""
        unknown = set(fields) - MARKET_EVENT_COLUMNS
        if unknown:
            raise ValueError(f"Columnas desconocidas en market_events: {unknown}")
        fields.setdefault("ts", iso())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._lock:
            cur = self.conn.execute(
                f"INSERT OR IGNORE INTO market_events ({cols}) VALUES ({marks})",
                [_json(v) for v in fields.values()],
            )
            self.conn.commit()
            if cur.rowcount:
                return int(cur.lastrowid), True
            row = self.conn.execute(
                "SELECT id FROM market_events WHERE mode = ? AND symbol = ? AND timeframe = ? AND candle_time = ? "
                "AND kind = ?",
                (fields["mode"], fields["symbol"], fields["timeframe"], fields["candle_time"], fields["kind"]),
            ).fetchone()
            return int(row["id"]), False

    def update_market_event(self, event_id: int, **fields: Any) -> None:
        if not fields:
            return
        unknown = set(fields) - MARKET_EVENT_COLUMNS
        if unknown:
            raise ValueError(f"Columnas desconocidas en market_events: {unknown}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE market_events SET {sets} WHERE id = ?",
                     [_json(v) for v in fields.values()] + [event_id])

    def recent_market_events(self, mode: str, limit: int = 30,
                             symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        if symbol:
            return self.query(
                "SELECT * FROM market_events WHERE mode = ? AND symbol = ? ORDER BY candle_time DESC LIMIT ?",
                (mode, symbol, limit),
            )
        return self.query("SELECT * FROM market_events WHERE mode = ? ORDER BY id DESC LIMIT ?", (mode, limit))

    # ------------------------------------------------------------------
    # Capital y eventos
    # ------------------------------------------------------------------
    def add_equity(self, mode: str, equity: float, balance: float, upnl: float, open_positions: int) -> None:
        self.execute(
            "INSERT INTO equity (ts, mode, equity, balance, upnl, open_positions) VALUES (?, ?, ?, ?, ?, ?)",
            (iso(), mode, equity, balance, upnl, open_positions),
        )

    def equity_series(self, mode: str, since: datetime) -> List[Dict[str, Any]]:
        return self.query(
            "SELECT ts, equity FROM equity WHERE mode = ? AND ts >= ? ORDER BY ts", (mode, iso(since))
        )

    def add_event(self, mode: str, level: str, message: str) -> None:
        self.execute(
            "INSERT INTO events (ts, mode, level, message) VALUES (?, ?, ?, ?)",
            (iso(), mode, level.upper(), message[:1000]),
        )

    def recent_events(self, mode: str, limit: int = 30) -> List[Dict[str, Any]]:
        return self.query(
            "SELECT ts, level, message FROM events WHERE mode = ? OR mode IS NULL ORDER BY id DESC LIMIT ?",
            (mode, limit),
        )

    def prune(self, keep_days: int = 90) -> None:
        cutoff = iso(utcnow() - timedelta(days=keep_days))
        self.execute("DELETE FROM equity WHERE ts < ?", (cutoff,))
        self.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        self.execute("DELETE FROM market_events WHERE ts < ?", (cutoff,))
