"""
seed_patterns.py — Base de Datos de Patrones ÚNICO STRATEGY
============================================================
Versión 4.0 — MÚLTIPLES TIMEFRAMES (M15 + M3)
R:R Fijo: 5:1 | SL: 4% capital | TP: 20% capital
"""

import sqlite3
from datetime import datetime, timezone

DB_PATH = "patterns.db"

PATTERNS = [

    # ═══════════════════════════════════════════════════════════════
    #  🟢 GRUPO M15 — ENTRADA PERFECTA (Verde clara)
    # ═══════════════════════════════════════════════════════════════

    {
        "symbol": "UNIVERSAL",
        "timeframe": "15m",
        "signal_type": "LONG_BREAKOUT",
        "trend": "BULLISH",
        "risk_level": "MINIMO",
        "entry_type": "ENTRADA_PRINCIPAL",
        "arrow_color": "VERDE_CLARA",
        "ema21_vs_ema55": "ABOVE",
        "ema55_vs_ema144": "CROSSING_UP",
        "ema144_vs_ema233": "FLAT_TO_UP",
        "ema21_slope": "UP",
        "ema144_slope": "UP",
        "precio_vs_ema21": "ABOVE",
        "precio_vs_ema55": "ABOVE",
        "precio_vs_ema144": "ABOVE",
        "precio_vs_ema233": "ABOVE",
        "bb_estado": "EXPANDING",
        "bb_precio": "MID_TO_UPPER",
        "volumen": "HIGH",
        "patron_vela": "STRONG_GREEN",
        "fib_zona": "0.786_1.000",
        "notas": "ENTRADA PERFECTA M15 - Cascada alcista completa | R:R 5:1",
        "resultado": "WIN",
        "rb_real": 5.0,
        "weight": 1.5,
    },

    {
        "symbol": "UNIVERSAL",
        "timeframe": "15m",
        "signal_type": "LONG_BREAKOUT",
        "trend": "BULLISH",
        "risk_level": "MINIMO",
        "entry_type": "ENTRADA_PRINCIPAL",
        "arrow_color": "VERDE_CLARA",
        "ema21_vs_ema55": "FLAT",
        "ema55_vs_ema144": "FLAT",
        "ema144_vs_ema233": "FLAT",
        "ema21_slope": "FLAT",
        "ema144_slope": "FLAT",
        "precio_vs_ema21": "NEAR",
        "bb_estado": "MAX_SQUEEZE",
        "bb_precio": "MID",
        "volumen": "VERY_LOW",
        "patron_vela": "STRONG_GREEN",
        "dias_squeeze": 3,
        "fib_zona": "N/A_NUEVO_MOVIMIENTO",
        "notas": "SQUEEZE PROLONGADO M15 - El patrón más rentable | R:R 5:1",
        "resultado": "WIN",
        "rb_real": 5.0,
        "weight": 2.0,
    },

    # ═══════════════════════════════════════════════════════════════
    #  🟢 GRUPO M15 — ENTRADA BAJO RIESGO (Verde oscura)
    # ═══════════════════════════════════════════════════════════════

    {
        "symbol": "UNIVERSAL",
        "timeframe": "15m",
        "signal_type": "LONG_REVERSAL",
        "trend": "BULLISH",
        "risk_level": "BAJO",
        "entry_type": "ENTRADA_PRINCIPAL",
        "arrow_color": "VERDE_OSCURA",
        "ema21_vs_ema55": "CROSSING_UP",
        "ema55_vs_ema144": "BELOW",
        "ema144_vs_ema233": "FLAT",
        "ema21_slope": "UP",
        "ema144_slope": "FLAT",
        "precio_vs_ema21": "TOUCHING",
        "precio_vs_ema144": "TOUCHING",
        "precio_vs_ema233": "ABOVE",
        "bb_estado": "EXPANDING",
        "bb_precio": "LOWER",
        "volumen": "HIGH",
        "patron_vela": "STRONG_GREEN",
        "fib_zona": "0.618_0.786",
        "notas": "ENTRADA BAJO RIESGO M15 - Precio tocando EMA144 | R:R 5:1",
        "resultado": "WIN",
        "rb_real": 5.0,
        "weight": 1.0,
    },

    # ═══════════════════════════════════════════════════════════════
    #  🔴 GRUPO M15 — VENTA AGRESIVA (Roja)
    # ═══════════════════════════════════════════════════════════════

    {
        "symbol": "UNIVERSAL",
        "timeframe": "15m",
        "signal_type": "SHORT_BREAKOUT",
        "trend": "BEARISH",
        "risk_level": "BAJO",
        "entry_type": "ENTRADA_PRINCIPAL",
        "arrow_color": "ROJA",
        "ema21_vs_ema55": "BELOW",
        "ema55_vs_ema144": "BELOW",
        "ema144_vs_ema233": "BELOW",
        "ema21_slope": "DOWN",
        "ema144_slope": "DOWN",
        "precio_vs_ema21": "TOUCHING",
        "precio_vs_ema55": "BELOW",
        "precio_vs_ema144": "BELOW",
        "precio_vs_ema233": "BELOW",
        "bb_estado": "EXPANDING",
        "bb_precio": "UPPER",
        "volumen": "HIGH",
        "patron_vela": "REJECTION",
        "fib_zona": "0.0_0.382",
        "notas": "VENTA AGRESIVA M15 - Rechazo en EMA21 | R:R 5:1",
        "resultado": "WIN",
        "rb_real": 5.0,
        "weight": 1.0,
    },

    # ═══════════════════════════════════════════════════════════════
    #  🟡 GRUPO M15 — VENTA MENOR RIESGO (Dorada)
    # ═══════════════════════════════════════════════════════════════

    {
        "symbol": "UNIVERSAL",
        "timeframe": "15m",
        "signal_type": "SHORT_REVERSAL",
        "trend": "BEARISH",
        "risk_level": "BAJO",
        "entry_type": "ENTRADA_PRINCIPAL",
        "arrow_color": "DORADA",
        "ema21_vs_ema55": "BELOW",
        "ema55_vs_ema144": "BELOW",
        "ema144_vs_ema233": "BELOW",
        "ema21_slope": "DOWN",
        "ema144_slope": "DOWN",
        "precio_vs_ema21": "TOUCHING",
        "precio_vs_ema55": "BELOW",
        "precio_vs_ema144": "TOUCHING",
        "precio_vs_ema233": "ABOVE",
        "bb_estado": "SQUEEZE",
        "bb_precio": "MID",
        "volumen": "LOW",
        "patron_vela": "BEARISH_ENGULF",
        "fib_zona": "0.382_0.618",
        "notas": "VENTA MENOR RIESGO M15 - Rechazo en EMA144 | R:R 5:1",
        "resultado": "WIN",
        "rb_real": 5.0,
        "weight": 0.8,
    },

    # ═══════════════════════════════════════════════════════════════
    #  🟢 GRUPO M3 — ENTRADA PERFECTA (Verde clara)
    # ═══════════════════════════════════════════════════════════════

    {
        "symbol": "UNIVERSAL",
        "timeframe": "3m",
        "signal_type": "LONG_BREAKOUT",
        "trend": "BULLISH",
        "risk_level": "MINIMO",
        "entry_type": "ENTRADA_PRINCIPAL",
        "arrow_color": "VERDE_CLARA",
        "ema21_vs_ema55": "CROSSING_UP",
        "ema55_vs_ema144": "ABOVE",
        "ema144_vs_ema233": "ABOVE",
        "ema21_slope": "UP",
        "ema144_slope": "UP",
        "precio_vs_ema21": "ABOVE",
        "precio_vs_ema55": "ABOVE",
        "precio_vs_ema144": "ABOVE",
        "precio_vs_ema233": "ABOVE",
        "bb_estado": "EXPANDING",
        "bb_precio": "MID_TO_UPPER",
        "volumen": "HIGH",
        "patron_vela": "STRONG_GREEN",
        "fib_zona": "0.786_1.000",
        "notas": "ENTRADA PERFECTA M3 - Rotura de EMAs con volumen | R:R 5:1",
        "resultado": "WIN",
        "rb_real": 5.0,
        "weight": 1.5,
    },

    # ═══════════════════════════════════════════════════════════════
    #  🟢 GRUPO M3 — ENTRADA BAJO RIESGO (Verde oscura)
    # ═══════════════════════════════════════════════════════════════

    {
        "symbol": "UNIVERSAL",
        "timeframe": "3m",
        "signal_type": "LONG_REVERSAL",
        "trend": "BULLISH",
        "risk_level": "BAJO",
        "entry_type": "RECOMPRA",
        "arrow_color": "VERDE_OSCURA",
        "ema21_vs_ema55": "ABOVE",
        "ema55_vs_ema144": "ABOVE",
        "ema144_vs_ema233": "ABOVE",
        "ema21_slope": "UP",
        "ema144_slope": "UP",
        "precio_vs_ema21": "TOUCHING",
        "precio_vs_ema55": "ABOVE",
        "precio_vs_ema144": "ABOVE",
        "precio_vs_ema233": "ABOVE",
        "bb_estado": "EXPANDING",
        "bb_precio": "UPPER",
        "volumen": "MEDIUM",
        "patron_vela": "GREEN",
        "fib_zona": "0.786_1.000",
        "notas": "ENTRADA BAJO RIESGO M3 - Precio elevado con momentum | R:R 5:1",
        "resultado": "WIN",
        "rb_real": 5.0,
        "weight": 1.0,
    },

    {
        "symbol": "UNIVERSAL",
        "timeframe": "3m",
        "signal_type": "LONG_REVERSAL",
        "trend": "BULLISH",
        "risk_level": "BAJO",
        "entry_type": "RECOMPRA",
        "arrow_color": "VERDE_OSCURA",
        "ema21_vs_ema55": "ABOVE",
        "ema55_vs_ema144": "ABOVE",
        "ema144_vs_ema233": "ABOVE",
        "ema21_slope": "UP",
        "ema144_slope": "FLAT",
        "precio_vs_ema21": "TOUCHING",
        "precio_vs_ema55": "ABOVE",
        "precio_vs_ema144": "ABOVE",
        "precio_vs_ema233": "ABOVE",
        "bb_estado": "SQUEEZE",
        "bb_precio": "MID",
        "volumen": "LOW",
        "patron_vela": "HAMMER",
        "fib_zona": "0.618_0.786",
        "notas": "RECOMPRA M3 - Después de máximos y descuento | R:R 5:1",
        "resultado": "WIN",
        "rb_real": 5.0,
        "weight": 1.0,
    },

    # ═══════════════════════════════════════════════════════════════
    #  🔴 GRUPO M3 — VENTA AGRESIVA (Roja)
    # ═══════════════════════════════════════════════════════════════

    {
        "symbol": "UNIVERSAL",
        "timeframe": "3m",
        "signal_type": "SHORT_BREAKOUT",
        "trend": "BEARISH",
        "risk_level": "BAJO",
        "entry_type": "ENTRADA_PRINCIPAL",
        "arrow_color": "ROJA",
        "ema21_vs_ema55": "CROSSING_DOWN",
        "ema55_vs_ema144": "BELOW",
        "ema144_vs_ema233": "BELOW",
        "ema21_slope": "DOWN",
        "ema144_slope": "DOWN",
        "precio_vs_ema21": "BELOW",
        "precio_vs_ema55": "BELOW",
        "precio_vs_ema144": "BELOW",
        "precio_vs_ema233": "BELOW",
        "bb_estado": "EXPANDING",
        "bb_precio": "UPPER",
        "volumen": "HIGH",
        "patron_vela": "REJECTION",
        "fib_zona": "0.0_0.382",
        "notas": "VENTA AGRESIVA M3 - Rechazo en zona alta | R:R 5:1",
        "resultado": "WIN",
        "rb_real": 5.0,
        "weight": 1.5,
    },

    {
        "symbol": "UNIVERSAL",
        "timeframe": "3m",
        "signal_type": "SHORT_BREAKOUT",
        "trend": "BEARISH",
        "risk_level": "BAJO",
        "entry_type": "ENTRADA_PRINCIPAL",
        "arrow_color": "ROJA",
        "ema21_vs_ema55": "BELOW",
        "ema55_vs_ema144": "BELOW",
        "ema144_vs_ema233": "BELOW",
        "ema21_slope": "DOWN",
        "ema144_slope": "DOWN",
        "precio_vs_ema21": "BELOW",
        "precio_vs_ema55": "BELOW",
        "precio_vs_ema144": "BELOW",
        "precio_vs_ema233": "BELOW",
        "bb_estado": "EXPANDING",
        "bb_precio": "LOWER",
        "volumen": "HIGH",
        "patron_vela": "STRONG_RED",
        "fib_zona": "0.0_0.382",
        "notas": "VENTA AGRESIVA M3 - Caída prolongada | R:R 5:1",
        "resultado": "WIN",
        "rb_real": 5.0,
        "weight": 1.2,
    },

    {
        "symbol": "UNIVERSAL",
        "timeframe": "3m",
        "signal_type": "SHORT_BREAKOUT",
        "trend": "BEARISH",
        "risk_level": "BAJO",
        "entry_type": "ENTRADA_PRINCIPAL",
        "arrow_color": "ROJA",
        "ema21_vs_ema55": "CROSSING_DOWN",
        "ema55_vs_ema144": "BELOW",
        "ema144_vs_ema233": "BELOW",
        "ema21_slope": "DOWN",
        "ema144_slope": "DOWN",
        "precio_vs_ema21": "TOUCHING",
        "precio_vs_ema55": "BELOW",
        "precio_vs_ema144": "BELOW",
        "precio_vs_ema233": "BELOW",
        "bb_estado": "EXPANDING",
        "bb_precio": "UPPER",
        "volumen": "HIGH",
        "patron_vela": "REJECTION",
        "fib_zona": "0.0_0.382",
        "notas": "VENTA AGRESIVA M3 - Rechazo en resistencia | R:R 5:1",
        "resultado": "WIN",
        "rb_real": 5.0,
        "weight": 1.0,
    },

    {
        "symbol": "UNIVERSAL",
        "timeframe": "3m",
        "signal_type": "SHORT_BREAKOUT",
        "trend": "BEARISH",
        "risk_level": "BAJO",
        "entry_type": "ENTRADA_PRINCIPAL",
        "arrow_color": "ROJA",
        "ema21_vs_ema55": "BELOW",
        "ema55_vs_ema144": "BELOW",
        "ema144_vs_ema233": "BELOW",
        "ema21_slope": "DOWN",
        "ema144_slope": "DOWN",
        "precio_vs_ema21": "BELOW",
        "precio_vs_ema55": "BELOW",
        "precio_vs_ema144": "BELOW",
        "precio_vs_ema233": "BELOW",
        "bb_estado": "EXPANDING",
        "bb_precio": "LOWER",
        "volumen": "HIGH",
        "patron_vela": "GAP_DOWN",
        "fib_zona": "0.0_0.382",
        "notas": "VENTA AGRESIVA M3 - Gap y caída | R:R 5:1",
        "resultado": "WIN",
        "rb_real": 5.0,
        "weight": 1.5,
    },

    # ═══════════════════════════════════════════════════════════════
    #  🟡 GRUPO M3 — VENTA MENOR RIESGO (Dorada)
    # ═══════════════════════════════════════════════════════════════

    {
        "symbol": "UNIVERSAL",
        "timeframe": "3m",
        "signal_type": "SHORT_REVERSAL",
        "trend": "BEARISH",
        "risk_level": "BAJO",
        "entry_type": "ENTRADA_PRINCIPAL",
        "arrow_color": "DORADA",
        "ema21_vs_ema55": "BELOW",
        "ema55_vs_ema144": "BELOW",
        "ema144_vs_ema233": "BELOW",
        "ema21_slope": "DOWN",
        "ema144_slope": "DOWN",
        "precio_vs_ema21": "TOUCHING",
        "precio_vs_ema55": "BELOW",
        "precio_vs_ema144": "BELOW",
        "precio_vs_ema233": "BELOW",
        "bb_estado": "SQUEEZE",
        "bb_precio": "MID",
        "volumen": "MEDIUM",
        "patron_vela": "BEARISH_ENGULF",
        "fib_zona": "0.382_0.618",
        "notas": "VENTA MENOR RIESGO M3 - Caída con volumen | R:R 5:1",
        "resultado": "WIN",
        "rb_real": 5.0,
        "weight": 0.8,
    },

    {
        "symbol": "UNIVERSAL",
        "timeframe": "3m",
        "signal_type": "SHORT_REVERSAL",
        "trend": "BEARISH",
        "risk_level": "BAJO",
        "entry_type": "ENTRADA_PRINCIPAL",
        "arrow_color": "DORADA",
        "ema21_vs_ema55": "BELOW",
        "ema55_vs_ema144": "BELOW",
        "ema144_vs_ema233": "BELOW",
        "ema21_slope": "DOWN",
        "ema144_slope": "DOWN",
        "precio_vs_ema21": "TOUCHING",
        "precio_vs_ema55": "TOUCHING",
        "precio_vs_ema144": "BELOW",
        "precio_vs_ema233": "BELOW",
        "bb_estado": "SQUEEZE",
        "bb_precio": "MID",
        "volumen": "LOW",
        "patron_vela": "REJECTION",
        "fib_zona": "0.382_0.618",
        "notas": "VENTA MENOR RIESGO M3 - Rechazo en resistencia | R:R 5:1",
        "resultado": "WIN",
        "rb_real": 5.0,
        "weight": 0.8,
    },

    # ═══════════════════════════════════════════════════════════════
    #  ⚫ GRUPO — CONDICIONES A EVITAR
    # ═══════════════════════════════════════════════════════════════

    {
        "symbol": "UNIVERSAL",
        "timeframe": "15m",
        "signal_type": "NO_SIGNAL",
        "trend": "SIDEWAYS",
        "risk_level": "ALTO",
        "entry_type": "EVITAR",
        "arrow_color": "NINGUNA",
        "ema21_vs_ema55": "CHOPPY",
        "ema55_vs_ema144": "CHOPPY",
        "ema21_slope": "FLAT",
        "precio_vs_ema21": "CROSSING_REPEATEDLY",
        "bb_estado": "IRREGULAR",
        "volumen": "IRREGULAR",
        "notas": "MERCADO LATERAL - EVITAR",
        "resultado": "EVITAR",
        "rb_real": 0.0,
        "weight": 0.0,
    },
]


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS patterns (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol            TEXT NOT NULL,
            timeframe         TEXT,
            signal_type       TEXT NOT NULL,
            trend             TEXT,
            risk_level        TEXT,
            entry_type        TEXT,
            arrow_color       TEXT,
            ema21_vs_ema55    TEXT,
            ema55_vs_ema144   TEXT,
            ema144_vs_ema233  TEXT,
            ema21_slope       TEXT,
            ema144_slope      TEXT,
            precio_vs_ema21   TEXT,
            precio_vs_ema55   TEXT,
            precio_vs_ema144  TEXT,
            precio_vs_ema233  TEXT,
            bb_estado         TEXT,
            bb_precio         TEXT,
            volumen           TEXT,
            patron_vela       TEXT,
            dias_squeeze      INTEGER,
            fib_zona          TEXT,
            notas             TEXT,
            resultado         TEXT DEFAULT 'WIN',
            rb_real           REAL DEFAULT 0.0,
            weight            REAL DEFAULT 1.0,
            timestamp         TEXT
        )
    """)
    conn.commit()


def insert_patterns(conn, patterns):
    ts = datetime.now(timezone.utc).isoformat()
    ok = 0
    for p in patterns:
        try:
            if p.get("resultado") != "EVITAR" and p.get("signal_type") == "NO_SIGNAL":
                continue

            conn.execute("""
                INSERT INTO patterns (
                    symbol, timeframe, signal_type, trend,
                    risk_level, entry_type, arrow_color,
                    ema21_vs_ema55, ema55_vs_ema144, ema144_vs_ema233,
                    ema21_slope, ema144_slope,
                    precio_vs_ema21, precio_vs_ema55,
                    precio_vs_ema144, precio_vs_ema233,
                    bb_estado, bb_precio, volumen, patron_vela,
                    dias_squeeze, fib_zona, notas, resultado, rb_real, weight, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p["symbol"], p.get("timeframe", "15m"), p["signal_type"],
                p.get("trend"), p.get("risk_level"), p.get("entry_type"),
                p.get("arrow_color"),
                p.get("ema21_vs_ema55"), p.get("ema55_vs_ema144"),
                p.get("ema144_vs_ema233"),
                p.get("ema21_slope"), p.get("ema144_slope"),
                p.get("precio_vs_ema21"), p.get("precio_vs_ema55"),
                p.get("precio_vs_ema144"), p.get("precio_vs_ema233"),
                p.get("bb_estado"), p.get("bb_precio"), p.get("volumen"),
                p.get("patron_vela"),
                p.get("dias_squeeze"), p.get("fib_zona"), p.get("notas"),
                p.get("resultado", "WIN"), p.get("rb_real", 0.0),
                p.get("weight", 1.0), ts
            ))
            ok += 1
        except Exception as e:
            print(f"  ⚠️ Error: {p.get('symbol')} {p.get('signal_type')}: {e}")
    conn.commit()
    return ok


