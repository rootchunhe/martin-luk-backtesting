"""Engine checks on synthetic data (no WRDS needed) plus, when the data is present, an empirical point-in-time check on real
minute bars. python3 selfcheck.py   (exit code 1 on any failure)"""
import sys, tempfile, shutil
from pathlib import Path
import numpy as np, pandas as pd
import config as C, specs, entry, portfolio

S0 = specs.get("base")
def spec(**ov): return dict(S0, **ov)
MORNING = dict(entry="morning_breakout", min_stop_width=0.003, cut_same_day=True)

# ---------------------------------------------------------------- entry: close_confirm
def bars(n=390, base=100.0, flush_at=120, flush_to=97.0, end=101.5, tail=None):
    m = np.arange(n); px = np.full(n, base)
    px[flush_at:flush_at + 5] = flush_to; px[flush_at + 5:] = np.linspace(flush_to + 1, end, n - flush_at - 5)
    b = pd.DataFrame(dict(m=m, open=px, high=px + 0.2, low=px - 0.2, close=px))
    if tail is not None:
        b.loc[b.m >= 386, ["open", "high", "low", "close"]] = tail
    return b

def check_future_bars_do_not_change_decision():
    e1 = entry.close_confirm(bars(), 98.0, 0.10, S0); e2 = entry.close_confirm(bars(tail=50.0), 98.0, 0.10, S0)
    assert e1 and e2 and e1["decision_px"] == e2["decision_px"] and e1["stop"] == e2["stop"] and e1["entry_px"] == e2["entry_px"]
    assert e2["sameday_stopped"] and not e1["sameday_stopped"]

def check_missing_fill_bar_means_no_trade():
    assert entry.close_confirm(bars()[bars().m <= 300], 98.0, 0.10, S0) is None                 # no print after the decision at all
    e = entry.close_confirm(bars()[bars().m != 385], 98.0, 0.10, S0)                             # no 15:55 print: the order fills at the next one
    assert e is not None and e["entry_minute"] == 386 and e["entry_px"] == bars().open[386]
    assert entry.close_confirm(bars()[(bars().m < 385) | (bars().m >= 390)], 98.0, 0.10, S0) is None   # nothing within 5 minutes: no fill

def check_touch_tolerance():
    assert entry.close_confirm(bars(), 96.0, 0.10, S0) is None                                   # low 96.8 never reaches 96
    assert entry.close_confirm(bars(), 97.0, 0.10, S0) is not None                               # trades through 97
    assert entry.close_confirm(bars(), 96.0, 0.10, spec(touch_tolerance_adr=0.25)) is not None   # within 2.5% above counts

# ---------------------------------------------------------------- entry: morning_breakout
def path_bars(px):
    return pd.DataFrame(dict(m=np.arange(len(px)), open=px, high=px + 0.2, low=px - 0.2, close=px))

def morning_path(after=None):
    """Open 100, flush to 96.5 at minutes 5-7 (low 96.3), minute 8 opens 97.6 (above the buy-stop at minute 7's high + tick)."""
    px = np.full(390, 100.0); px[5:8] = 96.5; px[8] = 97.6; px[9] = 98.4; px[10:] = np.linspace(98.5, 103.0, 380) if after is None else after
    return path_bars(px)

def check_morning_breakout_1min_fill_is_the_open_and_stop_is_the_prior_low():
    e = entry.morning_breakout(morning_path(), level=97.0, adr=0.10, prev_close=100.0, S=spec(**MORNING))
    assert e is not None and e["entry_minute"] == 8 and e["flush_minute"] == 5
    assert abs(e["entry_px"] - 97.6) < 1e-9 and abs(e["stop"] - 96.3) < 1e-9       # gapped above the buy-stop: filled at the open; stop = low of day before the fill bar
    assert not e["sameday_stopped"] and not e["sameday_cut"]
    assert entry.morning_breakout(morning_path(), 97.0, 0.10, 100.0, spec(**MORNING, max_chase_from_low=0.01)) is None

def check_morning_breakout_5min_fill_is_the_first_minute_through():
    """Flush in bucket 4 (minutes 20-24, high 97.2); minute 26 is the first bar through 97.2 + tick -> fill at 26, not at the bucket end."""
    px = np.full(390, 100.0); px[20:23] = 96.5; px[23:25] = 97.0; px[25] = 97.0; px[26] = 97.5; px[27:] = np.linspace(97.6, 102.0, 363)
    e = entry.morning_breakout(path_bars(px), level=97.0, adr=0.10, prev_close=100.0, S=spec(**MORNING))
    assert e is not None and e["entry_minute"] == 26 and abs(e["entry_px"] - 97.5) < 1e-9 and abs(e["stop"] - 96.3) < 1e-9

def check_morning_fill_bar_low_counts_as_a_stop_hit():
    px = np.full(390, 100.0); px[5:8] = 96.5; px[8] = 97.6; px[9:] = 100.0
    b = path_bars(px); b.loc[8, "low"] = 96.0                                     # the fill bar trades below the low of day: order inside the minute unknown
    e = entry.morning_breakout(b, 97.0, 0.10, 100.0, spec(**MORNING))
    assert e is not None and e["sameday_stopped"] and e["sameday_stop_minute"] == 8 and np.isnan(e["sameday_stop_open"])

def check_cut_same_day_decides_at_1554_and_fills_at_1555():
    path = morning_path(after=np.linspace(98.5, 97.0, 380))
    e = entry.morning_breakout(path, 97.0, 0.10, 100.0, spec(**MORNING))
    assert e is not None and not e["sameday_stopped"] and e["sameday_cut"] and e["sameday_cut_minute"] == 385
    assert abs(e["sameday_cut_px"] - path.open[385]) < 1e-9

# ---------------------------------------------------------------- portfolio
D = [pd.Timestamp("2020-01-02"), pd.Timestamp("2020-01-03"), pd.Timestamp("2020-01-06")]
def bar(permno, date, o, h, l, c, ema9=99.0, cfp=1.0, halted=False, ret=np.nan, delist="N"):
    return dict(permno=permno, date=date, open=o, high=h, low=l, close=c, ema9=ema9, ema21=98.0, ema50=97.0, cfp=cfp, halted=halted, ret=ret, delist_flag=delist)
