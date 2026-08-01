import logging
import time
from typing import Optional

from bybit_api_manager import BybitAPIManager

logger = logging.getLogger("TrailingStop")

class TrailingStopManager:
    def __init__(
        self,
        api_manager: BybitAPIManager,
        callback_pct: float = 0.005,  # 0.5% de retroceso para activar el cierre
        is_active: bool = True
    ):
        self.api = api_manager
        self.callback_pct = callback_pct
        self.is_active = is_active
        self._tracked_positions = {}  # Guarda el mejor precio alcanzado por símbolo

    async def manage(self, symbol: str, side: str) -> None:
        """
        Monitorea la posición y mueve el Stop Loss si el precio se mueve a favor.
        """
        if not self.is_active:
            return

        try:
            # 1. Obtener la posición actual en Bybit
            response = await self.api.get_positions(symbol=symbol)
            if isinstance(response, dict) and "list" in response:
                positions = response["list"]
            elif isinstance(response, list):
                positions = response
            else:
                return

            if not positions:
                return

            pos = positions[0]
            size = float(pos.get("size", 0))
            if size == 0:
                return

            current_price = float(pos.get("markPrice", 0))
            current_sl = float(pos.get("stopLoss", 0))
            entry_price = float(pos.get("avgPrice", 0))

            # 2. Determinar la dirección del precio
            if side == "LONG":
                # Para Longs, seguimos el precio más alto
                best_price = self._tracked_positions.get(symbol, entry_price)
                if current_price > best_price:
                    best_price = current_price
                    self._tracked_positions[symbol] = best_price

                # Calcular nuevo SL: Mejor precio - (Retroceso permitido)
                new_sl = best_price - (best_price * self.callback_pct)

                # Si el nuevo SL es mejor que el actual y está por encima del precio de entrada, lo actualizamos
                if new_sl > current_sl and new_sl > entry_price:
                    logger.info(f"[TrailingStop] Actualizando SL LONG {symbol}: {current_sl:.4f} -> {new_sl:.4f} (Best: {best_price:.4f})")
                    await self.api.place_order(
                        symbol=symbol,
                        side="buy",  # Para modificar parámetros
                        order_type="limit",
                        amount=0,
                        price=None,
                        stop_loss=new_sl,
                        take_profit=None,
                        reduce_only=False,
                    )

            elif side == "SHORT":
                # Para Shorts, seguimos el precio más bajo
                best_price = self._tracked_positions.get(symbol, entry_price)
                if current_price < best_price:
                    best_price = current_price
                    self._tracked_positions[symbol] = best_price

                # Calcular nuevo SL: Mejor precio + (Retroceso permitido)
                new_sl = best_price + (best_price * self.callback_pct)

                # Si el nuevo SL es mejor que el actual y está por debajo del precio de entrada, lo actualizamos
                if new_sl < current_sl and new_sl < entry_price:
                    logger.info(f"[TrailingStop] Actualizando SL SHORT {symbol}: {current_sl:.4f} -> {new_sl:.4f} (Best: {best_price:.4f})")
                    await self.api.place_order(
                        symbol=symbol,
                        side="sell",
                        order_type="limit",
                        amount=0,
                        price=None,
                        stop_loss=new_sl,
                        take_profit=None,
                        reduce_only=False,
                    )

        except Exception as e:
            logger.error(f"[TrailingStop] Error gestionando trailing para {symbol}: {e}")

    def reset(self, symbol: str) -> None:
        """Limpia el tracking de un símbolo cuando se cierra la posición."""
        if symbol in self._tracked_positions:
            del self._tracked_positions[symbol]
