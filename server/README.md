# Server scripts

Covers two markets kept side by side: the **TSX Venture** (tickers ending `.V`) and the **TSX** (`.TO`). Same rules for both; the webpage has a tab for each (index.html and tsx.html), and the research files come in pairs (`patterns.json` / `patterns_tsx.json`, `signal_lab.json` / `signal_lab_tsx.json`).

The scripts your home stock server runs. To install the latest version, run on the server:

    stocks-update

That pulls this folder and copies the scripts into ~/stocks. Your settings (stocks.env), database password and data are never touched.

## Liquidity rule

The scanner only flags stocks trading enough **dollars** a day (20-day average volume x price): **C$100k** on the Venture, **C$50k** on the TSX. Set in `benchmarks.MIN_DOLLARS`; chosen from the signal lab's 10-year liquidity test. Discovery still tracks anything with 50,000+ shares a day or that passes the dollar rule.

## What runs when

| When (server time) | Script | What it does |
|---|---|---|
| Weekdays, every 5 min | run_intraday.sh | Live scan, Rising flags (logged to `rising_log`), webpage; GitHub feed every 30 min |
| Weekdays 3:15 pm | run_daily.sh | Find new TSXV stocks, daily prices, screen, save 5-minute bars, dashboard, feed |
| Weekdays 6:30 am | run_news.sh | Latest headlines for every stock (Yahoo, then Google News), published for the webpage and terminal |
| Saturday 8:00 am | run_weekly.sh | New listings, pattern study (`patterns.json`), signal lab (`signal_lab.json`) |
| Daily 7:35 am and 9:30 pm | run_backup.sh | Load the terminal's backup (notes, picks, memes, track record, paper account) and rebuild the webpage |
| Always (checked every 5 min) | run_listapi.sh | Small service on port 8081 the webpage uses to add and remove stocks on My list |

## My list (shared with the trading terminal)

One watchlist, the same on the webpage and in the terminal. The terminal keeps the master copy:

- **Terminal to webpage:** the terminal sync task (weekdays 6:40am, 10:40am, 3:40pm) publishes the terminal's watchlist to the `mylist` branch of this repo. The server pulls it every 30 minutes on weekdays (`mylist.py`, via `sync_requests.py`) into the `my_list` table, and starts tracking any stock it didn't have.
- **Webpage to terminal:** adding or removing on the webpage goes to `listapi.py` (port 8081), is saved in `list_changes` and published as `list_changes.json` right away. The terminal applies it at its next sync, then the change clears.

Removing a stock from your list never stops the scanner tracking it. PRL, XTRA and PNG are built into the terminal and always on the list.

    ./venv/bin/python mylist.py --status   # your list and any changes waiting for the terminal

The webpage's Track record, Reports, Meme stocks and Paper account tabs show the terminal's own records from its backup (`terminal_backup.py`), refreshed at 7:35am and 9:30pm.

## Dashboard (webpage front page)

`ideas.py` ranks the stocks that fired a buy signal at the last close by how that signal did in the signal lab for the stock's own market and price band (under / over C$0.50), then adjusts for news in the last 3 days and what the terminal's pre-market notes, weekly picks and meme reviews said. It runs inside `dashboard.py` and only recomputes when new daily prices or a new lab run arrive.

    ./venv/bin/python ideas.py    # print the current ranking

It's the evidence in one place, not a forecast: every signal lost money on average when held for one day; the tested edges played out over 5 to 20 days.

## Research universe

`universe.py` keeps every TSX Venture listing Yahoo has ever shown us (table `universe`), with 10 years of daily prices, even after a stock stops trading. It's for research only: never screened. First load: `run_research.sh` (runs in the background, resumable). Consolidations trigger a full reload of that stock's history.

## Data you can open

- 5-minute spreadsheets, one CSV per day: http://192.168.1.108:8080/stocks/intraday/
- Market benchmark: TSX Venture Composite (`^SPCDNX`) and TSX Composite (`^GSPTSE`), stored in `daily_prices` but not in the watchlist, so they're never screened. Live index level in `benchmark_quotes`.
- Database tables: `daily_prices`, `bars_5m` (every 5-minute bar, kept for good), `rising_log`, `watchlist`, `my_list`, `list_changes`, `terminal_backup`

## One-off tools

    cd ~/stocks && . ./stocks.env
    ./venv/bin/python backtest.py                    # Flagged signal, past year
    ./venv/bin/python signal_lab.py --no-push        # all signals, print only
    ./venv/bin/python intraday_archive.py --backfill # re-save the ~60 days Yahoo keeps
