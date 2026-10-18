# UNICO BOT v10 — Bybit Futures (interés y desinterés)

Bot de trading algorítmico para futuros perpetuos USDT en Bybit, con panel web para ## Estrategia v10: interés y desinterés (la que usa por defecto)

Explicada por Karla con TIA y VVV. Colores: **morada = EMA55**, **azul = EMA144**, **amarilla = EMA233** (exponenciales;
se usan 55/144/233 y no 50/100/200 porque la mayoría trabaja con estas últimas).

- 🔻 **Venta por desinterés**: el precio cierra bajo la morada y la azul; vuelve a acercarse por debajo a la morada, la
  azul o la amarilla, la toca y **no logra cerrar encima** (no aguantó). Vela de rechazo bajista.
- 🔺 **Compra por interés**: el precio cierra sobre la morada y la azul; retrocede a una de ellas, la toca y se sostiene.
- No se opera si las tres EMAs están enredadas (base de lado).
- **Salida en la zona**: se cierra TODO en el suelo previo más cercano (ventas) o el techo previo más cercano (compras)
  de las últimas 96 h que esté al menos a 1,5 veces el riesgo. Si no hay zona así, no se entra.
- Stop: más allá del extremo de las últimas 3 velas + el mayor entre 0,3 % y 1 ATR.
- Las flechas de la v9 (abajo) siguen disponibles para comparar con `SETUPS`.

## Estrategia v9: las flechas

Velas de **15 minutos** con EMA 21 / 55 / 144 / 233 y Bandas de Bollinger (21, 2), como en los gráficos de Karla.
**Fase**: alcista si la EMA55 está sobre la EMA144, bajista si está debajo, transición si están a menos de 0,25 %.
Todas las flechas se confirman al **cierre** de la vela y se entra en la apertura de la siguiente.

| Flecha | Qué es | Regla del bot |
|---|---|---|
| 🔴 ROJA | Venta de menor riesgo | Fase alcista o transición. Hubo un techo de clímax en 24 h (tocó la banda superior, ≥ 2 % sobre la EMA55, agotamiento ≥ 0,40), retroceso con cierres bajo la EMA55 y ahora un **máximo más bajo** en la banda superior con vela de rechazo bajista |
| 🟡 AMARILLA | Reventa: muere el interés comprador | Fase bajista o transición. Subida que empezó en la banda inferior (últimas 6 h) y se rechaza en la banda superior o en la EMA144/233, máximo más bajo que el de 24 h |
| 🟢 VERDE | Agotamiento de ventas (**aviso**) | Mínimo de 12 h bajo las EMAs 55/144/233 tocando la banda inferior, con vela de rechazo alcista. Cierra los cortos que van ganando |
| 🔵 AZUL | Compra de menor riesgo | a) **tras la verde**: mínimo más alto después de un suelo de clímax y un rebote con cierres sobre la EMA55; b) **retroceso**: en fase alcista, bajada desde la banda superior que se rechaza en la banda inferior o la EMA144/233 |
| ⚪ TOPE | Agotamiento de compras (aviso, espejo de la verde) | Cierra los largos que van ganando |

- **Fibonacci**: preparado pero desactivado (`FIB_LEVELS` vacío). Las zonas de las capturas eran Fibonacci, pero no forman parte de la regla.
- **Vela de rechazo**: martillo / estrella fugaz o envolvente, de al menos 0,8 ATR y extremo de las últimas 2 h.
- **Ciclo**: después de una AZUL tras la verde no se revende (amarilla) durante 24 h; después de una ROJA no se compran retrocesos durante 24 h.
- **Stop**: más allá del extremo de las últimas 3 velas + el mayor entre 0,3 % y 1 ATR.
- **Puntaje** (0 a 1): 50 % contexto + 50 % calidad del rechazo. En las pruebas con velas reales no separó buenas de malas, por eso la IA revisa todas.
- **Salidas**: TP1 25 % a 2R, TP2 25 % a 5R, break-even a 3R, trailing por ATR tras el TP1 y cierre en el aviso contrario si va ganando.
- **Lista de monedas**: las 15 que más suben en 24 h (y las que estuvieron ahí en las últimas 96 h, para seguir su ciclo completo) + las 15 que más suben en 1 h, con 1 M USDT de volumen mínimo.
- **Panel**: sección «Gráfico con las flechas» para ver cualquier moneda con las flechas que marca el bot y tocar cada una para leer por qué.

