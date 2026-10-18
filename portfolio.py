"""Day-by-day portfolio simulation with intraday event ordering.
Each day: (1) plan every exit of the existing positions from the daily bar, with the minute from held-day bars when available
(unknown minute = end of day, the conservative choice); (2) merge those exits with the day's entries into one queue ordered by
(minute, at-the-open / within-the-minute, entries-before-exits); an exit that happens before an entry frees its slot and its
exposure, one that is simultaneous or later does not. Sizing and the exposure cap use the PRIOR-CLOSE account value and
prior-close marks of the positions still open at that moment. Shares are integers of the raw (unadjusted) stock; P&L is tracked
in split-adjusted units, so splits during a hold change nothing. Days with no volume or no OHLC are halts: no fills, exits wait.
Dividends are credited from the CRSP total return on the ex-day; on a CRSP delisting day (dlydelflg = Y) the position is settled
at the value implied by that day's total return (the delisting payoff), not at the last trade. A security whose rows simply end
without a delisting flag is closed at its last close."""
import math, heapq, itertools
import numpy as np
import pandas as pd
import config as C

class Book:
    """Per-permno arrays for O(log n) bar lookup by date."""
    COLS = ("open", "high", "low", "close", "ema9", "ema21", "ema50", "cfp", "halted", "ret", "delist_flag")
    def __init__(self, panel: pd.DataFrame):
        p = panel.sort_values(["permno", "date"], kind="mergesort").reset_index(drop=True)
        self.permno = p["permno"].values
        self.date = p["date"].values.astype("datetime64[D]").astype(np.int64)
        missing = [c for c in self.COLS if c not in p]
        if missing:
            raise ValueError(f"panel lacks {missing}: rebuild it with python3 data.py prepare")
        self.cols = {}
        for c in self.COLS:                                   # plain numpy: pandas nullable dtypes would hand out pd.NA, which float() rejects
            if c == "delist_flag": self.cols[c] = p[c].astype(str).values
            elif c == "halted": self.cols[c] = p[c].fillna(False).astype(bool).values
            else: self.cols[c] = p[c].to_numpy(dtype=float, na_value=np.nan)
        b = np.flatnonzero(np.r_[True, self.permno[1:] != self.permno[:-1], True])
        self.idx = {int(self.permno[s]): (int(s), int(e)) for s, e in zip(b[:-1], b[1:])}

    def bar(self, permno, d):
        """(bar dict or None, whether bars after day d exist)."""
        s, e = self.idx[permno]
        i = s + np.searchsorted(self.date[s:e], d)
        if i < e and self.date[i] == d:
            return {c: self.cols[c][i] for c in self.COLS}, i + 1 < e
        return None, i < e

def market_regime(panel: pd.DataFrame):
    """{day_int: QQQ 9 EMA > 21 EMA on the prior day} and the sample's trading days."""
    q = panel[panel.permno == C.BENCHMARKS["QQQ"]].set_index("date"); qd = q.index.values.astype("datetime64[D]").astype(np.int64)
    qp = q[["ema9", "ema21"]].shift(1); regime = {int(d): bool(u) for d, u in zip(qd, (qp.ema9 > qp.ema21).values)}
    lo, hi = (np.datetime64(x).astype("datetime64[D]").astype(np.int64) for x in (C.SAMPLE_START, C.SAMPLE_END))
    return regime, qd[(qd >= lo) & (qd <= hi)]

def _buy_cost(shares_raw):
    return max(C.COMMISSION_PER_SHARE * shares_raw, C.MIN_COMMISSION_PER_ORDER)

def _sell_cost(shares_raw, value, d):
    r, cap = C.finra_taf(d)
    return max(C.COMMISSION_PER_SHARE * shares_raw, C.MIN_COMMISSION_PER_ORDER) + C.sec_fee_rate(d) * value + min(r * shares_raw, cap)

def fallback_half_spread(advd, minute):
    table = C.HALF_SPREAD_FALLBACK["close" if minute >= 330 else "morning"]
    for cap, s in table:
        if advd < cap:
            return s
    return table[-1][1]

