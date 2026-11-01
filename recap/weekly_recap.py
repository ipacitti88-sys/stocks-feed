#!/usr/bin/env python3
"""Build the Weekly Market Recap page from a clone of ipacitti88-sys/stocks-feed.

Usage: python3 weekly_recap.py <path-to-stocks-feed-clone> <output.html>
Reads history/*.md, prices.json, signal_lab*.json, patterns*.json.
Writes ledger.json (every live signal as a paper trade) next to the output file.
Keep that ledger.json between runs (or commit it): old entries are kept and open ones updated.
"""
import json, re, sys, os, glob, html, math
from datetime import date, timedelta, datetime

repo, out = sys.argv[1], sys.argv[2]
J = lambda f: json.load(open(os.path.join(repo, f)))
E = html.escape

# ---------- history files ----------
files = sorted(glob.glob(os.path.join(repo, "history", "*.md")))
dated = [(date.fromisoformat(os.path.basename(f)[:10]), f) for f in files]
last_day = dated[-1][0]
week_start = last_day - timedelta(days=last_day.weekday())  # Monday
week = [(d, f) for d, f in dated if d >= week_start]
prior = [(d, f) for d, f in dated if d < week_start]

def parse(f):
    txt = open(f).read()
    out = {"markets": {}, "sections": {}}
    for m in re.finditer(r"^Market: (.+?) ([\d,\.]+) on [\d-]+ \(day ([-+\d\.]+)%, month ([-+\d\.]+)%, year ([-+\d\.]+)%, (\w+)\)", txt, re.M):
        out["markets"][m.group(1)] = dict(level=float(m.group(2).replace(",", "")), day=float(m.group(3)),
                                          month=float(m.group(4)), year=float(m.group(5)), trend=m.group(6))
    sec = None
    for line in txt.splitlines():
        h = re.match(r"^## (TSX Venture|TSX): (\w[\w ]*?)(?: at| in|\s*\(|$)", line)
        if h:
            sec = (h.group(1), h.group(2).strip()); out["sections"][sec] = []; continue
        it = re.match(r"^- ([A-Z0-9\.\-]+) \((.+?)\s*\):\s*(.*)", line)
        if it and sec:
            close = re.search(r"(?:close|at) ([\d\.]+)", it.group(3))
            out["sections"][sec].append(dict(t=it.group(1), n=it.group(2).strip(), close=float(close.group(1)) if close else None, raw=it.group(3)))
    return out

days = [(d, parse(f)) for d, f in week]
prev = parse(prior[-1][1]) if prior else None
prices = J("prices.json")

def last_close(t):
    s = prices.get(t); return s["closes"][-1] if s else None

def pct(a, b): return (b / a - 1) * 100 if a and b else None

# ---------- trading calendar, dated prices, fixed horizons ----------
H = (1, 5, 20)                          # follow-through horizons, in trading days
files_by_day = dict(dated)
parsed = {d: p for d, p in days}
def hist(d):
    if d not in parsed: parsed[d] = parse(files_by_day[d])
    return parsed[d]
# TSX holidays (the feed still writes files on some of them), plus any history day whose index
# levels repeat the previous file's exactly (market closed, nothing new).
TSX_HOLIDAYS = {date.fromisoformat(x) for x in (
    "2025-01-01 2025-02-17 2025-04-18 2025-05-19 2025-07-01 2025-08-04 2025-09-01 2025-10-13 2025-12-25 2025-12-26 "
    "2026-01-01 2026-02-16 2026-04-03 2026-05-18 2026-07-01 2026-08-03 2026-09-07 2026-10-12 2026-12-25 2026-12-28 "
    "2027-01-01 2027-02-15 2027-03-26 2027-05-24 2027-07-01 2027-08-02 2027-09-06 2027-10-11 2027-12-27 2027-12-28").split()}
_prev_lv = None
for d, _ in dated:
    lv = tuple(sorted((k, v["level"]) for k, v in hist(d)["markets"].items()))
    if lv and lv == _prev_lv: TSX_HOLIDAYS.add(d)
    _prev_lv = lv or _prev_lv
def trading(d): return d.weekday() < 5 and d not in TSX_HOLIDAYS
cal, _d = [], dated[0][0] - timedelta(days=400)   # every trading day, history or not
while _d <= last_day:
    if trading(_d): cal.append(_d)
    _d += timedelta(days=1)
cal_i = {d: i for i, d in enumerate(cal)}
INDEX = {"TSX": "S&P/TSX Composite", "TSX Venture": "S&P/TSX Venture Composite"}
def index_level(market, d):
    m = hist(d)["markets"].get(INDEX[market]) if d in files_by_day else None
    return m["level"] if m else None   # a trading day with no history file gives no index move