def signal(permno, date, entry_px=100.0, stop=95.0, stopped=False, close=100.0, ema9=99.0, ticker="AAA", minute=385, cfp=1.0, at_open=True):
    return dict(permno=permno, ticker=ticker, date=date, cfp=cfp, close=close, ema9=ema9, ema21=98.0, ema50=97.0, stop=stop, entry_px=entry_px, decision_px=entry_px, entry_minute=minute, fill_at_open=at_open,
                adr_p=.1, advd_p=1e8, ret21_p=.3, ret63_p=.4, sameday_stopped=stopped, sameday_stop_minute=388 if stopped else -1, sameday_stop_open=np.nan,
                sameday_cut=False, sameday_cut_minute=-1, sameday_cut_px=np.nan, day_close=close)
def panel_of(rows):
    q = [bar(C.BENCHMARKS["QQQ"], d, 100, 101, 99, 100) for d in D]                 # QQQ rows define the trading days
    return pd.DataFrame(rows + q)
def dn(d): return int(pd.Timestamp(d).to_datetime64().astype("datetime64[D]").astype(np.int64))
def sim(rows, panel_rows, ov=None, held=None, fees=False):
    S = spec(**{**dict(slippage=0.0, risk_per_trade=0.01, partial_at_r=None, market_filter=None, stop_fill="minute_open"), **(ov or {})})
    saved = (C.COMMISSION_PER_SHARE, C.MIN_COMMISSION_PER_ORDER, C.SAMPLE_START, C.SAMPLE_END, C.SEC_FEE_SCHEDULE, C.FINRA_TAF_SCHEDULE)
    C.SAMPLE_START, C.SAMPLE_END = "2020-01-01", "2020-01-31"; C.SEC_FEE_SCHEDULE = []; C.FINRA_TAF_SCHEDULE = []
    if not fees: C.COMMISSION_PER_SHARE = 0.0; C.MIN_COMMISSION_PER_ORDER = 0.0
    try:
        return portfolio.simulate(S, pd.DataFrame(rows), panel_of(panel_rows), held=held)
    finally:
        C.COMMISSION_PER_SHARE, C.MIN_COMMISSION_PER_ORDER, C.SAMPLE_START, C.SAMPLE_END, C.SEC_FEE_SCHEDULE, C.FINRA_TAF_SCHEDULE = saved

def check_accounting_same_day_stop():
    tr, dy = sim([signal(1, D[0], stopped=True)], [bar(1, D[0], 100, 101, 95, 100), bar(1, D[1], 100, 101, 99, 100)])
    assert tr.net_pnl.sum() == -1000 and tr.exit_minute.iloc[0] == 388                  # 200 shares x $5, exit at 15:58
    assert (dy.account_value - dy.cash - dy.gross).abs().max() < 1e-6 and abs(dy.account_value.iloc[0] - 99000) < 1e-6

def check_empty_signals():
    tr, dy = sim([], [bar(1, D[0], 100, 101, 95, 100)])
    assert len(tr) == 0 and len(dy) == 3

def check_sizing_uses_prior_close_account_value():
    """Day 1: buy A (200 shares at 100). Day 2: A closes at 200 and B is bought at 15:55: sized on the prior-close account value
    (100,000 -> 200 shares), not on the day-2 close (120,000 -> 240)."""
    panel = [bar(1, D[0], 100, 101, 96, 100), bar(1, D[1], 150, 201, 150, 200), bar(1, D[2], 200, 201, 199, 200),
             bar(2, D[1], 100, 101, 96, 100), bar(2, D[2], 100, 101, 99, 100)]
    tr, _ = sim([signal(1, D[0]), signal(2, D[1], ticker="BBB")], panel)
    assert tr[tr.permno == 1].shares_raw.iloc[0] == 200 and tr[tr.permno == 2].shares_raw.iloc[0] == 200 and tr[tr.permno == 2].account_value_at_entry.iloc[0] == 100000

def check_exposure_room_uses_prior_close_marks():
    """Gross cap 50% of 100,000 = 50,000. A is 200 x 100 = 20,000 at the prior close (room 30,000 -> B gets its full 200 shares);
    at the day-2 close A is worth 40,000, which would leave room for only 100. The day's close is not known at 15:55."""
    panel = [bar(1, D[0], 100, 101, 96, 100), bar(1, D[1], 150, 201, 150, 200), bar(1, D[2], 200, 201, 199, 200),
             bar(2, D[1], 100, 101, 96, 100), bar(2, D[2], 100, 101, 99, 100)]
    tr, _ = sim([signal(1, D[0]), signal(2, D[1], ticker="BBB")], panel, {"max_gross_exposure": 0.5})
    assert tr[tr.permno == 2].shares_raw.iloc[0] == 200

def check_intraday_event_order():
    """One slot. A stops out on day 2: at 15:59 the slot is still taken when B's 09:40 signal fires (no B trade); at 09:35 it is free."""
    panel = [bar(1, D[0], 100, 101, 96, 100), bar(1, D[1], 100, 101, 94, 94), bar(1, D[2], 94, 95, 93, 94),
             bar(2, D[1], 100, 101, 96, 100), bar(2, D[2], 100, 101, 99, 100)]
    late = pd.DataFrame(dict(m=np.arange(390), open=100.0, high=100.5, low=99.5, close=100.0)); late.loc[389, ["open", "low"]] = [94.5, 94.0]
    tr, _ = sim([signal(1, D[0]), signal(2, D[1], ticker="BBB", minute=10)], panel, {"max_positions": 1}, held={(1, dn(D[1])): late})
    assert set(tr.permno) == {1} and tr.exit_minute.iloc[0] == 389
    early = late.copy(); early.loc[389, ["open", "low"]] = [100.0, 99.5]; early.loc[5, ["open", "low"]] = [94.5, 94.0]
    tr, _ = sim([signal(1, D[0]), signal(2, D[1], ticker="BBB", minute=10)], panel, {"max_positions": 1}, held={(1, dn(D[1])): early})
    assert set(tr.permno) == {1, 2} and tr[tr.permno == 1].exit_minute.iloc[0] == 5
    tr, _ = sim([signal(1, D[0]), signal(2, D[1], ticker="BBB", minute=10)], panel, {"max_positions": 1})     # minute unknown: counted as end of day
    assert set(tr.permno) == {1}

