#!/usr/bin/env python3
"""
Debug script for The Racing API's /v1/racecards/free endpoint -- checks
whether it's genuinely usable for building a horse racing model: real
UK/Ireland racecards, per-horse odds/form/jockey/trainer data, and how
today's actual response is shaped (so nothing downstream gets built on
guessed field names).

Reads RAPIDAPI_KEY from the environment -- designed to run via GitHub
Actions, same pattern as check_iberian_leagues.py earlier.
"""

import os
import sys
import json
import requests

BASE = "https://the-racing-api1.p.rapidapi.com"


def main():
    key = os.environ.get("RAPIDAPI_KEY") or (sys.argv[1] if len(sys.argv) > 1 else None)
    if not key:
        print("Set RAPIDAPI_KEY or pass it as an argument.")
        sys.exit(1)

    headers = {
        "Content-Type": "application/json",
        "x-rapidapi-host": "the-racing-api1.p.rapidapi.com",
        "x-rapidapi-key": key,
    }
    params = {"day": "today", "region_codes": '["gb", "ire"]'}

    print("Fetching /v1/racecards/free ...")
    try:
        r = requests.get(f"{BASE}/v1/racecards/free", headers=headers, params=params, timeout=20)
    except Exception as e:
        print(f"[!] request failed: {e}")
        sys.exit(1)

    print(f"Status code: {r.status_code}")
    if r.status_code != 200:
        print("Response body (may explain the error):")
        print(r.text[:1000])
        sys.exit(1)

    try:
        data = r.json()
    except Exception as e:
        print(f"[!] response wasn't valid JSON: {e}")
        print(r.text[:1000])
        sys.exit(1)

    print("\nTop-level keys:", list(data.keys()) if isinstance(data, dict) else f"(response is a {type(data).__name__}, not a dict)")

    # Try the most likely shapes for where the actual racecards live
    racecards = None
    if isinstance(data, dict):
        for key_name in ("racecards", "data", "results", "cards"):
            if key_name in data and isinstance(data[key_name], list):
                racecards = data[key_name]
                print(f"\nFound racecards under key: '{key_name}' ({len(racecards)} entries)")
                break
    elif isinstance(data, list):
        racecards = data
        print(f"\nResponse is a bare list ({len(racecards)} entries)")

    if not racecards:
        print("\n[!] couldn't find an obvious racecards list. Full raw response (first 2000 chars):")
        print(json.dumps(data, indent=2)[:2000])
        return

    print("\n" + "=" * 70)
    print("FIRST RACECARD -- full structure:")
    print("=" * 70)
    first = racecards[0]
    print(json.dumps(first, indent=2)[:3000])

    # Try to find the runners/horses list within the first racecard
    runners = None
    if isinstance(first, dict):
        for key_name in ("runners", "horses", "entries"):
            if key_name in first and isinstance(first[key_name], list):
                runners = first[key_name]
                print(f"\n\nFound runners under key: '{key_name}' ({len(runners)} runners)")
                break

    if runners:
        print("\n" + "=" * 70)
        print("FIRST RUNNER -- full structure (this is what tells us if odds/form/jockey/trainer are real):")
        print("=" * 70)
        print(json.dumps(runners[0], indent=2)[:2000])

        print("\n\nSpecifically checking for odds/form/jockey/trainer fields in this runner:")
        r0 = runners[0]
        for field in ["odds", "sp", "price", "form", "jockey", "trainer", "weight", "draw", "rating", "or"]:
            if field in r0:
                print(f"  -> FOUND: {field} = {r0[field]}")
    else:
        print("\n[!] couldn't find a runners/horses list inside the first racecard -- "
              "see the full structure printed above to find the real key name.")

    print(f"\n\nTotal racecards returned today: {len(racecards)}")


if __name__ == "__main__":
    main()
