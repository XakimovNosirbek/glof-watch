#!/usr/bin/env python3
"""Download the OpenStreetMap layers used by the Uzbekistan study (Overpass API, cached as JSON).

  places.json  settlements (place=city|town|village|hamlet) as nodes, in the study box
  rivers.json  named rivers (waterway=river) with geometry, in the study box
  lakes.json   named water bodies (natural=water), as centre points, to put names on inventory lakes

Overpass rejects requests without a User-Agent (HTTP 406) and is often busy (502/504), so each query is retried on two
public endpoints. Usage: uz_osm.py --out data/osm
"""
import argparse
import json
import os
import time

import requests

ENDPOINTS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
             "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
UA = {"User-Agent": "glof-watch/0.1 (glacial lake research script)"}
BOX = "37.0,65.5,42.8,73.5"            # S,W,N,E: Uzbekistan's mountain fringe and the Kyrgyz/Tajik headwaters
QUERIES = {
    "places.json": f'[out:json][timeout:180];node["place"~"^(city|town|village|hamlet)$"]({BOX});out body;',
    "rivers.json": f'[out:json][timeout:180];way["waterway"="river"]["name"]({BOX});out tags geom;',
    "lakes.json": f'[out:json][timeout:180];(way["natural"="water"]["name"]({BOX});relation["natural"="water"]["name"]({BOX}););'
                  'out tags center;',
}


def fetch(q, tries=4):
    for t in range(tries):
        for ep in ENDPOINTS:
            try:
                r = requests.post(ep, data={"data": q}, headers=UA, timeout=240)
                if r.status_code == 200 and r.text.lstrip().startswith("{"):
                    return r.json()
                print(f"  {ep}: HTTP {r.status_code}", flush=True)
            except requests.RequestException as e:
                print(f"  {ep}: {type(e).__name__}", flush=True)
        time.sleep(20 * (t + 1))
    raise SystemExit("Overpass unavailable")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    for fn, q in QUERIES.items():
        p = os.path.join(a.out, fn)
        if os.path.exists(p) and os.path.getsize(p) > 1000:
            continue
        d = fetch(q)
        json.dump(d, open(p, "w"))
        print(f"{fn}: {len(d['elements'])} elements", flush=True)


if __name__ == "__main__":
    main()
