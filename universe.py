"""
universe.py — Qué monedas vigila el bot
=======================================
Las que más suben: TOP_N_24H por variación de 24 h y TOP_N_1H por variación de 1 h.

v9 — memoria de la lista (WATCHLIST_MEMORY_HOURS, 96 h por defecto):
  Las flechas de Karla siguen a una moneda DESPUÉS de su subida: roja en el techo, amarillas en la
  caída, verde en el suelo y azules en la recuperación (LAB, SKYAI y ESPORTS en sus capturas).
  Si la lista solo tuviera las que suben AHORA, la moneda saldría en cuanto deja de subir y el bot
  no vería ese ciclo. Por eso una moneda que entró en el top de 24 h se sigue vigilando 96 h.
  El top de 1 h no tiene memoria (es más ruidoso).

Corrección v8: Bybit no envía ningún campo "price1hPcnt"; la variación de 1 h se calcula con
lastPrice y prevPrice1h (el precio de hace una hora).

La misma regla se usa en el backtest (membership_from_closes) para que pruebe lo mismo que el bot.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd


@dataclass
class TickerRow:
    market_id: str
    symbol: str
    last: float
    change_24h: float
    change_1h: Optional[float]
    turnover_24h: float
    funding_rate: Optional[float]
    next_funding_ms: Optional[int]


def _f(value) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def is_eligible_id(market_id: str, excluded_prefixes: Sequence[str]) -> bool:
    mid = (market_id or "").upper()
    if not mid.endswith("USDT") or "USDC" in mid:
        return False
    return not any(mid.startswith(p) for p in excluded_prefixes)


def parse_tickers(raw_tickers: Iterable[dict], markets_by_id: Dict[str, dict],
                  excluded_prefixes: Sequence[str]) -> List[TickerRow]:
    """Convierte la respuesta de /v5/market/tickers (category=linear) en filas útiles."""
    rows: List[TickerRow] = []
    for t in raw_tickers:
        mid = t.get("symbol", "")
        if not is_eligible_id(mid, excluded_prefixes):
            continue
        market = markets_by_id.get(mid)
        if market is None or not market.get("linear") or not market.get("swap"):
            continue
        if market.get("active") is False:
            continue
        last = _f(t.get("lastPrice"))
        if not last or last <= 0:
            continue
        change_24h = _f(t.get("price24hPcnt"))
        if change_24h is None:
            prev24 = _f(t.get("prevPrice24h"))
            change_24h = (last / prev24 - 1) if prev24 else 0.0
        prev1h = _f(t.get("prevPrice1h"))
        change_1h = (last / prev1h - 1) if prev1h else None
        next_funding = _f(t.get("nextFundingTime"))
        rows.append(TickerRow(
            market_id=mid,
            symbol=market["symbol"],
            last=last,
            change_24h=change_24h,
            change_1h=change_1h,
            turnover_24h=_f(t.get("turnover24h")) or 0.0,
            funding_rate=_f(t.get("fundingRate")),
            next_funding_ms=int(next_funding) if next_funding else None,
        ))
    return rows


def top_lists(rows: List[TickerRow], top_n_24h: int, top_n_1h: int,
              min_turnover_24h: float = 0.0) -> Tuple[List[str], List[str]]:
    """(top 24 h, top 1 h) de las que más suben, con el filtro de liquidez."""
    pool = [r for r in rows if r.turnover_24h >= min_turnover_24h]
    top24 = [r.symbol for r in sorted(pool, key=lambda r: r.change_24h, reverse=True)[:top_n_24h]]
    top1h = [r.symbol for r in sorted((r for r in pool if r.change_1h is not None),
                                      key=lambda r: r.change_1h, reverse=True)[:top_n_1h]]
    return top24, top1h


def _merge(*lists: Iterable[str]) -> List[str]:
    seen, result = set(), []
    for lst in lists:
        for s in lst:
            if s not in seen:
                seen.add(s)
                result.append(s)
    return result


def select_top(rows: List[TickerRow], top_n_24h: int, top_n_1h: int, min_turnover_24h: float = 0.0) -> List[str]:
    """Une las que más suben en 24 h y en 1 h (sin repetir, en ese orden)."""
    top24, top1h = top_lists(rows, top_n_24h, top_n_1h, min_turnover_24h)
    return _merge(top24, top1h)


class WatchlistMemory:
    """Recuerda cuándo vio por última vez cada moneda en el top de 24 h (se guarda en la base de datos)."""

    def __init__(self, store, mode: str, memory_hours: float):
        self.store = store
        self.key = f"{mode}:watchlist_memory"
        self.memory = timedelta(hours=memory_hours)

    def _load(self) -> Dict[str, str]:
        return dict(self.store.get_state(self.key, {}) or {})

    def update(self, top24: List[str], top1h: List[str], now: Optional[datetime] = None) -> List[str]:
        """Registra el top actual y devuelve la lista completa a vigilar."""
        now = now or datetime.now(timezone.utc)
        seen = self._load()
        for symbol in top24:
            seen[symbol] = now.isoformat()
        cutoff = now - self.memory
        seen = {s: t for s, t in seen.items() if datetime.fromisoformat(t) >= cutoff}
        self.store.set_state(self.key, seen)
        remembered = sorted((s for s in seen if s not in top24), key=lambda s: seen[s], reverse=True)
        return _merge(top24, remembered, top1h)

    def remembered(self) -> Dict[str, str]:
        return self._load()


DIRECTIONS = {"COMPRA": "LONG", "LONG": "LONG", "INTERES": "LONG", "INTERÉS": "LONG",
              "VENTA": "SHORT", "SHORT": "SHORT", "DESINTERES": "SHORT", "DESINTERÉS": "SHORT",
              "AMBAS": None, "": None}


def parse_manual(text: str) -> Tuple[Dict[str, Optional[str]], List[str]]:
    """Lista escrita por Karla: "TIA:VENTA, VVV, lab compra" → ({"TIAUSDT": "SHORT", ...}, errores).
    Sin dirección = el bot puede comprar y vender esa moneda."""
    result: Dict[str, Optional[str]] = {}
    errors: List[str] = []
    for raw in (text or "").replace(";", ",").replace("\n", ",").split(","):
        part = raw.strip().upper()
        if not part:
            continue
        for sep in (":", "=", " "):
            if sep in part:
                coin, direction = (x.strip() for x in part.split(sep, 1))
                break
        else:
            coin, direction = part, ""
        coin = coin.replace("/USDT:USDT", "").replace("/USDT", "").replace("-", "")
        mid = coin if coin.endswith("USDT") else coin + "USDT"
        if direction not in DIRECTIONS:
            errors.append(f"{raw.strip()}: dirección desconocida (usa COMPRA, VENTA o nada)")
            continue
        result[mid] = DIRECTIONS[direction]
    return result, errors


def format_manual(entries: Dict[str, Optional[str]]) -> str:
    names = {"LONG": "COMPRA", "SHORT": "VENTA"}
    return ", ".join(f"{mid[:-4]}:{names[d]}" if d else mid[:-4] for mid, d in entries.items())


def membership_from_closes(closes_1h: pd.DataFrame, top_n_24h: int, top_n_1h: int, memory_hours: int = 0,
                           eligible: Optional[pd.DataFrame] = None) -> Dict[pd.Timestamp, List[str]]:
    """
    Versión para el backtest. closes_1h: índice = hora de CIERRE de la vela, columnas = símbolos.
    Devuelve, para cada hora, la lista que el bot habría vigilado (top 24 h con memoria + top 1 h).
    `eligible` (mismo índice y columnas, booleano) aplica el filtro de liquidez.
    """
    change_24h = closes_1h / closes_1h.shift(24) - 1
    change_1h = closes_1h / closes_1h.shift(1) - 1
    if eligible is not None:
        change_24h = change_24h.where(eligible)
        change_1h = change_1h.where(eligible)
    rank24 = change_24h.rank(axis=1, ascending=False, method="first")
    in24 = (rank24 <= top_n_24h).astype(float)
    if memory_hours and memory_hours > 0:
        remembered = in24.rolling(int(memory_hours), min_periods=1).max() > 0
    else:
        remembered = in24 > 0
    result: Dict[pd.Timestamp, List[str]] = {}
    for ts in closes_1h.index:
        row24 = change_24h.loc[ts].dropna()
        row1h = change_1h.loc[ts].dropna()
        top24 = list(row24.nlargest(top_n_24h).index) if len(row24) else []
        top1h = list(row1h.nlargest(top_n_1h).index) if len(row1h) else []
        mem = remembered.loc[ts]
        extra = [s for s in mem.index[mem.to_numpy()] if s not in top24]
        result[ts] = _merge(top24, extra, top1h)
    return result


def rank_from_closes(closes_1h: pd.DataFrame, top_n_24h: int, top_n_1h: int,
                     eligible: Optional[pd.DataFrame] = None) -> Dict[pd.Timestamp, List[str]]:
    """Compatibilidad v8: sin memoria."""
    return membership_from_closes(closes_1h, top_n_24h, top_n_1h, 0, eligible)


def turnover_24h_from_hourly(closes_1h: pd.DataFrame, volumes_1h: pd.DataFrame) -> pd.DataFrame:
    """Volumen negociado (USDT) de las últimas 24 h a partir de velas de 1 h."""
    return (closes_1h * volumes_1h).rolling(24, min_periods=1).sum()
