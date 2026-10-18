"""Daily feature panel from the CRSP files: split-adjusted OHLCV, raw close, EMAs 9/21/50, ADR, dollar volume, 1- and 3-month
returns, AVWAP anchored at the 250-day high. One numba kernel per security. Cached as data/panel.parquet; rebuilt by data.py
when the CRSP files or the feature windows change."""
import json, time
import numpy as np
import pandas as pd
from numba import njit
import config as C

def params(crsp_files) -> dict:
    """Everything the panel depends on: feature windows plus the size and mtime of every CRSP file."""
    return dict(adr=C.ADR_WINDOW, adv=C.ADV_WINDOW, rets=list(C.RUN_UP_WINDOWS), anchor=C.AVWAP_ANCHOR_WINDOW, emas=[5, 9, 13, 21, 50], v=9,
                files={p.name: [p.stat().st_size, int(p.stat().st_mtime)] for p in sorted(crsp_files)})

@njit(cache=True)
def _kernel(starts, ends, o, h, l, c, v, adr_w, adv_w, r1_w, r2_w, anchor_w, ema9, ema21, ema50, adr, advd, ret1, ret2, hi_anchor, avwap):
    for g in range(starts.shape[0]):
        s, e = starts[g], ends[g]
        e9 = c[s]; e21 = c[s]; e50 = c[s]
        a9 = 2.0 / 10.0; a21 = 2.0 / 22.0; a50 = 2.0 / 51.0
        sum_adr = 0.0; sum_adv = 0.0
        cpv = 0.0; cv = 0.0                       # prefix sums of typical price * volume and volume (from s)
        anchor = s; anchor_cpv0 = 0.0; anchor_cv0 = 0.0
        for i in range(s, e):
            k = i - s                              # bars seen so far (0-based)
            e9 = a9 * c[i] + (1 - a9) * e9; e21 = a21 * c[i] + (1 - a21) * e21; e50 = a50 * c[i] + (1 - a50) * e50
            ema9[i] = e9 if k >= 8 else np.nan
            ema21[i] = e21 if k >= 20 else np.nan
            ema50[i] = e50 if k >= 49 else np.nan
            sum_adr += (h[i] - l[i]) / c[i]; sum_adv += c[i] * v[i]
            if k >= adr_w: sum_adr -= (h[i - adr_w] - l[i - adr_w]) / c[i - adr_w]
            if k >= adv_w: sum_adv -= c[i - adv_w] * v[i - adv_w]
            adr[i] = sum_adr / adr_w if k >= adr_w - 1 else np.nan
            advd[i] = sum_adv / adv_w if k >= adv_w - 1 else np.nan
            ret1[i] = c[i] / c[i - r1_w] - 1 if k >= r1_w else np.nan
            ret2[i] = c[i] / c[i - r2_w] - 1 if k >= r2_w else np.nan
            # highest high of the last anchor_w bars (latest tie wins) and the VWAP from that bar on
            tp_v = (h[i] + l[i] + c[i]) / 3.0 * v[i]
            if h[i] >= h[anchor] or i - anchor >= anchor_w:
                if h[i] >= h[anchor]:
                    anchor = i
                else:
                    lo = i - anchor_w + 1
                    best = lo
                    for j in range(lo, i + 1):
                        if h[j] >= h[best]: best = j
                    anchor = best
                anchor_cpv0 = 0.0; anchor_cv0 = 0.0
                for j in range(s, anchor):
                    anchor_cpv0 += (h[j] + l[j] + c[j]) / 3.0 * v[j]; anchor_cv0 += v[j]
            cpv += tp_v; cv += v[i]
            hi_anchor[i] = h[anchor]
            den = cv - anchor_cv0
            avwap[i] = (cpv - anchor_cpv0) / den if den > 0 else np.nan

def _common_flag(df: pd.DataFrame, sh: pd.DataFrame) -> np.ndarray:
    """True where the row's date lies inside one of the permno's common-stock validity intervals."""
    flag = np.zeros(len(df), dtype=bool)
    dates = df["dlycaldt"].values.astype("datetime64[D]").astype(np.int64)
    permno = df["permno"].values
    assert np.all(permno[:-1] <= permno[1:])
    starts = np.searchsorted(permno, sh["permno"].values, side="left")
    ends = np.searchsorted(permno, sh["permno"].values, side="right")
    s0 = sh["secinfostartdt"].values.astype("datetime64[D]").astype(np.int64)
    s1 = sh["secinfoenddt"].values.astype("datetime64[D]").astype(np.int64)
    for a, b, d0, d1 in zip(starts, ends, s0, s1):
        if b > a:
            seg = dates[a:b]
            flag[a:b] |= (seg >= d0) & (seg <= d1)
    return flag