def check_stop_fills():
    """A gaps through the stop (filled at the open, 2R loss); B trades through it intraday: without minute bars at the stop price,
    with minute bars at the open of the minute that traded through (94.5 < stop 95 -> 94.5 at minute 125)."""
    panel = [bar(1, D[0], 100, 101, 96, 100), bar(1, D[1], 90, 95, 85, 92), bar(1, D[2], 92, 93, 91, 92),
             bar(2, D[0], 100, 101, 96, 100), bar(2, D[1], 100, 101, 94, 99), bar(2, D[2], 99, 100, 98, 99)]
    tr, _ = sim([signal(1, D[0]), signal(2, D[0], ticker="BBB")], panel)
    a, b = tr[tr.permno == 1].iloc[0], tr[tr.permno == 2].iloc[0]
    assert a.exit_reason == "stop_gap" and a.exit_price == 90 and a.R == -2.0 and a.exit_minute == 0   # zero slippage in this helper
    assert b.exit_reason == "stop" and b.exit_price == 95 and b.R == -1.0 and b.exit_minute == -1
    mb = pd.DataFrame(dict(m=np.arange(390), open=100.0, high=100.5, low=99.5, close=100.0)); mb.loc[125, ["open", "low"]] = [94.5, 94.0]
    tr, _ = sim([signal(2, D[0], ticker="BBB")], panel[3:], held={(2, dn(D[1])): mb})
    b = tr.iloc[0]; assert b.exit_reason == "stop" and b.exit_price == 94.5 and b.exit_minute == 125

def check_partial_before_stop_when_the_minutes_say_so():
    """Day 2 reaches the 3R target at minute 30 and the stop at minute 300: partial first, then the stop on the remainder."""
    panel = [bar(1, D[0], 100, 101, 96, 100), bar(1, D[1], 100, 120, 94, 95), bar(1, D[2], 95, 96, 94, 95)]
    mb = pd.DataFrame(dict(m=np.arange(390), open=100.0, high=100.5, low=99.5, close=100.0)); mb.loc[30, "high"] = 120.0; mb.loc[300, ["open", "low"]] = [94.8, 94.0]
    tr, _ = sim([signal(1, D[0])], panel, {"partial_at_r": 3.0, "partial_fraction": 0.15}, held={(1, dn(D[1])): mb})
    r = tr.iloc[0]
    assert r.partial_minute == 30 and r.partial_price == 115 and r.partial_shares_raw == 30 and r.exit_reason == "stop" and r.exit_minute == 300 and r.exit_price == 94.8
    tr, _ = sim([signal(1, D[0])], panel, {"partial_at_r": 3.0, "partial_fraction": 0.15})          # minutes unknown: the stop is assumed first
    assert tr.iloc[0].partial_day == -1 and tr.iloc[0].exit_reason == "stop"

def check_partial_needs_the_same_minute_confirmation_as_the_stop():
    """Day 2's daily bar says high 120 / low 90 but the minute bars never leave 99.5-100.5: neither the stop nor the partial fills."""
    panel = [bar(1, D[0], 100, 101, 96, 100), bar(1, D[1], 100, 120, 90, 100), bar(1, D[2], 100, 101, 99, 100)]
    mb = pd.DataFrame(dict(m=np.arange(390), open=100.0, high=100.5, low=99.5, close=100.0))
    tr, _ = sim([signal(1, D[0])], panel, {"partial_at_r": 3.0, "partial_fraction": 0.15}, held={(1, dn(D[1])): mb})
    r = tr.iloc[0]; assert r.partial_day == -1 and r.exit_reason == "sample_end" and r.partial_pnl == 0

def check_fill_under_the_stop_is_a_recorded_loss():
    """The 15:55 print is at 96 with the stop at 96.8: the order was in, it fills, and the stop takes it out at once at 96.
    Sized on the planned risk (decision price 100 - stop 96.8 = 3.2 -> 312 shares at 1%, capped at 30% of the account on the decision price -> 300)."""
    s = signal(1, D[0], entry_px=96.0, stop=96.8, stopped=True); s.update(sameday_stop_minute=385, sameday_stop_open=96.0, decision_px=100.0)
    tr, dy = sim([s], [bar(1, D[0], 100, 101, 95, 100), bar(1, D[1], 100, 101, 99, 100)])
    r = tr.iloc[0]; assert len(tr) == 1 and r.exit_reason == "stop_sameday" and r.exit_minute == 385 and r.exit_price == 96.0 and r.shares_raw == 300 and abs(r.net_pnl) < 1e-9
    assert abs(r.risk_dollars - 300 * 3.2) < 1e-9 and (dy.account_value - dy.cash - dy.gross).abs().max() < 1e-6

def check_passive_hold_compounds_within_the_month():
    """Two stocks, equal weight: one doubles then halves, the other is flat -> the month's return is 0, not +12.5% (daily re-equal-weighting)."""
    import report
    seg = pd.DataFrame({"A": [1.0, -0.5], "B": [0.0, 0.0]}, index=pd.to_datetime(["2020-01-02", "2020-01-03"]))
    daily, w_end = report.hold_returns(seg, pd.Series({"A": 0.5, "B": 0.5}))
    assert abs((1 + daily).prod() - 1) < 1e-12 and abs(w_end["A"] - 0.5) < 1e-12
    seg = pd.DataFrame({"A": [1.0, np.nan], "B": [0.0, 0.1]}, index=seg.index)                 # A is gone after day 1: it stays at its last value
    daily, w_end = report.hold_returns(seg, pd.Series({"A": 0.5, "B": 0.5}))
    assert abs((1 + daily).prod() - (2 * 0.5 + 1.1 * 0.5)) < 1e-12 and abs(w_end["A"] - 1.0 / 1.55) < 1e-12

