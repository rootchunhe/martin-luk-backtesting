"""Per-spec output folder: spec.json, metrics.json, summary.md, trades.csv, daily.csv, cumret.png. Percentages only."""
import json
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import config as C, specs

def pct(x, d=1):
    return "n/a" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{100*x:.{d}f}%"

def _ts(day, minute):
    """'YYYY-MM-DD HH:MM' from a day int and minutes after 9:30; minute -1 = unknown -> date only."""
    d = str(np.datetime64(int(day), "D"))
    if minute is None or minute < 0:
        return d
    h, m = divmod(570 + int(minute), 60)
    return f"{d} {h:02d}:{m:02d}"

def trades_table(t: pd.DataFrame) -> pd.DataFrame:
    if t.empty:
        return pd.DataFrame(columns=["ticker", "entry_time", "entry_price", "stop", "shares", "exit_time", "exit_price", "exit_reason"])
    # prices in the dollars of the day (adjusted price x that day's price factor), so shares x price = dollars actually paid
    ecfp = t.entry_cfp.fillna(1.0); xcfp = t.exit_cfp.fillna(ecfp); pcfp = t.partial_cfp.fillna(ecfp)
    out = pd.DataFrame({
        "ticker": t.ticker, "entry_time": [_ts(d, m) for d, m in zip(t.entry_day, t.entry_minute)], "entry_price": (t.entry_price * ecfp).round(4),
        "stop": (t.stop * ecfp).round(4), "shares": t.shares_raw.astype(int), "exit_time": [_ts(d, m) for d, m in zip(t.exit_day, t.exit_minute)],
        "exit_price": (t.exit_price * xcfp).round(4), "exit_shares": t.exit_shares_raw.round(0), "exit_reason": t.exit_reason, "hold_days": t.hold_days, "price_return": t.price_return.round(4),
        "net_return": t.net_return.round(4), "pnl": t.net_pnl.round(2), "costs": t.costs.round(2), "dividends": t.dividends.round(2),
        "partial_time": [_ts(d, m) if d >= 0 else "" for d, m in zip(t.partial_day, t.partial_minute)], "partial_price": (t.partial_price * pcfp).round(4),
        "partial_shares": t.partial_shares_raw.round(2), "stop_width": t.stop_width.round(4), "adv": t.adv.round(0), "adr": t.adr.round(4),
        "run_up_1m": t.run_up_1m.round(3), "run_up_3m": t.run_up_3m.round(3), "entry_half_spread": t.entry_half_spread.round(5), "exit_half_spread": t.exit_half_spread.round(5),
        "account_value_at_entry": t.account_value_at_entry.round(0), "qqq_up": t.qqq_up, "permno": t.permno, "R": t.R.round(2)})
    return out.sort_values(["entry_time", "ticker"]).reset_index(drop=True)

BENCH_CACHE = C.DATA / "passive_benchmarks.csv"

def _half_spread_close(advd):
    for cap, s in C.HALF_SPREAD_FALLBACK["close"]:
        if advd < cap: return s
    return C.HALF_SPREAD_FALLBACK["close"][-1][1]

