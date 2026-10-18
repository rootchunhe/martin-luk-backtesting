"""python3 backtest.py [name ...] [--allow-incomplete]      no names = specs.LOGIC + specs.SENSITIVITY.   Each spec -> results/<name>/ (spec.json,
metrics.json, summary.md, trades.csv, daily.csv, cumret.png); all specs run so far -> results/overview.md.
Runs after the download, as the single writer of the data files. Passes per spec: the first simulation (fallback spreads, stops
at the stop price) gives a trade list; for those trades the minute bars of the stop / partial days and the NBBO quote before
every fill minute are pulled on demand (appended to data/taq and data/nbbo.csv) and the simulation is repeated until the trade
list stops changing (at most 10 passes). A failed pull aborts the run: results are never produced on silently missing data."""
import sys, time, hashlib
from pathlib import Path
import numpy as np, pandas as pd
import config as C, specs, data, features, entry, portfolio, report

def code_hash():
    h = hashlib.sha1()
    for f in ("config.py", "data.py", "features.py", "specs.py", "entry.py", "portfolio.py", "report.py", "backtest.py"):
        h.update(Path(__file__).with_name(f).read_bytes())
    return h.hexdigest()[:10]

def _cfp_map(panel, permnos):
    p = panel[panel.permno.isin(permnos)]
    return dict(zip(zip(p.permno.astype(int), p.date.values.astype("datetime64[D]").astype(np.int64)), p.cfp.fillna(1.0).values))

def _col_map(panel, permnos, col):
    p = panel[panel.permno.isin(permnos)]
    return dict(zip(zip(p.permno.astype(int), p.date.values.astype("datetime64[D]").astype(np.int64)), p[col].values))

def fetch_minute_data(t: pd.DataFrame, panel: pd.DataFrame, held: dict, quotes: dict, S: dict, log=print):
    """Add to held / quotes what the trades t need: stop and partial days' bars (panel units) and the quote before each fill.
    A fill in minute 0 (a gap through the stop at the open) has no quote before it and uses the morning-tier fallback: not requested."""
    if t.empty:
        return
    need = [(r.permno, r.ticker, r.exit_day) for r in t.itertuples() if r.exit_reason == "stop" and (r.permno, r.exit_day) not in held]
    need += [(r.permno, r.ticker, r.partial_day) for r in t.itertuples() if r.partial_day >= 0 and (r.permno, r.partial_day) not in held]
    # entry days on which the daily high reached the partial target (a same-day partial is possible after a morning fill)
    if S.get("partial_at_r") is not None:
        hi = _col_map(panel, set(t.permno), "high")
        need += [(r.permno, r.ticker, r.entry_day) for r in t.itertuples()
                 if (r.permno, r.entry_day) not in held and hi.get((r.permno, r.entry_day), -np.inf) >= r.entry_price + S["partial_at_r"] * r.planned_risk]       # the simulation's target: fill + R x planned risk
    pairs = pd.DataFrame(sorted(set(need)), columns=["permno", "ticker", "day"])
    if len(pairs):
        pairs["date"] = pd.to_datetime(pairs.day.values.astype("datetime64[D]"))
        tk = data.symbol_on(data.security_hist(), pairs.permno, pairs.date)             # the symbol valid on the held day, not the entry day
        pairs["ticker"] = [t_ if t_ else t0_ for t_, t0_ in zip(tk, pairs.ticker)]
        try:
            raw = data.minute_bars_for(pairs[["date", "ticker", "permno"]]); cfp = _cfp_map(panel, set(pairs.permno)); n0 = len(held)
            for (permno, dn), b in raw.items():
                if (permno, dn) not in cfp or not cfp[(permno, dn)] > 0:
                    raise RuntimeError(f"no positive price factor in the panel for permno {permno} on day {dn}")
                f = float(cfp[(permno, dn)])
                held[(permno, dn)] = b.assign(open=b.open.astype(float) / f, high=b.high.astype(float) / f, low=b.low.astype(float) / f, close=b.close.astype(float) / f)
            log(f"held-day bars: +{len(held)-n0} of {len(pairs)} requested stock-days")
        except Exception as e:
            raise RuntimeError(f"held-day minute bars could not be pulled: {str(e)[:120]}")
    if S["slippage"] != "nbbo":
        return                                                    # flat slippage: no quotes needed
    # fill minutes to quote: the entry, the exit and the partial of every trade; the exit-day symbol may differ from the entry-day one
    ex = t[t.exit_minute >= 0]; pa = t[(t.partial_day >= 0) & (t.partial_minute >= 0)]
    sh = data.security_hist()
    ex_sym = data.symbol_on(sh, ex.permno, pd.to_datetime(ex.exit_day.values.astype("datetime64[D]"))) if len(ex) else []
    pa_sym = data.symbol_on(sh, pa.permno, pd.to_datetime(pa.partial_day.values.astype("datetime64[D]"))) if len(pa) else []
    req = [(int(r.entry_day), r.ticker, int(r.entry_minute), r.ticker) for r in t.itertuples() if r.entry_minute > 0]
    req += [(int(r.exit_day), s_ or r.ticker, int(r.exit_minute), r.ticker) for r, s_ in zip(ex.itertuples(), ex_sym) if r.exit_minute > 0]
    req += [(int(r.partial_day), s_ or r.ticker, int(r.partial_minute), r.ticker) for r, s_ in zip(pa.itertuples(), pa_sym) if r.partial_minute > 0]
    rq = pd.DataFrame(sorted(set(req)), columns=["day", "ticker", "minute", "entry_ticker"]); rq["date"] = pd.to_datetime(rq.day.values.astype("datetime64[D]"))
    rq = rq[[(tk, int(dy), int(mn)) not in quotes for dy, tk, mn in zip(rq.day, rq.entry_ticker, rq.minute)]]
    if len(rq):
        try:
            n0 = len(quotes); got = data.nbbo_for(rq[["date", "ticker", "minute"]].drop_duplicates())
            for dy, tk, mn, et in zip(rq.day, rq.ticker, rq.minute, rq.entry_ticker):
                q = got.get((tk, int(dy), int(mn)))
                if q is not None:
                    quotes[(et, int(dy), int(mn))] = q         # the simulation looks quotes up under the entry-day ticker
            log(f"NBBO quotes: +{len(quotes)-n0} of {len(rq)} requested fill minutes (the rest had no valid quote in the 5 minutes before the fill)")
        except Exception as e:
            raise RuntimeError(f"NBBO quotes could not be pulled: {str(e)[:120]}")

