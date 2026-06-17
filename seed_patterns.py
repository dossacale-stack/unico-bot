"""
seed_patterns.py — Base de Datos de Patrones ÚNICO STRATEGY
============================================================
Versión 2.0 — Actualizada con análisis de 13 charts

SISTEMA DE FLECHAS:
  🟢 Verde oscura  → Compra bajo riesgo (condiciones básicas)
  🟩 Verde clara   → Entrada perfecta — riesgo mínimo (máxima confirmación)
  🔴 Roja          → Reverso de long A short / apertura de venta agresiva
  🟡 Dorada        → Entrada de venta de menor riesgo (más conservadora)

COLORES EMAs:
  Rojo    = EMA 21  (más rápida)
  Rosa    = EMA 55
  Azul    = EMA 144
  Naranja = EMA 233 (más lenta)
  Verde   = Bandas de Bollinger

ZONAS FIBONACCI (sobre onda anterior):
  0.0   - 0.382 → zona rojo fuerte    (soporte/resistencia débil)
  0.382 - 0.500 → zona rojo oscuro
  0.500 - 0.618 → zona naranja/café
  0.618 - 0.786 → zona verde medio    ← entradas de menor riesgo
  0.786 - 1.000 → zona verde oscuro   ← entradas perfectas

REGLA FUNDAMENTAL:
  El bot opera por COMPORTAMIENTO de colores, no por precio absoluto.
  Un patrón en $0.001 = mismo setup que en $60,000
  si el comportamiento de EMAs + BB es idéntico.

Ejecutar: python seed_patterns.py
"""

import sqlite3
from datetime import datetime, timezone

DB_PATH = "patterns.db"

