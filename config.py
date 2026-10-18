"""
config.py — Configuración central de UNICO BOT (v9)
====================================================
Única fuente de verdad para todos los parámetros del bot.

- En Railway los valores llegan como variables de entorno (Variables del servicio).
- En local se leen del archivo .env (copia .env.example a .env).

Modos (BOT_MODE):
  DRY_RUN  → precios reales de Bybit, operaciones simuladas (sin dinero).
  DEMO     → precios reales, órdenes en la cuenta Demo de Bybit (dinero virtual).
  TESTNET  → todo en la red de pruebas de Bybit (precios de prueba).
  LIVE     → dinero real. Exige CONFIRM_LIVE=SI.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Dict, List, Tuple

try:  # python-dotenv es opcional: en Railway las variables ya vienen en el entorno
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

VALID_MODES = ("DRY_RUN", "DEMO", "TESTNET", "LIVE")
EXCHANGE_MODES = ("DEMO", "TESTNET", "LIVE")
VALID_SETUPS = ("DESINTERES", "INTERES", "ROJA", "AMARILLA", "AZUL")
DEFAULT_SETUPS = ("DESINTERES", "INTERES")
HTF_FILTERS = ("off", "tendencia")

TF_MINUTES: Dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720, "1d": 1440,
}

DEFAULT_LLM_MODELS = {
    "anthropic": "claude-haiku-5-5",
    "openrouter": "openrouter/free",
    "groq": "llama-3.3-70b-versatile",
}

_TRUE = {"1", "true", "si", "sí", "yes", "y", "on", "verdadero"}


def _raw(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value if value != "" else None


def _str(name: str, default: str = "") -> str:
    value = _raw(name)
    return default if value is None else value


def _float(name: str, default: float) -> float:
    value = _raw(name)
    if value is None:
        return float(default)
    try:
        return float(value.replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"La variable {name} debe ser un número (recibido: {value!r})") from exc


def _int(name: str, default: int) -> int:
    value = _raw(name)
    if value is None:
        return int(default)
    try:
        return int(float(value.replace(",", ".")))
    except ValueError as exc:
        raise ValueError(f"La variable {name} debe ser un entero (recibido: {value!r})") from exc


def _bool(name: str, default: bool) -> bool:
    value = _raw(name)
    if value is None:
        return default
    return value.lower() in _TRUE


def _list(name: str, default: Tuple[str, ...]) -> Tuple[str, ...]:
    value = _raw(name)
    if value is None:
        return tuple(default)
    return tuple(part.strip().upper().replace("É", "E") for part in value.split(",") if part.strip())


def _floats(name: str, default: Tuple[float, ...]) -> Tuple[float, ...]:
    value = _raw(name)
    if value is None:
        return tuple(default)
    try:
        return tuple(float(x.strip().replace("%", "")) / (100 if float(x.strip().replace("%", "")) > 1 else 1)
                     for x in value.split(";" if ";" in value else ",") if x.strip())
    except ValueError as exc:
        raise ValueError(f"La variable {name} debe ser una lista de números (recibido: {value!r})") from exc


def tf_ms(timeframe: str) -> int:
    return TF_MINUTES[timeframe] * 60_000


@dataclass(frozen=True)
class Config:
    # --- Modo y conexión -------------------------------------------------
    mode: str = "DRY_RUN"
    confirm_live: bool = False
    api_key: str = ""
    api_secret: str = ""
    proxy: str = ""

    # --- Archivos ----------------------------------------------------------
    db_path: str = "unico.db"
    data_dir: str = "data"

    # --- Temporalidades y ciclos -------------------------------------------
    trend_timeframe: str = "1h"
    entry_timeframe: str = "15m"
    candles_limit: int = 1000
    scan_interval: float = 60.0
    monitor_interval: float = 15.0
    watchlist_refresh_min: int = 15
    signal_max_age_min: int = 10

    # --- Universo (las que más suben) --------------------------------------
    top_n_24h: int = 15
    top_n_1h: int = 15
    watchlist_memory_hours: int = 96
    min_turnover_24h: float = 1_000_000.0
    excluded_prefixes: Tuple[str, ...] = ("1000", "100")

    # --- Estrategia: las 4 flechas (ver strategy.py) ---------------------------
    min_score: float = 0.40
    llm_auto_approve_score: float = 1.0
    rejection_min_score: float = 0.40
    setups: Tuple[str, ...] = DEFAULT_SETUPS
    entry_on_green: bool = False
    entry_on_top: bool = False
    exit_on_opposite: bool = True
    htf_filter: str = "off"
    swing_hours: float = 24.0
    new_extreme_hours: float = 12.0
    pivot_hours: float = 2.0
    leg_hours: float = 6.0
    min_range_atr: float = 0.8
    event_min_exhaustion: float = 0.35
    climax_min_exhaustion: float = 0.40
    climax_min_stretch: float = 0.02
    retrace_min_pct: float = 0.015
    retrace_min_atr: float = 1.0
    band_tol: float = 0.003
    touch_tol: float = 0.005
    phase_tol: float = 0.0025
    wick_body_ratio: float = 1.2
    wick_range_ratio: float = 0.40
    engulf_body: float = 0.5
    stop_lookback: int = 3
    stop_buffer: float = 0.003
    stop_atr: float = 1.0
    fib_hours: float = 96.0
    fib_levels: Tuple[float, ...] = ()          # vacío = Fibonacci desactivado
    fib_min_range_atr: float = 5.0
    zone_hours: float = 96.0
    zone_pivot_hours: float = 2.0
    min_target_r: float = 1.5
    ema_min_spread: float = 0.004
    exit_at_zone: bool = True
    pattern_min_match: int = 5
    setup_min_samples: int = 20
    setup_min_hit_rate: float = 0.0

    # --- Riesgo (lo que pidió Karla: 15% de margen, SL = 40% del margen) ----
    margin_pct: float = 0.15
    max_loss_pct_of_margin: float = 0.40
    leverage_cap: int = 20
    min_leverage: int = 3
    st_reduction: float = 0.50
    max_positions: int = 4
    cooldown_minutes: int = 60
    max_entries_daily: int = 5
    daily_dd_limit: float = 15.0
    weekly_dd_limit: float = 25.0

    # --- Salidas -------------------------------------------------------------
    tp1_r: float = 2.0
    tp1_pct: float = 0.25
    tp2_r: float = 5.0
    tp2_pct: float = 0.25
    be_r: float = 3.0
    be_offset_pct: float = 0.0015
    trail_atr_mult: float = 2.5
    min_sl_pct: float = 0.001
    max_fee_to_risk: float = 0.20

    # --- Costos de futuros perpetuos (Bybit, cuenta estándar) ----------------
    taker_fee: float = 0.00055
    maker_fee: float = 0.0002
    slippage_pct: float = 0.0005

    # --- Simulación ----------------------------------------------------------
    paper_balance: float = 10_000.0
    backtest_exchange: str = "bybit"

    # --- Filtro LLM ------------------------------------------------------------
    llm_provider: str = "none"
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout: float = 20.0
    llm_on_error: str = "allow"
    llm_min_confidence: float = 0.0

    # --- Panel web -------------------------------------------------------------
    dashboard_enabled: bool = True
    port: int = 8080
    dashboard_user: str = "karla"
    dashboard_password: str = ""

    log_level: str = "INFO"

    # ------------------------------------------------------------------------
    @classmethod
    def from_env(cls) -> "Config":
        provider = _str("LLM_PROVIDER", "auto").lower()
        keys = {
            "anthropic": _str("ANTHROPIC_API_KEY"),
            "openrouter": _str("OPENROUTER_API_KEY"),
            "groq": _str("GROQ_API_KEY"),
        }
        if provider == "auto":
            provider = next((name for name, key in keys.items() if key), "none")
        if provider not in ("none", *keys.keys()):
            raise ValueError(f"LLM_PROVIDER desconocido: {provider}")
        model = _str("LLM_MODEL", "")
        if not model and provider == "groq":
            model = _str("GROQ_MODEL", "")
        if not model:
            model = DEFAULT_LLM_MODELS.get(provider, "")

        return cls(
            mode=_str("BOT_MODE", "DRY_RUN").upper(),
            confirm_live=_str("CONFIRM_LIVE", "").upper() in {"SI", "SÍ", "YES"},
            api_key=_str("BYBIT_API_KEY"),
            api_secret=_str("BYBIT_API_SECRET"),
            proxy=_str("BYBIT_PROXY"),
            db_path=_str("DB_PATH", "unico.db"),
            data_dir=_str("DATA_DIR", "data"),
            trend_timeframe=_str("TREND_TIMEFRAME", "1h").lower(),
            entry_timeframe=_str("ENTRY_TIMEFRAME", "15m").lower(),
            candles_limit=_int("CANDLES_LIMIT", 1000),
            scan_interval=_float("SCAN_INTERVAL", 60.0),
            monitor_interval=_float("MONITOR_INTERVAL", 15.0),
            watchlist_refresh_min=_int("WATCHLIST_REFRESH_MIN", 15),
            signal_max_age_min=_int("SIGNAL_MAX_AGE_MIN", 10),
            top_n_24h=_int("TOP_N_24H", 15),
            top_n_1h=_int("TOP_N_1H", 15),
            watchlist_memory_hours=_int("WATCHLIST_MEMORY_HOURS", 96),
            min_turnover_24h=_float("MIN_TURNOVER_24H", 1_000_000.0),
            excluded_prefixes=_list("EXCLUDED_PREFIXES", ("1000", "100")),
            min_score=_float("MIN_SCORE", 0.40),
            llm_auto_approve_score=_float("LLM_AUTO_APPROVE_SCORE", 1.0),
            rejection_min_score=_float("REJECTION_MIN_SCORE", 0.40),
            setups=_list("SETUPS", DEFAULT_SETUPS),
            entry_on_green=_bool("ENTRY_ON_GREEN", False),
            entry_on_top=_bool("ENTRY_ON_TOP", False),
            exit_on_opposite=_bool("EXIT_ON_OPPOSITE", True),
            htf_filter=_str("FILTRO_1H", "off").lower(),
            swing_hours=_float("SWING_HOURS", 24.0),
            new_extreme_hours=_float("NEW_EXTREME_HOURS", 12.0),
            pivot_hours=_float("PIVOT_HOURS", 2.0),
            leg_hours=_float("LEG_HOURS", 6.0),
            min_range_atr=_float("MIN_RANGE_ATR", 0.8),
            event_min_exhaustion=_float("EVENT_MIN_EXHAUSTION", 0.35),
            climax_min_exhaustion=_float("CLIMAX_MIN_EXHAUSTION", 0.40),
            climax_min_stretch=_float("CLIMAX_MIN_STRETCH", 0.02),
            retrace_min_pct=_float("RETRACE_MIN_PCT", 0.015),
            retrace_min_atr=_float("RETRACE_MIN_ATR", 1.0),
            band_tol=_float("BAND_TOL", 0.003),
            touch_tol=_float("TOUCH_TOL", 0.005),
            phase_tol=_float("PHASE_TOL", 0.0025),
            wick_body_ratio=_float("WICK_BODY_RATIO", 1.2),
            wick_range_ratio=_float("WICK_RANGE_RATIO", 0.40),
            engulf_body=_float("ENGULF_BODY", 0.5),
            stop_lookback=_int("STOP_LOOKBACK", 3),
            stop_buffer=_float("STOP_BUFFER", 0.003),
            stop_atr=_float("STOP_ATR", 1.0),
            fib_hours=_float("FIB_HOURS", 96.0),
            fib_levels=_floats("FIB_LEVELS", ()),
            fib_min_range_atr=_float("FIB_MIN_RANGE_ATR", 5.0),
            zone_hours=_float("ZONE_HOURS", 96.0),
            zone_pivot_hours=_float("ZONE_PIVOT_HOURS", 2.0),
            min_target_r=_float("MIN_TARGET_R", 1.5),
            ema_min_spread=_float("EMA_MIN_SPREAD", 0.004),
            exit_at_zone=_bool("EXIT_AT_ZONE", True),
            pattern_min_match=_int("PATTERN_MIN_MATCH", 5),
            setup_min_samples=_int("SETUP_MIN_SAMPLES", 20),
            setup_min_hit_rate=_float("SETUP_MIN_HIT_RATE", 0.0),
            margin_pct=_float("MARGIN_PCT", _float("POSITION_PCT", 0.15)),
            max_loss_pct_of_margin=_float("MAX_LOSS_PCT_OF_MARGIN", _float("SL_PCT", 0.40)),
            leverage_cap=_int("LEVERAGE_CAP", _int("LEVERAGE", 20)),
            min_leverage=_int("MIN_LEVERAGE", 3),
            st_reduction=_float("ST_REDUCTION", 0.50),
            max_positions=_int("MAX_POSITIONS", 4),
            cooldown_minutes=_int("COOLDOWN_MINUTES", 60),
            max_entries_daily=_int("MAX_ENTRIES_DAILY", 5),
            daily_dd_limit=_float("DAILY_DD_LIMIT", 15.0),
            weekly_dd_limit=_float("WEEKLY_DD_LIMIT", 25.0),
            tp1_r=_float("TP1_R", 2.0),
            tp1_pct=_float("TP1_PCT", 0.25),
            tp2_r=_float("TP2_R", _float("TP_MULTIPLE", 5.0)),
            tp2_pct=_float("TP2_PCT", 0.25),
            be_r=_float("BE_R", 3.0),
            be_offset_pct=_float("BE_OFFSET_PCT", 0.0015),
            trail_atr_mult=_float("TRAIL_ATR_MULT", 2.5),
            min_sl_pct=_float("MIN_SL_PCT", 0.001),
            max_fee_to_risk=_float("MAX_FEE_TO_RISK", 0.20),
            taker_fee=_float("TAKER_FEE", 0.00055),
            maker_fee=_float("MAKER_FEE", 0.0002),
            slippage_pct=_float("SLIPPAGE_PCT", 0.0005),
            paper_balance=_float("PAPER_BALANCE", 10_000.0),
            backtest_exchange=_str("BACKTEST_EXCHANGE", "bybit").lower(),
            llm_provider=provider,
            llm_model=model,
            llm_api_key=keys.get(provider, ""),
            llm_timeout=_float("LLM_TIMEOUT", 20.0),
            llm_on_error=_str("LLM_ON_ERROR", "allow").lower(),
            llm_min_confidence=_float("LLM_MIN_CONFIDENCE", 0.0),
            dashboard_enabled=_bool("DASHBOARD_ENABLED", True),
            port=_int("PORT", 8080),
            dashboard_user=_str("DASHBOARD_USER", "karla"),
            dashboard_password=_str("DASHBOARD_PASSWORD", ""),
            log_level=_str("LOG_LEVEL", "INFO").upper(),
        )

    # ------------------------------------------------------------------------
    @property
    def uses_exchange(self) -> bool:
        return self.mode in EXCHANGE_MODES

    @property
    def entry_tf_minutes(self) -> int:
        return TF_MINUTES[self.entry_timeframe]

    @property
    def max_loss_pct_of_balance(self) -> float:
        return self.margin_pct * self.max_loss_pct_of_margin

    def validate(self) -> List[str]:
        """Devuelve la lista de errores. Lista vacía = configuración válida."""
        errors: List[str] = []
        if self.mode not in VALID_MODES:
            errors.append(f"BOT_MODE debe ser uno de {', '.join(VALID_MODES)} (recibido: {self.mode}).")
        if self.mode == "LIVE" and not self.confirm_live:
            errors.append("Para operar con dinero real pon CONFIRM_LIVE=SI (protección contra activaciones por error).")
        if self.uses_exchange and (not self.api_key or not self.api_secret):
            errors.append(f"El modo {self.mode} necesita BYBIT_API_KEY y BYBIT_API_SECRET.")
        for name, tf in (("TREND_TIMEFRAME", self.trend_timeframe), ("ENTRY_TIMEFRAME", self.entry_timeframe)):
            if tf not in TF_MINUTES:
                errors.append(f"{name}={tf} no es válido. Usa uno de {', '.join(TF_MINUTES)}.")
        if self.trend_timeframe in TF_MINUTES and self.entry_timeframe in TF_MINUTES:
            if TF_MINUTES[self.trend_timeframe] < TF_MINUTES[self.entry_timeframe]:
                errors.append("TREND_TIMEFRAME no puede ser menor que ENTRY_TIMEFRAME.")
        if not 0 < self.margin_pct <= 1:
            errors.append("MARGIN_PCT debe estar entre 0 y 1 (0.15 = 15%).")
        if not 0 < self.max_loss_pct_of_margin <= 1:
            errors.append("MAX_LOSS_PCT_OF_MARGIN debe estar entre 0 y 1 (0.40 = 40%).")
        if not 1 <= self.min_leverage <= self.leverage_cap:
            errors.append("MIN_LEVERAGE debe ser >= 1 y <= LEVERAGE_CAP.")
        if self.max_positions < 1:
            errors.append("MAX_POSITIONS debe ser al menos 1.")
        if self.tp1_pct < 0 or self.tp2_pct < 0 or self.tp1_pct + self.tp2_pct > 1:
            errors.append("TP1_PCT + TP2_PCT no puede superar 1 (100% de la posición).")
        if self.tp1_r <= 0 or self.tp2_r <= 0:
            errors.append("TP1_R y TP2_R deben ser mayores que 0.")
        if self.llm_on_error not in ("allow", "block"):
            errors.append("LLM_ON_ERROR debe ser 'allow' (seguir con las reglas) o 'block' (no operar).")
        if self.llm_provider != "none" and not self.llm_api_key:
            errors.append(f"LLM_PROVIDER={self.llm_provider} necesita su clave de API.")
        unknown = [s for s in self.setups if s not in VALID_SETUPS]
        if unknown:
            errors.append(f"SETUPS tiene nombres desconocidos: {', '.join(unknown)}. Usa {', '.join(VALID_SETUPS)}.")
        if self.htf_filter not in HTF_FILTERS:
            errors.append(f"FILTRO_1H debe ser {' o '.join(HTF_FILTERS)}.")
        if self.swing_hours <= self.new_extreme_hours:
            errors.append("SWING_HOURS debe ser mayor que NEW_EXTREME_HOURS.")
        if self.backtest_exchange not in ("bybit", "binance"):
            errors.append("BACKTEST_EXCHANGE debe ser bybit o binance.")
        if any(not 0 < r < 1 for r in self.fib_levels):
            errors.append("FIB_LEVELS van entre 0 y 1 (ej.: 0.382,0.5,0.618) o en % (38.2,50,61.8).")
        if not 0 <= self.min_score <= 1 or not 0 <= self.llm_auto_approve_score <= 1:
            errors.append("MIN_SCORE y LLM_AUTO_APPROVE_SCORE van de 0 a 1.")
        return errors

    def public_dict(self) -> Dict[str, object]:
        """Parámetros sin secretos, para mostrarlos en el panel."""
        data = asdict(self)
        for secret in ("api_key", "api_secret", "llm_api_key", "dashboard_password", "proxy"):
            data[secret] = "***" if data.get(secret) else ""
        data["excluded_prefixes"] = list(self.excluded_prefixes)
        data["setups"] = list(self.setups)
        data["fib_levels"] = list(self.fib_levels)
        data["max_loss_pct_of_balance"] = round(self.max_loss_pct_of_balance, 4)
        return data
