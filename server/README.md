# Server scripts

Covers two markets kept side by side: the **TSX Venture** (tickers ending `.V`) and the **TSX** (`.TO`). Same rules for both; the webpage has a tab for each (index.html and tsx.html), and the research files come in pairs (`patterns.json` / `patterns_tsx.json`, `signal_lab.json` / `signal_lab_tsx.json`).

The scripts your home stock server runs. To install the latest version, run on the server:

    stocks-update

That pulls this folder and copies the scripts into ~/stocks. Your settings (stocks.env), database password and data are never touched.

## What runs when

| When (server time) | Script | What it does |
|---|---|---|
| Weekdays, every 30 min | run_intraday.sh | Live scan, Rising flags (logged to `rising_log`), dashboard, feed |
| Weekdays 3:15 pm | run_daily.sh | Find new TSXV stocks, daily prices, screen, save 5-minute bars, dashboard, feed |
| Saturday 8:00 am | run_weekly.sh | New listings, pattern study (`patterns.json`), signal lab (`signal_lab.json`) |

## Research universe

`universe.py` keeps every TSX Venture listing Yahoo has ever shown us (table `universe`), with 10 years of daily prices, even after a stock stops trading. It's for research only: never screened. First load: `run_research.sh` (runs in the background, resumable). Consolidations trigger a full reload of that stock's history.

## Data you can open

- 5-minute spreadsheets, one CSV per day: http://192.168.1.108:8080/stocks/intraday/
- Market benchmark: TSX Venture Composite (`^SPCDNX`) and TSX Composite (`^GSPTSE`), stored in `daily_prices` but not in the watchlist, so they're never screened. Live index level in `benchmark_quotes`.
- Database tables: `daily_prices`, `bars_5m` (every 5-minute bar, kept for good), `rising_log`, `watchlist`

## One-off tools

    cd ~/stocks && . ./stocks.env
    ./venv/bin/python backtest.py                    # Flagged signal, past year
    ./venv/bin/python signal_lab.py --no-push        # all signals, print only
    ./venv/bin/python intraday_archive.py --backfill # re-save the ~60 days Yahoo keeps
