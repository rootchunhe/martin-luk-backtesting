"""Entries on 1-minute bars. Two styles (specs.py `entry`):
  close_confirm     at the decision minute (15:54) the day's low has touched the support, the price is above the open and the
                    support and in the upper part of the day's range; fill at the open of the next (15:55) bar. [ours]
  morning_breakout  Luk's own: within the first hour the price flushes through the support, then buy the break of the previous
                    1-minute (before minute 15) / 5-minute bar high, at most 3% above the low of day; stop = low of day.
Point-in-time by construction: minute bars are converted to panel (split-adjusted) units with the day's cumulative price factor;
a decision uses only bars completed before the fill; the stop is the low of day BEFORE the fill bar; the fill bar's own low
counts as a stop hit (its order inside the minute is unknown); later bars give the same-day outcome.
The entry table is computed once for every candidate stock-day (the download gate) and cached; specs then select their universe."""
import json, hashlib, time
from pathlib import Path
import numpy as np
import pandas as pd
import config as C
import specs

TICK = 0.001   # a buy-stop one tenth of a percent above the trigger bar's high

def universe_mask(cand: pd.DataFrame, S: dict) -> np.ndarray:
    """Spec universe on the t-1 columns of the candidate (or entry) table."""
    m = ((cand["close_raw_p"] >= S["min_price"]) & (cand["advd_p"] >= S["min_dollar_volume"]) & (cand["adr_p"] >= S["min_adr"])
         & ((cand["ret21_p"] >= S["min_run_up"]) | (cand["ret63_p"] >= S["min_run_up"])) & (cand["ema9_p"] > cand["ema21_p"])
         & (cand["ret63_p"] < 20) & (cand["ret21_p"] < 20))                                   # a 2,000% run-up is a price-factor error, not a leader
    if S["ema_stack"] == "9>21>50":
        m &= cand["ema21_p"] > cand["ema50_p"]
    elif S["ema_stack"] != "9>21":
        raise ValueError(f"ema_stack {S['ema_stack']!r}")
    if S["above_avwap"]:
        m &= cand["close_p"] > cand["avwap_p"]
    return m.values

def check_within_gate(S: dict) -> list:
    """A spec must be at least as strict as the download gate, otherwise its signals are not all in the minute-bar files.
    Raises for a spec outside the gate; returns warnings for a partial overlap (reported in the summary)."""
    G = C.CANDIDATE_GATE; bad = []; warn = []
    for k in ("min_price", "min_dollar_volume", "min_adr", "min_run_up"):
        if S[k] < G[k]: bad.append(f"{k}={S[k]} < gate {G[k]}")
    if G["ema_stack"] == "9>21>50" and S["ema_stack"] != "9>21>50": bad.append("ema_stack looser than the gate")
    if G["above_avwap"] and not S["above_avwap"]: bad.append("above_avwap=False but the gate requires it")
    if S["support"] not in ("ema5", "ema9", "ema13", "ema21"): bad.append(f"support {S['support']!r}")
    if S["touch_tolerance_adr"] > G["touch_tolerance_adr"]: bad.append("touch_tolerance_adr wider than the gate")
    if S["support"] == "ema5": warn.append("5 EMA support: only the touches inside the 9 EMA download zone are observed (the 5 EMA sits above the 9 in an uptrend); entry count is a lower bound")
    if bad:
        raise ValueError("spec outside the candidate gate (widen config.CANDIDATE_GATE and re-run data.py): " + "; ".join(bad))
    return warn

def _max_width(S, adr):
    return min(S["max_stop_width"], S["max_stop_width_adr"] * adr)

