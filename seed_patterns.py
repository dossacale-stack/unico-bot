"""
seed_patterns.py — Base de Datos de Patrones ÚNICO STRATEGY
============================================================
Versión 3.0 — Patrones UNIVERSALES por comportamiento de EMAs

SISTEMA DE FLECHAS (Entradas):
  🟢 Verde clara   → ENTRADA PERFECTA — Riesgo MÍNIMO (todas las EMAs alineadas)
  🟢 Verde oscura  → ENTRADA BAJO RIESGO — Precio tocando EMA144/EMA233
  🔴 Roja          → VENTA AGRESIVA / REVERSO — Rechazo en EMA21
  🟡 Dorada        → VENTA MENOR RIESGO — Rechazo en EMA144

COLORES EMAs:
  Rojo    = EMA 21  (más rápida)
  Morado  = EMA 55
  Azul    = EMA 144
  Amarillo = EMA 233 (más lenta)

REGLA FUNDAMENTAL:
  El bot opera por COMPORTAMIENTO de colores, no por precio absoluto.
  Un patrón en $0.001 = mismo setup que en $60,000
  si el comportamiento de EMAs + BB es idéntico.
"""

import sqlite3
from datetime import datetime, timezone

DB_PATH = "patterns.db"

PATTERNS = [

    # ═══════════════════════════════════════════════════════════════
    #  🟢 GRUPO 1 — ENTRADA PERFECTA (Verde clara)
    #  Riesgo MÍNIMO — Todas las EMAs en cascada alcista
    # ═══════════════════════════════════════════════════════════════

    {
        "symbol":            "UNIVERSAL",
        "timeframe":         "15m",
        "signal_type":       "LONG_BREAKOUT",
        "trend":             "BULLISH",
        "risk_level":        "MINIMO",
        "entry_type":        "ENTRADA_PRINCIPAL",
        "arrow_color":       "VERDE_CLARA",
        "ema21_vs_ema55":    "ABOVE",
        "ema55_vs_ema144":   "CROSSING_UP",
        "ema144_vs_ema233":  "FLAT_TO_UP",
        "ema21_slope":       "UP",
        "ema144_slope":      "UP",
        "precio_vs_ema21":   "ABOVE",
        "precio_vs_ema55":   "ABOVE",
        "precio_vs_ema144":  "ABOVE",
        "precio_vs_ema233":  "ABOVE",
        "bb_estado":         "EXPANDING",
        "bb_precio":         "MID_TO_UPPER",
        "volumen":           "HIGH",
        "patron_vela":       "STRONG_GREEN",
        "fib_zona":          "0.786_1.000",
        "notas": (
            "ENTRADA PERFECTA — Verde clara. "
            "Cascada alcista completa: Rojo > Morado > Azul > Amarillo. "
            "Precio sobre TODAS las EMAs — confirmación máxima. "
            "BB expandiéndose con fuerza. Volumen 2x+ vs media. "
            "Zona Fibonacci 0.786-1.0 — el riesgo más bajo posible. "
            "SL: debajo de EMA21 — distancia mínima. "
            "Este es el setup de menor riesgo del sistema."
        ),
        "resultado": "WIN",
        "rb_real":   45.0,
        "weight":    1.5,  # Peso inicial alto por ser entrada perfecta
    },

    {
        "symbol":            "UNIVERSAL",
        "timeframe":         "15m",
        "signal_type":       "LONG_REVERSAL",
        "trend":             "BULLISH",
        "risk_level":        "MINIMO",
        "entry_type":        "ENTRADA_PIRAMIDE",
        "arrow_color":       "VERDE_CLARA",
        "ema21_vs_ema55":    "ABOVE",
        "ema55_vs_ema144":   "ABOVE",
        "ema144_vs_ema233":  "ABOVE",
        "ema21_slope":       "UP",
        "ema144_slope":      "UP",
        "precio_vs_ema21":   "TOUCHING",
        "precio_vs_ema233":  "ABOVE",
        "bb_estado":         "EXPANDING",
        "bb_precio":         "MID_TO_UPPER",
        "volumen":           "MEDIUM",
        "patron_vela":       "STRONG_GREEN",
        "fib_zona":          "0.786_1.000",
        "notas": (
            "PIRÁMIDE PERFECTA — Verde clara. "
            "Cascada alcista completa: Rojo > Morado > Azul > Amarillo. "
            "Precio hace pullback mínimo a EMA21 y rebota. "
            "Todas las EMAs en pendiente positiva. "
            "BB expandido — momentum activo. "
            "Añadir al long existente. SL: debajo de EMA21."
        ),
        "resultado": "WIN",
        "rb_real":   30.0,
        "weight":    1.3,
    },

    {
        "symbol":            "UNIVERSAL",
        "timeframe":         "15m",
        "signal_type":       "LONG_BREAKOUT",
        "trend":             "BULLISH",
        "risk_level":        "MINIMO",
        "entry_type":        "ENTRADA_PRINCIPAL",
        "arrow_color":       "VERDE_CLARA",
        "ema21_vs_ema55":    "FLAT",
        "ema55_vs_ema144":   "FLAT",
        "ema144_vs_ema233":  "FLAT",
        "ema21_slope":       "FLAT",
        "ema144_slope":      "FLAT",
        "precio_vs_ema21":   "NEAR",
        "bb_estado":         "MAX_SQUEEZE",
        "bb_precio":         "MID",
        "volumen":           "VERY_LOW",
        "patron_vela":       "STRONG_GREEN",
        "dias_squeeze":      3,
        "fib_zona":          "N/A_NUEVO_MOVIMIENTO",
        "notas": (
            "SQUEEZE PROLONGADO — Verde clara. "
            "El patrón más rentable del sistema. "
            "Todas las EMAs planas y apiladas 3+ días. "
            "BB en squeeze máximo — bandwidth mínimo histórico. "
            "Volumen casi inexistente durante acumulación. "
            "GATILLO: primera vela verde que cierra sobre EMA21 y EMA55 "
            "con volumen mínimo 2x la media. "
            "SL: debajo EMA233 — muy corto porque amarillo está plano."
        ),
        "resultado": "WIN",
        "rb_real":   100.0,
        "weight":    2.0,  # Peso máximo por ser el patrón más rentable
    },

    # ═══════════════════════════════════════════════════════════════
    #  🟢 GRUPO 2 — ENTRADA BAJO RIESGO (Verde oscura)
    #  Precio tocando EMA144 (Azul) o EMA233 (Amarillo)
    # ═══════════════════════════════════════════════════════════════

    {
        "symbol":            "UNIVERSAL",
        "timeframe":         "15m",
        "signal_type":       "LONG_REVERSAL",
        "trend":             "BULLISH",
        "risk_level":        "BAJO",
        "entry_type":        "ENTRADA_PRINCIPAL",
        "arrow_color":       "VERDE_OSCURA",
        "ema21_vs_ema55":    "CROSSING_UP",
        "ema55_vs_ema144":   "BELOW",
        "ema144_vs_ema233":  "FLAT",
        "ema21_slope":       "UP",
        "ema144_slope":      "FLAT",
        "precio_vs_ema21":   "TOUCHING",
        "precio_vs_ema144":  "TOUCHING",
        "precio_vs_ema233":  "ABOVE",
        "bb_estado":         "EXPANDING",
        "bb_precio":         "LOWER",
        "volumen":           "HIGH",
        "patron_vela":       "STRONG_GREEN",
        "fib_zona":          "0.618_0.786",
        "notas": (
            "ENTRADA BAJO RIESGO — Verde oscura. "
            "Precio tocando EMA144 (Azul) como soporte. "
            "EMA21 (Rojo) cruzando EMA55 (Morado) hacia arriba. "
            "BB expandiéndose desde zona lower. "
            "Volumen confirmando el movimiento. "
            "SL: debajo de EMA144. Riesgo controlado."
        ),
        "resultado": "WIN",
        "rb_real":   25.0,
        "weight":    1.0,
    },

    {
        "symbol":            "UNIVERSAL",
        "timeframe":         "15m",
        "signal_type":       "LONG_REVERSAL",
        "trend":             "BULLISH",
        "risk_level":        "BAJO",
        "entry_type":        "RECOMPRA",
        "arrow_color":       "VERDE_OSCURA",
        "ema21_vs_ema55":    "ABOVE",
        "ema55_vs_ema144":   "ABOVE",
        "ema144_vs_ema233":  "FLAT_TO_UP",
        "ema21_slope":       "UP",
        "ema144_slope":      "UP",
        "precio_vs_ema21":   "TOUCHING",
        "precio_vs_ema55":   "TOUCHING",
        "bb_estado":         "SQUEEZE",
        "bb_precio":         "LOWER",
        "volumen":           "LOW",
        "patron_vela":       "HAMMER",
        "fib_zona":          "0.618_0.786",
        "notas": (
            "RECOMPRA BAJO RIESGO — Verde oscura. "
            "Tendencia alcista activa. "
            "Precio corrigió a zona EMA21+EMA55 (Rojo+Morado). "
            "BB recomprimiéndose — nueva expansión inminente. "
            "Volumen bajo en corrección — saludable. "
            "Vela martillo en zona soporte. "
            "SL debajo del mínimo de la corrección."
        ),
        "resultado": "WIN",
        "rb_real":   20.0,
        "weight":    1.0,
    },

    {
        "symbol":            "UNIVERSAL",
        "timeframe":         "15m",
        "signal_type":       "LONG_REVERSAL",
        "trend":             "BULLISH",
        "risk_level":        "BAJO",
        "entry_type":        "RECOMPRA",
        "arrow_color":       "VERDE_OSCURA",
        "ema21_vs_ema55":    "BELOW",
        "ema55_vs_ema144":   "BELOW",
        "ema144_vs_ema233":  "FLAT",
        "ema21_slope":       "FLAT",
        "ema144_slope":      "FLAT",
        "precio_vs_ema144":  "TOUCHING",
        "precio_vs_ema233":  "TOUCHING",
        "bb_estado":         "SQUEEZE",
        "bb_precio":         "LOWER",
        "volumen":           "LOW",
        "patron_vela":       "HAMMER",
        "fib_zona":          "0.618_0.786",
        "notas": (
            "POST-EXPLOSIÓN — Verde oscura. "
            "El primer impulso fue enorme (100%-300%+). "
            "Precio corrigió y tocó EMA144 o EMA233. "
            "EMA21 vuelve a cruzar EMA55 — segunda confirmación. "
            "BB vuelve a comprimir — acumulación post-explosión. "
            "SL: debajo EMA233 — corto porque amarillo plano."
        ),
        "resultado": "WIN",
        "rb_real":   35.0,
        "weight":    1.0,
    },

    # ═══════════════════════════════════════════════════════════════
    #  🔴 GRUPO 3 — VENTA AGRESIVA / REVERSO (Roja)
    #  Rechazo en EMA21 (Rojo) en tendencia bajista
    # ═══════════════════════════════════════════════════════════════

    {
        "symbol":            "UNIVERSAL",
        "timeframe":         "15m",
        "signal_type":       "SHORT_BREAKOUT",
        "trend":             "BEARISH",
        "risk_level":        "BAJO",
        "entry_type":        "ENTRADA_PRINCIPAL",
        "arrow_color":       "ROJA",
        "ema21_vs_ema55":    "BELOW",
        "ema55_vs_ema144":   "BELOW",
        "ema144_vs_ema233":  "BELOW",
        "ema21_slope":       "DOWN",
        "ema144_slope":      "DOWN",
        "precio_vs_ema21":   "TOUCHING",
        "precio_vs_ema55":   "BELOW",
        "precio_vs_ema144":  "BELOW",
        "precio_vs_ema233":  "BELOW",
        "bb_estado":         "EXPANDING",
        "bb_precio":         "UPPER",
        "volumen":           "HIGH",
        "patron_vela":       "REJECTION",
        "fib_zona":          "0.0_0.382",
        "notas": (
            "VENTA AGRESIVA / REVERSO — Roja. "
            "EMA21 (Rojo) cruza hacia abajo — señal primaria. "
            "Precio rechazado en EMA21 o zona de resistencia. "
            "Vela de rechazo con mecha larga arriba. "
            "Si hay long abierto: REVERTIR contrato aquí. "
            "Si no hay posición: abrir short directo. "
            "SL: encima de la mecha de rechazo. "
            "TP: siguiente soporte o cuando Rojo cruce Morado arriba."
        ),
        "resultado": "WIN",
        "rb_real":   15.0,
        "weight":    1.0,
    },

    {
        "symbol":            "UNIVERSAL",
        "timeframe":         "15m",
        "signal_type":       "SHORT_BREAKOUT",
        "trend":             "BEARISH",
        "risk_level":        "BAJO",
        "entry_type":        "ENTRADA_PRINCIPAL",
        "arrow_color":       "ROJA",
        "ema21_vs_ema55":    "BELOW",
        "ema55_vs_ema144":   "BELOW",
        "ema144_vs_ema233":  "BELOW",
        "ema21_slope":       "DOWN",
        "ema144_slope":      "DOWN",
        "precio_vs_ema21":   "TOUCHING",
        "precio_vs_ema55":   "TOUCHING",
        "bb_estado":         "EXPANDING",
        "bb_precio":         "UPPER",
        "volumen":           "HIGH",
        "patron_vela":       "FAKE_BREAKOUT",
        "fib_zona":          "0.0_0.382",
        "notas": (
            "TRIPLE TECHO + BREAKOUT FALSO — Roja. "
            "Cascada bajista completa activa. "
            "Tercer intento al mismo máximo — lo rompe pero cierra abajo. "
            "Volumen alto en vela trampa — liquidación masiva. "
            "REVERTIR long a short en este punto. "
            "SL: encima del máximo falso. "
            "TP: -40% a -50% desde entrada."
        ),
        "resultado": "WIN",
        "rb_real":   18.0,
        "weight":    1.0,
    },

    # ═══════════════════════════════════════════════════════════════
    #  🟡 GRUPO 4 — VENTA MENOR RIESGO (Dorada)
    #  Rechazo en EMA144 (Azul) en tendencia bajista establecida
    # ═══════════════════════════════════════════════════════════════

    {
        "symbol":            "UNIVERSAL",
        "timeframe":         "15m",
        "signal_type":       "SHORT_REVERSAL",
        "trend":             "BEARISH",
        "risk_level":        "BAJO",
        "entry_type":        "ENTRADA_PRINCIPAL",
        "arrow_color":       "DORADA",
        "ema21_vs_ema55":    "BELOW",
        "ema55_vs_ema144":   "BELOW",
        "ema144_vs_ema233":  "BELOW",
        "ema21_slope":       "DOWN",
        "ema144_slope":      "DOWN",
        "precio_vs_ema21":   "TOUCHING",
        "precio_vs_ema55":   "BELOW",
        "precio_vs_ema144":  "TOUCHING",
        "precio_vs_ema233":  "ABOVE",
        "bb_estado":         "SQUEEZE",
        "bb_precio":         "MID",
        "volumen":           "LOW",
        "patron_vela":       "BEARISH_ENGULF",
        "fib_zona":          "0.382_0.618",
        "notas": (
            "VENTA MENOR RIESGO — Dorada. "
            "Tendencia bajista establecida y confirmada. "
            "Precio rebota técnico y toca EMA144 (Azul). "
            "EMA144 actúa como resistencia dinámica. "
            "Volumen bajo en rebote — sin compradores reales. "
            "BB comprime levemente. "
            "Zona Fibonacci 0.382-0.618 — retroceso técnico. "
            "Más conservadora que la roja — espera más confirmación. "
            "SL: encima de EMA144."
        ),
        "resultado": "WIN",
        "rb_real":   12.0,
        "weight":    0.8,
    },

    {
        "symbol":            "UNIVERSAL",
        "timeframe":         "15m",
        "signal_type":       "SHORT_REVERSAL",
        "trend":             "BEARISH",
        "risk_level":        "BAJO",
        "entry_type":        "RECOMPRA",
        "arrow_color":       "DORADA",
        "ema21_vs_ema55":    "BELOW",
        "ema55_vs_ema144":   "BELOW",
        "ema144_vs_ema233":  "BELOW",
        "ema21_slope":       "DOWN",
        "ema144_slope":      "DOWN",
        "precio_vs_ema21":   "TOUCHING",
        "precio_vs_ema55":   "TOUCHING",
        "precio_vs_ema144":  "BELOW",
        "bb_estado":         "SQUEEZE",
        "bb_precio":         "MID",
        "volumen":           "LOW",
        "patron_vela":       "REJECTION",
        "fib_zona":          "0.500_0.618",
        "notas": (
            "RECOMPRA EN VENTA — Dorada. "
            "Cascada bajista activa. "
            "Precio toca zona EMA21+EMA55 (doble resistencia). "
            "Ambas EMAs en pendiente negativa. "
            "Recompra al short principal. "
            "Zona Fibonacci 0.5-0.618 del rebote. "
            "SL: encima del máximo del rebote."
        ),
        "resultado": "WIN",
        "rb_real":   10.0,
        "weight":    0.8,
    },

    # ═══════════════════════════════════════════════════════════════
    #  GRUPO 5 — CONDICIONES A EVITAR
    #  El bot NO entra aunque parezca señal
    # ═══════════════════════════════════════════════════════════════

    {
        "symbol":            "UNIVERSAL",
        "timeframe":         "15m",
        "signal_type":       "NO_SIGNAL",
        "trend":             "SIDEWAYS",
        "risk_level":        "ALTO",
        "entry_type":        "EVITAR",
        "arrow_color":       "NINGUNA",
        "ema21_vs_ema55":    "CHOPPY",
        "ema55_vs_ema144":   "CHOPPY",
        "ema144_vs_ema233":  "FLAT",
        "ema21_slope":       "FLAT",
        "ema144_slope":      "FLAT",
        "precio_vs_ema21":   "CROSSING_REPEATEDLY",
        "bb_estado":         "IRREGULAR",
        "bb_precio":         "MID",
        "volumen":           "IRREGULAR",
        "patron_vela":       "CHOPPY",
        "fib_zona":          "N/A",
        "notas": (
            "CONDICIÓN A EVITAR — Sin flecha. "
            "EMAs cruzándose repetidamente sin dirección. "
            "Precio cruza EMA21 arriba y abajo sin confirmar. "
            "BB irregular — sin squeeze ni expansión clara. "
            "Volumen irregular sin patrón. "
            "MERCADO LATERAL RUIDOSO — no hay tendencia. "
            "El bot ignora este par y busca el siguiente."
        ),
        "resultado": "EVITAR",
        "rb_real":   0.0,
        "weight":    0.0,
    },

    {
        "symbol":            "UNIVERSAL",
        "timeframe":         "15m",
        "signal_type":       "NO_SIGNAL",
        "trend":             "BEARISH",
        "risk_level":        "ALTO",
        "entry_type":        "EVITAR",
        "arrow_color":       "NINGUNA",
        "ema21_vs_ema55":    "BELOW",
        "ema55_vs_ema144":   "BELOW",
        "ema144_vs_ema233":  "BELOW",
        "ema21_slope":       "DOWN",
        "ema144_slope":      "DOWN",
        "precio_vs_ema233":  "BELOW",
        "bb_estado":         "EXPANDING",
        "bb_precio":         "LOWER",
        "volumen":           "HIGH",
        "patron_vela":       "STRONG_RED",
        "fib_zona":          "N/A",
        "notas": (
            "EVITAR LONG EN CAÍDA VERTICAL — Sin flecha verde. "
            "Cascada bajista activa con BB expandido al máximo. "
            "Precio cayendo en caída libre. "
            "NO abrir long aquí aunque BB lower parezca atractivo. "
            "Esperar que la caída se detenga y el BB comprima."
        ),
        "resultado": "EVITAR",
        "rb_real":   0.0,
        "weight":    0.0,
    },
]


