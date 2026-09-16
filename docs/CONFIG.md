# ApexTrader Configuration Reference

This document lists all environment variables and config options for easy, holistic, and modular setup. Set these in your `.env` file or environment.

| Variable                    | Default   | Description                                      |
|-----------------------------|-----------|--------------------------------------------------|
| STOCKS_BROKER               | alpaca    | Broker for stocks: 'alpaca' or 'etrade'          |
| OPTIONS_ENABLED             | true      | Master kill-switch for all options trading        |
| OPTIONS_ALLOCATION_PCT      | 15.0      | % of equity for all options                      |
| OPTIONS_MAX_POSITIONS       | 3         | Max open options contracts                       |
| OPTIONS_DTE_MIN             | 14        | Min days-to-expiry at entry                      |
| OPTIONS_DTE_MAX             | 30        | Max days-to-expiry at entry                      |
| OPTIONS_DELTA_TARGET        | 0.40      | Target delta (0.30-0.50)                         |
| OPTIONS_MIN_OPEN_INTEREST   | 100       | Skip illiquid strikes                            |
| OPTIONS_MAX_SPREAD_PCT      | 10.0      | Max bid/ask spread % of mid                      |
| OPTIONS_MAX_IV_PCT          | 150.0     | Skip when IV is extreme                          |
| OPTIONS_MIN_IV_PCT          | 15.0      | Skip when IV is too flat                         |
| OPTIONS_PROFIT_TARGET_PCT   | 50.0      | Close at +50% gain                               |
| OPTIONS_STOP_LOSS_PCT       | 30.0      | Close at -30% loss                               |
| OPTIONS_COVERED_CALL_DELTA  | 0.25      | Sell OTM calls ~0.25 delta                       |
| OPTIONS_MIN_SIGNAL_CONFIDENCE| 0.80     | Higher bar for options                           |
| OPTIONS_MIN_STOCK_PRICE     | 5.0       | Skip options on sub-$5 stocks                    |
| OPTIONS_MIN_MOVE_PCT        | 5.0       | Min % daily move to qualify                      |
| OPTIONS_MIN_RVOL            | 3.0       | Min relative volume for MomentumCall entry        |
| OPTIONS_MIN_ADV             | 2_000_000 | Min 20-day avg daily dollar volume ($2M)         |
| OPTIONS_STOP_COOLDOWN_DAYS  | 5         | No re-entry within N days after a stop           |
| OPTIONS_EARNINGS_AVOID_DAYS | 15        | Skip entries if earnings within N days           |
| TRADE_MODE                  | paper     | 'paper' or 'live' trading mode                   |
| ...                         | ...       | ... (add more as needed)                         |

- See `engine/config.py` for all tunable settings.
- All variables can be set in `.env` or as environment variables.
- For advanced users: add/override variables per user or profile.

---

This file is auto-generated and should be updated with every new config option.
