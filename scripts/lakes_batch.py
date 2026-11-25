#!/usr/bin/env python3
"""Extend a published glacial lake inventory with new years of Sentinel-2 lake areas.

Reference lakes come from the Glacial Lake Observatory (GLO, Rawlins et al., CC BY 4.0): annual Sentinel-2 outlines
for Nepal and transboundary basins, 2017-2024. For each lake (filtered by area in --ref-year) and each requested year,
this reads up to --per-year clear scenes, takes the water bodies that touch the reference outline, and keeps the
annual maximum extent (same rule as lake_timeseries.py). Rows are appended to a CSV, so the run can resume.

Usage: lakes_batch.py --glo data/glo/S2_20172024_TB_GLOID_v1_0.gpkg --min-area 0.1 --years 2024,2025,2026 \
                      --out out/batch/areas.csv [--jobs 10] [--limit N]
"""
import argparse
import csv
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import rasterize
from rasterio.transform import from_bounds as tf_from_bounds
from scipy import ndimage
from shapely.ops import transform as shp_transform
from pyproj import Transformer

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from shapely.geometry import Point, shape

from lake_timeseries import BAD_SCL, STAC, read

LOCK = threading.Lock()
SESSION = requests.Session()
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(total=6, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                                                        allowed_methods=None)))
CACHE = {}


def region_items(cell, year, month, max_cloud):
    """All Sentinel-2 L2A items over one 1x1 degree cell for one month (paged once, then cached)."""
    key = (cell, year, month)
    bbox = (cell[0], cell[1], cell[0] + 1, cell[1] + 1)
    if key not in CACHE:
        end = 31 if month in (1, 3, 5, 7, 8, 10, 12) else 30 if month != 2 else 28
        body = {"collections": ["sentinel-2-l2a"], "bbox": list(bbox),
                "datetime": f"{year}-{month:02d}-01T00:00:00Z/{year}-{month:02d}-{end}T23:59:59Z",
                "query": {"eo:cloud_cover": {"lt": max_cloud}}, "limit": 100}
        out, url = [], STAC
        while url:
            r = SESSION.post(url, json=body, timeout=120).json()
            out += r.get("features", [])
            nxt = [l for l in r.get("links", []) if l.get("rel") == "next"]
            url, body = (nxt[0]["href"], nxt[0].get("body", body)) if nxt else (None, None)
        for f in out:
            f["_shape"] = shape(f["geometry"])
        CACHE[key] = out
        print(f"  cached {cell} {year}-{month:02d}: {len(out)} scenes", flush=True)
    return CACHE[key]


def scenes(lon, lat, year, months, max_cloud):
    p = Point(lon, lat)
    cell = (math.floor(lon), math.floor(lat))
    feats = [f for m in months for f in region_items(cell, year, m, max_cloud) if f["_shape"].contains(p)]
    return sorted(feats, key=lambda f: f["properties"]["eo:cloud_cover"])


REGION = None
CLIP_PX = 0            # --clip-px: count water only within N pixels of the reference outline (small lakes next to rivers)
HARMONIZE = False      # --harmonize: apply each asset's scale/offset (baseline >= 04.00, from 2022, adds a +1000 DN offset)


def _bands(A, green, nir):
    if not HARMONIZE:
        return green.astype("float32"), nir.astype("float32")
    out = []
    for k, arr in (("green", green), ("nir", nir)):
        rb = (A[k].get("raster:bands") or [{}])[0]
        out.append(arr.astype("float32") * rb.get("scale", 1e-4) + rb.get("offset", 0.0))
    return out


