"""
strategy.py — Las flechas de Apollo convertidas en reglas (v9)
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
    pat_bear = ((pin_bear | eng_bear) & big_enough & pivot_high).fillna(False)
