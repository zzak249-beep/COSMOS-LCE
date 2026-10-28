#!/usr/bin/env python3
"""Pruebas sin red ni claves:  python selftest.py"""
import os
import random
import tempfile
import time
from datetime import datetime

import qs_bot as Q
import research

TF = Q.TF_MS


def ny_ms(y, mo, d, h, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=Q.NY).timestamp() * 1000)


def test_clock():
    c = Q.clock(ny_ms(2024, 3, 13, 10), False)
    assert (c["qW"], c["qD"], c["q90"], c["qM"], c["qY"]) == (3, 3, 3, 2, 1), c
    c = Q.clock(ny_ms(2024, 3, 3, 18, 30), False)
    assert (c["qW"], c["qD"], c["q90"]) == (1, 1, 1), c
    assert Q.clock(ny_ms(2024, 3, 4, 19), False)["qW"] == 2
    assert Q.clock(ny_ms(2024, 3, 15, 10), False)["qW"] == 0
    assert Q.clock(ny_ms(2024, 3, 15, 10), True)["qW"] == 4
    assert Q.clock(ny_ms(2024, 3, 16, 12), True)["qW"] == 0
    assert sum(Q.is369(a, b, c) for a in range(1, 5) for b in range(1, 5) for c in range(1, 5)) == 22
    assert Q.chain_len([4, 1, 1, 2], [True] * 4, True) == 3 and Q.chain_len([4, 1, 1, 2], [True] * 4, False) == 2
    print("OK reloj NY, cuartos, 369 (22/64) y cadenas")


def test_positions():
    p = Q.Params(use_partial=True, fee_pct=0, slippage_pct=0, tp1_r=1, tp1_pct=50)
    pos = Q.new_position("long", 100, 98, 106, 10, 20, 0, 0, p)
    assert abs(pos["tp1"] - 102) < 1e-9
    assert Q.advance(pos, [1, 100, 102.5, 100.5, 102, 1], 1, p) is None and pos["tp1_done"] and pos["sl"] == 100
    res = Q.advance(pos, [2, 102, 102.5, 99.5, 100, 1], 2, p)
    assert res["reason"] == "BE" and abs(res["pnl"] - 10) < 1e-9, res
    pos = Q.new_position("long", 100, 98, 106, 10, 20, 0, 0, Q.Params(fee_pct=0, slippage_pct=0))
    res = Q.advance(pos, [1, 100, 107, 99, 106, 1], 1, Q.Params(fee_pct=0, slippage_pct=0))
    assert res["reason"] == "TP" and abs(res["pnl"] - 60) < 1e-9
    # SL y TP en la misma vela -> cuenta SL (conservador)
    pos = Q.new_position("short", 100, 102, 94, 10, 20, 0, 0, Q.Params(fee_pct=0, slippage_pct=0))
    assert Q.advance(pos, [1, 100, 103, 93, 98, 1], 1, Q.Params(fee_pct=0, slippage_pct=0))["reason"] == "SL"
    print("OK gestion de posicion: parcial, BE, TP, SL-primero")


def synth(n=8640, seed=5):
    rnd = random.Random(seed)
    start = ny_ms(2024, 1, 1, 18)
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
    sigs = Q.compute_signals(a, b, c, Q.Params())
    relaxed = Q.Params(align_mode="Sin filtro", use_to=False, use2=False)
    sigs_r = Q.compute_signals(a, b, c, relaxed)
    n_rel = sum(1 for s in sigs_r if s)
    print(f"Velas {len(a)} | senales por defecto: {sum(1 for s in sigs if s)} | relajadas: {n_rel}")
    assert n_rel > 0
    for s in sigs_r:
        if s:
            assert (s["sl"] < s["entry"] < s["tp"]) if s["side"] == "long" else (s["tp"] < s["entry"] < s["sl"])
    cut = 6500
    s_cut = Q.compute_signals(a[:cut], b[:cut], c[:cut], relaxed)
    for i in range(cut):
        x, y = sigs_r[i], s_cut[i]
        assert (x is None) == (y is None), f"no causal en {i}"
        if x:
            assert x["side"] == y["side"] and abs(x["entry"] - y["entry"]) < 1e-9
    # filtro de costes: descarta senales con riesgo muy pequeno frente a las comisiones
    n_nocost = sum(1 for s in Q.compute_signals(a, b, c, Q.Params(align_mode="Sin filtro", use_to=False, use2=False, max_cost_r=0)) if s)
    assert n_nocost >= n_rel
    print(f"filtro de costes: {n_nocost} senales sin filtro -> {n_rel} con max_cost_r=0.25")
    # filtros extra
    n_cd = sum(1 for s in Q.compute_signals(a, b, c, Q.Params(align_mode="Sin filtro", use_to=False, use2=False, cooldown_bars=40)) if s)
    assert n_cd < n_rel
    print("OK senales coherentes, causales y cooldown reduce senales")
    r_open = Q.backtest(a, b, c, relaxed, quiet=True, sigs=sigs_r)
    r_close = Q.backtest(a, b, c, Q.Params(align_mode="Sin filtro", use_to=False, use2=False, entry_mode="close"), quiet=True, sigs=sigs_r)
    assert r_open["trades"] > 0 and r_close["trades"] > 0
    print(f"backtest next_open: {r_open['trades']} ops (R medio {r_open['avg_r']:+.2f}) | close: {r_close['trades']} ops (R medio {r_close['avg_r']:+.2f})")
    return a, b, c, relaxed


