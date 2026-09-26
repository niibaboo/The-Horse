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


# ---------------------------------------------------------------------
# Each-way place terms -- standard UK bookmaker defaults by field size
# and race type. NOTE: individual bookmakers sometimes run promotional
# "extra places" offers (e.g. 4 places instead of 3 for a big handicap)
# -- those aren't knowable from this API and are NOT reflected here.
# Treat this as the standard baseline terms, not a guarantee of what
# every specific bookmaker is currently offering.
# ---------------------------------------------------------------------

def is_jumps_race(title):
    """No explicit flat/jumps field exists in this API's response --
    inferred from the race title instead, which is standard racing
    terminology (Hurdle/Chase/Hunt/N.H. always indicate National Hunt).
    Imperfect but reasonable given what's actually available."""
    t = (title or "").upper()
    return any(kw in t for kw in ["HURDLE", "CHASE", "N.H.", "NATIONAL HUNT", "I.N.H."])


def place_terms(field_size, jumps):
    """Returns (place_count, place_fraction) -- standard UK each-way
    terms. Handicap vs non-handicap isn't reliably determinable from
    this API either (class/title text is inconsistent), so this uses
    the more common non-handicap-style terms as the default."""
    if field_size < 5:
        return 0, None  # win-only, no each-way place part at all
    if field_size <= 7:
        return 2, 0.25
    if field_size <= 15:
        return 3, 0.25 if not jumps else 0.20
    return 4, 0.25


