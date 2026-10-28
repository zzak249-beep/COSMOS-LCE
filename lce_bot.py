#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LCE BOT  -  Nivel -> Confirmacion -> Ejecucion  (5 minutos, perpetuos USDT)

Port a Python de "LCE Zone Lab + Senales" (Pine):
  * Cajas de oferta/demanda por pivotes (grosor ATR, fusion de solapadas, caducidad opcional)
  * Maquina de estados por caja: toque -> rebote / ruptura, con nota de calidad A/B/C
  * Senales:
        RV = rebote confirmado en caja de nota A/B (por defecto hasta el 3er toque)
        BO = ruptura de caja + nube EMA 9/21 + tendencia 1H + volumen + cuerpo + pendiente
  * SL tras la caja/nube, TP en la siguiente caja (un poco antes) o R:R de reserva
  * Riesgo fijo por operacion, limite diario, pausa tras perdidas seguidas, stop por tiempo

Modos:
  selftest   prueba con datos sinteticos (sin red, sin claves)
  backtest   descarga velas publicas (ccxt) o lee CSV y simula con comisiones y slippage
  signals    SOLO avisa por Telegram de cada senal (sin claves, sin operar)
  paper      sigue el mercado en vivo con velas publicas y opera SIMULADO (sin claves)
  live       opera de verdad (requiere claves y confirmacion)
  run        usa el modo definido en BOT_MODE / config (para Railway)