def test_research():
    a, b, c = synth(5000, seed=9)
    lines = []
    research.run_report(a, b, c, Q.Params(align_mode="Sin filtro", use_to=False, use2=False), folds=4, out=lines.append)
    txt = "\n".join(lines)
    for key in ("ABLACION", "WALK-FORWARD", "AZAR", "MONTE CARLO"):
        assert key in txt, key
    print("OK research (ablacion, walk-forward, IC, Monte Carlo). Extracto:")
    print("\n".join(l for l in lines if ("IC 95%" in l or "Tramos con" in l or "Prob. de acabar" in l)))


class FakeEx:
    def __init__(self, data, symbols):
        self.data = dict(zip(symbols, data))
        self.idx = 0
        self.orders = []
        self.fail_sl = False
        self.ext_pos = False
        self.fill = 100.0
        self.ticker = None
        self.markets = {s: {} for s in symbols}

    def fetch_ohlcv(self, symbol, tf, since=None, limit=500):
        return [list(c) for c in self.data[symbol][: self.idx + 1][-limit:]]

    def fetch_ticker(self, s):
        if self.ticker:
            return self.ticker
        px = self.data[s][self.idx][4]
        return {"bid": px * 0.9999, "ask": px * 1.0001, "last": px}

    def market(self, s): return {"contractSize": 1.0, "limits": {"amount": {"min": 0.0001}, "cost": {"min": 1}}}
    def amount_to_precision(self, s, a): return str(round(a, 4))
    def price_to_precision(self, s, p): return str(round(p, 4))
    def set_margin_mode(self, *a, **k): pass
    def set_leverage(self, *a, **k): pass
    def cancel_all_orders(self, s): pass
    def fetch_open_orders(self, s): return [{"id": 1}]
    def fetch_balance(self): return {"total": {"USDT": 1000.0}}

    def create_order(self, symbol, typ, side, amount, price, params):
        self.orders.append((typ, side, amount, dict(params)))
        if self.fail_sl and "stopLossPrice" in params:
            raise Exception("sl rechazado")
        return {"average": self.fill}

    def fetch_positions(self, syms):
        return [{"contracts": 1, "side": "long"}] if self.ext_pos else []