def harville_place_probabilities(win_probs, place_count):
    """Harville (1973) formula -- estimates the probability each horse
    finishes in the top `place_count` positions, extended from win
    probabilities alone. Works by modeling the race as a sequence of
    "who wins from the remaining field" draws: P(2nd) sums over who
    could have won instead, weighted by their win prob and this
    horse's renormalized share of what's left; P(3rd) extends the same
    idea one level deeper, and so on.

    KNOWN LIMITATION (well documented in racing literature, not unique
    to this implementation): Harville's formula has a mild bias for
    long-shots (tends to overestimate outsiders' place chances) and
    favorites (mild underestimate) in large fields -- it's a genuine,
    established method, not a guess, but still an approximation, not
    a perfect model.
    """
    ids = list(win_probs.keys())
    p1 = dict(win_probs)
    place_prob = {hid: p1.get(hid, 0.0) for hid in ids}  # start with P(1st)

    if place_count <= 1:
        return place_prob

    # P(2nd): sum over each possible winner j != i of
    #   P(j wins) * P(i wins the remaining field, excluding j)
    p2 = {hid: 0.0 for hid in ids}
    for j in ids:
        remaining = 1.0 - p1.get(j, 0.0)
        if remaining <= 1e-9:
            continue
        for i in ids:
            if i == j:
                continue
            p2[i] += p1.get(j, 0.0) * (p1.get(i, 0.0) / remaining)
    for hid in ids:
        place_prob[hid] += p2[hid]

    if place_count <= 2:
        return place_prob

    # P(3rd): sum over each possible 1st (j) and 2nd (k), j != k != i,
    # of P(j wins) * P(k wins remaining after j) * P(i wins remaining after j,k)
    p3 = {hid: 0.0 for hid in ids}
    for j in ids:
        pj = p1.get(j, 0.0)
        rem_j = 1.0 - pj
        if rem_j <= 1e-9:
            continue
        for k in ids:
            if k == j:
                continue
            pk_given_j = p1.get(k, 0.0) / rem_j
            rem_jk = rem_j - p1.get(k, 0.0)
            if rem_jk <= 1e-9:
                continue
            for i in ids:
                if i == j or i == k:
                    continue
                pi_given_jk = p1.get(i, 0.0) / rem_jk
                p3[i] += pj * pk_given_j * pi_given_jk
    for hid in ids:
        place_prob[hid] += p3[hid]

    if place_count <= 3:
        return place_prob

    # P(4th) -- same idea, one level deeper. Only computed for the
    # rare 16+ runner races that pay 4 places -- O(n^4), still fast
    # even for a 20-runner field.
    p4 = {hid: 0.0 for hid in ids}
    for j in ids:
        pj = p1.get(j, 0.0)
        rem_j = 1.0 - pj
        if rem_j <= 1e-9:
            continue
        for k in ids:
            if k == j:
                continue
            pk = p1.get(k, 0.0) / rem_j
            rem_jk = rem_j - p1.get(k, 0.0)
            if rem_jk <= 1e-9:
                continue
            for l in ids:
                if l == j or l == k:
                    continue
                pl = p1.get(l, 0.0) / rem_jk
                rem_jkl = rem_jk - p1.get(l, 0.0)
                if rem_jkl <= 1e-9:
                    continue
                for i in ids:
                    if i in (j, k, l):
                        continue
                    pi = p1.get(i, 0.0) / rem_jkl
                    p4[i] += pj * pk * pl * pi
    for hid in ids:
        place_prob[hid] += p4[hid]

    return place_prob


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

        with_market_prob = {hid: p for hid, p in market_probs.items() if p is not None}
        jumps = is_jumps_race(rc.get("title"))
        place_count, place_fraction = place_terms(len(horses), jumps)
        place_probs = (harville_place_probabilities(with_market_prob, place_count)
                       if place_count > 0 and with_market_prob else {})

        runners = []
        for h in horses:
            form_scores = parse_form(h.get("form", ""))
            form_val = recency_weighted_form(form_scores)
            odds_vals = [float(o["odd"]) for o in (h.get("odds") or []) if o.get("odd")]
            best_odds = max(odds_vals) if odds_vals else None
            hid = h.get("id_horse")
            place_prob = place_probs.get(hid)
            place_odds = (1 + (best_odds - 1) * place_fraction) if (best_odds and place_fraction) else None
            runners.append({
                "name": h.get("horse"), "id_horse": hid,
                "jockey": h.get("jockey"), "trainer": h.get("trainer"),
                "number": h.get("number"), "weight": h.get("weight"),
                "form_raw": h.get("form", ""), "form_score": form_val,
                "market_prob": market_probs.get(hid),
                "best_odds": best_odds,
                "place_prob": round(place_prob, 4) if place_prob is not None else None,
                "place_odds": round(place_odds, 2) if place_odds is not None else None,
                "place_count": place_count, "place_fraction": place_fraction,
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

        # ---------------------------------------------------------------
        # Projected Finishing Order (top 1-4) -- a pre-race ranking of the
        # most-likely finishing order, not a post-race result and not a
        # horse's own historical best.
        #
        # WHY THIS IS JUST market_prob, SORTED: the "proper" way to build a
        # single most-likely running order is sequential elimination -- pick
        # the most-likely winner, remove it, pick the most-likely 2nd from
        # what's left (renormalizing the remaining horses' probabilities by
        # dividing by 1 - p(winner)), remove it, and so on for 3rd/4th. That
        # renormalization divides every REMAINING horse's probability by the
        # same constant, which cannot change their relative order. So the
        # single most-likely running order is mathematically identical to
        # just sorting every horse by its own win probability once -- this
        # is exactly what by_market already is. No new/separate calculation
        # needed, and no new probability invented -- projected_order reuses
        # the same de-vigged market_prob already computed above, just
        # exposed as an explicit ranked list capped at the top 4.
        # ---------------------------------------------------------------
        projected_order = [
            {"rank": i + 1, "name": r["name"], "number": r.get("number"),
             "market_prob": r["market_prob"]}
            for i, r in enumerate(by_market[:4])
        ]

        predictions.append({
            "id_race": rc["id_race"], "course": rc.get("course"), "date": rc.get("date"),
            "title": rc.get("title"), "distance": rc.get("distance"), "going": rc.get("going"),
            "prize": rc.get("prize"), "runners": runners,
            "jumps": jumps, "place_count": place_count, "place_fraction": place_fraction,
            "projected_order": projected_order,
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


def build_ew_entries(predictions):
    """Each-way picks -- horses with a strong Harville-derived PLACE
    probability, shown alongside real place odds (best win odds scaled
    by the race's standard each-way fraction). This is where E/W value
    is more likely to actually exist: a horse doesn't need to be the
    outright favorite to have a strong place chance, and medium-priced
    horses in bigger fields are exactly where the place portion of an
    each-way bet does real work. Sorted by place probability, not win
    probability -- deliberately a different ranking than Market
    Favorites above."""
    entries = []
    for p in predictions:
        if not p.get("place_count"):
            continue  # win-only race (fewer than 5 runners), no E/W terms exist
        for r in p["runners"]:
            if r.get("place_prob") is None or r.get("place_odds") is None:
                continue
            entries.append({
                "name": r["name"], "course": p["course"], "date": p["date"], "title": p["title"],
                "jockey": r.get("jockey"), "win_prob": r.get("market_prob"),
                "place_prob": r["place_prob"], "place_odds": r["place_odds"],
                "place_count": p["place_count"],
                "place_fraction_str": f"1/{int(1/p['place_fraction'])}" if p.get("place_fraction") else "",
                "field_size": len(p["runners"]), "jumps": p.get("jumps"),
            })
    entries.sort(key=lambda e: -e["place_prob"])
    return entries


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

EW_PANEL_TEMPLATE = """<div style="background:#1a1310;border-radius:12px;padding:16px;margin:12px 0;border:1px solid #3a2a20">
  <div style="font-size:14px;font-weight:bold;margin-bottom:6px">🎯 Each-Way Picks</div>
  <div style="font-size:11px;color:#998;margin-bottom:10px">
    Ranked by PLACE probability (via Harville's formula, 1973 -- an established method that extends
    win probabilities into place chances, not an invented one), not win probability -- this is
    deliberately a different list from Market Favorites above. Place terms shown are the STANDARD UK
    default for that field size; individual bookmakers sometimes run promotional extra-places offers
    this can't see. Harville's formula is a genuine approximation with a known mild bias for long-shots
    in big fields -- treat as a strong lead, not a certainty, especially with no backtested track
    record yet for this project.
  </div>
  {rows}
</div>"""

EW_ROW = """<div style="display:flex;justify-content:space-between;font-size:12px;padding:6px 0;border-top:1px solid #3a2a20">
  <div><b>{name}</b><br><span style="color:#998">{course} {time} · {title}</span><br><span style="color:#998">{jockey} · {field_size} runners{jumps_tag}</span></div>
  <div style="text-align:right"><span style="color:#7dd3a8;font-weight:bold">place {place_prob_str}</span><br><span style="color:#998">win {win_prob_str} · place odds {place_odds} ({place_fraction_str}, top {place_count})</span></div>
</div>"""

HTML_TEMPLATE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>UK/Ireland Horse Racing</title></head>
<body style="background:#0f0a08;color:#e8dcd0;font-family:Arial;padding:12px;max-width:600px;margin:auto">
<h2 style="text-align:center;color:#7dd3a8">🐎 UK/Ireland Racing</h2>
<p style="text-align:center;color:#998;font-size:11px">De-vigged market consensus + recency-weighted form · {generated}</p>
<p style="text-align:center;margin-bottom:16px"><a href="horse_racing_predictions.csv" download style="background:#2a201a;border:1px solid #443;color:white;padding:8px 14px;border-radius:8px;text-decoration:none;font-size:13px">⬇ Download CSV</a></p>

<div style="background:#14261a;border-radius:12px;padding:16px;margin:12px 0;border:1px solid #2a4a34;font-size:12px;line-height:1.6">
  <div style="font-size:14px;font-weight:bold;margin-bottom:8px">📖 Reading the numbers</div>
  <div style="margin-bottom:8px"><b style="color:#ffeb3b">Win % / Place %</b> — real market-consensus win probability (margin removed, sums to 100% per race), and a Harville-derived estimate of finishing in the paid places. Two different questions: win % asks "will it come 1st", place % asks "will it hit the frame at all".</div>
  <div style="margin-bottom:8px"><b style="color:#7dd3a8">Form (e.g. "4113")</b> — the horse's own finishing positions, read left→right, <u>oldest to newest</u>. So "4113" means: 4th, then 1st, then 1st, then 3rd <i>most recently</i>. "0" means finished 10th or worse; a letter (F/U/P/R) means the horse didn't complete that race.</div>
  <div style="margin-bottom:8px"><b>Rank (e.g. "1/6")</b> — this horse's recency-weighted form ranks #1 out of 6 runners in its own race. Recent runs count more than older ones.</div>
  <div style="margin-bottom:8px"><b style="color:#ff9a2e">Best odds / Place odds</b> — best odds is the highest decimal WIN price seen across bookmakers at last fetch. Place odds is that price scaled down to the standard each-way place fraction (1/4 or 1/5) for this field size. Stake × odds = total payout either way. Odds move right up to post time.</div>
  <div><b>Place terms (e.g. "1/4, top 3")</b> — standard UK each-way terms for this field size: place odds fraction, and how many finishing positions actually get paid. Races under 5 runners are win-only, no each-way part exists.</div>
  <div style="margin-bottom:8px"><b style="color:#7dd3a8">🏁 Projected order</b> — each race card's top-4 pre-race ranking, most likely winner through 4th, by de-vigged market win probability. This is the same win % already shown per horse, just laid out as an ordered list — not a separate prediction, and not a guess at who "should" finish where based on a horse's own past best.</div>
  <div style="margin-top:10px;padding-top:10px;border-top:1px solid #2a4a34;color:#8ba">
    <b>Where to focus:</b> Market Win %'s are the closest thing here to an actual calibrated prediction
    for the WIN market. For each-way specifically, the Each-Way Picks panel (ranked by place chance, not
    win chance) is the right one to check -- short-priced favorites are usually the WEAKEST each-way
    value, since their place chance adds little on top of an already-high win chance. Form and the
    Form/Market Gap are raw, unvalidated screens with no track record yet. The strongest signal right
    now is a horse appearing in <b>both</b> Market Favorites and Recent Form at once.
  </div>
</div>

{builder}
{ew_panel}
{form_panel}
{value_panel}
{cards}
<div style="font-size:11px;color:#998;text-align:center;margin-top:20px;line-height:1.6">
  Official Rating, owner, sire, and dam weren't available for any race checked so far -- possibly a
  free-tier limit, possibly normal for early-season juvenile races. Market Consensus is real,
  accurately de-vigged market data. Place probabilities use Harville's formula (1973), an established
  method with a known mild long-shot bias in large fields. Recent Form and the Form/Market Gap are raw
  screens with no backtested track record yet -- treat them as leads, not predictions.
</div>
</body></html>"""

RACE_CARD_TEMPLATE = """<div style="background:#1a1310;border-radius:12px;padding:16px;margin:12px 0;border:1px solid #3a2a20">
  <div style="font-size:11px;color:#998;margin-bottom:4px">{course} {time} · {going} · {distance}{place_terms_str}</div>
  <div style="font-size:15px;font-weight:bold;margin-bottom:10px">{title}</div>
  {projected_order_html}
  {runner_rows}
</div>"""

PROJECTED_ORDER_TEMPLATE = """<div style="background:#0f1a12;border:1px solid #2a4a34;border-radius:8px;padding:8px 10px;margin-bottom:10px;font-size:12px">
  <span style="color:#7dd3a8;font-weight:bold">🏁 Projected order </span><span style="color:#998">(by win %, before the race)</span><br>
  {picks}
</div>"""

RUNNER_ROW = """<div style="display:flex;justify-content:space-between;font-size:12px;padding:5px 0;border-top:1px solid #3a2a20">
  <div>{number}. <b>{name}</b><br><span style="color:#998">{jockey} · {trainer}</span></div>
  <div style="text-align:right"><span style="color:#ffeb3b;font-weight:bold">win {market_prob_str}</span> <span style="color:#7dd3a8;font-weight:bold">place {place_prob_str}</span><br><span style="color:#998">form {form_raw} · odds {best_odds}/{place_odds}</span></div>
</div>"""


def make_html(predictions):
    cards = ""
    for p in predictions:
        runner_rows = "".join(RUNNER_ROW.format(
            number=r.get("number") or "-", name=r["name"], jockey=r.get("jockey") or "?",
            trainer=r.get("trainer") or "?",
            market_prob_str=f"{r['market_prob']*100:.0f}%" if r.get("market_prob") is not None else "-",
            place_prob_str=f"{r['place_prob']*100:.0f}%" if r.get("place_prob") is not None else "-",
            form_raw=r.get("form_raw") or "-", best_odds=r.get("best_odds") or "-",
            place_odds=r.get("place_odds") or "-",
        ) for r in sorted(p["runners"], key=lambda r: r.get("number") or 99))
        place_terms_str = (f" · E/W {int(1/p['place_fraction'])}, top {p['place_count']}"
                            if p.get("place_count") else " · win only (< 5 runners)")

        proj = p.get("projected_order") or []
        if proj:
            picks_str = " &nbsp;·&nbsp; ".join(
                f"<b>{pk['rank']}.</b> {pk['name']} ({pk['market_prob']*100:.0f}%)" for pk in proj
            )
            projected_order_html = PROJECTED_ORDER_TEMPLATE.format(picks=picks_str)
        else:
            projected_order_html = ""

        cards += RACE_CARD_TEMPLATE.format(
            course=p["course"], time=p["date"][11:16], going=p.get("going") or "",
            distance=p.get("distance") or "", title=p["title"], runner_rows=runner_rows,
            place_terms_str=place_terms_str, projected_order_html=projected_order_html,
        )
    if not cards:
        cards = '<p style="text-align:center;color:#998">No usable races today.</p>'

    legs = build_legs(predictions)
    builder = BUILDER_TEMPLATE.format(legs_json=json.dumps(legs)) if legs else ""

    ew_entries = build_ew_entries(predictions)[:15]  # top 15 by place probability, keeps the page manageable
    ew_rows = "".join(EW_ROW.format(
        name=e["name"], course=e["course"], time=e["date"][11:16], title=e["title"],
        jockey=e.get("jockey") or "?", field_size=e["field_size"],
        jumps_tag=" · jumps" if e.get("jumps") else "",
        place_prob_str=f"{e['place_prob']*100:.0f}%",
        win_prob_str=f"{e['win_prob']*100:.0f}%" if e.get("win_prob") is not None else "-",
        place_odds=e["place_odds"], place_fraction_str=e["place_fraction_str"], place_count=e["place_count"],
    ) for e in ew_entries)
    ew_panel = EW_PANEL_TEMPLATE.format(rows=ew_rows) if ew_entries else ""

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
        builder=builder, ew_panel=ew_panel, form_panel=form_panel, value_panel=value_panel, cards=cards,
    )


def write_csv(predictions, path):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", "Course", "Race", "Horse", "Number", "Jockey", "Trainer",
                          "Form", "FormScore", "FormRank", "MarketProb", "MarketRank", "BestOdds",
                          "PlaceProb", "PlaceOdds", "PlaceCount", "PlaceFraction"])
        for p in predictions:
            for r in p["runners"]:
                writer.writerow([
                    p["date"], p["course"], p["title"], r["name"], r.get("number"),
                    r.get("jockey"), r.get("trainer"), r.get("form_raw"), r.get("form_score"),
                    r.get("form_rank"), r.get("market_prob"), r.get("market_rank"), r.get("best_odds"),
                    r.get("place_prob"), r.get("place_odds"), r.get("place_count"), r.get("place_fraction"),
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
