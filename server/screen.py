#!/usr/bin/env python3
"""
Daily screen: flags watchlist stocks showing signs of strength and writes a
short report you can read, or attach in Claude for analysis.

    python3 screen.py            # prints the report and saves reports/screen_YYYY-MM-DD.md

Signals per ticker (latest trading day):
  rel_vol     volume vs its 20-day average (2.0+ = unusual interest)
  pct_20d     price change over 20 trading days
  from_high   % below the 52-week closing high (near 0 = at a breakout)
  above_ma50  close above 50-day moving average
  vol_spike   volume more than 2x normal
  new_high    close at a new 52-week high
"""
import datetime as dt
import os
import sys
from pathlib import Path

import psycopg2

TOP_N = 25               # rows shown in the table
MIN_LIQUIDITY = 50_000   # avg daily shares needed to be flagged

SCREEN_SQL = """
WITH p AS (
  SELECT d.ticker, d.trade_date, d.close, d.volume,
    AVG(d.close)  OVER w50  AS ma50,
    AVG(d.volume) OVER w20  AS avg_vol20,
    MAX(d.close)  OVER w252 AS high_52w,
    LAG(d.close, 20) OVER (PARTITION BY d.ticker ORDER BY d.trade_date) AS close_20d_ago,
    ROW_NUMBER()  OVER (PARTITION BY d.ticker ORDER BY d.trade_date DESC) AS rn
  FROM daily_prices d
  JOIN watchlist w ON w.ticker = d.ticker AND w.active
  WINDOW
    w50  AS (PARTITION BY d.ticker ORDER BY d.trade_date ROWS 49 PRECEDING),
    w20  AS (PARTITION BY d.ticker ORDER BY d.trade_date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING),
    w252 AS (PARTITION BY d.ticker ORDER BY d.trade_date ROWS 251 PRECEDING)
)
SELECT
  ticker,
  trade_date,
  close,
  ROUND(volume / NULLIF(avg_vol20, 0), 1)                   AS rel_vol,
  ROUND(100 * (close / NULLIF(close_20d_ago, 0) - 1), 1)    AS pct_20d,
  ROUND(100 * (close / NULLIF(high_52w, 0) - 1), 1)         AS from_high,
  avg_vol20,
  close > ma50                                              AS above_ma50,
  volume > 2 * avg_vol20                                    AS vol_spike,
  close >= high_52w                                         AS new_high
FROM p
WHERE rn = 1
ORDER BY rel_vol DESC NULLS LAST;
"""


def fmt(v, suffix=""):
    return "-" if v is None else f"{v}{suffix}"


def main() -> int:
    conn = psycopg2.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(SCREEN_SQL)
            rows = cur.fetchall()
    finally:
        conn.close()

    today = dt.date.today().isoformat()
    lines = [f"# Stock screen {today}", ""]
    if not rows:
        lines.append("No data yet. Run load_history.py first.")
    else:
        yn = lambda b: "yes" if b else ""
        flagged = []
        for t, d, close, rv, p20, fh, avgv, ma, spike, nh in rows:
            score = sum(bool(x) for x in (ma, spike, nh))
            liquid = avgv is not None and avgv >= MIN_LIQUIDITY
            if score >= 2 and liquid:
                flagged.append(
                    f"- **{t}** ({score}/3): close {close:.3f}, rel vol {fmt(rv, 'x')}, "
                    f"20d {fmt(p20, '%')}, {fmt(fh, '%')} from 52w high"
                )

        lines += [f"Screened {len(rows)} stocks.", "", "## Flagged (2+ signals)", ""]
        lines += flagged or ["- Nothing flagged today."]
        lines += [
            "", f"## Top {TOP_N} by relative volume", "",
            "| Ticker | Date | Close | Rel vol | 20d % | From 52w high | >MA50 | Vol spike | New high |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for t, d, close, rv, p20, fh, avgv, ma, spike, nh in rows[:TOP_N]:
            lines.append(
                f"| {t} | {d} | {close:.3f} | {fmt(rv, 'x')} | {fmt(p20, '%')} | "
                f"{fmt(fh, '%')} | {yn(ma)} | {yn(spike)} | {yn(nh)} |"
            )

    report = "\n".join(lines) + "\n"
    print(report)

    out_dir = Path(os.environ.get("REPORT_DIR", Path(__file__).parent / "reports"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"screen_{today}.md").write_text(report)
    (out_dir / "latest.md").write_text(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
