#!/usr/bin/env python3
"""Pruebas sin red ni claves:  python selftest.py"""
import os
import random
import sys
import tempfile
from datetime import datetime

import qs_bot as Q

TF = Q.TF_MS


def ny_ms(y, mo, d, h, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=Q.NY).timestamp() * 1000)


def test_clock():
    # Miercoles 13-mar-2024 10:00 NY -> semanal Q3, diario Q3 (6h), 90m Q3, mensual Q2
    c = Q.clock(ny_ms(2024, 3, 13, 10), False)
    assert (c["qW"], c["qD"], c["q90"], c["qM"], c["qY"]) == (3, 3, 3, 2, 1), c
    # Domingo 3-mar-2024 18:30 NY -> ya cuenta como lunes (Q1); primer cuarto de 6h y de 90m
    c = Q.clock(ny_ms(2024, 3, 3, 18, 30), False)
    assert (c["qW"], c["qD"], c["q90"]) == (1, 1, 1), c
    # Lunes 19:00 NY -> pertenece al martes de trading (Q2)
    assert Q.clock(ny_ms(2024, 3, 4, 19), False)["qW"] == 2
    # Viernes: Q0 salvo que cuente como Q4; sabado: 0
    assert Q.clock(ny_ms(2024, 3, 15, 10), False)["qW"] == 0
    assert Q.clock(ny_ms(2024, 3, 15, 10), True)["qW"] == 4
    assert Q.clock(ny_ms(2024, 3, 16, 12), True)["qW"] == 0
    # 369: 22 de las 64 combinaciones (dato del propio indicador)
    n369 = sum(Q.is369(a, b, c) for a in range(1, 5) for b in range(1, 5) for c in range(1, 5))
    assert n369 == 22, n369
    assert Q.chain_len([4, 1, 1, 2], [True] * 4, True) == 3       # puente Q4<->Q1
    assert Q.chain_len([4, 1, 1, 2], [True] * 4, False) == 2
    print("OK reloj NY, cuartos, 369 (22/64) y cadenas")


def synth(n=8640, seed=5):
    rnd = random.Random(seed)
    start = ny_ms(2024, 1, 1, 18) - 0     # domingo 18:00 NY
    prices = [100.0, 50.0, 30.0]
    out = [[], [], []]
    for k in range(n):
        common = rnd.gauss(0, 0.0012)
        for a in range(3):
            o = prices[a]
            c = o * (1 + common * (1 + 0.1 * a) + rnd.gauss(0, 0.0010))
            hi = max(o, c) * (1 + abs(rnd.gauss(0, 0.0006)))
            lo = min(o, c) * (1 - abs(rnd.gauss(0, 0.0006)))
            out[a].append([start + k * TF, o, hi, lo, c, 1000.0])
            prices[a] = c
    return out


def test_engine():
    a, b, c = synth()
    p = Q.Params()
    sigs = Q.compute_signals(a, b, c, p)
    n_def = sum(1 for s in sigs if s)
    relaxed = Q.Params(align_mode="Sin filtro", use_to=False, use2=False)
    sigs_r = Q.compute_signals(a, b, c, relaxed)
    n_rel = sum(1 for s in sigs_r if s)
    print(f"Velas {len(a)} | senales con parametros por defecto: {n_def} | relajados (sin filtros): {n_rel}")
    assert n_rel > 0, "el motor no genera ninguna senal ni sin filtros"
    for s in sigs_r:
        if s:
            if s["side"] == "long":
                assert s["sl"] < s["entry"] < s["tp"], s
            else:
                assert s["tp"] < s["entry"] < s["sl"], s
            assert s["rr"] >= relaxed.min_rr
    # causalidad: recortar el final no cambia las senales anteriores
    cut = 6500
    s_cut = Q.compute_signals(a[:cut], b[:cut], c[:cut], relaxed)
    for i in range(cut):
        x, y = sigs_r[i], s_cut[i]
        assert (x is None) == (y is None), f"no causal en {i}"
        if x:
            assert x["side"] == y["side"] and abs(x["entry"] - y["entry"]) < 1e-9
    print("OK senales coherentes (SL < entrada < TP) y causales")
    Q.backtest(a, b, c, relaxed)
    return a, b, c, relaxed


class FakeEx:
    def __init__(self, data, symbols):
        self.data = dict(zip(symbols, data))
        self.idx = 0
        self.orders = []
        self.fail_sl = False
        self.ext_pos = False
        self.pos_open = False
        self.markets = {s: {} for s in symbols}

    def fetch_ohlcv(self, symbol, tf, since=None, limit=500):
        return [list(c) for c in self.data[symbol][: self.idx + 1][-limit:]]

    def market(self, s):
        return {"contractSize": 1.0, "limits": {"amount": {"min": 0.0001}, "cost": {"min": 1}}}

    def amount_to_precision(self, s, a): return str(round(a, 4))
    def price_to_precision(self, s, p): return str(round(p, 4))
    def set_margin_mode(self, *a, **k): pass
    def set_leverage(self, *a, **k): pass
    def cancel_all_orders(self, s): pass
    def fetch_balance(self): return {"total": {"USDT": 1000.0}}

    def create_order(self, symbol, typ, side, amount, price, params):
        self.orders.append((typ, side, amount, dict(params)))
        if self.fail_sl and "stopLossPrice" in params:
            raise Exception("sl rechazado")
        return {"average": 100.0}

    def fetch_positions(self, syms):
        return [{"contracts": 1, "side": "long"}] if (self.ext_pos or self.pos_open) else []


