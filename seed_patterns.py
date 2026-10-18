"""
seed_patterns.py — Patrones WASHI (velas respecto a las EMAs)
=============================================================
Patrones iniciales que Karla definió para que el bot reconozca comportamientos conocidos.

Cómo se usan en v8 (interpretación A, configurable más adelante):
  - Cada señal de agotamiento se compara con estos patrones.
  - Si coincide en al menos PATTERN_MIN_MATCH de los 7 rasgos, la señal queda etiquetada
    con ese patrón (pattern_id).
  - SetupMemory va guardando los resultados reales por patrón y, cuando hay muestras suficientes,
    bloquea los patrones que pierden (hit rate bajo o expectativa negativa).
  - Los campos "resultado", "rb_real" y "weight" son referencia del autor: NO se cuentan
    como operaciones ganadas (eso sesgaría el aprendizaje).

Comandos:
  python main.py --init-db     → crea las tablas e inserta estos patrones.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ✅ Lista principal definida antes de usarse
PATTERNS: List[Dict[str, Any]] = []

WASHI_PATTERNS = [
    # 🟢 WASHI - COMPRA EN SOPORTE HISTÓRICO
    {
        "symbol": "UNIVERSAL",
        "timeframe": "15m",
        "signal_type": "LONG_BREAKOUT",
        "trend": "BULLISH",
        "risk_level": "MINIMO",
        "entry_type": "ENTRADA_PRINCIPAL",
        "arrow_color": "WASHI_VERDE",
        "ema21_vs_ema55": "ABOVE",
        "ema55_vs_ema144": "ABOVE",
        "precio_vs_ema21": "TOUCHING",
        "bb_estado": "LOWER",
        "volumen": "HIGH",
        "patron_vela": "STRONG_GREEN",
        "notas": "WASHI: Compra en soporte histórico con confirmación",
        "resultado": "WIN",
        "rb_real": 5.0,
        "weight": 2.0,
    },
    # 🔴 WASHI - VENTA EN RESISTENCIA HISTÓRICA
    {
        "symbol": "UNIVERSAL",
        "timeframe": "15m",
        "signal_type": "SHORT_BREAKOUT",
        "trend": "BEARISH",
        "risk_level": "MINIMO",
        "entry_type": "ENTRADA_PRINCIPAL",
        "arrow_color": "WASHI_ROJA",
        "ema21_vs_ema55": "BELOW",
        "ema55_vs_ema144": "BELOW",
        "precio_vs_ema21": "TOUCHING",
        "bb_estado": "UPPER",
        "volumen": "HIGH",
        "patron_vela": "REJECTION",
        "notas": "WASHI: Venta en resistencia histórica con confirmación",
        "resultado": "WIN",
        "rb_real": 5.0,
        "weight": 2.0,
    },
    # 🟡 WASHI - SQUEEZE
    {
        "symbol": "UNIVERSAL",
        "timeframe": "15m",
        "signal_type": "LONG_BREAKOUT",
        "trend": "BULLISH",
        "risk_level": "BAJO",
        "entry_type": "ENTRADA_PRINCIPAL",
        "arrow_color": "WASHI_DORADA",
        "ema21_vs_ema55": "FLAT",
        "ema55_vs_ema144": "FLAT",
        "precio_vs_ema21": "NEAR",
        "bb_estado": "SQUEEZE",
        "volumen": "VERY_LOW",
        "patron_vela": "NEUTRAL",
        "notas": "WASHI: Squeeze detectado - esperando breakout",
        "resultado": "WIN",
        "rb_real": 5.0,
        "weight": 1.5,
    },
]

# ✅ PATTERNS ya existe, ahora se extiende sin error
PATTERNS.extend(WASHI_PATTERNS)

FEATURE_KEYS = (
    "trend", "ema21_vs_ema55", "ema55_vs_ema144", "precio_vs_ema21",
    "bb_estado", "volumen", "patron_vela",
)

# Umbrales para traducir números a las categorías de los patrones (configurables aquí)
EMA_FLAT_PCT = 0.001       # diferencia menor a 0,1% entre EMAs = FLAT
NEAR_PCT = 0.005           # precio a menos de 0,5% de la EMA21 = NEAR
BB_EDGE = 0.2              # %B <= 0,2 = LOWER ; >= 0,8 = UPPER
SQUEEZE_QUANTILE = 0.20    # ancho de banda en el 20% más bajo de las últimas 100 velas
VOL_HIGH = 1.5
VOL_LOW = 0.8
VOL_VERY_LOW = 0.5
STRONG_BODY = 0.6          # cuerpo >= 60% del rango = vela fuerte
REJECTION_WICK = 2.0       # mecha >= 2 veces el cuerpo = rechazo

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pattern_templates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    symbol          TEXT,
    timeframe       TEXT,
    signal_type     TEXT NOT NULL,
    trend           TEXT,
    risk_level      TEXT,
    entry_type      TEXT,
    arrow_color     TEXT,
    ema21_vs_ema55  TEXT,
    ema55_vs_ema144 TEXT,
    precio_vs_ema21 TEXT,
    bb_estado       TEXT,
    volumen         TEXT,
    patron_vela     TEXT,
    notas           TEXT,
    resultado_ref   TEXT,
    rb_real         REAL,
    weight          REAL DEFAULT 1.0,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
)
"""


def pattern_name(p: Dict[str, Any]) -> str:
    return p.get("name") or p.get("arrow_color") or p.get("signal_type", "PATRON")


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(TABLE_SQL)
    conn.commit()


