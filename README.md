# unico-bot

Bot de trading para futuros perpetuos USDT-M en Bybit.

## Estructura del proyecto

- `main.py`: entrypoint del bot.
- `bybit_api_manager.py`: gestor de conexión y órdenes con Bybit.
- `risk_manager.py`: cálculo de tamaño de posición, kill switch y seguimiento.
- `market_scanner.py`: escaneo de mercado básico y generación de señales.
- `order_executor.py`: ejecución de órdenes y simulación DRY_RUN.
- `seed_patterns.py`: base de datos de patrones de trading.
- `requirements.txt`: dependencias del proyecto.

## Instalación

```bash
python3 -m pip install -r requirements.txt
```

## Uso

```bash
python main.py --init-db
python main.py --status
python main.py --dry-run
python main.py --live
```

Variables de entorno recomendadas:

- `BYBIT_API_KEY`
- `BYBIT_API_SECRET`
- `BYBIT_SANDBOX=true`
- `BOT_MODE=DRY_RUN`

## Notas

- El scanner actual es un ejemplo básico; puedes mejorar `market_scanner.py` con tus patrones.
- `seed_patterns.py` crea `patterns.db` a partir de los patrones definidos.
- El modo `DRY_RUN` simula órdenes sin enviar nada a Bybit.