def test_bot(a, b, c, relaxed):
    msgs = []
    Q.notify = lambda t: msgs.append(t)
    tmp = tempfile.mkdtemp()
    syms = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]

    def mk(mode, name, **kw):
        cfg = Q.load_cfg(None)
        cfg.update(mode=mode, state_file=os.path.join(tmp, name + ".json"), trades_log=os.path.join(tmp, name + ".csv"),
                   trade_symbol=syms[0], triad=syms[1:], candles=1000, **kw)
        return cfg

    for mode in ("signals", "paper"):
        msgs.clear()
        ex = FakeEx([a, b, c], syms)
        bot = Q.Bot(mk(mode, mode), relaxed, ex)
        st = set()
        for k in range(1200, 5200):
            ex.idx = k
            st.add(bot.tick(a[k][0] + 1000))
        n_sig = sum(1 for m in msgs if "QS-SSMT" in m and ("LONG" in m or "SHORT" in m))
        print(f"{mode}: senales {n_sig} | abiertas {len(bot.state['positions'])} | equity {bot.state['paper_equity']:.2f} | estados {st}")
        assert n_sig > 0 and st == {"ok"}
        if mode == "signals":
            assert not bot.state["positions"]
        else:
            paper_bot, paper_ex, paper_k = bot, ex, 5199

    # vela aun no publicada -> reintento
    ex = FakeEx([a, b, c], syms)
    bot = Q.Bot(mk("paper", "retry"), relaxed, ex)
    ex.idx = 2000
    assert bot.tick(a[2002][0] + 1000) == "retry"
    print("OK vela no publicada -> 'retry'")

    # kill switch por drawdown
    ex = FakeEx([a, b, c], syms)
    bot = Q.Bot(mk("paper", "dd", max_drawdown_pct=5.0), relaxed, ex)
    ex.idx = 2000
    bot.state["peak_equity"], bot.state["paper_equity"] = 1000.0, 900.0
    bot.tick(a[2000][0] + 1000)
    assert bot.state["halt"] is True and "DETENIDO" in bot.status_text()
    print("OK kill switch por drawdown")

    # live simulado y guardas
    sig = dict(side="long", entry=100.0, sl=98.0, tp=104.0, rr=2.0, ts=a[2000][0], info="x")
    msgs.clear()
    ex = FakeEx([a, b, c], syms)
    ex.idx = 2000
    bot = Q.Bot(mk("live", "live"), relaxed, ex)
    bot.state["day_start_equity"] = 1000.0
    ex.ticker = {"bid": 99.99, "ask": 100.01, "last": 100.0}
    assert bot.pre_entry_checks(sig) is None
    ex.ticker = {"bid": 99.5, "ask": 100.5, "last": 100.0}
    assert "spread" in bot.pre_entry_checks(sig)
    ex.ticker = {"bid": 100.5, "ask": 100.6, "last": 100.55}
    assert "alejo" in bot.pre_entry_checks(sig)
    ex.ticker = {"bid": 103.9, "ask": 104.1, "last": 104.0}
    assert bot.pre_entry_checks(sig) is not None
    ex.ticker = None
    bot.open_position(sig, sig["ts"])
    kinds = [o[3] for o in ex.orders]
    assert kinds[0].get("positionSide") == "LONG" and "stopLossPrice" in kinds[1] and "takeProfitPrice" in kinds[2], kinds
    assert bot.entries_allowed(a[2000][0]) == "ya hay una posicion abierta"
    # SL rechazado -> cierra
    ex2 = FakeEx([a, b, c], syms); ex2.fail_sl = True
    bot2 = Q.Bot(mk("live", "live2"), relaxed, ex2)
    bot2.open_position(sig, sig["ts"])
    assert syms[0] not in bot2.state["positions"] and ex2.orders[-1][1] == "sell"
    # slippage excesivo en el fill -> cierra
    ex4 = FakeEx([a, b, c], syms); ex4.fill = 100.9
    bot4 = Q.Bot(mk("live", "live4"), relaxed, ex4)
    bot4.open_position(sig, sig["ts"])
    assert syms[0] not in bot4.state["positions"] and ex4.orders[-1][1] == "sell"
    assert any("slippage" in m for m in msgs)
    # posicion externa bloquea
    ex3 = FakeEx([a, b, c], syms); ex3.ext_pos = True
    bot3 = Q.Bot(mk("live", "live3"), relaxed, ex3)
    bot3.state["day_start_equity"] = 1000.0
    assert "no gestiona" in bot3.entries_allowed(a[2000][0])
    # parcial no permitido en live
    try:
        os.environ["BOT_MODE"] = "live"; os.environ["BOT_CONFIRM_LIVE"] = "YES"
        os.environ["BOT_PARAMS"] = '{"use_partial": true}'
        class A: config = None; yes = False
        Q.cmd_run(A, None)
        raise AssertionError("debia rechazar use_partial en live")
    except SystemExit as e:
        assert "use_partial" in str(e)
    finally:
        for k in ("BOT_MODE", "BOT_CONFIRM_LIVE", "BOT_PARAMS"):
            os.environ.pop(k, None)
    print("OK live: guardas de spread/persecucion/slippage, SL obligatorio, bloqueo externo, parcial rechazado")

    # Telegram: comandos
    os.environ["TELEGRAM_CHAT_ID"] = "42"
    ups = [{"update_id": 1, "message": {"chat": {"id": 42}, "text": "/stop"}},
           {"update_id": 2, "message": {"chat": {"id": 99}, "text": "/resume"}},
           {"update_id": 3, "message": {"chat": {"id": 42}, "text": "/risk 0.2"}},
           {"update_id": 4, "message": {"chat": {"id": 42}, "text": "/risk 9"}},
           {"update_id": 5, "message": {"chat": {"id": 42}, "text": "/pnl"}},
           {"update_id": 6, "message": {"chat": {"id": 42}, "text": "/trades"}}]
    Q.tg_updates = lambda off: [u for u in ups if u["update_id"] >= off]
    msgs.clear()
    pb = Q.Bot(mk("paper", "tg"), relaxed, FakeEx([a, b, c], syms))
    pb.ex.idx = 3000
    pb.poll_telegram()
    assert pb.state["halt"] is True and pb.risk_pct() == 0.2
    assert any("Uso: /risk" in m for m in msgs) and any("PnL" in m for m in msgs)
    # cierre manual en paper
    pb.state["halt"] = False
    s2 = dict(side="long", entry=a[3000][4], sl=a[3000][4] * 0.99, tp=a[3000][4] * 1.02, rr=2.0, ts=a[3000][0], info="x")
    pb.open_position(s2, s2["ts"])
    assert pb.state["positions"]
    ups[:] = [{"update_id": 10, "message": {"chat": {"id": 42}, "text": "/close"}}]
    pb.poll_telegram()
    assert not pb.state["positions"] and "Ultimas operaciones" in pb.trades_text()
    print("OK Telegram: /stop /risk /pnl /trades /close, chat ajeno ignorado")


if __name__ == "__main__":
    t0 = time.time()
    test_clock()
    test_positions()
    a, b, c, relaxed = test_engine()
    test_research()
    test_bot(a, b, c, relaxed)
    print(f"\nTODO OK ({time.time() - t0:.0f}s)")
