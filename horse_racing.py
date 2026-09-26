#!/usr/bin/env python3
"""
UK/Ireland Horse Racing Predictor
Same architecture as the rest of this suite, built on horse-racing.p.rapidapi.com
(confirmed live via manual testing -- real racecards, real horses/jockeys/
trainers, real form strings, and real multi-bookmaker odds with timestamps).

DESIGN HONESTY NOTE: this does NOT attempt an independent win-probability
model from form alone -- with typically 3-10 races of per-horse history and
no backtested calibration, that would be overconfident. Instead:
  1. Market Consensus -- de-vigs (removes bookmaker margin from) the real
     odds already fetched, same math as Match IQ's own edge calculator.
     This IS a legitimate probability estimate, since it's just accurately
     relaying professional market assessment, not inventing one.
  2. Recent Form -- a raw, recency-weighted form screen (same Hot-Form
     philosophy as every other tool in this suite) -- NOT a probability,
     just "has this horse actually been finishing well lately".
  3. Value Flag -- horses whose recent-form rank sits notably ahead of
     their market-rank -- a discrepancy-spotter, explicitly NOT validated
     against real outcomes yet. Treat as a lead worth checking, not a bet.

Setup:
    pip3 install requests --break-system-packages
    python3 horse_racing.py

Output:
    docs/horse-racing/index.html, docs/horse-racing/horse_racing_predictions.csv
"""

import os
import sys
import csv
import json
import time
from datetime import date, datetime
import requests

BASE = "https://horse-racing.p.rapidapi.com"
REQUEST_DELAY = 0.6
RECENT_FORM_RUNS = 6  # how many of a horse's most recent runs feed the form score
MIN_RUNNERS = 3


def _headers():
    key = os.environ.get("RAPIDAPI_KEY")
    if not key:
        print("[!] RAPIDAPI_KEY not set -- nothing will work without it.")
    return {"x-rapidapi-host": "horse-racing.p.rapidapi.com", "x-rapidapi-key": key or ""}


def _get(path, params=None):
    try:
        r = requests.get(f"{BASE}{path}", headers=_headers(), params=params or {}, timeout=20)
        time.sleep(REQUEST_DELAY)
        if r.status_code != 200:
            print(f"  [!] {r.status_code} on {path}")
            return None
        return r.json()
    except Exception as e:
        print(f"  [!] request failed: {path} ({e})")
        return None


def get_racecards(target_date):
    return _get("/racecards", params={"date": target_date.isoformat()}) or []


def get_race_detail(id_race):
    return _get(f"/race/{id_race}")


# ---------------------------------------------------------------------
# Form parsing -- standard racing convention: a form string reads
# oldest-to-newest LEFT TO RIGHT, so the LAST character is the most
# recent run. '/' and '-' are season-break separators, not results, and
# are skipped. Digits 1-9 = actual finishing position; '0' = finished
# 10th or worse. Letters (F/U/R/P/PU/BD etc) mean the horse didn't
# complete the race (fell, unseated, refused, pulled up, brought down)
# -- treated as the worst outcome, same as finishing unplaced.
# ---------------------------------------------------------------------

def parse_form(form_str):
    if not form_str:
        return []
    scores = []
    for ch in form_str:
        if ch in "/-":
            continue
        if ch.isdigit():
            pos = 10 if ch == "0" else int(ch)
        else:
            pos = 99  # DNF (fell/unseated/refused/pulled up/brought down)
        score = 1.0 / pos if pos <= 10 else 0.0
        scores.append(score)
    return scores


def recency_weighted_form(scores):
    """scores is oldest-first (see parse_form) -- weight the LAST
    (most recent) entries highest, same decaying-weight pattern used
    for every other recency-weighted stat in this suite."""
    if not scores:
        return None
    recent = scores[-RECENT_FORM_RUNS:]
    n = len(recent)
    wts = [1.3 ** i for i in range(n)]
    return round(sum(w * s for w, s in zip(wts, recent)) / sum(wts), 4)