def check_full_loss_delisting_row_survives_the_panel_build():
    """A -100% delisting row has no price; it must stay in the panel (settled at 0) so the position is written off, not closed at the last close."""
    import features
    days = pd.to_datetime(["2020-01-02", "2020-01-03", "2020-01-06"])
    df = pd.DataFrame(dict(permno=[1, 1, 1], dlycaldt=days, dlyopen=[10.0, 10.0, np.nan], dlyhigh=[10.5, 10.5, np.nan], dlylow=[9.5, 9.5, np.nan], dlyclose=[10.0, 10.0, np.nan], dlyprc=[10.0, 10.0, 0.0],
                           dlyvol=[1e5, 1e5, np.nan], dlycap=[1e6, 1e6, np.nan], dlyret=[0.0, 0.0, -1.0], dlydelflg=["N", "N", "Y"], dlycumfacpr=[1.0, 1.0, 1.0], dlycumfacshr=[1.0, 1.0, 1.0]))
    sh = pd.DataFrame(dict(permno=[1], secinfostartdt=[days[0]], secinfoenddt=[days[-1]]))
    saved = (C.PANEL, C.PANEL_META); tmp = Path(tempfile.mkdtemp()); C.PANEL, C.PANEL_META = tmp / "p.parquet", tmp / "p.json"
    try:
        out = features.build(df, sh, {}, log=lambda *a: None)
    finally:
        C.PANEL, C.PANEL_META = saved; shutil.rmtree(tmp)
    last = out.iloc[-1]; assert len(out) == 3 and last.delist_flag == "Y" and last.ret == -1.0 and last.close == 0.0 and bool(last.halted)
    tr, _ = sim([signal(1, D[0], entry_px=10.0, stop=9.5)], [bar(1, D[0], 10, 10.5, 9.5, 10), bar(1, D[1], 10, 10.5, 9.9, 10, ret=-1.0, delist="Y", halted=True)])
    r = tr.iloc[0]; assert r.exit_reason == "delist" and r.exit_price == 0.0 and abs(r.net_pnl + r.shares_raw * 10.0) < 1e-9

def check_quantity_is_fixed_at_the_decision_not_at_the_fill():
    """Decision price 100, stop 99, 1% risk, 30% cap -> 300 shares by the cap, whether the 15:55 print is 100 or 110."""
    out = []
    for fill in (100.0, 110.0):
        s = signal(1, D[0], entry_px=fill, stop=99.0); s["decision_px"] = 100.0
        tr, _ = sim([s], [bar(1, D[0], 100, 112, 99, 100), bar(1, D[1], 100, 101, 99, 100)], {"max_position_pct": 0.30})
        out.append((int(tr.shares_raw.iloc[0]), float(tr.planned_risk.iloc[0]), float(tr.notional.iloc[0])))
    assert out[0][0] == out[1][0] == 300 and out[0][1] == out[1][1] == 1.0 and out[1][2] == 300 * 110.0

def check_rebalance_cost_charges_a_full_liquidation():
    import report
    adv = pd.Series({"A": 1e9, "B": 1e9}); half = C.HALF_SPREAD_FALLBACK["close"][-1][1] + 0.0001
    full = report.rebalance_cost(pd.Series({"A": 0.5, "B": 0.5}), pd.Series(dtype=float), adv)       # everything sold
    swap = report.rebalance_cost(pd.Series({"A": 0.5, "B": 0.5}), pd.Series({"A": 0.5, "B": 0.5}), adv)
    assert abs(full - half) < 1e-12 and swap == 0.0

def check_market_filter_uses_the_prior_days_qqq_emas():
    """QQQ 9 EMA < 21 EMA on day 2 only. A signal on day 2 is taken (the filter looks at day 1), a signal on day 3 is not (it looks at day 2)."""
    rows = [bar(1, D[0], 100, 101, 96, 100), bar(1, D[1], 100, 101, 96, 100), bar(1, D[2], 100, 101, 96, 100), bar(2, D[1], 100, 101, 96, 100), bar(2, D[2], 100, 101, 96, 100)]
    q = [bar(C.BENCHMARKS["QQQ"], D[0], 100, 101, 99, 100, ema9=99.0), bar(C.BENCHMARKS["QQQ"], D[1], 100, 101, 99, 100, ema9=90.0), bar(C.BENCHMARKS["QQQ"], D[2], 100, 101, 99, 100, ema9=99.0)]
    S = spec(slippage=0.0, risk_per_trade=0.01, partial_at_r=None, market_filter="qqq_9>21", stop_fill="minute_open")
    saved = (C.SAMPLE_START, C.SAMPLE_END); C.SAMPLE_START, C.SAMPLE_END = "2020-01-01", "2020-01-31"
    try:
        tr, _ = portfolio.simulate(S, pd.DataFrame([signal(1, D[1]), signal(2, D[2], ticker="BBB")]), pd.DataFrame(rows + q))
    finally:
        C.SAMPLE_START, C.SAMPLE_END = saved
    assert list(tr.permno) == [1]
    try:
        portfolio.simulate(dict(S, market_filter="spy_9>21"), pd.DataFrame([]), pd.DataFrame(rows + q)); assert False
    except ValueError:
        pass

def check_priority_and_max_positions():
    """Three signals in the same minute, two slots: the two strongest 3-month performers are taken."""
    panel = [bar(p, D[0], 100, 101, 96, 100) for p in (1, 2, 3)] + [bar(p, D[1], 100, 101, 99, 100) for p in (1, 2, 3)]
    sigs = [dict(signal(1, D[0], ticker="A"), ret63_p=.4), dict(signal(2, D[0], ticker="B"), ret63_p=.9), dict(signal(3, D[0], ticker="C"), ret63_p=.6)]
    tr, _ = sim(sigs, panel, {"max_positions": 2})
    assert sorted(tr.ticker) == ["B", "C"]

def check_signal_skipped_when_exposure_room_is_under_a_quarter():
    """Gross cap 24% of 100,000. A takes 20,000; B's intended 20,000 would get only 4,000 of room (20%) -> skipped, not scaled down."""
    panel = [bar(1, D[0], 100, 101, 96, 100), bar(1, D[1], 100, 101, 99, 100), bar(2, D[0], 100, 101, 96, 100), bar(2, D[1], 100, 101, 99, 100)]
    tr, _ = sim([signal(1, D[0]), dict(signal(2, D[0], ticker="BBB"), ret63_p=.1)], panel, {"max_gross_exposure": 0.24})
    assert list(tr.ticker) == ["AAA"]
    tr, _ = sim([signal(1, D[0]), dict(signal(2, D[0], ticker="BBB"), ret63_p=.1)], panel, {"max_gross_exposure": 0.25})   # exactly a quarter: taken
    assert sorted(tr.ticker) == ["AAA", "BBB"] and tr[tr.ticker == "BBB"].shares_raw.iloc[0] == 50

