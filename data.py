"""Data layer. Everything lives flat under data/ except the minute bars (data/taq/, one file per trading day).

  python3 data.py prepare --shards 4 [--chunk 40] [--refresh 2026]   CRSP security history and daily bars, Fama-French factors,
                                                           the feature panel, the candidate stock-days, and download_run.json
                                                           (frozen: candidate set, shard count, chunk size, SQL variant)
  python3 data.py download --shard 2                       one worker per shard (1..n), each owns a fixed slice of the day list, so
                                                           no two workers ever write the same file; start one process per shard,
                                                           re-run to resume; exit code 1 if any of its days failed or stayed incomplete
  python3 data.py status                                   coverage by year: candidate stock-days with bars / empty / missing,
                                                           share of complete trading days, logged failures

A day file taq/YYYY-MM-DD.parquet holds the 1-minute bars (9:30-16:00, raw TAQ prices) of every symbol needed that day. A symbol
that was queried and had no eligible print is kept as one empty row (ntrd = 0), so the file is also the download status: a symbol
present = done or empty, absent = still to pull (a symbol whose bars failed validation is left absent and logged, so it is
retried). Every query chunk is written as soon as it arrives (temporary file, atomic rename). backtest.py runs after the
download, as the single writer, and appends held-day symbols into the same files and quotes into nbbo.csv. Symbols are CRSP
trading symbols (BRKA, LGFB, GOOGL); TAQ is queried by the matching (sym_root, sym_suffix) pair, and the form that answered is
stored with the bars so the quotes use the same one."""
import sys, os, time, json, hashlib, uuid
import numpy as np
import pandas as pd
import config as C
import features
from wrds_db import sql, in_list

C.DATA.mkdir(parents=True, exist_ok=True); C.TAQ_DIR.mkdir(parents=True, exist_ok=True)
SECURITY_HIST = C.DATA / "security_hist.csv"
BAR_COLS = ["symbol", "suffixed", "minute", "open", "high", "low", "close", "vol", "ntrd"]

# ------------------------------------------------------------------------------------------------------------ CRSP
def security_hist() -> pd.DataFrame:
    """Common-stock validity intervals with the trading symbol and share class (what TAQ needs)."""
    if SECURITY_HIST.exists():
        return pd.read_csv(SECURITY_HIST, parse_dates=["secinfostartdt", "secinfoenddt"], dtype={"ticker": str, "tradingsymbol": str, "shareclass": str})
    df = sql(f"""select s.permno, s.permco, s.ticker, s.tradingsymbol, s.shareclass, s.primaryexch, s.secinfostartdt, s.secinfoenddt, s.siccd, s.issuernm
                 from {C.CRSP_LIB}.stksecurityinfohist s
                 where {C.COMMON_FILTER} and s.secinfoenddt >= '{C.DATA_START_YEAR}-01-01' and s.secinfostartdt <= '{C.DATA_END_YEAR}-12-31'""")
    for c in ("secinfostartdt", "secinfoenddt"):
        df[c] = pd.to_datetime(df[c])
    df.to_csv(SECURITY_HIST, index=False)
    return df

def crsp_year_path(y): return C.DATA / f"crsp_{y}.parquet"