def devig_race_odds(horses):
    """For each bookmaker present, computes implied probabilities
    (1/decimal_odds) for every horse THAT bookie quotes, normalizes by
    that bookie's own overround so its probabilities sum to 1 (removes
    the margin), then averages the de-vigged probability across all
    bookies for a genuine market-consensus win probability per horse.
    Same de-vig math already used in Match IQ's edge calculator."""
    bookie_odds = {}
    for h in horses:
        for o in h.get("odds", []) or []:
            try:
                odd = float(o["odd"])
            except (KeyError, TypeError, ValueError):
                continue
            if odd <= 1:
                continue
            bookie_odds.setdefault(o["bookie"], {})[h["id_horse"]] = odd

    bookie_probs = {}
    for bookie, odds_map in bookie_odds.items():
        raw_probs = {hid: 1.0 / odd for hid, odd in odds_map.items()}
        overround = sum(raw_probs.values())
        if overround <= 0:
            continue
        bookie_probs[bookie] = {hid: p / overround for hid, p in raw_probs.items()}

    consensus = {}
    for h in horses:
        hid = h["id_horse"]
        vals = [bp[hid] for bp in bookie_probs.values() if hid in bp]
        consensus[hid] = round(sum(vals) / len(vals), 4) if vals else None
    return consensus


def build_predictions(target_date=None):
    target_date = target_date or date.today()
    print(f"Fetching racecards for {target_date.isoformat()}...")
    racecards = get_racecards(target_date)
    print(f"{len(racecards)} races found")

    predictions = []
    for rc in racecards:
        if rc.get("finished") == "1" or rc.get("canceled") == "1":
            continue
        print(f"  Fetching detail: {rc.get('course')} {rc.get('date', '')[11:16]} — {rc.get('title')}")
        detail = get_race_detail(rc["id_race"])
        if not detail:
            continue
        horses = [h for h in (detail.get("horses") or []) if h.get("non_runner") != "1"]
        if len(horses) < MIN_RUNNERS:
            continue

        market_probs = devig_race_odds(horses)

        runners = []
        for h in horses:
            form_scores = parse_form(h.get("form", ""))
            form_val = recency_weighted_form(form_scores)
            odds_vals = [float(o["odd"]) for o in (h.get("odds") or []) if o.get("odd")]
            runners.append({
                "name": h.get("horse"), "id_horse": h.get("id_horse"),
                "jockey": h.get("jockey"), "trainer": h.get("trainer"),
                "number": h.get("number"), "weight": h.get("weight"),
                "form_raw": h.get("form", ""), "form_score": form_val,
                "market_prob": market_probs.get(h.get("id_horse")),
                "best_odds": max(odds_vals) if odds_vals else None,
            })

        # Rank within THIS race by form and by market, to power the value flag
        by_form = sorted([r for r in runners if r["form_score"] is not None],
                          key=lambda r: -r["form_score"])
        for i, r in enumerate(by_form):
            r["form_rank"] = i + 1
        by_market = sorted([r for r in runners if r["market_prob"] is not None],
                            key=lambda r: -r["market_prob"])
        for i, r in enumerate(by_market):
            r["market_rank"] = i + 1

        predictions.append({
            "id_race": rc["id_race"], "course": rc.get("course"), "date": rc.get("date"),
            "title": rc.get("title"), "distance": rc.get("distance"), "going": rc.get("going"),
            "prize": rc.get("prize"), "runners": runners,
        })
    return predictions


def build_legs(predictions):
    """Market Favorite legs -- the top de-vigged-probability horse per
    race, shown with its real best-available odds. Legitimate because
    it's accurately relaying real market data, not inventing a model."""
    legs = []
    for p in predictions:
        with_prob = [r for r in p["runners"] if r.get("market_prob") is not None]
        if not with_prob:
            continue
        fav = max(with_prob, key=lambda r: r["market_prob"])
        legs.append({
            "match": f"{p['course']} {p['date'][11:16]}", "subject": fav["name"],
            "market": f"{fav['name']} to Win",
            "prob": round(fav["market_prob"] * 100),
            "category": "Market Favorite",
            "detail": f"{p['title']} · jockey {fav.get('jockey')} · best odds {fav.get('best_odds')}",
            "history": None, "hit_rate": None,
        })
    return legs


