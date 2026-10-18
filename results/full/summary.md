# `full`: the complete rule: momentum selection + pullback timing + trend-following exit

Martin Luk's pullback rule, mechanised. Universe: common stocks with a close of $5 or more, 20-day dollar volume of $50M or more, 20-day average daily range of 5% or more, up 30% or more over the last month or quarter, daily 9 > 21 > 50 EMA and above the VWAP anchored at the 250-day high (all on the prior day's close). Setup: the day's low trades at or through the prior day's 9 EMA, not more than one ADR below it. Entry: at 15:54 the stock is above its open and above the 9 EMA and in the upper 60% of the day's range; buy at the 15:55 open. Stop: the low of the day, 2-5% below the entry and at most half the ADR. Sizing: 0.5% of the prior close's account value at risk per trade, at most 30% of the account per position, 280% gross, 10 positions, new entries only while QQQ's 9 EMA is above its 21 EMA. Exits: sell 15% once up 3 times the risk; close the rest at the next open after the first daily close below the 9 EMA, or after 15 trading days. Costs: NBBO half-spread of the fill minute, per-share commission, SEC and FINRA fees, margin at T-bill + 1.5%, idle cash at T-bill.

Sample 2016-01-04 to 2026-08-31, starting capital $100,000, code `cb16764382`.  
Entry `close_confirm` on the `ema9` pullback, 0.5% of the account at risk per trade, at most 10 positions and 280% gross, market filter `qqq_9>21`, slippage `nbbo`, stop fill `minute_open`, financing T-bill + 1.5%; idle cash earns the T-bill rate.
- note: 73 candidate stock-days were queried and had no eligible print (empty)
- note: 7844 of 324807 candidate stock-days were too thinly traded for close_confirm (min_bars / max_stale_minutes)

## Headline

| trades | win rate | avg win | avg loss | payoff | avg trade | profit/loss ratio | median hold win / loss (days) | avg % invested | longest underwater |
|---|---|---|---|---|---|---|---|---|---|
| 287 (27/yr) | 29% | 14.3% | -2.9% | 4.8 | 2.10% | 1.98 | 10 / 2 | 8% | 3.9 yr |

Avg % invested = capital utilisation: market value of open positions / net asset value, averaged over all trading days (the rest earns the T-bill rate); profit/loss ratio = gross profits / gross losses.


The strategy against passive benchmarks, all after costs, all on daily returns. 6-factor alpha is the intercept of daily excess returns regressed on the Fama-French factors (market, size, value, profitability, investment, momentum), times 252: the return per year the factor exposures do not explain; t uses Newey-West errors (5 lags); beta is the market loading.

|  | CAGR | vol | Sharpe | max drawdown | 6-factor alpha / yr (t) | market beta |
|---|---|---|---|---|---|---|
| **strategy** | 12.1% | 11.8% | 0.83 | -13% | 9.6% (2.5) | 0.04 |
| QQQ (total return) | 19.9% | 22.3% | 0.83 | -35% | 3.0% (1.9) | 1.11 |
| QQQ, vol-matched x0.53 | 12.0% | 11.8% | 0.83 | -19% | 1.6% (1.9) | 0.59 |
| 12-1 momentum top decile (87 names), net | 21.0% | 32.1% | 0.68 | -46% | 7.5% (1.7) | 1.17 |
| momentum, vol-matched x0.37 | 10.2% | 11.8% | 0.68 | -18% | 2.7% (1.7) | 0.43 |
| universe hold: Luk leader screen (16 names), net | -19.2% | 50.2% | -0.22 | -95% | -19.3% (-1.6) | 0.90 |
| universe hold, vol-matched x0.24 | -1.0% | 11.8% | -0.22 | -37% | -4.6% (-1.6) | 0.21 |

Passive benchmarks: equal-weight portfolios in the same liquidity pool (common stocks, price >= $5, 20-day dollar volume >= $50M), selected on the day before each month end, bought at the month end's close and held for the month, net of the same spread table plus commission. Vol-matched = a fraction k of the benchmark, chosen so the realised volatility equals the strategy's, with the rest in T-bills. Sharpe = annualised mean of daily returns in excess of the T-bill rate over their standard deviation.

![cumulative return](cumret.png)

## By year

| year | strategy | QQQ | QQQ vm | momentum vm | universe vm | trades | win | avg trade | max DD in year |
|---|---|---|---|---|---|---|---|---|---|
| 2016 | -0.2% | 7.1% | 4.1% | 7.1% | -2.4% | 9 | 22% | -0.05% | -3% |
| 2017 | -0.5% | 32.7% | 16.4% | 8.1% | 2.9% | 1 | 0% | -2.16% | -1% |
| 2018 | 0.8% | -0.1% | 1.8% | 0.1% | -5.4% | 3 | 0% | -3.08% | -1% |
| 2019 | 2.6% | 39.0% | 20.9% | 12.8% | -22.1% | 7 | 43% | 0.30% | -2% |
| 2020 | 19.0% | 48.6% | 25.8% | 22.4% | 11.4% | 68 | 28% | 1.40% | -8% |
| 2021 | 8.2% | 27.4% | 14.2% | 8.7% | -0.9% | 43 | 30% | 0.79% | -12% |
| 2022 | 4.2% | -32.6% | -17.4% | -1.8% | -15.1% | 13 | 31% | 0.73% | -2% |
| 2023 | 17.5% | 54.9% | 29.7% | 12.8% | 5.3% | 26 | 35% | 4.21% | -6% |
| 2024 | 27.7% | 25.6% | 16.0% | 17.0% | 9.3% | 25 | 40% | 3.98% | -4% |
| 2025 | 66.7% | 20.8% | 13.9% | 13.5% | 10.0% | 52 | 33% | 5.33% | -5% |
| 2026 | -1.9% | 17.0% | 10.2% | 9.3% | 2.1% | 40 | 18% | -0.34% | -13% |