PATTERNS = [

    # ═══════════════════════════════════════════════════════════════
    #  GRUPO 1 — COMPRA BAJO RIESGO (Verde oscura)
    #  Condiciones básicas cumplidas — precio en zona Fibonacci 0.618+
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
        "precio_vs_ema233":  "TOUCHING",
        "bb_estado":         "EXPANDING",
        "bb_precio":         "LOWER",
        "volumen":           "HIGH",
        "patron_vela":       "STRONG_GREEN",
        "fib_zona":          "0.618_0.786",
        "notas": (
            "COMPRA BAJO RIESGO — Verde oscura. "
            "Condiciones mínimas cumplidas para entrada. "
            "EMA21 (rojo) cruzando EMA55 (rosa) hacia arriba. "
            "Precio tocando EMA233 (naranja) como soporte. "
            "BB expandiéndose desde zona lower. "
            "Precio en zona Fibonacci 0.618-0.786 de onda anterior. "
            "Volumen confirmando el movimiento. "
            "SL: debajo de EMA233. Riesgo controlado."
        ),
        "resultado": "WIN",
        "rb_real":   25.0,
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
            "Precio corrigió a zona EMA21+EMA55 (rojo+rosa). "
            "BB recomprimiéndose — nueva expansión inminente. "
            "Volumen bajo en corrección — saludable. "
            "Vela martillo en zona soporte. "
            "Zona Fibonacci 0.618-0.786 de impulso anterior. "
            "SL debajo del mínimo de la corrección."
        ),
        "resultado": "WIN",
        "rb_real":   20.0,
    },

    # ═══════════════════════════════════════════════════════════════
    #  GRUPO 2 — ENTRADA PERFECTA (Verde clara)
    #  Máxima confirmación — riesgo mínimo absoluto
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
            "Cascada alcista completa construyéndose: "
            "rojo > rosa, rosa cruzando azul hacia arriba. "
            "Precio sobre TODAS las EMAs — confirmación máxima. "
            "BB expandiéndose con fuerza. "
            "Volumen 2x+ vs media. "
            "Zona Fibonacci 0.786-1.0 — el riesgo más bajo posible. "
            "SL: debajo de EMA21 — distancia mínima. "
            "Este es el setup de menor riesgo del sistema. "
            "Visto en SKYAI, ATOM, ESPORTS, ALLO."
        ),
        "resultado": "WIN",
        "rb_real":   45.0,
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
            "Cascada alcista completa: rojo>rosa>azul>naranja. "
            "Precio hace pullback mínimo a EMA21 y rebota. "
            "Todas las EMAs en pendiente positiva. "
            "BB expandido — momentum activo. "
            "Añadir al long existente. "
            "SL: debajo de EMA21. "
            "Visto en ATOM — tendencia limpia días 7-11."
        ),
        "resultado": "WIN",
        "rb_real":   30.0,
    },

    # ═══════════════════════════════════════════════════════════════
    #  GRUPO 3 — VENTA AGRESIVA / REVERSO (Roja)
    #  Reverso de long a short O apertura de venta directa
    # ═══════════════════════════════════════════════════════════════

    {
        "symbol":            "UNIVERSAL",
        "timeframe":         "15m",
        "signal_type":       "SHORT_BREAKOUT",
        "trend":             "BEARISH",
        "risk_level":        "BAJO",
        "entry_type":        "ENTRADA_PRINCIPAL",
        "arrow_color":       "ROJA",
        "ema21_vs_ema55":    "CROSSING_DOWN",
        "ema55_vs_ema144":   "ABOVE",
        "ema144_vs_ema233":  "ABOVE",
        "ema21_slope":       "DOWN",
        "ema144_slope":      "TURNING_DOWN",
        "precio_vs_ema21":   "BELOW",
        "precio_vs_ema55":   "TOUCHING",
        "bb_estado":         "EXPANDING",
        "bb_precio":         "UPPER",
        "volumen":           "HIGH",
        "patron_vela":       "REJECTION",
        "intentos_maximo":   1,
        "fib_zona":          "0.0_0.382",
        "notas": (
            "REVERSO / VENTA AGRESIVA — Roja. "
            "EMA21 (rojo) cruza hacia abajo EMA55 (rosa) — señal primaria. "
            "Precio rechazado en BB upper. "
            "Vela de rechazo con mecha larga arriba. "
            "Si hay long abierto: REVERTIR contrato aquí. "
            "Si no hay posición: abrir short directo. "
            "SL: encima de la mecha de rechazo. "
            "TP: siguiente soporte o cuando rojo cruce rosa hacia arriba. "
            "Visto en ESPORTS, LAB, SKYAI imagen 2 y 3."
        ),
        "resultado": "WIN",
        "rb_real":   15.0,
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
        "intentos_maximo":   3,
        "maximo_previo":     "BROKEN_FAKE",
        "fib_zona":          "0.0_0.382",
        "notas": (
            "TRIPLE TECHO + BREAKOUT FALSO — Roja. "
            "Cascada bajista completa activa. "
            "Tercer intento al mismo máximo — lo rompe pero cierra abajo. "
            "Volumen alto en vela trampa — liquidación masiva. "
            "REVERTIR long a short en este punto. "
            "SL: encima del máximo falso. "
            "TP: -40% a -50% desde entrada. "
            "Patrón día 3 — consistente en altcoins bajo cap."
        ),
        "resultado": "WIN",
        "rb_real":   18.0,
    },

    # ═══════════════════════════════════════════════════════════════
    #  GRUPO 4 — VENTA MENOR RIESGO (Dorada)
    #  Entrada conservadora en tendencia bajista establecida
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
        "bb_estado":         "SQUEEZE",
        "bb_precio":         "MID",
        "volumen":           "LOW",
        "patron_vela":       "BEARISH_ENGULF",
        "fib_zona":          "0.382_0.618",
        "notas": (
            "VENTA MENOR RIESGO — Dorada. "
            "Tendencia bajista establecida y confirmada. "
            "Precio rebota técnico y toca EMA21 (rojo). "
            "EMA21 actúa como resistencia dinámica. "
            "Volumen bajo en rebote — sin compradores reales. "
            "BB comprime levemente. "
            "Zona Fibonacci 0.382-0.618 — retroceso técnico. "
            "Más conservadora que la roja — espera más confirmación. "
            "SL: encima de EMA21. "
            "Visto en SKYAI, LAB, ESPORTS imagen 10."
        ),
        "resultado": "WIN",
        "rb_real":   12.0,
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
            "SL: encima del máximo del rebote. "
            "Visto en SKYAI imagen 3 múltiples entradas doradas."
        ),
        "resultado": "WIN",
        "rb_real":   10.0,
    },

    # ═══════════════════════════════════════════════════════════════
    #  GRUPO 5 — SQUEEZE PROLONGADO UNIVERSAL
    #  El patrón más poderoso del sistema
    # ═══════════════════════════════════════════════════════════════

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
            "GATILLO: primera vela verde que cierra sobre EMA21 Y EMA55 "
            "con volumen mínimo 2x la media. "
            "SL: debajo EMA233 — muy corto porque naranja está plano. "
            "Movimientos observados: STG +154%, ALLO +310%, HEI +200%+. "
            "Con 20x: 3000%-6000%+ sobre capital. "
            "Bot no se aburre ni duda — detecta y entra automático."
        ),
        "resultado": "WIN",
        "rb_real":   100.0,
    },

    # ═══════════════════════════════════════════════════════════════
    #  GRUPO 6 — FIBONACCI RETRACEMENT + EMA CONFLUENCE
    #  Entradas en zonas Fibonacci con confirmación de EMAs
    # ═══════════════════════════════════════════════════════════════

    {
        "symbol":            "UNIVERSAL",
        "timeframe":         "15m",
        "signal_type":       "LONG_REVERSAL",
        "trend":             "BULLISH",
        "risk_level":        "MINIMO",
        "entry_type":        "RECOMPRA",
        "arrow_color":       "VERDE_CLARA",
        "ema21_vs_ema55":    "ABOVE",
        "ema55_vs_ema144":   "ABOVE",
        "ema144_vs_ema233":  "FLAT_TO_UP",
        "ema21_slope":       "UP",
        "ema144_slope":      "UP",
        "precio_vs_ema144":  "TOUCHING",
        "precio_vs_ema233":  "ABOVE",
        "bb_estado":         "SQUEEZE",
        "bb_precio":         "LOWER",
        "volumen":           "LOW",
        "patron_vela":       "HAMMER",
        "dias_squeeze":      1,
        "fib_zona":          "0.618_0.786",
        "notas": (
            "RECOMPRA FIBONACCI + EMA — Verde clara. "
            "Precio corrigió exactamente al nivel 0.618-0.786. "
            "En esa zona EMA144 (azul) actúa como soporte adicional. "
            "Confluencia: Fib 0.618 + EMA144 = zona de menor riesgo. "
            "BB recomprimiéndose. Volumen bajo en corrección. "
            "La corrección dura aprox 3 días post-impulso. "
            "Noche consolida en mínimos — madrugada rompe arriba. "
            "SL: debajo del 0.786 Fibonacci. "
            "TP: nuevo impulso = 150%-200% desde aquí. "
            "Visto en ALLO, HEI, SKYAI post-explosión."
        ),
        "resultado": "WIN",
        "rb_real":   40.0,
    },

    {
        "symbol":            "UNIVERSAL",
        "timeframe":         "15m",
        "signal_type":       "SHORT_REVERSAL",
        "trend":             "BEARISH",
        "risk_level":        "MINIMO",
        "entry_type":        "RECOMPRA",
        "arrow_color":       "DORADA",
        "ema21_vs_ema55":    "BELOW",
        "ema55_vs_ema144":   "BELOW",
        "ema144_vs_ema233":  "BELOW",
        "ema21_slope":       "DOWN",
        "ema144_slope":      "DOWN",
        "precio_vs_ema144":  "TOUCHING",
        "precio_vs_ema233":  "BELOW",
        "bb_estado":         "SQUEEZE",
        "bb_precio":         "UPPER",
        "volumen":           "LOW",
        "patron_vela":       "REJECTION",
        "fib_zona":          "0.618_0.786",
        "notas": (
            "RECOMPRA SHORT FIBONACCI — Dorada. "
            "Precio rebotó al nivel 0.618-0.786 de la caída. "
            "EMA144 (azul) actúa como resistencia en esa zona. "
            "Confluencia: Fib 0.618 + EMA144 = resistencia doble. "
            "Vela de rechazo con mecha arriba. "
            "SL: encima del 0.786 Fibonacci. "
            "TP: continuación de la caída. "
            "Visto en SKYAI bajista, LAB, ESPORTS."
        ),
        "resultado": "WIN",
        "rb_real":   14.0,
    },

    # ═══════════════════════════════════════════════════════════════
    #  GRUPO 7 — TENDENCIA ALCISTA CON PULLBACKS (ATOM style)
    #  Entradas en pullbacks a EMA21 en tendencia limpia
    # ═══════════════════════════════════════════════════════════════

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
        "precio_vs_ema55":   "ABOVE",
        "precio_vs_ema233":  "ABOVE",
        "bb_estado":         "CONTRACTING",
        "bb_precio":         "MID",
        "volumen":           "LOW",
        "patron_vela":       "HAMMER",
        "fib_zona":          "0.786_1.000",
        "notas": (
            "PULLBACK EN TENDENCIA ALCISTA — Verde clara. "
            "Cascada alcista perfecta: rojo>rosa>azul>naranja. "
            "Precio hace pullback limpio a EMA21 (rojo). "
            "EMA21 actúa como soporte dinámico perfecto. "
            "BB contrayéndose — pausa saludable. "
            "Volumen bajo en pullback — sin presión vendedora. "
            "Vela martillo en EMA21. "
            "SL: debajo del mínimo del pullback — muy corto. "
            "TP: dejar correr mientras cascada activa. "
            "Visto en ATOM días 7-11: 3 entradas perfectas. "
            "ATOMUSDT: $1.58 → $1.98 = +25% | Con 20x = 500%."
        ),
        "resultado": "WIN",
        "rb_real":   35.0,
    },

    # ═══════════════════════════════════════════════════════════════
    #  GRUPO 8 — POST-EXPLOSIÓN SEGUNDA OPORTUNIDAD
    #  Entrada después del primer impulso enorme
    # ═══════════════════════════════════════════════════════════════

    {
        "symbol":            "UNIVERSAL",
        "timeframe":         "15m",
        "signal_type":       "LONG_REVERSAL",
        "trend":             "BULLISH",
        "risk_level":        "BAJO",
        "entry_type":        "RECOMPRA",
        "arrow_color":       "VERDE_OSCURA",
        "ema21_vs_ema55":    "CROSSING_UP",
        "ema55_vs_ema144":   "BELOW",
        "ema144_vs_ema233":  "FLAT",
        "ema21_slope":       "UP",
        "ema144_slope":      "FLAT",
        "precio_vs_ema144":  "TOUCHING",
        "precio_vs_ema233":  "TOUCHING",
        "bb_estado":         "SQUEEZE",
        "bb_precio":         "LOWER",
        "volumen":           "LOW",
        "patron_vela":       "HAMMER",
        "fib_zona":          "0.618_0.786",
        "notas": (
            "POST-EXPLOSIÓN SEGUNDA OPORTUNIDAD — Verde oscura. "
            "El primer impulso fue enorme (100%-300%+). "
            "Precio corrigió y tocó EMA144 o EMA233. "
            "EMA21 vuelve a cruzar EMA55 — segunda confirmación. "
            "BB vuelve a comprimir — acumulación post-explosión. "
            "Volumen bajo durante corrección — saludable. "
            "Esta es la entrada que se pierde por desesperación. "
            "El bot espera el gatillo exacto sin emoción. "
            "SL: debajo EMA233 — corto porque naranja plano. "
            "TP: segundo impulso 150%-200%. "
            "Visto en HEI: corrección a $0.072 → subida a $0.095+."
        ),
        "resultado": "WIN",
        "rb_real":   35.0,
    },

    # ═══════════════════════════════════════════════════════════════
    #  GRUPO 9 — CONDICIONES A EVITAR
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
            "El bot ignora este par y busca el siguiente. "
            "Entrar aquí = SL repetidos sin dirección clara. "
            "EMA144 completamente plana confirma lateralidad."
        ),
        "resultado": "EVITAR",
        "rb_real":   0.0,
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
            "Esperar que la caída se detenga y el BB comprima. "
            "Primero: BB debe comprimir. "
            "Segundo: EMA21 debe dejar de caer. "
            "Tercero: vela de reversión con volumen. "
            "Solo entonces considerar entrada verde."
        ),
        "resultado": "EVITAR",
        "rb_real":   0.0,
    },

    # ═══════════════════════════════════════════════════════════════
    #  GRUPO 10 — PATRONES ESPECÍFICOS POR PAR
    # ═══════════════════════════════════════════════════════════════

    {
        "symbol":            "BTCUSDT",
        "timeframe":         "1h",
        "signal_type":       "SHORT_REVERSAL",
        "trend":             "BEARISH",
        "risk_level":        "BAJO",
        "entry_type":        "RECOMPRA",
        "arrow_color":       "ROJA",
        "ema21_vs_ema55":    "BELOW",
        "ema55_vs_ema144":   "BELOW",
        "ema144_vs_ema233":  "BELOW",
        "ema21_slope":       "DOWN",
        "ema144_slope":      "DOWN",
        "precio_vs_ema21":   "TOUCHING",
        "precio_vs_ema55":   "TOUCHING",
        "bb_estado":         "SQUEEZE",
        "bb_precio":         "MID",
        "volumen":           "LOW",
        "patron_vela":       "BEARISH_ENGULF",
        "fib_zona":          "0.382_0.618",
        "notas": (
            "RECOMPRA SHORT BTC H1 — Roja. "
            "Tendencia bajista larga establecida. "
            "EMA21+EMA55 actúan como resistencia doble. "
            "Precio rebota técnico con volumen bajo. "
            "Rechazo en zona rojo+rosa. "
            "R:B observado: 1:12 a 1:20. "
            "Patrón repetido 3 veces en Mayo 2024."
        ),
        "resultado": "WIN",
        "rb_real":   15.0,
    },

    {
        "symbol":            "STGUSDT",
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
        "dias_squeeze":      4,
        "fib_zona":          "N/A_NUEVO_MOVIMIENTO",
        "notas": (
            "STG SQUEEZE MÁXIMO — Verde clara. "
            "4 días de squeeze absoluto Mayo 28-31. "
            "Todas las EMAs completamente planas. "
            "Ruptura con volumen masivo. "
            "$0.165 → $0.420 = +154%. "
            "Con 20x = 3,080% sobre capital usado. "
            "Ejemplo perfecto del patrón universal de squeeze."
        ),
        "resultado": "WIN",
        "rb_real":   77.0,
    },
]