# ─────────────────────────────────────────────
#  FUNCIONES DE BD
# ─────────────────────────────────────────────
def init_db(conn):
    # Tabla de patrones (existente)
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
            intentos_maximo   INTEGER,
            maximo_previo     TEXT,
            dias_squeeze      INTEGER,
            fib_zona          TEXT,
            notas             TEXT,
            resultado         TEXT DEFAULT 'WIN',
            rb_real           REAL DEFAULT 0.0,
            weight            REAL DEFAULT 1.0,
            timestamp         TEXT,
            pnl_percent       REAL DEFAULT NULL,
            pnl_usd           REAL DEFAULT NULL,
            exit_price        REAL DEFAULT NULL,
            exit_reason       TEXT DEFAULT NULL,
            trade_result      TEXT DEFAULT 'SEED'
        )
    """)

    # Tabla de pattern_stats (existente)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pattern_stats (
            symbol       TEXT,
            signal_type  TEXT,
            arrow_color  TEXT,
            total        INTEGER DEFAULT 0,
            wins         INTEGER DEFAULT 0,
            avg_rb       REAL DEFAULT 0.0,
            win_rate     REAL DEFAULT 0.0,
            weight       REAL DEFAULT 1.0,
            PRIMARY KEY (symbol, signal_type, arrow_color)
        )
    """)

    conn.commit()


