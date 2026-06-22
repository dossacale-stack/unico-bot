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
        # Apalancamiento por defecto (puedes cambiarlo según tu estrategia)
        self.default_leverage = 10  # Ajusta según tu tolerancia al riesgo

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
        leverage: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Abre una posición en modo One-Way.
        :param symbol: Símbolo (ej. "BTC/USDT:USDT")
        :param side: "buy" o "sell"
        :param position_size: Objeto con atributos .contracts, .entry_price, etc.
        :param stop_loss: Precio de stop loss
        :param take_profit: Precio de take profit
        :param leverage: Apalancamiento (si no se pasa, usa default_leverage)
        """
        amount = float(position_size.contracts)
        lev = leverage or self.default_leverage

        if self.mode == BotMode.DRY_RUN:
            order_id = f"DRY-{symbol}-{int(time.time())}"
            logger.info(
                f"[OrderExecutor] DRY_RUN abrir {symbol} {side} {amount} contratos "
                f"(SL={stop_loss}, TP={take_profit})"
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
            # 1. Ajustar apalancamiento (solo en modo LIVE)
            await self.api.set_leverage(symbol, lev)

            # 2. Verificar balance disponible (diagnóstico)
            try:
                bal = await self.api.fetch_balance()
                logger.debug(
                    f"[OrderExecutor][DEBUG] balance before order: total={bal.get('total')} free={bal.get('free')}"
                )
            except Exception as e:
                logger.debug(f"[OrderExecutor][DEBUG] no se pudo fetch_balance: {e}")

            # 3. Verificar tamaño mínimo según el mercado
            try:
                min_amt = await self.api.get_min_amount(symbol)
            except Exception:
                min_amt = 0.0

            if min_amt and amount < float(min_amt):
                logger.warning(
                    f"[OrderExecutor] Cantidad {amount} menor que min del mercado {min_amt}. Ajustando a min."
                )
                amount = float(min_amt)

            # 4. Colocar orden (modo One-Way, no necesita positionIdx)
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
        """
        Cierra una posición con orden de mercado reduce_only.
        :param side: "sell" si la posición es long, "buy" si es short.
        """
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
        leverage: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Invierte la posición actual usando una orden de mercado sin reduce_only.
        (Funciona en modo One-Way: al enviar una orden contraria sin reduce_only,
        Bybit cierra la posición actual y abre la nueva en una sola operación).
        """
        opposite = "sell" if current_side.lower() == "buy" else "buy"
        amount = float(position_size.contracts)
        lev = leverage or self.default_leverage

        # Primero cerramos la posición actual (reduce_only) y luego abrimos la contraria
        # Para mayor seguridad, lo hacemos en dos pasos.
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

        # Abrir la nueva posición en dirección contraria
        open_result = await self.open_position(
            symbol=symbol,
            side=opposite,
            position_size=position_size,
            stop_loss=position_size.stop_loss,
            take_profit=position_size.take_profit,
            leverage=lev,
        )
        return open_result