def passive_benchmarks(panel: pd.DataFrame) -> pd.DataFrame:
    """Two monthly-rebalanced, equal-weight, long-only portfolios in the strategy's liquidity universe (common stocks, raw price
    >= $5, 20-day dollar volume >= $50M), gross and net of costs. Select on the data of the day before each month end, trade at the month end's close, hold the shares for the next month (weights drift, no daily rebalancing):
      momentum : the top decile of 12-1 momentum (the textbook factor portfolio);
      leaders  : every stock passing Luk's leader screen that day (ADR >= 5%, 1- or 3-month run-up >= 30%, 9 > 21 > 50 EMA,
                 close above the 250-day-high AVWAP), i.e. "buy his stocks without the pullback timing".
    Costs: each rebalance pays, on the traded weight, the closing half-spread of the stock's liquidity tier (the table measured
    from NBBO) plus 0.01% commission. Daily returns are CRSP total returns. Cached as CSV keyed to the panel version."""
    import json
    meta = json.loads(C.PANEL_META.read_text()) if C.PANEL_META.exists() else {}
    tag = hashlib_tag(json.dumps(dict(panel=meta, spreads=C.HALF_SPREAD_FALLBACK["close"], commission=0.0001, calc="hold-4"), sort_keys=True, default=str))
    if BENCH_CACHE.exists():
        c = pd.read_csv(BENCH_CACHE, parse_dates=["date"])
        if "tag" in c and str(c.tag.iloc[0]) == tag:
            return c.drop(columns="tag").set_index("date")
    p = panel[panel.common & ~panel.is_benchmark]
    piv = lambda col: p.pivot(index="date", columns="permno", values=col)
    close, ret, raw, adv = piv("close"), piv("ret"), piv("close_raw"), piv("advd")
    e9, e21, e50, adr, avwap, r21, r63 = piv("ema9"), piv("ema21"), piv("ema50"), piv("adr"), piv("avwap"), piv("ret21"), piv("ret63")
    mom = close.shift(21) / close.shift(252) - 1
    dates = close.index; month = dates.to_period("M"); ends = [i for i in range(len(dates) - 1) if month[i] != month[i + 1]]
    out = pd.DataFrame(0.0, index=dates, columns=["momentum", "momentum_net", "leaders", "leaders_net", "n_momentum", "n_leaders"])
    prev = {"momentum": pd.Series(dtype=float), "leaders": pd.Series(dtype=float)}
    for j, i in enumerate(ends):
        if i == 0:
            continue
        t = dates[i - 1]; liquid = (raw.loc[t] >= 5) & (adv.loc[t] >= 50e6)      # selection on the day before the month end; the trade is the month end's close
        sel = {}
        m_el = liquid & mom.loc[t].notna()
        sel["momentum"] = m_el & (mom.loc[t] >= mom.loc[t][m_el].quantile(0.9)) if m_el.sum() >= 20 else m_el & False
        sel["leaders"] = (liquid & (adr.loc[t] >= 0.05) & ((r21.loc[t] >= 0.30) | (r63.loc[t] >= 0.30)) & (e9.loc[t] > e21.loc[t]) & (e21.loc[t] > e50.loc[t]) & (close.loc[t] > avwap.loc[t]))
        nxt = dates[ends[j + 1]] if j + 1 < len(ends) else dates[-1]
        for k in ("momentum", "leaders"):
            members = sel[k][sel[k]].index
            w = pd.Series(1.0 / len(members), index=members) if len(members) else pd.Series(dtype=float)
            cost = rebalance_cost(prev[k], w, adv.loc[t])
            if len(members) == 0:                                                      # nothing qualifies: sell everything, sit in cash for the month
                out.loc[dates[i + 1], k + "_net"] = -cost; prev[k] = w; continue
            daily, w_end = hold_returns(ret.loc[dates[i + 1]:nxt, members], w)
            out.loc[daily.index, k] = daily.values; out.loc[daily.index, k + "_net"] = daily.values
            out.loc[daily.index[0], k + "_net"] = (1 + daily.iloc[0]) * (1 - cost) - 1
            out.loc[daily.index, "n_" + k] = len(members)
            prev[k] = w_end
    out.assign(tag=tag).reset_index().rename(columns={"index": "date"}).to_csv(BENCH_CACHE, index=False)
    return out

def rebalance_cost(prev: pd.Series, w: pd.Series, adv_row: pd.Series) -> float:
    """Cost of moving from the drifted weights prev to the new weights w: on every weight bought or sold, the closing half-spread
    of the stock's liquidity tier plus 0.01% commission (a full liquidation pays it on every old weight)."""
    traded = w.subtract(prev, fill_value=0.0).abs()
    return float(sum(tw * (_half_spread_close(float(adv_row.get(s_, 0.0)) if pd.notna(adv_row.get(s_, np.nan)) else 0.0) + 0.0001) for s_, tw in traded.items()))

def hold_returns(seg: pd.DataFrame, w: pd.Series):
    """Buy-and-hold of weights w (set at the close before seg starts) through the daily returns in seg: the portfolio's daily
    returns and its drifted end weights. A member with no return (delisted, settled) stays at its last value, i.e. cash."""
    growth = (1 + seg[w.index].fillna(0.0)).cumprod()
    val = (growth * w).sum(axis=1)
    daily = val / val.shift(1).fillna(1.0) - 1
    w_end = growth.iloc[-1] * w / val.iloc[-1]
    return daily, w_end

def hashlib_tag(s: str) -> str:
    import hashlib
    return hashlib.sha1(s.encode()).hexdigest()[:10]

def account_series(dl: pd.DataFrame, S: dict) -> np.ndarray:
    """Account value with the starting capital in front, so the first day's change counts."""
    return np.r_[S["starting_capital"], dl.account_value.values]

def rf_on(dates, ff) -> np.ndarray:
    """Daily T-bill rate on each date (0 when no factor file is given)."""
    if ff is None:
        return np.zeros(len(dates))
    return ff.set_index("date")["rf"].reindex(pd.to_datetime(dates)).fillna(0.0).values

def sharpe(r: np.ndarray, rf: np.ndarray) -> float:
    """Annualised Sharpe ratio on daily returns in excess of the T-bill rate."""
    x = r - rf
    return float(x.mean() / x.std() * np.sqrt(252)) if x.std() > 0 else np.nan

