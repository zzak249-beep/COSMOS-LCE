# -*- coding: utf-8 -*-
"""Herramientas de validacion: ablacion de filtros, walk-forward, IC bootstrap y Monte Carlo.

Objetivo: saber si la estrategia tiene una ventaja DISTINGUIBLE del azar en tus datos, no elegir
la configuracion que mejor quedo (eso es sobreajuste)."""
import bisect
import random
from dataclasses import replace
from typing import Dict, List

import qs_bot as Q


def curve(rs: List[float], risk_pct: float):
    eq = peak = 1.0
    mdd = 0.0
    for r in rs:
        eq *= max(0.0, 1 + r * risk_pct / 100.0)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100.0)
    return (eq - 1) * 100.0, mdd


def stats(rs: List[float], risk_pct: float) -> Dict[str, float]:
    n = len(rs)
    if n == 0:
        return dict(n=0, win=0.0, pf=0.0, avg=0.0, net=0.0, mdd=0.0)
    gw = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r <= 0)
    net, mdd = curve(rs, risk_pct)
    return dict(n=n, win=sum(1 for r in rs if r > 0) / n * 100, pf=(gw / gl if gl > 0 else (float("inf") if gw > 0 else 0.0)),
                avg=sum(rs) / n, net=net, mdd=mdd)