def check_minute_zero_fills_use_the_morning_fallback_not_a_later_quote():
    """A gap through the stop at the open is a market order in minute 0: no quote precedes the fill, so it pays the morning-tier fallback,
    neither the pre-open book (minute 0 key) nor the 09:30-09:31 quote (minute 1 key)."""
    panel = [bar(1, D[0], 100, 101, 96, 100), bar(1, D[1], 90, 92, 89, 91)]
    S = spec(slippage="nbbo", risk_per_trade=0.01, partial_at_r=None, market_filter=None)
    quotes = {("AAA", dn(D[0]), 385): 0.001, ("AAA", dn(D[1]), 0): 0.05, ("AAA", dn(D[1]), 1): 0.004}
    saved = (C.COMMISSION_PER_SHARE, C.MIN_COMMISSION_PER_ORDER, C.SAMPLE_START, C.SAMPLE_END, C.SEC_FEE_SCHEDULE, C.FINRA_TAF_SCHEDULE)
    C.COMMISSION_PER_SHARE = 0.0; C.MIN_COMMISSION_PER_ORDER = 0.0; C.SAMPLE_START, C.SAMPLE_END = "2020-01-01", "2020-01-31"; C.SEC_FEE_SCHEDULE = []; C.FINRA_TAF_SCHEDULE = []
    try:
        tr, _ = portfolio.simulate(S, pd.DataFrame([signal(1, D[0])]), panel_of(panel), quotes=quotes)
    finally:
        C.COMMISSION_PER_SHARE, C.MIN_COMMISSION_PER_ORDER, C.SAMPLE_START, C.SAMPLE_END, C.SEC_FEE_SCHEDULE, C.FINRA_TAF_SCHEDULE = saved
    r = tr.iloc[0]; assert r.exit_reason == "stop_gap" and r.exit_minute == 0 and r.exit_half_spread == portfolio.fallback_half_spread(1e8, 0)

def check_kernel_emas_match_pandas():
    """The numba EMAs equal pandas ewm(span, adjust=False) on a random series; the AVWAP equals the VWAP from the 250-day high."""
    import features
    rng = np.random.default_rng(3); n = 400; close = 50 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    days = pd.bdate_range("2019-01-01", periods=n)
    df = pd.DataFrame(dict(permno=1, dlycaldt=days, dlyopen=close, dlyhigh=close * 1.01, dlylow=close * 0.99, dlyclose=close, dlyprc=close, dlyvol=1e5,
                           dlycap=1e6, dlyret=0.0, dlydelflg="N", dlycumfacpr=1.0, dlycumfacshr=1.0))
    sh = pd.DataFrame(dict(permno=[1], secinfostartdt=[days[0]], secinfoenddt=[days[-1]]))
    saved = (C.PANEL, C.PANEL_META); tmp = Path(tempfile.mkdtemp()); C.PANEL, C.PANEL_META = tmp / "p.parquet", tmp / "p.json"
    try:
        out = features.build(df, sh, {}, log=lambda *a: None)
    finally:
        C.PANEL, C.PANEL_META = saved; shutil.rmtree(tmp)
    s = pd.Series(close)
    for span in (9, 21, 50):
        assert np.allclose(out[f"ema{span}"].values[60:], s.ewm(span=span, adjust=False).mean().values[60:], rtol=1e-10)
    i = n - 1; w = C.AVWAP_ANCHOR_WINDOW; hi = out.high.values[i - w + 1:i + 1]; a = i - w + 1 + int(np.argmax(hi))
    tp = (out.high.values + out.low.values + out.close.values) / 3; v_ = out.vol.values
    ref = (tp[a:i + 1] * v_[a:i + 1]).sum() / v_[a:i + 1].sum()
    assert abs(out.avwap.values[i] - ref) < 1e-6 * ref or abs(out.avwap.values[i] - (out.close.values[a:i + 1] * v_[a:i + 1]).sum() / v_[a:i + 1].sum()) < 1e-6 * ref

def check_ema_exit_and_max_hold():
    panel = [bar(1, D[0], 100, 101, 96, 100), bar(1, D[1], 100, 101, 97, 98), bar(1, D[2], 97, 99, 96, 98)]    # day-2 close 98 < 9 EMA 99
    tr, _ = sim([signal(1, D[0])], panel)
    assert tr.exit_reason.iloc[0] == "close_below_ema9" and tr.exit_price.iloc[0] == 97 and tr.exit_minute.iloc[0] == 0 and tr.hold_days.iloc[0] == 3
    panel = [bar(1, D[0], 100, 101, 96, 100), bar(1, D[1], 100, 101, 99, 100), bar(1, D[2], 101, 102, 100, 101)]
    tr, _ = sim([signal(1, D[0])], panel, {"max_hold_days": 2})
    assert tr.exit_reason.iloc[0] == "max_hold" and tr.exit_price.iloc[0] == 101

def check_halted_day_has_no_fills():
    """Day 2 is a halt (no volume): its low of 90 cannot fill the stop; day 3 opens at 96 and trades through 95 -> stop on day 3."""
    panel = [bar(1, D[0], 100, 101, 96, 100), bar(1, D[1], 100, 100, 90, 100, halted=True), bar(1, D[2], 96, 97, 94, 96)]
    tr, _ = sim([signal(1, D[0])], panel)
    assert tr.exit_day.iloc[0] == dn(D[2]) and tr.exit_reason.iloc[0] == "stop" and tr.exit_price.iloc[0] == 95

def check_split_adjusted_units_and_per_share_fees():
    """Prices are split-adjusted by 2 (raw = 2 x adjusted): the trader held 100 raw shares, not 200, and pays commission on 100."""
    panel = [bar(1, D[0], 100, 101, 96, 100, cfp=2.0), bar(1, D[1], 100, 101, 97, 98, cfp=2.0), bar(1, D[2], 97, 99, 96, 98, cfp=2.0)]
    tr, _ = sim([signal(1, D[0], cfp=2.0)], panel, fees=True)
    r = tr.iloc[0]
    assert r.shares_raw == 100 and abs(r.notional - 20000) < 1e-9 and abs(r.risk_dollars - 1000) < 1e-9
    assert abs(r.costs - 2 * max(0.0035 * 100, 0.35)) < 1e-9 and abs(r.net_pnl - (200 * (97 - 100) - r.costs)) < 1e-9

