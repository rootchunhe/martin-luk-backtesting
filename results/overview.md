# Overview: all runs and benchmarks

Sample 2016-01-04 to 2026-08-31. Every row is after costs and on daily returns; Sharpe = annualised mean of daily returns in excess of the T-bill rate over their standard deviation; vm = a fraction of the benchmark plus T-bills, scaled to the `full` strategy's volatility; 6-factor alpha = intercept (x252) of daily excess returns on the Fama-French factors (market, size, value, profitability, investment, momentum), the return per year the factor exposures do not explain, Newey-West t in brackets; beta = market loading. Passive benchmarks are monthly-rebalanced; the trade columns do not apply to the benchmarks. Avg % invested = capital utilisation: market value of open positions / net asset value, averaged over all trading days; profit/loss ratio = gross profits / gross losses.

## Passive benchmarks

|  | CAGR | vol | Sharpe | max DD | 6-factor alpha/yr (t) | beta | trades/yr | win | avg win | avg loss | avg trade | profit/loss ratio | avg % invested |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| QQQ | 19.9% | 22.3% | 0.83 | -35% | 3.0% (1.9) | 1.11 | - | - | - | - | - | - | - |
| QQQ vm | 12.0% | 11.8% | 0.83 | -19% | 1.6% (1.9) | 0.59 | - | - | - | - | - | - | - |
| 12-1 momentum top decile | 21.0% | 32.1% | 0.68 | -46% | 7.5% (1.7) | 1.17 | - | - | - | - | - | - | - |
| momentum vm | 10.2% | 11.8% | 0.68 | -18% | 2.7% (1.7) | 0.43 | - | - | - | - | - | - | - |
| universe hold (Luk leader screen) | -19.2% | 50.2% | -0.22 | -95% | -19.3% (-1.6) | 0.90 | - | - | - | - | - | - | - |
| universe hold vm | -1.0% | 11.8% | -0.22 | -37% | -4.6% (-1.6) | 0.21 | - | - | - | - | - | - | - |

## Strategy logic: one block removed at a time

Martin Luk's pullback rule, mechanised. Universe: common stocks with a close of $5 or more, 20-day dollar volume of $50M or more, 20-day average daily range of 5% or more, up 30% or more over the last month or quarter, daily 9 > 21 > 50 EMA and above the VWAP anchored at the 250-day high (all on the prior day's close). Setup: the day's low trades at or through the prior day's 9 EMA, not more than one ADR below it. Entry: at 15:54 the stock is above its open and above the 9 EMA and in the upper 60% of the day's range; buy at the 15:55 open. Stop: the low of the day, 2-5% below the entry and at most half the ADR. Sizing: 0.5% of the prior close's account value at risk per trade, at most 30% of the account per position, 280% gross, 10 positions, new entries only while QQQ's 9 EMA is above its 21 EMA. Exits: sell 15% once up 3 times the risk; close the rest at the next open after the first daily close below the 9 EMA, or after 15 trading days. Costs: NBBO half-spread of the fill minute, per-share commission, SEC and FINRA fees, margin at T-bill + 1.5%, idle cash at T-bill.

- `full`: the complete rule: momentum selection + pullback timing + trend-following exit
- `no_timing`: the rule without the pullback timing: a strong stock is bought at 15:55 on any day it trades within half an ADR of its 9 EMA by 15:54, with no touch, reclaim or strong-close requirement; stops, sizing and exits as in full. If this works, the edge is the universe, not the timing. Differs from full in: `touch_tolerance_adr` 0.0 -> 0.5; `require_green` True -> False; `min_close_position` 0.4 -> 0.0
- `no_trail`: the rule without the trend-following exit: the same entries and stops, no partial, no EMA exit, a fixed 10-day hold. Differs from full in: `exit_ema` 9 -> None; `partial_at_r` 3.0 -> None; `max_hold_days` 15 -> 10

