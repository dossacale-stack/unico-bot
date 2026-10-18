"""
historical_levels.py — Máximos y mínimos desde que la moneda empezó a cotizar en Bybit
=====================================================================================
Pedido de Karla: que el bot "entre a los activos y haga un análisis de máximos y mínimos
desde que comenzó a cotizar en la plataforma".

Qué calcula (con velas diarias desde el listado, launchTime de Bybit):
  - ATH / ATL: máximo y mínimo histórico en Bybit, con su fecha
  - Días desde el listado
  - Posición del precio dentro del rango histórico (0% = en el mínimo, 100% = en el máximo)
  - Distancia porcentual al ATH y al ATL

Uso actual: contexto para el filtro LLM, el panel y el registro de cada operación.
Todavía NO bloquea entradas: la regla concreta (por ejemplo, no comprar cerca del ATH)
se definirá con las imágenes de la estrategia. Así no se inventa nada.

En el backtest el cálculo es "punto en el tiempo": solo usa velas anteriores al momento evaluado.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import Dict, Optional

import pandas as pd

logger = logging.getLogger("HistoricalLevels")


@dataclass
class HistLevels:
    ath: float
    atl: float
    ath_date: str
    atl_date: str
    listing_date: str
    days_listed: int
    price: float
    range_position_pct: float     # 0 = en el mínimo histórico, 100 = en el máximo
    pct_from_ath: float           # negativo = por debajo del máximo
    pct_from_atl: float           # positivo = por encima del mínimo

    def to_dict(self) -> Dict:
        return asdict(self)

    def describe(self) -> str:
        return (
            f"rango histórico {self.range_position_pct:.0f}% | {self.pct_from_ath:+.1f}% del ATH "
            f"| {self.pct_from_atl:+.1f}% del ATL | {self.days_listed} días cotizando"
        )


def levels_from_daily(daily: pd.DataFrame, price: float, now: Optional[pd.Timestamp] = None) -> Optional[HistLevels]:
    """daily: velas diarias (índice = apertura UTC) desde el listado hasta ahora."""
    if daily is None or daily.empty or price <= 0:
        return None
    highs, lows = daily["high"], daily["low"]
    ath, atl = float(highs.max()), float(lows.min())
    ath = max(ath, price)
    atl = min(atl, price)
    first = daily.index[0]
    now = now or pd.Timestamp.now(tz="UTC")
    span = ath - atl
    position = 100.0 * (price - atl) / span if span > 0 else 50.0
    return HistLevels(
        ath=ath,
        atl=atl,
        ath_date=str(highs.idxmax().date()),
        atl_date=str(lows.idxmin().date()),
        listing_date=str(first.date()),
        days_listed=int((now - first).days),
        price=float(price),
        range_position_pct=round(position, 2),
        pct_from_ath=round((price / ath - 1) * 100, 2) if ath > 0 else 0.0,
        pct_from_atl=round((price / atl - 1) * 100, 2) if atl > 0 else 0.0,
    )


def levels_point_in_time(daily: pd.DataFrame, intraday: pd.DataFrame, ts: pd.Timestamp,
                         price: float) -> Optional[HistLevels]:
    """Para el backtest: días completos anteriores a ts + lo que va del día con velas intradía."""
    day_start = ts.normalize()
    past_days = daily[daily.index < day_start]
    today = intraday[(intraday.index >= day_start) & (intraday.index < ts)]
    frames = [f[["high", "low"]] for f in (past_days, today) if not f.empty]
    if not frames:
        return None
    combined = pd.concat(frames)
    return levels_from_daily(combined, price, now=ts)


class HistoricalLevels:
    """Calcula y guarda en caché (un día) los niveles históricos de cada moneda."""

    def __init__(self, api, refresh_seconds: int = 6 * 3600):
        self.api = api
        self.refresh_seconds = refresh_seconds
        self._daily: Dict[str, pd.DataFrame] = {}
        self._fetched_at: Dict[str, float] = {}

    async def _daily_candles(self, symbol: str) -> Optional[pd.DataFrame]:
        cached = self._daily.get(symbol)
        if cached is not None and time.time() - self._fetched_at.get(symbol, 0) < self.refresh_seconds:
            return cached
        launch_ms = self.api.listing_time_ms(symbol)
        since = launch_ms if launch_ms else int((time.time() - 4 * 365 * 86400) * 1000)
        daily = await self.api.fetch_ohlcv_range(symbol, "1d", since_ms=since)
        if daily is not None and not daily.empty:
            self._daily[symbol] = daily
            self._fetched_at[symbol] = time.time()
        return daily

    async def get(self, symbol: str, price: float) -> Optional[HistLevels]:
        try:
            daily = await self._daily_candles(symbol)
            return levels_from_daily(daily, price)
        except Exception as exc:  # contexto opcional: nunca debe tumbar el bot
            logger.warning(f"[Historial] {symbol}: no se pudieron calcular máximos/mínimos ({exc})")
            return None