def check_spread_applies_to_both_fills():
    panel = [bar(1, D[0], 100, 101, 96, 100), bar(1, D[1], 100, 101, 97, 98), bar(1, D[2], 97, 99, 96, 98)]
    tr, _ = sim([signal(1, D[0])], panel, {"slippage": 0.001})
    r = tr.iloc[0]; assert abs(r.entry_price - 100.1) < 1e-9 and abs(r.exit_price - 97 * 0.999) < 1e-9

def check_cut_same_day_is_not_cancelled_by_a_later_stop():
    """15:54 close below the entry -> cut at 15:55, even if the stop would have been hit at 15:58."""
    after = np.linspace(98.5, 97.0, 380); path = morning_path(after=after); path.loc[388, "low"] = 90.0
    e = entry.morning_breakout(path, 97.0, 0.10, 100.0, spec(**MORNING))
    assert e is not None and e["sameday_cut"] and e["sameday_cut_minute"] == 385 and not e["sameday_stopped"]
    path.loc[300, "low"] = 90.0                                                   # a stop before 15:54 still comes first
    e = entry.morning_breakout(path, 97.0, 0.10, 100.0, spec(**MORNING))
    assert e is not None and e["sameday_stopped"] and e["sameday_stop_minute"] == 300 and not e["sameday_cut"]

def check_same_minute_exit_does_not_free_the_slot():
    """One slot. A's stop trades inside minute 10 (open above the stop); B's 09:40 open fill is simultaneous -> B is not taken.
    With A's stop at minute 9, B is taken."""
    panel = [bar(1, D[0], 100, 101, 96, 100), bar(1, D[1], 100, 101, 94, 94), bar(1, D[2], 94, 95, 93, 94),
             bar(2, D[1], 100, 101, 96, 100), bar(2, D[2], 100, 101, 99, 100)]
    mb = pd.DataFrame(dict(m=np.arange(390), open=100.0, high=100.5, low=99.5, close=100.0)); mb.loc[10, "low"] = 94.0
    tr, _ = sim([signal(1, D[0]), signal(2, D[1], ticker="BBB", minute=10)], panel, {"max_positions": 1}, held={(1, dn(D[1])): mb})
    assert set(tr.permno) == {1} and tr.exit_minute.iloc[0] == 10
    mb9 = mb.copy(); mb9.loc[10, "low"] = 99.5; mb9.loc[9, "low"] = 94.0
    tr, _ = sim([signal(1, D[0]), signal(2, D[1], ticker="BBB", minute=10)], panel, {"max_positions": 1}, held={(1, dn(D[1])): mb9})
    assert set(tr.permno) == {1, 2}

def check_entry_day_partial():
    """09:40 morning fill at 100 (stop 95), the 3R target 115 is reached at 10:00 the same day -> partial at minute 30."""
    panel = [bar(1, D[0], 100, 120, 96, 118), bar(1, D[1], 118, 119, 117, 118), bar(1, D[2], 118, 119, 117, 118)]
    mb = pd.DataFrame(dict(m=np.arange(390), open=100.0, high=100.5, low=99.5, close=100.0)); mb.loc[30:, ["open", "high", "low", "close"]] = [118.0, 120.0, 117.0, 118.0]
    tr, _ = sim([signal(1, D[0], minute=10, close=118.0)], panel, {"partial_at_r": 3.0, "partial_fraction": 0.15}, held={(1, dn(D[0])): mb})
    r = tr.iloc[0]; assert r.partial_day == dn(D[0]) and r.partial_minute == 30 and r.partial_price == 115 and r.partial_shares_raw == 30
    mb2 = mb.copy(); mb2.loc[10, "high"] = 116.0                                                      # target reached inside the fill minute: counts after an at-open fill
    tr, _ = sim([signal(1, D[0], minute=10, close=118.0)], panel, {"partial_at_r": 3.0, "partial_fraction": 0.15}, held={(1, dn(D[0])): mb2})
    assert tr.iloc[0].partial_minute == 10
    tr, _ = sim([signal(1, D[0], minute=10, close=118.0, at_open=False)], panel, {"partial_at_r": 3.0, "partial_fraction": 0.15}, held={(1, dn(D[0])): mb2})
    assert tr.iloc[0].partial_minute == 30                                                           # a within-minute fill: the same minute's high may precede it

def check_dividend_and_delisting_settlement():
    """Day 2: total return 5% with an unchanged close -> a $5 dividend per share is credited (whether or not later rows exist).
    Permno 2 is flagged delisted on day 2 with a -50% total return -> settled at 50 on day 2, not at the last close of 100, and
    the same when day 2 is the last day of the sample."""
    panel = [bar(1, D[0], 100, 101, 96, 100), bar(1, D[1], 100, 101, 99, 100, ret=0.05), bar(1, D[2], 100, 101, 99, 100),
             bar(2, D[0], 100, 101, 96, 100), bar(2, D[1], 100, 101, 99, 100, ret=-0.5, delist="Y", halted=True)]
    tr, dy = sim([signal(1, D[0]), signal(2, D[0], ticker="BBB")], panel)
    a, b = tr[tr.permno == 1].iloc[0], tr[tr.permno == 2].iloc[0]
    assert abs(a.dividends - 200 * 5) < 1e-9 and a.exit_reason == "sample_end"
    assert b.exit_reason == "delist" and abs(b.exit_price - 50) < 1e-9 and b.exit_day == dn(D[1]) and abs(b.net_pnl - 200 * (50 - 100)) < 1e-9
    assert (dy.account_value - dy.cash - dy.gross).abs().max() < 1e-6
    tr1, _ = sim([signal(1, D[0])], panel[:2]); assert abs(tr1.iloc[0].dividends - 1000) < 1e-9        # dividend does not depend on a third row
    tr2, _ = sim([signal(2, D[0], ticker="BBB")], panel[3:]); assert tr2.iloc[0].exit_reason == "delist" and abs(tr2.iloc[0].exit_price - 50) < 1e-9