def test_bot(a, b, c, relaxed):
    msgs = []
    Q.notify = lambda t: msgs.append(t)
    tmp = tempfile.mkdtemp()
    syms = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]

    def mk(mode, name):
        cfg = Q.load_cfg(None)
        cfg.update(mode=mode, state_file=os.path.join(tmp, name + ".json"), trades_log=os.path.join(tmp, name + ".csv"),
                   trade_symbol=syms[0], triad=syms[1:], candles=1000)
        return cfg

    # signals / paper con velas "en vivo": la ultima vela devuelta esta en formacion
    for mode in ("signals", "paper"):
        msgs.clear()
        ex = FakeEx([a, b, c], syms)
        cfg = mk(mode, mode)
        bot = Q.Bot(cfg, relaxed, ex)
        for k in range(1200, 5200):
            ex.idx = k
            bot.tick(a[k][0] + 1000)
        n_sig = sum(1 for m in msgs if "QS-SSMT" in m and ("LONG" in m or "SHORT" in m))
        print(f"{mode}: mensajes de senal {n_sig} | abiertas {len(bot.state['positions'])} | equity {bot.state['paper_equity']:.2f}")
        assert n_sig > 0
        if mode == "signals":
            assert not bot.state["positions"]
    print("ultimo mensaje de senal:\n" + [m for m in msgs if "QS-SSMT" in m][-1])

    # live simulado
    sig = dict(side="long", entry=100.0, sl=98.0, tp=104.0, rr=2.0, ts=a[2000][0], info="x")
    msgs.clear()
    ex = FakeEx([a, b, c], syms)
    cfg = mk("live", "live")
    bot = Q.Bot(cfg, relaxed, ex)
    bot.state["day_start_equity"] = 1000.0
    bot.open_position(sig, sig["ts"])
    kinds = [o[3] for o in ex.orders]
    assert kinds[0].get("positionSide") == "LONG" and "stopLossPrice" in kinds[1] and "takeProfitPrice" in kinds[2], kinds
    assert syms[0] in bot.state["positions"]
    # un segundo intento no debe abrir otra: ya hay posicion
    assert bot.entries_allowed(a[2000][0]) == "ya hay una posicion abierta"
    # si el SL falla se cierra la posicion
    ex2 = FakeEx([a, b, c], syms)
    ex2.fail_sl = True
    cfg2 = mk("live", "live2")
    bot2 = Q.Bot(cfg2, relaxed, ex2)
    bot2.open_position(sig, sig["ts"])
    assert syms[0] not in bot2.state["positions"] and ex2.orders[-1][1] == "sell"
    # posicion externa abierta bloquea nuevas entradas
    ex3 = FakeEx([a, b, c], syms)
    ex3.ext_pos = True
    bot3 = Q.Bot(mk("live", "live3"), relaxed, ex3)
    bot3.state["day_start_equity"] = 1000.0
    assert "no gestiona" in bot3.entries_allowed(a[2000][0])
    print("OK live simulado: hedge, SL/TP separados, SL obligatorio, bloqueo con posicion externa")

    # Telegram
    os.environ["TELEGRAM_CHAT_ID"] = "42"
    ups = [{"update_id": 1, "message": {"chat": {"id": 42}, "text": "/stop"}},
           {"update_id": 2, "message": {"chat": {"id": 99}, "text": "/resume"}}]
    Q.tg_updates = lambda off: [u for u in ups if u["update_id"] >= off]
    bot.poll_telegram()
    assert bot.state["halt"] is True and bot.entries_allowed(0) == "bot detenido con /stop"
    print("OK Telegram: /stop propio aceptado, chat ajeno ignorado")

    # variables de entorno
    os.environ.update(BOT_TRADE_SYMBOL="SOL/USDT:USDT", BOT_TRIAD="BTC/USDT:USDT,ETH/USDT:USDT", BOT_MODE="paper",
                      BOT_PARAMS='{"stage_cyc":"90m"}', DATA_DIR=os.path.join(tmp, "d"))
    cfg = Q.load_cfg(None)
    assert cfg["trade_symbol"] == "SOL/USDT:USDT" and Q.make_params(cfg).stage_cyc == "90m"
    print("OK variables de entorno")


if __name__ == "__main__":
    test_clock()
    a, b, c, relaxed = test_engine()
    test_bot(a, b, c, relaxed)
    print("\nTODO OK")
