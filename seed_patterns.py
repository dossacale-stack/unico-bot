# seed_patterns.py — AÑADIR ESTOS PATRONES WASHI

# ✅ CORRECCIÓN: Lista principal definida antes de usarse
PATTERNS = []

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

# ✅ CORRECCIÓN: PATTERNS ya existe, ahora se extiende sin error
PATTERNS.extend(WASHI_PATTERNS)