_px = {}
def px(t):
    """{date: (open, high, low, close)}. Uses prices.json "dates" when present; otherwise
    assumes the closes end on the last history day and line up with the history calendar."""
    if t in _px: return _px[t]
    s, m = prices.get(t), {}
    if s:
        n = len(s["closes"])
        if s.get("dates"): ds = [date.fromisoformat(str(x)[:10]) for x in s["dates"]]
        else:   # closes end on the stock's own "date" (some stop updating a day or two early)
            end = date.fromisoformat(str(s.get("date") or last_day)[:10])
            back = [c for c in cal if c <= end]; k = min(n, len(back)); ds = [None] * (n - k) + back[len(back) - k:]
        col = lambda key: s.get(key) or [None] * n
        for d, o, hi, lo, c in zip(ds, col("opens"), col("highs"), col("lows"), s["closes"]):
            if d: m[d] = (o, hi, lo, c)
    _px[t] = m; return m

def horizons(t, market, d, flag_close):
    """Entry at the next open when prices.json has opens (as in the backtest), else the flag-day close.
    Returns (entry, {h: (stock %, index %)} for each finished horizon, basis)."""
    i = cal_i.get(d)
    if i is None: return None, {}, None          # flagged on a weekend or holiday file: skip
    p = px(t)
    nxt = cal[i + 1] if i + 1 < len(cal) else None
    entry = p.get(nxt, (None,))[0] if nxt else None
    basis = "next open" if entry else "flag close"
    entry = entry or flag_close or p.get(d, (None,) * 4)[3]
    ix0, out = index_level(market, d), {}
    i0 = i + 1 if basis == "next open" else i     # count the hold from the entry day
    for h in H:
        if i0 + h >= len(cal): break
        dj = cal[i0 + h]
        out[h] = (pct(entry, p.get(dj, (None,) * 4)[3]), pct(ix0, index_level(market, dj)))
    return entry, out, basis

# ---------- markets ----------
MK = [("S&P/TSX Composite", "TSX"), ("S&P/TSX Venture Composite", "TSX Venture")]
markets = []
for full, short in MK:
    end = days[-1][1]["markets"].get(full)
    start = (prev or days[0][1])["markets"].get(full)
    series = ([prev["markets"][full]["level"]] if prev and full in prev["markets"] else []) + \
             [p["markets"][full]["level"] for _, p in days if full in p["markets"]]
    if end:
        markets.append(dict(name=short, end=end, week=pct(start["level"], end["level"]) if start else None, series=series))

# ---------- repeat names & signal follow-through ----------
def collect(market):
    seen = {}
    for d, p in days:
        for (mk, kind), items in p["sections"].items():
            if mk != market or kind not in ("breakouts", "flagged", "rising"): continue
            for it in items:
                r = seen.setdefault(it["t"], dict(t=it["t"], n=it["n"], kinds={}, first=None, first_close=None, days=set()))
                r["kinds"][kind] = r["kinds"].get(kind, 0) + 1
                r["days"].add(d)
                if r["first"] is None or d < r["first"]:
                    r["first"], r["first_close"] = d, it["close"]
    rows = []
    for r in seen.values():
        lc = last_close(r["t"])
        r["last"] = lc; r["chg"] = pct(r["first_close"], lc); r["ndays"] = len(r["days"])
        rows.append(r)
    rows.sort(key=lambda r: (-r["ndays"], -(r["kinds"].get("breakouts", 0)), -(r["chg"] or -99)))
    return rows

repeat = {m: collect(m) for m in ("TSX Venture", "TSX")}

def follow(market, kind):
    rows = []
    for d, p in days:
        if d == last_day: continue
        for it in p["sections"].get((market, kind), []):
            entry, hz, basis = horizons(it["t"], market, d, it["close"])
            lc = last_close(it["t"])
            rows.append(dict(d=d, t=it["t"], n=it["n"], entry=entry, basis=basis, last=lc, chg=pct(entry, lc),
                             ix=pct(index_level(market, d), index_level(market, last_day)), hz=hz))
    return rows

fb = {m: follow(m, "breakouts") for m in ("TSX Venture", "TSX")}
fr = {m: follow(m, "rising") for m in ("TSX Venture", "TSX")}

# ---------- signal lab ----------
def lab(f):
    d = J(f)
    L = next(l for l in d["liquidity"] if l["key"] in ("d50000",) or "C$50k" in l["label"])
    costs = {c["key"]: c for c in d["costs"]}
    sigs = []
    for s in L["signals"]:
        c = costs.get(s["key"], {})
        sigs.append(dict(key=s["key"], name=s["name"], grade=s["grade"], exit=s["exit"], avg=s["avg"], h1=s["h1"], h2=s["h2"],
                         won=s["won"], n=s["n"], real=c.get("avg_est")))
    liq = []
    for l in d["liquidity"]:
        b = next((s for s in l["signals"] if s["key"] == "breakout"), None)
        if b: liq.append(dict(label=l["label"], per_day=l.get("stocks_per_day"), avg=b["avg"], h1=b["h1"], h2=b["h2"], grade=b["grade"], n=b["n"]))
    rising = d.get("rising", {})
    return dict(sigs=sigs, liq=liq, rising=rising, market=d["market"], today=d.get("today", []), stops=d.get("stops", {}))