def check_cut_fills_at_the_1555_open_even_below_the_stop():
    panel = [bar(1, D[0], 100, 101, 94, 95), bar(1, D[1], 95, 96, 94, 95), bar(1, D[2], 95, 96, 94, 95)]
    row = dict(signal(1, D[0], entry_px=97.6, stop=96.3, minute=8, close=95.0), sameday_cut=True, sameday_cut_minute=385, sameday_cut_px=95.0)
    tr, _ = sim([row], panel)
    assert tr.exit_reason.iloc[0] == "cut_sameday" and abs(tr.exit_price.iloc[0] - 95.0) < 1e-9 and tr.exit_minute.iloc[0] == 385

def check_open_auction_fills_pay_no_spread_but_other_fills_do():
    panel = [bar(1, D[0], 100, 101, 96, 100), bar(1, D[1], 100, 101, 97, 98), bar(1, D[2], 97, 99, 96, 98)]      # 9 EMA exit at the day-3 open
    S = spec(slippage="nbbo", risk_per_trade=0.01, partial_at_r=None, market_filter=None)
    quotes = {("AAA", dn(D[0]), 385): 0.01, ("AAA", dn(D[2]), 0): 0.02}
    saved = (C.COMMISSION_PER_SHARE, C.MIN_COMMISSION_PER_ORDER, C.SAMPLE_START, C.SAMPLE_END, C.SEC_FEE_SCHEDULE, C.FINRA_TAF_SCHEDULE)
    C.COMMISSION_PER_SHARE = 0.0; C.MIN_COMMISSION_PER_ORDER = 0.0; C.SAMPLE_START, C.SAMPLE_END = "2020-01-01", "2020-01-31"; C.SEC_FEE_SCHEDULE = []; C.FINRA_TAF_SCHEDULE = []
    try:
        tr, _ = portfolio.simulate(S, pd.DataFrame([signal(1, D[0])]), panel_of(panel), quotes=quotes)
    finally:
        C.COMMISSION_PER_SHARE, C.MIN_COMMISSION_PER_ORDER, C.SAMPLE_START, C.SAMPLE_END, C.SEC_FEE_SCHEDULE, C.FINRA_TAF_SCHEDULE = saved
    r = tr.iloc[0]
    assert abs(r.entry_price - 101.0) < 1e-9 and r.entry_half_spread == 0.01            # 15:55 open fill pays the quoted half-spread
    assert r.exit_reason == "close_below_ema9" and abs(r.exit_price - 97.0) < 1e-9 and r.exit_half_spread == 0.0   # the opening auction does not

def check_delisting_settlement_in_raw_shares_pays_no_fee():
    """Split-adjusted units (cfp 4): 50 raw shares in and out; the delisting payoff is a settlement, so the only cost is the entry commission."""
    panel = [bar(1, D[0], 100, 101, 96, 100, cfp=4.0), bar(1, D[1], 100, 101, 99, 100, cfp=4.0, ret=-0.5, delist="Y")]
    tr, _ = sim([signal(1, D[0], cfp=4.0)], panel, fees=True)
    r = tr.iloc[0]; assert r.exit_reason == "delist" and r.shares_raw == 50 and abs(r.exit_shares_raw - 50) < 1e-9 and abs(r.costs - 0.35) < 1e-9 and r.exit_half_spread == 0.0

def check_close_confirm_open_below_lod_is_a_stopped_trade():
    b = bars(); b.loc[385, "open"] = 96.0                                                    # the 15:55 bar opens under the low of day
    e = entry.close_confirm(b, 98.0, 0.10, S0)
    assert e is not None and e["sameday_stopped"] and e["sameday_stop_minute"] == 385 and e["sameday_stop_open"] == 96.0

def check_thin_tape_guard():
    b = bars(); keep = (b.m < 300) | (b.m >= 385)                                            # no priced minute between 14:30 and 15:54
    assert entry.close_confirm(b[keep], 98.0, 0.10, S0) is None
    assert entry.close_confirm(b[(b.m >= 330)], 98.0, 0.10, S0) is None                      # only 55 priced minutes before the decision

def check_idle_cash_earns_rf_and_borrowed_cash_pays_margin():
    panel = [bar(1, D[0], 100, 101, 96, 100), bar(1, D[1], 100, 101, 99, 100), bar(1, D[2], 100, 101, 99, 100)]
    S = spec(slippage=0.0, risk_per_trade=0.01, partial_at_r=None, market_filter=None, margin_rate=0.0)
    saved = (C.COMMISSION_PER_SHARE, C.MIN_COMMISSION_PER_ORDER, C.SAMPLE_START, C.SAMPLE_END, C.SEC_FEE_SCHEDULE, C.FINRA_TAF_SCHEDULE)
    C.COMMISSION_PER_SHARE = 0.0; C.MIN_COMMISSION_PER_ORDER = 0.0; C.SAMPLE_START, C.SAMPLE_END = "2020-01-01", "2020-01-31"; C.SEC_FEE_SCHEDULE = []; C.FINRA_TAF_SCHEDULE = []
    try:
        rf = {dn(D[0]): 0.0001, dn(D[1]): 0.0001, dn(D[2]): 0.0001}
        tr, dy = portfolio.simulate(S, pd.DataFrame([signal(1, D[0])]), panel_of(panel), rf=rf)
        assert abs(dy.cash.iloc[0] - 80000 * 1.0001) < 1e-6                                       # day 1: 80,000 idle after the buy earns one day of rf
        interest = dy.account_value.iloc[-1] - (100000 + tr.net_pnl.sum())
        assert 0 < interest < 80000 * 1.0001 ** 3 - 80000 + 1e-6                                  # interest is outside trade P&L and bounded by three days on the idle cash
        S2 = dict(S, margin_rate=None, margin_spread=0.0252, max_position_pct=2.0, max_gross_exposure=3.0, risk_per_trade=0.1)     # borrow: 2,000 shares x 100 = 200,000 -> cash -100,000
        tr2, dy2 = portfolio.simulate(S2, pd.DataFrame([signal(1, D[0], stop=95.0)]), panel_of(panel), rf=rf)
        assert abs(dy2.cash.iloc[0] - (-100000) * (1 + 0.0001 + 0.0252 / 252)) < 1e-6              # one day of T-bill + spread on the borrowed cash
        tr0, dy0 = portfolio.simulate(S, pd.DataFrame([signal(1, D[0])]), panel_of(panel))
        assert abs(dy0.cash.iloc[0] - 80000) < 1e-6                                               # no rf given: nothing credited
    finally:
        C.COMMISSION_PER_SHARE, C.MIN_COMMISSION_PER_ORDER, C.SAMPLE_START, C.SAMPLE_END, C.SEC_FEE_SCHEDULE, C.FINRA_TAF_SCHEDULE = saved