END = 390        # "some time today, minute unknown": ordered after every known minute
AT_OPEN, WITHIN = 0, 1
ENTRY, EXIT = 0, 1   # simultaneous events: the entry is processed first, so the exit cannot free a slot for it

def simulate(S: dict, sig: pd.DataFrame, panel: pd.DataFrame, held: dict = None, quotes: dict = None, rf: dict = None, log=lambda *a: None):
    """S: spec. sig: entry rows of the spec's universe. held: {(permno, day_int): minute bars in panel units (m, open, high,
    low, close)}. quotes: {(ticker, day_int, minute): half_spread of the last quote before that minute}. rf: {day_int: daily
    T-bill rate} credited on idle cash (so excess returns in the factor regression are consistent). Returns (trades, daily)."""
    if S["market_filter"] not in (None, "qqq_9>21"):
        raise ValueError(f"market_filter {S['market_filter']!r}: only None or 'qqq_9>21' is implemented")
    held = held or {}; quotes = quotes or {}; rf = rf or {}
    book = Book(panel); regime, days = market_regime(panel)
    sig_by_day = {int(k): v for k, v in sig.groupby(sig["date"].values.astype("datetime64[D]").astype(np.int64))} if len(sig) else {}
    cash = float(S["starting_capital"]); positions = {}; trades, daily = [], []
    trail = f"ema{S['exit_ema']}" if S["exit_ema"] is not None else None
    flat_slip = None if S["slippage"] == "nbbo" else float(S["slippage"])
    rank_col = S["priority"] + "_p" if S["priority"] in ("ret21", "ret63", "advd", "adr") else S["priority"]
    n_quoted = [0, 0]; n_halted = 0; seq = itertools.count()

    def half_spread(ticker, advd, d, minute, auction=False):
        if flat_slip is not None:
            return flat_slip
        if auction:
            return 0.0                                        # a market-on-open order fills at the opening auction price: no spread
        q = quotes.get((ticker, int(d), int(minute))) if 0 < minute < END else None   # minute 0 (a market order at the open): no quote precedes the fill, so the
        if 0 < minute < END:                                                           # measured morning-tier median is used instead of the pre-open book
            n_quoted[0] += q is not None; n_quoted[1] += 1  # coverage over fills that can have a quote
        return q if q is not None else fallback_half_spread(advd, minute)

    def record_exit(pos, shares, px_raw, d, minute, reason, cfp, final, auction=False):
        """shares in adjusted units; px_raw = adjusted price before the spread; cfp = the day's price factor (raw price = adjusted * cfp, raw shares = adjusted / cfp)."""
        nonlocal cash
        hs = 0.0 if reason == "delist" else half_spread(pos["ticker"], pos["advd"], d, minute, auction)   # a delisting payoff is a settlement: no spread, no commission
        px = px_raw * (1 - hs)
        cost = 0.0 if reason == "delist" else _sell_cost(shares / cfp, shares * px, _dstr(d))
        cash += shares * px - cost
        pnl = shares * (px - pos["entry_px"])
        t = pos["trade"]; t["costs"] += cost
        if final:
            t.update(exit_day=int(d), exit_minute=int(minute) if minute < END else -1, exit_price=px, exit_price_raw=px_raw, exit_half_spread=hs, exit_reason=reason,
                     hold_days=pos["hold_days"], gross_pnl=t["gross_pnl"] + pnl, exit_shares_raw=shares / cfp, exit_cfp=cfp)
            t["net_pnl"] = t["gross_pnl"] + t["dividends"] - t["costs"]; t["R"] = t["net_pnl"] / t["risk_dollars"]
            t["price_return"] = px_raw / t["entry_price_raw"] - 1; t["net_return"] = t["net_pnl"] / t["notional"]
            trades.append(t)
        else:
            t.update(partial_day=int(d), partial_minute=int(minute) if minute < END else -1, partial_price=px, partial_shares_raw=shares / cfp, partial_pnl=pnl, gross_pnl=t["gross_pnl"] + pnl, partial_cfp=cfp)

    def open_position(row, d, account, gross):
        """Sized on `account` (prior close) against `gross` (prior-close marks of the positions open at this moment)."""
        nonlocal cash
        hs = half_spread(row.ticker, float(row.advd_p), d, int(row.entry_minute))
        # quantity is fixed when the order is placed, on the decision price (planned risk, position cap, exposure room); the fill price only sets the notional
        px_raw = float(row.entry_px); px = px_raw * (1 + hs); stop = float(row.stop); cfp = float(row.cfp)
        dec = float(row.decision_px); risk_ps = dec - stop
        if not risk_ps > 0:
            return 0.0
        n = math.floor(S["risk_per_trade"] * account / (risk_ps * cfp))
        n = min(n, math.floor(S["max_position_pct"] * account / (dec * cfp)))
        intended = n
        n = min(n, math.floor(max(S["max_gross_exposure"] * account - gross, 0) / (dec * cfp)))
        if n < 1 or n < 0.25 * intended:                      # exposure room for less than a quarter of the intended size: skip the signal
            return 0.0
        shares = n * cfp                                      # adjusted units (not necessarily an integer)
        cost = _buy_cost(n); cash -= shares * px + cost
        permno = int(row.permno)
        trade = dict(ticker=row.ticker, permno=permno, entry_day=int(d), entry_minute=int(row.entry_minute), entry_price=px, entry_price_raw=px_raw,
                     entry_half_spread=hs, stop=stop, stop_width=px / stop - 1, shares_raw=n, risk_dollars=shares * risk_ps, planned_risk=risk_ps, notional=shares * px,
                     account_value_at_entry=account, adv=float(row.advd_p), adr=float(row.adr_p), run_up_1m=float(row.ret21_p), run_up_3m=float(row.ret63_p),
                     qqq_up=bool(regime.get(int(d), False)), partial_day=-1, partial_minute=-1, partial_price=np.nan, partial_shares_raw=0.0, partial_pnl=0.0,
                     gross_pnl=0.0, dividends=0.0, costs=cost, exit_shares_raw=np.nan, entry_cfp=cfp, exit_cfp=np.nan, partial_cfp=np.nan)
        positions[permno] = dict(ticker=row.ticker, advd=float(row.advd_p), entry_px=px, stop=stop, risk_ps=risk_ps, shares=shares, shares0=shares,
                                 partialed=False, pending=None, hold_days=1, trade=trade, last_close=float(row.close), prev_close=px_raw,
                                 entered_today=True, last_cfp=cfp)
        return shares * px

    def first_minute(permno, d, col, op, level, after=-1):
        """First minute > after of day d whose bar's `col` is op level -> (minute, that minute's open); (END, nan) when unknown."""
        b = held.get((permno, int(d)))
        if b is None:
            return END, np.nan
        hit = b[((b[col] <= level) if op == "<=" else (b[col] >= level)) & (b["m"] > after)]
        return (int(hit["m"].iloc[0]), float(hit["open"].iloc[0])) if len(hit) else (END, np.nan)

    def push(events, minute, sub, kind, payload):
        heapq.heappush(events, (int(minute), sub, kind, next(seq), payload))

    year_seen = None
    for d in days:
        d = int(d); y = (np.datetime64(d, "D").astype("datetime64[Y]")).astype(int) + 1970
        n_exits = n_entries = 0
        account_pre = cash + sum(p["shares"] * p["last_close"] for p in positions.values())    # prior-close account value
        gross_open = 0.0                                       # prior-close marks of the positions open at the current moment
        events = []
        # 1. plan today's exits from the daily bar; nothing is executed yet
        for permno, pos in positions.items():
            pos["prev_close"] = pos["last_close"]; pos["entered_today"] = False
            gross_open += pos["shares"] * pos["prev_close"]
            bar, has_later = book.bar(permno, d)
            if bar is None:
                if not has_later:                               # rows end without a delisting flag: close at the last close
                    push(events, END, WITHIN, EXIT, ("exit", permno, pos["last_close"], "data_end", pos["last_cfp"], False))
                continue
            o, h, l, c, cfp, ret = (float(bar[k]) for k in ("open", "high", "low", "close", "cfp", "ret"))
            pos["last_close"] = c; pos["hold_days"] += 1; pos["last_cfp"] = cfp
            if bar["delist_flag"] == "Y":                      # CRSP delisting day: the total return carries the payoff
                px = pos["prev_close"] * (1 + ret) if np.isfinite(ret) else c
                push(events, END, WITHIN, EXIT, ("exit", permno, px, "delist", cfp, False)); continue
            if np.isfinite(ret):
                div = pos["prev_close"] * (1 + ret) - c          # total-return value above the close: an ex-dividend day
                if 0.0005 * pos["prev_close"] < div < 0.5 * pos["prev_close"]:   # above 50% of the price it is a data error (price factor gap), not a dividend
                    cash += pos["shares"] * div; pos["trade"]["dividends"] += pos["shares"] * div
            if bool(bar["halted"]):
                n_halted += 1; continue                        # no trading today: stops and pending exits wait
            if pos["pending"]:
                push(events, 0, AT_OPEN, EXIT, ("exit", permno, o, pos["pending"], cfp, True)); continue   # market-on-open: auction fill
            if o <= pos["stop"]:
                push(events, 0, WITHIN, EXIT, ("exit", permno, o, "stop_gap", cfp, False)); continue     # a stop order hit by the open print pays the spread
            stop_ev = partial_ev = None
            if l <= pos["stop"] and not ((permno, int(d)) in held and first_minute(permno, d, "low", "<=", pos["stop"])[0] == END):   # the daily low must be confirmed by a minute print when bars exist
                m, m_open = first_minute(permno, d, "low", "<=", pos["stop"])
                fill = pos["stop"]; sub = WITHIN
                if S["stop_fill"] == "minute_open" and m < END and np.isfinite(m_open) and m_open < pos["stop"]:
                    fill = m_open; sub = AT_OPEN                  # the minute opened through the stop: filled at its open
                stop_ev = (m, sub, ("exit", permno, fill * (1 - S["stop_slippage"]), "stop", cfp, False))
            if S["partial_at_r"] is not None and not pos["partialed"]:
                tgt = pos["entry_px"] + S["partial_at_r"] * pos["risk_ps"]
                if h >= tgt and not ((permno, int(d)) in held and first_minute(permno, d, "high", ">=", tgt)[0] == END):   # same rule as the stop: the daily high must be confirmed by a minute print when bars exist
                    m2, _ = first_minute(permno, d, "high", ">=", tgt)
                    partial_ev = (m2, WITHIN, ("partial", permno, max(o, tgt), "partial", cfp, False))
            if stop_ev and partial_ev and partial_ev[0] < stop_ev[0]:
                push(events, partial_ev[0], partial_ev[1], EXIT, partial_ev[2])
            if stop_ev:
                push(events, stop_ev[0], stop_ev[1], EXIT, stop_ev[2]); continue      # stop first when the order is unknown
            if partial_ev:
                push(events, partial_ev[0], partial_ev[1], EXIT, partial_ev[2])
            if trail is not None and not np.isnan(bar[trail]) and c < bar[trail]:
                pos["pending"] = f"close_below_{trail}"
            elif S["max_hold_days"] is not None and pos["hold_days"] >= S["max_hold_days"]:
                pos["pending"] = "max_hold"
        # 2. today's entries, in fill order (priority breaks ties within a minute)
        todays = sig_by_day.get(d); n_sig = 0
        if todays is not None and (S["market_filter"] is None or regime.get(d, False)):
            s = todays.sort_values(["entry_minute", rank_col], ascending=[True, False]); n_sig = len(s)
            for row in s.itertuples(index=False):
                push(events, int(row.entry_minute), AT_OPEN if bool(row.fill_at_open) else WITHIN, ENTRY, ("entry", row))
        # 3. process the queue
        while events:
            minute, sub, kind, _, ev = heapq.heappop(events)
            if ev[0] == "entry":
                row = ev[1]; permno = int(row.permno)
                if permno in positions or len(positions) >= S["max_positions"]:
                    continue
                bar0, _ = book.bar(permno, d)
                if bar0 is not None and bar0["delist_flag"] == "Y":
                    continue                                       # the security is delisted today: nothing to hold tomorrow
                v = open_position(row, d, account_pre, gross_open)
                if v <= 0:
                    continue
                gross_open += v; n_entries += 1; pos = positions[permno]; cfp = float(row.cfp)
                end_m = END
                if bool(row.sameday_stopped):
                    fill = pos["stop"]; sub_s = WITHIN; so = row.sameday_stop_open
                    if S["stop_fill"] == "minute_open" and pd.notna(so) and float(so) < pos["stop"]:
                        fill = float(so); sub_s = AT_OPEN
                    push(events, int(row.sameday_stop_minute), sub_s, EXIT, ("exit", permno, fill * (1 - S["stop_slippage"]), "stop_sameday", cfp, False)); end_m = int(row.sameday_stop_minute)
                elif bool(row.sameday_cut):
                    push(events, int(row.sameday_cut_minute), AT_OPEN, EXIT, ("exit", permno, float(row.sameday_cut_px), "cut_sameday", cfp, False)); end_m = int(row.sameday_cut_minute)   # the 15:55 open, even below the stop
                elif trail is not None and float(row.close) < float(getattr(row, trail)):
                    pos["pending"] = f"close_below_{trail}"
                if S["partial_at_r"] is not None and (permno, d) in held:            # entry-day partial, when the day's bars are known
                    tgt = pos["entry_px"] + S["partial_at_r"] * pos["risk_ps"]
                    m2, _ = first_minute(permno, d, "high", ">=", tgt, after=int(row.entry_minute) - (1 if bool(row.fill_at_open) else 0))
                    if m2 < end_m:
                        push(events, m2, WITHIN, EXIT, ("partial", permno, tgt, "partial", cfp, False))
                continue
            _, permno, px, reason, cfp, auction = ev
            pos = positions.get(permno)
            if pos is None:
                continue
            basis = pos["prev_close"] if not pos["entered_today"] else pos["entry_px"]
            if ev[0] == "partial":
                q = math.floor(S["partial_fraction"] * pos["shares0"] / cfp) * cfp       # a whole number of raw shares
                if not pos["partialed"] and 0 < q < pos["shares"]:
                    record_exit(pos, q, px, d, minute, reason, cfp, final=False); pos["shares"] -= q; gross_open -= q * basis
                pos["partialed"] = True
                continue
            record_exit(pos, pos["shares"], px, d, minute, reason, cfp, final=True, auction=auction)
            gross_open -= pos["shares"] * basis; del positions[permno]; n_exits += 1
        # 4. interest: borrowed cash pays the margin rate (flat, or T-bill + spread), idle cash earns the T-bill rate; then marks
        if cash < 0:
            rate = S["margin_rate"] / 252.0 if S["margin_rate"] is not None else rf.get(d, 0.0) + S["margin_spread"] / 252.0
            cash -= -cash * rate
        elif cash > 0 and rf.get(d, 0.0) > 0:
            cash += cash * rf[d]
        gross = sum(p["shares"] * p["last_close"] for p in positions.values())
        account = cash + gross
        daily.append(dict(day=d, account_value=account, cash=cash, gross=gross, exposure=gross / account if account > 0 else np.nan,
                          n_positions=len(positions), n_signals=n_sig, n_entries=n_entries, n_exits=n_exits))
        if y != year_seen:
            if year_seen is not None:
                log(f"  {S['name']}: end of {year_seen}  account ${account:,.0f}  trades so far {len(trades)}")
            year_seen = y
    for permno, pos in list(positions.items()):
        record_exit(pos, pos["shares"], pos["last_close"], int(days[-1]), END, "sample_end", pos["last_cfp"], final=True)
    if daily:
        daily[-1].update(account_value=cash, cash=cash, gross=0.0, exposure=0.0, n_positions=0)
    log(f"  {S['name']}: end of {year_seen}  account ${cash:,.0f}  trades {len(trades)}")
    t = pd.DataFrame(trades); dd = pd.DataFrame(daily)
    t.attrs["quote_coverage"] = n_quoted[0] / n_quoted[1] if n_quoted[1] else np.nan
    t.attrs["halted_position_days"] = n_halted
    return t, dd

def _dstr(d):
    return str(np.datetime64(int(d), "D"))
