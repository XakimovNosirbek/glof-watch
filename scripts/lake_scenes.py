#!/usr/bin/env python3
"""Per-scene lake area for one lake (ice-dammed lakes fill and drain within weeks, so annual maxima are not enough).

Water = NDWI > ndwi AND (green-red)/(green+red) > gr. The colour test separates turbid glacial water (greener than red)
from dark debris-covered ice, which NDWI alone also flags. Speckle is removed by a morphological opening, and the lake
is the connected component that touches the seed box. Scenes are skipped when the lake box is cloudy in the L2A SCL.

Usage: lake_scenes.py --name Shisper --bbox 74.540,36.348,74.590,36.378 --seed 74.556,36.357,74.572,36.364 \
                      --start 2018-05-01 --end 2023-12-31 --out out/shisper
"""
import argparse
import csv
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import ndimage

from lake_timeseries import BAD_SCL, STAC, read
from lake_visuals import SESSION, _refl


def search(bbox, start, end, max_cloud):
    body = {"collections": ["sentinel-2-l2a"], "bbox": bbox, "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
            "query": {"eo:cloud_cover": {"lt": max_cloud}}, "limit": 100}
    out, url = [], STAC
    while url:
        r = SESSION.post(url, json=body, timeout=120).json()
        out += r.get("features", [])
        nxt = [l for l in r.get("links", []) if l.get("rel") == "next"]
        url, body = (nxt[0]["href"], nxt[0].get("body", body)) if nxt else (None, None)
    # several tiles/orbits per date: try the least cloudy first; main() keeps the first one that covers the lake
    return sorted(out, key=lambda f: (f["properties"]["datetime"][:10], f["properties"]["eo:cloud_cover"]))


def scene_areas(b, s, start, end, max_cloud=60, ndwi=0.2, gr=0.02, verbose=True):
    """Lake area for every clear Sentinel-2 scene in [start, end]. b = lake window, s = seed box (lon0,lat0,lon1,lat1)."""
    rows = []
    feats = search(b, start, end, max_cloud)
    if verbose:
        print(f"{len(feats)} scenes", flush=True)
    done_dates = set()
    for f in feats:
        if f["properties"]["datetime"][:10] in done_dates:
            continue
        A = f["assets"]
        try:
            g, _, graw = _refl(A["green"], b)
            if (graw == 0).mean() > 0.02:
                continue
            n, _, _ = _refl(A["nir"], b, graw.shape)
            r, _, _ = _refl(A["red"], b, graw.shape)
            scl, _ = read(A["scl"]["href"], b, graw.shape)
        except Exception:
            continue
        H, W = graw.shape
        c0 = int((s[0] - b[0]) / (b[2] - b[0]) * W); c1 = int((s[2] - b[0]) / (b[2] - b[0]) * W)
        r0 = int((b[3] - s[3]) / (b[3] - b[1]) * H); r1 = int((b[3] - s[1]) / (b[3] - b[1]) * H)
        if np.isin(scl[r0:r1, c0:c1], list(BAD_SCL)).mean() > 0.05:
            continue
        water = ((g - n) / np.maximum(g + n, 1e-6) > ndwi) & ((g - r) / np.maximum(g + r, 1e-6) > gr)
        water &= ~np.isin(scl, list(BAD_SCL))
        water = ndimage.binary_opening(water, iterations=2)
        lab, _ = ndimage.label(water)
        ids = np.unique(lab[r0:r1, c0:c1]); ids = ids[ids > 0]
        m = ndimage.binary_fill_holes(np.isin(lab, ids)) if ids.size else np.zeros_like(water)
        px = (b[2] - b[0]) / W * 111320 * np.cos(np.radians((b[1] + b[3]) / 2)) * (b[3] - b[1]) / H * 111320
        area = m.sum() * px / 1e6
        date = f["properties"]["datetime"][:10]
        done_dates.add(date)
        frozen = float((scl[r0:r1, c0:c1] == 11).mean())
        rows.append({"date": date, "area_km2": round(area, 4), "cloud_pct": round(f["properties"]["eo:cloud_cover"], 1),
                     "seed_snow_ice_frac": round(frozen, 2), "scene": f["id"]})
        if verbose:
            print(f"{date} {area:.3f} km2", flush=True)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--bbox", required=True)
    ap.add_argument("--seed", required=True, help="lon0,lat0,lon1,lat1 box inside the lake")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--max-cloud", type=float, default=60)
    ap.add_argument("--ndwi", type=float, default=0.2)
    ap.add_argument("--gr", type=float, default=0.02)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    b = [float(v) for v in a.bbox.split(",")]
    s = [float(v) for v in a.seed.split(",")]
    rows = scene_areas(b, s, a.start, a.end, a.max_cloud, a.ndwi, a.gr)
    with open(os.path.join(a.out, "scenes.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    d = pd.DataFrame(rows); d["date"] = pd.to_datetime(d.date)
    fig, ax = plt.subplots(figsize=(12, 4.2))
    ax.plot(d.date, d.area_km2, "-", color="#8ecae6", lw=1)
    ax.plot(d.date, d.area_km2, "o", ms=3.5, color="#023047")
    ax.set_ylabel("lake area (km²)"); ax.grid(alpha=0.3)
    ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=(1, 7))); ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.set_title(f"{a.name}: lake area in every clear Sentinel-2 scene ({len(d)} scenes)")
    fig.savefig(os.path.join(a.out, "scenes_chart.png"), dpi=110, bbox_inches="tight"); plt.close(fig)


if __name__ == "__main__":
    main()