def build_form_entries(predictions):
    """Raw Hot Form screen -- horses whose recency-weighted form score
    ranks near the top of their field. NOT a probability."""
    entries = []
    for p in predictions:
        for r in p["runners"]:
            if r.get("form_score") is None or r.get("form_rank") is None:
                continue
            if r["form_rank"] <= 2 and r["form_score"] >= 0.4:
                entries.append({
                    "name": r["name"], "course": p["course"], "date": p["date"],
                    "title": p["title"], "form_raw": r["form_raw"], "form_score": r["form_score"],
                    "form_rank": r["form_rank"], "field_size": len(p["runners"]),
                })
    entries.sort(key=lambda e: -e["form_score"])
    return entries


def build_value_entries(predictions):
    """Discrepancy-spotter: horses whose FORM rank is notably better
    than their MARKET rank -- i.e. recent form looks stronger than the
    price suggests. Explicitly unvalidated -- a lead to check, not a
    calibrated edge."""
    entries = []
    for p in predictions:
        for r in p["runners"]:
            fr, mr = r.get("form_rank"), r.get("market_rank")
            if fr is None or mr is None:
                continue
            if fr <= 2 and mr - fr >= 2:  # form top-2, but market ranks it meaningfully lower
                entries.append({
                    "name": r["name"], "course": p["course"], "date": p["date"], "title": p["title"],
                    "form_rank": fr, "market_rank": mr, "form_raw": r["form_raw"],
                    "best_odds": r.get("best_odds"), "field_size": len(p["runners"]),
                })
    entries.sort(key=lambda e: e["form_rank"])
    return entries


BUILDER_TEMPLATE = """
<div style="background:#14261a;border-radius:12px;padding:16px;margin:12px 0;border:1px solid #2a4a34">
  <div style="font-size:14px;font-weight:bold;margin-bottom:10px">🎯 Market Favorites Builder</div>
  <div style="font-size:11px;color:#8ba;margin-bottom:10px">
    Each leg is the genuine de-vigged (margin-removed) market-consensus favorite for that race --
    this is real, accurately-relayed market data, not an independent prediction. Odds move right up
    to post time, so treat "best odds" as a snapshot, not guaranteed.
  </div>
  <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
    <label style="font-size:12px;color:#8ba">Min probability:</label>
    <input id="minProb" type="number" step="1" min="0" max="100" value="30"
      style="width:60px;background:#0a1710;border:1px solid #2a4a34;color:white;border-radius:6px;padding:6px 8px;font-size:13px">
    <button onclick="buildFavorites()"
      style="background:#2a7d4a;border:none;color:white;padding:7px 14px;border-radius:6px;font-size:13px;cursor:pointer">
      Build
    </button>
  </div>
  <div id="builderResult" style="font-size:12px;color:#8ba"></div>
</div>
<script>
const LEGS = {legs_json};
function buildFavorites() {{
  const minProb = parseInt(document.getElementById('minProb').value) || 0;
  const chosen = LEGS.filter(l => l.prob >= minProb).sort((a,b) => b.prob - a.prob);
  const el = document.getElementById('builderResult');
  if (!chosen.length) {{ el.innerHTML = 'No favorites clear that probability threshold today.'; return; }}
  const rows = chosen.map(l =>
    `<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #2a4a34">
       <span>${{l.match}}<br><span style="color:#7ed3a8">${{l.market}}</span>
       ${{l.detail ? `<br><span style="color:#668;font-size:10px">${{l.detail}}</span>` : ''}}</span>
       <span style="color:#ffeb3b;font-weight:bold">${{l.prob}}%</span>
     </div>`
  ).join('');
  el.innerHTML = `<div style="color:white;font-size:13px;margin-bottom:6px">${{chosen.length}} favorite(s)</div>${{rows}}`;
}}
buildFavorites();
</script>
"""

FORM_PANEL_TEMPLATE = """<div style="background:#1a1310;border-radius:12px;padding:16px;margin:12px 0;border:1px solid #3a2a20">
  <div style="font-size:14px;font-weight:bold;margin-bottom:6px">📊 Recent Form Leaders</div>
  <div style="font-size:11px;color:#998;margin-bottom:10px">
    Raw recency-weighted form screen -- ranks 1st or 2nd in their own field by recent finishing
    positions. NOT a probability and NOT market-adjusted -- a horse can show up here and still be a
    long shot if the market disagrees. Cross-check against the Market Favorites panel above.
  </div>
  {rows}
</div>"""

