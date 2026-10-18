# Server scripts

Covers two markets kept side by side: the **TSX Venture** (tickers ending `.V`) and the **TSX** (`.TO`). Same rules for both; the webpage has a tab for each (index.html and tsx.html), and the research files come in pairs (`patterns.json` / `patterns_tsx.json`, `signal_lab.json` / `signal_lab_tsx.json`).

The scripts your home stock server runs. To install the latest version, run on the server:

    stocks-update

That pulls this folder and copies the scripts into ~/stocks. Your settings (stocks.env), database password and data are never touched.

## Liquidity rule

The scanner only flags stocks trading enough **dollars** a day (20-day average volume x price): **C$50k** on both the Venture and the TSX. Set in `benchmarks.MIN_DOLLARS`; based on the signal lab's 10-year liquidity test (the Venture was C$100k until Oct 17, 2026). Discovery still tracks anything with 50,000+ shares a day or that passes the dollar rule.

## What runs when

| When (server time) | Script | What it does |
|---|---|---|
| Weekdays, every 5 min | run_intraday.sh | Live scan, Rising flags (logged to `rising_log`), phone alerts, webpage; GitHub feed every 30 min |
| Weekdays 3:15 pm | run_daily.sh | Find new TSXV stocks, daily prices, screen, save 5-minute bars, dashboard, feed |
| Weekdays 6:30 am | run_news.sh | Latest headlines for every stock (Yahoo, then Google News), published for the webpage and terminal |
| Saturday 8:00 am | run_weekly.sh | New listings, pattern study (`patterns.json`), signal lab (`signal_lab.json`) |
| Daily 7:35 am and 9:30 pm | run_backup.sh | Load the terminal's backup (notes, picks, memes, track record, paper account) and rebuild the webpage |
| Always (checked every 5 min) | run_listapi.sh | Small service on port 8081 the webpage uses to add and remove stocks on My list |

## My list (shared with the trading terminal)

One watchlist, the same on the webpage and in the terminal. The terminal keeps the master copy:

- **Terminal to webpage:** the terminal sync task (hourly on weekdays, 6:40am to 1:40pm, plus 3:40pm) publishes the terminal's watchlist to the `mylist` branch of this repo. The server pulls it every 30 minutes on weekdays (`mylist.py`, via `sync_requests.py`) into the `my_list` table, and starts tracking any stock it didn't have.
- **Webpage to terminal:** adding or removing on the webpage goes to `listapi.py` (port 8081), is saved in `list_changes` and published as `list_changes.json` right away. The terminal applies it at its next sync, then the change clears. You can add a stock the server doesn't track yet (a weekly pick, say); `sync_requests.py` starts tracking it within 30 minutes on weekdays.
- **Price alerts:** set a buy level (at or below) and a stop-loss on any stock's detail panel. They travel the same way (`list_changes.json`, action `alert`) into the terminal's paper account, and the terminal publishes its alert levels back in `mylist.json`, so both sides show the same levels.

Removing a stock from your list never stops the scanner tracking it. PRL, XTRA and PNG are built into the terminal and always on the list.

    ./venv/bin/python mylist.py --status   # your list and any changes waiting for the terminal

The webpage's Track record, Reports, Meme stocks and Paper account tabs show the terminal's own records from its backup (`terminal_backup.py`), refreshed at 7:35am and 9:30pm.

## Phone alerts during the day (ntfy)

`live_alerts.py` runs after every 5-minute intraday pass and pushes a notification when a stock reaches your buy level or stop-loss, or when a stock on your list starts Rising. Prices are Yahoo's, about 15 minutes delayed; each alert fires once per stock and kind per day. The terminal still messages you at the close as before.