def daily_table(dl: pd.DataFrame, panel: pd.DataFrame, S: dict, ff=None) -> pd.DataFrame:
    out = pd.DataFrame({"date": pd.to_datetime(dl.day.values.astype("datetime64[D]"))})
    out["cum_return"] = dl.account_value.values / S["starting_capital"] - 1
    q = panel[panel.permno == C.BENCHMARKS["QQQ"]].set_index("date")["ret"].sort_index().fillna(0.0)     # total return, dividends included
    rq = q.reindex(out.date).fillna(0.0).values
    out["cum_return_qqq"] = np.cumprod(1 + rq) - 1
    rs = np.diff(account_series(dl, S)) / account_series(dl, S)[:-1]; rf = rf_on(out.date, ff)
    k = rs.std() / rq.std() if rq.std() > 0 else 1.0                       # scale QQQ to the strategy's realised volatility (ex post, full sample)
    out["cum_return_qqq_vol_matched"] = np.cumprod(1 + k * rq + (1 - k) * rf) - 1          # k in the benchmark, the rest in T-bills, like the strategy's idle cash
    pb = passive_benchmarks(panel).reindex(out.date).fillna(0.0)
    out.attrs["qqq_vol_scale"] = float(k)
    for k_ in ("momentum", "leaders"):
        rg, rn = pb[k_].values, pb[k_ + "_net"].values
        kk = rs.std() / rn.std() if rn.std() > 0 else 1.0
        out[f"cum_return_{k_}"] = np.cumprod(1 + rg) - 1
        out[f"cum_return_{k_}_net"] = np.cumprod(1 + rn) - 1
        out[f"cum_return_{k_}_vol_matched"] = np.cumprod(1 + kk * rn + (1 - kk) * rf) - 1   # net of costs, scaled to the strategy's volatility, rest in T-bills
        out.attrs[f"{k_}_vol_scale"] = float(kk); out.attrs[f"{k_}_n"] = float(pb["n_" + k_][pb["n_" + k_] > 0].mean()) if (pb["n_" + k_] > 0).any() else np.nan
    out["account_value"] = dl.account_value.values.round(2); out["cash"] = dl.cash.values.round(2)
    out["exposure"] = dl.exposure.values.round(4); out["n_positions"] = dl.n_positions.values
    return out

def metrics(t: pd.DataFrame, dl: pd.DataFrame, S: dict, ff=None) -> dict:
    av = account_series(dl, S); r = np.diff(av) / av[:-1]; years = len(dl) / 252; rf = rf_on(dl.day.values.astype('datetime64[D]'), ff)
    dd = av / np.maximum.accumulate(av) - 1
    longest = 0; run = 0
    for x in dd:
        run = run + 1 if x < 0 else 0; longest = max(longest, run)
    w = t[t.net_pnl > 0] if len(t) else t; lo = t[t.net_pnl <= 0] if len(t) else t
    return dict(n=int(len(t)), per_year=len(t) / years, win=float((t.net_pnl > 0).mean()) if len(t) else np.nan,
                avg_trade=float(t.net_return.mean()) if len(t) else np.nan, median_trade=float(t.net_return.median()) if len(t) else np.nan,
                avg_win=float(w.net_return.mean()) if len(w) else np.nan, avg_loss=float(lo.net_return.mean()) if len(lo) else np.nan,
                payoff=float(w.net_return.mean() / -lo.net_return.mean()) if len(w) and len(lo) and lo.net_return.mean() < 0 else np.nan,
                pf=float(t.loc[t.net_pnl > 0, "net_pnl"].sum() / max(-t.loc[t.net_pnl <= 0, "net_pnl"].sum(), 1e-9)) if len(t) else np.nan,
                vol=float(r.std() * np.sqrt(252)),
                total=float(av[-1] / av[0] - 1), cagr=float((av[-1] / av[0]) ** (1 / years) - 1),
                sharpe=sharpe(r, rf), maxdd=float(dd.min()), underwater_days=int(longest),
                exposure=float(np.nanmean(dl.exposure.values)), hold_win=float(w.hold_days.median()) if len(w) else np.nan, hold_loss=float(lo.hold_days.median()) if len(lo) else np.nan)

def _alpha(dates, ret, ff) -> dict:
    """OLS of daily excess returns on FF5 + UMD with Newey-West (5 lags) errors: alpha per year, its t, R2, betas."""
    if ff is None:
        return dict(n=0, alpha_annual=np.nan, alpha_t=np.nan, r2=np.nan, betas={k: (np.nan, np.nan) for k in FACTORS}, mean_excess_annual=np.nan)
    import statsmodels.api as sm
    m = pd.DataFrame({"date": dates, "ret": ret}).merge(ff, on="date", how="inner")
    if len(m) < 100:
        return dict(n=int(len(m)), alpha_annual=np.nan, alpha_t=np.nan, r2=np.nan, betas={k: (np.nan, np.nan) for k in FACTORS}, mean_excess_annual=np.nan)
    y = m.ret - m.rf; X = sm.add_constant(m[list(FACTORS)])
    res = sm.OLS(y, X).fit(cov_type="HAC", cov_kwds={"maxlags": 5})
    return dict(n=int(len(m)), alpha_annual=float(res.params["const"] * 252), alpha_t=float(res.tvalues["const"]), r2=float(res.rsquared),
                betas={k: (float(res.params[k]), float(res.tvalues[k])) for k in FACTORS}, mean_excess_annual=float(y.mean() * 252))