se combinan 15m y 1h.

## Qué cambió en la v8

| Antes (v7.3) | Ahora (v8) |
|---|---|
| En real, las posiciones se abrían **sin stop-loss** | La orden de entrada lleva el **SL adjunto**; si Bybit la rechaza, no se abre nada |
| El bot «olvidaba» posiciones al cerrarlas (formato de símbolo equivocado) | Se usan los símbolos correctos y **nunca** se borra una posición sin confirmarlo con Bybit |
| Ante un fallo de red inventaba precios (~100) | Si no hay datos, ese ciclo no opera ni cierra nada |
| Kill switch con saldo fijo de 10.000 y que apagaba el bot | Usa el capital real, se reinicia cada día/semana, se guarda y **no apaga el bot** |
| Tras un reinicio no sabía qué posiciones tenía | Concilia con Bybit al arrancar y en cada ciclo; todo queda en la base de datos |
| `BOT_MODE`, `--live`, `.env` ignorados | Modos `DRY_RUN`, `DEMO`, `TESTNET`, `LIVE` desde `BOT_MODE` (LIVE pide `CONFIRM_LIVE=SI`) |
| PnL de simulación inflado (hasta 2,5×) y sin comisiones | PnL = contratos × precio − comisiones − funding, con velas de 1 minuto |
| Backtest con otra lógica, 10 monedas fijas, sin costos, saltos de 200 velas | Misma lógica que el bot, universo dinámico, una sola cartera, costos reales, duración real |
| «Top 1 h» tomaba las primeras monedas de la lista | Variación de 1 h calculada con el precio de hace una hora |
| TP en la banda de Bollinger (y luego ignorado) | Salidas reales 2R / 5R; se eliminó el TP por bandas |
| Umbral 0,15 en el scanner pero 0,20 en el LLM | Un solo umbral: `MIN_SCORE` |
| Trailing stop vacío | Trailing y break-even funcionando y enviados a Bybit |
| `--init-db` fallaba | Restaurado; los patrones WASHI etiquetan señales y el bot aprende cuáles ganan |
| LLM con solo texto y sin control de errores | Claude/OpenRouter/Groq con datos reales, `LLM_ON_ERROR`, y seguimiento «en sombra» de los vetos |
| Sin forma de verlo | Panel web con contraseña: capital, posiciones, operaciones, señales, pausa y backtest |

Archivos eliminados por no usarse: `scanner_v2.py`, `backtest_v2.py`, `Backrest.py`, `learning_engine.py`,
`historical_context.py`, `schemas.py`, `agents/` (siguen en el historial de git).

## Archivos

| Archivo | Para qué sirve |
|---|---|
| `main.py` | Arranca el bot y el panel; comandos de consola |
| `config.py` | Todas las variables en un solo sitio |
| `strategy.py` | Las flechas (la misma función para el bot, el backtest y el gráfico del panel) |
| `rejection_analyzer.py` | Puntaje de la vela de rechazo (5 contextos) |
| `universe.py` | Qué monedas vigilar (top 24 h con memoria de 96 h y top 1 h) |
| `risk_manager.py` | Tamaño, costos, TP/SL, break-even, trailing, kill switch |
| `order_executor.py` | Ejecución en Bybit y simulador DRY_RUN |
| `bybit_api_manager.py` | Conexión con Bybit |
| `llm_filter.py` | Segunda opinión con IA para cada flecha |
| `seed_patterns.py` / `setup_memory.py` | Patrones WASHI y memoria de resultados por flecha y por patrón |
| `historical_levels.py` | Máximos y mínimos desde que la moneda cotiza en Bybit |
| `database.py` | Base de datos SQLite |
| `dashboard.py` | Panel web |
| `backtest.py` | Backtest de cartera |
| `tests/` | Pruebas automáticas (sin red ni claves), incluidas escenas de cada flecha |

## Puesta en marcha en Railway