|  | CAGR | vol | Sharpe | max DD | 6-factor alpha/yr (t) | beta | trades/yr | win | avg win | avg loss | avg trade | profit/loss ratio | avg % invested |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| full | 12.1% | 11.8% | 0.83 | -13% | 9.6% (2.5) | 0.04 | 27 | 29% | 14.3% | -2.9% | 2.10% | 1.98 | 8% |
| no_timing | 7.1% | 35.7% | 0.30 | -62% | 8.9% (0.8) | 0.29 | 239 | 29% | 8.3% | -3.0% | 0.27% | 1.07 | 48% |
| no_trail | 12.1% | 12.8% | 0.78 | -13% | 9.6% (2.3) | 0.05 | 27 | 21% | 22.4% | -3.3% | 2.10% | 1.79 | 8% |

## Sensitivity: one parameter at a time

- `support_ema5`: the pullback level moved to the 5 EMA (faster, more touches). Differs from full in: `support` 'ema9' -> 'ema5'
- `support_ema13`: the pullback level moved to the 13 EMA (slower, fewer touches). Differs from full in: `support` 'ema9' -> 'ema13'
- `no_avwap`: the anchored-VWAP condition dropped from the universe. Differs from full in: `above_avwap` True -> False
- `adv_10m`: the liquidity floor lowered from $50M to $10M of 20-day dollar volume. Differs from full in: `min_dollar_volume` 50000000.0 -> 10000000.0
- `risk_1pct`: the same trades with 1% of the account at risk per trade instead of 0.5%. Differs from full in: `risk_per_trade` 0.005 -> 0.01
- `exit_ema21`: the trailing exit moved from the 9 EMA to the 21 EMA. Differs from full in: `exit_ema` 9 -> 21

|  | CAGR | vol | Sharpe | max DD | 6-factor alpha/yr (t) | beta | trades/yr | win | avg win | avg loss | avg trade | profit/loss ratio | avg % invested |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| support_ema5 | 7.2% | 14.1% | 0.40 | -18% | 5.1% (1.1) | 0.07 | 45 | 22% | 14.0% | -3.0% | 0.78% | 1.27 | 12% |
| support_ema13 | 5.3% | 8.6% | 0.37 | -11% | 3.2% (1.2) | 0.01 | 18 | 35% | 7.4% | -2.5% | 0.93% | 1.65 | 3% |
| no_avwap | 18.2% | 18.5% | 0.87 | -27% | 15.7% (2.6) | 0.09 | 74 | 26% | 14.0% | -3.0% | 1.46% | 1.55 | 19% |
| adv_10m | 16.8% | 18.3% | 0.82 | -30% | 14.1% (2.3) | 0.11 | 73 | 28% | 12.5% | -3.0% | 1.27% | 1.52 | 20% |
| risk_1pct | 17.4% | 18.3% | 0.84 | -23% | 15.0% (2.4) | 0.07 | 27 | 29% | 14.3% | -2.9% | 2.10% | 1.87 | 12% |
| exit_ema21 | 11.1% | 12.9% | 0.70 | -16% | 8.7% (2.1) | 0.05 | 27 | 22% | 19.9% | -3.0% | 1.91% | 1.79 | 9% |

## Other runs

- `morning_breakout`: Luk's own entry, mechanised: a flush through the 9 EMA in the first hour, then the break of the previous 1-minute (first 15 min) or 5-minute bar high, no more than 3% above the low of day, losers closed at the end of the day; not in the default runs. Differs from full in: `entry` 'close_confirm' -> 'morning_breakout'; `min_stop_width` 0.02 -> 0.003; `cut_same_day` False -> True

|  | CAGR | vol | Sharpe | max DD | 6-factor alpha/yr (t) | beta | trades/yr | win | avg win | avg loss | avg trade | profit/loss ratio | avg % invested |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| morning_breakout | -2.2% | 30.0% | -0.00 | -59% | -1.7% (-0.2) | 0.23 | 166 | 13% | 13.0% | -1.8% | 0.04% | 0.95 | 25% |