labs = {"TSX Venture": lab("signal_lab.json"), "TSX": lab("signal_lab_tsx.json")}
pats = {"TSX Venture": J("patterns.json"), "TSX": J("patterns_tsx.json")}
sectors = {m: next((f for f in p["findings"] if f["key"].startswith("sectors")), None) or next(f for f in p["findings"] if f["title"].startswith("Sectors")) for m, p in pats.items()}

# ---------- per-stock spread ----------
def tick(p): return 0.005 if p < 0.5 else 0.01      # TSX/TSXV price step
TODAY = {it["ticker"]: it for m in labs for it in labs[m]["today"]}
def spread_est(t, price=None):
    """Round-trip bid/ask spread as % of price, and its source:
    'feed' (signal_lab today[].spread_pct), 'high/low' (Corwin-Schultz over 20 days), or 'floor' (one tick)."""
    it = TODAY.get(t, {})
    if it.get("spread_pct") is not None: return float(it["spread_pct"]), "feed"
    rows = [v for _, v in sorted(px(t).items())][-21:]
    price = price or (rows[-1][3] if rows else None)
    floor = tick(price) / price * 100 if price else None
    est, k = [], 3 - 2 * math.sqrt(2)
    for a, b in zip(rows, rows[1:]):
        h0, l0, h1, l1 = a[1], a[2], b[1], b[2]
        if not (h0 and l0 and h1 and l1): continue
        beta = math.log(h0 / l0) ** 2 + math.log(h1 / l1) ** 2
        gamma = math.log(max(h0, h1) / min(l0, l1)) ** 2
        al = (math.sqrt(2 * beta) - math.sqrt(beta)) / k - math.sqrt(gamma / k)
        est.append(max(0.0, 2 * (math.exp(al) - 1) / (1 + math.exp(al))))
    if len(est) >= 10: return max(sum(est) / len(est) * 100, floor or 0), "high/low"
    return floor, "floor"

# ---------- paper-trade ledger ----------
KIND = (("washout", "washout"), ("breakout", "breakout"), ("flag", "flagged"), ("rising", "rising"),
        ("gap", "gap_hold"), ("volume", "volume_up"), ("pullback", "pullback"))
def sig_key(kind):   # "top by relative volume" / "top gainers" are rankings, not signals
    k = kind.lower()
    return None if k.startswith("top") else next((v for s, v in KIND if s in k), None)
ledger_path = os.path.join(os.path.dirname(os.path.abspath(out)), "ledger.json")
try: ledger = {e["id"]: e for e in json.load(open(ledger_path))}
except (OSError, ValueError): ledger = {}
for d, _ in dated:
    for (mk, kind), items in hist(d)["sections"].items():
        sk = sig_key(kind)
        if not sk: continue
        for it in items:
            eid = f"{d}|{mk}|{sk}|{it['t']}"
            old = ledger.get(eid)
            if old and old["ret"].get(str(H[-1])) is not None: continue   # closed trade, leave as recorded
            entry, hz, basis = horizons(it["t"], mk, d, it["close"])
            if not entry: continue
            sp, src = (old["spread"], old["spread_src"]) if old else spread_est(it["t"], entry)
            ledger[eid] = dict(id=eid, date=d.isoformat(), market=mk, signal=sk, ticker=it["t"], name=it["n"],
                               entry=round(entry, 4), basis=basis, spread=sp and round(sp, 3), spread_src=src,
                               ret={str(h): v[0] and round(v[0], 3) for h, v in hz.items()},
                               idx={str(h): v[1] and round(v[1], 3) for h, v in hz.items()})
json.dump(sorted(ledger.values(), key=lambda e: e["id"]), open(ledger_path, "w"), indent=1)

def mean(xs): xs = [x for x in xs if x is not None]; return sum(xs) / len(xs) if xs else None
def live_stats(m):
    bt = {s["key"]: s for s in labs[m]["sigs"]}
    groups = {}
    for e in ledger.values():
        if e["market"] == m: groups.setdefault(e["signal"], []).append(e)
    out = []
    for sk, es in groups.items():
        done = [e for e in es if e["ret"].get("20") is not None]
        net = [e["ret"]["20"] - (e["spread"] or 0) for e in done]
        d5 = [e for e in es if e["ret"].get("5") is not None and e["idx"].get("5") is not None]
        out.append(dict(key=sk, name=bt.get(sk, {}).get("name", sk.replace("_", " ").title()), n=len(es), done=len(done),
                        edge5=mean(e["ret"]["5"] - e["idx"]["5"] for e in d5), n5=len(d5), net20=mean(net),
                        won=round(100 * sum(x > 0 for x in net) / len(net)) if net else None, bt=bt.get(sk, {}).get("real")))
    return sorted(out, key=lambda r: -r["n"])