def insert_patterns(conn: sqlite3.Connection, patterns: List[Dict[str, Any]]) -> int:
    init_db(conn)
    inserted = 0
    for p in patterns:
        cur = conn.execute(
            """INSERT OR IGNORE INTO pattern_templates
               (name, symbol, timeframe, signal_type, trend, risk_level, entry_type, arrow_color,
                ema21_vs_ema55, ema55_vs_ema144, precio_vs_ema21, bb_estado, volumen, patron_vela,
                notas, resultado_ref, rb_real, weight)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pattern_name(p), p.get("symbol"), p.get("timeframe"), p["signal_type"], p.get("trend"),
                p.get("risk_level"), p.get("entry_type"), p.get("arrow_color"),
                p.get("ema21_vs_ema55"), p.get("ema55_vs_ema144"), p.get("precio_vs_ema21"),
                p.get("bb_estado"), p.get("volumen"), p.get("patron_vela"), p.get("notas"),
                p.get("resultado"), p.get("rb_real"), p.get("weight", 1.0),
            ),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def load_templates(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    init_db(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM pattern_templates ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def print_summary(conn: sqlite3.Connection) -> None:
    templates = load_templates(conn)
    print(f"\nPatrones cargados: {len(templates)}")
    for t in templates:
        print(
            f"  #{t['id']:>3} {t['name']:<14} {t['signal_type']:<15} tf={t['timeframe']:<4} "
            f"EMA21/55={t['ema21_vs_ema55']:<6} BB={t['bb_estado']:<8} vela={t['patron_vela']}"
        )


# ---------------------------------------------------------------------------
# Clasificación de la vela actual con el vocabulario de los patrones
# ---------------------------------------------------------------------------
def _relation(a: float, b: float) -> str:
    if b == 0 or not np.isfinite(a) or not np.isfinite(b):
        return "FLAT"
    diff = (a - b) / b
    if diff > EMA_FLAT_PCT:
        return "ABOVE"
    if diff < -EMA_FLAT_PCT:
        return "BELOW"
    return "FLAT"


def extract_features(feat: pd.DataFrame, i: int = -1) -> Dict[str, str]:
    """Traduce la vela i (con indicadores de strategy.build_features) a los rasgos del patrón."""
    if i < 0:
        i = len(feat) + i
    row = feat.iloc[i]
    o, h, l, c = (float(row[k]) for k in ("open", "high", "low", "close"))

    stack = int(row.get("stack", 0))
    trend = {1: "BULLISH", -1: "BEARISH"}.get(stack, "SIDEWAYS")

    ema21 = float(row["ema21"])
    if l <= ema21 <= h:
        precio = "TOUCHING"
    elif ema21 > 0 and abs(c - ema21) / ema21 <= NEAR_PCT:
        precio = "NEAR"
    else:
        precio = "ABOVE" if c > ema21 else "BELOW"

    bw_hist = feat["bb_bandwidth"].iloc[max(0, i - 99): i + 1].dropna()
    bandwidth = float(row["bb_bandwidth"])
    upper, lower = float(row["bb_upper"]), float(row["bb_lower"])
    if len(bw_hist) >= 20 and np.isfinite(bandwidth) and bandwidth <= bw_hist.quantile(SQUEEZE_QUANTILE):
        bb = "SQUEEZE"
    elif np.isfinite(upper) and np.isfinite(lower) and upper > lower:
        pct_b = (c - lower) / (upper - lower)
        bb = "UPPER" if pct_b >= 1 - BB_EDGE else "LOWER" if pct_b <= BB_EDGE else "MID"
    else:
        bb = "MID"

    vr = float(row["vol_ratio"]) if np.isfinite(float(row["vol_ratio"])) else 1.0
    if vr >= VOL_HIGH:
        vol = "HIGH"
    elif vr <= VOL_VERY_LOW:
        vol = "VERY_LOW"
    elif vr < VOL_LOW:
        vol = "LOW"
    else:
        vol = "NORMAL"

    rng = h - l
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    wick = max(upper_wick, lower_wick)
    if rng > 0 and wick >= REJECTION_WICK * max(body, rng * 0.05) and wick >= 0.5 * rng:
        vela = "REJECTION"
    elif rng > 0 and body >= STRONG_BODY * rng:
        vela = "STRONG_GREEN" if c > o else "STRONG_RED"
    else:
        vela = "NEUTRAL"

    return {
        "trend": trend,
        "ema21_vs_ema55": _relation(ema21, float(row["ema_fast"])),
        "ema55_vs_ema144": _relation(float(row["ema_fast"]), float(row["ema_mid"])),
        "precio_vs_ema21": precio,
        "bb_estado": bb,
        "volumen": vol,
        "patron_vela": vela,
    }


def side_of(signal_type: str) -> str:
    return "LONG" if str(signal_type).upper().startswith("LONG") else "SHORT"


def match_pattern(features: Dict[str, str], side: str, templates: List[Dict[str, Any]],
                  min_match: int = 5) -> Tuple[Optional[Dict[str, Any]], int]:
    """Busca el patrón de la misma dirección que más rasgos comparte. Devuelve (patrón, coincidencias)."""
    best: Optional[Dict[str, Any]] = None
    best_hits = -1
    for t in templates:
        if side_of(t["signal_type"]) != side:
            continue
        hits = sum(1 for k in FEATURE_KEYS if t.get(k) and t.get(k) == features.get(k))
        if hits > best_hits or (hits == best_hits and best is not None and (t.get("weight") or 0) > (best.get("weight") or 0)):
            best, best_hits = t, hits
    if best is None or best_hits < min_match:
        return None, max(best_hits, 0)
    return best, best_hits
