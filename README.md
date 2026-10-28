# QS + SSMT 2 etapas + tCISD — Bot 15m (BingX · Railway · Telegram)

Port a Python del indicador Pine **"QS + SSMT 2 Etapas + tCISD | Señales 15m"**.
Opera automáticamente (entrada, SL y TP en el exchange) y te avisa por Telegram.

## Qué hace
1. **Alineación de cuartos** (reloj de Nueva York, el día empieza a las 18:00): suma 369 y/o cadena de cuartos iguales.
2. **Llave SSMT de 2 etapas** sobre una tríada de activos correlacionados (el del gráfico + 2 de referencia).
3. **Gatillo tCISD**: cierre de cuerpo por detrás del cuerpo de la vela que marcó el extremo del cuarto.
4. **SL** tras el extremo del cuarto, **TP** en la extensión "137" o RR fijo. Filtro de True Opens (TDO/TSO/TWO).

## ⚠️ Léelo antes de usar dinero real
- El propio indicador avisa de que **el "137" y la tríada por defecto son supuestos sin fuente verificada** y que **no es una estrategia validada**. La suma "369" tampoco tiene evidencia que la respalde.
- El indicador se pensó para índices CME (NQ/ES/YM). Aquí la tríada por defecto son **BTC/ETH/SOL de BingX** (supuesto mío, configurable).
  Los datos de las tres salen de BingX; no hay datos CME.
- Los cuartos semanales dependen de días de trading: **viernes (salvo `fri_q4`) y fines de semana no generan señales**.
- Haz **backtest → signals → paper (semanas) → live con riesgo mínimo**. Un backtest bueno no garantiza resultados futuros.
- Probado solo con datos sintéticos y exchange simulado (`python selftest.py`). **No probado contra BingX real.**
- No es asesoramiento financiero. Puedes perder todo el capital.

## Archivos
`qs_bot.py` (bot) · `selftest.py` (pruebas sin red) · `Dockerfile` · `railway.json` · `requirements.txt` · `.env.example` · `.github/workflows/selftest.yml`

## 1) GitHub
```bash
cd qs_ssmt_bot
git init -b main
git add .
git commit -m "QS-SSMT bot"
# con la CLI de GitHub:
gh repo create qs-ssmt-bot --private --source=. --push
# o creando el repo vacio en github.com:
# git remote add origin https://github.com/TU_USUARIO/qs-ssmt-bot.git && git push -u origin main
```
Repositorio **privado**. El `.gitignore` ya excluye `.env`, `config.json` y el estado.

## 2) Telegram
1. @BotFather → `/newbot` → copia el **token**.
2. Escribe un mensaje a tu bot y abre `https://api.telegram.org/bot<TOKEN>/getUpdates` → copia `"chat":{"id": ...}`
   (hazlo antes de arrancar el bot, que consume esas actualizaciones).
3. Comandos del bot: `/status` · `/stop` (no abre más entradas; las abiertas siguen con SL/TP) · `/resume`.

## 3) Railway (todo automático)
1. **New Project → Deploy from GitHub repo** → elige el repo (detecta el `Dockerfile`; arranca con `python qs_bot.py run`).
2. **Variables**: copia `.env.example` (mínimo `BOT_MODE`, `BOT_TRADE_SYMBOL`, `BOT_TRIAD`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `DATA_DIR=/data`).
3. **Volume** montado en `/data` (guarda `state.json` y `trades_log.csv` entre despliegues).
4. Mira **Logs** y Telegram: llegará "Bot QS-SSMT iniciado".

## 4) Pasar a dinero real en BingX
1. API key **sin permiso de retiro**, solo trading de futuros perpetuos. No uses lista blanca de IP (la IP de Railway cambia).
2. Variables en Railway: `BOT_MODE=live`, `BOT_API_KEY`, `BOT_API_SECRET`, `BOT_TESTNET=false`, `BOT_CONFIRM_LIVE=YES`.
3. `BOT_HEDGE=true` si tu cuenta usa modo cobertura (por defecto en BingX), `false` si es one-way.
4. Empieza con `BOT_RISK_PCT=0.1` y vigila las primeras operaciones en la app de BingX.

## Seguridad incluida
Riesgo fijo por operación · tope de apalancamiento · **SL obligatorio** (si no se coloca, cierra la posición y avisa) ·
una sola posición a la vez · bloqueo si hay una posición externa abierta · límite de pérdida diaria · pausa tras pérdidas seguidas ·
`/stop` por Telegram · resumen diario · si faltan símbolos, avisa con parecidos.

## Comandos locales
```bash
pip install -r requirements.txt
python selftest.py                                        # pruebas sin red
python qs_bot.py markets --filter NAS                     # buscar simbolos en BingX
python qs_bot.py backtest --days 90                       # backtest con velas reales de BingX
BOT_MODE=signals python qs_bot.py run                     # solo avisos
```

## Parámetros (BOT_PARAMS, JSON)
`seq_mode`, `align_mode`, `min_chain`, `bridge`, `fri_q4`, `use_month`, `use_year`, `stage_cyc`, `use2`, `req_div`, `inv2`, `inv3`,
`use_to`, `min_to`, `max_wait`, `atr_len`, `sl_buf`, `max_risk_atr`, `tp_mode`, `fib_ext`, `rr_fixed`, `min_rr`, `max_bars`, `fee_pct`, `slippage_pct`.
Equivalen a los inputs del indicador.

## Límites
- No incluye el dibujo (cajas, paneles) del indicador, solo la lógica de señales y la ejecución.
- Las señales replican el Pine, pero no son idénticas vela a vela (historial distinto, activos distintos, redondeos).
- Las órdenes condicionales de BingX y el modo cobertura varían: verifica con riesgo mínimo.
