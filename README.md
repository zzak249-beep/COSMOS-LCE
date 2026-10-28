# QS + SSMT 2 etapas + tCISD — Bot 15m (BingX · Railway · Telegram)

Port a Python del indicador Pine **"QS + SSMT 2 Etapas + tCISD | Señales 15m"**, con ejecución automática
(entrada, SL y TP en el exchange), avisos por Telegram y herramientas para **validar** la estrategia antes de arriesgar dinero.

## Qué hace
1. **Alineación de cuartos** (reloj de Nueva York, el día empieza a las 18:00): suma 369 y/o cadena de cuartos iguales.
2. **Llave SSMT de 2 etapas** sobre una tríada de activos (el que se opera + 2 de referencia).
3. **Gatillo tCISD**: cierre de cuerpo por detrás del cuerpo de la vela del extremo del cuarto.
4. **SL** tras el extremo del cuarto, **TP** en la extensión "137" o RR fijo. Filtro de True Opens (TDO/TSO/TWO).

## ⚠️ Léelo antes de usar dinero real
- El propio indicador avisa de que **el "137" y la tríada por defecto son supuestos sin fuente verificada** y que **no es una estrategia validada**. La suma "369" tampoco tiene evidencia que la respalde.
- El indicador se pensó para índices CME. Aquí la tríada por defecto son **BTC/ETH/SOL de BingX** (supuesto mío, configurable); los datos salen de BingX.
- **Viernes (salvo `fri_q4`) y fines de semana no generan señales** (los cuartos semanales son días de trading).
- **Con los filtros por defecto salen pocas señales** (en datos sintéticos, 8 en 90 días). Eso es normal en esta estrategia, pero significa que una muestra estadísticamente útil tarda meses.
- **Filtro de costes (`max_cost_r`, añadido por mí):** descarta señales cuyos costes ida y vuelta superan el 25 % del riesgo. Es lo que más recorta: muchas señales tienen stops tan cortos que comisiones y slippage se comerían el beneficio.
- Probado con datos sintéticos y exchange simulado (`python selftest.py`). **No probado contra BingX real.**
- No es asesoramiento financiero. Puedes perder todo el capital.

## Orden recomendado
1. `python qs_bot.py research --days 180` → ¿hay ventaja distinguible del azar? (ver abajo)
2. `BOT_MODE=signals` unas semanas (solo Telegram) → `paper` unas semanas → `live` con `BOT_RISK_PCT=0.1`.

## Validación: `research`
```bash
python qs_bot.py research --days 180 --folds 4
```
Descarga velas reales de BingX y muestra, con costes incluidos:
1. **Ablación**: qué aporta cada pieza (alineación, SSMT 2 etapas, True Opens, etapa 90m, TP, parcial/BE, costes…).
2. **Walk-forward**: la misma configuración en 4 tramos consecutivos (¿se repite el resultado?).
3. **Intervalo de confianza** del R medio por bootstrap: si incluye 0, no se puede afirmar que haya ventaja.
4. **Monte Carlo**: rango de resultados y de drawdown reordenando tus operaciones.

No elijas "la mejor fila" de la ablación: con muchas variantes siempre sale alguna buena por azar.

## Seguridad en vivo
- Riesgo fijo por operación y tope de apalancamiento; **una sola posición**; bloqueo si hay una posición externa abierta.
- **SL obligatorio**: si no se puede colocar, cierra la posición y avisa. Aviso si no ve órdenes SL/TP por API.
- **No perseguir el precio**: antes de entrar mira spread y precio actual; si ya se alejó más del 25 % del riesgo, o está fuera de SL–TP, no entra.
- **Slippage**: si el fill se aleja más del 35 % del riesgo, cierra al instante.
- Límite de pérdida diaria, **kill switch por drawdown** desde el máximo de equity, pausa tras pérdidas seguidas.
- Datos: espera a que se publique la última vela de los 3 activos (reintentos de 10 s) y avisa si no hay ciclos correctos (watchdog).
- Las lecturas se reintentan; **las órdenes nunca** (para no duplicarlas).
- Parcial/BE (`use_partial`) funciona en backtest y paper, **no en live** (el bot se niega a arrancar).

## Telegram
Comandos: `/status` · `/pnl` · `/trades` · `/close` (cierra la posición) · `/risk 0.3` (riesgo por operación) · `/stop` · `/resume`.
Solo responde a tu chat. Resumen diario automático.

## GitHub y Railway
```bash
cd qs_ssmt_bot
git init -b main && git add . && git commit -m "QS-SSMT bot v2"
gh repo create qs-ssmt-bot --private --source=. --push        # o crea el repo en github.com y haz push
```
Railway: **New Project → Deploy from GitHub repo** (usa el `Dockerfile`) → **Variables** de `.env.example`
(mínimo `BOT_MODE`, `BOT_TRADE_SYMBOL`, `BOT_TRIAD`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `DATA_DIR=/data`) → **Volume** en `/data`.
Telegram: @BotFather → `/newbot` → token; chat id en `https://api.telegram.org/bot<TOKEN>/getUpdates` (antes de arrancar el bot).

## Dinero real en BingX
API key **sin permiso de retiro** (solo futuros perpetuos), sin lista blanca de IP. Variables: `BOT_MODE=live`, `BOT_API_KEY`,
`BOT_API_SECRET`, `BOT_TESTNET=false`, `BOT_CONFIRM_LIVE=YES`; `BOT_HEDGE=true` (modo cobertura, el de BingX por defecto) o `false` (one-way).
Las órdenes condicionales y el modo cobertura de BingX varían: verifica las primeras operaciones en la app.

## Comandos locales
```bash
pip install -r requirements.txt
python selftest.py                                   # pruebas sin red (~3 min por la parte de research)
python qs_bot.py markets --filter NAS                # buscar simbolos en BingX
python qs_bot.py backtest --days 90                  # backtest (entrada en la apertura de la vela siguiente)
python qs_bot.py research --days 180                 # validacion
BOT_MODE=signals python qs_bot.py run                # solo avisos
```

## Parámetros (`BOT_PARAMS`, JSON)
Indicador: `seq_mode`, `align_mode`, `min_chain`, `bridge`, `fri_q4`, `use_month`, `use_year`, `stage_cyc`, `use2`, `req_div`, `inv2`, `inv3`,
`use_to`, `min_to`, `max_wait`, `atr_len`, `sl_buf`, `max_risk_atr`, `tp_mode`, `fib_ext`, `rr_fixed`, `min_rr`, `use_long`, `use_short`.
Añadidos: `max_cost_r`, `cooldown_bars`, `min_atr_pct`, `max_bars`, `use_partial`, `tp1_r`, `tp1_pct`, `use_be`, `entry_mode`,
`fee_pct`, `slippage_pct`, `max_spread_pct`, `max_chase_frac`, `max_slip_frac`.

## Límites
- No incluye el dibujo (cajas, paneles) del indicador, solo la lógica y la ejecución.
- Las señales replican el Pine, pero no son idénticas vela a vela (historial distinto, activos distintos).
- Un backtest bueno no garantiza resultados futuros.
