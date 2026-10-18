"""
bybit_api_manager.py — Conexión con Bybit (v8)
==============================================
Cambios clave respecto a la v7.3:
  - NUNCA inventa precios: si Bybit no responde, se lanza un error y ese ciclo no opera ni cierra nada.
  - Usa siempre el símbolo correcto para cada endpoint (formato ccxt "BTC/USDT:USDT" en las
    funciones de ccxt y el id nativo "BTCUSDT" en las llamadas directas a la API v5).
  - La orden de entrada lleva el stop-loss adjunto (protección desde el primer segundo).
  - SL/TP posteriores (break-even, trailing) con /v5/position/trading-stop.
  - Velas solo cerradas, caché por (símbolo, temporalidad) que se invalida al cerrar cada vela.
  - Capital real con /v5/account/wallet-balance (cuenta unificada) o cuenta de contratos.

Modos:
  DRY_RUN → solo datos públicos de mainnet.
  DEMO    → datos de mainnet + órdenes en Bybit Demo Trading (api-demo).
  TESTNET → datos y órdenes en la testnet.
  LIVE    → datos y órdenes en mainnet.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import ccxt
import numpy as np
import pandas as pd

from config import EXCHANGE_MODES, Config, tf_ms
from risk_manager import LONG, SHORT, MarketLimits

logger = logging.getLogger("BybitAPIManager")


class DataUnavailable(Exception):
    """Bybit no devolvió datos utilizables. El ciclo debe saltarse ese símbolo."""


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def candles_to_df(raw: List[List[float]]) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop(columns=["timestamp"]).set_index("datetime")
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype(np.float64)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.dropna()


def last_closed_open_ms(timeframe: str, now_ms: Optional[int] = None) -> int:
    """Hora de apertura de la última vela ya CERRADA."""
    now_ms = now_ms or int(time.time() * 1000)
    step = tf_ms(timeframe)
    return (now_ms // step) * step - step


@dataclass
class _CacheEntry:
    df: pd.DataFrame
    last_open_ms: int
    limit: int


class BybitAPIManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.mode = cfg.mode
        options = {
            "defaultType": "swap",
            "defaultSubType": "linear",
            "fetchMarkets": {"types": ["linear"]},
            "recvWindow": 10000,
        }
        base: Dict[str, Any] = {"enableRateLimit": True, "timeout": 20000, "options": dict(options)}
        if cfg.proxy:
            base["proxies"] = {"http": cfg.proxy, "https": cfg.proxy}
            logger.info("[Bybit] Usando proxy para Bybit.")

        self.data_ex = ccxt.bybit({**base, "options": dict(options)})
        if self.mode == "TESTNET":
            self.data_ex.set_sandbox_mode(True)

        self.trade_ex: Optional[ccxt.bybit] = None
        if self.mode in EXCHANGE_MODES:
            self.trade_ex = ccxt.bybit({
                **base,
                "apiKey": cfg.api_key,
                "secret": cfg.api_secret,
                "options": {**options, "adjustForTimeDifference": True},
            })
            if self.mode == "TESTNET":
                self.trade_ex.set_sandbox_mode(True)
            elif self.mode == "DEMO":
                self.trade_ex.enable_demo_trading(True)

        self.markets_by_id: Dict[str, dict] = {}
        self._cache: Dict[Tuple[str, str], _CacheEntry] = {}
        self.consecutive_failures = 0
        self.last_error = ""
        self.account_type = "desconocida"
        logger.info(f"[Bybit] Inicializado en modo {self.mode}")

    # ------------------------------------------------------------------
    # Infraestructura
    # ------------------------------------------------------------------
    async def _run(self, fn, *args, **kwargs):
        try:
            result = await asyncio.to_thread(fn, *args, **kwargs)
            self.consecutive_failures = 0
            return result
        except ccxt.AuthenticationError as exc:
            self.last_error = f"Autenticación rechazada: {exc}"
            logger.critical(f"[Bybit] {self.last_error}")
            raise
        except ccxt.NetworkError as exc:
            self.consecutive_failures += 1
            self.last_error = f"Red/Bybit no disponible: {exc}"
            logger.warning(f"[Bybit] {self.last_error}")
            raise
        except ccxt.ExchangeError as exc:
            self.last_error = str(exc)
            raise

    def _require_trade(self) -> ccxt.bybit:
        if self.trade_ex is None:
            raise RuntimeError("Este modo no tiene cuenta de trading (DRY_RUN).")
        return self.trade_ex

    async def close(self) -> None:
        for ex in (self.data_ex, self.trade_ex):
            if ex is None:
                continue
            try:
                result = ex.close() if hasattr(ex, "close") else None
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # pragma: no cover
                pass

    # ------------------------------------------------------------------
    # Mercados
    # ------------------------------------------------------------------
    async def load_markets(self, reload: bool = False) -> Dict[str, dict]:
        markets = await self._run(self.data_ex.load_markets, reload)
        self.markets_by_id = {
            m["id"]: m for m in markets.values() if m.get("linear") and m.get("swap") and m.get("settle") == "USDT"
        }
        if self.trade_ex is not None:
            self.trade_ex.set_markets_from_exchange(self.data_ex)
        logger.info(f"[Bybit] {len(self.markets_by_id)} contratos perpetuos USDT cargados.")
        return markets

    def market(self, symbol: str) -> dict:
        return self.data_ex.market(symbol)

    def market_id(self, symbol: str) -> str:
        return self.market(symbol)["id"]

    def limits(self, symbol: str) -> MarketLimits:
        m = self.market(symbol)
        info = m.get("info", {}) or {}
        lot = info.get("lotSizeFilter", {}) or {}
        price_filter = info.get("priceFilter", {}) or {}
        precision = m.get("precision", {}) or {}
        limits = m.get("limits", {}) or {}
        return MarketLimits(
            qty_step=_f(lot.get("qtyStep"), _f(precision.get("amount"))),
            min_qty=_f(lot.get("minOrderQty"), _f((limits.get("amount") or {}).get("min"))),
            min_notional=_f(lot.get("minNotionalValue"), _f((limits.get("cost") or {}).get("min"))),
            tick_size=_f(price_filter.get("tickSize"), _f(precision.get("price"))),
        )

    def max_leverage(self, symbol: str) -> int:
        m = self.market(symbol)
        info = m.get("info", {}) or {}
        lev = _f((info.get("leverageFilter") or {}).get("maxLeverage"))
        if lev <= 0:
            lev = _f(((m.get("limits") or {}).get("leverage") or {}).get("max"), 10)
        return max(1, int(lev))

    def is_st(self, symbol: str) -> bool:
        """Activo con etiqueta de riesgo especial. (La v7.3 marcaba cualquier símbolo con 'ST', como STX.)"""
        info = self.market(symbol).get("info", {}) or {}
        tags = info.get("tags") or []
        return bool(str(info.get("isST", "")).lower() in ("1", "true") or str(info.get("stTag", "0")) == "1"
                    or any(str(t).upper() in ("ST", "SPECIAL_TREATMENT") for t in tags))

    def listing_time_ms(self, symbol: str) -> Optional[int]:
        info = self.market(symbol).get("info", {}) or {}
        value = _f(info.get("launchTime"))
        return int(value) if value > 0 else None

    def price_to_precision(self, symbol: str, price: float) -> str:
        return self.data_ex.price_to_precision(symbol, price)

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return float(self.data_ex.amount_to_precision(symbol, amount))

    # ------------------------------------------------------------------
    # Datos de mercado
    # ------------------------------------------------------------------
    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: Optional[int] = None) -> pd.DataFrame:
        """Velas CERRADAS. Lanza DataUnavailable si no hay datos (nunca inventa precios)."""
        limit = int(limit or self.cfg.candles_limit)
        expected_last = last_closed_open_ms(timeframe)
        cached = self._cache.get((symbol, timeframe))
        if cached and cached.last_open_ms >= expected_last and cached.limit >= limit:
            return cached.df.tail(limit)

        raw = await self._run(self.data_ex.fetch_ohlcv, symbol, timeframe, None, min(limit + 1, 1000))
        df = candles_to_df(raw)
        if df.empty:
            raise DataUnavailable(f"Bybit no devolvió velas de {symbol} {timeframe}")
        cutoff = pd.Timestamp(expected_last, unit="ms", tz="UTC")
        df = df[df.index <= cutoff]
        if df.empty:
            raise DataUnavailable(f"Sin velas cerradas de {symbol} {timeframe}")
        self._cache[(symbol, timeframe)] = _CacheEntry(df=df, last_open_ms=int(df.index[-1].value // 10**6), limit=limit)
        return df.tail(limit)

    async def fetch_ohlcv_range(self, symbol: str, timeframe: str, since_ms: int,
                                until_ms: Optional[int] = None, closed_only: bool = True) -> pd.DataFrame:
        """Descarga velas desde since_ms hasta ahora (o until_ms), paginando de 1000 en 1000."""
        step = tf_ms(timeframe)
        until_ms = until_ms or int(time.time() * 1000)
        frames: List[pd.DataFrame] = []
        cursor = int(since_ms)
        while cursor < until_ms:
            window_end = min(cursor + 1000 * step - 1, until_ms)
            raw = await self._run(self.data_ex.fetch_ohlcv, symbol, timeframe, cursor, 1000, {"until": window_end})
            df = candles_to_df(raw)
            if df.empty:
                cursor = window_end + 1          # tramo sin datos (antes del listado): se salta
                continue
            frames.append(df)
            last_ms = int(df.index[-1].value // 10**6)
            cursor = max(last_ms + step, window_end + 1) if len(df) < 1000 else last_ms + step
        if not frames:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        out = pd.concat(frames)
        out = out[~out.index.duplicated(keep="last")].sort_index()
        if closed_only:
            cutoff = pd.Timestamp(last_closed_open_ms(timeframe), unit="ms", tz="UTC")
            out = out[out.index <= cutoff]
        return out[out.index <= pd.Timestamp(until_ms, unit="ms", tz="UTC")]

    async def fetch_linear_tickers(self) -> List[dict]:
        resp = await self._run(self.data_ex.public_get_v5_market_tickers, {"category": "linear"})
        return ((resp or {}).get("result") or {}).get("list") or []

    async def fetch_ticker(self, symbol: str) -> Dict[str, float]:
        resp = await self._run(self.data_ex.public_get_v5_market_tickers,
                               {"category": "linear", "symbol": self.market_id(symbol)})
        items = ((resp or {}).get("result") or {}).get("list") or []
        if not items:
            raise DataUnavailable(f"Sin ticker para {symbol}")
        t = items[0]
        last = _f(t.get("lastPrice"))
        if last <= 0:
            raise DataUnavailable(f"Precio inválido para {symbol}")
        return {
            "last": last,
            "mark": _f(t.get("markPrice"), last),
            "funding_rate": _f(t.get("fundingRate")),
            "next_funding_ms": int(_f(t.get("nextFundingTime"))),
        }

    async def fetch_funding_history(self, symbol: str, start_ms: int, end_ms: int) -> List[Tuple[int, float]]:
        """Tasas de funding históricas [(timestamp_ms, tasa)] entre start y end."""
        out: Dict[int, float] = {}
        cursor_end = int(end_ms)
        mid = self.market_id(symbol)
        for _ in range(200):
            resp = await self._run(self.data_ex.public_get_v5_market_funding_history,
                                   {"category": "linear", "symbol": mid, "endTime": cursor_end, "limit": 200})
            items = ((resp or {}).get("result") or {}).get("list") or []
            if not items:
                break
            for it in items:
                ts = int(_f(it.get("fundingRateTimestamp")))
                if ts >= start_ms:
                    out[ts] = _f(it.get("fundingRate"))
            oldest = min(int(_f(it.get("fundingRateTimestamp"))) for it in items)
            if oldest <= start_ms or len(items) < 200:
                break
            cursor_end = oldest - 1
        return sorted(out.items())

    # ------------------------------------------------------------------
    # Cuenta
    # ------------------------------------------------------------------
    async def detect_account(self) -> str:
        ex = self._require_trade()
        try:
            await self._run(ex.load_time_difference)   # evita rechazos por reloj desfasado
        except Exception as exc:
            logger.warning(f"[Bybit] No se pudo sincronizar la hora con Bybit: {exc}")
        try:
            unified_margin, unified_account = await self._run(ex.is_unified_enabled)
            self.account_type = "UNIFICADA" if unified_account else ("UNIFIED MARGIN" if unified_margin else "CLÁSICA")
        except Exception as exc:
            logger.warning(f"[Bybit] No se pudo detectar el tipo de cuenta: {exc}")
        return self.account_type

    async def get_equity(self) -> Dict[str, float]:
        """Capital real (incluye PnL no realizado)."""
        ex = self._require_trade()
        try:
            resp = await self._run(ex.private_get_v5_account_wallet_balance, {"accountType": "UNIFIED"})
            acc = (((resp or {}).get("result") or {}).get("list") or [{}])[0]
            equity = _f(acc.get("totalEquity"))
            if equity > 0 or acc:
                return {
                    "equity": equity,
                    "available": _f(acc.get("totalAvailableBalance")),
                    "wallet": _f(acc.get("totalWalletBalance")),
                    "upnl": _f(acc.get("totalPerpUPL")),
                }
        except ccxt.ExchangeError as exc:
            logger.info(f"[Bybit] Cuenta unificada no disponible ({exc}); probando cuenta de contratos.")
        resp = await self._run(ex.private_get_v5_account_wallet_balance, {"accountType": "CONTRACT", "coin": "USDT"})
        acc = (((resp or {}).get("result") or {}).get("list") or [{}])[0]
        coin = next((c for c in acc.get("coin", []) if c.get("coin") == "USDT"), {})
        return {
            "equity": _f(coin.get("equity")),
            "available": _f(coin.get("availableToWithdraw"), _f(coin.get("walletBalance"))),
            "wallet": _f(coin.get("walletBalance")),
            "upnl": _f(coin.get("unrealisedPnl")),
        }

    async def ensure_one_way_mode(self) -> None:
        ex = self._require_trade()
        try:
            await self._run(ex.set_position_mode, False, None, {"coin": "USDT"})
            logger.info("[Bybit] Modo de posición: one-way.")
        except ccxt.NoChange:
            logger.info("[Bybit] La cuenta ya está en modo one-way.")
        except Exception as exc:
            logger.warning(f"[Bybit] No se pudo confirmar el modo one-way ({exc}). "
                           "Si la cuenta está en modo cobertura (hedge), cámbialo a one-way en Bybit.")

    # ------------------------------------------------------------------
    # Posiciones y órdenes
    # ------------------------------------------------------------------
    async def fetch_open_positions(self) -> List[Dict[str, Any]]:
        ex = self._require_trade()
        raw = await self._run(ex.fetch_positions, None, {"category": "linear", "settleCoin": "USDT"})
        positions: List[Dict[str, Any]] = []
        for p in raw or []:
            contracts = _f(p.get("contracts"))
            if contracts <= 0:
                continue
            info = p.get("info", {}) or {}
            side = (p.get("side") or "").lower()
            positions.append({
                "symbol": p["symbol"],
                "side": LONG if side == "long" else SHORT,
                "contracts": contracts,
                "entry_price": _f(p.get("entryPrice"), _f(info.get("avgPrice"))),
                "mark_price": _f(p.get("markPrice"), _f(info.get("markPrice"))),
                "upnl": _f(p.get("unrealizedPnl")),
                "stop_loss": _f(info.get("stopLoss")) or None,
                "take_profit": _f(info.get("takeProfit")) or None,
                "leverage": _f(p.get("leverage"), _f(info.get("leverage"))),
                "liq_price": _f(p.get("liquidationPrice")) or None,
                "position_idx": int(_f(info.get("positionIdx"))),
                "created_ms": int(_f(info.get("createdTime"))),
            })
        return positions

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        ex = self._require_trade()
        try:
            await self._run(ex.set_leverage, int(leverage), symbol)
        except ccxt.ExchangeError as exc:
            text = str(exc)
            if "110043" in text or "not modified" in text.lower():
                return
            raise

    async def create_entry_order(self, symbol: str, side: str, qty: float, stop_loss: float,
                                 client_id: str) -> dict:
        """Orden a mercado con el stop-loss ADJUNTO (si Bybit la rechaza, no se abre nada)."""
        ex = self._require_trade()
        order_side = "buy" if side == LONG else "sell"
        params = {
            "stopLoss": {"triggerPrice": float(self.price_to_precision(symbol, stop_loss))},
            "tpslMode": "Full",
            "slTriggerBy": "LastPrice",
            "clientOrderId": client_id,
        }
        return await self._run(ex.create_order, symbol, "market", order_side, qty, None, params)

    async def create_reduce_only_limit(self, symbol: str, position_side: str, qty: float, price: float,
                                       client_id: str) -> dict:
        ex = self._require_trade()
        order_side = "sell" if position_side == LONG else "buy"
        params = {"reduceOnly": True, "timeInForce": "GTC", "clientOrderId": client_id}
        return await self._run(ex.create_order, symbol, "limit", order_side, qty,
                               float(self.price_to_precision(symbol, price)), params)

    async def close_market(self, symbol: str, position_side: str, qty: float) -> dict:
        ex = self._require_trade()
        order_side = "sell" if position_side == LONG else "buy"
        return await self._run(ex.create_order, symbol, "market", order_side, qty, None, {"reduceOnly": True})

    async def set_trading_stop(self, symbol: str, stop_loss: Optional[float] = None,
                               take_profit: Optional[float] = None, position_idx: int = 0) -> None:
        ex = self._require_trade()
        request: Dict[str, Any] = {
            "category": "linear",
            "symbol": self.market_id(symbol),
            "tpslMode": "Full",
            "positionIdx": position_idx,
        }
        if stop_loss is not None:
            request["stopLoss"] = self.price_to_precision(symbol, stop_loss)
            request["slTriggerBy"] = "LastPrice"
        if take_profit is not None:
            request["takeProfit"] = self.price_to_precision(symbol, take_profit)
            request["tpTriggerBy"] = "LastPrice"
        try:
            await self._run(ex.private_post_v5_position_trading_stop, request)
        except ccxt.ExchangeError as exc:
            if "34040" in str(exc) or "not modified" in str(exc).lower():
                return
            raise

    async def fetch_open_orders(self, symbol: str) -> List[dict]:
        ex = self._require_trade()
        return await self._run(ex.fetch_open_orders, symbol)

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        ex = self._require_trade()
        try:
            await self._run(ex.cancel_order, order_id, symbol)
        except ccxt.OrderNotFound:
            pass

    async def cancel_all_orders(self, symbol: str) -> None:
        ex = self._require_trade()
        try:
            await self._run(ex.cancel_all_orders, symbol)
        except ccxt.ExchangeError as exc:
            logger.warning(f"[Bybit] No se pudieron cancelar las órdenes de {symbol}: {exc}")

    async def fetch_closed_pnl(self, symbol: str, start_ms: int, end_ms: Optional[int] = None) -> List[dict]:
        """Registros de PnL cerrado de Bybit (ventanas de máximo 7 días)."""
        ex = self._require_trade()
        end_ms = end_ms or int(time.time() * 1000)
        window = 7 * 86_400_000 - 1
        records: List[dict] = []
        start = int(start_ms)
        while start < end_ms:
            stop = min(start + window, end_ms)
            cursor = None
            for _ in range(50):
                request = {"category": "linear", "symbol": self.market_id(symbol),
                           "startTime": start, "endTime": stop, "limit": 100}
                if cursor:
                    request["cursor"] = cursor
                resp = await self._run(ex.private_get_v5_position_closed_pnl, request)
                result = (resp or {}).get("result") or {}
                records.extend(result.get("list") or [])
                cursor = result.get("nextPageCursor")
                if not cursor:
                    break
            start = stop + 1
        return records

    def status(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "account_type": self.account_type,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "markets": len(self.markets_by_id),
        }
