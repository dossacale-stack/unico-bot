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
        logger.info("[OrderExecutor] Reconciliando posiciones.")
        await asyncio.sleep(0.01)

    async def check_position_exists(self, symbol: str) -> Optional[Dict[str, Any]]:
        try:
            response = await self.api.get_positions(symbol=symbol)
            if isinstance(response, dict) and "list" in response:
                positions = response["list"]
            elif isinstance(response, list):
                positions = response
            else:
                positions = []
            for pos in positions:
                if pos.get("symbol") == symbol:
                    if float(pos.get("size", 0)) > 0:
                        return pos
            return None
        except Exception as e:
            logger.error(f"[OrderExecutor] Error verificando posicion {symbol}: {e}")
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

        if symbol.upper().endswith("USDC"):
            logger.info(f"[OrderExecutor] Par USDC bloqueado: {symbol}")
            return None

        if self.mode == BotMode.DRY_RUN:
            order_id = f"DRY-{symbol}-{int(time.time())}"
            logger.info(f"[OrderExecutor] DRY abrir {symbol} {side} {amount} (SL={stop_loss:.4f})")
            return {
                "id": order_id,
                "symbol": symbol,
                "side": side,
                "amount": amount,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "entry_price": position_size.entry_price,
            }

        try:
            existing_pos = await self.check_position_exists(symbol)
            if existing_pos:
                logger.warning(f"[OrderExecutor] Ya hay posicion en {symbol}.")
                return None

            await self.api.set_leverage(symbol, lev)

            order = await self.api.place_order(
                symbol=symbol,
                side=side,
                order_type="market",
                amount=amount,
                price=None,
                stop_loss=None,
                take_profit=None,
                reduce_only=False,
            )

            if not order:
                return None

            await asyncio.sleep(0.5)
            live_pos = await self.check_position_exists(symbol)

            if not live_pos:
                logger.warning(f"[OrderExecutor] No se encontro posicion recien abierta en {symbol}.")
                return order

            executed_price = float(live_pos.get("avgPrice", position_size.entry_price))
            risk_distance = abs(executed_price - stop_loss)

            if side.lower() == "buy":
                new_sl = executed_price - risk_distance
                new_tp1 = executed_price + (risk_distance * 2)
                new_tp2 = executed_price + (risk_distance * 5)
            else:
                new_sl = executed_price + risk_distance
                new_tp1 = executed_price - (risk_distance * 2)
                new_tp2 = executed_price - (risk_distance * 5)

            logger.info(f"[OrderExecutor] Precio real {executed_price:.4f} | SL {new_sl:.4f} | TP2 {new_tp2:.4f}")

            try:
                await self.api.place_order(
                    symbol=symbol,
                    side=side,
                    order_type="limit",
                    amount=0,
                    price=None,
                    stop_loss=new_sl,
                    take_profit=new_tp2,
                    reduce_only=False,
                )
            except Exception as e:
                logger.error(f"[OrderExecutor] Error aplicando SL/TP: {e}")

            return {
                "id": order.get("id", str(order.get("order_link_id", ""))),
                "symbol": symbol,
                "side": side,
                "amount": amount,
                "stop_loss": new_sl,
                "take_profit": new_tp2,
                "tp1": new_tp1,
                "entry_price": executed_price,
            }

        except Exception as exc:
            logger.error(f"[OrderExecutor] Error abriendo posicion: {exc}")
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
            return {"id": f"DRY-CLOSE-{symbol}-{int(time.time())}"}

        try:
            position_exists = await self.check_position_exists(symbol)
            if not position_exists:
                logger.warning(f"[OrderExecutor] {symbol} no existe. GHOST.")
                return {"id": "GHOST_POSITION", "symbol": symbol, "is_ghost": True}

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
            error_msg = str(exc)
            if "110017" in error_msg or "current position is zero" in error_msg.lower():
                return {"id": "GHOST_POSITION", "symbol": symbol, "is_ghost": True}
            logger.error(f"[OrderExecutor] Error cerrando: {exc}")
            return None

    async def update_stop_loss(self, symbol: str, side: str, new_sl: float, new_tp: float) -> bool:
        if self.mode == BotMode.DRY_RUN:
            return True
        try:
            await self.api.place_order(
                symbol=symbol,
                side=side,
                order_type="limit",
                amount=0,
                price=None,
                stop_loss=new_sl,
                take_profit=new_tp,
                reduce_only=False,
            )
            return True
        except Exception as e:
            logger.error(f"[OrderExecutor] Error actualizando SL {symbol}: {e}")
            return False

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
            return None

        return await self.open_position(
            symbol=symbol,
            side=opposite,
            position_size=position_size,
            stop_loss=position_size.stop_loss,
            take_profit=position_size.take_profit,
            leverage=lev,
        )
