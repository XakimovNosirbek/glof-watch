#!/usr/bin/env python3
"""Sentinel-1 radar check of a lake: median VV backscatter inside a reference outline, per relative orbit.

Uses Microsoft Planetary Computer 'sentinel-1-rtc' (radiometrically terrain-corrected, 10 m; free, items signed
anonymously). Open water is dark in VV; a drained lake bed (wet sediment, boulders) is much brighter. Because each
relative orbit sees the slopes differently, change is measured within an orbit: z = (scene - pre-event mean) / pre std.

Usage: s1_lake.py --glo data/glo/S2_20172024_TB_GLOID_v1_0.gpkg --glo-id GLO_86.57281_27.82796 --glo-year 2023 \
                  --start 2024-05-01 --end 2024-11-30 [--event 2024-08-16] --out out/thame_s1.csv
"""
import argparse

import geopandas as gpd
import numpy as np
import pandas as pd
import planetary_computer as pc
import pystac_client
from rasterio.features import rasterize
from rasterio.transform import from_bounds

from lake_timeseries import read


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glo")
    ap.add_argument("--glo-id")
    ap.add_argument("--box", help="lon0,lat0,lon1,lat1 lake box when the lake is not in GLO")
    ap.add_argument("--glo-year", type=int, default=2024)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--pad", type=float, default=0.002, help="degrees around the outline")
    ap.add_argument("--event", help="optional event date: prints per-orbit z-score of the first scene after it")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.box:
        from shapely.geometry import box
        lake = box(*[float(v) for v in a.box.split(",")])
    else:
        g = gpd.read_file(a.glo, where=f"GLO_ID = '{a.glo_id}' AND AREA_YEAR = {a.glo_year}")
        lake = g.dissolve().to_crs(4326).geometry.iloc[0]
    x0, y0, x1, y1 = lake.bounds
    b = (x0 - a.pad, y0 - a.pad, x1 + a.pad, y1 + a.pad)
    cat = pystac_client.Client.open("https://planetarycomputer.microsoft.com/api/stac/v1", modifier=pc.sign_inplace)
    items = sorted(cat.search(collections=["sentinel-1-rtc"], bbox=list(b), datetime=f"{a.start}/{a.end}").items(),
                   key=lambda i: i.datetime)
    rows, mask = [], None
    for it in items:
        vv, _ = read(it.assets["vv"].href, b)
        vv = vv.astype("float32")
        if mask is None:
            H, W = vv.shape
            mask = rasterize([(lake, 1)], out_shape=(H, W), transform=from_bounds(*b, W, H)).astype(bool)
        db = 10 * np.log10(np.clip(vv[mask], 1e-6, None))
        rows.append({"date": it.datetime.date(), "orbit": it.properties["sat:relative_orbit"],
                     "orbit_state": it.properties.get("sat:orbit_state"), "median_db": round(float(np.median(db)), 2),
                     "dark_frac": round(float((db < -18).mean()), 3), "pixels": int(mask.sum())})
    d = pd.DataFrame(rows)
    d.to_csv(a.out, index=False)
    print(f"{len(d)} scenes -> {a.out}")
    if a.event:
        ev = pd.Timestamp(a.event).date()
        for o, x in d.groupby("orbit"):
            pre, post = x[x.date < ev], x[x.date > ev]
            if len(pre) < 3 or post.empty:
                continue
            f = post.iloc[0]
            z = (f.median_db - pre.median_db.mean()) / pre.median_db.std()
            print(f"orbit {o}: pre {pre.median_db.mean():.1f}±{pre.median_db.std():.1f} dB, first after {f.date} "
                  f"({(f.date - ev).days} d): {f.median_db:.1f} dB, z = {z:.1f}")


if __name__ == "__main__":
    main()
