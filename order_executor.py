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

    async def reconcile_positions(self) -> None:
        logger.info("[OrderExecutor] Reconciliando posiciones (modo base).")
        await asyncio.sleep(0.01)

    async def open_position(
        self,
        symbol: str,
        side: str,
        position_size: Any,
        stop_loss: float,
        take_profit: float,
    ) -> Optional[Dict[str, Any]]:
        amount = float(position_size.contracts)
        if self.mode == BotMode.DRY_RUN:
            order_id = f"DRY-{symbol}-{int(time.time())}"
            logger.info(
                f"[OrderExecutor] DRY_RUN abrir {symbol} {side} {amount} contratos"
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
            logger.error(f"[OrderExecutor] Error cerrando posición: {exc}")
            return None

    async def reverse_position(
        self,
        symbol: str,
        current_side: str,
        position_size: Any,
    ) -> Optional[Dict[str, Any]]:
        opposite = "sell" if current_side == "buy" else "buy"
        await self.close_position(
            symbol=symbol,
            side=current_side,
            contracts=position_size.contracts,
            current_price=position_size.entry_price,
            reason="REVERSE",
        )
        return await self.open_position(
            symbol=symbol,
            side=opposite,
            position_size=position_size,
            stop_loss=position_size.stop_loss,
            take_profit=position_size.take_profit,
        )