def check_fill_bar_stop_is_stamped_at_the_fill_minute():
    b = bars(); b = b[b.m != 385]; b.loc[386, "open"] = 96.0                                      # no 15:55 print; 15:56 opens under the low of day
    e = entry.close_confirm(b, 98.0, 0.10, S0)
    assert e is not None and e["entry_minute"] == 386 and e["sameday_stopped"] and e["sameday_stop_minute"] == 386

def check_trades_csv_prices_are_in_dollars_of_the_day():
    import report
    panel = [bar(1, D[0], 100, 101, 96, 100, cfp=2.0), bar(1, D[1], 100, 101, 97, 98, cfp=2.0), bar(1, D[2], 97, 99, 96, 98, cfp=2.0)]
    tr, _ = sim([signal(1, D[0], cfp=2.0)], panel)
    tt = report.trades_table(tr).iloc[0]
    assert tt.shares == 100 and abs(tt.entry_price - 200.0) < 1e-9 and abs(tt.stop - 190.0) < 1e-9 and abs(tt.exit_price - 194.0) < 1e-9   # raw shares x raw price = dollars

# ---------------------------------------------------------------- download layer (no WRDS)
def check_fixed_shards_cover_every_day_once():
    days = list(range(100)); slices = [days[i::4] for i in range(4)]
    assert sorted(sum(slices, [])) == days and all(len(set(a) & set(b)) == 0 for i, a in enumerate(slices) for b in slices[i+1:])

def check_validate_bars_separates_empty_from_invalid():
    import data
    df = pd.DataFrame(dict(symbol=["AAA", "AAA", "BBB", "CCC"], suffixed=False, minute=pd.to_timedelta(["09:30:00", "09:31:00", "10:00:00", "10:00:00"]),
                           open=[10.0, 10.1, 5.0, np.nan], high=[10.5, 10.5, 4.0, np.nan], low=[9.5, 9.5, 4.0, np.nan], close=[10.2, 10.2, 5.0, np.nan], vol=[1, 1, 1, np.nan], ntrd=[1, 1, 1, 0]))
    out, invalid = data.validate_bars(df, ["AAA", "BBB", "CCC", "DDD"])
    assert len(out[out.symbol == "AAA"]) == 2                                      # clean bars kept
    assert "BBB" in invalid and "BBB" not in set(out.symbol)                      # illegal OHLC: left out for a retry, never marked empty
    assert "CCC" in invalid                                                       # a bar row with no prices and no trades is not data either
    assert out[out.symbol == "DDD"].minute.isna().all() and out[out.symbol == "DDD"].ntrd.iloc[0] == 0   # queried, nothing: empty row
    dup = pd.concat([df.iloc[:1], df.iloc[:1]]); out2, inv2 = data.validate_bars(dup, ["AAA"]); assert "AAA" in inv2

# ---------------------------------------------------------------- empirical PIT on real bars (skipped without data)
def check_pit_on_real_bars():
    if not (C.CANDIDATES.exists() and any(C.TAQ_DIR.glob("*.parquet"))):
        print("   (no data yet: PIT check on real bars skipped)"); return
    import data
    cand = data.candidates(); rng = np.random.default_rng(1)
    days = sorted(d for d in cand.date.dt.date.unique() if (C.TAQ_DIR / f"{d}.parquet").exists()); sample = [days[i] for i in rng.choice(len(days), min(20, len(days)), replace=False)]
    for S in (S0, spec(**MORNING)):
        base = entry.entries(S, cand, days=sample, log=lambda *a: None, use_cache=False)
        tmp = Path(tempfile.mkdtemp()); saved = C.TAQ_DIR
        try:
            for d in sample:                                                        # scramble every bar after each entry's own fill bar
                b = pd.read_parquet(saved / f"{d}.parquet"); b = b[b.minute.notna()]; m = (b.minute / pd.Timedelta(minutes=1)).astype(int) - 570
                fills = base[base.date.dt.date == d].set_index("ticker").entry_minute.to_dict() if len(base) else {}
                late = np.array([mm > fills.get(s, S["decision_minute"]) for s, mm in zip(b.symbol, m)])
                for c in ("open", "high", "low", "close"): b.loc[late, c] = b.loc[late, c].astype(float) * rng.uniform(0.3, 1.7, late.sum())
                b.to_parquet(tmp / f"{d}.parquet", index=False)
            C.TAQ_DIR = tmp
            pert = entry.entries(S, cand, days=sample, log=lambda *a: None, use_cache=False)
        finally:
            C.TAQ_DIR = saved; shutil.rmtree(tmp)
        keys = ["permno", "date"]; f = ["decision_px", "stop", "entry_px", "entry_minute"]
        mg = base[keys + f].merge(pert[keys + f], on=keys, how="outer", suffixes=("", "_p"), indicator=True) if len(base) else pd.DataFrame()
        same = int(((mg._merge == "both") & np.all([np.isclose(mg[c], mg[c + "_p"]) for c in f], axis=0)).sum()) if len(mg) else 0
        assert same == len(mg) == len(base), f"PIT [{S['entry']}]: {same}/{len(mg)} identical, {len(base)} base"
        print(f"   PIT [{S['entry']}] on {len(sample)} real days: {len(base)} entries, decision/stop/fill/minute identical after scrambling the bars after each fill")

if __name__ == "__main__":
    failed = 0
    for k, f in list(globals().items()):
        if k.startswith("check_"):
            try:
                f(); print("ok  ", k)
            except Exception as e:
                failed += 1; print("FAIL", k, repr(e))
    sys.exit(1 if failed else 0)