def insert_patterns(conn, patterns):
    ts = datetime.now(timezone.utc).isoformat()
    ok = 0
    for p in patterns:
        try:
            # Validar que signal_type no sea NO_SIGNAL (excepto patrones EVITAR)
            if p.get("resultado") != "EVITAR" and p.get("signal_type") == "NO_SIGNAL":
                logger.warning(f"Saltando patrón NO_SIGNAL que no es EVITAR: {p.get('symbol')}")
                continue

            data = {
                "symbol":           p["symbol"],
                "timeframe":        p.get("timeframe", "15m"),
                "signal_type":      p["signal_type"],
                "trend":            p.get("trend"),
                "risk_level":       p.get("risk_level"),
                "entry_type":       p.get("entry_type"),
                "arrow_color":      p.get("arrow_color"),
                "ema21_vs_ema55":   p.get("ema21_vs_ema55"),
                "ema55_vs_ema144":  p.get("ema55_vs_ema144"),
                "ema144_vs_ema233": p.get("ema144_vs_ema233"),
                "ema21_slope":      p.get("ema21_slope"),
                "ema144_slope":     p.get("ema144_slope"),
                "precio_vs_ema21":  p.get("precio_vs_ema21"),
                "precio_vs_ema55":  p.get("precio_vs_ema55"),
                "precio_vs_ema144": p.get("precio_vs_ema144"),
                "precio_vs_ema233": p.get("precio_vs_ema233"),
                "bb_estado":        p.get("bb_estado"),
                "bb_precio":        p.get("bb_precio"),
                "volumen":          p.get("volumen"),
                "patron_vela":      p.get("patron_vela"),
                "intentos_maximo":  p.get("intentos_maximo"),
                "maximo_previo":    p.get("maximo_previo"),
                "dias_squeeze":     p.get("dias_squeeze"),
                "fib_zona":         p.get("fib_zona"),
                "notas":            p.get("notas"),
                "resultado":        p.get("resultado", "WIN"),
                "rb_real":          p.get("rb_real", 0.0),
                "weight":           p.get("weight", 1.0),
                "timestamp":        ts,
            }

            conn.execute("""
                INSERT INTO patterns (
                    symbol, timeframe, signal_type, trend,
                    risk_level, entry_type, arrow_color,
                    ema21_vs_ema55, ema55_vs_ema144, ema144_vs_ema233,
                    ema21_slope, ema144_slope,
                    precio_vs_ema21, precio_vs_ema55,
                    precio_vs_ema144, precio_vs_ema233,
                    bb_estado, bb_precio, volumen, patron_vela,
                    intentos_maximo, maximo_previo, dias_squeeze,
                    fib_zona, notas, resultado, rb_real, weight, timestamp
                ) VALUES (
                    :symbol, :timeframe, :signal_type, :trend,
                    :risk_level, :entry_type, :arrow_color,
                    :ema21_vs_ema55, :ema55_vs_ema144, :ema144_vs_ema233,
                    :ema21_slope, :ema144_slope,
                    :precio_vs_ema21, :precio_vs_ema55,
                    :precio_vs_ema144, :precio_vs_ema233,
                    :bb_estado, :bb_precio, :volumen, :patron_vela,
                    :intentos_maximo, :maximo_previo, :dias_squeeze,
                    :fib_zona, :notas, :resultado, :rb_real, :weight, :timestamp
                )
            """, data)
            ok += 1
        except Exception as e:
            print(f"  ⚠️  Error: {p.get('symbol')} {p.get('signal_type')}: {e}")
    conn.commit()
    return ok