1. **Volume**: en el servicio, añade un *Volume* montado en `/data` (si no, la base de datos se borra en cada despliegue).
2. **Variables** (Settings → Variables). Copia las de `.env.example`. Como mínimo:
   - `BOT_MODE=DRY_RUN` para empezar.
   - `DB_PATH=/data/unico.db` y `DATA_DIR=/data`.
   - `DASHBOARD_PASSWORD=` una contraseña larga (sin ella el panel no se activa).
3. **Comando de inicio**: si en Railway tenías un *Start Command* con `--dry-run`, bórralo. El Dockerfile ya ejecuta `python main.py`.
4. **Dominio**: genera un dominio público para el servicio y ábrelo en el móvil. Usuario `karla` (o `DASHBOARD_USER`) y tu contraseña.
5. Revisa en el panel que aparezcan monedas vigiladas y, más adelante, señales.

### Antes de usar dinero real

1. `DRY_RUN` unos días y un **backtest** desde el panel (60 días). Compara el gráfico del panel con tus flechas de TradingView.
2. `DEMO` con claves de la cuenta demo de Bybit: comprueba en la app de Bybit que cada posición
   aparece con su SL, sus dos TP y que el SL se mueve a break-even y con el trailing.
3. `LIVE` solo con `CONFIRM_LIVE=SI`, claves **sin permiso de retiro** y la cuenta en modo **one-way**
   (no cobertura) y **margen cruzado**.

## Filtro de IA con Claude

- **OpenRouter**: `LLM_PROVIDER=openrouter`, `OPENROUTER_API_KEY=...` y `LLM_MODEL=openrouter/free` (gratis) o un modelo de pago.
- Claude directo: crea una clave en la consola de Claude (console.anthropic.com). Se paga por uso, aparte de la suscripción de la app.
- `ANTHROPIC_API_KEY=...` y `LLM_MODEL=claude-haiku-5-5` (rápido y económico).
- Revisa todas las flechas que pasan las reglas (`LLM_AUTO_APPROVE_SCORE=1.0`); recibe la flecha, sus motivos, la fase de 1 h, los máximos/mínimos históricos y las velas con EMAs y bandas.
- `LLM_ON_ERROR=allow`: si la IA falla, decide la regla. `block`: si falla, no se opera.
- El panel muestra cuántas señales vetó y qué habrían dado «en sombra», para saber si de verdad ayuda.

## Backtest

Desde el panel (sección Backtest) o en consola:

```bash
python backtest.py --days 60          # máximo 180
python backtest.py --days 30 --min-score 0.45
```

Usa la misma lógica, el mismo tamaño de posición y las mismas salidas que el bot, una sola cartera
con `MAX_POSITIONS`, comisiones taker/maker, deslizamiento y funding histórico de Bybit.
Informe por flecha, por motivo de salida y por rango de puntaje. Resultados en `DATA_DIR/backtests/<fecha>/`: `summary.json`, `trades.csv`, `equity.csv`, `report.md`.

Limitaciones: no simula el filtro de IA; solo incluye contratos que cotizan hoy; dentro de una vela
asume primero el SL; el break-even y el trailing se mueven al cierre de cada vela.

## Comandos

```bash
pip install -r requirements.txt
python main.py --check      # comprueba configuración y conexión con Bybit
python main.py --init-db    # crea la base de datos y los patrones WASHI
python main.py --status     # resumen en consola
python main.py              # arranca el bot y el panel
```

En local usa `DB_PATH=unico.db` y `DATA_DIR=data`.

Pruebas: `pip install -r requirements-dev.txt && pytest`

## Decisiones tomadas con Karla

1. **La verde** es aviso: cierra los cortos que van ganando (`ENTRY_ON_GREEN=no`).
2. **La 1 h** es solo contexto, no bloquea retrocesos (`FILTRO_1H=off`).
3. **Zonas horizontales** de las capturas = Fibonacci; no se usan en el código (desactivado).
7. **Las flechas** de las capturas ilustran cómo se ven las condiciones; la entrada es por **zona** (pendiente de que Karla explique el desarrollo).
4. **Riesgo**: 15 % de margen y SL = 40 % del margen (hasta 6 % de la cuenta por operación).
5. **IA**: OpenRouter (o Claude/Groq si se pone su clave).
6. **Backtest**: velas de Bybit o de Binance (`BACKTEST_EXCHANGE`).
