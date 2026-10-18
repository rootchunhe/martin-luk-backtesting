"""Strategy specifications. BASE is the full rule, written in the order Luk describes it (README section 1): pick leaders, wait for the
pullback into support, buy the recovery, stop at the low of day, size by risk, trail the 9 EMA. Variants list only the keys
they change. python3 backtest.py runs DEFAULT; python3 backtest.py <name> runs one.

Parameters marked [ours] are choices the data forced or we made, not Luk's words."""

BASE = dict(
    # 1. leaders: what qualifies as a strong stock (all judged on the prior day's close)
    min_price=5.0,              # raw close >= $5 [ours; he only sizes down under $5]
    min_dollar_volume=50e6,     # 20-day average dollar volume >= $50M: no micro-caps (Luk prefers liquid names; his "premier" tier is >= $100M)
    min_adr=0.05,               # 20-day average daily range >= 5% (Luk: "fast-moving stocks, larger than 5% ADR")
    min_run_up=0.30,            # up >= 30% over 1 or 3 months [a third party's threshold, not Luk's]
    ema_stack="9>21>50",        # daily 9 > 21 > 50 EMA (Luk's "leading" list); "9>21" relaxes it
    above_avwap=True,           # close above the AVWAP anchored at the 250-day high (Luk: never buy pullbacks basing below the AVWAP)

    # 2. pullback: which support, how close
    support="ema9",             # prior day's 9 EMA (Luk: "wait at least for the daily 9 EMA"); "ema21" = the slower pullback
    touch_tolerance_adr=0.0,    # the day's low (up to the decision minute) must reach the support: 0 = trade at or through it; 0.5 = within half an ADR above it counts
    max_undercut_adr=1.0,       # a low more than 1 ADR under the support is a breakdown, not a pullback [ours]

    # 3. recovery and entry
    entry="close_confirm",      # [ours] at 15:54 require touch + reclaim + strong position in the range, fill at the 15:55 open.
                                # "morning_breakout" is Luk's own entry: flush through the support in the first hour, buy the
                                # break of the previous 1-minute (first 15 min) / 5-minute bar high, no more than 3% off the low
    decision_minute=385,        # close_confirm: minutes after 9:30 -> 15:55; the decision uses the bars before it, the fill is its open
    require_green=True,         # close_confirm: price above the open and above the support at the decision ("reclaim")
    min_close_position=0.40,    # close_confirm: price in the upper 60% of the day's range so far ("strong close")
    min_bars=60,                # close_confirm: at least 60 minutes with an eligible print before 15:54, else no trade [ours; data-gap guard.
                                # NOTE: this is a liquidity screen in disguise: minutes with only odd-lot / sub-penny prints carry no price]
    max_stale_minutes=5,        # close_confirm: the last priced minute before the decision must be within 5 minutes of it [ours; data-gap guard]
    entry_window_minutes=60,    # morning_breakout: entries only in the first 60 minutes (Luk: "the first one or two hours")
    bar_switch_minute=15,       # morning_breakout: 1-minute bars before minute 15, 5-minute bars after (Luk)
    flush_undercut=0.005,       # morning_breakout: the flush must trade >= 0.5% under the support ("a really fast flush into the support")
    max_chase_from_low=0.03,    # morning_breakout: no entry more than 3% above the low of day (Luk)
    cut_same_day=False,         # exit at the close of the entry day if the close is under the entry (Luk cuts losers the same day)

    # 4. stop: the low of day
    min_stop_width=0.02,        # entry at least 2% above the low of day [ours; daily noise]
    max_stop_width=0.05,        # at most 5% ... (Luk)
    max_stop_width_adr=0.5,     # ... and at most half the ADR (Luk)

    # 5. sizing and portfolio
    starting_capital=100_000,
    risk_per_trade=0.005,       # loss at the stop = 0.5% of the prior day's closing account value (Luk: "around 0.5%", up to 0.8%)
    max_position_pct=0.30,      # one position at most 30% of the account (Luk: typically 20-30%, cap 35%)
    max_gross_exposure=2.8,     # gross long at most 280% (Luk's 2025 peak)
    max_positions=10,           # (Luk: normally 3-4, up to 11)
    priority="ret63",           # with more signals than slots, buy the strongest 3-month performers first [ours]
    market_filter="qqq_9>21",   # new entries only while QQQ 9 EMA > 21 EMA on the prior day (Luk: no longs with the index under the EMAs); None = off

    # 6. exits
    partial_at_r=3.0,           # sell part of the position once up 3R (Luk: partials at 3-5R); None = no partial
    partial_fraction=0.15,      # 15% of the position (Luk: 10-15% each time)
    exit_ema=9,                 # out at the next open after the first daily close below the 9 EMA (Luk); None = no trend exit
    max_hold_days=15,           # [ours; Luk holds 1-3 weeks]; with exit_ema=None this is a fixed holding period

    # 7. costs
    slippage="nbbo",            # each fill pays the NBBO half-spread of its minute (fallback: liquidity-tier medians); a number = flat per side
    stop_fill="minute_open",    # stops fill at the open of the minute that traded through the stop when that is worse ("stop" = at the stop price)
    stop_slippage=0.0,          # extra adverse fill on stop orders, fraction of price
    margin_rate=None,           # annual interest on borrowed cash: None = the daily T-bill rate + margin_spread (a retail broker's base rate); a number = flat
    margin_spread=0.015,        # 1.5% over T-bills when margin_rate is None
)

