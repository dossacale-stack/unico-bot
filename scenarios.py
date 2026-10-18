"""
Escenarios sintéticos que reproducen cada flecha de las capturas de Karla.
Se construyen vela a vela; la vela de la flecha se diseña con los niveles reales (bandas, EMAs)
calculados en ese momento, igual que los vería el bot.
"""
from __future__ import annotations

from typing import List, Sequence, Tuple

import numpy as np
import pandas as pd

import strategy

STEP = pd.Timedelta(minutes=15)


def legs(start: float, segments: Sequence[Tuple[int, float, float]], seed: int = 0) -> np.ndarray:
    """segments = [(velas, cambio_por_vela, ruido)] → precios de cierre."""
    rng = np.random.default_rng(seed)
    closes: List[float] = []
    price = start
    for n, drift, noise in segments:
        for _ in range(n):
            price *= 1 + drift + rng.normal(0, noise)
            closes.append(price)
    return np.array(closes)


def candles(closes: np.ndarray, seed: int = 1, wick: float = 0.0012, start: str = "2026-09-01") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = len(closes)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) * (1 + np.abs(rng.normal(0, wick, n)))
    lows = np.minimum(opens, closes) * (1 - np.abs(rng.normal(0, wick, n)))
    volume = rng.lognormal(10, 0.3, n)
    idx = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": volume}, index=idx)


def add(df: pd.DataFrame, o: float, h: float, l: float, c: float, volume_mult: float = 1.3) -> pd.DataFrame:
    vol = float(df["volume"].iloc[-20:].mean()) * volume_mult
    row = pd.DataFrame({"open": [o], "high": [max(h, o, c)], "low": [min(l, o, c)], "close": [c], "volume": [vol]},
                       index=[df.index[-1] + STEP])
    return pd.concat([df, row])


def extend(df: pd.DataFrame, segments: Sequence[Tuple[int, float, float]], seed: int = 5) -> pd.DataFrame:
    closes = legs(float(df["close"].iloc[-1]), segments, seed=seed)
    more = candles(closes, seed=seed + 1, start=str(df.index[-1] + STEP))
    more.iloc[0, more.columns.get_loc("open")] = float(df["close"].iloc[-1])
    return pd.concat([df, more])


def levels(df: pd.DataFrame, close: float) -> pd.Series:
    """Indicadores de la vela siguiente si cerrara en `close` (las bandas incluyen ese cierre)."""
    tmp = add(df, close, close, close, close)
    return strategy.add_indicators(tmp).iloc[-1]


def shooting_star(df: pd.DataFrame, at: str = "band", wick_atr: float = 1.0) -> pd.DataFrame:
    """Estrella fugaz cuya mecha toca la banda superior (o el nivel indicado) y cierra abajo."""
    o = float(df["close"].iloc[-1])
    c = o * 0.997
    lv = levels(df, c)
    atr = float(lv["atr"])
    target = {"band": float(lv["bb_upper"]), "ema144": float(lv["ema_mid"]), "ema233": float(lv["ema_slow"]),
              "ema55": float(lv["ema_fast"])}[at]
    h = max(target * 1.002, o + wick_atr * atr, o * 1.004)
    l = c - 0.15 * atr
    return add(df, o, h, l, c)


def hammer(df: pd.DataFrame, at: str = "band", wick_atr: float = 1.0, depth: float = 0.999) -> pd.DataFrame:
    """Martillo cuya mecha toca la banda inferior (o el nivel indicado) y cierra arriba."""
    o = float(df["close"].iloc[-1])
    c = o * 1.003
    lv = levels(df, c)
    atr = float(lv["atr"])
    target = {"band": float(lv["bb_lower"]), "ema144": float(lv["ema_mid"]), "ema233": float(lv["ema_slow"]),
              "ema21": float(lv["ema21"]), "ema55": float(lv["ema_fast"])}[at]
    l = min(target * depth, o - wick_atr * atr, o * 0.996)
    h = c + 0.15 * atr
    return add(df, o, h, l, c)


def spike_up(df: pd.DataFrame, extra: float = 0.03) -> pd.DataFrame:
    """Vela de clímax: mecha muy por encima de la banda superior y cierre rechazado."""
    o = float(df["close"].iloc[-1])
    c = o * 0.998
    lv = levels(df, c)
    h = float(lv["bb_upper"]) * (1 + extra)
    return add(df, o, h, c * 0.997, c, volume_mult=3.0)


def spike_down(df: pd.DataFrame, extra: float = 0.03) -> pd.DataFrame:
    """Capitulación: mecha muy por debajo de la banda inferior y cierre recuperado (flecha verde)."""
    o = float(df["close"].iloc[-1])
    c = o * 1.004
    lv = levels(df, c)
    l = float(lv["bb_lower"]) * (1 - extra)
    return add(df, o, c * 1.001, l, c, volume_mult=3.0)