AVISO: no es asesoramiento financiero. Empieza SIEMPRE en backtest y paper.
"""
import argparse
import bisect
import csv
import json
import logging
import math
import os
import random
import sys
import time
import urllib.parse
import urllib.request
import copy
from dataclasses import dataclass, asdict, fields
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

TF_MS = 300_000          # 5 minutos
HTF_MS = 3_600_000       # 1 hora
log = logging.getLogger("lce")


# ----------------------------------------------------------------------------
# Parametros de la estrategia (equivalentes a los inputs del Pine)
# ----------------------------------------------------------------------------
@dataclass
class Params:
    # cajas
    pivot_len: int = 10
    zone_mult: float = 0.6
    max_zones: int = 10
    zone_max_age: int = 0          # 0 = sin caducidad
    dep_min: float = 0.0           # salida impulsiva minima para crear la caja (x ATR)
    dep_strong: float = 3.0
    dep_ok: float = 1.5
    age_good: int = 150
    vol_orig_mult: float = 1.3
    max_inside: int = 10
    # nube / tendencia
    ema_fast: int = 9
    ema_slow: int = 21
    slope_bars: int = 3
    use_htf: bool = True
    # senales
    use_bo: bool = True
    use_rv: bool = True
    allow_long: bool = True
    allow_short: bool = True
    min_grade: str = "A+B"         # Todas | A+B | Solo A
    max_touch_sig: int = 3
    need_wick: bool = True
    wick_ratio: float = 0.35
    rv_with_htf: bool = False
    use_vol: bool = True
    vol_mult: float = 1.2
    body_mult: float = 0.5
    use_slope: bool = True
    # gestion
    atr_len: int = 14
    sl_buffer: float = 0.3
    tp_buffer: float = 0.1
    min_rr: float = 1.2
    rr_fallback: float = 2.0
    max_risk_atr: float = 3.0      # 0 = sin limite
    cooldown: int = 6
    max_bars: int = 48             # stop por tiempo (0 = off)
    cost_mult: float = 3.0         # el TP debe superar costes x (0 = off)
    fee_pct: float = 0.05          # comision por lado (%)
    slippage_pct: float = 0.03     # slippage por lado (%)


# ----------------------------------------------------------------------------
# Indicadores (sin dependencias)
# ----------------------------------------------------------------------------
def ema(vals: List[float], n: int) -> List[float]:
    a = 2.0 / (n + 1.0)
    out, prev = [], None
    for v in vals:
        prev = v if prev is None else a * v + (1 - a) * prev
        out.append(prev)
    return out


def sma(vals: List[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rma(vals: List[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(vals)
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


def atr_series(h, l, c, n) -> List[Optional[float]]:
    tr = [h[0] - l[0]]
    for i in range(1, len(c)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return rma(tr, n)


def resample(c5: List[List[float]], tf_ms: int = HTF_MS) -> List[List[float]]:
    out: List[List[float]] = []
    cur = None
    for ts, o, h, l, c, v in c5:
        b = ts - ts % tf_ms
        if cur is None or cur[0] != b:
            if cur is not None:
                out.append(cur)
            cur = [b, o, h, l, c, v]
        else:
            cur[2] = max(cur[2], h)
            cur[3] = min(cur[3], l)
            cur[4] = c
            cur[5] += v
    if cur is not None:
        out.append(cur)
    return out


def htf_flags(ts5: List[int], htf: List[List[float]], p: Params):
    """Tendencia del TF superior sin repintado: EMA de la ultima vela 1H CERRADA."""
    n = len(ts5)
    if not htf:
        return [False] * n, [False] * n
    hts = [x[0] for x in htf]
    hc = [x[4] for x in htf]
    ef, es = ema(hc, p.ema_fast), ema(hc, p.ema_slow)
    bull, bear = [], []
    for t in ts5:
        k = bisect.bisect_right(hts, t) - 2      # -1 = vela que contiene t, -2 = ultima cerrada
        if k < p.ema_slow:
            bull.append(False)
            bear.append(False)
        else:
            bull.append(ef[k] > es[k])
            bear.append(ef[k] < es[k])
    return bull, bear


# ----------------------------------------------------------------------------
# Motor de senales
# ----------------------------------------------------------------------------
def _score(z: dict, i: int, aligned: bool, p: Params) -> int:
    dep = z["dep"]
    pts = 2 if dep >= p.dep_strong else (1 if dep >= p.dep_ok else 0)
    pts += 2 if z["touches"] == 0 else (1 if z["touches"] == 1 else 0)
    pts += 1 if (i - z["created"]) <= p.age_good else 0
    pts += 1 if z["vol_ok"] else 0
    pts += 1 if aligned else 0
    return pts


def _grade(score: int) -> int:
    return 0 if score >= 5 else (1 if score >= 3 else 2)


def compute_signals(c5: List[List[float]], htf: List[List[float]], p: Params) -> List[Optional[dict]]:
    n = len(c5)
    ts = [int(x[0]) for x in c5]
    o = [x[1] for x in c5]
    h = [x[2] for x in c5]
    l = [x[3] for x in c5]
    cl = [x[4] for x in c5]
    v = [x[5] for x in c5]
    atr = atr_series(h, l, cl, p.atr_len)
    ef, es = ema(cl, p.ema_fast), ema(cl, p.ema_slow)
    vavg = sma(v, 20)
    hb, hbear = htf_flags(ts, htf, p)

    L = p.pivot_len
    warm = max(p.ema_slow, p.atr_len, 20, p.slope_bars) + 2 * L + 5
    zones: List[dict] = []
    signals: List[Optional[dict]] = [None] * n
    last_sig = -10 ** 9
    start = max(p.atr_len, 20)

    for i in range(start, n):
        a = atr[i]
        if a is None or vavg[i] is None:
            continue

        # ---- crear cajas con el pivote confirmado en la vela i ----
        j = i - L
        if j - L >= 0:
            if h[j] >= max(h[j - L:j]) and h[j] > max(h[j + 1:i + 1]):
                dep = (h[j] - min(l[j + 1:i + 1])) / a
                if dep >= p.dep_min:
                    top, bot = h[j], h[j] - a * p.zone_mult
                    if not any(z["active"] and z["typ"] == 1 and bot < z["top"] and top > z["bot"] for z in zones):
                        zones.append(dict(top=top, bot=bot, typ=1, active=True, created=i, dep=dep,
                                          vol_ok=v[j] >= vavg[i] * p.vol_orig_mult, touches=0,
                                          inside=False, t_bar=0, t_grade=2))
            if l[j] <= min(l[j - L:j]) and l[j] < min(l[j + 1:i + 1]):
                dep = (max(h[j + 1:i + 1]) - l[j]) / a
                if dep >= p.dep_min:
                    top, bot = l[j] + a * p.zone_mult, l[j]
                    if not any(z["active"] and z["typ"] == -1 and bot < z["top"] and top > z["bot"] for z in zones):
                        zones.append(dict(top=top, bot=bot, typ=-1, active=True, created=i, dep=dep,
                                          vol_ok=v[j] >= vavg[i] * p.vol_orig_mult, touches=0,
                                          inside=False, t_bar=0, t_grade=2))
            while len(zones) > p.max_zones:
                zones.pop(0)

        # ---- maquina de estados de las cajas ----
        brk_up = brk_dn = bnc_up = bnc_dn = False
        bnc_up_grade = bnc_dn_grade = 2
        bnc_up_touch = bnc_dn_touch = 0
        bnc_up_bot = bnc_dn_top = None
        near_up = near_dn = None            # (distancia, caja)

        for z in zones:
            if not z["active"]:
                continue
            if p.zone_max_age > 0 and i - z["created"] > p.zone_max_age:
                z["active"] = False
                continue
            t, b, typ = z["top"], z["bot"], z["typ"]
            touched = (h[i] >= b) if typ == 1 else (l[i] <= t)
            aligned = hbear[i] if typ == 1 else hb[i]
            if not z["inside"] and touched:
                z["t_grade"] = _grade(_score(z, i, aligned, p))
                z["touches"] += 1
                z["inside"] = True
                z["t_bar"] = i
            if z["inside"]:
                brk = (cl[i] > t) if typ == 1 else (cl[i] < b)
                bnc = (cl[i] < b) if typ == 1 else (cl[i] > t)
                if brk:
                    z["active"] = False
                    z["inside"] = False
                    if typ == 1:
                        brk_up = True
                    else:
                        brk_dn = True
                elif bnc:
                    z["inside"] = False
                    if typ == 1:
                        bnc_dn = True
                        bnc_dn_grade = min(bnc_dn_grade, z["t_grade"])
                        bnc_dn_touch = z["touches"]
                        bnc_dn_top = t if bnc_dn_top is None else max(bnc_dn_top, t)
                    else:
                        bnc_up = True
                        bnc_up_grade = min(bnc_up_grade, z["t_grade"])
                        bnc_up_touch = z["touches"]
                        bnc_up_bot = b if bnc_up_bot is None else min(bnc_up_bot, b)
                elif i - z["t_bar"] >= p.max_inside:
                    z["inside"] = False
            if z["active"]:
                if typ == 1 and b > cl[i]:
                    d = (b - cl[i]) / a
                    if near_up is None or d < near_up[0]:
                        near_up = (d, z)
                if typ == -1 and t < cl[i]:
                    d = (cl[i] - t) / a
                    if near_dn is None or d < near_dn[0]:
                        near_dn = (d, z)

        # ---- senales ----
        if i < warm:
            continue
        rng = max(h[i] - l[i], 1e-12)
        body = abs(cl[i] - o[i])
        up_wick = h[i] - max(o[i], cl[i])
        dn_wick = min(o[i], cl[i]) - l[i]
        strong_body = body >= a * p.body_mult
        vol_ok = (not p.use_vol) or v[i] > vavg[i] * p.vol_mult
        bull_cloud = ef[i] > es[i] and cl[i] > es[i]
        bear_cloud = ef[i] < es[i] and cl[i] < es[i]
        slope_up = es[i] > es[i - p.slope_bars]
        slope_dn = es[i] < es[i - p.slope_bars]

        def grade_ok(g: int) -> bool:
            return p.min_grade == "Todas" or (p.min_grade == "A+B" and g <= 1) or (p.min_grade == "Solo A" and g == 0)

        long_rv = (p.use_rv and bnc_up and grade_ok(bnc_up_grade) and bnc_up_touch <= p.max_touch_sig
                   and (not p.need_wick or (cl[i] > o[i] and dn_wick >= p.wick_ratio * rng))
                   and (not p.rv_with_htf or hb[i]))
        short_rv = (p.use_rv and bnc_dn and grade_ok(bnc_dn_grade) and bnc_dn_touch <= p.max_touch_sig
                    and (not p.need_wick or (cl[i] < o[i] and up_wick >= p.wick_ratio * rng))
                    and (not p.rv_with_htf or hbear[i]))
        long_bo = (p.use_bo and brk_up and bull_cloud and (not p.use_htf or hb[i]) and vol_ok
                   and strong_body and (not p.use_slope or slope_up))
        short_bo = (p.use_bo and brk_dn and bear_cloud and (not p.use_htf or hbear[i]) and vol_ok
                    and strong_body and (not p.use_slope or slope_dn))

        long_cand = long_bo or long_rv
        short_cand = (short_bo or short_rv) and not long_cand
        if not (long_cand or short_cand) or (i - last_sig) <= p.cooldown:
            continue

        entry = cl[i]
        next_sup = near_up[1]["bot"] if near_up else None
        next_dem = near_dn[1]["top"] if near_dn else None
        sig = None
        if long_cand:
            if long_bo:
                sl, kind, grade = es[i] - a * p.sl_buffer, "BO", None
            else:
                base = bnc_up_bot if bnc_up_bot is not None else l[i]
                sl, kind, grade = min(l[i], base) - a * p.sl_buffer, "RV", "ABC"[bnc_up_grade]
            if sl >= entry:
                sl = entry - a
            risk = entry - sl
            tp = entry + risk * p.rr_fallback if next_sup is None else next_sup - a * p.tp_buffer
            rr = (tp - entry) / risk
            if rr >= p.min_rr:
                sig = dict(side="long", kind=kind, grade=grade, entry=entry, sl=sl, tp=tp, rr=rr)
        else:
            if short_bo:
                sl, kind, grade = es[i] + a * p.sl_buffer, "BO", None
            else:
                base = bnc_dn_top if bnc_dn_top is not None else h[i]
                sl, kind, grade = max(h[i], base) + a * p.sl_buffer, "RV", "ABC"[bnc_dn_grade]
            if sl <= entry:
                sl = entry + a
            risk = sl - entry
            tp = entry - risk * p.rr_fallback if next_dem is None else next_dem + a * p.tp_buffer
            rr = (entry - tp) / risk
            if rr >= p.min_rr:
                sig = dict(side="short", kind=kind, grade=grade, entry=entry, sl=sl, tp=tp, rr=rr)

        if sig is None:
            continue
        last_sig = i                                   # igual que en Pine: cuenta aunque luego no se opere
        # filtros propios del bot (seguridad)
        risk_abs = abs(sig["entry"] - sig["sl"])
        if p.max_risk_atr > 0 and risk_abs > a * p.max_risk_atr:
            continue
        reward_pct = abs(sig["tp"] - sig["entry"]) / sig["entry"] * 100.0
        if p.cost_mult > 0 and reward_pct < p.cost_mult * 2 * (p.fee_pct + p.slippage_pct):
            continue
        if (sig["side"] == "long" and not p.allow_long) or (sig["side"] == "short" and not p.allow_short):
            continue
        sig.update(i=i, ts=ts[i], atr=a)
        signals[i] = sig
    return signals


# ----------------------------------------------------------------------------
# Backtest
# ----------------------------------------------------------------------------
def position_size(equity: float, risk_pct: float, max_lev: float, entry: float, sl: float) -> float:
    dist = abs(entry - sl)
    if dist <= 0 or entry <= 0 or equity <= 0:
        return 0.0
    return max(0.0, min(equity * risk_pct / 100.0 / dist, equity * max_lev / entry))


def backtest(c5, htf, p: Params, equity0=1000.0, risk_pct=1.0, max_lev=5.0, quiet=False) -> Dict[str, Any]:
    sigs = compute_signals(c5, htf, p)
    eq, peak, max_dd = equity0, equity0, 0.0
    pos = None
    trades: List[dict] = []
    fee, slip = p.fee_pct / 100.0, p.slippage_pct / 100.0
    for i, c in enumerate(c5):
        ts, o, h, l, cl, v = c
        if pos is not None and i > pos["i"]:
            d = 1 if pos["side"] == "long" else -1
            ex_px, reason = None, None
            if (d == 1 and l <= pos["sl"]) or (d == -1 and h >= pos["sl"]):
                ex_px, reason = pos["sl"] * (1 - slip * d), "SL"
            elif (d == 1 and h >= pos["tp"]) or (d == -1 and l <= pos["tp"]):
                ex_px, reason = pos["tp"], "TP"
            elif p.max_bars > 0 and i - pos["i"] >= p.max_bars:
                ex_px, reason = cl * (1 - slip * d), "TIME"
            if ex_px is not None:
                pnl = (ex_px - pos["entry"]) * d * pos["qty"] - pos["qty"] * (pos["entry"] + ex_px) * fee
                eq += pnl
                peak = max(peak, eq)
                max_dd = max(max_dd, (peak - eq) / peak * 100.0)
                trades.append(dict(ts=pos["ts"], side=pos["side"], kind=pos["kind"], grade=pos["grade"],
                                   entry=pos["entry"], exit=ex_px, reason=reason, pnl=pnl,
                                   r=pnl / pos["risk_cash"] if pos["risk_cash"] else 0.0, bars=i - pos["i"]))
                pos = None
        s = sigs[i]
        if pos is None and s is not None:
            d = 1 if s["side"] == "long" else -1
            entry_px = s["entry"] * (1 + slip * d)
            qty = position_size(eq, risk_pct, max_lev, entry_px, s["sl"])
            if qty > 0:
                pos = dict(i=i, ts=ts, side=s["side"], kind=s["kind"], grade=s["grade"], entry=entry_px,
                           sl=s["sl"], tp=s["tp"], qty=qty, risk_cash=eq * risk_pct / 100.0)
    return summarize(trades, equity0, eq, max_dd, quiet)


def summarize(trades, equity0, eq, max_dd, quiet=False) -> Dict[str, Any]:
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    res = dict(trades=n, win_rate=(len(wins) / n * 100 if n else 0.0),
               profit_factor=(gross_w / gross_l if gross_l > 0 else float("inf") if gross_w > 0 else 0.0),
               net_pct=(eq / equity0 - 1) * 100, max_dd_pct=max_dd,
               avg_r=(sum(t["r"] for t in trades) / n if n else 0.0), final_equity=eq, trades_list=trades)
    if not quiet:
        print("\n=== RESULTADO BACKTEST ===")
        print(f"Operaciones : {n}")
        print(f"Win rate    : {res['win_rate']:.1f}%")
        print(f"Profit factor: {res['profit_factor']:.2f}")
        print(f"R medio     : {res['avg_r']:.2f}")
        print(f"Neto        : {res['net_pct']:.1f}%   (capital final {eq:.2f})")
        print(f"Max drawdown: {max_dd:.1f}%")
        for key in ("kind", "side", "reason"):
            groups: Dict[str, List[dict]] = {}
            for t in trades:
                groups.setdefault(str(t[key]), []).append(t)
            line = " | ".join(f"{k}: {len(g)} ops, {sum(1 for x in g if x['pnl'] > 0) / len(g) * 100:.0f}% win, {sum(x['r'] for x in g):.1f}R"
                              for k, g in sorted(groups.items()))
            print(f"Por {key:6s}: {line}")
        if n < 100:
            print("AVISO: menos de 100 operaciones, resultado poco fiable.")
    return res


# ----------------------------------------------------------------------------
# Datos
# ----------------------------------------------------------------------------
def load_csv(path: str) -> List[List[float]]:
    out = []
    with open(path, newline="") as f:
        for row in csv.reader(f):
            try:
                out.append([int(float(row[0])), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])])
            except (ValueError, IndexError):
                continue
    out.sort(key=lambda x: x[0])
    return out


def make_exchange(cfg: dict, with_keys: bool):
    import ccxt  # importacion tardia: no hace falta para selftest
    ex_id = cfg.get("exchange", "bybit")
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


def fetch_all(ex, symbol: str, tf: str, since: int, tf_ms: int, limit: int = 1000) -> List[List[float]]:
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


def synthetic(n: int = 20000, seed: int = 7, start_ts: int = 1_700_000_000_000) -> List[List[float]]:
    """Datos sinteticos con regimenes de tendencia/rango y volumen ligado al movimiento."""
    rnd = random.Random(seed)
    price, out, drift, vol = 100.0, [], 0.0, 0.002
    start_ts -= start_ts % TF_MS
    for k in range(n):
        if k % 400 == 0:
            drift = rnd.choice([-0.0004, 0.0, 0.0, 0.0005])
            vol = rnd.choice([0.0015, 0.0025, 0.004])
        o = price
        ret = rnd.gauss(drift, vol)
        c = o * (1 + ret)
        hi = max(o, c) * (1 + abs(rnd.gauss(0, vol * 0.5)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, vol * 0.5)))
        v = 1000 * (1 + abs(ret) / vol * rnd.uniform(0.3, 1.5))
        out.append([start_ts + k * TF_MS, o, hi, lo, c, v])
        price = c
    return out


# ----------------------------------------------------------------------------
# Notificaciones
# ----------------------------------------------------------------------------
def _tg_api(method: str, params: Optional[dict] = None, timeout: int = 10):
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
        res = _tg_api("getUpdates", {"offset": offset, "timeout": 0})
        return (res or {}).get("result", [])
    except Exception as e:  # noqa
        log.warning("Telegram getUpdates fallo: %s", e)
        return []


def fmt_px(x: float) -> str:
    return f"{x:.6g}"


def signal_text(symbol: str, sig: dict, equity: float, risk_pct: float, max_lev: float) -> str:
    d = 1 if sig["side"] == "long" else -1
    risk_dist = abs(sig["entry"] - sig["sl"])
    qty = position_size(equity, risk_pct, max_lev, sig["entry"], sig["sl"])
    sl_pct = (sig["sl"] / sig["entry"] - 1) * 100
    tp_pct = (sig["tp"] / sig["entry"] - 1) * 100
    kind = sig["kind"] + (f" (caja {sig['grade']})" if sig["grade"] else "")
    when = datetime.fromtimestamp(sig["ts"] / 1000, tz=timezone.utc).strftime("%H:%M UTC")
    return (f"{'🟢 LONG' if d == 1 else '🔴 SHORT'} · {kind}\n{symbol}  (vela 5m cerrada {when})\n"
            f"Entrada ≈ {fmt_px(sig['entry'])}\nSL {fmt_px(sig['sl'])} ({sl_pct:+.2f}%)\n"
            f"TP {fmt_px(sig['tp'])} ({tp_pct:+.2f}%)   R:R {sig['rr']:.2f}\n"
            f"Tamano sugerido: {qty:.6g} (riesgo {risk_pct}% de {equity:.0f} USDT = {equity * risk_pct / 100:.2f} USDT)")


# ----------------------------------------------------------------------------
# Bot (paper / live)
# ----------------------------------------------------------------------------
DEFAULT_CFG: Dict[str, Any] = {
    "exchange": "bingx",
    "testnet": True,
    "symbols": ["BULL/USDT:USDT"],
    "mode": "paper",
    "risk_pct": 0.75,
    "max_leverage": 3.0,
    "paper_equity": 1000.0,
    "max_open_positions": 2,
    "max_daily_loss_pct": 3.0,
    "max_consecutive_losses": 3,
    "pause_minutes": 180,
    "hedge_mode": True,             # BingX trae el modo cobertura (LONG/SHORT) por defecto
    "sl_tp_mode": "separate",        # separate | attach
    "candles_5m": 900,
    "candles_1h": 200,
    "loop_delay_sec": 5,
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
        self.state = self._load_state()

    # ---- estado ----
    def _load_state(self) -> dict:
        path = self.cfg["state_file"]
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                log.warning("No se pudo leer %s, se empieza de cero", path)
        return dict(day="", day_start_equity=None, paper_equity=self.cfg["paper_equity"], positions={},
                    last_signal_ts={}, pause_until_ms=0, consec_losses=0, halt=False, tg_offset=0)

    def _save_state(self):
        tmp = self.cfg["state_file"] + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.cfg["state_file"])

    # ---- equity ----
    def equity(self) -> float:
        if not self.live:
            return float(self.state["paper_equity"])
        bal = self.ex.fetch_balance()
        tot = bal.get("total", {}).get("USDT")
        if tot is None:
            tot = bal.get("USDT", {}).get("total", 0.0)
        return float(tot or 0.0)

    # ---- datos ----
    def get_candles(self, symbol: str, now_ms: int):
        raw5 = self.ex.fetch_ohlcv(symbol, "5m", limit=self.cfg["candles_5m"])
        raw1h = self.ex.fetch_ohlcv(symbol, "1h", limit=self.cfg["candles_1h"])
        c5 = [[int(x[0])] + [float(y) for y in x[1:6]] for x in raw5 if int(x[0]) + TF_MS <= now_ms]
        h1 = [[int(x[0])] + [float(y) for y in x[1:6]] for x in raw1h]
        return c5, h1

    # ---- ciclo ----
    def tick(self, now_ms: Optional[int] = None):
        now_ms = now_ms or int(time.time() * 1000)
        day = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if self.state["day"] != day:
            eq_now = self.equity()
            if self.state["day"] and self.state["day_start_equity"] is not None and not self.signals_only:
                notify(f"📊 Resumen {self.state['day']}: equity {eq_now:.2f} USDT "
                       f"({eq_now - self.state['day_start_equity']:+.2f} en el dia)")
            self.state["day"] = day
            self.state["day_start_equity"] = eq_now
        for symbol in self.cfg["symbols"]:
            try:
                self.process(symbol, now_ms)
            except Exception as e:  # noqa
                log.exception("Error procesando %s: %s", symbol, e)
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
        if len(self.state["positions"]) >= self.cfg["max_open_positions"]:
            return "maximo de posiciones abiertas"
        return None

    def process(self, symbol: str, now_ms: int):
        c5, h1 = self.get_candles(symbol, now_ms)
        if len(c5) < 120:
            log.warning("%s: pocas velas (%d)", symbol, len(c5))
            return
        if symbol in self.state["positions"] and not self.signals_only:
            self.manage(symbol, c5)
        if symbol in self.state["positions"]:
            return
        sigs = compute_signals(c5, h1, self.p)
        sig = sigs[-1]
        if not sig or sig["ts"] != c5[-1][0]:
            return
        if self.state["last_signal_ts"].get(symbol) == sig["ts"]:
            return
        self.state["last_signal_ts"][symbol] = sig["ts"]
        if self.signals_only:
            notify(signal_text(symbol, sig, float(self.cfg["paper_equity"]), self.cfg["risk_pct"], self.cfg["max_leverage"]))
            return
        why = self.entries_allowed(now_ms)
        if why:
            notify(f"{symbol}: senal {sig['side'].upper()} {sig['kind']} IGNORADA ({why})")
            return
        self.open_position(symbol, sig, c5[-1][0])

    # ---- apertura ----
    def open_position(self, symbol: str, sig: dict, bar_ts: int):
        eq = self.equity()
        d = 1 if sig["side"] == "long" else -1
        slip = self.p.slippage_pct / 100.0
        entry = sig["entry"] * (1 + slip * d)
        qty = position_size(eq, self.cfg["risk_pct"], self.cfg["max_leverage"], entry, sig["sl"])
        if qty <= 0:
            return
        pos = dict(side=sig["side"], kind=sig["kind"], grade=sig["grade"], entry=entry, sl=sig["sl"], tp=sig["tp"],
                   qty=qty, bar_ts=bar_ts, checked_ts=bar_ts, equity_at_entry=eq, risk_cash=eq * self.cfg["risk_pct"] / 100.0)
        if self.live:
            ok, pos = self._live_entry(symbol, sig, pos)
            if not ok:
                return
        self.state["positions"][symbol] = pos
        notify(("💰 [LIVE] ENTRADA EJECUTADA\n" if self.live else "🧪 [PAPER] entrada simulada\n")
               + signal_text(symbol, dict(sig, entry=pos["entry"]), eq, self.cfg["risk_pct"], self.cfg["max_leverage"]))

    def _side_params(self, sig_side: str, closing: bool = False) -> dict:
        prm: Dict[str, Any] = {}
        if self.cfg["hedge_mode"]:
            prm["positionSide"] = "LONG" if sig_side == "long" else "SHORT"
        elif closing:
            prm["reduceOnly"] = True      # en modo cobertura el cierre lo define positionSide
        return prm

    def _live_entry(self, symbol: str, sig: dict, pos: dict):
        ex = self.ex
        market = ex.market(symbol)
        cs = float(market.get("contractSize") or 1.0)
        amount = float(ex.amount_to_precision(symbol, pos["qty"] / cs))
        mn = (market.get("limits", {}).get("amount", {}) or {}).get("min")
        mc = (market.get("limits", {}).get("cost", {}) or {}).get("min")
        if (mn and amount < mn) or (mc and amount * cs * pos["entry"] < mc):
            notify(f"⚠️ {symbol}: tamano {amount} por debajo del minimo del exchange, no se opera")
            return False, pos
        try:
            ex.set_margin_mode("isolated", symbol)
        except Exception:
            pass
        try:
            ex.set_leverage(int(max(1, round(self.cfg["max_leverage"]))), symbol)
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
            else:
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
                    self._close_market(symbol, pos, amount)
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

    def _close_market(self, symbol: str, pos: dict, amount: Optional[float] = None):
        opp = "sell" if pos["side"] == "long" else "buy"
        amt = amount if amount is not None else pos.get("amount")
        self.ex.create_order(symbol, "market", opp, amt, None, self._side_params(pos["side"], closing=True))
        try:
            self.ex.cancel_all_orders(symbol)
        except Exception:
            pass

    # ---- gestion ----
    def manage(self, symbol: str, c5: List[List[float]]):
        pos = self.state["positions"][symbol]
        d = 1 if pos["side"] == "long" else -1
        slip = self.p.slippage_pct / 100.0
        new = [c for c in c5 if c[0] > pos["checked_ts"]]
        if self.live:
            self._manage_live(symbol, pos, new, c5)
            return
        for c in new:
            ts, o, h, l, cl, v = c
            pos["checked_ts"] = ts
            ex_px, reason = None, None
            if (d == 1 and l <= pos["sl"]) or (d == -1 and h >= pos["sl"]):
                ex_px, reason = pos["sl"] * (1 - slip * d), "SL"
            elif (d == 1 and h >= pos["tp"]) or (d == -1 and l <= pos["tp"]):
                ex_px, reason = pos["tp"], "TP"
            elif self.p.max_bars > 0 and (ts - pos["bar_ts"]) // TF_MS >= self.p.max_bars:
                ex_px, reason = cl * (1 - slip * d), "TIME"
            if ex_px is not None:
                fee = self.p.fee_pct / 100.0
                pnl = (ex_px - pos["entry"]) * d * pos["qty"] - pos["qty"] * (pos["entry"] + ex_px) * fee
                self.state["paper_equity"] += pnl
                self._record_close(symbol, pos, ex_px, reason, pnl, ts)
                return

    def _manage_live(self, symbol: str, pos: dict, new: List[List[float]], c5):
        try:
            plist = self.ex.fetch_positions([symbol])
        except Exception as e:  # noqa
            log.warning("fetch_positions fallo: %s", e)
            return
        open_ = [q for q in plist if abs(float(q.get("contracts") or 0)) > 0
                 and (not self.cfg["hedge_mode"] or str(q.get("side", "")).lower() == pos["side"])]
        if not open_:   # cerrada por SL/TP o a mano
            eq = self.equity()
            pnl = eq - pos["equity_at_entry"]
            try:
                self.ex.cancel_all_orders(symbol)
            except Exception:
                pass
            self._record_close(symbol, pos, None, "EXCHANGE", pnl, c5[-1][0])
            return
        if self.p.max_bars > 0 and (c5[-1][0] - pos["bar_ts"]) // TF_MS >= self.p.max_bars:
            try:
                self._close_market(symbol, pos)
                notify(f"{symbol}: cierre por tiempo")
            except Exception as e:  # noqa
                notify(f"{symbol}: ERROR cerrando por tiempo: {e}")

    def _record_close(self, symbol: str, pos: dict, exit_px, reason: str, pnl: float, ts: int):
        r = pnl / pos["risk_cash"] if pos["risk_cash"] else 0.0
        del self.state["positions"][symbol]
        if pnl < -0.3 * pos["risk_cash"]:
            self.state["consec_losses"] += 1
        else:
            self.state["consec_losses"] = 0
        if self.state["consec_losses"] >= self.cfg["max_consecutive_losses"]:
            self.state["pause_until_ms"] = ts + self.cfg["pause_minutes"] * 60_000
            self.state["consec_losses"] = 0
            notify(f"Pausa de {self.cfg['pause_minutes']} min por perdidas seguidas")
        notify(f"{'✅' if pnl > 0 else '🔻'} {'[LIVE]' if self.live else '[PAPER]'} {symbol} cerrada por {reason}\nPnL {pnl:+.2f} USDT ({r:+.2f}R)")
        new_file = not os.path.exists(self.cfg["trades_log"])
        with open(self.cfg["trades_log"], "a", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["ts", "symbol", "side", "kind", "grade", "entry", "exit", "reason", "pnl", "r"])
            w.writerow([ts, symbol, pos["side"], pos["kind"], pos["grade"], pos["entry"], exit_px, reason, round(pnl, 6), round(r, 3)])

    def status_text(self) -> str:
        eq = self.equity()
        eq0 = self.state.get("day_start_equity") or eq
        pos = self.state["positions"]
        lines = [f"Bot LCE · modo {self.cfg['mode']} · {self.cfg['exchange']}",
                 f"Simbolos: {', '.join(self.cfg['symbols'])}",
                 f"Equity: {eq:.2f} USDT ({eq - eq0:+.2f} hoy)",
                 f"Estado: {'⏸ DETENIDO (/resume para reanudar)' if self.state.get('halt') else '▶️ activo'}"]
        if self.state["pause_until_ms"] > time.time() * 1000:
            lines.append("Pausa activa por perdidas seguidas")
        if pos:
            for sym, q in pos.items():
                lines.append(f"Abierta: {sym} {q['side']} {q['kind']} entrada {fmt_px(q['entry'])} SL {fmt_px(q['sl'])} TP {fmt_px(q['tp'])}")
        else:
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
        notify(f"🤖 Bot LCE iniciado · modo {self.cfg['mode']} · {self.cfg['exchange']}\n"
               f"Simbolos: {', '.join(self.cfg['symbols'])}\nRiesgo {self.cfg['risk_pct']}% · apal. max {self.cfg['max_leverage']}x\n"
               f"Comandos: /status /stop /resume")
        last_err = 0.0
        next_tick = (int(time.time() // 300) + 1) * 300 + self.cfg["loop_delay_sec"]
        while True:
            if time.time() >= next_tick:
                try:
                    self.tick()
                except Exception as e:  # noqa
                    log.exception("Error en el ciclo: %s", e)
                    if time.time() - last_err > 1800:
                        notify(f"⚠️ Error en el ciclo: {e}")
                        last_err = time.time()
                next_tick = (int(time.time() // 300) + 1) * 300 + self.cfg["loop_delay_sec"]
            try:
                self.poll_telegram()
            except Exception as e:  # noqa
                log.warning("poll_telegram: %s", e)
            time.sleep(3)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
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
    if env.get("BOT_SYMBOLS"):
        cfg["symbols"] = [x.strip() for x in env["BOT_SYMBOLS"].split(",") if x.strip()]
    for key, ek, cast in (("risk_pct", "BOT_RISK_PCT", float), ("max_leverage", "BOT_LEVERAGE", float),
                          ("paper_equity", "BOT_PAPER_EQUITY", float), ("max_daily_loss_pct", "BOT_MAX_DAILY_LOSS", float),
                          ("max_open_positions", "BOT_MAX_POSITIONS", int), ("pause_minutes", "BOT_PAUSE_MINUTES", int)):
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
    return cfg


def make_params(cfg: dict) -> Params:
    valid = {f.name for f in fields(Params)}
    bad = [k for k in cfg.get("params", {}) if k not in valid]
    if bad:
        raise SystemExit(f"Parametros desconocidos en config: {bad}")
    return Params(**cfg.get("params", {}))


def cmd_selftest(args):
    p = Params()
    data = synthetic(20000)
    htf = resample(data)
    sigs = compute_signals(data, htf, p)
    n_sig = sum(1 for s in sigs if s)
    kinds = {}
    for s in sigs:
        if s:
            kinds[(s["kind"], s["side"])] = kinds.get((s["kind"], s["side"]), 0) + 1
    print(f"Velas: {len(data)} | senales: {n_sig} | desglose: {kinds}")
    assert n_sig > 0, "el motor no genero ninguna senal"
    for s in sigs:
        if s:
            if s["side"] == "long":
                assert s["sl"] < s["entry"] < s["tp"], s
            else:
                assert s["tp"] < s["entry"] < s["sl"], s
    res = backtest(data, htf, p, risk_pct=args.risk_pct, max_lev=args.max_lev)
    # comprobar que el motor es causal: recortar el final no cambia las senales anteriores
    cut = 15000
    s_cut = compute_signals(data[:cut], resample(data[:cut]), p)
    for i in range(0, cut - 400):
        a, b = sigs[i], s_cut[i]
        assert (a is None) == (b is None), f"senal no causal en {i}"
        if a:
            assert abs(a["entry"] - b["entry"]) < 1e-9 and a["side"] == b["side"], f"senal distinta en {i}"
    print("OK: senales coherentes (SL < entrada < TP) y causales (sin mirar al futuro).")


def cmd_backtest(args):
    cfg = load_cfg(args.config)
    p = make_params(cfg)
    if args.csv:
        c5 = load_csv(args.csv)
    else:
        ex = make_exchange(cfg, with_keys=False)
        symbol = args.symbol or cfg["symbols"][0]
        since = int((time.time() - (args.days + 4) * 86400) * 1000)
        print(f"Descargando {symbol} 5m ({args.days}+4 dias) de {cfg['exchange']} ...")
        c5 = fetch_all(ex, symbol, "5m", since, TF_MS)
    if len(c5) < 500:
        raise SystemExit("Pocas velas para el backtest")
    res = backtest(c5, resample(c5), p, equity0=args.equity, risk_pct=args.risk_pct or cfg["risk_pct"],
                   max_lev=args.max_lev or cfg["max_leverage"])
    if args.out:
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(res["trades_list"][0].keys()) if res["trades_list"] else ["ts"])
            w.writeheader()
            w.writerows(res["trades_list"])
        print(f"Operaciones guardadas en {args.out}")


def cmd_run(args, mode: Optional[str] = None):
    cfg = load_cfg(args.config)
    if mode and mode != "run":
        cfg["mode"] = mode
    mode = cfg["mode"]
    if mode not in ("signals", "paper", "live"):
        raise SystemExit(f"Modo desconocido: {mode} (usa signals, paper o live)")
    p = make_params(cfg)
    if mode == "live" and not (getattr(args, "yes", False) or os.environ.get("BOT_CONFIRM_LIVE", "").upper() == "YES"):
        raise SystemExit("Modo live: define BOT_CONFIRM_LIVE=YES (o --yes-i-understand-the-risk) tras probar backtest y paper.")
    ex = make_exchange(cfg, with_keys=(mode == "live"))
    missing = [s for s in cfg["symbols"] if s not in ex.markets]
    if missing:
        hints = []
        for s in missing:
            base = s.split("/")[0]
            hints += [m for m in ex.markets if m.startswith(base + "/") and ":USDT" in m][:3]
        msg = f"Simbolos no encontrados en {cfg['exchange']}: {missing}. Parecidos: {hints}"
        notify("❌ " + msg)
        raise SystemExit(msg)
    bot = Bot(cfg, p, ex)
    if mode == "live":
        notify(f"🔌 Conexion OK con {cfg['exchange']}{' (TESTNET)' if cfg['testnet'] else ' (REAL)'} · equity {bot.equity():.2f} USDT")
    bot.run_forever()


def main():
    ap = argparse.ArgumentParser(description="LCE Bot (5m)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s0 = sub.add_parser("selftest", help="prueba con datos sinteticos")
    s0.add_argument("--risk-pct", type=float, default=1.0)
    s0.add_argument("--max-lev", type=float, default=5.0)
    s1 = sub.add_parser("backtest")
    s1.add_argument("--config", default="config.json")
    s1.add_argument("--symbol")
    s1.add_argument("--days", type=int, default=60)
    s1.add_argument("--csv", help="CSV con timestamp_ms,open,high,low,close,volume (5m)")
    s1.add_argument("--equity", type=float, default=1000.0)
    s1.add_argument("--risk-pct", type=float, default=0.0)
    s1.add_argument("--max-lev", type=float, default=0.0)
    s1.add_argument("--out")
    for name in ("paper", "signals", "live", "run"):
        sp = sub.add_parser(name)
        sp.add_argument("--config", default="config.json")
        sp.add_argument("--yes-i-understand-the-risk", dest="yes", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    if args.cmd == "selftest":
        cmd_selftest(args)
    elif args.cmd == "backtest":
        cmd_backtest(args)
    else:
        cmd_run(args, args.cmd)


if __name__ == "__main__":
    main()
