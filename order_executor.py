import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from bybit_api_manager import BybitAPIManager
from risk_manager import BotMode

logger = logging.getLogger("OrderExecutor")


@dataclass
class OrderResult:
    id: str
    symbol: str
    side: str
    amount: float
    price: Optional[float]
    metadata: Dict[str, Any]


class OrderExecutor:
    def __init__(self, api_manager: BybitAPIManager, mode: BotMode):
        self.api = api_manager
        self.mode = mode
        self.default_leverage = 10

    async def reconcile_positions(self) -> None:
        logger.info("[OrderExecutor] Reconciliando posiciones (modo base).")
        await asyncio.sleep(0.01)

    # ==========================================
    # 🟢 NUEVO: Método para verificar existencia real de la posición en Bybit
    # ==========================================
    async def check_position_exists(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Verifica si la posición está realmente abierta en Bybit.
        Retorna el dict de la posición si existe (size > 0), o None si no existe.
        """
        try:
            # Usamos el método get_positions de tu BybitAPIManager
            response = await self.api.get_positions(symbol=symbol)
            
            # Bybit a veces devuelve un diccionario con "list" dentro
            if isinstance(response, dict) and "list" in response:
                positions = response["list"]
            elif isinstance(response, list):
                positions = response
            else:
                positions = []

            for pos in positions:
                # Verificamos que el símbolo coincida y que el tamaño sea > 0
                if pos.get("symbol") == symbol:
                    size = float(pos.get("size", 0))
                    if size > 0:
                        return pos
            
            return None  # No se encontró posición activa

        except Exception as e:
            logger.error(f"[OrderExecutor] Error verificando posición {symbol}: {e}")
            return None

    async def open_position(
        self,
        symbol: str,
        side: str,
        position_size: Any,
        stop_loss: float,
        take_profit: float,
        leverage: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        amount = float(position_size.contracts)
        lev = leverage or self.default_leverage

        if self.mode == BotMode.DRY_RUN:
            order_id = f"DRY-{symbol}-{int(time.time())}"
            logger.info(
                f"[OrderExecutor] DRY_RUN abrir {symbol} {side} {amount} contratos "
                f"(SL={stop_loss:.4f}, TP={take_profit:.4f})"
            )
            return {
                "id": order_id,
                "symbol": symbol,
                "side": side,
                "amount": amount,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }

        try:
            # 1. Ajustar apalancamiento
            await self.api.set_leverage(symbol, lev)

            # 2. Verificar balance disponible
            try:
                bal = await self.api.fetch_balance()
                logger.debug(
                    f"[OrderExecutor] balance antes de orden: total={bal.get('total')} free={bal.get('free')}"
                )
            except Exception as e:
                logger.debug(f"[OrderExecutor] no se pudo fetch_balance: {e}")

            # 3. Verificar tamaño mínimo
            try:
                min_amt = await self.api.get_min_amount(symbol)
            except Exception:
                min_amt = 0.0

            if min_amt and amount < float(min_amt):
                logger.warning(
                    f"[OrderExecutor] Cantidad {amount} menor que min del mercado {min_amt}. Ajustando a min."
                )
                amount = float(min_amt)

            # 4. Validar SL y TP
            if stop_loss <= 0 or take_profit <= 0:
                logger.error(f"[OrderExecutor] SL/TP inválidos: SL={stop_loss}, TP={take_profit}")
                return None

            # 5. Colocar orden (modo One-Way)
            order = await self.api.place_order(
                symbol=symbol,
                side=side,
                order_type="market",
                amount=amount,
                price=None,
                stop_loss=stop_loss,
                take_profit=take_profit,
                reduce_only=False,
            )
            return {
                "id": order.get("id", str(order.get("order_link_id", ""))),
                "symbol": symbol,
                "side": side,
                "amount": amount,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }
        except Exception as exc:
            logger.error(f"[OrderExecutor] Error abriendo posición: {exc}")
            return None

    async def close_position(
        self,
        symbol: str,
        side: str,
        contracts: float,
        current_price: float,
        reason: Any,
    ) -> Optional[Dict[str, Any]]:
        if self.mode == BotMode.DRY_RUN:
            logger.info(
                f"[OrderExecutor] DRY_RUN cerrar {symbol} {contracts} contratos con side {side}"
            )
            return {"id": f"DRY-CLOSE-{symbol}-{int(time.time())}"}

        try:
            # 🟢 CORRECCIÓN 1: Antes de cerrar, verificar que la posición realmente existe en Bybit
            position_exists = await self.check_position_exists(symbol)
            if not position_exists:
                logger.warning(
                    f"[OrderExecutor] 🛡️ Posición {symbol} no existe en Bybit (ya cerrada). "
                    f"Devolviendo 'GHOST' para forzar limpieza en RiskManager."
                )
                # Retornamos un diccionario especial para que main.py entienda que debe limpiarla
                return {"id": "GHOST_POSITION", "symbol": symbol, "is_ghost": True}

            # Si existe, procedemos a cerrar
            order = await self.api.place_order(
                symbol=symbol,
                side=side,
                order_type="market",
                amount=float(contracts),
                price=None,
                stop_loss=None,
                take_profit=None,
                reduce_only=True,
            )
            return {"id": order.get("id", str(order.get("order_link_id", "")))}
        
        except Exception as exc:
            # 🟢 CORRECCIÓN 2: Manejo específico del error de posición fantasma
            error_msg = str(exc)
            if "110017" in error_msg or "current position is zero" in error_msg:
                logger.warning(
                    f"[OrderExecutor] ⚠️ Bybit rechazó orden (Error 110017) en {symbol}. "
                    f"La posición es fantasma. Devolviendo 'GHOST' para limpieza."
                )
                return {"id": "GHOST_POSITION", "symbol": symbol, "is_ghost": True}

            logger.error(f"[OrderExecutor] Error cerrando posición: {exc}")
            return None

    async def reverse_position(
        self,
        symbol: str,
        current_side: str,
        position_size: Any,
        leverage: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        opposite = "sell" if current_side.lower() == "buy" else "buy"
        amount = float(position_size.contracts)
        lev = leverage or self.default_leverage

        close_result = await self.close_position(
            symbol=symbol,
            side=current_side,
            contracts=amount,
            current_price=position_size.entry_price,
            reason="REVERSE",
        )
        if close_result is None:
            logger.error(f"[OrderExecutor] No se pudo cerrar la posición para revertir {symbol}")
            return None

        open_result = await self.open_position(
            symbol=symbol,
            side=opposite,
            position_size=position_size,
            stop_loss=position_size.stop_loss,
            take_profit=position_size.take_profit,
            leverage=lev,
        )
        return open_result
