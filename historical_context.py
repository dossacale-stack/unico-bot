import logging
import pandas as pd
import numpy as np

logger = logging.getLogger("HistoricalContext")

class HistoricalContext:
    def __init__(self, api_manager):
        self.api = api_manager

    async def get_context(self, symbol: str) -> dict:
        """
        Obtiene los rangos de precio del activo en diferentes temporalidades.
        Retorna un diccionario con los niveles clave.
        """
        context = {
            "symbol": symbol,
            "current_price": 0.0,
            "day_high": 0.0, "day_low": 0.0,
            "week_high": 0.0, "week_low": 0.0,
            "month_high": 0.0, "month_low": 0.0,
            "all_time_high": 0.0, "all_time_low": 0.0,
            "position": "UNKNOWN" # MID, HIGH_ZONE, LOW_ZONE, ATH, ATL
        }

        try:
            # 1. Obtener ticker para el precio actual y el rango diario (24h)
            ticker = await self.api._safe_call(
                lambda: self.api.exchange.fetch_ticker(symbol),
                endpoint_type="public"
            )
            context["current_price"] = ticker["last"]
            context["day_high"] = ticker["high"]
            context["day_low"] = ticker["low"]

            # 2. Obtener datos semanales (últimos 7 días - 168 velas de 1h)
            df_week = await self.api.fetch_ohlcv(symbol, timeframe="1h", limit=168)
            if df_week is not None and not df_week.empty:
                context["week_high"] = df_week["high"].max()
                context["week_low"] = df_week["low"].min()

            # 3. Obtener datos mensuales (últimos 30 días - 720 velas de 1h)
            df_month = await self.api.fetch_ohlcv(symbol, timeframe="1h", limit=720)
            if df_month is not None and not df_month.empty:
                context["month_high"] = df_month["high"].max()
                context["month_low"] = df_month["low"].min()

            # 4. Obtener datos históricos (Desde el listado en Bybit)
            # Limitamos a 1500 velas de 1h para no saturar la API (aprox 2 meses de datos)
            # Si el activo tiene más tiempo, el bot trabajará con los últimos 2 meses.
            df_hist = await self.api.fetch_ohlcv(symbol, timeframe="1h", limit=1500)
            if df_hist is not None and not df_hist.empty:
                context["all_time_high"] = df_hist["high"].max()
                context["all_time_low"] = df_hist["low"].min()

            # 5. Clasificar la posición del precio
            price = context["current_price"]
            ath = context["all_time_high"]
            atl = context["all_time_low"]
            month_high = context["month_high"]
            month_low = context["month_low"]

            # Lógica para saber en qué zona está el precio
            if ath > 0 and price >= (ath * 0.95):
                context["position"] = "ATH_ZONE" # Zona de Máximo Histórico (Peligro para Longs)
            elif atl > 0 and price <= (atl * 1.05):
                context["position"] = "ATL_ZONE" # Zona de Mínimo Histórico (Peligro para Shorts)
            elif month_high > 0 and price >= (month_high * 0.95):
                context["position"] = "MONTH_HIGH" # Zona de Máximo del mes
            elif month_low > 0 and price <= (month_low * 1.05):
                context["position"] = "MONTH_LOW" # Zona de Mínimo del mes
            else:
                context["position"] = "MID_RANGE" # Zona media, sin peligro extremo

        except Exception as e:
            logger.error(f"[HistoricalContext] Error obteniendo contexto histórico para {symbol}: {e}")

        return context