def print_summary(conn):
    total = conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0]
    evitar = conn.execute("SELECT COUNT(*) FROM patterns WHERE resultado='EVITAR'").fetchone()[0]
    m15 = conn.execute("SELECT COUNT(*) FROM patterns WHERE timeframe='15m' AND resultado!='EVITAR'").fetchone()[0]
    m3 = conn.execute("SELECT COUNT(*) FROM patterns WHERE timeframe='3m' AND resultado!='EVITAR'").fetchone()[0]

    print(f"\n{'═'*60}")
    print(f"  🧠 ÚNICO STRATEGY v4.0 — Patrones M15 + M3")
    print(f"  R:R 5:1 | SL: 4% capital | TP: 20% capital")
    print(f"{'═'*60}")
    print(f"  Total patrones:    {total}")
    print(f"  M15:               {m15}")
    print(f"  M3:                {m3}")
    print(f"  A evitar:          {evitar}")
    print(f"{'─'*60}")

    print(f"\n  Por color de flecha (M15):")
    colors = conn.execute("""
        SELECT arrow_color, COUNT(*) as n, AVG(rb_real) as rb
        FROM patterns WHERE timeframe='15m' AND resultado != 'EVITAR'
        GROUP BY arrow_color ORDER BY rb DESC
    """).fetchall()
    for c in colors:
        color, n, rb = c
        if color and color != "NINGUNA":
            print(f"  {color:<20} {n}  |  R:R 1:{rb:.0f}")

    print(f"\n  Por color de flecha (M3):")
    colors = conn.execute("""
        SELECT arrow_color, COUNT(*) as n, AVG(rb_real) as rb
        FROM patterns WHERE timeframe='3m' AND resultado != 'EVITAR'
        GROUP BY arrow_color ORDER BY rb DESC
    """).fetchall()
    for c in colors:
        color, n, rb = c
        if color and color != "NINGUNA":
            print(f"  {color:<20} {n}  |  R:R 1:{rb:.0f}")

    print(f"{'═'*60}\n")


if __name__ == "__main__":
    print("\n🔥 ÚNICO STRATEGY v4.0 — M15 + M3")
    print("─" * 60)
    print("  🟢 Verde clara  → Entrada PERFECTA | R:R 5:1")
    print("  🟢 Verde oscura → Entrada BAJO RIESGO | R:R 5:1")
    print("  🔴 Roja         → Venta AGRESIVA | R:R 5:1")
    print("  🟡 Dorada       → Venta MENOR RIESGO | R:R 5:1")
    print("─" * 60)

    with sqlite3.connect(DB_PATH) as conn:
        print(f"  📂 BD: {DB_PATH}")
        init_db(conn)
        print(f"  📥 Insertando {len(PATTERNS)} patrones...")
        ok = insert_patterns(conn, PATTERNS)
        print(f"  ✅ {ok}/{len(PATTERNS)} patrones insertados")
        print_summary(conn)

    print("  🚀 Listo para el scanner. Siguiente: python main.py --init-db\n")