FACTORS = ("mktrf", "smb", "hml", "rmw", "cma", "umd")
BENCHMARK_COLS = (("qqq", "cum_return_qqq"), ("qqq_vol_matched", "cum_return_qqq_vol_matched"),
                  ("momentum", "cum_return_momentum"), ("momentum_net", "cum_return_momentum_net"), ("momentum_vol_matched", "cum_return_momentum_vol_matched"),
                  ("leaders", "cum_return_leaders"), ("leaders_net", "cum_return_leaders_net"), ("leaders_vol_matched", "cum_return_leaders_vol_matched"))

def series_metrics(dates, av, ff=None) -> dict:
    """CAGR, vol, Sharpe, max drawdown, longest underwater, alpha and market beta of one account-value path."""
    r = np.diff(av) / av[:-1]; yrs = len(r) / 252; dd = av / np.maximum.accumulate(av) - 1
    under, run = 0, 0
    for x in dd:
        run = run + 1 if x < 0 else 0; under = max(under, run)
    f = _alpha(dates, r, ff)
    return dict(cagr=(av[-1] / av[0]) ** (1 / yrs) - 1 if yrs > 0 else np.nan, vol=r.std() * np.sqrt(252), sharpe=sharpe(r, rf_on(dates, ff)),
                maxdd=float(dd.min()), underwater_days=int(under), alpha_annual=f["alpha_annual"], alpha_t=f["alpha_t"], beta_mkt=f["betas"]["mktrf"][0], r2=f["r2"])

def benchmark_metrics(d: pd.DataFrame, S: dict, ff=None) -> dict:
    """The passive benchmarks (raw and vol-matched) in the same metrics as the strategy, alpha included."""
    out = {}
    for name, col in BENCHMARK_COLS:
        out[name] = series_metrics(d.date.values, np.r_[1.0, 1 + d[col].values], ff)
    out["scale"] = d.attrs.get("qqq_vol_scale", np.nan); out["mom_scale"] = d.attrs.get("momentum_vol_scale", np.nan); out["lead_scale"] = d.attrs.get("leaders_vol_scale", np.nan)
    out["mom_n"] = d.attrs.get("momentum_n", np.nan); out["lead_n"] = d.attrs.get("leaders_n", np.nan)
    return out

def factor_regression(dl: pd.DataFrame, ff: pd.DataFrame, S: dict, invested_only=False) -> dict | None:
    if ff is None:
        return None
    av = account_series(dl, S)
    d = pd.DataFrame({"date": pd.to_datetime(dl.day.values.astype("datetime64[D]")), "ret": np.diff(av) / av[:-1],
                      "invested": (np.r_[0, dl.n_positions.values[:-1]] > 0) | (dl.n_positions.values > 0) | (dl.n_entries.values > 0)})   # any day with a position or a fill
    if invested_only:
        d = d[d.invested]
    f = _alpha(d.date.values, d.ret.values, ff)
    return None if f["n"] < 100 else f

def top_trades(t: pd.DataFrame, n=10) -> pd.DataFrame:
    """The n trades with the largest net P&L and their share of the total net P&L."""
    if t.empty:
        return t
    top = t.sort_values("net_pnl", ascending=False).head(n).copy()
    top["share"] = top.net_pnl / t.net_pnl.sum() if t.net_pnl.sum() != 0 else np.nan
    return top

