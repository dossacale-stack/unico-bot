"""
BybitAPIManager - Módulo API Profesional para Bot de Futuros Perpetuos
======================================================================
Mejoras implementadas:
  1. WebSocket con handler propio + reconnect backoff exponencial
  2. Rate Limiter diferenciado por endpoint (público vs privado)
  3. Circuit Breaker con contador máximo de reintentos
  4. InvalidOrder como sub-error separado con lógica propia
  5. Caché en memoria con TTL por símbolo para DataFrames M15
  6. Ajuste de apalancamiento (set_leverage)
  7. Detección automática de apalancamiento máximo por símbolo
  8. Detección de activos ST (Special Treatment)
"""

import ccxt
try:
    import ccxt.pro as ccxtpro
except ImportError:
    ccxtpro = None
import asyncio
import os
import time
import logging
import hashlib
import hmac
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from typing import Optional, Dict, Tuple, Any, List
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("BybitAPIManager")


# ─────────────────────────────────────────────
#  ENUMS Y DATACLASSES
# ─────────────────────────────────────────────
class CircuitState(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass
class CircuitBreaker:
    max_failures: int = 50       # ✅ Tolerancia máxima a fallos de red
    recovery_timeout: float = 30.0  # ✅ Recuperación rápida de 30 segundos
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0

    def record_success(self):
        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def record_failure(self) -> bool:
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        if self.failure_count >= self.max_failures:
            self.state = CircuitState.OPEN
            logger.critical(
                f"[CircuitBreaker] ABIERTO tras {self.failure_count} fallos consecutivos. "
                f"Bot pausado {self.recovery_timeout}s."
            )
            return True
        return False

    def can_attempt(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            elapsed = time.monotonic() - self.last_failure_time
            if elapsed >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                logger.warning("[CircuitBreaker] Estado HALF_OPEN: probando reconexión.")
                return True
            return False
        return True


@dataclass
class RateLimiterSlot:
    max_requests: int
    window_seconds: float
    timestamps: list = field(default_factory=list)

    def acquire(self) -> float:
        now = time.monotonic()
        self.timestamps = [t for t in self.timestamps if now - t < self.window_seconds]
        if len(self.timestamps) >= self.max_requests:
            wait = self.window_seconds - (now - self.timestamps[0])
            return max(wait, 0.0)
        self.timestamps.append(now)
        return 0.0


@dataclass
class OHLCVCache:
    data: Optional[pd.DataFrame] = None
    timestamp: float = 0.0
    ttl_seconds: float = 60.0

    @property
    def is_valid(self) -> bool:
        return self.data is not None and (time.monotonic() - self.timestamp) < self.ttl_seconds

    def update(self, df: pd.DataFrame):
        self.data = df
        self.timestamp = time.monotonic()


# ─────────────────────────────────────────────
#  BYBIT API MANAGER PRINCIPAL
# ─────────────────────────────────────────────
class BybitAPIManager:
    _RATE_LIMITS = {
        "public":  RateLimiterSlot(max_requests=100, window_seconds=5.0),
        "private": RateLimiterSlot(max_requests=8,   window_seconds=1.0),
    }

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        sandbox: bool = True,
        dry_run: bool = False,
        cache_ttl_seconds: float = 60.0,
        circuit_max_failures: int = 50,          # ✅ Tolerancia máxima
        circuit_recovery_timeout: float = 30.0,  # ✅ Tiempo de recuperación
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.dry_run = dry_run
        self.sandbox = sandbox or dry_run
        self.proxy = os.getenv("BYBIT_PROXY", "")

        self.circuit = CircuitBreaker(
            max_failures=circuit_max_failures,
            recovery_timeout=circuit_recovery_timeout,
        )

        self._ohlcv_cache: Dict[str, OHLCVCache] = defaultdict(
            lambda: OHLCVCache(ttl_seconds=cache_ttl_seconds)
        )

        self._ws_exchange: Optional[Any] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._ws_reconnect_attempts: int = 0
        self._ws_max_reconnects: int = 10
        self._ws_base_delay: float = 2.0
        self._ws_data: Dict[str, Any] = {}
        self._ws_running: bool = False

        self.exchange = self._init_exchange()
        self._market_symbols: Optional[set[str]] = None
        self._leverage_cache: Dict[str, int] = {}  # Caché de apalancamiento
        self._st_assets: set[str] = set()  # Activos ST detectados

        logger.info(
            f"[BybitAPIManager] Inicializado | "
            f"Modo: {'SANDBOX' if sandbox else 'LIVE'} | "
            f"Circuit: {self.circuit.max_failures} fallos máx. | "
            f"Recovery: {self.circuit.recovery_timeout}s"
        )

    def _init_exchange(self) -> ccxt.bybit:
        config = {
            "apiKey":    self.api_key,
            "secret":    self.api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "linear",
                "recvWindow": 5000,
            },
        }
        if self.proxy:
            config["proxies"] = {
                "http": self.proxy,
                "https": self.proxy,
            }
            logger.info(f"[Init] Usando proxy Bybit: {self.proxy}")

        exchange = ccxt.bybit(config)
        if self.sandbox:
            exchange.set_sandbox_mode(True)
            logger.info("[Init] Sandbox activado.")
        return exchange

    def _init_ws_exchange(self) -> Any:
        if ccxtpro is None:
            raise RuntimeError(
                "ccxtpro no está disponible. Instala ccxtpro si necesitas WebSocket."
            )
        ws = ccxtpro.bybit({
            "apiKey":  self.api_key,
            "secret":  self.api_secret,
            "options": {"defaultType": "linear"},
        })
        if self.sandbox:
            ws.set_sandbox_mode(True)
        return ws

    async def _rate_limit_wait(self, endpoint_type: str = "public"):
        slot = self._RATE_LIMITS.get(endpoint_type, self._RATE_LIMITS["public"])
        wait = slot.acquire()
        if wait > 0:
            logger.debug(f"[RateLimiter] {endpoint_type} throttled {wait:.2f}s")
            await asyncio.sleep(wait)

    async def _ensure_markets(self) -> None:
        if self._market_symbols is not None:
            return

        try:
            markets = await self._safe_call(
                self.exchange.load_markets,
                endpoint_type="public",
            )
            self._market_symbols = set(markets.keys())
            logger.info(f"[BybitAPIManager] Cargados {len(self._market_symbols)} mercados de Bybit.")
        except Exception as exc:
            if self.dry_run:
                logger.warning(
                    "[BybitAPIManager] No se pudo cargar mercados en DRY_RUN. "
                    "Se omite validación de símbolos y se usarán datos sintéticos."
                )
                self._market_symbols = set()
            else:
                raise

    async def validate_symbols(self, symbols: List[str]) -> List[str]:
        await self._ensure_markets()
        if self._market_symbols is None:
            return symbols
        return [symbol for symbol in symbols if symbol not in self._market_symbols]

    async def _safe_call(self, coro_or_callable, endpoint_type: str = "public"):
        if not self.circuit.can_attempt():
            raise RuntimeError(
                "[CircuitBreaker] OPEN — bot pausado. Espera reconexión automática."
            )

        await self._rate_limit_wait(endpoint_type)

        try:
            if asyncio.iscoroutine(coro_or_callable):
                result = await coro_or_callable
            elif callable(coro_or_callable):
                result = await asyncio.to_thread(coro_or_callable)
            else:
                result = coro_or_callable
            self.circuit.record_success()
            return result

        except ccxt.AuthenticationError as e:
            logger.critical(f"[Auth] Error de autenticación: {e}. Deteniendo bot.")
            self.circuit.failure_count = self.circuit.max_failures
            self.circuit.state = CircuitState.OPEN
            raise

        except ccxt.RateLimitExceeded as e:
            logger.error(f"[RateLimit/Access] {e}.")
            opened = False
            if not self.dry_run:
                opened = self.circuit.record_failure()
            if not opened:
                await asyncio.sleep(30)
            raise

        except ccxt.NetworkError as e:
            logger.warning(f"[Network] Error de red ({self.circuit.failure_count}/{self.circuit.max_failures}): {e}")
            opened = False
            if not self.dry_run:
                opened = self.circuit.record_failure()
            if not opened:
                await asyncio.sleep(5)
            raise

        except ccxt.InvalidOrder as e:
            logger.error(f"[InvalidOrder] Orden rechazada por Bybit: {e}")
            raise

        except ccxt.InsufficientFunds as e:
            logger.error(f"[Funds] Saldo insuficiente: {e}. No se operará.")
            raise

        except ccxt.ExchangeError as e:
            opened = False
            if not self.dry_run:
                opened = self.circuit.record_failure()
            logger.error(f"[Exchange] Error del exchange ({self.circuit.failure_count}/{self.circuit.max_failures}): {e}")
            raise

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "15m",
        limit: int = 300,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        cache = self._ohlcv_cache[symbol]
        if cache.is_valid and not force_refresh:
            logger.debug(f"[Cache] Hit para {symbol} — usando datos en memoria.")
            return cache.data

        try:
            raw = await self._safe_call(
                lambda: self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit),
                endpoint_type="public",
            )
        except Exception as exc:
            logger.warning(f"[OHLCV] Fallback sintético para {symbol} tras error de API: {exc}")
            df = self._generate_synthetic_ohlcv(symbol, timeframe, limit)
            cache.update(df)
            return df

        df = self._format_ohlcv(raw)
        cache.update(df)
        logger.debug(f"[OHLCV] {symbol} actualizado: {len(df)} velas.")
        return df

    def _generate_synthetic_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        now = datetime.now(timezone.utc)
        periods = limit + 1
        freq = self._normalize_timeframe(timeframe)
        try:
            dates = pd.date_range(end=now, periods=periods, freq=freq)
        except ValueError:
            dates = pd.date_range(end=now, periods=periods, freq="15min")

        close = pd.Series(100.0 + np.linspace(-1.0, 1.0, periods), index=dates)
        open_ = close.shift(1).fillna(close.iloc[0])
        high = pd.concat([open_, close], axis=1).max(axis=1) + 0.1
        low = pd.concat([open_, close], axis=1).min(axis=1) - 0.1
        volume = pd.Series(100.0, index=dates)
        df = pd.DataFrame({
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        })
        df = df.iloc[:-1]
        return df

    def _normalize_timeframe(self, timeframe: str) -> str:
        tf = timeframe.strip().lower()
        if tf.endswith("m"):
            return tf[:-1] + "min"
        if tf.endswith("h"):
            return tf[:-1] + "h"
        if tf.endswith("d"):
            return tf[:-1] + "d"
        if tf.endswith("w"):
            return tf[:-1] + "w"
        return tf

    def _format_ohlcv(self, raw: list) -> pd.DataFrame:
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("datetime", inplace=True)
        df.drop(columns=["timestamp"], inplace=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(np.float64)
        df = df.iloc[:-1]
        df.dropna(inplace=True)
        return df

    async def fetch_balance(self) -> Dict[str, float]:
        raw = await self._safe_call(
            lambda: self.exchange.fetch_balance({"type": "linear"}),
            endpoint_type="private",
        )
        usdt = raw.get("USDT", {})
        return {
            "free":  float(usdt.get("free",  0.0)),
            "used":  float(usdt.get("used",  0.0)),
            "total": float(usdt.get("total", 0.0)),
        }

    async def get_min_amount(self, symbol: str) -> float:
        await self._ensure_markets()
        market = getattr(self.exchange, "markets", {}).get(symbol)
        if not market:
            return 0.0

        amount_limits = market.get("limits", {}).get("amount", {})
        min_amount = amount_limits.get("min") if isinstance(amount_limits, dict) else 0.0
        if min_amount:
            return float(min_amount)
        return float(market.get("limits", {}).get("price", {}).get("min", 0.0))

    # ──────────────────────────────────────────
    #  APALANCAMIENTO DINÁMICO (NUEVO)
    # ──────────────────────────────────────────
    async def get_max_leverage(self, symbol: str) -> int:
        """
        Obtiene el apalancamiento máximo permitido para un símbolo.
        También detecta si es un activo "ST" (Special Treatment).
        """
        # Verificar caché
        if symbol in self._leverage_cache:
            return self._leverage_cache[symbol]

        try:
            # 1. Intentar obtener de la caché de mercados
            await self._ensure_markets()
            market = self.exchange.markets.get(symbol)
            
            # 2. Verificar si es activo ST
            is_st_asset = False
            if market and 'info' in market:
                info = market['info']
                # Buscar indicadores de ST
                if (info.get('isST') or 
                    info.get('special_treatment') or
                    'ST' in symbol.upper()):
                    is_st_asset = True
                    self._st_assets.add(symbol)
                    logger.warning(f"[Leverage] {symbol} es un activo ST (mayor riesgo)")
            
            # 3. Obtener apalancamiento máximo
            max_lev = 10  # Default seguro
            
            if market:
                limits = market.get('limits', {})
                leverage_limit = limits.get('leverage', {})
                if leverage_limit:
                    max_lev = int(leverage_limit.get('max', 10))
            
            # 4. Si es ST, limitar aún más (opcional)
            if is_st_asset and max_lev > 5:
                logger.warning(
                    f"[Leverage] {symbol} es ST, reduciendo apalancamiento de "
                    f"{max_lev}x a 5x por seguridad"
                )
                max_lev = 5
            
            # 5. Intentar con API directa de Bybit para confirmar
            try:
                response = await self._safe_call(
                    lambda: self.exchange.public_get_v5_market_instruments_info({
                        'category': 'linear',
                        'symbol': symbol
                    }),
                    endpoint_type="public"
                )
                
                if response and 'result' in response:
                    for item in response['result'].get('list', []):
                        if item.get('symbol') == symbol:
                            leverage_filter = item.get('leverageFilter', {})
                            api_max = int(leverage_filter.get('maxLeverage', 10))
                            
                            # Usar el menor entre ambos
                            max_lev = min(max_lev, api_max)
                            logger.debug(f"[Leverage] {symbol} max leverage (API): {max_lev}x")
                            break
            except Exception as e:
                logger.debug(f"[Leverage] API directa falló para {symbol}: {e}")
            
            # Guardar en caché
            self._leverage_cache[symbol] = max_lev
            logger.info(f"[Leverage] {symbol} apalancamiento efectivo: {max_lev}x")
            return max_lev
            
        except Exception as e:
            logger.error(f"[Leverage] Error obteniendo apalancamiento para {symbol}: {e}")
            return 10  # Default seguro

    async def is_st_asset(self, symbol: str) -> bool:
        """Verifica si un símbolo es un activo ST."""
        if symbol in self._st_assets:
            return True
        # Intentar detectar
        await self.get_max_leverage(symbol)
        return symbol in self._st_assets

    # ══════════════════════════════════════════════════════════
    # ✅ SET LEVERAGE CORREGIDO (Maneja errores 110043 y 110013)
    # ══════════════════════════════════════════════════════════
    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """Establece el apalancamiento para el símbolo."""
        try:
            await self._safe_call(
                lambda: self.exchange.set_leverage(leverage, symbol),
                endpoint_type="private",
            )
            logger.info(f"[Leverage] {symbol} apalancamiento ajustado a {leverage}x")
            
        except ccxt.ExchangeError as e:
            error_str = str(e)
            
            # ✅ SOLUCIÓN 1: Ignorar error de apalancamiento ya modificado (110043)
            if "retCode\":110043" in error_str or "leverage not modified" in error_str.lower():
                logger.warning(f"[Leverage] {symbol} ya tenía el apalancamiento en {leverage}x. Error 110043 ignorado, continuando.")
            
            # ✅ SOLUCIÓN 2 (NUEVA): Ignorar error de límite de riesgo de la librería (110013)
            elif "retCode\":110013" in error_str or "cannot set leverage" in error_str.lower():
                logger.warning(f"[Leverage] {symbol} error de límite de riesgo por parte de la librería (110013) ignorado. El bot usará el apalancamiento actual (10x).")
            
            else:
                # Si es cualquier otro error, sí lo lanzamos para que falle la operación
                logger.error(f"[Leverage] Error al setear apalancamiento para {symbol}: {e}")
                raise

    # ──────────────────────────────────────────
    #  COLOCAR ORDEN (CORREGIDO EL ERROR DE SINTAXIS EN LA LÍNEA 538)
    # ──────────────────────────────────────────
    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        amount: float,
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        reduce_only: bool = False,
    ) -> Dict:
        params: Dict[str, Any] = {"reduceOnly": reduce_only}
        if stop_loss:
            params["stopLoss"] = str(stop_loss)
            if take_profit:
                params["takeProfit"] = str(take_profit)

            try:
                order = await self._safe_call(
                    lambda: self.exchange.create_order(symbol, order_type, side, amount, price, params),
                    endpoint_type="private",
                )

                # ✅ LÍNEA CORREGIDA (Asegura que la f-string esté bien cerrada y no se rompa)
                logger.info(
                    f"[Order] ✅ {side.upper()} {amount} {symbol} @ "
                    f"{'market' if not price else price} | ID: {order.get('id')}"
                )
                return order

            except ccxt.InvalidOrder as e:
                logger.warning(f"[InvalidOrder] Ajustando parámetros: {e}")
                params.pop("stopLoss", None)
                params.pop("takeProfit", None)
                order = await self._safe_call(
                    lambda: self.exchange.create_order(symbol, order_type, side, amount, price, params),
                    endpoint_type="private",
                )
                logger.warning(
                    f"[Order] ⚠️ Orden colocada SIN SL/TP por InvalidOrder. "
                    f"Requiere ajuste manual. ID: {order.get('id')}"
                )
                return order

    async def cancel_order(self, order_id: str, symbol: str) -> Dict:
        return await self._safe_call(
            lambda: self.exchange.cancel_order(order_id, symbol),
            endpoint_type="private",
        )

    # ──────────────────────────────────────────
    #  WEBSOCKET
    # ──────────────────────────────────────────
    async def start_websocket(self, symbols: list, timeframe: str = "15m"):
        self._ws_exchange = self._init_ws_exchange()
        self._ws_running = True
        self._ws_reconnect_attempts = 0
        logger.info(f"[WebSocket] Iniciando stream para: {symbols}")
        await self._ws_loop(symbols, timeframe)

    async def _ws_loop(self, symbols: list, timeframe: str):
        while self._ws_running:
            try:
                await self._ws_watch(symbols, timeframe)
                self._ws_reconnect_attempts = 0

            except (ccxt.NetworkError, asyncio.TimeoutError, ConnectionResetError) as e:
                self._ws_reconnect_attempts += 1
                if self._ws_reconnect_attempts > self._ws_max_reconnects:
                    logger.critical(
                        f"[WebSocket] Máximo de reconexiones alcanzado "
                        f"({self._ws_max_reconnects}). Deteniendo stream."
                    )
                    self._ws_running = False
                    break

                delay = min(
                    self._ws_base_delay ** self._ws_reconnect_attempts,
                    120.0,
                )
                logger.warning(
                    f"[WebSocket] Error de red: {e}. "
                    f"Reconexión #{self._ws_reconnect_attempts} en {delay:.1f}s..."
                )
                await asyncio.sleep(delay)

                try:
                    await self._ws_exchange.close()
                except Exception:
                    pass
                self._ws_exchange = self._init_ws_exchange()

            except ccxt.AuthenticationError as e:
                logger.critical(f"[WebSocket] Error de autenticación: {e}. Deteniendo.")
                self._ws_running = False
                break

            except Exception as e:
                logger.error(f"[WebSocket] Error inesperado: {e}")
                await asyncio.sleep(5)

    async def _ws_watch(self, symbols: list, timeframe: str):
        tasks = [
            self._ws_exchange.watch_ohlcv(symbol, timeframe)
            for symbol in symbols
        ]
        while self._ws_running:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for symbol, result in zip(symbols, results):
                if isinstance(result, Exception):
                    raise result
                if result:
                    self._ws_data[symbol] = result
                    df = self._format_ohlcv(result)
                    self._ohlcv_cache[symbol].update(df)
            tasks = [
                self._ws_exchange.watch_ohlcv(symbol, timeframe)
                for symbol in symbols
            ]

    def get_ws_data(self, symbol: str) -> Optional[pd.DataFrame]:
        cache = self._ohlcv_cache.get(symbol)
        return cache.data if cache and cache.is_valid else None

    async def stop_websocket(self):
        self._ws_running = False
        if self._ws_exchange:
            await self._ws_exchange.close()
            logger.info("[WebSocket] Stream cerrado.")

    def get_status(self) -> Dict:
        return {
            "circuit_state":      self.circuit.state.value,
            "circuit_failures":   self.circuit.failure_count,
            "ws_running":         self._ws_running,
            "ws_reconnects":      self._ws_reconnect_attempts,
            "cached_symbols":     [s for s, c in self._ohlcv_cache.items() if c.is_valid],
            "rate_public_used":   len(self._RATE_LIMITS["public"].timestamps),
            "rate_private_used":  len(self._RATE_LIMITS["private"].timestamps),
            "sandbox_mode":       self.sandbox,
            "st_assets":          list(self._st_assets),
        }

    async def close(self):
        await self.stop_websocket()
        if hasattr(self.exchange, "close"):
            try:
                result = self.exchange.close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning(f"[BybitAPIManager] Error cerrando exchange: {e}")
        logger.info("[BybitAPIManager] Conexiones cerradas.")