# ---------------------------------------------------------------------------
# Escenarios completos (devuelven el DataFrame cuya ÚLTIMA vela es la flecha)
# ---------------------------------------------------------------------------
def scenario_roja(seed: int = 0, climax: bool = True, lower_high: bool = True) -> pd.DataFrame:
    df = candles(legs(100.0, [(520, 0.0004, 0.0025)], seed=seed), seed=seed + 1)
    if climax:
        df = extend(df, [(6, 0.012, 0.001)], seed=seed + 2)      # empuje fuerte
        df = spike_up(df, extra=0.03)                            # techo con mecha (clímax)
        df = extend(df, [(14, -0.007, 0.001)], seed=seed + 3)     # retroceso bajo la EMA55
        df = extend(df, [(10, 0.006, 0.001)], seed=seed + 4)      # nueva subida hacia la banda
    else:
        df = extend(df, [(6, 0.0015, 0.001)], seed=seed + 2)     # subida suave, sin estirarse
        df = extend(df, [(14, -0.0012, 0.001)], seed=seed + 3)
        df = extend(df, [(10, 0.0012, 0.001)], seed=seed + 4)
    if not lower_high:
        top = float(df["high"].iloc[-40:].max())
        o = float(df["close"].iloc[-1])
        return add(df, o, top * 1.01, o * 0.996, o * 0.997)
    return shooting_star(df, at="band")


def scenario_amarilla(seed: int = 0) -> pd.DataFrame:
    df = candles(legs(100.0, [(520, -0.0006, 0.0025)], seed=seed), seed=seed + 1)
    df = extend(df, [(8, -0.008, 0.001)], seed=seed + 2)          # bajada hasta la banda inferior
    df = extend(df, [(9, 0.005, 0.001)], seed=seed + 3)           # subida hacia la resistencia
    return shooting_star(df, at="band")


def scenario_verde(seed: int = 0) -> pd.DataFrame:
    df = candles(legs(100.0, [(520, -0.0005, 0.0025)], seed=seed), seed=seed + 1)
    df = extend(df, [(6, -0.012, 0.001)], seed=seed + 2)          # capitulación
    return spike_down(df, extra=0.03)


def scenario_azul_tras_verde(seed: int = 0, bounce: float = 0.008) -> pd.DataFrame:
    df = scenario_verde(seed)
    df = extend(df, [(12, bounce, 0.001)], seed=seed + 3)         # rebote con cierres sobre la EMA55
    df = extend(df, [(8, -0.006, 0.001)], seed=seed + 4)          # retroceso: mínimo más alto
    return hammer(df, at="band")


def scenario_azul_retroceso(seed: int = 0) -> pd.DataFrame:
    df = candles(legs(100.0, [(520, 0.0005, 0.0025)], seed=seed), seed=seed + 1)
    df = extend(df, [(6, 0.006, 0.001)], seed=seed + 2)           # empuje a la banda superior
    df = extend(df, [(10, -0.005, 0.001)], seed=seed + 3)         # retroceso
    return hammer(df, at="band")


def scenario_tope(seed: int = 0) -> pd.DataFrame:
    df = candles(legs(100.0, [(520, 0.0004, 0.0025)], seed=seed), seed=seed + 1)
    df = extend(df, [(8, 0.012, 0.001)], seed=seed + 2)
    o = float(df["close"].iloc[-1])
    c = o * 0.998
    lv = levels(df, c)
    h = float(lv["bb_upper"]) * 1.03
    return add(df, o, h, c * 0.997, c, volume_mult=3.0)


def scenario_desinteres(seed: int = 0) -> pd.DataFrame:
    """Como TIA / VVV: subida, pierde la morada y la azul, vuelve a la morada por debajo y no la aguanta."""
    df = candles(legs(100.0, [(520, 0.0006, 0.0025)], seed=seed), seed=seed + 1)
    df = extend(df, [(30, -0.0035, 0.001)], seed=seed + 2)        # descuento: cae bajo la morada y la azul
    df = extend(df, [(8, 0.002, 0.0008)], seed=seed + 3)          # se acerca a la morada por debajo
    return shooting_star(df, at="ema55", wick_atr=0.5)


def scenario_interes(seed: int = 0) -> pd.DataFrame:
    """Subida ordenada (morada > azul > amarilla), retrocede a la morada y se sostiene."""
    df = candles(legs(100.0, [(520, 0.0006, 0.0025)], seed=seed), seed=seed + 1)
    df = extend(df, [(16, 0.004, 0.001)], seed=seed + 2)          # impulso: crea el techo previo (zona)
    df = extend(df, [(14, -0.003, 0.0008)], seed=seed + 3)        # retroceso hacia la morada
    return hammer(df, at="ema55", wick_atr=0.5)