live = {m: live_stats(m) for m in ("TSX Venture", "TSX")}

# ---------- takeaways (rules-based, TSX first) ----------
take = []
v = next((m for m in markets if m["name"] == "TSX Venture"), None)
t = next((m for m in markets if m["name"] == "TSX"), None)
def art(w): return "an" if w[0] in "aeiou" else "a"
def moved(x): return f"{'fell' if (x or 0) < 0 else 'rose'} {abs(x or 0):.1f}%"
if t:
    take.append(f"The TSX {moved(t['week'])} on the week and is in {art(t['end']['trend'])} {t['end']['trend']} ({t['end']['year']:+.1f}% on the year).")
rows = [r for r in fb["TSX"] if r["hz"].get(1, (None,))[0] is not None]
if rows:
    up = sum(1 for r in rows if r["hz"][1][0] > 0)
    a, b = mean(r["hz"][1][0] for r in rows), mean(r["hz"][1][1] for r in rows)
    take.append(f"TSX breakouts one day after the flag: {up} of {len(rows)} higher, averaging {a:+.1f}%"
                + (f" vs {b:+.1f}% for the TSX." if b is not None else "."))
lv = [r for r in live["TSX"] if r["done"] >= 5 and r["net20"] is not None]
if lv:
    take.append("Live TSX record at 20 days, after each stock's spread: " + "; ".join(
        f"{r['name']} {r['net20']:+.1f}% over {r['done']} trades" + (f" (backtest {r['bt']:+.1f}%)" if r["bt"] is not None else "") for r in lv[:3]) + ".")
top = [r for r in repeat["TSX"] if r["ndays"] >= 2]
if top:
    take.append("TSX names on the lists 2+ days this week: " + ", ".join(f"{r['t']} ({r['chg']:+.0f}%)" if r["chg"] is not None else r["t"] for r in top[:6]) + ".")
good = [s for s in labs["TSX"]["sigs"] if s["grade"] == "Promising"]
best = max(labs["TSX"]["sigs"], key=lambda s: s["real"] if s["real"] is not None else -99)
take.append(f"TSX signal lab: {len(good)} of {len(labs['TSX']['sigs'])} signals graded Promising. Strongest after real spreads: {best['name']} ({best['real']:+.1f}% per trade over 20 days).")
sec = sectors["TSX"]
if sec:
    rs = sorted(sec["rows"], key=lambda r: r[2])
    take.append(f"TSX sectors over the past month: strongest {rs[-1][0]} ({rs[-1][2]:+.0f}%), weakest {rs[0][0]} ({rs[0][2]:+.0f}%).")
if v:
    take.append(f"TSX Venture {moved(v['week'])} and is in {art(v['end']['trend'])} {v['end']['trend']}; its signals still lose money after spreads.")

# ---------- HTML helpers ----------
def f_pct(x, d=1):
    if x is None: return '<span class="mute">–</span>'
    cls = "up" if x > 0 else "dn" if x < 0 else ""
    return f'<span class="{cls}">{x:+.{d}f}%</span>'

def spark(series):
    if len(series) < 2: return ""
    w, h, p = 160, 40, 4
    lo, hi = min(series), max(series); rng = (hi - lo) or 1
    pts = [(p + i * (w - 2 * p) / (len(series) - 1), h - p - (s - lo) / rng * (h - 2 * p)) for i, s in enumerate(series)]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"{pts[0][0]:.1f},{h-p} " + path + f" {pts[-1][0]:.1f},{h-p}"
    cls = "up" if series[-1] >= series[0] else "dn"
    return (f'<svg class="spark {cls}" viewBox="0 0 {w} {h}" role="img" aria-label="Index over the week">'
            f'<polygon points="{area}" class="area"/><polyline points="{path}" class="line"/>'
            f'<circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="2.6" class="dot"/></svg>')

GRADE = {"Promising": "good", "Inconsistent": "warn", "Beats the market, still loses": "warn", "No edge": "bad"}
def pill(g): return f'<span class="pill {GRADE.get(g, "")}">{E(g)}</span>'

def table(head, rows, cls=""):
    th = "".join(f"<th>{h}</th>" for h in head)
    tr = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="tw"><table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>'