def _outcome(b: pd.DataFrame, i_fill: int, px: float, stop: float, fill_bar_counts: bool, S: dict) -> dict:
    """Same-day outcome after a fill in bar i_fill, in time order: a stop before the decision minute ends the trade; otherwise the
    cut_same_day decision at the close of the bar before the decision minute (filled at that minute's open); otherwise a stop
    later in the day. The fill bar itself counts as a stop hit when fill_bar_counts (its order inside the minute is unknown)."""
    after = b.iloc[i_fill:] if fill_bar_counts else b.iloc[i_fill + 1:]
    hit = after[after["low"] <= stop]
    out = dict(sameday_stopped=False, sameday_stop_minute=-1, sameday_stop_open=np.nan, day_close=float(b["close"].iloc[-1]),
               sameday_cut=False, sameday_cut_minute=-1, sameday_cut_px=np.nan)
    T = int(S["decision_minute"]); m_fill = int(b["m"].iloc[i_fill])
    first_hit = int(hit["m"].iloc[0]) if len(hit) else None
    if first_hit is not None and first_hit < T:
        out.update(sameday_stopped=True, sameday_stop_minute=first_hit, sameday_stop_open=float(hit["open"].iloc[0]) if int(hit.index[0]) != i_fill else np.nan)
        return out
    if S["cut_same_day"]:
        dec = b[(b["m"] <= T - 1) & (b["m"] > m_fill)]; fill_bar = b[(b["m"] >= T) & (b["m"] < T + S["max_stale_minutes"])].head(1)
        if len(dec) and not fill_bar.empty and float(dec["close"].iloc[-1]) < px:
            out.update(sameday_cut=True, sameday_cut_minute=int(fill_bar["m"].iloc[0]), sameday_cut_px=float(fill_bar["open"].iloc[0]))
            return out
    if first_hit is not None:
        out.update(sameday_stopped=True, sameday_stop_minute=first_hit, sameday_stop_open=float(hit["open"].iloc[0]) if int(hit.index[0]) != i_fill else np.nan)
    return out

def close_confirm(b: pd.DataFrame, level: float, adr: float, S: dict) -> dict | None:
    T = int(S["decision_minute"])
    dec = b[b["m"] <= T - 1]; fill_bar = b[(b["m"] >= T) & (b["m"] < T + S["max_stale_minutes"])].head(1)   # the order fills at the first print from 15:55 on
    if fill_bar.empty or len(dec) < S["min_bars"] or int(dec["m"].iloc[-1]) < T - S["max_stale_minutes"]:
        return None                                                    # no print after the decision, too few prints, or no print close to it
    o, hi, lo, px = float(dec["open"].iloc[0]), float(dec["high"].max()), float(dec["low"].min()), float(dec["close"].iloc[-1])
    touched = lo <= level * (1 + S["touch_tolerance_adr"] * adr)       # reached the support (tolerance 0: traded at or through it)
    if not touched:
        return None
    if lo < level * (1 - S["max_undercut_adr"] * adr):                 # collapsed through it
        return None
    if S["require_green"] and (px <= o or px <= level):      # green on the day and back above the support
        return None
    if (px - lo) / max(hi - lo, 1e-12) < S["min_close_position"]:      # strong: upper part of the range so far
        return None
    width = px / lo - 1
    if width > _max_width(S, adr) or width < S["min_stop_width"]:
        return None
    fill = float(fill_bar["open"].iloc[0])
    e = dict(entry_px=fill, stop=lo, entry_minute=int(fill_bar["m"].iloc[0]), decision_px=px, flush_minute=-1, fill_at_open=True)
    e.update(_outcome(b, int(fill_bar.index[0]), fill, lo, fill_bar_counts=True, S=dict(S, cut_same_day=False)))
    if fill <= lo:                                                     # the order was already in: it fills at the open and is stopped at once
        e.update(sameday_stopped=True, sameday_stop_minute=int(fill_bar["m"].iloc[0]), sameday_stop_open=fill)
    return e