FORM_ROW = """<div style="display:flex;justify-content:space-between;font-size:12px;padding:6px 0;border-top:1px solid #3a2a20">
  <div><b>{name}</b><br><span style="color:#998">{course} {time} · {title}</span></div>
  <div style="text-align:right"><span style="color:#7dd3a8;font-weight:bold">form {form_raw}</span><br><span style="color:#998">rank {form_rank}/{field_size}</span></div>
</div>"""

VALUE_PANEL_TEMPLATE = """<div style="background:#1a1310;border-radius:12px;padding:16px;margin:12px 0;border:1px solid #3a2a20">
  <div style="font-size:14px;font-weight:bold;margin-bottom:6px">🔎 Form/Market Gap</div>
  <div style="font-size:11px;color:#998;margin-bottom:10px">
    Horses whose recent-FORM rank sits notably ahead of where the MARKET currently prices them.
    This is a discrepancy-spotter, not a validated edge -- there's no backtested track record yet
    for whether this gap actually predicts anything. Treat as a lead worth a closer look, not a bet
    on its own.
  </div>
  {rows}
</div>"""

VALUE_ROW = """<div style="display:flex;justify-content:space-between;font-size:12px;padding:6px 0;border-top:1px solid #3a2a20">
  <div><b>{name}</b><br><span style="color:#998">{course} {time} · {title}</span></div>
  <div style="text-align:right"><span style="color:#ff9a2e;font-weight:bold">form #{form_rank} · market #{market_rank}</span><br><span style="color:#998">best odds {best_odds}</span></div>
</div>"""

HTML_TEMPLATE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>UK/Ireland Horse Racing</title></head>
<body style="background:#0f0a08;color:#e8dcd0;font-family:Arial;padding:12px;max-width:600px;margin:auto">
<h2 style="text-align:center;color:#7dd3a8">🐎 UK/Ireland Racing</h2>
<p style="text-align:center;color:#998;font-size:11px">De-vigged market consensus + recency-weighted form · {generated}</p>
<p style="text-align:center;margin-bottom:16px"><a href="horse_racing_predictions.csv" download style="background:#2a201a;border:1px solid #443;color:white;padding:8px 14px;border-radius:8px;text-decoration:none;font-size:13px">⬇ Download CSV</a></p>

<div style="background:#14261a;border-radius:12px;padding:16px;margin:12px 0;border:1px solid #2a4a34;font-size:12px;line-height:1.6">
  <div style="font-size:14px;font-weight:bold;margin-bottom:8px">📖 Reading the numbers</div>
  <div style="margin-bottom:8px"><b style="color:#ffeb3b">Percentage (%)</b> — real market-consensus win probability, after removing all bookmakers' profit margin. Not a guess — the underlying math forces these to sum to 100% across a race.</div>
  <div style="margin-bottom:8px"><b style="color:#7dd3a8">Form (e.g. "4113")</b> — the horse's own finishing positions, read left→right, <u>oldest to newest</u>. So "4113" means: 4th, then 1st, then 1st, then 3rd <i>most recently</i>. "0" means finished 10th or worse; a letter (F/U/P/R) means the horse didn't complete that race.</div>
  <div style="margin-bottom:8px"><b>Rank (e.g. "1/6")</b> — this horse's recency-weighted form ranks #1 out of 6 runners in its own race. Recent runs count more than older ones.</div>
  <div><b style="color:#ff9a2e">Best odds</b> — the highest decimal odds seen across bookmakers at last fetch. Stake × odds = total payout. Odds move right up to post time — treat this as a snapshot, not a locked-in price.</div>
  <div style="margin-top:10px;padding-top:10px;border-top:1px solid #2a4a34;color:#8ba">
    <b>Where to focus:</b> Market %'s are the closest thing here to an actual calibrated prediction. Form
    and the Form/Market Gap below are raw, unvalidated screens with no track record yet — treat them as
    context, not a basis for picks, until a results tracker exists for this project. The strongest signal
    right now is a horse appearing in <b>both</b> the Market Favorites and Recent Form panels at once.
  </div>
</div>

{builder}
{form_panel}
{value_panel}
{cards}
<div style="font-size:11px;color:#998;text-align:center;margin-top:20px;line-height:1.6">
  Official Rating, owner, sire, and dam weren't available for any race checked so far -- possibly a
  free-tier limit, possibly normal for early-season juvenile races. Market Consensus is real,
  accurately de-vigged market data. Recent Form and the Form/Market Gap are raw screens with no
  backtested track record yet -- treat them as leads, not predictions.