def pull_crsp_year(y) -> pd.DataFrame:
    bench = ",".join(str(v) for v in C.BENCHMARKS.values())
    df = sql(f"""
    select d.permno, d.dlycaldt, d.dlyprc, d.dlyopen, d.dlyhigh, d.dlylow, d.dlyclose, d.dlyvol, d.dlycap, d.dlyret, d.dlydelflg,
           a.dlycumfacpr, a.dlycumfacshr
    from {C.CRSP_LIB}.stkdlysecuritydata d
    left join {C.CRSP_LIB}.stkdlycumulativeadjfactor a on a.permno=d.permno and a.dlycaldt=d.dlycaldt
    where d.dlycaldt between '{y}-01-01' and '{y}-12-31'
      and (d.permno in ({bench}) or d.permno in (
            select distinct s.permno from {C.CRSP_LIB}.stksecurityinfohist s
            where {C.COMMON_FILTER} and s.secinfoenddt >= '{y}-01-01' and s.secinfostartdt <= '{y}-12-31'))""")
    df["dlycaldt"] = pd.to_datetime(df["dlycaldt"])
    for c in df.columns:
        if c not in ("dlycaldt", "dlydelflg"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values(["permno", "dlycaldt"]).reset_index(drop=True)

def ensure_crsp(refresh=()):
    t0 = time.time(); sh = security_hist()
    print(f"security_hist: {len(sh)} intervals, {sh['permno'].nunique()} permnos ({time.time()-t0:.0f}s)", flush=True)
    for y in range(C.DATA_START_YEAR, C.DATA_END_YEAR + 1):
        p = crsp_year_path(y)
        if p.exists() and y not in refresh:
            continue
        t0 = time.time(); df = pull_crsp_year(y); _atomic_parquet(df, p)
        print(f"crsp {y}: {len(df):,} rows, {df['permno'].nunique()} permnos ({time.time()-t0:.0f}s)", flush=True)

def load_crsp() -> pd.DataFrame:
    return pd.concat([pd.read_parquet(crsp_year_path(y)) for y in range(C.DATA_START_YEAR, C.DATA_END_YEAR + 1)], ignore_index=True)

# ------------------------------------------------------------------------------------------------------------ factors
def ensure_factors():
    if C.FF_FACTORS.exists():
        return
    df = sql(f"select date, mktrf, smb, hml, rmw, cma, umd, rf from {C.FF_TABLE} where date >= '{C.DATA_START_YEAR}-01-01' order by date")
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df.to_csv(C.FF_FACTORS, index=False)
    print(f"ff factors: {len(df)} days {df.date.min()}..{df.date.max()}", flush=True)

def factors() -> pd.DataFrame | None:
    if not C.FF_FACTORS.exists():
        return None
    df = pd.read_csv(C.FF_FACTORS, parse_dates=["date"])
    for c in df.columns[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    return df

# ------------------------------------------------------------------------------------------------------------ panel
def panel_meta() -> dict:
    return features.params(list(C.DATA.glob("crsp_*.parquet")))

def ensure_panel(log=print) -> pd.DataFrame:
    meta = panel_meta()
    if features.is_current(meta):
        return features.load()
    return features.build(load_crsp(), security_hist(), meta, log=log)

# ------------------------------------------------------------------------------------------------------------ candidates
def symbol_on(sh: pd.DataFrame, permnos, dates) -> list:
    """CRSP trading symbol valid on each (permno, date); None when the security was not a common stock that day."""
    sh = sh.sort_values(["permno", "secinfostartdt"])
    groups = {k: g for k, g in sh.groupby("permno")}
    out = []
    for permno, d in zip(permnos, dates):
        g = groups.get(int(permno))
        if g is None:
            out.append(None); continue
        r = g[(g["secinfostartdt"] <= d) & (g["secinfoenddt"] >= d)]
        sym = r["tradingsymbol"].iloc[-1] if len(r) else None
        out.append(sym if isinstance(sym, str) and sym else None)
    return out

def candidate_meta() -> dict:
    return dict(gate=C.CANDIDATE_GATE, sample=[C.SAMPLE_START, C.SAMPLE_END], panel=json.loads(C.PANEL_META.read_text()) if C.PANEL_META.exists() else None, schema=6)

def ensure_candidates(panel=None, log=print) -> pd.DataFrame:
    meta = candidate_meta()
    if C.CANDIDATES.exists() and C.CANDIDATES_META.exists() and json.loads(C.CANDIDATES_META.read_text()) == meta:
        return pd.read_parquet(C.CANDIDATES)
    G = C.CANDIDATE_GATE
    p = (panel if panel is not None else features.load()).sort_values(["permno", "date"], kind="mergesort").reset_index(drop=True)
    g = p.groupby("permno", sort=False)
    cols = ["close", "close_raw", "ema5", "ema9", "ema13", "ema21", "ema50", "adr", "advd", "avwap", "hi_anchor", "ret21", "ret63", "cap"]
    prev = g[cols].shift(1); prev.columns = [c + "_p" for c in cols]
    elig = ((p["common"] & ~p["is_benchmark"]).values
            & (prev["close_raw_p"] >= G["min_price"]).values & (prev["advd_p"] >= G["min_dollar_volume"]).values
            & (prev["adr_p"] >= G["min_adr"]).values & ((prev["ret21_p"] >= G["min_run_up"]) | (prev["ret63_p"] >= G["min_run_up"])).values
            & (prev["ema9_p"] > prev["ema21_p"]).values)
    if G["ema_stack"] == "9>21>50": elig &= (prev["ema21_p"] > prev["ema50_p"]).values
    if G["above_avwap"]: elig &= (prev["close_p"] > prev["avwap_p"]).values
    level = prev[G["support"] + "_p"].values
    touch = p["low"].values <= level * (1 + G["touch_tolerance_adr"] * prev["adr_p"].values)
    sel = elig & touch & (p["date"] >= pd.Timestamp(C.SAMPLE_START)).values & (p["date"] <= pd.Timestamp(C.SAMPLE_END)).values
    s = pd.concat([p.loc[sel, ["permno", "date", "cfp", "open", "high", "low", "close", "ema9", "ema21", "ema50"]], prev.loc[sel]], axis=1).reset_index(drop=True)
    s["ticker"] = symbol_on(security_hist(), s["permno"], s["date"])
    n0 = len(s); s = s[s["ticker"].notna()].copy()
    _atomic_parquet(s, C.CANDIDATES); C.CANDIDATES_META.write_text(json.dumps(meta))
    log(f"candidates: {len(s):,} stock-days over {s.date.nunique()} days ({len(s)/s.date.nunique():.0f} symbols/day); {n0-len(s)} dropped without a trading symbol")
    return s

def candidates() -> pd.DataFrame:
    if not C.CANDIDATES.exists():
        raise FileNotFoundError("data/candidates.parquet missing: run python3 data.py prepare")
    return pd.read_parquet(C.CANDIDATES)

def _candidates_hash() -> str:
    return hashlib.sha1(C.CANDIDATES_META.read_text().encode() + str(C.CANDIDATES.stat().st_size).encode()).hexdigest()[:12]

def is_current() -> bool:
    """Panel and candidates match the code and the CRSP files on disk, and the download run was frozen on them."""
    ok = features.is_current(panel_meta()) and C.CANDIDATES.exists() and C.CANDIDATES_META.exists() and json.loads(C.CANDIDATES_META.read_text()) == candidate_meta()
    return ok and C.DOWNLOAD_RUN.exists() and json.loads(C.DOWNLOAD_RUN.read_text()).get("candidates") == _candidates_hash()

# ------------------------------------------------------------------------------------------------------------ TAQ minute bars
def _excl(letters):
    return " and ".join(f"position('{c}' in coalesce(tr_scond,''))=0" for c in letters)
CORR_OK = "tr_corr in ('00','01')"
PRICE_OK = ("(((tr_source='N' and " + _excl("ILZUWP4V7CNRH") + ") or (tr_source<>'N' and " + _excl("ILZUBWP4V7CNRH") + "))"
            " and abs(price*100 - round(price*100)) < 1e-6)")
VOL_OK = "position('Q' in coalesce(tr_scond,''))=0 and position('M' in coalesce(tr_scond,''))=0"
REGULAR = "position('T' in coalesce(tr_scond,''))=0 and position('U' in coalesce(tr_scond,''))=0"

_class_cache = {}
def symbol_pairs(symbols) -> dict:
    """{trading symbol: [(sym_root, sym_suffix), ...]}: a class share such as BRKA is BRK/A in TAQ, while a Nasdaq class symbol such
    as GOOGL is listed as its own root, so both forms are queried for a symbol that carries a share class."""
    if not _class_cache:
        sh = security_hist()
        for sym, cls in zip(sh["tradingsymbol"], sh["shareclass"]):
            if isinstance(sym, str) and isinstance(cls, str) and cls and sym.endswith(cls) and len(sym) > len(cls):
                _class_cache[sym] = cls
    out = {}
    for s in symbols:
        pairs = [(s, "")]
        cls = _class_cache.get(s)
        if cls:
            pairs.append((s[:-len(cls)], cls))
        if s in _class_symbols() and len(s) >= 5 and (s[:-1], s[-1]) not in pairs:
            pairs.append((s[:-1], s[-1]))                        # Nasdaq fifth-letter class symbols (LBRDK, class C) may be listed as LBRD/K
        out[s] = pairs
    return out

_cls_syms = set()
def _class_symbols() -> set:
    if not _cls_syms:
        sh = security_hist()
        _cls_syms.update(s for s, cls in zip(sh["tradingsymbol"], sh["shareclass"]) if isinstance(s, str) and isinstance(cls, str) and cls)
    return _cls_syms

def _pair_filter(pairs) -> str:
    """`sym_root in (...)` lets the columnar store skip chunks (a VALUES join does not: it hash-joins a full scan of the day table,
    70x the cost in EXPLAIN); the exact (root, suffix) pairs are applied on top."""
    roots = in_list(sorted({r for r, _ in pairs}))
    pl = ", ".join(f"('{r}', '{sfx}')" for r, sfx in pairs)
    return f"sym_root in {roots} and (sym_root, coalesce(sym_suffix, '')) in ({pl})"

def minute_bar_sql(d, symbols, variant="array_agg") -> str:
    d = pd.Timestamp(d); sp = symbol_pairs(symbols); pairs = sorted({pr for s in symbols for pr in sp[s]})
    base = f"""
      select t.sym_root || coalesce(t.sym_suffix, '') as symbol, (t.sym_suffix is not null) as suffixed,
             date_trunc('minute', t.time_m) as minute, t.time_m, t.tr_seqnum, t.price, t.size, ({PRICE_OK}) as pok, ({VOL_OK}) as vok
      from taqm_{d.year}.ctm_{d.strftime('%Y%m%d')} t
      where {_pair_filter(pairs)} and {CORR_OK} and {REGULAR} and t.time_m >= '09:30:00' and t.time_m < '16:00:00'"""
    if variant == "array_agg":
        return f"""with t as ({base})
    select symbol, suffixed, minute,
      (array_agg(price order by time_m, tr_seqnum) filter (where pok))[1] as open,
      max(price) filter (where pok) as high, min(price) filter (where pok) as low,
      (array_agg(price order by time_m desc, tr_seqnum desc) filter (where pok))[1] as close,
      sum(size) filter (where vok) as vol, count(*) as ntrd
    from t group by 1,2,3 order by 1,2,3"""
    if variant == "rownum":
        return f"""with t as ({base}),
    r as (select *, row_number() over (partition by symbol, suffixed, minute order by (case when pok then 0 else 1 end), time_m, tr_seqnum) as rn_a,
                    row_number() over (partition by symbol, suffixed, minute order by (case when pok then 0 else 1 end), time_m desc, tr_seqnum desc) as rn_d from t)
    select symbol, suffixed, minute,
      max(price) filter (where pok and rn_a = 1) as open, max(price) filter (where pok) as high, min(price) filter (where pok) as low,
      max(price) filter (where pok and rn_d = 1) as close, sum(size) filter (where vok) as vol, count(*) as ntrd
    from r group by 1,2,3 order by 1,2,3"""
    raise ValueError(variant)

def _one_form(df: pd.DataFrame, d=None) -> pd.DataFrame:
    """A class symbol can match TAQ as root+suffix (BRK/A) or as its own root (BRKA). When both return bars the suffixed form
    (the NYSE convention) is kept and the case is written to data/symbol_ambiguities_<pid>.csv for review; bars are never merged."""
    if df.empty or "suffixed" not in df:
        return df
    both = df.groupby("symbol")["suffixed"].nunique(); amb = sorted(both[both > 1].index)
    for sym in amb:
        n = df[df["symbol"] == sym].groupby("suffixed").size()
        _log_csv("symbol_ambiguities", dict(day=str(d), symbol=sym, bars_suffixed=int(n.get(True, 0)), bars_plain=int(n.get(False, 0)), kept="suffixed"))
    if amb:
        df = df[~(df["symbol"].isin(amb) & ~df["suffixed"].astype(bool))]
    return df

def pull_minute_bars(d, symbols, chunk=None, variant=None, on_chunk=None) -> pd.DataFrame:
    """Regular-session 1-minute OHLCV for the symbols on day d (raw TAQ prices), queried in chunks of `chunk` symbols;
    on_chunk(df) is called after each chunk (used to persist batches as they arrive)."""
    symbols = sorted(set(symbols)); run = _run()
    chunk = chunk or run.get("chunk", 40); variant = variant or run.get("sql", "array_agg")
    parts = []
    for i in range(0, len(symbols), chunk):
        df = _one_form(sql(minute_bar_sql(d, symbols[i:i + chunk], variant)), d)
        df = _typed(df); parts.append(df)
        if on_chunk: on_chunk(df, symbols[i:i + chunk])
    return pd.concat(parts, ignore_index=True)[BAR_COLS] if parts else pd.DataFrame(columns=BAR_COLS)

def _typed(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=BAR_COLS)
    df = df.copy()
    df["minute"] = pd.to_timedelta(df["minute"].astype(str)) if df["minute"].dtype == object else df["minute"]
    df["suffixed"] = df["suffixed"].astype(bool)
    for c in ("open", "high", "low", "close", "vol", "ntrd"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[BAR_COLS]

def validate_bars(df: pd.DataFrame, symbols, log=None) -> tuple[pd.DataFrame, list]:
    """Checks one day's bars. A priced bar must have all four prices present, finite, > 0, with open/close inside [low, high];
    minutes must lie in the session; keys must be unique. A symbol with ANY failing row is dropped entirely and returned in the
    `invalid` list (it stays absent from the file and is retried; it is never recorded as empty). Symbols with no bar at all get one
    empty row so the file records that they were queried."""
    df = df.copy(); invalid = set(); counts = {}
    if len(df):
        m = (df["minute"] / pd.Timedelta(minutes=1)).astype(int) - 570
        bad_m = (m < 0) | (m > 389); counts["outside_session"] = int(bad_m.sum()); invalid |= set(df.loc[bad_m, "symbol"])
        dup = df.duplicated(["symbol", "minute"], keep=False); counts["duplicate"] = int(dup.sum()); invalid |= set(df.loc[dup, "symbol"])
        px = df[["open", "high", "low", "close"]]
        priced = px.notna().any(axis=1)
        bad_px = priced & (~np.isfinite(px.astype(float)).all(axis=1) | (px <= 0).any(axis=1) | (df["high"] < df[["open", "close"]].max(axis=1)) | (df["low"] > df[["open", "close"]].min(axis=1)))
        counts["illegal_ohlc"] = int(bad_px.sum()); invalid |= set(df.loc[bad_px, "symbol"])
        unpriced = ~priced & (df["ntrd"].fillna(0) <= 0); counts["unpriced_empty"] = int(unpriced.sum()); invalid |= set(df.loc[unpriced, "symbol"])
        df = df[~df["symbol"].isin(invalid)]
    if log and (invalid or any(counts.values())): log(f"  invalid bars {counts}; symbols left for retry: {sorted(invalid)}")
    missing = sorted(set(symbols) - set(df["symbol"].unique()) - invalid)
    if missing:
        empty = pd.DataFrame({"symbol": missing, "suffixed": False, "minute": pd.Series([pd.NaT] * len(missing), dtype="timedelta64[ns]"),
                              "open": np.nan, "high": np.nan, "low": np.nan, "close": np.nan, "vol": np.nan, "ntrd": 0})
        df = pd.concat([df, empty], ignore_index=True) if len(df) else empty
    return df.sort_values(["symbol", "minute"]).reset_index(drop=True), sorted(invalid)

def day_path(d): return C.TAQ_DIR / f"{pd.Timestamp(d).date()}.parquet"

def read_day(d) -> pd.DataFrame:
    p = day_path(d)
    return pd.read_parquet(p) if p.exists() else pd.DataFrame(columns=BAR_COLS)

def symbols_in(d) -> set:
    p = day_path(d)
    return set(pd.read_parquet(p, columns=["symbol"]).symbol.unique()) if p.exists() else set()

def _atomic_parquet(df: pd.DataFrame, path):
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    df.to_parquet(tmp, index=False); os.replace(tmp, path)

def _merge_write(d, have, new):
    out = pd.concat([have, new], ignore_index=True) if len(have) else new
    real = set(out.loc[out["minute"].notna(), "symbol"])
    out = out[~(out["minute"].isna() & out["symbol"].isin(real))]        # an empty row never shadows real bars
    out = out.sort_values(["symbol", "minute"]).reset_index(drop=True)
    _atomic_parquet(out, day_path(d)); return out

def fetch_day(d, symbols, log=None) -> tuple[int, list]:
    """Pull the symbols of day d that the day file does not have yet, one chunk at a time; every chunk is validated, merged and
    written before the next is requested, so a failure keeps what was already fetched. Returns (symbols pulled, invalid symbols)."""
    have = read_day(d); missing = sorted(set(symbols) - set(have["symbol"].unique()))
    if not missing:
        return 0, []
    state = {"have": have, "invalid": []}
    def persist(df, chunk_syms):
        ok, bad = validate_bars(df, chunk_syms, log=log); state["invalid"] += bad
        state["have"] = _merge_write(d, state["have"], ok)
    pull_minute_bars(d, missing, on_chunk=persist)
    return len(missing), state["invalid"]

def _log_csv(name, row):
    """Append one row to data/<name>_<pid>.csv (one file per process, so parallel workers never interleave)."""
    p = C.DATA / f"{name}_{os.getpid()}.csv"
    pd.DataFrame([row]).to_csv(p, mode="a", header=not p.exists(), index=False)

def _read_logs(name) -> pd.DataFrame:
    fs = sorted(C.DATA.glob(f"{name}_*.csv"))
    return pd.concat([pd.read_csv(f) for f in fs], ignore_index=True) if fs else pd.DataFrame()

def _log_failure(d, err):
    _log_csv("download_failures", dict(time=pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"), day=str(d), pid=os.getpid(), error=str(err)[:200].replace("\n", " ")))

def download(cand: pd.DataFrame = None, shard=(0, 1), label="minute bars", log=print) -> int:
    """Pull every day of this shard (fixed slice of the full day list, newest first) that is not complete yet. Single writer per
    day file by construction: a shard is one process. Returns the number of days that failed or stayed incomplete."""
    cand = cand if cand is not None else candidates()
    need_by_day = {d: sorted(set(g["ticker"])) for d, g in cand.groupby(cand["date"].dt.date)}
    days = sorted(need_by_day, reverse=True)[shard[0]::shard[1]]
    todo = [d for d in days if not set(need_by_day[d]) <= symbols_in(d)]
    log(f"{label}: {len(cand):,} stock-days; shard {shard[0]+1}/{shard[1]} owns {len(days)} days, {len(todo)} to pull; pid {os.getpid()}")
    t_start = time.time(); failed = pulled = 0
    for i, d in enumerate(todo):
        need = need_by_day[d]; t0 = time.time()
        try:
            n, invalid = fetch_day(d, need, log=log); pulled += n
            if invalid:
                raise RuntimeError(f"{len(invalid)} symbols failed validation: {invalid[:5]}")
            if not set(need) <= symbols_in(d):
                raise RuntimeError("day file still incomplete after the pull")
            if i % 25 == 0 or i == len(todo) - 1:
                log(f"{d}: {len(need)} symbols, pulled {n} ({time.time()-t0:.1f}s)  [{i+1}/{len(todo)}, {(time.time()-t_start)/60:.0f} min]")
        except Exception as e:
            failed += 1; _log_failure(d, e); log(f"{d}: FAILED {str(e)[:160]}")
    log(f"{label}: shard {shard[0]+1}/{shard[1]} finished: {len(todo)} days, {pulled} symbols pulled, {failed} failed ({(time.time()-t_start)/60:.0f} min)")
    return failed

def minute_bars_for(pairs: pd.DataFrame) -> dict:
    """On-demand bars for (date, ticker, permno) rows (held days): pulled into the day files if missing, returned as
    {(permno, day_int): bars with m = minutes since 9:30, raw prices}. Raises if any pull fails."""
    if len(pairs):
        failed = download(pairs, label="held-day bars", log=lambda *a: None)          # single writer: backtest runs after the download
        if failed:
            raise RuntimeError(f"{failed} held-day pulls failed or stayed incomplete")
    out = {}
    for d, g in pairs.groupby(pairs["date"].dt.date):
        b = read_day(d); b = b[b["minute"].notna()]
        if b.empty:
            continue
        b = b.assign(m=(b["minute"] / pd.Timedelta(minutes=1)).astype(int) - 570)
        dn = int(np.datetime64(d, "D").astype(np.int64))
        for permno, tick in zip(g["permno"], g["ticker"]):
            bs = b[b["symbol"] == tick]
            if len(bs):
                out[(int(permno), dn)] = bs.sort_values("m")[["m", "open", "high", "low", "close"]].reset_index(drop=True)
    return out

# ------------------------------------------------------------------------------------------------------------ NBBO
NBBO_COLS = ["date", "symbol", "minute", "bid", "ask"]

def _form_on(d) -> dict:
    """{symbol: suffixed?} as recorded in the day file (the TAQ form that answered the minute-bar query)."""
    f = read_day(d); f = f[f["minute"].notna()]
    return dict(zip(f["symbol"], f["suffixed"].astype(bool))) if len(f) else {}

def nbbo_sql(d, req: pd.DataFrame) -> str:
    """Last valid quote in the 5 minutes before each requested (symbol, fill minute); one row per request, NULLs when none.
    The TAQ form is the one the day's minute bars came from; symbols without bars that day query both forms."""
    d = pd.Timestamp(d); sp = symbol_pairs(req["ticker"].unique()); form = _form_on(d)
    rows = []; roots = set()
    for sym, m in zip(req["ticker"], req["minute"]):
        pairs = sp[sym]
        if sym in form:
            pairs = [p for p in pairs if (p[1] != "") == form[sym]] or pairs
        rows += [f"('{sym}', '{root}', '{sfx}', {int(m)})" for root, sfx in pairs]; roots |= {root for root, _ in pairs}
    return f"""
    select r.sym as symbol, (r.sfx <> '') as suffixed, r.m as minute, q.best_bid as bid, q.best_ask as ask
    from (values {', '.join(rows)}) as r(sym, root, sfx, m)
    left join lateral (
        select best_bid, best_ask from taqmsec.complete_nbbo_{d.strftime('%Y%m%d')} n
        where n.sym_root in {in_list(sorted(roots))} and n.sym_root = r.root and coalesce(n.sym_suffix, '') = r.sfx
          and n.time_m >= time '09:30:00' + (r.m - 5) * interval '1 minute' and n.time_m < time '09:30:00' + r.m * interval '1 minute'
          and n.best_bid > 0 and n.best_ask > n.best_bid and n.best_ask < 99999
        order by n.time_m desc limit 1) q on true"""

def nbbo_for(req: pd.DataFrame) -> dict:
    """req: DataFrame(date, ticker, minute). Returns {(ticker, day_int, minute): half_spread} for requests with a valid quote;
    requests already answered (with or without a quote) are read from nbbo.csv, the rest are queried (one query per day) and
    appended. A failed query raises."""
    have = pd.read_csv(C.NBBO, parse_dates=["date"], dtype={"symbol": str}) if C.NBBO.exists() else pd.DataFrame(columns=NBBO_COLS)
    req = req.drop_duplicates(["date", "ticker", "minute"])
    got = set(zip(have["date"].dt.date, have["symbol"], have["minute"].astype(int))) if len(have) else set()
    todo = req[[(d.date(), s, int(m)) not in got for d, s, m in zip(req["date"], req["ticker"], req["minute"])]]
    new = []
    days = sorted(todo["date"].dt.date.unique())
    if days:                                                        # WRDS lacks the NBBO table for a few days (e.g. 2026-02-24): those requests are stored as "no quote" (fallback spread)
        names = set(sql("select table_name from information_schema.tables where table_schema='taqmsec' and table_name in " + in_list([f"complete_nbbo_{d.strftime('%Y%m%d')}" for d in days]))["table_name"])
        missing = [d for d in days if f"complete_nbbo_{d.strftime('%Y%m%d')}" not in names]
        if missing:
            print(f"NBBO: no quote table on WRDS for {', '.join(str(d) for d in missing)}; {int(todo['date'].dt.date.isin(missing).sum())} fill minutes fall back to the tier spread")
    for d, g in todo.groupby(todo["date"].dt.date):
        if d in missing:
            new.append(pd.DataFrame(dict(date=pd.Timestamp(d), symbol=g["ticker"].values, suffixed=False, minute=g["minute"].astype(int).values, bid=np.nan, ask=np.nan))[NBBO_COLS]); continue
        df = sql(nbbo_sql(d, g))                                   # raises on failure
        df["has"] = df["bid"].notna(); df["suffixed"] = df["suffixed"].astype(bool)
        # both TAQ forms of a class symbol may answer: keep the suffixed form when it has a quote, else the plain one (never by price)
        df = df.sort_values(["symbol", "minute", "has", "suffixed"], ascending=[True, True, False, False]).drop_duplicates(["symbol", "minute"], keep="first")
        new.append(df.assign(date=pd.Timestamp(d))[NBBO_COLS])
    if new:                                                         # single writer (backtests run one at a time); atomic replace
        have = pd.concat([have, pd.concat(new, ignore_index=True)], ignore_index=True)
        have["date"] = pd.to_datetime(have["date"]); have["minute"] = have["minute"].astype(int)
        have = have.drop_duplicates(["date", "symbol", "minute"], keep="first")
        tmp = C.NBBO.with_name(f"nbbo.tmp{os.getpid()}"); have.to_csv(tmp, index=False); os.replace(tmp, C.NBBO)
    out = {}
    want = set(zip(req["date"].dt.date, req["ticker"], req["minute"].astype(int)))
    for d, s, m, bid, ask in zip(have["date"].dt.date, have["symbol"], have["minute"].astype(int), have["bid"], have["ask"]):
        if (d, s, m) in want and pd.notna(bid) and pd.notna(ask) and ask > bid > 0:
            out[(s, int(np.datetime64(d, "D").astype(np.int64)), m)] = float((ask - bid) / 2 / ((ask + bid) / 2))
    return out

# ------------------------------------------------------------------------------------------------------------ run file, status, main
def _run() -> dict:
    return json.loads(C.DOWNLOAD_RUN.read_text()) if C.DOWNLOAD_RUN.exists() else {}

def coverage(cand: pd.DataFrame = None) -> pd.DataFrame:
    """Per year: candidate stock-days, with bars, empty (queried, no eligible print), missing; complete days."""
    cand = cand if cand is not None else candidates()
    rows = []
    for d, g in cand.groupby(cand["date"].dt.date):
        p = day_path(d)
        if not p.exists():
            rows.append((d.year, len(g), 0, 0, len(g), 0)); continue
        f = pd.read_parquet(p, columns=["symbol", "minute", "open", "high", "low", "close"])
        priced = f["minute"].notna() & f[["open", "high", "low", "close"]].notna().all(axis=1)
        with_bars = set(f.loc[priced, "symbol"]); empty = set(f["symbol"]) - with_bars           # odd-lot-only minutes do not count as data
        need = set(g["ticker"]); nb = len(need & with_bars); ne = len(need & empty); nm = len(need) - nb - ne
        rows.append((d.year, len(need), nb, ne, nm, int(nm == 0)))
    df = pd.DataFrame(rows, columns=["year", "stock_days", "with_bars", "empty", "missing", "complete_day"])
    out = df.groupby("year").agg(stock_days=("stock_days", "sum"), with_bars=("with_bars", "sum"), empty=("empty", "sum"), missing=("missing", "sum"),
                                 days=("complete_day", "size"), complete_days=("complete_day", "sum"))
    out["bars_pct"] = out["with_bars"] / out["stock_days"]; out["empty_pct"] = out["empty"] / out["stock_days"]; out["complete_pct"] = out["complete_days"] / out["days"]
    return out

def main(argv):
    cmd = argv[0] if argv else "status"
    if cmd == "prepare":
        refresh = [int(argv[i + 1]) for i, a in enumerate(argv) if a == "--refresh"]
        chunk = int(argv[argv.index("--chunk") + 1]) if "--chunk" in argv else 40
        shards = int(argv[argv.index("--shards") + 1]) if "--shards" in argv else 4
        t0 = time.time(); ensure_crsp(refresh); ensure_factors(); panel = ensure_panel(); cand = ensure_candidates(panel)
        run = dict(run_id=uuid.uuid4().hex[:8], candidates=_candidates_hash(), shards=shards, chunk=chunk, sql=_run().get("sql", "array_agg"), created=pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"))
        C.DOWNLOAD_RUN.write_text(json.dumps(run, indent=1)); print(f"prepared ({(time.time()-t0)/60:.0f} min): {run}", flush=True)
    elif cmd == "download":
        if not is_current():
            raise SystemExit("run python3 data.py prepare first (panel / candidates / run file out of date)")
        n = _run()["shards"]; i = int(argv[argv.index("--shard") + 1]) if "--shard" in argv else 1
        if not 1 <= i <= n:
            raise SystemExit(f"--shard must be 1..{n} (frozen by prepare --shards {n})")
        sys.exit(1 if download(shard=(i - 1, n)) else 0)
    elif cmd == "repair-empties":
        # empty rows written for class-share symbols before the fifth-letter TAQ form was queried: drop them so a download re-pulls them
        cls = _class_symbols(); n = 0
        for p in sorted(C.TAQ_DIR.glob("*.parquet")):
            f = pd.read_parquet(p); drop = f["minute"].isna() & f["symbol"].isin(cls)
            if drop.any():
                _atomic_parquet(f[~drop].reset_index(drop=True), p); n += int(drop.sum())
        print(f"removed {n} empty rows of class-share symbols; re-run download --shard 1..{_run().get('shards', 1)} to retry them")
    elif cmd == "status":
        cov = coverage(); print(cov.to_string(formatters={"bars_pct": "{:.1%}".format, "empty_pct": "{:.1%}".format, "complete_pct": "{:.1%}".format}))
        print(f"total: {cov['with_bars'].sum():,} with bars, {cov['empty'].sum():,} empty, {cov['missing'].sum():,} missing of {cov['stock_days'].sum():,} stock-days; "
              f"{cov['complete_days'].sum()} of {cov['days'].sum()} days complete")
        f = _read_logs("download_failures")
        if len(f): print(f"failures logged: {len(f)} ({f.day.nunique()} days; last: {f.time.iloc[-1]} {f.day.iloc[-1]} {str(f.error.iloc[-1])[:80]})")
        a = _read_logs("symbol_ambiguities")
        if len(a): print(f"symbol-form ambiguities logged: {len(a)} stock-days, {a.symbol.nunique()} symbols (data/symbol_ambiguities_*.csv)")

    else:
        raise SystemExit(__doc__)

if __name__ == "__main__":
    main(sys.argv[1:])