def tk(t, n): return f'<b class="tk">{E(t)}</b> <span class="nm">{E(n.title())}</span>'
def dshort(d): return d.strftime("%a")

# ---------- sections ----------
mk_html = ""
for m in markets:
    e = m["end"]
    mk_html += f'''<div class="mk"><div class="mk-h"><span class="lbl">{E(m["name"])}</span><span class="trend {'good' if e['trend']=='uptrend' else 'bad'}">{E(e["trend"])}</span></div>
<div class="mk-v"><span class="lvl">{e["level"]:,.2f}</span>{spark(m["series"])}</div>
<dl class="mk-s"><div><dt>Week</dt><dd>{f_pct(m["week"])}</dd></div><div><dt>Month</dt><dd>{f_pct(e["month"])}</dd></div><div><dt>Year</dt><dd>{f_pct(e["year"])}</dd></div></dl></div>'''

def repeat_rows(m, k=12):
    rows = []
    for r in repeat[m][:k]:
        k = r["kinds"]
        tags = "".join(f'<span class="tag {c}">{lbl}{"×"+str(k[c]) if k[c]>1 else ""}</span>' for c, lbl in (("breakouts", "Breakout"), ("flagged", "Flagged"), ("rising", "Rising")) if c in k)
        rows.append([tk(r["t"], r["n"]), f'<span class="num">{r["ndays"]}</span>', tags, f'<span class="num">{dshort(r["first"])} {r["first_close"]}</span>' if r["first_close"] else "–", f'<span class="num">{r["last"]}</span>' if r["last"] else "–", f_pct(r["chg"])])
    return rows

def fol_rows(rows):
    def h(r, k): return f_pct(r["hz"][k][0]) if k in r["hz"] else '<span class="mute">not yet</span>'
    def edge(r): return f_pct(r["chg"] - r["ix"]) if r["chg"] is not None and r["ix"] is not None else f_pct(None)
    return [[f'<span class="num">{dshort(r["d"])}</span>', tk(r["t"], r["n"]), f'<span class="num">{r["entry"]:g}</span>' if r["entry"] else "–",
             h(r, 1), h(r, 5), f_pct(r["chg"]), edge(r)] for r in rows]

def live_rows(m):
    return [[E(r["name"]), f'<span class="num">{r["n"]}</span>', f'<span class="num">{r["done"]}</span>',
             f_pct(r["edge5"], 2), f_pct(r["net20"], 2), f_pct(r["bt"], 2),
             f'<span class="num">{r["won"]}%</span>' if r["won"] is not None else f_pct(None)] for r in live[m]]
LIVE_HEAD = ["Signal", "Flags", "20 days done", "5-day vs index", "20-day after spread", "Backtest 20-day", "Won"]

def lab_rows(m):
    return [[E(s["name"]), pill(s["grade"]), f'<span class="sub">{E(s["exit"])}</span>', f_pct(s["avg"], 2), f_pct(s["real"], 2), f_pct(s["h1"], 2), f_pct(s["h2"], 2), f'<span class="num">{s["won"]}%</span>', f'<span class="num">{s["n"]:,}</span>'] for s in labs[m]["sigs"]]

def liq_rows():
    return [[E(l["label"]), f'<span class="num">{l["per_day"]}</span>', f_pct(l["avg"], 2), f_pct(l["h1"], 2), f_pct(l["h2"], 2), pill(l["grade"]), f'<span class="num">{l["n"]:,}</span>'] for l in labs["TSX Venture"]["liq"]]

def rising_rows():
    out = []
    for m in ("TSX Venture", "TSX"):
        r = labs[m]["rising"]
        for e in r.get("exits", []):
            out.append([E(m), E(e["exit"]), f'<span class="num">{e["n"]:,}</span>', f_pct(e["avg"], 2), f_pct(e.get("base"), 2), f'<span class="num">{e["won"]}%</span>'])
    return out

def sector_rows(m):
    s = sectors[m]
    if not s: return []
    return [[E(r[0]), f'<span class="num">{r[1]}</span>', f_pct(r[2]), f_pct(r[3]), f_pct(r[4]), f'<span class="num">{r[5] if r[5] is not None else "–"}</span>'] for r in s["rows"]]

pv = pats["TSX Venture"]
up10 = next(f for f in pv["findings"] if f["key"].startswith("after_a_10%+_up"))
pat_rows = [[E(r[0]), f'<span class="num">{r[1]:,}</span>', f_pct(r[4], 2), E(r[5]), f_pct(r[6], 2), E(r[7])] for r in up10["rows"]]


