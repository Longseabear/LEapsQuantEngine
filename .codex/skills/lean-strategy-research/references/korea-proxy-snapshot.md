# Korea Proxy LEAN Strategy Snapshot

Snapshot date: 2026-05-17 Asia/Seoul.

Use this as a starting point only. Re-browse before claiming current rankings,
recent leaderboard status, or latest performance.

## Bottom Line

Public QuantConnect/LEAN evidence found during the snapshot did not expose a
clear "best recent KOSPI/KOSDAQ individual-stock model." The visible Korea
exposure was mainly through US-listed country ETFs:

- `EWY`: iShares MSCI South Korea ETF
- `FLKR`: Franklin FTSE South Korea ETF

The strongest visible candidate by CAR and Sharpe in the snapshot was
`SeasonalFrontRunninginCountryETFs`, a country ETF seasonal rotation model that
included `EWY`.

## Candidate Table

| Candidate | Korea exposure | Model family | Generated | CAR | Sharpe | Drawdown | Net Profit | Source |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `SeasonalFrontRunninginCountryETFs` | `EWY` | Country ETF seasonality / front-running | 2025-12-02 11:44:22 | 29.308% | 1.128 | 13.900% | 26.605% | https://www.quantconnect.com/terminal/cache/embedded_backtest_1f8bbd5874aac17e3e3b65b78deee22a.html |
| `CountryRotationAlphaModel` | `FLKR` | Country ETF rotation / country allocation | 2024-08-06 00:11:17 | 19.353% | 0.724 | 15.600% | 38.223% | https://www.quantconnect.com/terminal/cache/embedded_backtest_426a2cb2a5b2d860dfc1499daf1a82a4.html |
| `ConstantNoBenchmarkAlphaModel` snapshot | `EWY`, `FLKR` | ETF clustering / momentum-like allocation | 2025-11-30 01:30:29 | 17.829% | 0.526 | 51.000% | 163.864% | https://www.quantconnect.com/terminal/cache/embedded_backtest_9c3b7c89dfb37d271c4d8d9757f783a2.html |
| `value-factor-effect-within-countries` | `EWY` | Cross-country value factor | 2024 snapshot | 9.807% | 0.384 | 36.100% | 358.031% | https://www.quantconnect.com/terminal/quantpediaBacktestResult/embedded_backtest_baa1f4331a9f3ecea43984415ebaae93.html |
| `mean-reversion-effect-in-country-equity-indexes` | `EWY` | Country index mean reversion | 2025 snapshot | -4.624% | -0.301 | 81.000% | -70.006% | https://www.quantconnect.com/terminal/quantpediaBacktestResult/embedded_backtest_8a48823e9dd3709668231f64779ac816.html |

## SeasonalFrontRunninginCountryETFs Breakdown

Source article:
https://quantpedia.com/strategies/seasonal-front-running-in-country-etfs

Related article:
https://quantpedia.com/front-running-in-country-etfs-or-how-to-spot-and-leverage-seasonality/

Observed model shape:

- Universe: 23 country ETFs including `EWY`; also `SPY`, `EWU`, `EWG`, `EWQ`,
  `EWI`, `EWD`, `EWN`, `EWP`, `EWK`, `EWL`, `EWC`, `EWJ`, `EWW`, `EWM`, `EWA`,
  `EWS`, `EWT`, `EWZ`, `EWH`, `EZA`, `FXI`, and `INDY`.
- Alpha: rank ETFs by performance in the same calendar month of the previous
  year, using the model's seasonality/front-running assumption.
- Portfolio: select ranks 3 through 8 in the observed code slice and equal
  weight selected ETFs.
- Rebalance: monthly, around month end.
- Backtest setup observed in embedded code: `set_start_date(2025, 1, 1)`,
  `set_cash(100_000)`, Interactive Brokers margin model.

## LEaps Adaptation Notes

For LEapsQuantEngine, treat the public model as a research candidate:

- Use `Universe` for KRX-listed sector ETFs, liquid country/market ETFs, or a
  top-liquidity KOSPI/KOSDAQ basket.
- Implement `Alpha` as a monthly seasonality insight generator. Inputs should
  be confirmed daily bars or cached history, not live quotes.
- Implement `Portfolio` as equal-weight or volatility-adjusted targets over
  selected symbols.
- Add `Risk` for max drawdown, liquidity, per-symbol cap, minimum price, and
  stale-data rejection.
- Use `Execution` to create rebalance order intents at the scheduled cadence.
- Run a local LEaps backtest with KRW fees/slippage and Korean market holidays
  before comparing with the US ETF backtest.

## Useful Source Checks

- QuantConnect Strategies docs:
  https://www.quantconnect.com/docs/v2/cloud-platform/community/strategies
- QuantConnect LEAN discussion area:
  https://www.quantconnect.com/forum/discussions/1/lean
- QuantConnect non-US stocks forum thread:
  https://www.quantconnect.com/forum/discussion/14041/backtesting-and-trading-with-non-us-stocks/
- QuantConnect LEAN GitHub:
  https://github.com/QuantConnect/Lean

## Query Seeds

Use combinations of these when re-browsing:

- `site:quantconnect.com/terminal/cache embedded_backtest EWY Sharpe`
- `site:quantconnect.com/terminal/cache embedded_backtest FLKR "Compounding Annual Return"`
- `site:quantconnect.com "CountryRotationAlphaModel" FLKR`
- `site:quantconnect.com "SeasonalFrontRunninginCountryETFs"`
- `site:quantpedia.com country ETF seasonality EWY`
- `QuantConnect LEAN Korea ETF EWY strategy backtest`