def _trade_key(t):
    return set(zip(t.permno, t.entry_day, t.exit_day, t.exit_minute, t.exit_reason, t.partial_day, t.partial_minute)) if len(t) else set()

def _rf(ff):
    """{day_int: daily T-bill rate} for idle cash."""
    return {int(d): float(r) for d, r in zip(ff.date.values.astype("datetime64[D]").astype(np.int64), ff.rf.values)} if ff is not None else {}

def run_spec(name, panel, cand, ff, log=print):
    S = specs.get(name); warn = entry.check_within_gate(S); t0 = time.time(); rf = _rf(ff)
    S["data_version"] = entry.data_version()                      # run id of the download plus the minute-bar coverage, into spec.json
    ent = entry.entries(S, cand, log=log)
    sig = ent[entry.universe_mask(ent, S)].copy() if len(ent) else ent
    log(f"{name}: {len(sig)} entries in the universe")
    notes = list(warn)
    if ent.attrs.get("stock_days_missing", 0):
        notes.append(f"{ent.attrs['stock_days_missing']} of {ent.attrs.get('stock_days', 0)} candidate stock-days have no minute bars ({ent.attrs.get('days_missing', 0)} days without a file); their entries are missing")
    if ent.attrs.get("stock_days_empty", 0):
        notes.append(f"{ent.attrs['stock_days_empty']} candidate stock-days were queried and had no eligible print (empty)")
    if ent.attrs.get("stock_days_thin", 0):
        notes.append(f"{ent.attrs['stock_days_thin']} of {ent.attrs.get('stock_days', 0)} candidate stock-days were too thinly traded for close_confirm (min_bars / max_stale_minutes)")
    held, quotes = {}, {}
    def converge(S_, log_):
        """Simulate, pull the minute data the trades need, repeat until the trade list is stable (at most 10 passes)."""
        t_, d_ = portfolio.simulate(S_, sig, panel, held, quotes, rf)
        for k in range(10):
            fetch_minute_data(t_, panel, held, quotes, S_, log=log_)
            t2, d2 = portfolio.simulate(S_, sig, panel, held, quotes, rf)
            changed = _trade_key(t2) != _trade_key(t_); t_, d_ = t2, d2
            if not changed:
                return t_, d_
        notes.append(f"{S_['name']}: trade list still changing after 10 passes; reported with the last pass"); return t_, d_
    t, d = converge(S, log)
    scenarios = {}
    for label, ov in [("zero slippage (commissions and fees only)", dict(slippage=0.0, stop_slippage=0.0, margin_rate=0.0, stop_fill="stop")),
                      (f"as specified (slippage={S['slippage']}, stop fill={S['stop_fill']}, margin {'T-bill+%.1f%%' % (100*S['margin_spread']) if S['margin_rate'] is None else '%.0f%%' % (100*S['margin_rate'])})", {}),
                      ("conservative (0.2% per side, stop slip 0.5%, 8% margin)", dict(slippage=0.002, stop_slippage=0.005, margin_rate=0.08))]:
        scenarios[label] = (t, d) if not ov else converge(dict(S, **ov, name=S["name"] + " / " + label.split(" (")[0]), lambda *a: None)
    txt = report.write(C.RESULTS / S["name"], S, t, d, panel, ff, scenarios, code_hash(), notes)
    log("\n".join(txt.split("\n")[3 + len(notes):7 + len(notes)]) + f"\n   ({time.time()-t0:.0f}s)")
    return t, d