# ---- the strategy is momentum selection + pullback timing + trend-following exit; each ablation removes one block ----
VARIANTS = {
    "no_timing":  dict(touch_tolerance_adr=0.5, require_green=False, min_close_position=0.0),   # a strong stock near its 9 EMA (within 0.5 ADR by 15:54), bought at 15:55 whatever the day
                                                                                               # looks like: no touch, no reclaim, no strong close; stops and exits as in full
    "no_trail":   dict(exit_ema=None, partial_at_r=None, max_hold_days=10),              # stop at the low of day, then a fixed 10-day hold; no trailing exit, no partial
}
LOGIC = ["full", "no_timing", "no_trail"]

# ---- sensitivity: one parameter at a time ----
SENSITIVITY = {
    "support_ema5":   dict(support="ema5"),             # a faster pullback level
    "support_ema13":  dict(support="ema13"),            # a slower one
    "no_avwap":       dict(above_avwap=False),          # without the anchored-VWAP condition
    "adv_10m":        dict(min_dollar_volume=10e6),     # small names back in
    "risk_1pct":      dict(risk_per_trade=0.01),        # same trades, double leverage
    "exit_ema21":     dict(exit_ema=21),                # slower trailing exit
}

# ---- kept for reference, not run by default ----
EXTRA = {
    "morning_breakout": dict(entry="morning_breakout", min_stop_width=0.003, cut_same_day=True),   # Luk's own entry, mechanised
}
VARIANTS.update(SENSITIVITY); VARIANTS.update(EXTRA)

# ---- plain-words descriptions, used as the first paragraph of every results/<name>/summary.md and in results/overview.md ----
RULE = ("Martin Luk's pullback rule, mechanised. Universe: common stocks with a close of $5 or more, 20-day dollar volume of $50M or more, "
        "20-day average daily range of 5% or more, up 30% or more over the last month or quarter, daily 9 > 21 > 50 EMA and above the VWAP anchored "
        "at the 250-day high (all on the prior day's close). Setup: the day's low trades at or through the prior day's 9 EMA, not more than one ADR "
        "below it. Entry: at 15:54 the stock is above its open and above the 9 EMA and in the upper 60% of the day's range; buy at the 15:55 open. "
        "Stop: the low of the day, 2-5% below the entry and at most half the ADR. Sizing: 0.5% of the prior close's account value at risk per trade, "
        "at most 30% of the account per position, 280% gross, 10 positions, new entries only while QQQ's 9 EMA is above its 21 EMA. Exits: sell 15% "
        "once up 3 times the risk; close the rest at the next open after the first daily close below the 9 EMA, or after 15 trading days. "
        "Costs: NBBO half-spread of the fill minute, per-share commission, SEC and FINRA fees, margin at T-bill + 1.5%, idle cash at T-bill.")

DESCRIPTIONS = {
    "full":           "the complete rule: momentum selection + pullback timing + trend-following exit",
    "no_timing":      "the rule without the pullback timing: a strong stock is bought at 15:55 on any day it trades within half an ADR of its 9 EMA by 15:54, "
                      "with no touch, reclaim or strong-close requirement; stops, sizing and exits as in full. If this works, the edge is the universe, not the timing",
    "no_trail":       "the rule without the trend-following exit: the same entries and stops, no partial, no EMA exit, a fixed 10-day hold",
    "support_ema5":   "the pullback level moved to the 5 EMA (faster, more touches)",
    "support_ema13":  "the pullback level moved to the 13 EMA (slower, fewer touches)",
    "no_avwap":       "the anchored-VWAP condition dropped from the universe",
    "adv_10m":        "the liquidity floor lowered from $50M to $10M of 20-day dollar volume",
    "risk_1pct":      "the same trades with 1% of the account at risk per trade instead of 0.5%",
    "exit_ema21":     "the trailing exit moved from the 9 EMA to the 21 EMA",
    "morning_breakout": "Luk's own entry, mechanised: a flush through the 9 EMA in the first hour, then the break of the previous 1-minute (first 15 min) "
                        "or 5-minute bar high, no more than 3% above the low of day, losers closed at the end of the day; not in the default runs",
}

def diff_from_full(name: str) -> str:
    return "; ".join(f"`{k}` {BASE[k]!r} -> {v!r}" for k, v in VARIANTS[name].items())

def describe(name: str) -> str:
    """One sentence on what the run is, plus the parameters it changes from full."""
    name = "full" if name == "base" else name
    d = DESCRIPTIONS.get(name, "")
    return d if name == "full" else f"{d}. Differs from full in: {diff_from_full(name)}"

UNIVERSE_KEYS = ("min_price", "min_dollar_volume", "min_adr", "min_run_up", "ema_stack", "above_avwap")
ENTRY_KEYS = ("support", "touch_tolerance_adr", "max_undercut_adr", "entry", "decision_minute", "require_green", "min_close_position", "min_bars", "max_stale_minutes",
              "entry_window_minutes", "bar_switch_minute", "flush_undercut", "max_chase_from_low", "cut_same_day",
              "min_stop_width", "max_stop_width", "max_stop_width_adr")

def get(name: str) -> dict:
    if name in ("base", "full"):
        return dict(BASE, name="full")
    if name not in VARIANTS:
        raise KeyError(f"unknown spec {name!r}; known: base, {', '.join(VARIANTS)}")
    unknown = set(VARIANTS[name]) - set(BASE)
    if unknown:
        raise KeyError(f"spec {name!r} sets unknown keys {sorted(unknown)}")
    return dict(BASE, **VARIANTS[name], name=name)

def entry_params(S: dict) -> dict:
    return {k: S[k] for k in ENTRY_KEYS}
