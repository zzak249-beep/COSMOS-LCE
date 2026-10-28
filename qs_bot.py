#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QS + SSMT 2 ETAPAS + tCISD  --  bot de 15 minutos para BingX (ccxt)

Port a Python del indicador Pine "QS + SSMT 2 Etapas + tCISD | Señales 15m".
Flujo (HTF -> LTF):
  1) Alineacion de cuartos (369 y/o cadena de cuartos iguales)
  2) Llave: SSMT del ciclo elegido + SSMT del ciclo superior en la misma direccion
  3) Gatillo: tCISD (cierre de cuerpo por detras del cuerpo de la vela del extremo del cuarto)
  4) SL tras el extremo del cuarto, TP en extension "137" o RR fijo

Modos:  signals (solo Telegram) | paper (simulado) | live (dinero real) | run (segun BOT_MODE)
        backtest | markets (buscar simbolos en el exchange)

AVISO: el autor del indicador indica que el "137" y la triada por defecto son supuestos sin
fuente verificada y que NO es una estrategia validada. Haz backtest y paper antes de live.
No es asesoramiento financiero.
"""
import argparse
import bisect
import copy
import csv
import json
import logging
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
TF_MS = 900_000            # 15 minutos
log = logging.getLogger("qs")


# ============================================================================
# Parametros (equivalentes a los inputs del Pine)
# ============================================================================
@dataclass
class Params:
    # alineacion de cuartos
    seq_mode: str = "Semanal-Diario-90m"      # o "Mensual-Semanal-Diario"
    align_mode: str = "369 o cadena"          # "Solo 369" | "Solo cadena" | "369 y cadena" | "Sin filtro"
    min_chain: int = 3
    bridge: bool = True
    fri_q4: bool = False
    use_month: bool = True
    use_year: bool = False
    # SSMT
    stage_cyc: str = "Diario"                 # o "90m"
    use2: bool = True
    req_div: bool = False
    inv2: bool = False
    inv3: bool = False
    # True opens
    use_to: bool = True
    min_to: int = 1
    # gatillo
    max_wait: int = 16
    # riesgo / objetivos
    use_long: bool = True
    use_short: bool = True
    atr_len: int = 14
    sl_buf: float = 0.1
    max_risk_atr: float = 8.0
    tp_mode: str = "Extension 137"            # o "RR fijo"
    fib_ext: float = 1.37
    rr_fixed: float = 2.0
    min_rr: float = 1.0
    # ejecucion
    max_bars: int = 0                         # stop por tiempo en velas de 15m (0 = off)
    fee_pct: float = 0.05
    slippage_pct: float = 0.03


# ============================================================================
# Indicadores basicos
# ============================================================================
def rma(vals, n):
    out = [None] * len(vals)
    prev, s = None, 0.0
    for i, v in enumerate(vals):
        if prev is None:
            s += v
            if i == n - 1:
                prev = s / n
                out[i] = prev
        else:
            prev = (prev * (n - 1) + v) / n
            out[i] = prev
    return out


def atr_series(h, l, c, n):
    tr = [h[0] - l[0]]
    for i in range(1, len(c)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return rma(tr, n)


# ============================================================================
# Reloj de Nueva York: el dia de trading empieza a las 18:00
# ============================================================================
def pine_dow(dt: datetime) -> int:
    """Domingo=1 ... Sabado=7 (igual que Pine)."""
    return (dt.weekday() + 1) % 7 + 1


def q_weekly(dt: datetime, fri_q4: bool) -> int:
    sd = pine_dow(dt) + (1 if dt.hour >= 18 else 0)
    if sd in (2, 3, 4, 5):
        return sd - 1
    if sd == 6:
        return 4 if fri_q4 else 0
    return 0


@lru_cache(maxsize=256)
def mq1_start(y: int, mo: int) -> int:
    first = datetime(y, mo, 1, 12, 0, tzinfo=NY)
    first_mon = 1 + ((2 - pine_dow(first) + 7) % 7)
    return int(datetime(y, mo, first_mon, 18, 0, tzinfo=NY).timestamp() * 1000) - 86400000


def q_monthly(ts_ms: int, dt: datetime) -> int:
    y, mo = dt.year, dt.month
    start = mq1_start(y, mo)
    if ts_ms < start:
        mo -= 1
        if mo == 0:
            mo, y = 12, y - 1
        start = mq1_start(y, mo)
    else:
        ny_, nmo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        nxt = mq1_start(ny_, nmo)
        if ts_ms >= nxt:
            start = nxt
    return min((ts_ms - start) // 604_800_000 + 1, 4)


def q_yearly(dt: datetime) -> int:
    return (dt.month - 1) // 3 + 1


def clock(ts_ms: int, fri_q4: bool) -> dict:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, NY)
    sod = dt.hour * 3600 + dt.minute * 60 + dt.second
    msd = ((sod - 64800 + 86400) % 86400) * 1000
    return dict(sod=sod, dow=pine_dow(dt), msd=msd,
                qD=(msd % 86400000) // 21600000 + 1,
                q90=((msd % 86400000) // 5400000) % 4 + 1,
                qW=q_weekly(dt, fri_q4), qM=q_monthly(ts_ms, dt), qY=q_yearly(dt),
                dayId=ts_ms - msd)


def is369(a: int, b: int, c: int) -> bool:
    ok = all(1 <= x <= 4 for x in (a, b, c))
    s = a + b + c
    return ok and (((s - 1) % 9) + 1) % 3 == 0


def valid_pair(a: int, b: int, br: bool) -> bool:
    return (a == b and 1 <= a <= 4) or (br and ((a == 4 and b == 1) or (a == 1 and b == 4)))


def chain_len(q: List[int], en: List[bool], br: bool) -> int:
    best = run = prev = 0
    for qi, e in zip(q, en):
        if not e:
            continue
        if 1 <= qi <= 4:
            run = run + 1 if (run > 0 and valid_pair(prev, qi, br)) else 1
            prev = qi
        else:
            run = prev = 0
        best = max(best, run)
    return best


# ============================================================================
# SSMT
# ============================================================================
class Track:
    """Extremos del cuarto actual (ch, cl) y del anterior (ph, pl)."""
    __slots__ = ("ch", "cl", "ph", "pl")

    def __init__(self):
        self.ch = self.cl = self.ph = self.pl = None

    def update(self, new_q: bool, hi: Optional[float], lo: Optional[float]):
        if new_q:
            self.ph, self.pl = self.ch, self.cl
            self.ch, self.cl = hi, lo
        else:
            self.ch = hi if self.ch is None else (self.ch if hi is None else max(self.ch, hi))
            self.cl = lo if self.cl is None else (self.cl if lo is None else min(self.cl, lo))


def f_div(t0: Track, t1: Track, t2: Track, is_high: bool):
    """Devuelve (hay divergencia, el activo del grafico barrio, el activo del grafico es el diverger)."""
    def swept(t):
        c, p = (t.ch, t.ph) if is_high else (t.cl, t.pl)
        valid = c is not None and p is not None
        return valid, (valid and ((c > p) if is_high else (c < p)))
    v0, s0 = swept(t0)
    v1, s1 = swept(t1)
    v2, s2 = swept(t2)
    nv = int(v0) + int(v1) + int(v2)
    ns = int(s0) + int(s1) + int(s2)
    dv = v0 and nv >= 2 and ns >= 1 and ns < nv
    d1 = (not v1) or s0 != s1
    d2 = (not v2) or s0 != s2
    return dv, s0, (dv and d1 and d2)


def align_to(base: List[List[float]], other: List[List[float]], inverse: bool):
    """Alinea (por timestamp) los high/low de otro activo con las velas del activo del grafico."""
    m = {int(x[0]): (x[2], x[3]) for x in other}
    hs, ls = [], []
    last = (None, None)
    for b in base:
        v = m.get(int(b[0]), last)
        last = v
        h, l = v
        if inverse and h is not None:
            h, l = -l, -h
        hs.append(h)
        ls.append(l)
    return hs, ls


def trig(cur: int, prev: int, tgt: int) -> bool:
    return cur >= tgt and (prev < tgt or cur < prev)


# ============================================================================
# Motor de senales
# ============================================================================
def compute_signals(c0, c1, c2, p: Params) -> List[Optional[dict]]:
    n = len(c0)
    ts = [int(x[0]) for x in c0]
    o = [x[1] for x in c0]
    h = [x[2] for x in c0]
    l = [x[3] for x in c0]
    cl = [x[4] for x in c0]
    H1, L1 = align_to(c0, c1, p.inv2)
    H2, L2 = align_to(c0, c2, p.inv3)
    atr = atr_series(h, l, cl, p.atr_len)

    d = [Track() for _ in range(3)]       # diario (cuartos de 6h)
    nn = [Track() for _ in range(3)]      # 90m
    w = [Track() for _ in range(3)]       # semanal (cuartos = dias de trading)

    seq_wd = p.seq_mode == "Semanal-Diario-90m"
    stage_d = p.stage_cyc == "Diario"
    signals: List[Optional[dict]] = [None] * n
    prevc = None
    sod_prev = -1
    tdo = tso = two = None
    ref_hi_body_low = ref_lo_body_high = None
    ref_hi_bar = ref_lo_bar = -10 ** 9
    done_bear = done_bull = False

    for i in range(n):
        ck = clock(ts[i], p.fri_q4)
        new_d = prevc is not None and ck["qD"] != prevc["qD"]
        new_n = prevc is not None and ck["q90"] != prevc["q90"]
        new_w = prevc is not None and ck["dayId"] != prevc["dayId"]
        prevc = ck
        vals = ((h[i], l[i]), (H1[i], L1[i]), (H2[i], L2[i]))
        for k in range(3):
            d[k].update(new_d, *vals[k])
            nn[k].update(new_n, *vals[k])
            w[k].update(new_w, *vals[k])

        # ---- alineacion ----
        sa, sb, sc = (ck["qW"], ck["qD"], ck["q90"]) if seq_wd else (ck["qM"], ck["qW"], ck["qD"])
        t369 = is369(sa, sb, sc)
        chain = chain_len([ck["qY"], ck["qM"], ck["qW"], ck["qD"], ck["q90"]],
                          [p.use_year, p.use_month, True, True, True], p.bridge)
        chain_ok = chain >= p.min_chain
        if p.align_mode == "369 o cadena":
            align_ok = t369 or chain_ok
        elif p.align_mode == "Solo 369":
            align_ok = t369
        elif p.align_mode == "Solo cadena":
            align_ok = chain_ok
        elif p.align_mode == "369 y cadena":
            align_ok = t369 and chain_ok
        else:
            align_ok = True

        # ---- SSMT ----
        d_bear, _, d_bear_dv = f_div(d[0], d[1], d[2], True)
        d_bull, _, d_bull_dv = f_div(d[0], d[1], d[2], False)
        n_bear, _, n_bear_dv = f_div(nn[0], nn[1], nn[2], True)
        n_bull, _, n_bull_dv = f_div(nn[0], nn[1], nn[2], False)
        w_bear, _, _ = f_div(w[0], w[1], w[2], True)
        w_bull, _, _ = f_div(w[0], w[1], w[2], False)
        if stage_d:
            st_bear, st_bull, st_bear_dv, st_bull_dv = d_bear, d_bull, d_bear_dv, d_bull_dv
            pa_bear, pa_bull = w_bear, w_bull
            q0, new_qs = d[0], new_d
        else:
            st_bear, st_bull, st_bear_dv, st_bull_dv = n_bear, n_bull, n_bear_dv, n_bull_dv
            pa_bear, pa_bull = d_bear, d_bull
            q0, new_qs = nn[0], new_n
        key_bear = st_bear and (not p.use2 or pa_bear)
        key_bull = st_bull and (not p.use2 or pa_bull)
        q_hi, q_lo, p_hi, p_lo = q0.ch, q0.cl, q0.ph, q0.pl

        # ---- True Opens ----
        sod = ck["sod"]
        if trig(sod, sod_prev, 0):
            tdo = o[i]
        if trig(sod, sod_prev, 70200) or trig(sod, sod_prev, 5400) or trig(sod, sod_prev, 27000) or trig(sod, sod_prev, 48600):
            tso = o[i]
        if trig(sod, sod_prev, 64800) and ck["dow"] == 2:
            two = o[i]
        sod_prev = sod
        disc = sum(1 for x in (tdo, tso, two) if x is not None and cl[i] < x)
        prem = sum(1 for x in (tdo, tso, two) if x is not None and cl[i] > x)
        to_ok_l = (not p.use_to) or disc >= p.min_to
        to_ok_s = (not p.use_to) or prem >= p.min_to

        # ---- tCISD ----
        if new_qs:
            done_bear = done_bull = False
        if new_qs or (q_hi is not None and h[i] >= q_hi):
            ref_hi_body_low, ref_hi_bar = min(o[i], cl[i]), i
        if new_qs or (q_lo is not None and l[i] <= q_lo):
            ref_lo_body_high, ref_lo_bar = max(o[i], cl[i]), i
        tc_bear = ref_hi_body_low is not None and cl[i] < ref_hi_body_low and i > ref_hi_bar and i - ref_hi_bar <= p.max_wait
        tc_bull = ref_lo_body_high is not None and cl[i] > ref_lo_body_high and i > ref_lo_bar and i - ref_lo_bar <= p.max_wait
        bear_bias = align_ok and key_bear and not key_bull
        bull_bias = align_ok and key_bull and not key_bear

        a = atr[i]
        if a is None or q_hi is None or q_lo is None:
            continue
        entry = cl[i]
        rng_prev = (p_hi - p_lo) if (p_hi is not None and p_lo is not None) else None

        def build(side: str):
            if side == "short":
                sl = q_hi + a * p.sl_buf
                risk = sl - entry
                tp137 = q_hi - p.fib_ext * rng_prev if rng_prev is not None else None
                tpfix = entry - risk * p.rr_fixed
                tp = tp137 if (p.tp_mode.startswith("Ext") and tp137 is not None and tp137 < entry) else tpfix
                rr = (entry - tp) / risk if risk > 0 else None
            else:
                sl = q_lo - a * p.sl_buf
                risk = entry - sl
                tp137 = q_lo + p.fib_ext * rng_prev if rng_prev is not None else None
                tpfix = entry + risk * p.rr_fixed
                tp = tp137 if (p.tp_mode.startswith("Ext") and tp137 is not None and tp137 > entry) else tpfix
                rr = (tp - entry) / risk if risk > 0 else None
            return sl, risk, tp, rr

        sig = None
        if p.use_short and bear_bias and tc_bear and not done_bear and to_ok_s:
            sl, risk, tp, rr = build("short")
            if risk > 0 and risk <= p.max_risk_atr * a and rr is not None and rr >= p.min_rr and (not p.req_div or st_bear_dv):
                sig = dict(side="short", entry=entry, sl=sl, tp=tp, rr=rr)
                done_bear = True
        elif p.use_long and bull_bias and tc_bull and not done_bull and to_ok_l:
            sl, risk, tp, rr = build("long")
            if risk > 0 and risk <= p.max_risk_atr * a and rr is not None and rr >= p.min_rr and (not p.req_div or st_bull_dv):
                sig = dict(side="long", entry=entry, sl=sl, tp=tp, rr=rr)
                done_bull = True
        if sig:
            seq = f"{sa}-{sb}-{sc}"
            sig.update(i=i, ts=ts[i], atr=a, seq=seq, t369=t369, chain=chain,
                       info=(f"QS {seq}{' [369]' if t369 else ''}{' [C' + str(chain) + ']' if chain_ok else ''} | "
                             f"SSMT {p.stage_cyc}{'+' + ('Semanal' if stage_d else 'Diario') if p.use2 else ''} | tCISD | "
                             f"True Opens {'descuento ' + str(disc) if sig['side'] == 'long' else 'prima ' + str(prem)}/3"))
            signals[i] = sig
    return signals


# ============================================================================
# Tamano, backtest
# ============================================================================
def position_size(equity, risk_pct, max_lev, entry, sl):
    dist = abs(entry - sl)
    if dist <= 0 or entry <= 0 or equity <= 0:
        return 0.0
    return max(0.0, min(equity * risk_pct / 100.0 / dist, equity * max_lev / entry))


def backtest(c0, c1, c2, p: Params, equity0=1000.0, risk_pct=0.5, max_lev=3.0, quiet=False):
    sigs = compute_signals(c0, c1, c2, p)
    eq, peak, max_dd, pos, trades = equity0, equity0, 0.0, None, []
    fee, slip = p.fee_pct / 100.0, p.slippage_pct / 100.0
    for i, c in enumerate(c0):
        ts, o, h, l, cl, v = c
        if pos is not None and i > pos["i"]:
            dr = 1 if pos["side"] == "long" else -1
            ex_px = reason = None
            if (dr == 1 and l <= pos["sl"]) or (dr == -1 and h >= pos["sl"]):
                ex_px, reason = pos["sl"] * (1 - slip * dr), "SL"
            elif (dr == 1 and h >= pos["tp"]) or (dr == -1 and l <= pos["tp"]):
                ex_px, reason = pos["tp"], "TP"
            elif p.max_bars > 0 and i - pos["i"] >= p.max_bars:
                ex_px, reason = cl * (1 - slip * dr), "TIME"
            if ex_px is not None:
                pnl = (ex_px - pos["entry"]) * dr * pos["qty"] - pos["qty"] * (pos["entry"] + ex_px) * fee
                eq += pnl
                peak = max(peak, eq)
                max_dd = max(max_dd, (peak - eq) / peak * 100.0)
                trades.append(dict(ts=pos["ts"], side=pos["side"], entry=pos["entry"], exit=ex_px, reason=reason,
                                   pnl=pnl, r=pnl / pos["risk_cash"] if pos["risk_cash"] else 0.0, bars=i - pos["i"]))
                pos = None
        s = sigs[i]
        if pos is None and s is not None:
            dr = 1 if s["side"] == "long" else -1
            ep = s["entry"] * (1 + slip * dr)
            qty = position_size(eq, risk_pct, max_lev, ep, s["sl"])
            if qty > 0:
                pos = dict(i=i, ts=ts, side=s["side"], entry=ep, sl=s["sl"], tp=s["tp"], qty=qty, risk_cash=eq * risk_pct / 100.0)
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    gw = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    res = dict(trades=n, win_rate=(len(wins) / n * 100 if n else 0.0),
               profit_factor=(gw / gl if gl > 0 else (float("inf") if gw > 0 else 0.0)),
               net_pct=(eq / equity0 - 1) * 100, max_dd_pct=max_dd,
               avg_r=(sum(t["r"] for t in trades) / n if n else 0.0), final_equity=eq, trades_list=trades)
    if not quiet:
        print("\n=== RESULTADO BACKTEST ===")
        print(f"Operaciones: {n} | Win rate: {res['win_rate']:.1f}% | PF: {res['profit_factor']:.2f} | R medio: {res['avg_r']:.2f}")
        print(f"Neto: {res['net_pct']:.1f}% (capital final {eq:.2f}) | Max drawdown: {max_dd:.1f}%")
        for key in ("side", "reason"):
            g: Dict[str, List[dict]] = {}
            for t in trades:
                g.setdefault(str(t[key]), []).append(t)
            print(f"Por {key}: " + " | ".join(f"{k}: {len(v)} ops, {sum(x['r'] for x in v):.1f}R" for k, v in sorted(g.items())))
        if n < 100:
            print("AVISO: menos de 100 operaciones -> resultado poco fiable.")
    return res


# ============================================================================
# Datos y exchange
# ============================================================================
def make_exchange(cfg: dict, with_keys: bool):
    import ccxt  # importacion tardia
    ex_id = cfg.get("exchange", "bingx")
    opts: Dict[str, Any] = {"enableRateLimit": True, "options": {"defaultType": "swap"}}
    if with_keys:
        opts.update(apiKey=os.environ.get("BOT_API_KEY", ""), secret=os.environ.get("BOT_API_SECRET", ""))
        if os.environ.get("BOT_API_PASSWORD"):
            opts["password"] = os.environ["BOT_API_PASSWORD"]
    ex = getattr(ccxt, ex_id)(opts)
    if cfg.get("testnet") and with_keys:
        try:
            ex.set_sandbox_mode(True)
        except Exception as e:  # noqa
            raise SystemExit(f"{ex_id} no ofrece sandbox en ccxt ({e}). Pon BOT_TESTNET=false solo si quieres operar en real.")
    ex.load_markets()
    return ex


def fetch_all(ex, symbol, tf, since, tf_ms, limit=1000):
    out: Dict[int, List[float]] = {}
    now = int(time.time() * 1000)
    while since < now:
        batch = ex.fetch_ohlcv(symbol, tf, since=since, limit=limit)
        if not batch:
            break
        for b in batch:
            out[int(b[0])] = [int(b[0])] + [float(x) for x in b[1:6]]
        last = int(batch[-1][0])
        if last + tf_ms <= since:
            break
        since = last + tf_ms
        time.sleep(max(ex.rateLimit, 100) / 1000.0)
    return [out[k] for k in sorted(out)]


# ============================================================================
# Telegram
# ============================================================================
def _tg_api(method, params=None, timeout=10):
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        return None
    data = urllib.parse.urlencode(params or {}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=timeout) as r:
        return json.loads(r.read().decode())


def notify(text: str):
    log.info(text.replace("\n", " | "))
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not os.environ.get("TELEGRAM_TOKEN") or not chat:
        return
    try:
        _tg_api("sendMessage", {"chat_id": chat, "text": text[:3900]})
    except Exception as e:  # noqa
        log.warning("Telegram fallo: %s", e)


def tg_updates(offset: int) -> List[dict]:
    if not os.environ.get("TELEGRAM_TOKEN"):
        return []
    try:
        return (_tg_api("getUpdates", {"offset": offset, "timeout": 0}) or {}).get("result", [])
    except Exception as e:  # noqa
        log.warning("Telegram getUpdates fallo: %s", e)
        return []


def fmt_px(x: float) -> str:
    return f"{x:.6g}"


def signal_text(symbol: str, sig: dict, equity: float, risk_pct: float, max_lev: float) -> str:
    dr = 1 if sig["side"] == "long" else -1
    qty = position_size(equity, risk_pct, max_lev, sig["entry"], sig["sl"])
    when = datetime.fromtimestamp(sig["ts"] / 1000, tz=NY).strftime("%a %H:%M NY")
    return (f"{'🟢 LONG' if dr == 1 else '🔴 SHORT'} · QS-SSMT\n{symbol}  (vela 15m cerrada {when})\n"
            f"Entrada ≈ {fmt_px(sig['entry'])}\nSL {fmt_px(sig['sl'])} ({(sig['sl'] / sig['entry'] - 1) * 100:+.2f}%)\n"
            f"TP {fmt_px(sig['tp'])} ({(sig['tp'] / sig['entry'] - 1) * 100:+.2f}%)   R:R {sig['rr']:.2f}\n"
            f"{sig['info']}\n"
            f"Tamano sugerido: {qty:.6g} (riesgo {risk_pct}% de {equity:.0f} USDT = {equity * risk_pct / 100:.2f} USDT)")


# ============================================================================
# Bot
# ============================================================================
DEFAULT_CFG: Dict[str, Any] = {
    "exchange": "bingx",
    "testnet": True,
    "mode": "signals",                       # signals | paper | live
    "trade_symbol": "BTC/USDT:USDT",         # activo 1 (el que se opera)
    "triad": ["ETH/USDT:USDT", "SOL/USDT:USDT"],   # activos 2 y 3 (solo para el SSMT)
    "risk_pct": 0.5,
    "max_leverage": 3.0,
    "paper_equity": 1000.0,
    "max_daily_loss_pct": 3.0,
    "max_consecutive_losses": 3,
    "pause_minutes": 360,
    "hedge_mode": True,
    "sl_tp_mode": "separate",                # separate | attach
    "candles": 1000,
    "loop_delay_sec": 8,
    "data_dir": ".",
    "state_file": "state.json",
    "trades_log": "trades_log.csv",
    "params": {},
}


class Bot:
    def __init__(self, cfg: dict, p: Params, ex):
        self.cfg, self.p, self.ex = cfg, p, ex
        self.live = cfg["mode"] == "live"
        self.signals_only = cfg["mode"] == "signals"
        self.symbol = cfg["trade_symbol"]
        self.state = self._load_state()

    def _load_state(self) -> dict:
        path = self.cfg["state_file"]
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                log.warning("No se pudo leer %s, se empieza de cero", path)
        return dict(day="", day_start_equity=None, paper_equity=self.cfg["paper_equity"], positions={},
                    last_signal_ts=0, pause_until_ms=0, consec_losses=0, halt=False, tg_offset=0)

    def _save_state(self):
        tmp = self.cfg["state_file"] + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.cfg["state_file"])

    def equity(self) -> float:
        if not self.live:
            return float(self.state["paper_equity"])
        bal = self.ex.fetch_balance()
        tot = bal.get("total", {}).get("USDT")
        if tot is None:
            tot = bal.get("USDT", {}).get("total", 0.0)
        return float(tot or 0.0)

    def get_candles(self, now_ms: int):
        lim = self.cfg["candles"]
        res = []
        for s in [self.symbol] + list(self.cfg["triad"]):
            raw = self.ex.fetch_ohlcv(s, "15m", limit=lim)
            res.append([[int(x[0])] + [float(y) for y in x[1:6]] for x in raw if int(x[0]) + TF_MS <= now_ms])
        return res

    # ---- ciclo ----
    def tick(self, now_ms: Optional[int] = None):
        now_ms = now_ms or int(time.time() * 1000)
        day = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if self.state["day"] != day:
            eq_now = self.equity()
            if self.state["day"] and self.state["day_start_equity"] is not None and not self.signals_only:
                notify(f"📊 Resumen {self.state['day']}: equity {eq_now:.2f} USDT ({eq_now - self.state['day_start_equity']:+.2f} en el dia)")
            self.state["day"] = day
            self.state["day_start_equity"] = eq_now
        try:
            self.process(now_ms)
        except Exception as e:  # noqa
            log.exception("Error procesando: %s", e)
            notify(f"⚠️ Error en el ciclo: {e}")
        self._save_state()

    def entries_allowed(self, now_ms: int) -> Optional[str]:
        if os.path.exists("STOP"):
            return "archivo STOP presente"
        if self.state.get("halt"):
            return "bot detenido con /stop"
        if now_ms < self.state["pause_until_ms"]:
            return "en pausa por perdidas seguidas"
        eq, eq0 = self.equity(), self.state["day_start_equity"] or self.equity()
        if eq0 > 0 and eq < eq0 * (1 - self.cfg["max_daily_loss_pct"] / 100.0):
            return f"limite de perdida diaria ({eq:.2f} < {eq0:.2f})"
        if self.state["positions"]:
            return "ya hay una posicion abierta"
        if self.live:
            try:
                for q in self.ex.fetch_positions([self.symbol]):
                    if abs(float(q.get("contracts") or 0)) > 0:
                        return "hay una posicion abierta en el exchange que el bot no gestiona"
            except Exception as e:  # noqa
                return f"no se pudo comprobar posiciones ({e})"
        return None

    def process(self, now_ms: int):
        c0, c1, c2 = self.get_candles(now_ms)
        if len(c0) < 300 or len(c1) < 300 or len(c2) < 300:
            log.warning("Pocas velas (%d/%d/%d)", len(c0), len(c1), len(c2))
            return
        if self.symbol in self.state["positions"] and not self.signals_only:
            self.manage(c0)
        sig = compute_signals(c0, c1, c2, self.p)[-1]
        if not sig or sig["ts"] != c0[-1][0] or self.state["last_signal_ts"] == sig["ts"]:
            return
        self.state["last_signal_ts"] = sig["ts"]
        if self.signals_only:
            notify(signal_text(self.symbol, sig, float(self.cfg["paper_equity"]), self.cfg["risk_pct"], self.cfg["max_leverage"]))
            return
        why = self.entries_allowed(now_ms)
        if why:
            notify(f"⏭ Senal {sig['side'].upper()} IGNORADA ({why})\n{signal_text(self.symbol, sig, self.equity(), self.cfg['risk_pct'], self.cfg['max_leverage'])}")
            return
        self.open_position(sig, c0[-1][0])

    # ---- apertura ----
    def open_position(self, sig: dict, bar_ts: int):
        eq = self.equity()
        dr = 1 if sig["side"] == "long" else -1
        entry = sig["entry"] * (1 + self.p.slippage_pct / 100.0 * dr)
        qty = position_size(eq, self.cfg["risk_pct"], self.cfg["max_leverage"], entry, sig["sl"])
        if qty <= 0:
            return
        pos = dict(side=sig["side"], entry=entry, sl=sig["sl"], tp=sig["tp"], qty=qty, bar_ts=bar_ts, checked_ts=bar_ts,
                   equity_at_entry=eq, risk_cash=eq * self.cfg["risk_pct"] / 100.0)
        if self.live:
            ok, pos = self._live_entry(sig, pos)
            if not ok:
                return
        self.state["positions"][self.symbol] = pos
        notify(("💰 [LIVE] ENTRADA EJECUTADA\n" if self.live else "🧪 [PAPER] entrada simulada\n")
               + signal_text(self.symbol, dict(sig, entry=pos["entry"]), eq, self.cfg["risk_pct"], self.cfg["max_leverage"]))

    def _side_params(self, side: str, closing: bool = False) -> dict:
        prm: Dict[str, Any] = {}
        if self.cfg["hedge_mode"]:
            prm["positionSide"] = "LONG" if side == "long" else "SHORT"
        elif closing:
            prm["reduceOnly"] = True
        return prm

    def _live_entry(self, sig: dict, pos: dict):
        ex, symbol = self.ex, self.symbol
        market = ex.market(symbol)
        cs = float(market.get("contractSize") or 1.0)
        amount = float(ex.amount_to_precision(symbol, pos["qty"] / cs))
        mn = (market.get("limits", {}).get("amount", {}) or {}).get("min")
        mc = (market.get("limits", {}).get("cost", {}) or {}).get("min")
        if (mn and amount < mn) or (mc and amount * cs * pos["entry"] < mc):
            notify(f"⚠️ {symbol}: tamano {amount} por debajo del minimo del exchange, no se opera")
            return False, pos
        for fn, args in ((ex.set_margin_mode, ("isolated", symbol)), (ex.set_leverage, (int(max(1, round(self.cfg["max_leverage"]))), symbol))):
            try:
                fn(*args)
            except Exception:
                pass
        side = "buy" if sig["side"] == "long" else "sell"
        opp = "sell" if side == "buy" else "buy"
        sl_px = float(ex.price_to_precision(symbol, sig["sl"]))
        tp_px = float(ex.price_to_precision(symbol, sig["tp"]))
        prm = self._side_params(sig["side"])
        try:
            if self.cfg["sl_tp_mode"] == "attach":
                prm.update({"stopLoss": {"triggerPrice": sl_px}, "takeProfit": {"triggerPrice": tp_px}})
            order = ex.create_order(symbol, "market", side, amount, None, prm)
        except Exception as e:  # noqa
            notify(f"❌ {symbol}: ERROR al abrir la orden: {e}")
            return False, pos
        if self.cfg["sl_tp_mode"] != "attach":
            cprm = self._side_params(sig["side"], closing=True)
            try:        # el SL es obligatorio: si falla, se cierra la posicion
                ex.create_order(symbol, "market", opp, amount, None, dict(cprm, stopLossPrice=sl_px))
            except Exception as e:  # noqa
                notify(f"❌ {symbol}: no se pudo colocar el SL ({e}). Cierro la posicion por seguridad.")
                try:
                    self._close_market(pos, amount)
                except Exception as e2:  # noqa
                    notify(f"🚨 {symbol}: NO SE PUDO CERRAR. REVISA EL EXCHANGE YA. ({e2})")
                return False, pos
            try:
                ex.create_order(symbol, "market", opp, amount, None, dict(cprm, takeProfitPrice=tp_px))
            except Exception as e:  # noqa
                notify(f"⚠️ {symbol}: no se pudo colocar el TP ({e}). La posicion queda solo con SL.")
        fill = order.get("average") or order.get("price")
        if fill:
            pos["entry"] = float(fill)
        pos.update(amount=amount, sl=sl_px, tp=tp_px)
        return True, pos

    def _close_market(self, pos: dict, amount: Optional[float] = None):
        opp = "sell" if pos["side"] == "long" else "buy"
        amt = amount if amount is not None else pos.get("amount")
        self.ex.create_order(self.symbol, "market", opp, amt, None, self._side_params(pos["side"], closing=True))
        try:
            self.ex.cancel_all_orders(self.symbol)
        except Exception:
            pass

    # ---- gestion ----
    def manage(self, c0: List[List[float]]):
        pos = self.state["positions"][self.symbol]
        dr = 1 if pos["side"] == "long" else -1
        slip = self.p.slippage_pct / 100.0
        if self.live:
            return self._manage_live(pos, c0)
        for c in [x for x in c0 if x[0] > pos["checked_ts"]]:
            ts, o, h, l, cl, v = c
            pos["checked_ts"] = ts
            ex_px = reason = None
            if (dr == 1 and l <= pos["sl"]) or (dr == -1 and h >= pos["sl"]):
                ex_px, reason = pos["sl"] * (1 - slip * dr), "SL"
            elif (dr == 1 and h >= pos["tp"]) or (dr == -1 and l <= pos["tp"]):
                ex_px, reason = pos["tp"], "TP"
            elif self.p.max_bars > 0 and (ts - pos["bar_ts"]) // TF_MS >= self.p.max_bars:
                ex_px, reason = cl * (1 - slip * dr), "TIME"
            if ex_px is not None:
                fee = self.p.fee_pct / 100.0
                pnl = (ex_px - pos["entry"]) * dr * pos["qty"] - pos["qty"] * (pos["entry"] + ex_px) * fee
                self.state["paper_equity"] += pnl
                self._record_close(pos, ex_px, reason, pnl, ts)
                return

    def _manage_live(self, pos: dict, c0):
        try:
            plist = self.ex.fetch_positions([self.symbol])
        except Exception as e:  # noqa
            log.warning("fetch_positions fallo: %s", e)
            return
        open_ = [q for q in plist if abs(float(q.get("contracts") or 0)) > 0
                 and (not self.cfg["hedge_mode"] or str(q.get("side", "")).lower() == pos["side"])]
        if not open_:
            pnl = self.equity() - pos["equity_at_entry"]
            try:
                self.ex.cancel_all_orders(self.symbol)
            except Exception:
                pass
            self._record_close(pos, None, "EXCHANGE", pnl, c0[-1][0])
            return
        if self.p.max_bars > 0 and (c0[-1][0] - pos["bar_ts"]) // TF_MS >= self.p.max_bars:
            try:
                self._close_market(pos)
                notify(f"{self.symbol}: cierre por tiempo")
            except Exception as e:  # noqa
                notify(f"❌ {self.symbol}: ERROR cerrando por tiempo: {e}")

    def _record_close(self, pos: dict, exit_px, reason: str, pnl: float, ts: int):
        r = pnl / pos["risk_cash"] if pos["risk_cash"] else 0.0
        del self.state["positions"][self.symbol]
        self.state["consec_losses"] = self.state["consec_losses"] + 1 if pnl < -0.3 * pos["risk_cash"] else 0
        if self.state["consec_losses"] >= self.cfg["max_consecutive_losses"]:
            self.state["pause_until_ms"] = ts + self.cfg["pause_minutes"] * 60_000
            self.state["consec_losses"] = 0
            notify(f"⏸ Pausa de {self.cfg['pause_minutes']} min por perdidas seguidas")
        notify(f"{'✅' if pnl > 0 else '🔻'} {'[LIVE]' if self.live else '[PAPER]'} {self.symbol} cerrada por {reason}\nPnL {pnl:+.2f} USDT ({r:+.2f}R)")
        new_file = not os.path.exists(self.cfg["trades_log"])
        with open(self.cfg["trades_log"], "a", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["ts", "symbol", "side", "entry", "exit", "reason", "pnl", "r"])
            w.writerow([ts, self.symbol, pos["side"], pos["entry"], exit_px, reason, round(pnl, 6), round(r, 3)])

    # ---- Telegram ----
    def status_text(self) -> str:
        eq = self.equity()
        eq0 = self.state.get("day_start_equity") or eq
        lines = [f"Bot QS-SSMT · modo {self.cfg['mode']} · {self.cfg['exchange']}",
                 f"Opera: {self.symbol} | triada: {', '.join(self.cfg['triad'])}",
                 f"Equity: {eq:.2f} USDT ({eq - eq0:+.2f} hoy)",
                 f"Estado: {'⏸ DETENIDO (/resume)' if self.state.get('halt') else '▶️ activo'}"]
        if self.state["pause_until_ms"] > time.time() * 1000:
            lines.append("Pausa activa por perdidas seguidas")
        for sym, q in self.state["positions"].items():
            lines.append(f"Abierta: {sym} {q['side']} entrada {fmt_px(q['entry'])} SL {fmt_px(q['sl'])} TP {fmt_px(q['tp'])}")
        if not self.state["positions"]:
            lines.append("Sin posiciones abiertas")
        return "\n".join(lines)

    def poll_telegram(self):
        chat = os.environ.get("TELEGRAM_CHAT_ID")
        changed = False
        for u in tg_updates(self.state.get("tg_offset", 0)):
            self.state["tg_offset"] = u["update_id"] + 1
            changed = True
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != str(chat):
                continue
            txt = (msg.get("text") or "").strip().lower()
            if txt.startswith("/status"):
                notify(self.status_text())
            elif txt.startswith("/stop"):
                self.state["halt"] = True
                notify("⏸ Entradas detenidas. Las posiciones abiertas siguen con su SL/TP. /resume para reanudar.")
            elif txt.startswith("/resume"):
                self.state["halt"] = False
                notify("▶️ Entradas reanudadas.")
            elif txt.startswith("/help") or txt.startswith("/start"):
                notify("Comandos: /status  /stop  /resume")
        if changed:
            self._save_state()

    def run_forever(self):
        notify(f"🤖 Bot QS-SSMT iniciado · modo {self.cfg['mode']} · {self.cfg['exchange']}\n"
               f"Opera {self.symbol} (triada {', '.join(self.cfg['triad'])})\n"
               f"Riesgo {self.cfg['risk_pct']}% · apal. max {self.cfg['max_leverage']}x\nComandos: /status /stop /resume")
        step = TF_MS // 1000
        next_tick = (int(time.time() // step) + 1) * step + self.cfg["loop_delay_sec"]
        while True:
            if time.time() >= next_tick:
                self.tick()
                next_tick = (int(time.time() // step) + 1) * step + self.cfg["loop_delay_sec"]
            try:
                self.poll_telegram()
            except Exception as e:  # noqa
                log.warning("poll_telegram: %s", e)
            time.sleep(3)


# ============================================================================
# CLI
# ============================================================================
def _env_bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "si", "on")


def load_cfg(path: Optional[str]) -> dict:
    cfg = copy.deepcopy(DEFAULT_CFG)
    if path and os.path.exists(path):
        with open(path) as f:
            user = json.load(f)
        cfg.update({k: v for k, v in user.items() if k != "params"})
        cfg["params"] = user.get("params", {})
    env = os.environ
    if env.get("BOT_EXCHANGE"):
        cfg["exchange"] = env["BOT_EXCHANGE"].strip().lower()
    if env.get("BOT_MODE"):
        cfg["mode"] = env["BOT_MODE"].strip().lower()
    if env.get("BOT_TRADE_SYMBOL"):
        cfg["trade_symbol"] = env["BOT_TRADE_SYMBOL"].strip()
    if env.get("BOT_TRIAD"):
        cfg["triad"] = [x.strip() for x in env["BOT_TRIAD"].split(",") if x.strip()]
    for key, ek, cast in (("risk_pct", "BOT_RISK_PCT", float), ("max_leverage", "BOT_LEVERAGE", float),
                          ("paper_equity", "BOT_PAPER_EQUITY", float), ("max_daily_loss_pct", "BOT_MAX_DAILY_LOSS", float),
                          ("max_consecutive_losses", "BOT_MAX_CONSEC_LOSSES", int), ("pause_minutes", "BOT_PAUSE_MINUTES", int)):
        if env.get(ek):
            cfg[key] = cast(env[ek])
    if env.get("BOT_HEDGE"):
        cfg["hedge_mode"] = _env_bool(env["BOT_HEDGE"])
    if env.get("BOT_TESTNET"):
        cfg["testnet"] = _env_bool(env["BOT_TESTNET"])
    if env.get("BOT_SL_TP_MODE"):
        cfg["sl_tp_mode"] = env["BOT_SL_TP_MODE"].strip().lower()
    if env.get("BOT_PARAMS"):
        cfg["params"] = dict(cfg.get("params", {}), **json.loads(env["BOT_PARAMS"]))
    data_dir = env.get("DATA_DIR") or cfg["data_dir"]
    os.makedirs(data_dir, exist_ok=True)
    for k in ("state_file", "trades_log"):
        if not os.path.isabs(cfg[k]):
            cfg[k] = os.path.join(data_dir, cfg[k])
    if len(cfg["triad"]) != 2:
        raise SystemExit("BOT_TRIAD debe tener exactamente 2 simbolos (activos 2 y 3)")
    return cfg


def make_params(cfg: dict) -> Params:
    valid = {f.name for f in fields(Params)}
    bad = [k for k in cfg.get("params", {}) if k not in valid]
    if bad:
        raise SystemExit(f"Parametros desconocidos: {bad}")
    return Params(**cfg.get("params", {}))


def cmd_backtest(args):
    cfg = load_cfg(args.config)
    p = make_params(cfg)
    ex = make_exchange(cfg, with_keys=False)
    since = int((time.time() - (args.days + 10) * 86400) * 1000)
    data = []
    for s in [cfg["trade_symbol"]] + cfg["triad"]:
        print(f"Descargando {s} 15m ({args.days}+10 dias) de {cfg['exchange']} ...")
        data.append(fetch_all(ex, s, "15m", since, TF_MS))
    res = backtest(data[0], data[1], data[2], p, equity0=args.equity, risk_pct=cfg["risk_pct"], max_lev=cfg["max_leverage"])
    if args.out and res["trades_list"]:
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(res["trades_list"][0].keys()))
            w.writeheader()
            w.writerows(res["trades_list"])
        print(f"Operaciones guardadas en {args.out}")


def cmd_markets(args):
    ex = make_exchange({"exchange": args.exchange}, with_keys=False)
    f = args.filter.upper()
    found = [m for m, v in ex.markets.items() if v.get("swap") and f in m.upper()]
    print("\n".join(sorted(found)[:100]) or "Sin coincidencias")


def cmd_run(args, mode: Optional[str] = None):
    cfg = load_cfg(args.config)
    if mode and mode != "run":
        cfg["mode"] = mode
    mode = cfg["mode"]
    if mode not in ("signals", "paper", "live"):
        raise SystemExit(f"Modo desconocido: {mode}")
    p = make_params(cfg)
    if mode == "live" and not (getattr(args, "yes", False) or os.environ.get("BOT_CONFIRM_LIVE", "").upper() == "YES"):
        raise SystemExit("Modo live: define BOT_CONFIRM_LIVE=YES tras probar backtest y paper.")
    ex = make_exchange(cfg, with_keys=(mode == "live"))
    missing = [s for s in [cfg["trade_symbol"]] + cfg["triad"] if s not in ex.markets]
    if missing:
        hints = []
        for s in missing:
            base = s.split("/")[0]
            hints += [m for m in ex.markets if m.startswith(base) and ":USDT" in m][:3]
        msg = f"Simbolos no encontrados en {cfg['exchange']}: {missing}. Parecidos: {hints}"
        notify("❌ " + msg)
        raise SystemExit(msg)
    bot = Bot(cfg, p, ex)
    if mode == "live":
        notify(f"🔌 Conexion OK con {cfg['exchange']}{' (TESTNET)' if cfg['testnet'] else ' (REAL)'} · equity {bot.equity():.2f} USDT")
    bot.run_forever()


def main():
    ap = argparse.ArgumentParser(description="QS + SSMT + tCISD bot (15m)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backtest")
    b.add_argument("--config", default="config.json")
    b.add_argument("--days", type=int, default=90)
    b.add_argument("--equity", type=float, default=1000.0)
    b.add_argument("--out")
    m = sub.add_parser("markets")
    m.add_argument("--exchange", default="bingx")
    m.add_argument("--filter", default="")
    for name in ("signals", "paper", "live", "run"):
        sp = sub.add_parser(name)
        sp.add_argument("--config", default="config.json")
        sp.add_argument("--yes-i-understand-the-risk", dest="yes", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
    if args.cmd == "backtest":
        cmd_backtest(args)
    elif args.cmd == "markets":
        cmd_markets(args)
    else:
        cmd_run(args, args.cmd)


if __name__ == "__main__":
    main()