SIGNAME = {s["key"]: s for s in labs["TSX"]["sigs"]}
def setup_rows():
    order = {"washout": 0, "gap_hold": 1, "breakout": 2, "volume_up": 3, "flagged": 4, "pullback": 5}
    out = []
    for it in sorted(labs["TSX"]["today"], key=lambda x: order.get(x["signal"], 9)):
        sg = SIGNAME.get(it["signal"], {})
        c = it["close"]; sp, src = spread_est(it["ticker"], c)
        # Swap the scorecard's generic tick cost (one step + 0.5%) for this stock's own spread.
        est = sg["avg"] + tick(c) / c * 100 + 0.5 - sp if sg.get("avg") is not None and sp is not None and src != "floor" else None
        sp_cell = f_pct(None) if sp is None else f'<span class="num">{"≥" if src == "floor" else ""}{sp:.1f}%</span>'
        warn = ' <span class="pill bad">Spread eats edge</span>' if est is not None and est <= 0 else ""
        out.append([tk(it["ticker"], it["name"]), E(sg.get("name", it["signal"])), pill(sg.get("grade", "")), f'<span class="num">{c}</span>',
                    sp_cell, f_pct(sg.get("real"), 2), f_pct(est, 2) + warn])
    return out

def stop_rows():
    st = labs["TSX"]["stops"]
    res = st.get("bands", {}).get("all", {}).get("results", {})
    labels = {r["key"]: r["label"] for r in st.get("rules", [])}
    keys = ["none", "fixed0.15", "fixed0.2", "fixed0.25", "trail0.25", "atr3"]
    sigs = [k for k in ("washout", "gap_hold", "breakout", "flagged") if k in res]
    head = ["Exit rule"] + [E(SIGNAME[k]["name"]) for k in sigs]
    rows = [[E(labels.get(r, r))] + [f_pct(res[k][r]["avg"], 2) for k in sigs] for r in keys]
    return head, rows

def lab_rows_short(m):
    return [[E(s["name"]), pill(s["grade"]), f_pct(s["avg"], 2), f_pct(s["real"], 2)] for s in labs[m]["sigs"]]

pt = pats["TSX"]
def pat_row(key, label):
    f = next(f for f in pt["findings"] if f["key"] == key)
    return [[E(label), E(r[0]), f'<span class="num">{r[1]:,}</span>', f_pct(r[4], 2), E(r[5]), f_pct(r[6], 2), E(r[7])] for r in f["rows"]]
pat_rows = pat_row("after_a_10%+_down_day", "10%+ down day") + pat_row("volume_spike_on_a_down_day", "Heavy-volume down day") + pat_row("after_a_10%+_up_day", "10%+ up day")
wk_label = f'{days[0][0].strftime("%b %-d")} – {last_day.strftime("%b %-d, %Y")}'
gen = datetime.now().strftime("%b %-d, %Y")