def plot_cumret(d: pd.DataFrame, path, title, t: pd.DataFrame = None, S: dict = None):
    """Four panels: cumulative return vs QQQ (raw and vol-matched); drawdown (thin); the ten largest trades as a bar chart;
    yearly returns of the strategy against the vol-matched QQQ."""
    fig = plt.figure(figsize=(16, 8))
    gs = fig.add_gridspec(2, 2, width_ratios=[2, 1], height_ratios=[4, 1.1], hspace=0.10, wspace=0.12, left=0.05, right=0.90, top=0.94, bottom=0.07)
    a1 = fig.add_subplot(gs[0, 0]); a2 = fig.add_subplot(gs[1, 0], sharex=a1); a3 = fig.add_subplot(gs[0, 1]); a4 = fig.add_subplot(gs[1, 1])
    a1.plot(d.date, 1 + d.cum_return, color="#1f4e79", lw=1.5, label="strategy")
    a1.plot(d.date, 1 + d.cum_return_leaders_vol_matched, color="#2e8b57", lw=1.1, label=f"Luk leaders, monthly equal-weight, net, vol-matched x{d.attrs.get('leaders_vol_scale', float('nan')):.2f}")
    a1.plot(d.date, 1 + d.cum_return_momentum_vol_matched, color="#d08c2a", lw=1.1, label=f"12-1 momentum top decile, monthly, net, vol-matched x{d.attrs.get('momentum_vol_scale', float('nan')):.2f}")
    a1.plot(d.date, 1 + d.cum_return_qqq_vol_matched, color="#7f7f7f", lw=1.1, label=f"QQQ, vol-matched x{d.attrs.get('qqq_vol_scale', float('nan')):.2f}")
    a1.plot(d.date, 1 + d.cum_return_qqq, color="#bbbbbb", lw=0.9, ls="--", label="QQQ")
    a1.set_yscale("log"); a1.set_ylabel("growth of 1 (log)"); a1.legend(loc="upper left", fontsize=9); a1.grid(alpha=.3); a1.set_title(title, loc="left")
    plt.setp(a1.get_xticklabels(), visible=False)
    av = 1 + d.cum_return.values; dd = av / np.maximum.accumulate(np.r_[1.0, av])[1:] - 1
    a2.fill_between(d.date, dd, 0, color="#c0392b", alpha=.5); a2.set_ylabel("drawdown", fontsize=9); a2.grid(alpha=.3)
    a2.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0)); a2.tick_params(labelsize=8)
    if t is not None and len(t):
        top = top_trades(t, 10).iloc[::-1]
        labels = [f"{r.ticker} {_ts(r.entry_day, -1)[:7]}  {r.price_return:+.0%}" for r in top.itertuples()]
        a3.barh(range(len(top)), top.share.values, color="#1f4e79"); a3.set_yticks(range(len(top))); a3.set_yticklabels(labels, fontsize=8.5, family="monospace")
        a3.yaxis.tick_right(); a3.yaxis.set_label_position("right")                       # labels on the outer edge, away from the left panel
        a3.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0)); a3.set_title("ten largest trades: share of net P&L", loc="left", fontsize=10)
        a3.xaxis.tick_top(); a3.set_xlim(0, float(top.share.max()) * 1.18)             # ticks on top, room for the value labels
        a3.grid(axis="x", alpha=.3); a3.tick_params(axis="x", labelsize=8); a3.tick_params(axis="y", length=0)
        for i, s_ in enumerate(top.share.values): a3.text(s_, i, f" {s_:.0%}", va="center", fontsize=8)
    dy = d.assign(y=d.date.dt.year)
    g = dy.groupby("y").agg(s=("cum_return", "last"), q=("cum_return_qqq_vol_matched", "last"), mo=("cum_return_momentum_vol_matched", "last"), le=("cum_return_leaders_vol_matched", "last"))
    yr_ = lambda c: ((1 + g[c]) / (1 + g[c]).shift(1).fillna(1.0) - 1).values
    x = np.arange(len(g)); wdt = 0.2
    a4.bar(x - 1.5 * wdt, yr_("s"), wdt, color="#1f4e79", label="strategy"); a4.bar(x - 0.5 * wdt, yr_("le"), wdt, color="#2e8b57", label="leaders vm")
    a4.bar(x + 0.5 * wdt, yr_("mo"), wdt, color="#d08c2a", label="momentum vm"); a4.bar(x + 1.5 * wdt, yr_("q"), wdt, color="#7f7f7f", label="QQQ vm")
    a4.set_xticks(x); a4.set_xticklabels([str(y)[2:] for y in g.index], fontsize=8); a4.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0)); a4.tick_params(axis="y", labelsize=8)
    a4.yaxis.tick_right(); a4.axhline(0, color="black", lw=0.6); a4.legend(fontsize=7, loc="upper left", ncol=2, title="by year (vm = vol-matched)", title_fontsize=8); a4.grid(axis="y", alpha=.3)
    fig.savefig(path, dpi=130); plt.close(fig)

