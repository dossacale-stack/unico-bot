# UNICO BOT — Bybit Futures

Bot de trading algorítmico para futuros perpetuos USDT-M en Bybit.

## Estrategia

- **EMAs 55 / 144 / 233** (números de Fibonacci) para detectar quién domina la subasta.
- **Detección de agotamiento**: distancia a la EMA55, velas consecutivas, expansión de Bandas de Bollinger y tiempo en zona.
- **Acción del precio**: velas de rechazo + estructura de máximos/mínimos + volumen.
- **Números no estándar**: usamos 55/144/233 en lugar de 50/100/200 porque la mayoría de traders mira los valores estándar y nosotros queremos anticiparnos a ellos.

## Estructura del proyecto

| Archivo | Descripción |
|---|---|
| `main.py` | Entrypoint del bot. Orquesta todo. |
| `bybit_api_manager.py` | Gestor de conexión, órdenes y rate limiting con Bybit. |
| `risk_manager.py` | Position sizing, kill switch, trailing, break-even. |
| `scanner_v2.py` | Scanner con lógica de agotamiento. |
| `order_executor.py` | Ejecutor de órdenes (live, testnet y DRY_RUN). |
| `setup_memory.py` | Memoria histórica por setup (hit rate y expectancy). |
| `seed_patterns.py` | Patrones iniciales para poblar la DB. |
| `historical_context.py` | Contexto histórico de precios por temporalidad. |
| `learning_engine.py` | Motor de aprendizaje WASHI (niveles, trampas, pesos). |
| `backtest_v2.py` | Backtest histórico con la lógica real del bot. |

## Instalación

```bash
git clone <tu-repo>
cd unico-bot
python3 -m venv .venv
source .venv/bin/activate   # En Windows: .venv\Scripts\activate
pip install -r requirements.txt
