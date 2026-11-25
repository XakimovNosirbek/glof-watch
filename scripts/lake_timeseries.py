#!/usr/bin/env python3
"""Glacial lake area time series from Sentinel-2 L2A (AWS Earth Search STAC, anonymous, COG windowed reads).

For each year, the clearest scene in the chosen months is read around the lake, water is NDWI > threshold,
and the lake is the water body connected to the seed point. Cloud/shadow/snow come from the L2A scene
classification (SCL); a year is skipped if the lake window is too cloudy.

Usage: lake_timeseries.py --name imja --lat 27.898 --lon 86.925 --years 2017-2026 --months 9,10,11 --out out/imja
Writes areas.csv, area_chart.png and one outline thumbnail per year.
"""
import argparse
import csv
import math
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
import requests
from pyproj import Transformer
from rasterio.windows import from_bounds
from scipy import ndimage

STAC = "https://earth-search.aws.element84.com/v1/search"
BAD_SCL = {0, 1, 3, 8, 9, 10}          # nodata, saturated, cloud shadow, cloud med/high, cirrus
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
import os as _os
for _k, _v in {"GDAL_HTTP_TIMEOUT": "60", "GDAL_HTTP_CONNECTTIMEOUT": "20", "GDAL_HTTP_MAX_RETRY": "4",
               "GDAL_HTTP_RETRY_DELAY": "3", "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "AWS_NO_SIGN_REQUEST": "YES"}.items():
    _os.environ.setdefault(_k, _v)          # without a timeout a stalled HTTP range read hangs a worker forever



def scenes(lon, lat, year, months, max_cloud):
    feats = []
    for m in months:
        end = (31 if m in (1, 3, 5, 7, 8, 10, 12) else 30 if m != 2 else 28)
        body = {"collections": ["sentinel-2-l2a"], "intersects": {"type": "Point", "coordinates": [lon, lat]},
                "datetime": f"{year}-{m:02d}-01T00:00:00Z/{year}-{m:02d}-{end}T23:59:59Z",
                "query": {"eo:cloud_cover": {"lt": max_cloud}}, "limit": 50}
        feats += requests.post(STAC, json=body, timeout=60).json().get("features", [])
    return sorted(feats, key=lambda f: f["properties"]["eo:cloud_cover"])


def read(href, bounds_ll, shape=None):
    with rasterio.open(href) as s:
        t = Transformer.from_crs("EPSG:4326", s.crs, always_xy=True)
        x0, y0 = t.transform(bounds_ll[0], bounds_ll[1])
        x1, y1 = t.transform(bounds_ll[2], bounds_ll[3])
        w = from_bounds(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), s.transform)
        out_shape = shape or (int(round(w.height)), int(round(w.width)))
        a = s.read(1, window=w, out_shape=out_shape, boundless=True, fill_value=0,
                   resampling=rasterio.enums.Resampling.nearest)
        return a, abs(s.res[0]) * w.width / out_shape[1]