def compare():
    """results/overview.md: passive benchmarks, the logic ablations, the sensitivity runs, all in the same general metrics."""
    import json
    ms = {p.parent.name: json.loads(p.read_text()) for p in C.RESULTS.glob("*/metrics.json")}
    hdr = ["", "CAGR", "vol", "Sharpe", "max DD", "6-factor alpha/yr (t)", "beta", "trades/yr", "win", "avg win", "avg loss", "avg trade", "profit/loss ratio", "avg % invested"]
    def row(name, m, trades=True):
        base = [name, report.pct(m['cagr']), report.pct(m['vol']), f"{m['sharpe']:.2f}", report.pct(m['maxdd'],0), f"{report.pct(m['alpha_annual'])} ({m['alpha_t']:.1f})", f"{m['beta_mkt']:.2f}"]
        if trades:
            return base + [f"{m['per_year']:.0f}", report.pct(m['win'],0), report.pct(m['avg_win']), report.pct(m['avg_loss']), report.pct(m['avg_trade'],2), f"{m['pf']:.2f}", report.pct(m['exposure'],0)]
        return base + ["-"] * 7
    ref = ms.get("full") or next(iter(ms.values()), None)
    L = ["# Overview: all runs and benchmarks\n", f"Sample {C.SAMPLE_START} to {C.SAMPLE_END}. Every row is after costs and on daily returns; Sharpe = annualised mean of daily returns in excess of the T-bill rate over their standard deviation; vm = a fraction of the benchmark plus T-bills, scaled to the `full` strategy's volatility; 6-factor alpha = intercept (x252) of daily excess returns on the Fama-French factors (market, size, value, profitability, investment, momentum), the return per year the factor exposures do not explain, Newey-West t in brackets; beta = market loading. Passive benchmarks are monthly-rebalanced; the trade columns do not apply to the benchmarks. Avg % invested = capital utilisation: market value of open positions / net asset value, averaged over all trading days; profit/loss ratio = gross profits / gross losses.\n"]
    if ref:
        L.append("## Passive benchmarks\n")
        L.append(report._md([row(n, ref[k], trades=False) for n, k in (("QQQ", "qqq"), ("QQQ vm", "qqq_vol_matched"), ("12-1 momentum top decile", "momentum"), ("momentum vm", "momentum_vol_matched"), ("universe hold (Luk leader screen)", "leaders"), ("universe hold vm", "leaders_vol_matched")) if k in ref], hdr))
    desc = lambda names: "\n".join(f"- `{n}`: {specs.describe(n)}" for n in names if n in ms) + "\n"
    L.append("\n## Strategy logic: one block removed at a time\n\n" + specs.RULE + "\n\n" + desc(specs.LOGIC))
    L.append(report._md([row(n, ms[n]) for n in specs.LOGIC if n in ms], hdr))
    L.append("\n## Sensitivity: one parameter at a time\n\n" + desc(specs.SENSITIVITY))
    L.append(report._md([row(n, ms[n]) for n in specs.SENSITIVITY if n in ms], hdr))
    extra = sorted(set(ms) - set(specs.LOGIC) - set(specs.SENSITIVITY))
    if extra:
        L.append("\n## Other runs\n\n" + desc(extra)); L.append(report._md([row(n, ms[n]) for n in extra], hdr))
    txt = "\n".join(L) + "\n"; (C.RESULTS / "overview.md").write_text(txt); print("\n" + txt)

def main(argv):
    allow = "--allow-incomplete" in argv; names = [a for a in argv if not a.startswith("--")]
    import subprocess
    if subprocess.run(["pgrep", "-f", "data.py download"], capture_output=True).returncode == 0:
        raise SystemExit("a download is running: the backtest appends to the same day files and must wait for it to finish")
    if not names:
        names = specs.LOGIC + list(specs.SENSITIVITY)
    if not data.is_current():
        raise SystemExit("panel, candidates or the download run are out of date with the code / CRSP files: run python3 data.py prepare")
    cov = data.coverage()
    if cov["missing"].sum() and not allow:
        raise SystemExit(f"{cov['missing'].sum():,} candidate stock-days have no minute bars yet (python3 data.py status); finish the download or pass --allow-incomplete")
    panel = features.load(); cand = data.candidates(); ff = data.factors(); C.RESULTS.mkdir(exist_ok=True)
    for name in names:
        run_spec(name, panel, cand, ff)
    compare()

if __name__ == "__main__":
    main(sys.argv[1:])