def print_summary(conn):
    total = conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0]
    evitar = conn.execute(
        "SELECT COUNT(*) FROM patterns WHERE resultado='EVITAR'"
    ).fetchone()[0]
    operables = total - evitar

    print(f"\n{'═'*60}")
    print(f"  🧠 ÚNICO STRATEGY — BASE DE DATOS v3.0")
    print(f"  Patrones UNIVERSALES por comportamiento de EMAs")
    print(f"{'═'*60}")
    print(f"  Total patrones:    {total}")
    print(f"  Operables:         {operables}")
    print(f"  A evitar:          {evitar}")
    print(f"{'─'*60}")

    print(f"\n  Por color de flecha:")
    colors = conn.execute("""
        SELECT arrow_color, COUNT(*) as n, AVG(rb_real) as avg_rb, AVG(weight) as avg_weight
        FROM patterns WHERE resultado != 'EVITAR'
        GROUP BY arrow_color ORDER BY avg_rb DESC
    """).fetchall()
    for c in colors:
        color, n, rb, weight = c
        if color and color != "NINGUNA":
            print(f"  {color:<20} {n} patrones  |  R:B prom 1:{rb:.0f}  |  Peso: {weight:.2f}")

    print(f"\n  Por tipo de señal:")
    sigs = conn.execute("""
        SELECT signal_type, COUNT(*) as n, AVG(rb_real) as avg_rb
        FROM patterns WHERE resultado != 'EVITAR'
        GROUP BY signal_type ORDER BY avg_rb DESC
    """).fetchall()
    for s in sigs:
        sig, n, rb = s
        if sig != "NO_SIGNAL":
            print(f"  {sig:<25} {n} patrones  |  R:B prom 1:{rb:.0f}")

    print(f"\n  Top R:B por patrón:")
    top = conn.execute("""
        SELECT symbol, signal_type, arrow_color, rb_real, weight, notas
        FROM patterns WHERE resultado != 'EVITAR'
        ORDER BY rb_real DESC LIMIT 5
    """).fetchall()
    for i, t in enumerate(top, 1):
        sym, sig, color, rb, weight, notas = t
        notas_short = (notas or "")[:50] + "..." if notas and len(notas) > 50 else notas
        print(f"  {i}. {sym} {sig} [{color}] → 1:{rb:.0f}  |  Peso: {weight:.2f}")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    print("\n🔥 ÚNICO STRATEGY — Inicializando BD v3.0 (Patrones Universales)")
    print("─" * 60)
    print("  🟢 Verde clara  → Entrada PERFECTA (Riesgo MÍNIMO)")
    print("  🟢 Verde oscura → Entrada BAJO RIESGO")
    print("  🔴 Roja         → Venta AGRESIVA / REVERSO")
    print("  🟡 Dorada       → Venta MENOR RIESGO")
    print("─" * 60)

    with sqlite3.connect(DB_PATH) as conn:
        print(f"  📂 BD: {DB_PATH}")
        init_db(conn)
        print(f"  📥 Insertando {len(PATTERNS)} patrones...")
        ok = insert_patterns(conn, PATTERNS)
        print(f"  ✅ {ok}/{len(PATTERNS)} patrones insertados")
        print_summary(conn)

    print("  🚀 Listo para el scanner. Siguiente: python main.py\n")