def lake_mask(green, nir, scl, seed_rc, thr):
    g, n = green.astype("float32"), nir.astype("float32")
    ndwi = (g - n) / np.maximum(g + n, 1)
    water = (ndwi > thr) & ~np.isin(scl, list(BAD_SCL))
    lab, _ = ndimage.label(water)
    r, c = seed_rc
    # nearest water pixel to the seed within 300 m (30 px at 10 m)
    win = lab[max(r - 30, 0):r + 31, max(c - 30, 0):c + 31]
    ids = win[win > 0]
    if ids.size == 0:
        return None, ndwi
    lake_id = np.bincount(ids).argmax()
    return ndimage.binary_fill_holes(lab == lake_id), ndwi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--years", default="2017-2026")
    ap.add_argument("--months", default="10,11")
    ap.add_argument("--radius-m", type=float, default=2500)
    ap.add_argument("--max-cloud", type=float, default=30)
    ap.add_argument("--thr", type=float, default=0.2)
    ap.add_argument("--tries", type=int, default=6, help="scenes tried per month")
    ap.add_argument("--per-year", type=int, default=4, help="usable scenes per year; the annual maximum extent is kept")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    y0, y1 = map(int, a.years.split("-"))
    months = [int(m) for m in a.months.split(",")]
    dlat = a.radius_m / 111320
    dlon = a.radius_m / (111320 * math.cos(math.radians(a.lat)))
    bounds = (a.lon - dlon, a.lat - dlat, a.lon + dlon, a.lat + dlat)
    rows = []
    for year in range(y0, y1 + 1):
        done = False
        good = []
        # months in priority order (e.g. 9,10 before 11: late-autumn scenes can show partial lake ice)
        cands = [f for m in months for f in scenes(a.lon, a.lat, year, [m], a.max_cloud)[:a.tries]]
        for f in cands:
            A = f["assets"]
            try:
                green, res = read(A["green"]["href"], bounds)
                nir, _ = read(A["nir"]["href"], bounds, green.shape)
                scl, _ = read(A["scl"]["href"], bounds, green.shape)
            except Exception as e:  # missing asset or read error -> try next scene
                print(f"{year} {f['id']}: read error {e}")
                continue
            if (green == 0).mean() > 0.05:
                continue                                   # scene edge / nodata over the lake window
            seed = (green.shape[0] // 2, green.shape[1] // 2)
            near = scl[seed[0] - 40:seed[0] + 41, seed[1] - 40:seed[1] + 41]
            if np.isin(near, list(BAD_SCL)).mean() > 0.05:
                continue                                   # lake neighbourhood cloudy
            m, ndwi = lake_mask(green, nir, scl, seed, a.thr)
            if m is None:
                continue
            area = m.sum() * res * res / 1e6
            date = f["properties"]["datetime"][:10]
            good.append((area, date, f, green, m))
            if len(good) >= a.per_year:
                break
        if good:
            area, date, f, green, m = max(good, key=lambda g: g[0])   # annual maximum open-water extent
            rows.append({"year": year, "date": date, "scene": f["id"], "cloud_pct": round(f["properties"]["eo:cloud_cover"], 1),
                         "area_km2": round(area, 4), "n_scenes": len(good),
                         "min_km2": round(min(g[0] for g in good), 4), "res_m": round(res, 1)})
            fig, ax = plt.subplots(figsize=(5, 5))
            ax.imshow(np.clip(green / 3000, 0, 1), cmap="gray")
            ax.contour(m, levels=[0.5], colors="#00e5ff", linewidths=1.2)
            ax.set_title(f"{a.name} {date}: {area:.3f} km² (max of {len(good)})", fontsize=10); ax.axis("off")
            fig.savefig(os.path.join(a.out, f"{year}_{date}.png"), dpi=90, bbox_inches="tight"); plt.close(fig)
            print(f"{year} {date} max of {len(good)} scenes -> {area:.3f} km2 (min {min(g[0] for g in good):.3f})")
            done = True
        if not done:
            print(f"{year}: no usable scene")
    if not rows:
        raise SystemExit("no data")
    with open(os.path.join(a.out, "areas.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)
    yrs = [r["year"] for r in rows]; ar = [r["area_km2"] for r in rows]
    fig, ax = plt.subplots(figsize=(7, 3.6))
    ax.plot(yrs, ar, "o-", color="#2e86ab")
    if len(yrs) > 2:
        k, b = np.polyfit(yrs, ar, 1)
        ax.plot(yrs, np.polyval([k, b], yrs), "--", color="#e4572e", label=f"trend {k*1e6/1e4:+.2f} ha/yr")
        ax.legend()
    ax.set_ylabel("lake area (km²)"); ax.set_title(f"{a.name}: Sentinel-2 lake area ({a.months} months)"); ax.grid(alpha=.3)
    fig.savefig(os.path.join(a.out, "area_chart.png"), dpi=100, bbox_inches="tight"); plt.close(fig)
    print("->", a.out)


if __name__ == "__main__":
    main()