def morning_breakout(b: pd.DataFrame, level: float, adr: float, prev_close: float, S: dict) -> dict | None:
    if prev_close < level * (1 - S["flush_undercut"]):
        return None                                                    # the support is only support if price comes down into it
    op, hi_arr, lo_arr, m_arr = b["open"].values, b["high"].values, b["low"].values, b["m"].values
    W = int(S["entry_window_minutes"]); switch = int(S["bar_switch_minute"])
    touch_px = level * (1 - S["flush_undercut"]); too_deep = level * (1 - S["max_undercut_adr"] * adr)
    k5 = m_arr // 5; bucket_hi = {}                                     # high of each completed 5-minute bucket, filled as bars complete
    lod_prev = np.inf; flushed = False; flush_m = -1; prev_hi_1m = None; cur_k = -1; cur_hi = -np.inf
    for i in range(len(b)):
        m = int(m_arr[i])
        if m >= W:                                                     # entries in the first W minutes only
            break
        if k5[i] != cur_k:                                             # a new 5-minute bucket starts: the previous one is complete
            if cur_k >= 0: bucket_hi[cur_k] = cur_hi
            cur_k = k5[i]; cur_hi = -np.inf
        if flushed and m > flush_m and lod_prev < too_deep:
            return None
        if flushed and m > flush_m:
            ref = prev_hi_1m if m < switch else bucket_hi.get(cur_k - 1)   # previous completed bar: 1-minute, or the last full 5-minute bucket
            if ref is not None and np.isfinite(ref):
                trig = ref * (1 + TICK)
                if hi_arr[i] >= trig:
                    px = op[i] if op[i] > trig else trig                 # gapped above the buy-stop: filled at the open
                    stop = lod_prev                                     # the low of day known before this bar
                    width = px / stop - 1
                    if px / stop - 1 <= S["max_chase_from_low"] and px >= level and width <= _max_width(S, adr) and width >= S["min_stop_width"]:
                        e = dict(entry_px=px, stop=stop, entry_minute=m, decision_px=px, flush_minute=flush_m, fill_at_open=bool(op[i] > trig))
                        e.update(_outcome(b, i, px, stop, fill_bar_counts=True, S=S))
                        return e
        lod_prev = min(lod_prev, lo_arr[i]); cur_hi = max(cur_hi, hi_arr[i]); prev_hi_1m = hi_arr[i]
        if not flushed and lo_arr[i] <= touch_px:
            flushed = True; flush_m = m
    return None

def data_version() -> str:
    """Candidate-day coverage of the minute files (run id, stock-days with bars, empty); unchanged by held-day appends, which the
    backtest makes between specs. Bars only change through a new prepare/download, which changes the run id."""
    import data
    run = data._run(); cov = data.coverage()
    return f"{run.get('run_id')}:{int(cov['with_bars'].sum())}:{int(cov['empty'].sum())}:{int(cov['missing'].sum())}"

def _entry_key(S: dict) -> str:
    """Entry parameters + candidate/panel version + minute-bar inventory + this file's source."""
    P = dict(specs.entry_params(S), cand=json.loads(C.CANDIDATES_META.read_text()) if C.CANDIDATES_META.exists() else None,
             taq=data_version(), code=hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:10], v=4)
    return hashlib.sha1(json.dumps(P, sort_keys=True, default=str).encode()).hexdigest()[:10]