page = f'''<title>Weekly Market Recap</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@75..100,500..800&family=Source+Sans+3:wght@400;600&family=JetBrains+Mono:wght@400;600&display=swap">
<style>
/* Layout: one reading column of stacked report sections; dense tables scroll inside their own frames. */
:root {{
  --bg: #f5f6f8; --surface: #ffffff; --fg: #17202b; --muted: #5d6877; --line: #dde2e9;
  --accent: #1d4f8f; --up: #137a4a; --dn: #b42d2d; --warn: #9a6400;
  --up-bg: #e3f3ea; --dn-bg: #fae6e5; --warn-bg: #fbf0d9; --chip: #eaeef4;
  --display: "Archivo", "Arial Narrow", system-ui, sans-serif;
  --body: "Source Sans 3", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "JetBrains Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #10151c; --surface: #171e27; --fg: #e4e9f0; --muted: #96a2b2; --line: #2a3441;
  --accent: #79a8e8; --up: #4fc48a; --dn: #f07a72; --warn: #e2b155;
  --up-bg: #15301f; --dn-bg: #3a1c1b; --warn-bg: #3a2e14; --chip: #222b37; color-scheme: dark; }} }}
:root[data-theme="dark"] {{
  --bg: #10151c; --surface: #171e27; --fg: #e4e9f0; --muted: #96a2b2; --line: #2a3441;
  --accent: #79a8e8; --up: #4fc48a; --dn: #f07a72; --warn: #e2b155;
  --up-bg: #15301f; --dn-bg: #3a1c1b; --warn-bg: #3a2e14; --chip: #222b37; color-scheme: dark; }}
body {{ background: var(--bg); color: var(--fg); font: 16px/1.5 var(--body); }}
.wrap {{ max-width: 1040px; margin: 0 auto; padding-block: 28px 64px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: grid; gap: 40px; }}
header {{ display: grid; gap: 6px; border-bottom: 2px solid var(--fg); padding-bottom: 14px; }}
.eyebrow {{ font: 600 12px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; color: var(--accent); }}
h1 {{ font: 800 clamp(32px, 6vw, 52px)/1 var(--display); font-stretch: 78%; margin: 0; letter-spacing: -.01em; text-wrap: balance; }}
.meta {{ color: var(--muted); font-size: 14px; }}
h2 {{ font: 700 24px/1.15 var(--display); font-stretch: 85%; margin: 0; text-wrap: balance; }}
h3 {{ font: 700 15px/1.2 var(--display); text-transform: uppercase; letter-spacing: .05em; margin: 0; color: var(--muted); }}
section {{ display: grid; gap: 14px; min-width: 0; }}
.lede {{ color: var(--muted); max-width: 68ch; margin: 0; }}
.mks {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }}
.mk {{ background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 16px; display: grid; gap: 10px; }}
.mk-h {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; }}
.lbl {{ font: 700 14px/1 var(--display); text-transform: uppercase; letter-spacing: .06em; }}
.mk-v {{ display: flex; justify-content: space-between; align-items: end; gap: 12px; }}
.lvl {{ font: 600 30px/1 var(--mono); font-variant-numeric: tabular-nums; }}
.spark {{ width: 160px; max-width: 45%; height: 40px; }}
.spark .line {{ fill: none; stroke-width: 2; }} .spark .area {{ opacity: .14; }}
.spark.up .line {{ stroke: var(--up); }} .spark.up .area, .spark.up .dot {{ fill: var(--up); }}
.spark.dn .line {{ stroke: var(--dn); }} .spark.dn .area, .spark.dn .dot {{ fill: var(--dn); }}
.mk-s {{ display: flex; gap: 22px; margin: 0; }} .mk-s dt {{ font-size: 12px; color: var(--muted); }} .mk-s dd {{ margin: 0; font: 600 15px var(--mono); }}
.trend, .pill {{ font: 600 12px/1 var(--body); padding: 4px 8px; border-radius: 99px; white-space: nowrap; background: var(--chip); color: var(--muted); }}
.good {{ background: var(--up-bg); color: var(--up); }} .bad {{ background: var(--dn-bg); color: var(--dn); }} .warn {{ background: var(--warn-bg); color: var(--warn); }}
ol.take {{ margin: 0; padding: 0; list-style: none; display: grid; gap: 10px; counter-reset: t; }}
ol.take li {{ background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 12px 14px 12px 46px; position: relative; max-width: 80ch; }}
ol.take li::before {{ counter-increment: t; content: counter(t); position: absolute; left: 14px; top: 12px; font: 600 14px var(--mono); color: var(--accent); }}
.tw {{ overflow-x: auto; background: var(--surface); border: 1px solid var(--line); border-radius: 6px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
th {{ text-align: left; font: 600 11px/1.2 var(--body); text-transform: uppercase; letter-spacing: .06em; color: var(--muted); padding: 10px 12px; border-bottom: 1px solid var(--line); white-space: nowrap; background: var(--bg); }}
td {{ padding: 9px 12px; border-bottom: 1px solid var(--line); vertical-align: middle; white-space: nowrap; }}
tr:last-child td {{ border-bottom: 0; }}
td, .num {{ font-variant-numeric: tabular-nums; }}
.num, td > .up, td > .dn {{ font-family: var(--mono); font-size: 13px; }}
.up {{ color: var(--up); }} .dn {{ color: var(--dn); }} .mute, .sub {{ color: var(--muted); }} .sub {{ font-size: 13px; }}
.tk {{ font: 600 13px var(--mono); }} .nm {{ color: var(--muted); font-size: 13px; }}
.tag {{ display: inline-block; font: 600 11px/1 var(--body); padding: 3px 6px; border-radius: 4px; margin-right: 4px; background: var(--chip); }}
.tag.breakouts {{ background: var(--up-bg); color: var(--up); }} .tag.rising {{ background: var(--warn-bg); color: var(--warn); }}
.two {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }} .two > div {{ display: grid; gap: 8px; min-width: 0; align-content: start; }}
.venture {{ border-top: 2px solid var(--line); padding-top: 24px; }}
.note {{ font-size: 13px; color: var(--muted); margin: 0; max-width: 80ch; }}
footer {{ font-size: 13px; color: var(--muted); border-top: 1px solid var(--line); padding-top: 14px; }}
</style>
<div class="wrap">
<header><span class="eyebrow">stocks-feed · weekly recap · TSX focus</span><h1>Week of {wk_label}</h1>
<span class="meta">TSX first, with the Venture in brief at the end. $0.10–$5.00, C$50k/day liquidity. Built {gen} from the feed and this week's Signal Lab run.</span></header>

<section><h2>Markets</h2><div class="mks">{mk_html}</div></section>

<section><h2>What stood out</h2><ol class="take">{"".join(f"<li>{E(x)}</li>" for x in take)}</ol></section>

<section><h2>TSX setups at the last close</h2>
<p class="lede">Stocks showing one of your signals as of {last_day.strftime("%A, %b %-d")}, strongest setup first. "Signal avg" is the signal's 10-year 20-day average after real spreads. "This stock" swaps in this stock's own spread.</p>
{table(["Stock","Signal","Grade","Close","Spread","Signal avg","This stock"], setup_rows()) + '<p class="note">Spread is the round-trip bid/ask as a share of price. A "≥" value is only the one-tick minimum (no high/low data), so "This stock" is left blank. "Spread eats edge" means this stock\'s spread alone would wipe out the signal\'s average.</p>' if labs["TSX"]["today"] else '<p class="note">No TSX signals at the last close.</p>'}</section>

<section><h2>TSX names that kept showing up</h2>
<p class="lede">Every TSX stock that hit the breakout, flagged or rising lists this week, ranked by days on the lists. Change is from the close on its first appearance to the latest close.</p>
{table(["Stock","Days","Lists","First","Last","Change"], repeat_rows("TSX"))}</section>

<section><h2>Did this week's TSX breakouts hold?</h2>
<p class="lede">Measured from entry at fixed 1- and 5-day marks so early- and late-week flags compare fairly. "vs TSX" is the change to date minus the index over the same days.</p>
{table(["Day","Stock","Entry","1 day","5 days","To date","vs TSX"], fol_rows(fb["TSX"])) if fb["TSX"] else '<p class="note">No TSX breakouts before the last day.</p>'}</section>

<section><h2>TSX signal scorecard</h2>
<p class="lede">Ten years of history at your C$50k/day rule, bought at the next open. "Tick cost" assumes one price step plus 0.5%. "Real spread" uses each stock's estimated bid/ask. Half 1 and half 2 split the decade in two.</p>
{table(["Signal","Grade","Best exit","Tick cost","Real spread","Half 1","Half 2","Won","Trades"], lab_rows("TSX"))}</section>

<section><h2>Live TSX signals vs the backtest</h2>
<p class="lede">Every TSX flag in the feed, logged as a paper trade. "20-day after spread" subtracts each stock's estimated spread, so it lines up with the backtest's real-spread number.</p>
{table(LIVE_HEAD, live_rows("TSX")) if live["TSX"] else '<p class="note">No live TSX flags logged yet.</p>'}
<p class="note">A signal needs a few dozen finished trades before its live number means much. Entry is the next open when the feed has opens, otherwise the flag-day close.</p></section>

<section><h2>TSX exits: does a stop help?</h2>
<p class="lede">Average result per trade over a 20-day hold, after real spreads, under different stop rules.</p>
{table(*stop_rows())}
<p class="note">On the TSX, tight stops cut results for every signal. Wide stops (25%) cost little; anything tighter mostly sells near the low before the rebound.</p></section>

<section><h2>TSX patterns after big moves</h2>
<p class="lede">How TSX stocks did after sharp moves, compared with the average stock in the same price band, over ten years.</p>
{table(["Event","Price band","Events","20 days vs avg","Years same direction","20-day after costs","Verdict"], pat_rows)}</section>

<section><h2>TSX sectors right now</h2>
{table(["Sector","Stocks","1 mo","3 mo","1 yr","Move together"], sector_rows("TSX"))}
<p class="note">Sectors are sorted from company names, so treat them as approximate. "Move together" near 0 means each stock trades on its own news; 0.4 or more means the group moves as one.</p></section>

<section><h2>Live "rising" list record</h2>
<p class="lede">Real intraday flags since the list went live, compared with buying any liquid stock at the same time.</p>
{table(["Market","Exit","Signals","Avg","Any stock","Won"], rising_rows())}</section>

<section class="venture"><h2>TSX Venture in brief</h2>
<div class="two"><div><h3>Names on the lists most often</h3>{table(["Stock","Days","Lists","First","Last","Change"], repeat_rows("TSX Venture", 6))}</div>
<div><h3>Signals at C$50k/day</h3>{table(["Signal","Grade","Tick cost","Real spread"], lab_rows_short("TSX Venture"))}</div></div>
<h3>Live Venture signals vs the backtest</h3>{table(LIVE_HEAD, live_rows("TSX Venture")) if live["TSX Venture"] else '<p class="note">No live Venture flags logged yet.</p>'}
<h3>Venture breakouts by liquidity cutoff</h3>{table(["Liquidity rule","Stocks/day","Avg","Half 1","Half 2","Grade","Trades"], liq_rows())}</section>

<footer>Data: ipacitti88-sys/stocks-feed (Yahoo, about 15 minutes delayed). Backtests describe the past and are not a forecast. Not financial advice.</footer>
</div>'''

open(out, "w").write(page)
print("wrote", out, "week", wk_label)