Notifications go through [ntfy](https://ntfy.sh), a free push service: install the ntfy app and subscribe to the topic in `NTFY_TOPIC` (stocks.env; `stocks-update` created a private one). Anyone who knows the topic name can read it, so keep it to yourself. Empty `NTFY_TOPIC` turns them off. Optional: `DASHBOARD_URL` (opened when you tap a notification), `NTFY_URL` (your own ntfy server).

    ./venv/bin/python notify.py --test       # send a test notification
    ./venv/bin/python live_alerts.py --status

## Weekly picks scorecard

The Track record tab scores every Sunday pick: bought at the open of the next trading day, then the close 1, 2 and 4 weeks later and today, next to the index over the same days (TSX Venture for .V, TSX for .TO). `sync_requests.py` starts tracking every pick from the last 6 months so they all have prices. Each pick in Reports also shows how it's done since.

## Dilution warning

`financings.py` keeps every headline that announces, upsizes or closes a share sale (private placement, bought deal, flow-through or public offering, LIFE offering, shares for debt). `news.py` checks every headline it fetches, and the 6:30am run also searches Google News 6 months back for your list, the weekly picks and today's flagged stocks and ideas. Headlines within 21 days of each other count as one financing. A FINANCING tag (with the count in 90 days) shows on the scanner, my list, picks and ideas, with the latest headline on the stock's detail panel. The count builds up over time, so expect more tags in the first few weeks.

    ./venv/bin/python financings.py          # stocks that raised money in the last 90 days
    ./venv/bin/python financings.py LVG.V

## Which analysis works best (scoreboard)

`scoreboard.py` scores every source of calls the same way: scanner signals (Flagged, Breakout, Rising), the Dashboard's ideas by tier (logged daily in `ideas_log` from this version on), weekly picks (Strong, Watch and Rejected), the pre-market note's verdicts (Backed, Unclear, Caution, read from each line that ends with one), Canadian names in the Reddit review, and stocks right after a share-sale headline. Each call is bought at the next open (the same morning for the pre-market note) and valued after 5, 10 and 20 trading days, both after estimated costs and against the average liquid tracked stock over the same days. A source gets a verdict once it has 20 calls with a 2-week result: Ahead, Behind or No edge yet (roughly a 95% range around its average edge). Track record tab, plus a "What's working" card on the Dashboard.

    ./venv/bin/python scoreboard.py

## Trading costs and stop-losses

`costs.py` estimates what each stock really costs to trade. Yahoo has no bid/ask, so the spread is estimated from 60 days of daily highs, lows and closes (the Abdi-Ranaldo method), never below one price step. Each stock's page shows the round trip (spread + 0.5%), the median dollars traded a day, a position size you could sell in about a day (10% of that), how often it goes 5 minutes without a trade, and a COSTLY tag in the Scanner and My list when the round trip is over 5%.

The signal lab (Saturdays) now also:
- re-runs each signal's best exit paying every stock's estimated spread at the time, split by cheap (under 2% round trip), medium and costly stocks: Dashboard, "What trading costs do to the signals";
- tests stop-losses on every signal's trades held up to 20 days: none, fixed 10-25%, trailing 15-25%, and 2x or 3x the stock's own average daily move: Dashboard, "Which stop-loss works". The multiple of the daily move that did best for the stock's market and price range sets the suggested stop on each stock's page (one tap fills it into the alert).

    ./venv/bin/python costs.py              # costliest and cheapest stocks to trade
    ./venv/bin/python costs.py LVG.V

## Dashboard (webpage front page)

`ideas.py` ranks the stocks that fired a buy signal at the last close by how that signal did in the signal lab for the stock's own market and price band (under / over C$0.50), then adjusts for news in the last 3 days and what the terminal's pre-market notes, weekly picks and meme reviews said. It runs inside `dashboard.py` and only recomputes when new daily prices or a new lab run arrive.

    ./venv/bin/python ideas.py    # print the current ranking

It's the evidence in one place, not a forecast: every signal lost money on average when held for one day; the tested edges played out over 5 to 20 days.

## Research universe

`universe.py` keeps every TSX Venture listing Yahoo has ever shown us (table `universe`), with 10 years of daily prices, even after a stock stops trading. It's for research only: never screened. First load: `run_research.sh` (runs in the background, resumable). Consolidations trigger a full reload of that stock's history.

## Data you can open

- 5-minute spreadsheets, one CSV per day: http://192.168.1.108:8080/stocks/intraday/
- Market benchmark: TSX Venture Composite (`^SPCDNX`) and TSX Composite (`^GSPTSE`), stored in `daily_prices` but not in the watchlist, so they're never screened. Live index level in `benchmark_quotes`.
- Database tables: `daily_prices`, `bars_5m` (every 5-minute bar, kept for good), `rising_log`, `watchlist`, `my_list`, `list_changes`, `terminal_backup`, `live_alerts`, `financings`

## One-off tools

    cd ~/stocks && . ./stocks.env
    ./venv/bin/python backtest.py                    # Flagged signal, past year
    ./venv/bin/python signal_lab.py --no-push        # all signals, print only
    ./venv/bin/python intraday_archive.py --backfill # re-save the ~60 days Yahoo keeps