def entries(S: dict, cand: pd.DataFrame, days=None, log=print, use_cache=True) -> pd.DataFrame:
    """Entry table for every candidate stock-day under the spec's entry parameters (the universe is applied later).
    Cached as data/entries_<hash>.parquet; the hash covers the parameters, the data version and this file."""
    key = _entry_key(S) if (use_cache and days is None) else "nocache"; path = C.DATA / f"entries_{key}.parquet"
    if use_cache and days is None and path.exists():
        return pd.read_parquet(path)
    t0 = time.time(); rows = []; n_days = 0; n_missing_days = 0; n_missing_syms = 0; n_cand = 0; n_thin = 0; n_empty = 0
    dates = sorted(cand["date"].dt.date.unique()) if days is None else days
    sup = S["support"] + "_p"
    for d in dates:
        p = C.TAQ_DIR / f"{d}.parquet"
        cd0 = cand[cand["date"].dt.date == d]; n_cand += len(cd0)
        if not p.exists():
            n_missing_days += 1; n_missing_syms += len(cd0); continue
        b = pd.read_parquet(p)
        if b.empty:
            continue
        n_days += 1
        empty_syms = set(b.loc[b["minute"].isna(), "symbol"])
        b = b[b["minute"].notna()]
        if b.empty:
            n_empty += int(cd0["ticker"].isin(empty_syms).sum()); n_missing_syms += int((~cd0["ticker"].isin(empty_syms)).sum()); continue
        b["m"] = (pd.to_timedelta(b["minute"]) / pd.Timedelta(minutes=1)).astype(int) - 570
        b = b[b["low"].notna() & b["open"].notna()]
        cd = cand[cand["date"].dt.date == d]
        for r in cd.itertuples(index=False):
            bs = b[b["symbol"] == r.ticker]
            if bs.empty:
                if r.ticker in empty_syms: n_empty += 1
                else: n_missing_syms += 1
                continue
            if not (pd.notna(r.cfp) and r.cfp > 0):
                raise ValueError(f"candidate {r.ticker} {d}: price factor {r.cfp!r}; the panel must carry a positive cfp on every row")
            cfp = float(r.cfp)
            bs = bs.sort_values("m").reset_index(drop=True)
            bs = bs.assign(open=bs["open"].astype(float) / cfp, high=bs["high"].astype(float) / cfp, low=bs["low"].astype(float) / cfp, close=bs["close"].astype(float) / cfp)
            level, adr = float(getattr(r, sup)), float(r.adr_p)
            if not (np.isfinite(level) and np.isfinite(adr)) or adr <= 0:
                continue
            if S["entry"] == "close_confirm":
                dec_n = int((bs["m"] <= S["decision_minute"] - 1).sum())
                if dec_n < S["min_bars"] or (dec_n and int(bs.loc[bs["m"] <= S["decision_minute"] - 1, "m"].iloc[-1]) < S["decision_minute"] - S["max_stale_minutes"]):
                    n_thin += 1
                e = close_confirm(bs, level, adr, S)
            elif S["entry"] == "morning_breakout":
                e = morning_breakout(bs, level, adr, float(r.close_p), S)
            else:
                raise ValueError(f"entry {S['entry']!r}")
            if e is None:
                continue
            row = dict(permno=int(r.permno), date=r.date, ticker=r.ticker, cfp=cfp, close=r.close, ema9=r.ema9, ema21=r.ema21, ema50=r.ema50,
                       close_raw_p=r.close_raw_p, close_p=r.close_p, ema9_p=r.ema9_p, ema21_p=r.ema21_p, ema50_p=r.ema50_p, adr_p=r.adr_p, advd_p=r.advd_p,
                       avwap_p=r.avwap_p, ret21_p=r.ret21_p, ret63_p=r.ret63_p, cap_p=r.cap_p, level=level)
            row.update(e); rows.append(row)
    out = pd.DataFrame(rows)
    if len(out):
        out["width"] = out["entry_px"] / out["stop"] - 1; out["date"] = pd.to_datetime(out["date"])
    out.attrs["days_with_bars"] = n_days; out.attrs["days_missing"] = n_missing_days; out.attrs["stock_days_missing"] = n_missing_syms; out.attrs["stock_days"] = n_cand; out.attrs["stock_days_thin"] = n_thin; out.attrs["stock_days_empty"] = n_empty
    log(f"entries[{S['entry']}, key {key}]: {n_days} days with bars; {n_missing_syms} of {n_cand} candidate stock-days have no bars ({n_missing_days} days without a file); "
        f"{n_thin} too thinly traded for close_confirm (< {S['min_bars']} priced minutes or no print in the last {S['max_stale_minutes']}); {len(out)} entries ({time.time()-t0:.0f}s)")
    if use_cache and days is None:
        out.to_parquet(path, index=False)
    return out
