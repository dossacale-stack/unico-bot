"""
strategy.py — Las flechas de Karla convertidas en reglas (v9)
==============================================================
La MISMA lógica para el bot en vivo, el backtest y el gráfico del panel.
Temporalidad de entrada por defecto: 15 minutos (la de sus capturas).

Indicadores (los de sus gráficos de TradingView):
  EMA 21 / 55 / 144 / 233 · Bandas de Bollinger (21, 2) · ATR 14 · volumen relativo (20)

Fase del mercado (EMA55 frente a EMA144):
  ALCISTA si la EMA55 está por encima de la EMA144, BAJISTA si está por debajo,
  TRANSICIÓN si están a menos de PHASE_TOL (0,25 %) una de otra (cuenta para ambos lados).

Las flechas se confirman al CERRAR la vela; la entrada es en la apertura de la vela siguiente.

  🔴 ROJA — Venta de menor riesgo (corto, reversión)
     Fase alcista o transición. En las últimas 24 h hubo un techo que tocó la banda superior
     y se estiró por encima de la EMA55 (clímax de compra, agotamiento ≥ 0,40). El precio
     retrocedió con cierres bajo la EMA55, volvió a la banda superior y marcó un MÁXIMO MÁS BAJO
     con vela de rechazo bajista.

  🟡 AMARILLA — Reventa (corto, continuación): "donde muere el interés comprador"
     Fase bajista o transición. El precio sube hasta una resistencia (banda superior o EMA 144/233),
     la toca, cierra por debajo con vela de rechazo bajista y es un máximo más bajo que el de las
     últimas 24 h. La subida empezó en la banda inferior (tocada en las últimas 6 h).

  🟢 VERDE — Agotamiento de ventas (aviso; no es entrada salvo ENTRY_ON_GREEN)
     Fase bajista o transición. Mínimo de las últimas 12 h, por debajo de las EMAs 55/144/233,
     tocando la banda inferior, con vela de rechazo alcista. Cierra los cortos que van ganando.

  🔵 AZUL — Compra de menor riesgo (largo)
     a) Tras la verde: fase bajista o transición, MÍNIMO MÁS ALTO que el suelo de las últimas 24 h
        (suelo que tocó la banda inferior y se estiró bajo la EMA55, agotamiento ≥ 0,40), después
        de un rebote con cierres sobre la EMA55, tocando la banda inferior, la EMA21 o la EMA55,
        con vela de rechazo alcista.
     b) Retroceso: fase alcista o transición, el precio baja a un soporte (banda inferior o
        EMA 144/233), lo toca, cierra por encima con vela de rechazo alcista. La bajada empezó en
        la banda superior (tocada en las últimas 6 h).

  ⚪ TOPE — Agotamiento de compras (espejo de la verde; aviso, no entrada salvo ENTRY_ON_TOP)
     Cierra los largos que van ganando.

Retrocesos de Fibonacci (OPCIONAL, desactivado por defecto: FIB_LEVELS vacío). Karla explicó que las zonas
horizontales de sus capturas son Fibonacci, pero no pidió que el bot los use; queda preparado por si lo decide:
  Se toma el último gran movimiento de las últimas FIB_HOURS (96 h): máximo y mínimo, y cuál vino después.
  Si el último tramo fue de SUBIDA, los niveles 38,2 / 50 / 61,8 / 78,6 % por debajo del máximo son soportes;
  si fue de BAJADA, los niveles por encima del mínimo son resistencias. Solo cuenta si el tramo mide al menos
  FIB_MIN_RANGE_ATR ATR. Tocar un nivel vale igual que tocar la banda o la EMA144/233 (amarilla, azul, roja).

INTERÉS / DESINTERÉS (v10, explicado por Karla con TIA y VVV — la entrada por defecto):
  Colores: morada = EMA55, azul = EMA144, amarilla = EMA233 (exponenciales).
  🔻 DESINTERÉS (venta): el precio cierra por debajo de la morada y de la azul; vuelve a acercarse por
     debajo a una de las tres (morada, azul o amarilla), la toca con la mecha y NO logra cerrar por encima
     (vela de rechazo bajista). "No aguantó": el interés comprador murió.
  🔺 INTERÉS (compra): el precio cierra por encima de la morada y de la azul; retrocede a una de las tres,
     la toca y se sostiene (vela de rechazo alcista).
  Las tres EMAs no pueden estar enredadas (base de lado): deben separarse al menos EMA_MIN_SPREAD.
  SALIDA = ZONA: se cierra TODO en la zona donde antes reaccionó el precio — el suelo previo más cercano
  (ventas) o el techo previo más cercano (compras) de las últimas ZONE_HOURS, que esté al menos a
  MIN_TARGET_R veces el riesgo. Si no hay zona así, no se entra.

Vela de rechazo: martillo / estrella fugaz (mecha ≥ 1,2 × cuerpo y ≥ 40 % del rango, cuerpo en el
otro extremo) o vela envolvente (cuerpo ≥ 50 % del rango que envuelve el cuerpo anterior). Además la vela
mide al menos 0,8 ATR y es el mínimo (compras) o el máximo (ventas) de las últimas 2 horas.
Stop: más allá del extremo de las últimas 3 velas + el mayor entre 0,3 % y 1 ATR (con stops más
ceñidos, el ruido de 15m los barría en la prueba con velas reales).
Puntaje (0 a 1) = 50 % contexto + 50 % calidad del rechazo (RejectionAnalyzer, 5 contextos).
  Contexto en ROJA / AZUL tras verde / VERDE / TOPE = agotamiento del clímax con la fórmula original
  (distancia a la EMA55 35 %, velas seguidas 20 %, expansión de Bollinger 25 %, tiempo en zona 20 %).
  Contexto en AMARILLA / AZUL retroceso = fuerza de la tendencia (orden de las 4 EMAs).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config import TF_MINUTES
from rejection_analyzer import RejectionAnalyzer

LONG, SHORT = "LONG", "SHORT"

EMA_21, EMA_55, EMA_144, EMA_233 = 21, 55, 144, 233
BB_PERIOD = 21
BB_STD = 2.0
ATR_PERIOD = 14
VOL_PERIOD = 20
MIN_BARS = EMA_233 + 20  # 253 velas mínimas para que las EMAs y bandas tengan sentido

# Fórmula original de agotamiento (v7.3), ahora medida sobre el clímax
MIN_DISTANCE_PCT = 0.01
DIST_SCALE = 0.08
CONSEC_SCALE = 8.0
CONSEC_MAX = 18
BB_LOOKBACK = 9
BB_SCALE = 0.30
ZONE_THRESHOLD = 0.015
ZONE_WINDOW = 30
ZONE_SCALE = 15.0
WEIGHTS = {"dist": 0.35, "consec": 0.20, "bb": 0.25, "time": 0.20}

ROJA, AMARILLA, VERDE, AZUL, TOPE = "ROJA", "AMARILLA", "VERDE", "AZUL", "TOPE"
DESINTERES, INTERES = "DESINTERES", "INTERES"
SETUP_NAMES = {
    ROJA: "Venta de menor riesgo",
    AMARILLA: "Reventa",
    VERDE: "Agotamiento de ventas",
    AZUL: "Compra de menor riesgo",
    TOPE: "Agotamiento de compras",
    DESINTERES: "Venta por desinterés",
    INTERES: "Compra por interés",
}
VARIANT_NAMES = {
    "tras_techo": "tras el techo",
    "retroceso": "en retroceso",
    "tras_verde": "tras la verde",
    "agresiva": "agresiva",
    "morada": "en la morada (EMA55)",
    "azul": "en la azul (EMA144)",
    "amarilla": "en la amarilla (EMA233)",
}
# Clave por tipo de flecha para la memoria de resultados (SetupMemory)
SETUP_CODES = {
    "ROJA": 1001,
    "AMARILLA": 1002,
    "AZUL_RETROCESO": 1003,
    "AZUL_TRAS_VERDE": 1004,
    "VERDE": 1005,
    "TOPE": 1006,
    "DESINTERES": 1007,
    "INTERES": 1008,
}
# Nombre corto de cada clave de SETUP_CODES (panel, informes del backtest)
SETUP_KEY_NAMES = {
    "ROJA": "ROJA · venta tras el techo",
    "AMARILLA": "AMARILLA · reventa",
    "AZUL_RETROCESO": "AZUL · compra en retroceso",
    "AZUL_TRAS_VERDE": "AZUL · compra tras la verde",
    "VERDE": "VERDE · compra agresiva",
    "TOPE": "TOPE · venta agresiva",
    "DESINTERES": "Venta por desinterés",
    "INTERES": "Compra por interés",
}
FASE_NAMES = {1: "ALCISTA", -1: "BAJISTA", 0: "TRANSICIÓN"}
STACK_NAMES = {1: "ALCISTA", -1: "BAJISTA", 0: "ENREDADA"}


def setup_key(setup: str, variant: str = "") -> str:
    return f"{setup}_{variant.upper()}" if setup == AZUL and variant else setup


# ---------------------------------------------------------------------------
# Parámetros
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StrategyParams:
    timeframe: str = "15m"
    min_score: float = 0.40
    rejection_min_score: float = 0.40
    swing_bars: int = 96             # ventana del techo/suelo previo (24 h en 15m)
    new_extreme_bars: int = 48       # "mínimo/máximo de las últimas 12 h" (en 15m)
    min_gap: int = 2                 # velas mínimas entre el extremo previo y la entrada
    pivot_bars: int = 8              # la vela de la flecha es el extremo de las últimas 8 velas (2 h en 15m)
    leg_bars: int = 24               # en retrocesos, la banda contraria se tocó en las últimas 24 velas (6 h)
    min_range_atr: float = 0.8       # la vela de rechazo mide al menos 0,8 ATR (no una vela diminuta)
    event_min_exhaustion: float = 0.35
    climax_min_exhaustion: float = 0.40
    climax_min_stretch: float = 0.02
    retrace_min_pct: float = 0.015
    retrace_min_atr: float = 1.0
    band_tol: float = 0.003
    touch_tol: float = 0.005
    phase_tol: float = 0.0025
    wick_body: float = 1.2
    wick_range: float = 0.40
    engulf_body: float = 0.5
    stop_lookback: int = 3
    stop_buffer: float = 0.003
    stop_atr: float = 1.0
    setups: Tuple[str, ...] = (DESINTERES, INTERES)
    entry_on_green: bool = False
    entry_on_top: bool = False
    htf_filter: str = "off"
    fib_bars: int = 384              # 96 h en 15m
    fib_levels: Tuple[float, ...] = ()   # desactivado por defecto (FIB_LEVELS)
    fib_min_range_atr: float = 5.0
    zone_bars: int = 384             # zonas de las últimas 96 h (15m)
    zone_pivot: int = 8              # un suelo/techo es el extremo de ±2 h a su alrededor
    min_target_r: float = 1.5
    ema_min_spread: float = 0.004    # las 3 EMAs separadas al menos 0,4 % (no enredadas)

    @classmethod
    def from_config(cls, cfg) -> "StrategyParams":
        per_hour = 60.0 / TF_MINUTES[cfg.entry_timeframe]
        return cls(
            timeframe=cfg.entry_timeframe,
            min_score=cfg.min_score,
            rejection_min_score=cfg.rejection_min_score,
            swing_bars=max(4, int(round(cfg.swing_hours * per_hour))),
            new_extreme_bars=max(2, int(round(cfg.new_extreme_hours * per_hour))),
            pivot_bars=max(2, int(round(cfg.pivot_hours * per_hour))),
            leg_bars=max(2, int(round(cfg.leg_hours * per_hour))),
            min_range_atr=cfg.min_range_atr,
            event_min_exhaustion=cfg.event_min_exhaustion,
            climax_min_exhaustion=cfg.climax_min_exhaustion,
            climax_min_stretch=cfg.climax_min_stretch,
            retrace_min_pct=cfg.retrace_min_pct,
            retrace_min_atr=cfg.retrace_min_atr,
            band_tol=cfg.band_tol,
            touch_tol=cfg.touch_tol,
            phase_tol=cfg.phase_tol,
            wick_body=cfg.wick_body_ratio,
            wick_range=cfg.wick_range_ratio,
            engulf_body=cfg.engulf_body,
            stop_lookback=cfg.stop_lookback,
            stop_buffer=cfg.stop_buffer,
            stop_atr=cfg.stop_atr,
            setups=tuple(cfg.setups),
            entry_on_green=cfg.entry_on_green,
            entry_on_top=cfg.entry_on_top,
            htf_filter=cfg.htf_filter,
            fib_bars=max(8, int(round(cfg.fib_hours * per_hour))),
            fib_levels=tuple(cfg.fib_levels),
            fib_min_range_atr=cfg.fib_min_range_atr,
            zone_bars=max(16, int(round(cfg.zone_hours * per_hour))),
            zone_pivot=max(2, int(round(cfg.zone_pivot_hours * per_hour))),
            min_target_r=cfg.min_target_r,
            ema_min_spread=cfg.ema_min_spread,
        )

    def public_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["setups"] = list(self.setups)
        data["fib_levels"] = list(self.fib_levels)
        return data


# ---------------------------------------------------------------------------
# Resultados
# ---------------------------------------------------------------------------
@dataclass
class Signal:
    symbol: str
    side: str                      # LONG / SHORT
    setup: str                     # ROJA, AMARILLA, AZUL (o VERDE/TOPE si se activan como entrada)
    variant: str                   # tras_techo, retroceso, tras_verde, agresiva
    timeframe: str
    trend_timeframe: str
    candle_open_time: pd.Timestamp
    candle_close_time: pd.Timestamp
    entry_ref: float               # cierre de la vela de la flecha
    stop_loss: float
    atr: float
    score: float                   # puntaje final 0-1
    contexto: float = 0.0
    rechazo: float = 0.0
    components: Dict[str, Any] = field(default_factory=dict)
    rejection: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    ref_price: Optional[float] = None       # techo/suelo previo (roja / azul tras verde)
    ref_time: Optional[pd.Timestamp] = None
    target: Optional[float] = None          # zona donde se cierra todo (INTERÉS / DESINTERÉS)
    pattern_id: Optional[int] = None        # patrón WASHI parecido (seed_patterns)
    pattern_name: Optional[str] = None
    pattern_features: Dict[str, str] = field(default_factory=dict)
    hist: Dict[str, Any] = field(default_factory=dict)
    signal_id: Optional[int] = None

    @property
    def is_long(self) -> bool:
        return self.side == LONG

    @property
    def signal_type(self) -> str:
        return f"{self.side}_{self.setup}"

    @property
    def setup_key(self) -> str:
        return setup_key(self.setup, self.variant)

    @property
    def setup_code(self) -> int:
        return SETUP_CODES.get(self.setup_key, SETUP_CODES.get(self.setup, 1000))

    @property
    def is_reversal(self) -> bool:
        return self.setup in (ROJA, VERDE, TOPE) or self.variant == "tras_verde"

    @property
    def sl_distance_pct(self) -> float:
        return abs(self.entry_ref - self.stop_loss) / self.entry_ref if self.entry_ref else 0.0

    @property
    def label(self) -> str:
        variant = VARIANT_NAMES.get(self.variant, self.variant)
        return f"{self.setup} · {SETUP_NAMES.get(self.setup, self.setup)}" + (f" ({variant})" if variant else "")

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        for key in ("candle_open_time", "candle_close_time", "ref_time"):
            if data.get(key) is not None:
                data[key] = pd.Timestamp(data[key]).isoformat()
        data["setup_key"] = self.setup_key
        return data


@dataclass
class MarketEvent:
    """Flecha de aviso: VERDE (agotamiento de ventas) o TOPE (agotamiento de compras)."""
    kind: str
    symbol: str
    timeframe: str
    candle_open_time: pd.Timestamp
    candle_close_time: pd.Timestamp
    price: float           # mínimo de la vela (VERDE) o máximo (TOPE)
    close: float
    exhaustion: float      # fórmula original de agotamiento en esa vela
    reasons: List[str] = field(default_factory=list)

    @property
    def closes_side(self) -> str:
        """Lado de las posiciones que este aviso manda cerrar."""
        return SHORT if self.kind == VERDE else LONG

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["candle_open_time"] = pd.Timestamp(self.candle_open_time).isoformat()
        data["candle_close_time"] = pd.Timestamp(self.candle_close_time).isoformat()
        return data


@dataclass
class Discarded:
    setup: str
    variant: str
    side: str
    reason: str
    score: Optional[float] = None


@dataclass
class BarResult:
    entry: Optional[Signal] = None
    events: List[MarketEvent] = field(default_factory=list)
    discarded: List[Discarded] = field(default_factory=list)


@dataclass
class ScanState:
    """Lo que el bot recuerda de las velas anteriores de la misma moneda (ciclo de las flechas)."""
    last_long_reversal: Optional[int] = None    # última AZUL tras verde (suelo confirmado)
    last_short_reversal: Optional[int] = None   # última ROJA (techo confirmado)

    def register(self, i: int, signal: "Signal") -> None:
        if signal.setup == AZUL and signal.variant == "tras_verde":
            self.last_long_reversal = i
        elif signal.setup == ROJA:
            self.last_short_reversal = i


@dataclass
class _Draft:
    setup: str
    variant: str
    side: str
    contexto: float
    ref_idx: Optional[int] = None
    reasons: List[str] = field(default_factory=list)
    zone_exit: bool = False


# ---------------------------------------------------------------------------
# Indicadores
# ---------------------------------------------------------------------------
def tf_delta(timeframe: str) -> pd.Timedelta:
    return pd.Timedelta(minutes=TF_MINUTES[timeframe])


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """EMAs 21/55/144/233, Bollinger (21, 2), ATR 14 y volumen relativo."""
    out = df.copy()
    close = out["close"]
    out["ema21"] = close.ewm(span=EMA_21, adjust=False).mean()
    out["ema_fast"] = close.ewm(span=EMA_55, adjust=False).mean()
    out["ema_mid"] = close.ewm(span=EMA_144, adjust=False).mean()
    out["ema_slow"] = close.ewm(span=EMA_233, adjust=False).mean()

    out["bb_mid"] = close.rolling(BB_PERIOD).mean()
    out["bb_std"] = close.rolling(BB_PERIOD).std()
    out["bb_upper"] = out["bb_mid"] + BB_STD * out["bb_std"]
    out["bb_lower"] = out["bb_mid"] - BB_STD * out["bb_std"]
    out["bb_bandwidth"] = (out["bb_upper"] - out["bb_lower"]) / out["bb_mid"]

    prev_close = close.shift(1)
    out["tr"] = np.maximum(
        out["high"] - out["low"],
        np.maximum((out["high"] - prev_close).abs(), (out["low"] - prev_close).abs()),
    )
    out["atr"] = out["tr"].rolling(ATR_PERIOD).mean()

    out["vol_avg"] = out["volume"].rolling(VOL_PERIOD).mean()
    out["vol_ratio"] = out["volume"] / out["vol_avg"].replace(0, 1e-9)
    return out


def _run_length(mask: pd.Series) -> pd.Series:
    """Cuántas velas seguidas cumplen la condición, terminando en cada vela."""
    m = mask.astype(np.int64)
    groups = (m != m.shift()).cumsum()
    return m.groupby(groups).cumsum() * m


def _fib_swing(high: np.ndarray, low: np.ndarray, bars: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Máximo y mínimo de las `bars` velas ANTERIORES y si el último tramo fue de subida (1) o bajada (0)."""
    n = len(high)
    hi = np.full(n, np.nan)
    lo = np.full(n, np.nan)
    up = np.full(n, -1, dtype=np.int8)
    if n <= bars or bars < 2:
        return hi, lo, up
    wh = np.lib.stride_tricks.sliding_window_view(high[:-1], bars)
    wl = np.lib.stride_tricks.sliding_window_view(low[:-1], bars)
    ih, il = wh.argmax(axis=1), wl.argmin(axis=1)
    rows = np.arange(len(ih))
    hi[bars:] = wh[rows, ih]
    lo[bars:] = wl[rows, il]
    up[bars:] = (il < ih).astype(np.int8)          # el mínimo vino antes que el máximo: tramo de subida
    return hi, lo, up