def bootstrap_ci(rs: List[float], n_boot: int = 4000, seed: int = 1):
    """IC 95% del R medio y probabilidad (bootstrap) de que el R medio sea <= 0."""
    if len(rs) < 5:
        return None
    rnd = random.Random(seed)
    n = len(rs)
    means = sorted(sum(rs[rnd.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    return means[int(0.025 * n_boot)], means[int(0.975 * n_boot)], sum(1 for m in means if m <= 0) / n_boot


def monte_carlo(rs: List[float], risk_pct: float, n_sim: int = 3000, seed: int = 2):
    if len(rs) < 5:
        return None
    rnd = random.Random(seed)
    n = len(rs)
    nets, mdds = [], []
    for _ in range(n_sim):
        net, mdd = curve([rs[rnd.randrange(n)] for _ in range(n)], risk_pct)
        nets.append(net)
        mdds.append(mdd)
    nets.sort()
    mdds.sort()
    pick = lambda a, q: a[min(len(a) - 1, int(q * len(a)))]
    return dict(p5=pick(nets, 0.05), med=pick(nets, 0.5), p95=pick(nets, 0.95), dd_med=pick(mdds, 0.5), dd_p95=pick(mdds, 0.95),
                prob_loss=sum(1 for x in nets if x < 0) / n_sim, prob_dd20=sum(1 for x in mdds if x >= 20) / n_sim,
                prob_dd30=sum(1 for x in mdds if x >= 30) / n_sim)


VARIANTS = [
    ("BASE (parametros actuales)", {}),
    ("sin filtro de alineacion", dict(align_mode="Sin filtro")),
    ("solo 369", dict(align_mode="Solo 369")),
    ("solo cadena", dict(align_mode="Solo cadena")),
    ("SSMT de 1 etapa (use2=False)", dict(use2=False)),
    ("sin True Opens", dict(use_to=False)),
    ("etapa 90m", dict(stage_cyc="90m")),
    ("secuencia Mensual-Semanal-Diario", dict(seq_mode="Mensual-Semanal-Diario")),
    ("TP = RR fijo 2.0", dict(tp_mode="RR fijo", rr_fixed=2.0)),
    ("TP = RR fijo 1.5", dict(tp_mode="RR fijo", rr_fixed=1.5)),
    ("solo si es Diverger (req_div)", dict(req_div=True)),
    ("parcial 50% en 1R + BE", dict(use_partial=True)),
    ("stop por tiempo 48 velas", dict(max_bars=48)),
    ("sin filtro de costes (max_cost_r=0)", dict(max_cost_r=0.0)),
    ("costes <= 0.15R", dict(max_cost_r=0.15)),
    ("espera tCISD 8 velas", dict(max_wait=8)),
    ("espera tCISD 32 velas", dict(max_wait=32)),
    ("entrada al cierre (optimista)", dict(entry_mode="close")),
]


def _slice(c: List[List[float]], t0: int, t1: int):
    ts = [x[0] for x in c]
    return c[bisect.bisect_left(ts, t0):bisect.bisect_left(ts, t1)]


def ablate(c0, c1, c2, base: Q.Params, risk_pct, max_lev, equity, out=print, variants=None):
    rows = []
    for name, kw in (variants or VARIANTS):
        res = Q.backtest(c0, c1, c2, replace(base, **kw), equity0=equity, risk_pct=risk_pct, max_lev=max_lev, quiet=True)
        rs = [t["r"] for t in res["trades_list"]]
        rows.append((name, stats(rs, risk_pct)))
    out("\n=== 1) ABLACION: que aporta cada pieza (R neto de costes) ===")
    out(f"{'variante':38s} {'ops':>5s} {'win%':>6s} {'PF':>6s} {'R med':>7s} {'neto%':>8s} {'maxDD%':>7s}")
    for name, s in rows:
        pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        out(f"{name:38s} {s['n']:5d} {s['win']:6.1f} {pf:>6s} {s['avg']:7.2f} {s['net']:8.1f} {s['mdd']:7.1f}")
    out(f"NOTA: has mirado {len(rows)} variantes. Con tantas, alguna sale bien por azar: NO elijas la mejor fila; "
        f"busca piezas que ayuden de forma consistente y en todos los tramos (seccion 2).")
    return rows


def walk_forward(c0, c1, c2, p: Q.Params, risk_pct, max_lev, equity, folds=4, warm_days=10, out=print):
    t_start = c0[0][0] + warm_days * 86400_000
    t_end = c0[-1][0] + 1
    if t_end - t_start < folds * 5 * 86400_000:
        out("\n=== 2) WALK-FORWARD === datos insuficientes para esos tramos")
        return []
    span = (t_end - t_start) // folds
    out(f"\n=== 2) WALK-FORWARD: {folds} tramos consecutivos, mismos parametros (sin reoptimizar) ===")
    out(f"{'tramo':>6s} {'ops':>5s} {'win%':>6s} {'PF':>6s} {'R med':>7s} {'neto%':>8s} {'maxDD%':>7s}")
    good = 0
    all_rs: List[float] = []
    for k in range(folds):
        a, b = t_start + k * span, t_start + (k + 1) * span
        w0 = a - warm_days * 86400_000
        s0, s1, s2 = _slice(c0, w0, b), _slice(c1, w0, b), _slice(c2, w0, b)
        res = Q.backtest(s0, s1, s2, p, equity0=equity, risk_pct=risk_pct, max_lev=max_lev, quiet=True)
        rs = [t["r"] for t in res["trades_list"] if t["ts"] >= a]
        all_rs += rs
        s = stats(rs, risk_pct)
        good += 1 if (s["n"] > 0 and s["avg"] > 0) else 0
        pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        out(f"{k + 1:6d} {s['n']:5d} {s['win']:6.1f} {pf:>6s} {s['avg']:7.2f} {s['net']:8.1f} {s['mdd']:7.1f}")
    out(f"Tramos con R medio > 0: {good}/{folds}  (una ventaja real deberia repetirse en la mayoria)")
    return all_rs


def run_report(c0, c1, c2, p: Q.Params, equity=1000.0, risk_pct=0.5, max_lev=3.0, folds=4, out=print):
    days = (c0[-1][0] - c0[0][0]) / 86400_000
    out(f"Datos: {len(c0)} velas de 15m (~{days:.0f} dias) | riesgo {risk_pct}% | costes {p.fee_pct}% + {p.slippage_pct}% por lado")
    rows = ablate(c0, c1, c2, p, risk_pct, max_lev, equity, out)
    all_rs = walk_forward(c0, c1, c2, p, risk_pct, max_lev, equity, folds, out=out)
    base = Q.backtest(c0, c1, c2, p, equity0=equity, risk_pct=risk_pct, max_lev=max_lev, quiet=True)
    rs = [t["r"] for t in base["trades_list"]]
    out("\n=== 3) ¿LA VENTAJA SE DISTINGUE DEL AZAR? (parametros actuales) ===")
    ci = bootstrap_ci(rs)
    if ci is None:
        out(f"Solo {len(rs)} operaciones: no se puede estimar nada fiable.")
        return
    lo, hi, p0 = ci
    out(f"{len(rs)} operaciones | R medio {sum(rs) / len(rs):+.3f} | IC 95%: [{lo:+.3f}, {hi:+.3f}] | prob. de que el R medio sea <= 0: {p0 * 100:.0f}%")
    if lo <= 0:
        out("El intervalo incluye 0: con estos datos NO se puede afirmar que haya ventaja.")
    else:
        out("El intervalo queda por encima de 0 en este periodo (no garantiza que se repita).")
    mc = monte_carlo(rs, risk_pct)
    out("\n=== 4) MONTE CARLO (reordenando tus operaciones 3000 veces) ===")
    out(f"Resultado final: peor 5% {mc['p5']:+.1f}% | mediana {mc['med']:+.1f}% | mejor 5% {mc['p95']:+.1f}%")
    out(f"Drawdown maximo: mediana {mc['dd_med']:.1f}% | peor 5% {mc['dd_p95']:.1f}%")
    out(f"Prob. de acabar en perdidas: {mc['prob_loss'] * 100:.0f}% | de DD >= 20%: {mc['prob_dd20'] * 100:.0f}% | de DD >= 30%: {mc['prob_dd30'] * 100:.0f}%")
    out("\nREGLA PRACTICA: no pases a dinero real si el IC incluye 0, si menos de la mitad de los tramos son positivos "
        "o si el drawdown del peor 5% supera lo que puedes aguantar. Un buen backtest no garantiza resultados futuros.")