def _md(rows, header):
    return "\n".join(["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(str(x) for x in r) + " |" for r in rows])

def summary_text(S, t, dl, panel, ff, scenarios, code_hash, notes) -> str:
    """summary.md for one spec: headline and benchmarks, chart, yearly table, factor attribution, largest trades, risk, execution."""
    L = []; m = metrics(t, dl, S, ff); d = daily_table(dl, panel, S, ff); bm = benchmark_metrics(d, S, ff)
    L.append(f"# `{S['name']}`: {specs.DESCRIPTIONS.get(S['name'], S['name'])}\n")
    if S["name"] == "full":
        L.append(specs.RULE + "\n")
    else:
        L.append(f"Same as `full` (see `results/full/summary.md`) except: " + specs.diff_from_full(S["name"]) + ".\n")
    L.append(f"Sample {C.SAMPLE_START} to {C.SAMPLE_END}, starting capital ${S['starting_capital']:,.0f}, code `{code_hash}`.  ")
    L.append(f"Entry `{S['entry']}` on the `{S['support']}` pullback, {pct(S['risk_per_trade'],1)} of the account at risk per trade, at most {S['max_positions']} positions and "
             f"{pct(S['max_gross_exposure'],0)} gross, market filter `{S['market_filter']}`, slippage `{S['slippage']}`, stop fill `{S['stop_fill']}`, "
             f"financing {'flat %.0f%%' % (100*S['margin_rate']) if S['margin_rate'] is not None else 'T-bill + %.1f%%' % (100*S['margin_spread'])}; idle cash earns the T-bill rate.")
    for n_ in notes: L.append(f"- note: {n_}")
    L.append("\n## Headline\n")
    L.append(_md([[f"{m['n']} ({m['per_year']:.0f}/yr)", pct(m['win'],0), pct(m['avg_win']), pct(m['avg_loss']), f"{m['payoff']:.1f}", pct(m['avg_trade'],2), f"{m['pf']:.2f}",
                   f"{m['hold_win']:.0f} / {m['hold_loss']:.0f}" if len(t) else "n/a", pct(m['exposure'],0), f"{m['underwater_days']/252:.1f} yr"]],
                 ["trades", "win rate", "avg win", "avg loss", "payoff", "avg trade", "profit/loss ratio", "median hold win / loss (days)", "avg % invested", "longest underwater"]))
    L.append("\nAvg % invested = capital utilisation: market value of open positions / net asset value, averaged over all trading days (the rest earns the T-bill rate); profit/loss ratio = gross profits / gross losses.\n")
    L.append("\nThe strategy against passive benchmarks, all after costs, all on daily returns. 6-factor alpha is the intercept of daily excess returns regressed on the Fama-French factors (market, size, value, profitability, investment, momentum), times 252: the return per year the factor exposures do not explain; t uses Newey-West errors (5 lags); beta is the market loading.\n")
    f_all = factor_regression(dl, ff, S)
    rows = [["**strategy**", pct(m['cagr']), pct(m['vol']), f"{m['sharpe']:.2f}", pct(m['maxdd'],0), f"{pct(f_all['alpha_annual'])} ({f_all['alpha_t']:.1f})" if f_all else "n/a", f"{f_all['betas']['mktrf'][0]:.2f}" if f_all else "n/a"]]
    for name, key in (("QQQ (total return)", "qqq"), (f"QQQ, vol-matched x{bm['scale']:.2f}", "qqq_vol_matched"), (f"12-1 momentum top decile ({bm['mom_n']:.0f} names), net", "momentum_net"),
                      (f"momentum, vol-matched x{bm['mom_scale']:.2f}", "momentum_vol_matched"), (f"universe hold: Luk leader screen ({bm['lead_n']:.0f} names), net", "leaders_net"), (f"universe hold, vol-matched x{bm['lead_scale']:.2f}", "leaders_vol_matched")):
        x = bm[key]; rows.append([name, pct(x['cagr']), pct(x['vol']), f"{x['sharpe']:.2f}", pct(x['maxdd'],0), f"{pct(x['alpha_annual'])} ({x['alpha_t']:.1f})", f"{x['beta_mkt']:.2f}"])
    L.append(_md(rows, ["", "CAGR", "vol", "Sharpe", "max drawdown", "6-factor alpha / yr (t)", "market beta"]))
    L.append("\nPassive benchmarks: equal-weight portfolios in the same liquidity pool (common stocks, price >= $5, 20-day dollar volume >= $50M), selected on the day before each month end, bought at the month end's close and held for the month, net of the same spread table plus commission. Vol-matched = a fraction k of the benchmark, chosen so the realised volatility equals the strategy's, with the rest in T-bills. Sharpe = annualised mean of daily returns in excess of the T-bill rate over their standard deviation.\n")
    L.append("![cumulative return](cumret.png)\n")
    L.append("## By year\n")
    dy = d.assign(y=d.date.dt.year); g = dy.groupby("y").agg(av=("account_value", "last"), q=("cum_return_qqq", "last"), qv=("cum_return_qqq_vol_matched", "last"), mv=("cum_return_momentum_vol_matched", "last"), lv=("cum_return_leaders_vol_matched", "last"))
    prev_av = g.av.shift(1).fillna(S["starting_capital"]); yr_ = lambda c, y: (1 + g[c][y]) / float((1 + g[c]).shift(1).fillna(1.0)[y]) - 1
    ty = pd.to_datetime(t.entry_day.values.astype("datetime64[D]")).year if len(t) else np.array([])
    rows = []
    for y in g.index:
        tt = t[ty == y] if len(t) else t; avy = np.r_[prev_av[y], dy[dy.y == y].account_value.values]
        rows.append([y, pct(g.av[y]/prev_av[y]-1), pct(yr_('q', y)), pct(yr_('qv', y)), pct(yr_('mv', y)), pct(yr_('lv', y)), len(tt), pct((tt.net_pnl>0).mean(),0) if len(tt) else "", pct(tt.net_return.mean(),2) if len(tt) else "", pct((avy/np.maximum.accumulate(avy)-1).min(),0)])
    L.append(_md(rows, ["year", "strategy", "QQQ", "QQQ vm", "momentum vm", "universe vm", "trades", "win", "avg trade", "max DD in year"]))
    L.append("\n## Factor attribution\n\nDaily excess returns on the Fama-French 5 factors plus momentum (Newey-West t-statistics).\n")
    rows = []
    for label, inv in (("all days", False), ("invested days only", True)):
        f = factor_regression(dl, ff, S, inv)
        if f is None:
            rows.append([label, "n/a"] + [""] * 8); continue
        rows.append([label, f['n'], f"{pct(f['alpha_annual'])} (t {f['alpha_t']:.1f})", f"{f['r2']:.2f}"] + [f"{b_:.2f} ({tv:.1f})" for b_, tv in f["betas"].values()])
    L.append(_md(rows, ["", "days", "6-factor alpha / yr", "R²", "mkt", "smb", "hml", "rmw", "cma", "umd"]))
    if len(t):
        L.append("\n## Largest trades\n")
        rows = [[r.ticker, _ts(r.entry_day, r.entry_minute), _ts(r.exit_day, r.exit_minute), r.exit_reason, f"{r.price_return:+.0%}", f"${r.net_pnl:,.0f}", pct(r.share,0)] for r in top_trades(t, 10).itertuples()]
        L.append(_md(rows, ["ticker", "entry", "exit", "exit reason", "return", "P&L", "share of net P&L"]))
        yr_top = t.assign(y=pd.to_datetime(t.entry_day.values.astype("datetime64[D]")).year).groupby("y")
        L.append("\nEach year's largest trade against the year's net P&L: " + "; ".join(f"{y} {g_.loc[g_.net_pnl.idxmax(), 'ticker']} ${g_.net_pnl.max():,.0f} / ${g_.net_pnl.sum():,.0f}" for y, g_ in yr_top) + ".")
        L.append("\n## Risk\n")
        srt = t.sort_values("net_pnl", ascending=False); wins = srt[srt.net_pnl > 0].net_pnl.sum(); k1 = max(1, len(srt) // 100); rest = srt.iloc[k1:]
        L.append(f"- Concentration: the top 5% of trades are {pct(srt.net_pnl.head(len(srt)//20).sum()/wins,0) if wins > 0 else 'n/a'} of gross wins; without the best 1% ({k1} trades) the average trade is {pct(rest.net_return.mean(),2)} and the profit/loss ratio {rest.loc[rest.net_pnl>0,'net_pnl'].sum()/max(-rest.loc[rest.net_pnl<=0,'net_pnl'].sum(),1e-9):.2f}.")
        L.append(f"- Trade returns (net): p5 {pct(t.net_return.quantile(.05))}, median {pct(t.net_return.median())}, p95 {pct(t.net_return.quantile(.95))}, best {pct(t.net_return.max())}, worst {pct(t.net_return.min())}.")
        gaps = t[t.exit_reason == "stop_gap"]
        L.append(f"- Gap-through stops: {len(gaps)} trades ({pct(len(gaps)/len(t))}), ${gaps.net_pnl.sum():,.0f} of the ${t.net_pnl.sum():,.0f} net; losses beyond twice the planned risk: {int((t.R<=-2).sum())}; closed by delisting: {int((t.exit_reason=='delist').sum())}; dividends credited ${t.dividends.sum():,.0f}; halted position-days: {t.attrs.get('halted_position_days', 0)}.")
        worst = t.nsmallest(5, "net_return")
        L.append("- Worst five: " + "; ".join(f"{r.ticker} {_ts(r.entry_day, -1)} {r.net_return:+.1%} ({r.exit_reason})" for r in worst.itertuples()) + ".")
        rng = np.random.default_rng(0); av = account_series(dl, S); rr = np.diff(av) / av[:-1]; rr_ex = rr - rf_on(dl.day.values.astype('datetime64[D]'), ff); B = 20; nb = len(rr) // B; sh, dds = [], []
        for _ in range(2000):
            idx = rng.integers(0, len(rr) - B + 1, size=nb); samp = np.concatenate([rr[i:i+B] for i in idx]); ex = np.concatenate([rr_ex[i:i+B] for i in idx]); av_ = np.r_[1.0, np.cumprod(1 + samp)]
            sh.append(ex.mean() / ex.std() * np.sqrt(252) if ex.std() > 0 else 0.0); dds.append((av_ / np.maximum.accumulate(av_) - 1).min())
        L.append(f"- Block bootstrap (20-day blocks, 2000 draws): Sharpe 90% interval [{np.percentile(sh,5):.2f}, {np.percentile(sh,95):.2f}], max drawdown median {pct(np.median(dds),0)}, 5th percentile {pct(np.percentile(dds,5),0)}.")
        part0 = t.notional * (S["starting_capital"] / t.account_value_at_entry) / t.adv; part1 = part0 * (1e6 / S["starting_capital"])
        L.append(f"- Capacity (position / 20-day dollar volume, sizing scaled to a fixed account): median {pct(part0.median(),2)} at ${S['starting_capital']:,.0f}; at $1M median {pct(part1.median(),2)}, {pct((part1>0.01).mean(),0)} of trades above 1%.")
        r_ = pd.Series(rr); dts = pd.to_datetime(dl.day.values.astype("datetime64[D]"))
        L.append(f"- Worst day {pct(r_.min())} ({dts[r_.idxmin()].date()}); days below -5%: {int((r_<-0.05).sum())}; max simultaneous positions {int(dl.n_positions.max())}.")
        L.append("\n## Execution and costs\n")
        tier = pd.cut(t.adv, [0, 30e6, 100e6, 1e13], labels=["<$30M", "$30-100M", ">$100M"])
        L.append(f"- Stop width median {pct(t.stop_width.median())}; planned loss per trade {pct(S['risk_per_trade'],1)} of the account.")
        L.append("- Entry half-spread by liquidity tier: " + "; ".join(f"{k} {pct(g_.entry_half_spread.median(),3)} (n={len(g_)}, avg trade {pct(g_.net_return.mean(),2)})" for k, g_ in t.groupby(tier, observed=True)) + ".")
        if S["slippage"] == "nbbo":
            L.append(f"- NBBO quotes (last quote before the fill minute) found for {pct(t.attrs.get('quote_coverage', np.nan),0)} of spread-paying fills (the rest use the tier table); exits with an exact minute: {pct((t.exit_minute>=0).mean(),0)}.")
        rows = [[name, pct(ms_['avg_trade'],2), f"{ms_['pf']:.2f}", pct(ms_['cagr']), f"{ms_['sharpe']:.2f}", pct(ms_['maxdd'],0)] for name, ms_ in ((n_, metrics(ts, ds, S, ff)) for n_, (ts, ds) in scenarios.items())]
        L.append("\n" + _md(rows, ["cost scenario", "avg trade", "profit/loss ratio", "CAGR", "Sharpe", "max drawdown"]))
    return "\n".join(L) + "\n"

def _git_commit():
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(C.ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(["git", "-C", str(C.ROOT), "status", "--porcelain", "--", "*.py"], capture_output=True, text=True, timeout=5).stdout.strip()
        return (out.stdout.strip() + ("+uncommitted" if dirty else "")) if out.returncode == 0 else None
    except Exception:
        return None

def write(outdir, S, t, dl, panel, ff, scenarios, code_hash, notes=()):
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "spec.json").write_text(json.dumps(dict(S, code_hash=code_hash, git_commit=_git_commit(), sample=[C.SAMPLE_START, C.SAMPLE_END], generated=pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")), indent=1, default=str))
    m = metrics(t, dl, S, ff); f = factor_regression(dl, ff, S); bm = benchmark_metrics(daily_table(dl, panel, S, ff), S, ff)
    m.update(alpha_annual=f["alpha_annual"] if f else np.nan, alpha_t=f["alpha_t"] if f else np.nan, beta_mkt=f["betas"]["mktrf"][0] if f else np.nan, notes=list(notes), qqq=bm["qqq"], qqq_vol_matched=bm["qqq_vol_matched"], qqq_vol_scale=bm["scale"], momentum=bm["momentum_net"], momentum_vol_matched=bm["momentum_vol_matched"], leaders=bm["leaders_net"], leaders_vol_matched=bm["leaders_vol_matched"],
             top_trades=[dict(ticker=r.ticker, entry=_ts(r.entry_day, -1), ret=round(float(r.price_return), 3), pnl=round(float(r.net_pnl)), share=round(float(r.share), 3)) for r in top_trades(t, 10).itertuples()] if len(t) else [])
    (outdir / "metrics.json").write_text(json.dumps(m, indent=1, default=str))
    trades_table(t).to_csv(outdir / "trades.csv", index=False)
    d = daily_table(dl, panel, S, ff); d.to_csv(outdir / "daily.csv", index=False)
    plot_cumret(d, outdir / "cumret.png", f"{S['name']}: cumulative return vs QQQ", t, S)
    txt = summary_text(S, t, dl, panel, ff, scenarios, code_hash, notes)
    (outdir / "summary.md").write_text(txt)
    return txt