## Factor attribution

Daily excess returns on the Fama-French 5 factors plus momentum (Newey-West t-statistics).

|  | days | 6-factor alpha / yr | R² | mkt | smb | hml | rmw | cma | umd |
|---|---|---|---|---|---|---|---|---|---|
| all days | 2680 | 9.6% (t 2.5) | 0.03 | 0.04 (3.0) | 0.11 (3.9) | 0.03 (1.0) | -0.10 (-3.0) | -0.03 (-0.6) | 0.02 (1.3) |
| invested days only | 739 | 26.8% (t 2.0) | 0.10 | 0.28 (4.4) | 0.27 (3.1) | 0.07 (0.9) | -0.28 (-3.0) | -0.05 (-0.3) | 0.09 (1.7) |

## Largest trades

| ticker | entry | exit | exit reason | return | P&L | share of net P&L |
|---|---|---|---|---|---|---|
| CDTX | 2025-11-10 15:55 | 2025-12-02 09:30 | max_hold | +111% | $63,581 | 32% |
| RKLB | 2024-11-04 15:55 | 2024-11-25 09:30 | max_hold | +125% | $35,637 | 18% |
| METC | 2025-09-17 15:55 | 2025-10-08 09:30 | max_hold | +57% | $18,660 | 9% |
| MARA | 2023-12-12 15:55 | 2024-01-02 09:30 | close_below_ema9 | +79% | $17,573 | 9% |
| ONDS | 2025-09-09 15:55 | 2025-09-30 09:30 | max_hold | +47% | $16,280 | 8% |
| BTDR | 2026-05-20 15:55 | 2026-06-08 09:30 | close_below_ema9 | +34% | $14,580 | 7% |
| NAVN | 2026-06-23 15:55 | 2026-07-15 09:30 | max_hold | +19% | $13,960 | 7% |
| CRDO | 2025-08-25 15:55 | 2025-09-16 09:30 | max_hold | +41% | $13,707 | 7% |
| SYM | 2025-09-30 15:55 | 2025-10-20 09:30 | close_below_ema9 | +30% | $13,503 | 7% |
| ADPT | 2026-06-18 15:55 | 2026-07-09 09:30 | close_below_ema9 | +17% | $13,006 | 7% |

Each year's largest trade against the year's net P&L: 2016 MDVN $2,960 / $-247; 2017 MDCO $-491 / $-491; 2018 RGNX $-525 / $-1,656; 2019 ETSY $837 / $107; 2020 MRNA $7,454 / $19,363; 2021 AAL $5,887 / $9,551; 2022 SWAV $9,606 / $3,961; 2023 MARA $17,573 / $21,969; 2024 RKLB $35,637 / $32,153; 2025 CDTX $63,581 / $128,162; 2026 BTDR $14,580 / $-13,568.

## Risk

- Concentration: the top 5% of trades are 65% of gross wins; without the best 1% (2 trades) the average trade is 1.35% and the profit/loss ratio 1.49.
- Trade returns (net): p5 -4.6%, median -2.5%, p95 28.9%, best 109.9%, worst -14.4%.
- Gap-through stops: 20 trades (7.0%), $-35,897 of the $199,302 net; losses beyond twice the planned risk: 2; closed by delisting: 0; dividends credited $794; halted position-days: 0.
- Worst five: SYRE 2026-08-20 -14.4% (stop_gap); BTDR 2026-06-12 -7.4% (close_below_ema9); COHR 2026-06-30 -6.5% (stop_gap); AEVA 2026-06-22 -6.3% (stop_gap); BEAM 2025-10-23 -5.6% (stop_gap).
- Block bootstrap (20-day blocks, 2000 draws): Sharpe 90% interval [0.46, 1.29], max drawdown median -12%, 5th percentile -18%.
- Capacity (position / 20-day dollar volume, sizing scaled to a fixed account): median 0.02% at $100,000; at $1M median 0.15%, 0% of trades above 1%.
- Worst day -6.1% (2026-06-05); days below -5%: 1; max simultaneous positions 10.

## Execution and costs

- Stop width median 2.8%; planned loss per trade 0.5% of the account.
- Entry half-spread by liquidity tier: $30-100M 0.056% (n=118, avg trade 2.06%); >$100M 0.036% (n=169, avg trade 2.12%).
- NBBO quotes (last quote before the fill minute) found for 100% of spread-paying fills (the rest use the tier table); exits with an exact minute: 100%.

| cost scenario | avg trade | profit/loss ratio | CAGR | Sharpe | max drawdown |
|---|---|---|---|---|---|
| zero slippage (commissions and fees only) | 2.24% | 2.14 | 13.0% | 0.90 | -12% |
| as specified (slippage=nbbo, stop fill=minute_open, margin T-bill+1.5%) | 2.10% | 1.98 | 12.1% | 0.83 | -13% |
| conservative (0.2% per side, stop slip 0.5%, 8% margin) | 1.60% | 1.64 | 9.5% | 0.62 | -16% |