# ─────────────────────────────────────────────
#  FUNCIONES DE BD
# ─────────────────────────────────────────────
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
            intentos_maximo   INTEGER,
            maximo_previo     TEXT,
            dias_squeeze      INTEGER,
            fib_zona          TEXT,
            notas             TEXT,
            resultado         TEXT DEFAULT 'WIN',
            rb_real           REAL DEFAULT 0.0,
            timestamp         TEXT,
            pnl_percent       REAL DEFAULT NULL,
            pnl_usd           REAL DEFAULT NULL,
            exit_price        REAL DEFAULT NULL,
            exit_reason       TEXT DEFAULT NULL,
            trade_result      TEXT DEFAULT 'SEED'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pattern_stats (
            symbol       TEXT,
            signal_type  TEXT,
            arrow_color  TEXT,
            total        INTEGER DEFAULT 0,
            wins         INTEGER DEFAULT 0,
            avg_rb       REAL DEFAULT 0.0,
            win_rate     REAL DEFAULT 0.0,
            PRIMARY KEY (symbol, signal_type, arrow_color)
        )
    """)
    conn.commit()


def insert_patterns(conn, patterns):
    ts = datetime.now(timezone.utc).isoformat()
    ok = 0
    for p in patterns:
        try:
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
                    fib_zona, notas, resultado, rb_real, timestamp
                ) VALUES (
                    :symbol, :timeframe, :signal_type, :trend,
                    :risk_level, :entry_type, :arrow_color,
                    :ema21_vs_ema55, :ema55_vs_ema144, :ema144_vs_ema233,
                    :ema21_slope, :ema144_slope,
                    :precio_vs_ema21, :precio_vs_ema55,
                    :precio_vs_ema144, :precio_vs_ema233,
                    :bb_estado, :bb_precio, :volumen, :patron_vela,
                    :intentos_maximo, :maximo_previo, :dias_squeeze,
                    :fib_zona, :notas, :resultado, :rb_real, :timestamp
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
    print(f"  ÚNICO STRATEGY — BASE DE DATOS v2.0")
    print(f"{'═'*60}")
    print(f"  Total patrones:    {total}")
    print(f"  Operables:         {operables}")
    print(f"  A evitar:          {evitar}")
    print(f"{'─'*60}")

    print(f"\n  Por color de flecha:")
    colors = conn.execute("""
        SELECT arrow_color, COUNT(*) as n, AVG(rb_real) as avg_rb
        FROM patterns WHERE resultado != 'EVITAR'
        GROUP BY arrow_color ORDER BY avg_rb DESC
    """).fetchall()
    for c in colors:
        color, n, rb = c
        print(f"  {color:<20} {n} patrones  |  R:B prom 1:{rb:.0f}")

    print(f"\n  Por tipo de señal:")
    sigs = conn.execute("""
        SELECT signal_type, COUNT(*) as n, AVG(rb_real) as avg_rb
        FROM patterns WHERE resultado != 'EVITAR'
        GROUP BY signal_type ORDER BY avg_rb DESC
    """).fetchall()
    for s in sigs:
        sig, n, rb = s
        print(f"  {sig:<25} {n} patrones  |  R:B prom 1:{rb:.0f}")

    print(f"\n  Top R:B por patrón:")
    top = conn.execute("""
        SELECT symbol, signal_type, arrow_color, rb_real, notas
        FROM patterns WHERE resultado != 'EVITAR'
        ORDER BY rb_real DESC LIMIT 5
    """).fetchall()
    for i, t in enumerate(top, 1):
        sym, sig, color, rb, notas = t
        print(f"  {i}. {sym} {sig} [{color}] → 1:{rb:.0f}")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    print("\n🔥 ÚNICO STRATEGY — Inicializando BD v2.0")
    print("─" * 60)
    with sqlite3.connect(DB_PATH) as conn:
        print(f"  📂 BD: {DB_PATH}")
        init_db(conn)
        print(f"  📥 Insertando {len(PATTERNS)} patrones...")
        ok = insert_patterns(conn, PATTERNS)
        print(f"  ✅ {ok}/{len(PATTERNS)} patrones insertados")
        print_summary(conn)
    print("  🚀 Lista para el scanner. Siguiente: python main.py\n")