</div>
</body></html>"""

RACE_CARD_TEMPLATE = """<div style="background:#1a1310;border-radius:12px;padding:16px;margin:12px 0;border:1px solid #3a2a20">
  <div style="font-size:11px;color:#998;margin-bottom:4px">{course} {time} · {going} · {distance}</div>
  <div style="font-size:15px;font-weight:bold;margin-bottom:10px">{title}</div>
  {runner_rows}
</div>"""

RUNNER_ROW = """<div style="display:flex;justify-content:space-between;font-size:12px;padding:5px 0;border-top:1px solid #3a2a20">
  <div>{number}. <b>{name}</b><br><span style="color:#998">{jockey} · {trainer}</span></div>
  <div style="text-align:right"><span style="color:#ffeb3b;font-weight:bold">{market_prob_str}</span><br><span style="color:#998">form {form_raw} · odds {best_odds}</span></div>
</div>"""


def make_html(predictions):
    cards = ""
    for p in predictions:
        runner_rows = "".join(RUNNER_ROW.format(
            number=r.get("number") or "-", name=r["name"], jockey=r.get("jockey") or "?",
            trainer=r.get("trainer") or "?",
            market_prob_str=f"{r['market_prob']*100:.0f}%" if r.get("market_prob") is not None else "-",
            form_raw=r.get("form_raw") or "-", best_odds=r.get("best_odds") or "-",
        ) for r in sorted(p["runners"], key=lambda r: r.get("number") or 99))
        cards += RACE_CARD_TEMPLATE.format(
            course=p["course"], time=p["date"][11:16], going=p.get("going") or "",
            distance=p.get("distance") or "", title=p["title"], runner_rows=runner_rows,
        )
    if not cards:
        cards = '<p style="text-align:center;color:#998">No usable races today.</p>'

    legs = build_legs(predictions)
    builder = BUILDER_TEMPLATE.format(legs_json=json.dumps(legs)) if legs else ""

    form_entries = build_form_entries(predictions)
    form_rows = "".join(FORM_ROW.format(
        name=e["name"], course=e["course"], time=e["date"][11:16], title=e["title"],
        form_raw=e["form_raw"], form_rank=e["form_rank"], field_size=e["field_size"],
    ) for e in form_entries)
    form_panel = FORM_PANEL_TEMPLATE.format(rows=form_rows) if form_entries else ""

    value_entries = build_value_entries(predictions)
    value_rows = "".join(VALUE_ROW.format(
        name=e["name"], course=e["course"], time=e["date"][11:16], title=e["title"],
        form_rank=e["form_rank"], market_rank=e["market_rank"], best_odds=e.get("best_odds") or "-",
    ) for e in value_entries)
    value_panel = VALUE_PANEL_TEMPLATE.format(rows=value_rows) if value_entries else ""

    return HTML_TEMPLATE.format(
        generated=datetime.now().strftime('%d %b %H:%M'),
        builder=builder, form_panel=form_panel, value_panel=value_panel, cards=cards,
    )


def write_csv(predictions, path):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", "Course", "Race", "Horse", "Number", "Jockey", "Trainer",
                          "Form", "FormScore", "FormRank", "MarketProb", "MarketRank", "BestOdds"])
        for p in predictions:
            for r in p["runners"]:
                writer.writerow([
                    p["date"], p["course"], p["title"], r["name"], r.get("number"),
                    r.get("jockey"), r.get("trainer"), r.get("form_raw"), r.get("form_score"),
                    r.get("form_rank"), r.get("market_prob"), r.get("market_rank"), r.get("best_odds"),
                ])


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = date.fromisoformat(sys.argv[1])
    else:
        target = date.today()

    predictions = build_predictions(target)
    os.makedirs("docs/horse-racing", exist_ok=True)
    with open("docs/horse-racing/index.html", "w") as f:
        f.write(make_html(predictions))
    write_csv(predictions, "docs/horse-racing/horse_racing_predictions.csv")
    with open("docs/horse-racing/horse_racing.json", "w") as f:
        json.dump(predictions, f, indent=2, default=str)
    print(f"\nDone — {len(predictions)} races processed.")