def add_signal_features(df: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    """Calcula, vela a vela y sin mirar al futuro, todo lo que usan las flechas."""
    out = df
    o, h, l, c = out["open"], out["high"], out["low"], out["close"]
    e21, e55, e144, e233 = out["ema21"], out["ema_fast"], out["ema_mid"], out["ema_slow"]
    up, lo = out["bb_upper"], out["bb_lower"]

    # --- Patrones de vela ---------------------------------------------------
    rng = h - l
    has_rng = rng > 0
    body = (c - o).abs()
    body_eff = body.where(body > 0, rng * 0.1)
    top_body = np.maximum(o, c)
    bot_body = np.minimum(o, c)
    upper_wick = h - top_body
    lower_wick = bot_body - l
    pin_bear = has_rng & (upper_wick >= p.wick_body * body_eff) & (upper_wick >= p.wick_range * rng) \
        & (top_body <= l + 0.6 * rng)
    pin_bull = has_rng & (lower_wick >= p.wick_body * body_eff) & (lower_wick >= p.wick_range * rng) \
        & (bot_body >= h - 0.6 * rng)
    prev_o, prev_c = o.shift(1), c.shift(1)
    eng_bear = has_rng & (c < o) & (prev_c > prev_o) & (o >= prev_c * (1 - 1e-4)) & (c <= prev_o) \
        & (body >= p.engulf_body * rng)
    eng_bull = has_rng & (c > o) & (prev_c < prev_o) & (o <= prev_c * (1 + 1e-4)) & (c >= prev_o) \
        & (body >= p.engulf_body * rng)
    big_enough = (rng >= p.min_range_atr * out["atr"]) | out["atr"].isna()   # sin ATR la vela no es válida igualmente
    pivot_low = l <= l.rolling(p.pivot_bars, min_periods=1).min()
    pivot_high = h >= h.rolling(p.pivot_bars, min_periods=1).max()
    pat_bear = ((pin_bear | eng_bear) & big_enough & pivot_high).fillna(False).astype(bool)
    pat_bull = ((pin_bull | eng_bull) & big_enough & pivot_low).fillna(False).astype(bool)
    both = pat_bear & pat_bull                       # vela de indecisión: no cuenta
    out["pat_bear"] = pat_bear & ~both
    out["pat_bull"] = pat_bull & ~both
    out["pat_name"] = np.select(
        [out["pat_bull"] & pin_bull, out["pat_bull"] & eng_bull, out["pat_bear"] & pin_bear, out["pat_bear"] & eng_bear],
        ["martillo", "envolvente alcista", "estrella fugaz", "envolvente bajista"], default="")

    # --- Fase y orden de las EMAs ---------------------------------------------
    out["fase"] = np.where(e55 > e144 * (1 + p.phase_tol), 1,
                           np.where(e55 < e144 * (1 - p.phase_tol), -1, 0)).astype(np.int8)
    bull_full = (e21 > e55) & (e55 > e144) & (e144 > e233)
    bear_full = (e21 < e55) & (e55 < e144) & (e144 < e233)
    out["trend_long"] = np.select([bull_full, (e55 > e144) & (e144 > e233), e55 > e144], [1.0, 0.75, 0.5], default=0.25)
    out["trend_short"] = np.select([bear_full, (e55 < e144) & (e144 < e233), e55 < e144], [1.0, 0.75, 0.5], default=0.25)
    bull_stack = (e55 > e144) & (e144 > e233) & (c > e55)   # misma definición que la v7.3 (patrones WASHI)
    bear_stack = (e55 < e144) & (e144 < e233) & (c < e55)
    out["stack"] = np.where(bull_stack, 1, np.where(bear_stack, -1, 0)).astype(np.int8)

    # --- Toques de soportes y resistencias -----------------------------------------
    touch_up_band = h >= up * (1 - p.band_tol)
    touch_dn_band = l <= lo * (1 + p.band_tol)

    def res(level):   # tocó el nivel con la mecha y cerró por debajo
        return (h >= level * (1 - p.touch_tol)) & (c <= level)

    def sup(level):   # tocó el nivel con la mecha y cerró por encima
        return (l <= level * (1 + p.touch_tol)) & (c >= level)

    # --- Retrocesos de Fibonacci del último gran tramo (solo velas ANTERIORES) ------
    fib_hi, fib_lo, fib_up = _fib_swing(h.to_numpy(float), l.to_numpy(float), p.fib_bars)
    out["fib_hi"], out["fib_lo"], out["fib_up"] = fib_hi, fib_lo, fib_up
    rng_fib = fib_hi - fib_lo
    fib_ok = np.isfinite(rng_fib) & (rng_fib >= p.fib_min_range_atr * out["atr"].to_numpy(float))
    fib_res = np.zeros(len(out), bool)
    fib_sup = np.zeros(len(out), bool)
    fib_res_lvl = np.zeros(len(out))
    fib_sup_lvl = np.zeros(len(out))
    hv, lv, cv = h.to_numpy(float), l.to_numpy(float), c.to_numpy(float)
    with np.errstate(invalid="ignore"):
        for r in p.fib_levels:
            sup_lvl = fib_hi - r * rng_fib            # tramo de subida: soportes
            res_lvl = fib_lo + r * rng_fib            # tramo de bajada: resistencias
            hit_s = fib_ok & (fib_up == 1) & (lv <= sup_lvl * (1 + p.touch_tol)) & (cv >= sup_lvl) & ~fib_sup
            hit_r = fib_ok & (fib_up == 0) & (hv >= res_lvl * (1 - p.touch_tol)) & (cv <= res_lvl) & ~fib_res
            fib_sup_lvl[hit_s] = r
            fib_res_lvl[hit_r] = r
            fib_sup |= hit_s
            fib_res |= hit_r
    out["fib_sup"], out["fib_res"] = fib_sup, fib_res
    out["fib_sup_lvl"], out["fib_res_lvl"] = fib_sup_lvl, fib_res_lvl

    out["touch_up_band"] = touch_up_band.fillna(False)
    out["touch_dn_band"] = touch_dn_band.fillna(False)
    # Retrocesos (amarilla / azul): banda de Bollinger o EMAs lentas 144/233 (como en sus capturas).
    # Un toque solo de la EMA55 pasa casi en cada vela de una tendencia: suma puntaje, pero no basta.
    out["res_touch"] = ((touch_up_band & (c <= up)) | res(e144) | res(e233)).fillna(False) | fib_res
    out["sup_touch"] = ((touch_dn_band & (c >= lo)) | sup(e144) | sup(e233)).fillna(False) | fib_sup
    out["sup_rebound"] = ((touch_dn_band & (c >= lo)) | sup(e21) | sup(e55)).fillna(False) | fib_sup
    out["roja_touch"] = touch_up_band.fillna(False) | fib_res

    cluster_lo = np.minimum(np.minimum(e55, e144), e233)
    cluster_hi = np.maximum(np.maximum(e55, e144), e233)
    out["below_cluster"] = l < cluster_lo
    out["above_cluster"] = h > cluster_hi

    # Suelos y techos previos (zonas). Un suelo en j se confirma en j + zone_pivot (se usa solo después).
    w = 2 * p.zone_pivot + 1
    out["zone_low"] = (l <= l.rolling(w, center=True, min_periods=1).min()).fillna(False)
    out["zone_high"] = (h >= h.rolling(w, center=True, min_periods=1).max()).fillna(False)
    ema_hi = np.maximum(np.maximum(e55, e144), e233)
    ema_lo = np.minimum(np.minimum(e55, e144), e233)
    out["ema_spread"] = (ema_hi - ema_lo) / c

    out["new_low"] = l <= l.rolling(p.new_extreme_bars, min_periods=1).min()
    out["new_high"] = h >= h.rolling(p.new_extreme_bars, min_periods=1).max()

    # --- Agotamiento (fórmula original), medido con el extremo de la vela -------------
    stretch_up = (h - e55) / e55
    stretch_dn = (e55 - l) / e55
    out["stretch_up"] = stretch_up
    out["stretch_dn"] = stretch_dn
    ds_up = ((stretch_up - MIN_DISTANCE_PCT) / DIST_SCALE).clip(lower=0.0, upper=1.0)
    ds_dn = ((stretch_dn - MIN_DISTANCE_PCT) / DIST_SCALE).clip(lower=0.0, upper=1.0)

    prev = c.shift(1)
    consec_up = _run_length(c > prev).shift(1).fillna(0).clip(upper=CONSEC_MAX)
    consec_dn = _run_length(c < prev).shift(1).fillna(0).clip(upper=CONSEC_MAX)
    out["consec_up"] = consec_up
    out["consec_dn"] = consec_dn

    bandwidth = out["bb_bandwidth"]
    bw_prev = bandwidth.shift(BB_LOOKBACK)
    expansion = pd.Series(np.where(bw_prev > 0, (bandwidth - bw_prev) / bw_prev, 0.0), index=out.index).fillna(0.0)
    out["bb_expansion"] = expansion
    bb_score = (expansion.clip(lower=0.0) / BB_SCALE).clip(upper=1.0)

    dist_close = (c - e55) / e55
    zone_up = (dist_close > ZONE_THRESHOLD).astype(float).rolling(ZONE_WINDOW, min_periods=1).sum()
    zone_dn = (dist_close < -ZONE_THRESHOLD).astype(float).rolling(ZONE_WINDOW, min_periods=1).sum()
    out["zone_up"] = zone_up
    out["zone_dn"] = zone_dn

    out["exh_up"] = (WEIGHTS["dist"] * ds_up + WEIGHTS["consec"] * (consec_up / CONSEC_SCALE).clip(upper=1.0)
                     + WEIGHTS["bb"] * bb_score + WEIGHTS["time"] * (zone_up / ZONE_SCALE).clip(upper=1.0))
    out["exh_dn"] = (WEIGHTS["dist"] * ds_dn + WEIGHTS["consec"] * (consec_dn / CONSEC_SCALE).clip(upper=1.0)
                     + WEIGHTS["bb"] * bb_score + WEIGHTS["time"] * (zone_dn / ZONE_SCALE).clip(upper=1.0))

    position = np.arange(len(out))
    out["valid"] = (position >= MIN_BARS - 1) & out["bb_mid"].notna() & out["atr"].notna() & out["ema_slow"].notna()
    out["candidate"] = out["valid"] & (out["pat_bull"] | out["pat_bear"])
    return out


_ARRAY_COLUMNS = (
    "open", "high", "low", "close", "volume", "ema21", "ema_fast", "ema_mid", "ema_slow", "bb_upper", "bb_lower",
    "atr", "vol_ratio", "pat_bear", "pat_bull", "fase", "trend_long", "trend_short", "touch_up_band", "touch_dn_band",
    "res_touch", "sup_touch", "sup_rebound", "roja_touch", "fib_hi", "fib_lo", "fib_up", "fib_sup", "fib_res",
    "fib_sup_lvl", "fib_res_lvl", "zone_low", "zone_high", "ema_spread", "below_cluster", "above_cluster", "new_low", "new_high", "stretch_up",
    "stretch_dn", "exh_up", "exh_dn", "valid", "candidate",
)


class Frame:
    """Velas con todos los indicadores + accesos rápidos (numpy) para evaluar vela a vela."""

    def __init__(self, df: pd.DataFrame, p: StrategyParams):
        self.df = df
        self.p = p
        self.index = df.index
        self.a: Dict[str, np.ndarray] = {col: df[col].to_numpy() for col in _ARRAY_COLUMNS}
        self.pat_name = df["pat_name"].to_numpy()

    def __len__(self) -> int:
        return len(self.df)


def build_features(df: pd.DataFrame, p: Optional[StrategyParams] = None) -> pd.DataFrame:
    return add_signal_features(add_indicators(df), p or StrategyParams())


def build_frame(df: pd.DataFrame, p: StrategyParams) -> Frame:
    return Frame(build_features(df, p), p)


def align_htf(entry_index: pd.DatetimeIndex, htf_feat: pd.DataFrame, entry_tf: str, htf_tf: str,
              column: str = "fase") -> pd.Series:
    """Valor de la última vela de la temporalidad mayor ya CERRADA al cierre de cada vela de entrada."""
    values = htf_feat[column].where(htf_feat["valid"], 0)
    closes = htf_feat.index + tf_delta(htf_tf)
    series = pd.Series(values.to_numpy(), index=closes).sort_index()
    series = series[~series.index.duplicated(keep="last")]
    entry_close = entry_index + tf_delta(entry_tf)
    aligned = series.reindex(entry_close, method="ffill").fillna(0)
    return pd.Series(aligned.to_numpy(), index=entry_index)


# ---------------------------------------------------------------------------
# Reglas de cada flecha
# ---------------------------------------------------------------------------
def _fmt(value: float) -> str:
    return f"{value:.6g}"


def _touched(frame: Frame, i: int, side: str) -> List[str]:
    """Nombres de los soportes (LONG) o resistencias (SHORT) que tocó la vela i."""
    a, p = frame.a, frame.p
    h, l, c = a["high"][i], a["low"][i], a["close"][i]
    levels = [("EMA21", a["ema21"][i]), ("EMA55", a["ema_fast"][i]), ("EMA144", a["ema_mid"][i]),
              ("EMA233", a["ema_slow"][i])]
    names = []
    if side == LONG:
        for name, level in levels:
            if l <= level * (1 + p.touch_tol) and c >= level:
                names.append(name)
        if a["touch_dn_band"][i]:
            names.append("banda inferior")
        if a["fib_sup"][i]:
            names.append(f"Fibonacci {a['fib_sup_lvl'][i] * 100:.1f}%")
    else:
        for name, level in levels:
            if h >= level * (1 - p.touch_tol) and c <= level:
                names.append(name)
        if a["touch_up_band"][i]:
            names.append("banda superior")
        if a["fib_res"][i]:
            names.append(f"Fibonacci {a['fib_res_lvl'][i] * 100:.1f}%")
    return names


def _roja(frame: Frame, i: int) -> Optional[_Draft]:
    a, p = frame.a, frame.p
    if not a["roja_touch"][i]:              # banda superior o retroceso de Fibonacci
        return None
    j0 = max(0, i - p.swing_bars)
    if i - 1 < j0:
        return None
    k = j0 + int(np.argmax(a["high"][j0:i]))
    if i - k < p.min_gap:
        return None
    ref = float(a["high"][k])
    if not a["high"][i] < ref:
        return None
    if not (a["touch_up_band"][k] and a["stretch_up"][k] >= p.climax_min_stretch
            and a["exh_up"][k] >= p.climax_min_exhaustion):
        return None
    if i <= k + 1:
        return None
    pullback_low = float(a["low"][k + 1:i].min())
    atr_k = float(a["atr"][k]) if np.isfinite(a["atr"][k]) else 0.0
    if ref - pullback_low < max(p.retrace_min_pct * ref, p.retrace_min_atr * atr_k):
        return None
    if not np.any(a["close"][k + 1:i] < a["ema_fast"][k + 1:i]):   # los vendedores cerraron velas bajo la EMA55
        return None
    reasons = [
        f"Techo previo {_fmt(ref)} hace {i - k} velas ({a['stretch_up'][k] * 100:+.1f}% sobre la EMA55, "
        f"agotamiento {a['exh_up'][k]:.2f}, tocó la banda superior)",
        f"Retroceso hasta {_fmt(pullback_low)} ({(pullback_low / ref - 1) * 100:.1f}%) con cierres bajo la EMA55",
        f"Máximo más bajo {_fmt(a['high'][i])} ({(a['high'][i] / ref - 1) * 100:+.1f}% frente al techo) tocando "
        f"{', '.join(_touched(frame, i, SHORT)) or 'resistencia'}",
    ]
    return _Draft(ROJA, "tras_techo", SHORT, float(a["exh_up"][k]), ref_idx=k, reasons=reasons)


EMA_COLORS = (("morada", "ema_fast"), ("azul", "ema_mid"), ("amarilla", "ema_slow"))


def _desinteres(frame: Frame, i: int) -> Optional[_Draft]:
    """Venta: bajo la morada y la azul, se acerca por debajo a una EMA y no logra cerrar encima."""
    a, p = frame.a, frame.p
    c, h = a["close"][i], a["high"][i]
    if not (c < a["ema_fast"][i] and c < a["ema_mid"][i]) or a["ema_spread"][i] < p.ema_min_spread:
        return None
    touched = [name for name, col in EMA_COLORS if h >= a[col][i] * (1 - p.touch_tol) and c < a[col][i]]
    if not touched:
        return None
    reasons = [f"Precio bajo la morada (EMA55) y la azul (EMA144): {_fmt(c)}",
               f"Se acercó a la {' y la '.join(touched)} por debajo y no logró cerrar encima: no aguantó"]
    return _Draft(DESINTERES, touched[0], SHORT, float(a["trend_short"][i]), reasons=reasons, zone_exit=True)


def _interes(frame: Frame, i: int) -> Optional[_Draft]:
    """Compra: sobre la morada y la azul, retrocede a una EMA y se sostiene."""
    a, p = frame.a, frame.p
    c, l = a["close"][i], a["low"][i]
    if not (c > a["ema_fast"][i] and c > a["ema_mid"][i]) or a["ema_spread"][i] < p.ema_min_spread:
        return None
    touched = [name for name, col in EMA_COLORS if l <= a[col][i] * (1 + p.touch_tol) and c > a[col][i]]
    if not touched:
        return None
    reasons = [f"Precio sobre la morada (EMA55) y la azul (EMA144): {_fmt(c)}",
               f"Retrocedió a la {' y la '.join(touched)} y se sostuvo: el interés comprador sigue"]
    return _Draft(INTERES, touched[0], LONG, float(a["trend_long"][i]), reasons=reasons, zone_exit=True)


def _zone_target(frame: Frame, i: int, side: str, entry: float, stop: float) -> Optional[Tuple[float, int]]:
    """Zona de salida: suelo previo (ventas) o techo previo (compras) más cercano a ≥ MIN_TARGET_R."""
    a, p = frame.a, frame.p
    risk = abs(stop - entry)
    j0, j1 = max(0, i - p.zone_bars), i - p.zone_pivot            # solo extremos ya confirmados
    if j1 <= j0 or risk <= 0:
        return None
    if side == SHORT:
        limit = entry - p.min_target_r * risk
        idx = np.nonzero(a["zone_low"][j0:j1] & (a["low"][j0:j1] <= limit))[0]
        if len(idx):
            k = j0 + idx[int(np.argmax(a["low"][j0 + idx]))]          # el más cercano por debajo
            return float(a["low"][k]), k
    else:
        limit = entry + p.min_target_r * risk
        idx = np.nonzero(a["zone_high"][j0:j1] & (a["high"][j0:j1] >= limit))[0]
        if len(idx):
            k = j0 + idx[int(np.argmin(a["high"][j0 + idx]))]
            return float(a["high"][k]), k
    return None


def _leg_from_opposite_band(frame: Frame, i: int, side: str) -> Optional[int]:
    """Velas desde que el precio tocó la banda contraria (la subida/bajada fue un tramo completo)."""
    a, p = frame.a, frame.p
    j0 = max(0, i - p.leg_bars)
    if i - 1 < j0:
        return None
    touches = a["touch_dn_band"][j0:i] if side == SHORT else a["touch_up_band"][j0:i]
    hits = np.nonzero(touches)[0]
    return int(i - (j0 + hits[-1])) if len(hits) else None


def _amarilla(frame: Frame, i: int) -> Optional[_Draft]:
    a, p = frame.a, frame.p
    if not a["res_touch"][i]:
        return None
    j0 = max(0, i - p.swing_bars)
    if i - 1 < j0:
        return None
    recent_max = float(a["high"][j0:i].max())
    if not a["high"][i] < recent_max:
        return None
    leg = _leg_from_opposite_band(frame, i, SHORT)
    if leg is None:
        return None
    reasons = [
        f"Subida desde la banda inferior (hace {leg} velas) hasta resistencia: {', '.join(_touched(frame, i, SHORT)) or 'banda/EMA'}",
        f"Máximo más bajo que el de las últimas {p.swing_bars} velas ({_fmt(recent_max)})",
    ]
    return _Draft(AMARILLA, "retroceso", SHORT, float(a["trend_short"][i]), reasons=reasons)


def _azul_tras_verde(frame: Frame, i: int) -> Optional[_Draft]:
    a, p = frame.a, frame.p
    if not a["sup_rebound"][i]:
        return None
    j0 = max(0, i - p.swing_bars)
    if i - 1 < j0:
        return None
    k = j0 + int(np.argmin(a["low"][j0:i]))
    if i - k < p.min_gap:
        return None
    ref = float(a["low"][k])
    if not a["low"][i] > ref:
        return None
    if not (a["touch_dn_band"][k] and a["stretch_dn"][k] >= p.climax_min_stretch
            and a["exh_dn"][k] >= p.climax_min_exhaustion):
        return None
    if i <= k + 1:
        return None
    bounce_high = float(a["high"][k + 1:i].max())
    atr_k = float(a["atr"][k]) if np.isfinite(a["atr"][k]) else 0.0
    if bounce_high - ref < max(p.retrace_min_pct * ref, p.retrace_min_atr * atr_k):
        return None
    if not np.any(a["close"][k + 1:i] > a["ema_fast"][k + 1:i]):  # los compradores cerraron velas sobre la EMA55
        return None
    reasons = [
        f"Suelo previo {_fmt(ref)} hace {i - k} velas ({-a['stretch_dn'][k] * 100:.1f}% bajo la EMA55, "
        f"agotamiento {a['exh_dn'][k]:.2f}, tocó la banda inferior)",
        f"Rebote hasta {_fmt(bounce_high)} ({(bounce_high / ref - 1) * 100:+.1f}%) con cierres sobre la EMA55",
        f"Mínimo más alto {_fmt(a['low'][i])} ({(a['low'][i] / ref - 1) * 100:+.1f}%) tocando {', '.join(_touched(frame, i, LONG)) or 'soporte'}",
    ]
    return _Draft(AZUL, "tras_verde", LONG, float(a["exh_dn"][k]), ref_idx=k, reasons=reasons)


def _azul_retroceso(frame: Frame, i: int) -> Optional[_Draft]:
    a = frame.a
    if not a["sup_touch"][i]:
        return None
    leg = _leg_from_opposite_band(frame, i, LONG)
    if leg is None:
        return None
    reasons = [f"Retroceso desde la banda superior (hace {leg} velas) hasta soporte: "
               f"{', '.join(_touched(frame, i, LONG)) or 'banda/EMA'}"]
    return _Draft(AZUL, "retroceso", LONG, float(a["trend_long"][i]), reasons=reasons)


def _is_green(frame: Frame, i: int) -> bool:
    a, p = frame.a, frame.p
    return bool(a["fase"][i] <= 0 and a["new_low"][i] and a["below_cluster"][i] and a["touch_dn_band"][i]
                and a["exh_dn"][i] >= p.event_min_exhaustion)


def _is_top(frame: Frame, i: int) -> bool:
    a, p = frame.a, frame.p
    return bool(a["fase"][i] >= 0 and a["new_high"][i] and a["above_cluster"][i] and a["touch_up_band"][i]
                and a["exh_up"][i] >= p.event_min_exhaustion)


def _event(frame: Frame, i: int, kind: str, symbol: str) -> MarketEvent:
    a, p = frame.a, frame.p
    open_time = frame.index[i]
    if kind == VERDE:
        price, exh = float(a["low"][i]), float(a["exh_dn"][i])
        reasons = [f"Mínimo de las últimas {p.new_extreme_bars} velas bajo las EMAs 55/144/233",
                   f"Tocó la banda inferior ({-a['stretch_dn'][i] * 100:.1f}% bajo la EMA55)",
                   f"Vela: {frame.pat_name[i]}"]
    else:
        price, exh = float(a["high"][i]), float(a["exh_up"][i])
        reasons = [f"Máximo de las últimas {p.new_extreme_bars} velas sobre las EMAs 55/144/233",
                   f"Tocó la banda superior ({a['stretch_up'][i] * 100:+.1f}% sobre la EMA55)",
                   f"Vela: {frame.pat_name[i]}"]
    return MarketEvent(kind=kind, symbol=symbol, timeframe=p.timeframe, candle_open_time=open_time,
                       candle_close_time=open_time + tf_delta(p.timeframe), price=price,
                       close=float(a["close"][i]), exhaustion=round(exh, 4), reasons=reasons)


def _stop(frame: Frame, i: int, side: str) -> float:
    a, p = frame.a, frame.p
    k0 = max(0, i - p.stop_lookback + 1)
    atr_i = float(a["atr"][i]) if np.isfinite(a["atr"][i]) else 0.0
    if side == SHORT:
        extreme = float(a["high"][k0:i + 1].max())
        return extreme + max(p.stop_buffer * extreme, p.stop_atr * atr_i)
    extreme = float(a["low"][k0:i + 1].min())
    return extreme - max(p.stop_buffer * extreme, p.stop_atr * atr_i)


def _finish(frame: Frame, i: int, draft: _Draft, *, symbol: str, analyzer: RejectionAnalyzer,
            trend_timeframe: str, htf_fase: Optional[int], log_rejection: bool,
            result: BarResult) -> Optional[Signal]:
    a, p = frame.a, frame.p
    enabled = draft.setup in p.setups or (draft.setup == VERDE and p.entry_on_green) \
        or (draft.setup == TOPE and p.entry_on_top)
    if not enabled:
        result.discarded.append(Discarded(draft.setup, draft.variant, draft.side, "flecha desactivada en SETUPS"))
        return None
    reversal = draft.setup in (ROJA, VERDE, TOPE) or draft.variant == "tras_verde"
    if p.htf_filter == "tendencia" and not reversal and htf_fase is not None:
        wanted = 1 if draft.side == LONG else -1
        if htf_fase == -wanted:
            result.discarded.append(Discarded(draft.setup, draft.variant, draft.side,
                                              f"la fase de {trend_timeframe} va en contra"))
            return None

    window = frame.df.iloc[max(0, i - 29): i + 1]
    direction = "BULLISH" if draft.side == LONG else "BEARISH"
    rejection = analyzer.analyze(window, direction, symbol=symbol, log=log_rejection,
                                 context="reversion" if reversal else "continuacion")
    rechazo = float(rejection.get("score") or 0.0)
    contexto = float(min(max(draft.contexto, 0.0), 1.0))
    score = round(0.5 * contexto + 0.5 * rechazo, 4)
    if rechazo < p.rejection_min_score:
        result.discarded.append(Discarded(draft.setup, draft.variant, draft.side,
                                          f"rechazo débil ({rechazo:.2f} < {p.rejection_min_score:.2f})", score))
        return None
    if score < p.min_score:
        result.discarded.append(Discarded(draft.setup, draft.variant, draft.side,
                                          f"puntaje bajo ({score:.2f} < {p.min_score:.2f})", score))
        return None

    close = float(a["close"][i])
    stop = _stop(frame, i, draft.side)
    if (draft.side == SHORT and stop <= close) or (draft.side == LONG and stop >= close):
        result.discarded.append(Discarded(draft.setup, draft.variant, draft.side, "stop inválido", score))
        return None
    target = None
    if draft.zone_exit:
        zone = _zone_target(frame, i, draft.side, close, stop)
        if zone is None:
            result.discarded.append(Discarded(draft.setup, draft.variant, draft.side,
                                              f"sin zona a {p.min_target_r:g} veces el riesgo o más", score))
            return None
        target, k = zone
        kind = "suelo" if draft.side == SHORT else "techo"
        draft.reasons.append(f"Zona de salida: {kind} previo {_fmt(target)} (hace {i - k} velas), "
                             f"{abs(target - close) / abs(stop - close):.1f} veces el riesgo")

    open_time = frame.index[i]
    fase = int(a["fase"][i])
    components = {
        "contexto": round(contexto, 4),
        "rechazo": round(rechazo, 4),
        "fase": FASE_NAMES[fase],
        "fase_1h": FASE_NAMES.get(htf_fase, None) if htf_fase is not None else None,
        "vela": str(frame.pat_name[i]),
        "toques": _touched(frame, i, draft.side),
        "estirado_ema55": round(float(a["stretch_up"][i] if draft.side == SHORT else a["stretch_dn"][i]), 5),
        "vol_ratio": round(float(a["vol_ratio"][i]), 3) if np.isfinite(a["vol_ratio"][i]) else None,
        "atr_pct": round(float(a["atr"][i] / close), 5) if close else None,
    }
    if np.isfinite(a["fib_hi"][i]) and a["fib_up"][i] >= 0:
        lvl = a["fib_sup_lvl"][i] if draft.side == LONG else a["fib_res_lvl"][i]
        components["fibonacci"] = {"tramo": "subida" if a["fib_up"][i] == 1 else "bajada",
                                   "maximo": float(f"{a['fib_hi'][i]:.6g}"), "minimo": float(f"{a['fib_lo'][i]:.6g}"),
                                   "nivel_tocado": round(float(lvl), 3) if lvl else None}
    ref_price = ref_time = None
    if draft.ref_idx is not None:
        k = draft.ref_idx
        ref_price = float(a["high"][k] if draft.side == SHORT else a["low"][k])
        ref_time = frame.index[k]
        components["velas_desde_extremo"] = int(i - k)
    reasons = [f"Flecha {draft.setup}: {SETUP_NAMES[draft.setup]} ({VARIANT_NAMES.get(draft.variant, draft.variant)})"]
    reasons += draft.reasons
    reasons += [f"Vela de rechazo: {frame.pat_name[i]}", f"Fase EMA55/EMA144: {FASE_NAMES[fase]}",
                f"Contexto {contexto:.2f} · Rechazo {rechazo:.2f} · Puntaje {score:.2f}"]
    return Signal(
        symbol=symbol, side=draft.side, setup=draft.setup, variant=draft.variant, timeframe=p.timeframe,
        trend_timeframe=trend_timeframe, candle_open_time=open_time,
        candle_close_time=open_time + tf_delta(p.timeframe), entry_ref=close, stop_loss=stop,
        atr=float(a["atr"][i]) if np.isfinite(a["atr"][i]) else 0.0, score=score, contexto=round(contexto, 4),
        rechazo=round(rechazo, 4), components=components,
        rejection={"score": rejection.get("score"), "context": rejection.get("context"),
                   "contexts": rejection.get("contexts"), "contexts_met": rejection.get("contexts_met"),
                   "event_id": rejection.get("event_id")},
        reasons=reasons, ref_price=ref_price, ref_time=ref_time, target=target,
    )


def evaluate_bar(frame: Frame, i: int, *, symbol: str, analyzer: RejectionAnalyzer,
                 trend_timeframe: str = "1h", htf_fase: Optional[int] = None,
                 log_rejection: bool = False, state: Optional[ScanState] = None) -> BarResult:
    """Evalúa la vela i (ya cerrada): devuelve la entrada (si hay) y los avisos VERDE/TOPE.

    `state` recuerda las flechas anteriores de la moneda: tras una AZUL que confirma el suelo no se
    revende (amarilla) durante SWING_HOURS, y tras una ROJA que confirma el techo no se compran
    retrocesos (azul) durante SWING_HOURS. Es el ciclo roja → amarilla → verde → azul.
    """
    result = BarResult()
    n = len(frame)
    if i < 0:
        i += n
    if i < 0 or i >= n:
        return result
    a, p = frame.a, frame.p
    if not a["valid"][i]:
        return result
    bull, bear = bool(a["pat_bull"][i]), bool(a["pat_bear"][i])
    if not (bull or bear):
        return result
    fase = int(a["fase"][i])
    draft: Optional[_Draft] = None

    if bull:
        green = _is_green(frame, i)
        if green:
            result.events.append(_event(frame, i, VERDE, symbol))
        if INTERES in p.setups:
            draft = _interes(frame, i)
        if draft is None and fase <= 0 and AZUL in p.setups:
            draft = _azul_tras_verde(frame, i)
        if draft is None and green and p.entry_on_green:
            draft = _Draft(VERDE, "agresiva", LONG, float(a["exh_dn"][i]),
                           reasons=["Entrada agresiva en la flecha verde (ENTRY_ON_GREEN)"])
        if draft is None and not green and fase >= 0 and AZUL in p.setups:
            draft = _azul_retroceso(frame, i)
            if draft is not None and state is not None and state.last_short_reversal is not None \
                    and i - state.last_short_reversal <= p.swing_bars:
                result.discarded.append(Discarded(AZUL, "retroceso", LONG,
                                                  "hubo una ROJA (techo confirmado) hace poco: no se compran retrocesos"))
                draft = None
    else:
        top = _is_top(frame, i)
        if top:
            result.events.append(_event(frame, i, TOPE, symbol))
        if DESINTERES in p.setups:
            draft = _desinteres(frame, i)
        if draft is None and fase >= 0 and ROJA in p.setups:
            draft = _roja(frame, i)
        if draft is None and fase <= 0 and AMARILLA in p.setups:
            draft = _amarilla(frame, i)
            if draft is not None and state is not None and state.last_long_reversal is not None \
                    and i - state.last_long_reversal <= p.swing_bars:
                result.discarded.append(Discarded(AMARILLA, "retroceso", SHORT,
                                                  "hubo una AZUL tras la verde (suelo confirmado) hace poco: no se revende"))
                draft = None
        if draft is None and top and p.entry_on_top:
            draft = _Draft(TOPE, "agresiva", SHORT, float(a["exh_up"][i]),
                           reasons=["Entrada agresiva en el tope (ENTRY_ON_TOP)"])

    if draft is not None:
        result.entry = _finish(frame, i, draft, symbol=symbol, analyzer=analyzer, trend_timeframe=trend_timeframe,
                               htf_fase=htf_fase, log_rejection=log_rejection, result=result)
        if result.entry is not None and state is not None:
            state.register(i, result.entry)
    return result


def scan_history(frame: Frame, *, symbol: str, analyzer: RejectionAnalyzer, start: Optional[pd.Timestamp] = None,
                 end: Optional[pd.Timestamp] = None, trend_timeframe: str = "1h",
                 htf_fase: Optional[pd.Series] = None, keep_discarded: bool = False,
                 log_last: bool = False) -> Dict[int, BarResult]:
    """Evalúa todas las velas candidatas (backtest y gráfico). Devuelve {posición de la vela: resultado}."""
    a = frame.a
    positions = np.nonzero(a["candidate"])[0]
    if start is not None:
        positions = positions[frame.index[positions] >= start]
    if end is not None:
        positions = positions[frame.index[positions] <= end]
    htf_values = htf_fase.to_numpy() if htf_fase is not None else None
    state = ScanState()
    results: Dict[int, BarResult] = {}
    last = len(frame) - 1          # solo se registra el rechazo de la vela más reciente (no se repite cada ciclo)
    for i in positions:
        i = int(i)
        fase_1h = int(htf_values[i]) if htf_values is not None else None
        res = evaluate_bar(frame, i, symbol=symbol, analyzer=analyzer, trend_timeframe=trend_timeframe,
                           htf_fase=fase_1h, state=state, log_rejection=log_last and i == last)
        if res.entry is not None or res.events or (keep_discarded and res.discarded):
            results[i] = res
    return results


def evaluate_last_closed(entry_df: pd.DataFrame, *, symbol: str, p: StrategyParams, analyzer: RejectionAnalyzer,
                         htf_df: Optional[pd.DataFrame] = None, htf_timeframe: str = "1h",
                         log_rejection: bool = True) -> Tuple[BarResult, Frame]:
    """Uso en vivo: evalúa la última vela CERRADA."""
    frame = build_frame(entry_df, p)
    htf_series = None
    if htf_df is not None and htf_timeframe != p.timeframe and len(htf_df) >= MIN_BARS:
        htf_feat = build_features(htf_df, p)
        htf_series = align_htf(frame.index, htf_feat, p.timeframe, htf_timeframe)
    last = len(frame) - 1
    if last < 0:
        return BarResult(), frame
    # Se recorren las últimas SWING_HOURS con la misma función del backtest para recordar el ciclo
    start = frame.index[max(0, last - p.swing_bars - 1)]
    results = scan_history(frame, symbol=symbol, analyzer=analyzer, start=start, trend_timeframe=htf_timeframe,
                           htf_fase=htf_series, keep_discarded=True, log_last=log_rejection)
    return results.get(last, BarResult()), frame


def htf_context(htf_df: Optional[pd.DataFrame], p: StrategyParams) -> Dict[str, Any]:
    """Resumen de la temporalidad mayor (1h) para la IA y el panel."""
    if htf_df is None or len(htf_df) < MIN_BARS:
        return {}
    feat = build_features(htf_df, p)
    last = feat.iloc[-1]
    upper, lower = float(last["bb_upper"]), float(last["bb_lower"])
    position = (float(last["close"]) - lower) / (upper - lower) if upper > lower else 0.5
    return {
        "fase": FASE_NAMES[int(last["fase"])],
        "pila": STACK_NAMES[int(last["stack"])],
        "distancia_ema55_pct": round(float((last["close"] - last["ema_fast"]) / last["ema_fast"] * 100), 2),
        "posicion_bandas_pct": round(position * 100, 1),
    }
