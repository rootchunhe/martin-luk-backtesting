"""Paths, sample window, WRDS table names, fee schedules and the candidate-download gate. No strategy parameters live here:
those are in specs.py."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"               # flat: security_hist.csv, crsp_YYYY.parquet, ff_factors.csv, panel.parquet/json, candidates.parquet/json,
                                   #       nbbo.csv, passive_benchmarks.csv, entries_<hash>.parquet, download_run.json
RESULTS = ROOT / "results"         # one folder per spec
TAQ_DIR = DATA / "taq"             # YYYY-MM-DD.parquet: 1-minute bars of every symbol needed that day (candidates and held days);
                                   # a symbol that was queried and had no eligible prints is kept as one empty row, so the file is also the download status
PANEL = DATA / "panel.parquet"; PANEL_META = DATA / "panel.json"
CANDIDATES = DATA / "candidates.parquet"; CANDIDATES_META = DATA / "candidates.json"
FF_FACTORS = DATA / "ff_factors.csv"
NBBO = DATA / "nbbo.csv"           # (date, symbol, minute, bid, ask): last valid quote in the 5 minutes before each fill minute we asked for
DOWNLOAD_RUN = DATA / "download_run.json"   # frozen by `data.py prepare --shards n`: run id, candidate-set hash, shard count, chunk size

DATA_START_YEAR, DATA_END_YEAR = 2015, 2026          # 2015 = warm-up for the 250-day features
SAMPLE_START, SAMPLE_END = "2016-01-04", "2026-08-31"
BENCHMARKS = {"QQQ": 86755, "SPY": 84398, "IWM": 88222}   # CRSP permnos kept in the panel as reference series; only QQQ is used (benchmark and market filter)

CRSP_LIB = "crsp_m_stock"
FF_TABLE = "ff_all.fivefactors_daily"                # mktrf smb hml rmw cma umd rf, daily, from Ken French via WRDS
COMMON_FILTER = ("s.sharetype='NS' and s.securitytype='EQTY' and s.issuertype in ('CORP','ACOR') "
                 "and s.primaryexch in ('Q','N','A') and coalesce(s.securitysubtype,'') not in ('UNIT','UNT','WRT','RT')")

# ---- feature windows (the panel is rebuilt when these change) ----
ADR_WINDOW = 20                   # ADR = mean((high - low) / close) over 20 days
ADV_WINDOW = 20                   # 20-day average dollar volume
RUN_UP_WINDOWS = (21, 63)         # 1-month and 3-month returns
AVWAP_ANCHOR_WINDOW = 250         # AVWAP anchored at the highest high of the last 250 bars

# ---- candidate gate: which stock-days get minute bars. Deliberately wider than any spec so specs can be changed without
#      re-downloading; backtest.py refuses a spec that falls outside it. All conditions use t-1 values except the day's low. ----
CANDIDATE_GATE = dict(
    min_price=3.0, min_dollar_volume=5e6, min_adr=0.05, min_run_up=0.10,   # ADR >= 5% is Luk's hard rule, so no slack there
    ema_stack="9>21", above_avwap=False,
    support="ema9", touch_tolerance_adr=0.5,     # day-t low <= prior 9 EMA * (1 + 0.5 ADR)
)

# ---- costs that are not strategy choices ----
COMMISSION_PER_SHARE = 0.0035
MIN_COMMISSION_PER_ORDER = 0.35
SEC_FEE_SCHEDULE = [("2015-02-14", 18.40e-6), ("2016-02-16", 21.80e-6), ("2017-07-04", 23.10e-6), ("2018-04-20", 13.00e-6), ("2019-04-16", 20.70e-6),
                    ("2020-02-18", 22.10e-6), ("2021-02-25", 5.10e-6), ("2022-02-25", 22.90e-6), ("2023-02-27", 8.00e-6),
                    ("2024-05-22", 27.80e-6), ("2025-05-14", 0.0)]
FINRA_TAF_SCHEDULE = [("2016-01-01", 0.000119, 5.95), ("2021-01-01", 0.000130, 6.49), ("2023-01-01", 0.000145, 7.27), ("2024-01-01", 0.000166, 8.30)]
# half-spread per side when no NBBO quote is available for a fill, by 20-day dollar volume (upper bound, half-spread);
# medians measured from TAQ NBBO at the candidate fills: the close is tight, the first hour is 3-8x wider
HALF_SPREAD_FALLBACK = {
    "close":   [(30e6, 0.0007), (100e6, 0.0006), (float("inf"), 0.0003)],
    "morning": [(30e6, 0.0040), (100e6, 0.0024), (float("inf"), 0.0013)],
}

def sec_fee_rate(d):
    r = 0.0
    for eff, rate in SEC_FEE_SCHEDULE:
        if str(d) >= eff: r = rate
    return r

def finra_taf(d):
    out = (0.0, 0.0)
    for eff, r, cap in FINRA_TAF_SCHEDULE:
        if str(d) >= eff: out = (r, cap)
    return out