def lake_area(geom_ll, lon, lat, year, months, max_cloud, tries, per_year, thr):
    """Annual maximum water extent (km2) touching the reference outline, plus metadata."""
    minx, miny, maxx, maxy = geom_ll.bounds
    pad = max(0.004, 0.6 * max(maxx - minx, maxy - miny))          # grow room around the reference lake
    bounds = (minx - pad, miny - pad, maxx + pad, maxy + pad)
    good = []
    cands = [f for m in months for f in scenes(lon, lat, year, [m], max_cloud)[:tries]]
    ref = None
    for f in cands:
        A = f["assets"]
        try:
            # same grid for every scene: a lake near a UTM zone edge is served by tiles in two projections
            green, res = read(A["green"]["href"], bounds, None if ref is None else ref.shape)
            nir, _ = read(A["nir"]["href"], bounds, green.shape)
            scl, _ = read(A["scl"]["href"], bounds, green.shape)
        except Exception:
            continue
        if (green == 0).mean() > 0.02:
            continue
        if ref is None:
            H, W = green.shape
            tr = tf_from_bounds(*bounds, W, H)                      # lon/lat grid of the window
            ref = rasterize([(geom_ll, 1)], out_shape=(H, W), transform=tr).astype(bool)
            ring = ndimage.binary_dilation(ref, iterations=3)
        if np.isin(scl[ring], list(BAD_SCL)).mean() > 0.03:
            continue                                                # clouds/shadow on or around the lake
        if (scl[ref] == 11).mean() > 0.2:
            continue                                                # lake frozen / snow-covered (SCL snow-ice class)
        g, n = _bands(A, green, nir)
        # NDWI, or the L2A water class: NDWI alone fails on shadowed late-autumn lakes in deep valleys
        water = (((g - n) / np.maximum(g + n, 1e-4 if HARMONIZE else 1) > thr) | (scl == 6)) & ~np.isin(scl, list(BAD_SCL))
        lab, _ = ndimage.label(water)
        ids = np.unique(lab[ref & (lab > 0)])
        if ids.size == 0:
            m = np.zeros_like(water)
        else:
            m = ndimage.binary_fill_holes(np.isin(lab, ids))
        if CLIP_PX:
            m &= ndimage.binary_dilation(ref, iterations=CLIP_PX)  # no leaking into rivers, wet valley floors, shadow
        # pixel area in the lon/lat window -> m2 using the latitude scale
        px = ((bounds[2] - bounds[0]) / water.shape[1] * 111320 * math.cos(math.radians(lat))) * \
             ((bounds[3] - bounds[1]) / water.shape[0] * 111320)
        good.append((m.sum() * px / 1e6, f["properties"]["datetime"][:10]))
        if len(good) >= per_year:
            break
    if not good:
        return None
    area, date = max(good)
    return {"area_km2": round(area, 5), "date": date, "n_scenes": len(good), "min_km2": round(min(a for a, _ in good), 5)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glo", required=True)
    ap.add_argument("--ref-year", type=int, default=2024)
    ap.add_argument("--min-area", type=float, default=0.1)
    ap.add_argument("--years", default="2024,2025,2026")
    ap.add_argument("--months", default="9,10,6,5,11")
    ap.add_argument("--max-cloud", type=float, default=40)
    ap.add_argument("--tries", type=int, default=5)
    ap.add_argument("--per-year", type=int, default=4)
    ap.add_argument("--thr", type=float, default=0.2)
    ap.add_argument("--jobs", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--harmonize", action="store_true", help="NDWI on surface reflectance (asset scale/offset applied)")
    ap.add_argument("--clip-px", type=int, default=0, help="count water only within N pixels (10 m) of the reference outline")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    g = gpd.read_file(a.glo)
    ref = g[(g.AREA_YEAR == a.ref_year) & (g.AREA >= a.min_area)].to_crs("EPSG:4326")
    ref = ref.sort_values("AREA", ascending=False)
    if a.limit:
        ref = ref.head(a.limit)
    global REGION, HARMONIZE, CLIP_PX
    HARMONIZE = a.harmonize
    CLIP_PX = a.clip_px
    REGION = tuple(ref.total_bounds + np.array([-0.05, -0.05, 0.05, 0.05]))
    years = [int(y) for y in a.years.split(",")]
    months = [int(m) for m in a.months.split(",")]
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    done = set()
    if os.path.exists(a.out):
        done = {(r["GLO_ID"], int(r["year"])) for r in csv.DictReader(open(a.out))}
    fields = ["GLO_ID", "year", "area_km2", "date", "n_scenes", "min_km2", "glo_ref_area", "basin", "country", "connectivity", "lon", "lat"]
    new_file = not os.path.exists(a.out)
    fh = open(a.out, "a", newline="")
    w = csv.DictWriter(fh, fieldnames=fields)
    if new_file:
        w.writeheader()
    jobs = [(r, y) for _, r in ref.iterrows() for y in years if (r.GLO_ID, y) not in done]
    cells = sorted({(math.floor(r.LONGITUDE), math.floor(r.LATITUDE)) for r, _ in jobs})
    print(f"warming scene cache: {len(cells)} cells", flush=True)
    for c in cells:                                      # warm the scene cache once, before the worker threads start
        for y in sorted({y for _, y in jobs}):
            for m in months:
                region_items(c, y, m, a.max_cloud)
    print(f"{len(ref)} lakes, {len(jobs)} lake-years to do", flush=True)

    def work(job):
        r, y = job
        res = lake_area(r.geometry, r.LONGITUDE, r.LATITUDE, y, months, a.max_cloud, a.tries, a.per_year, a.thr)
        row = {"GLO_ID": r.GLO_ID, "year": y, "glo_ref_area": round(r.AREA, 5), "basin": r.BASIN, "country": r.COUNTRY,
               "connectivity": r.CONNECTIVITY, "lon": r.LONGITUDE, "lat": r.LATITUDE,
               **(res or {"area_km2": "", "date": "", "n_scenes": 0, "min_km2": ""})}
        with LOCK:
            w.writerow(row); fh.flush()
        return row

    with ThreadPoolExecutor(a.jobs) as ex:
        for i, row in enumerate(ex.map(work, jobs), 1):
            if i % 25 == 0 or i == len(jobs):
                print(f"[{i}/{len(jobs)}] {row['GLO_ID']} {row['year']} {row['area_km2']}", flush=True)
    fh.close()


if __name__ == "__main__":
    main()