def build(daily: pd.DataFrame, sh: pd.DataFrame, meta: dict, log=print) -> pd.DataFrame:
    t0 = time.time()
    df = daily.sort_values(["permno", "dlycaldt"], kind="mergesort").reset_index(drop=True)
    log(f"panel: {len(df):,} rows, {df['permno'].nunique()} permnos loaded")
    # price in latest-split units: divide by the cumulative price factor; volume: multiply by the cumulative share factor
    g0 = df.groupby("permno")
    cfp = g0["dlycumfacpr"].transform(lambda x: x.replace(0, np.nan).ffill().bfill()).fillna(1.0).values   # a missing / zero factor day takes the neighbours' value
    cfs = g0["dlycumfacshr"].transform(lambda x: x.replace(0, np.nan).ffill().bfill()).fillna(1.0).values
    close = df["dlyclose"].where(df["dlyclose"].notna(), df["dlyprc"].abs())
    prev_close = close.groupby(df["permno"]).shift(1)
    delist = (df["dlydelflg"] == "Y").values
    ret = pd.to_numeric(df["dlyret"], errors="coerce")
    settle = (close.isna() | (close <= 0)) & delist & ret.notna() & prev_close.notna()      # delisting day without a price: settle at the total-return value
    close = close.where(~settle, prev_close * (1 + ret))
    close = close.groupby(df["permno"]).ffill()
    o = df["dlyopen"].where(df["dlyopen"].notna(), close); h = df["dlyhigh"].where(df["dlyhigh"].notna(), close)
    l = df["dlylow"].where(df["dlylow"].notna(), close); v = df["dlyvol"].fillna(0.0)
    halted = (df["dlyvol"].isna() | (df["dlyvol"] <= 0) | df["dlyopen"].isna() | df["dlyhigh"].isna() | df["dlylow"].isna()).values   # no trading that day: priced, not tradable
    ok = ((close.notna() & (close > 0)) | (delist & ret.notna() & prev_close.notna())).values   # a few CRSP rows carry a zero price and are dropped;
                                                                                                # a delisting row is kept even at a zero settlement (-100%)
    df = df[ok].reset_index(drop=True); cfp = cfp[ok]; cfs = cfs[ok]; halted = halted[ok]
    o, h, l, close, v = (x[ok].values.astype(np.float64) for x in (o, h, l, close, v))
    o /= cfp; h /= cfp; l /= cfp; close /= cfp; v *= cfs
    h = np.maximum(h, np.maximum(o, close)); l = np.minimum(l, np.minimum(o, close))
    permno = df["permno"].values
    bounds = np.flatnonzero(np.r_[True, permno[1:] != permno[:-1], True])
    starts, ends = bounds[:-1].astype(np.int64), bounds[1:].astype(np.int64)
    n = len(df)
    outs = [np.full(n, np.nan) for _ in range(9)]
    ck = np.where(close > 0, close, np.nan)               # a zero settlement price (last row of a delisted permno) yields NaN features, not a division error
    _kernel(starts, ends, o, h, l, ck, v, C.ADR_WINDOW, C.ADV_WINDOW, C.RUN_UP_WINDOWS[0], C.RUN_UP_WINDOWS[1], C.AVWAP_ANCHOR_WINDOW, *outs)
    ema9, ema21, ema50, adr, advd, ret1, ret2, hi_anchor, avwap = outs
    out = pd.DataFrame({"permno": permno, "date": df["dlycaldt"].values, "open": o, "high": h, "low": l, "close": close, "vol": v,
                        "cfp": cfp, "close_raw": close * cfp, "cap": df["dlycap"].values,
                        "ema9": ema9, "ema21": ema21, "ema50": ema50, "adr": adr, "advd": advd,
                        f"ret{C.RUN_UP_WINDOWS[0]}": ret1, f"ret{C.RUN_UP_WINDOWS[1]}": ret2, "hi_anchor": hi_anchor, "avwap": avwap})
    cs = pd.Series(ck); gp = cs.groupby(permno)
    out["ema5"] = gp.transform(lambda x: x.ewm(span=5, adjust=False).mean()).values           # sensitivity levels (pandas ewm, same convention as the kernel)
    out["ema13"] = gp.transform(lambda x: x.ewm(span=13, adjust=False).mean()).values
    out["halted"] = halted; out["delist_flag"] = df["dlydelflg"].values
    out["ret"] = pd.to_numeric(df["dlyret"], errors="coerce").values         # CRSP daily total return: carries dividends and the delisting payoff
    out["common"] = _common_flag(df, sh)
    out["is_benchmark"] = out["permno"].isin(list(C.BENCHMARKS.values()))
    out.to_parquet(C.PANEL, index=False)
    C.PANEL_META.write_text(json.dumps(meta))
    log(f"panel built and cached ({time.time()-t0:.0f}s)")
    return out

def load() -> pd.DataFrame:
    if not C.PANEL.exists():
        raise FileNotFoundError("data/panel.parquet missing: run python3 data.py")
    return pd.read_parquet(C.PANEL)

def is_current(meta: dict) -> bool:
    return C.PANEL.exists() and C.PANEL_META.exists() and json.loads(C.PANEL_META.read_text()) == meta
