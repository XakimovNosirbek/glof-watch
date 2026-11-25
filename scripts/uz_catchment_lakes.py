#!/usr/bin/env python3
"""Map every lake inside one catchment from Sentinel-2, for catchments whose lakes are too small for the inventories.

The catchment is a HydroBASINS level-10 sub-basin plus everything upstream of it. For each year, the clearest late-summer
scenes (all tiles of a date are mosaicked) are read over the catchment; water is NDWI > thr (surface reflectance) or the
L2A water class, darker than --max-nir in the near infrared (bare ice passes NDWI), on terrain flatter than --max-slope
(median Copernicus DEM slope per object; shadowed slopes pass NDWI), minus cloud, shadow and snow/ice classes; objects smaller than --min-px
pixels are dropped. A pixel
counts as lake when it is water in a strict majority of the clear scenes of that year, which removes one-scene shadow and
wet-snow artefacts.

Writes <out>/<year>_lakes.gpkg (polygons, GLO-like columns for lakes_batch.py) and <out>/summary.csv.
Usage: uz_catchment_lakes.py --hybas data/hybas/hybas_as_lev10_v1c.shp --root 4100454400 --name Shakhimardan \
                             --years 2018,2025 --out out/uz/shakhimardan_map
"""
import argparse
import json
import os
import time

os.environ.setdefault("GDAL_HTTP_TIMEOUT", "60")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "4")
os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "5")
os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize, shapes
from rasterio.transform import from_bounds as tf_from_bounds
from scipy import ndimage
from shapely.geometry import box, shape

from lake_timeseries import BAD_SCL, read
from lake_visuals import SESSION, STAC, _refl


def retry(fn, *args, tries=4):
    """COG range reads over HTTP sometimes come back truncated; try again before giving up."""
    for t in range(tries):
        try:
            return fn(*args)
        except Exception as e:
            if t == tries - 1:
                raise
            print(f"  retry {t + 1}: {str(e)[:80]}", flush=True)
            time.sleep(5 * (t + 1))


def catchment(hybas, root):
    hb = gpd.read_file(hybas, bbox=(60, 35, 80, 46))
    up = {}
    for h, n in zip(hb.HYBAS_ID, hb.NEXT_DOWN):
        up.setdefault(n, []).append(h)
    ids, i = [root], 0
    while i < len(ids):
        ids += up.get(ids[i], []); i += 1
    return hb[hb.HYBAS_ID.isin(ids)].geometry.union_all()


def search(bounds, year, months, max_cloud):
    out = []
    for m in months:
        end = 31 if m in (1, 3, 5, 7, 8, 10, 12) else 30
        body = {"collections": ["sentinel-2-l2a"], "bbox": list(bounds), "limit": 100,
                "datetime": f"{year}-{m:02d}-01T00:00:00Z/{year}-{m:02d}-{end}T23:59:59Z",
                "query": {"eo:cloud_cover": {"lt": max_cloud}}}
        out += SESSION.post(STAC, json=body, timeout=120).json().get("features", [])
    by_date, seen = {}, set()
    for f in sorted(out, key=lambda f: f["id"], reverse=True):     # one item per tile and date (newest processing)
        key = (f["properties"]["datetime"][:10], f["properties"].get("s2:mgrs_tile") or f["id"].split("_")[1])
        if key in seen or not shape(f["geometry"]).intersects(box(*bounds)):
            continue
        seen.add(key)
        by_date.setdefault(key[0], []).append(f)
    return by_date


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hybas", required=True)
    ap.add_argument("--root", type=int, required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--years", default="2018,2025")
    ap.add_argument("--months", default="9,10,8")
    ap.add_argument("--max-cloud", type=float, default=30)
    ap.add_argument("--scenes", type=int, default=3, help="clear scenes used per year")
    ap.add_argument("--thr", type=float, default=0.2)
    ap.add_argument("--max-nir", type=float, default=0.12, help="NIR reflectance ceiling for water (ice is brighter)")
    ap.add_argument("--max-slope", type=float, default=15, help="median DEM slope ceiling per object, degrees")
    ap.add_argument("--min-px", type=int, default=10, help="smallest lake kept, in 10 m pixels (10 px = 0.001 km2)")
    ap.add_argument("--max-lat", type=float, default=90, help="map only the catchment south of this latitude")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    poly = catchment(a.hybas, a.root).intersection(box(-180, -90, 180, a.max_lat))
    b = poly.bounds
    lat = (b[1] + b[3]) / 2
    W = int((b[2] - b[0]) * 111320 * np.cos(np.radians(lat)) / 10)
    H = int((b[3] - b[1]) * 111320 / 10)
    tr = tf_from_bounds(*b, W, H)
    inside = rasterize([(poly, 1)], out_shape=(H, W), transform=tr).astype(bool)
    cdir = os.path.join(a.out, "cache"); os.makedirs(cdir, exist_ok=True)
    dem_p = os.path.join(cdir, "dem.npy")
    if os.path.exists(dem_p) and np.load(dem_p).shape == (H, W):
        dem = np.load(dem_p)
    else:
        dem = np.zeros((H, W), "float32")
        for f in SESSION.post(STAC, json={"collections": ["cop-dem-glo-30"], "bbox": list(b), "limit": 20}, timeout=120).json()["features"]:
            t, _ = read(f["assets"]["data"]["href"], b, (H, W))      # 1x1 degree tiles, outside = 0
            dem = np.where(dem == 0, t, dem)
        np.save(dem_p, dem)
    # lakes are flat: water objects on steep ground are terrain shadow (the main false positive here). Tested per object
    # (median slope), because the 30 m DEM is not flat over narrow lakes in gorges.
    gy, gx = np.gradient(ndimage.uniform_filter(dem, 5), 10.0)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    px_km2 = (b[2] - b[0]) / W * 111320 * np.cos(np.radians(lat)) * (b[3] - b[1]) / H * 111320 / 1e6
    print(f"{a.name}: catchment {poly.area * (111.32 ** 2) * np.cos(np.radians(lat)):.0f} km2, grid {W}x{H}", flush=True)
    summary, maps = [], {}
    for year in [int(y) for y in a.years.split(",")]:
        dates = search(b, year, [int(m) for m in a.months.split(",")], a.max_cloud)
        masks, used = [], []
        rej_p = os.path.join(cdir, "rejected.json")
        rejected = set(json.load(open(rej_p))) if os.path.exists(rej_p) else set()
        for d in sorted(dates, key=lambda d: min(f["properties"]["eo:cloud_cover"] for f in dates[d])):
            cp = os.path.join(cdir, f"{d}.npz")
            if d in rejected:
                continue
            if os.path.exists(cp):                                # scene arrays already downloaded
                z = np.load(cp); g, n, scl = z["g"].astype("float32"), z["n"].astype("float32"), z["scl"]
                bad = np.isin(scl, list(BAD_SCL)) & inside; snow = (scl == 11) & inside
            else:
                scl = np.zeros((H, W), "uint8"); src = np.full((H, W), -1, "int16")
                for k, f in enumerate(dates[d]):                  # mosaic every tile of that date, SCL first (cheap)
                    try:
                        s_, _ = retry(read, f["assets"]["scl"]["href"], b, (H, W))
                    except Exception as e:
                        print(f"  {d} {f['id']}: {e}", flush=True); continue
                    fill = (scl == 0) & (s_ > 0)
                    scl[fill], src[fill] = s_[fill], k
                bad = np.isin(scl, list(BAD_SCL)) & inside
                snow = (scl == 11) & inside
                if (inside & (scl > 0)).sum() < 0.98 * inside.sum() or bad.sum() > 0.03 * inside.sum() \
                        or snow.sum() > 0.25 * inside.sum():
                    rejected.add(d); json.dump(sorted(rejected), open(rej_p, "w"))
                    continue                                      # not fully covered, cloudy, or snowy
                g = np.zeros((H, W), "float32"); n = np.zeros_like(g)
                for k, f in enumerate(dates[d]):
                    if not (src == k).any():
                        continue
                    gg, _, _ = retry(_refl, f["assets"]["green"], b, (H, W))
                    nn, _, _ = retry(_refl, f["assets"]["nir"], b, (H, W))
                    g[src == k], n[src == k] = gg[src == k], nn[src == k]
                np.savez_compressed(cp, g=g.astype("float16"), n=n.astype("float16"), scl=scl)
            # water: NDWI or the L2A water class, and dark in NIR (bare glacier ice and wet firn also pass NDWI > 0.2)
            w = (((g - n) / np.maximum(g + n, 1e-6) > a.thr) | (scl == 6)) & (n < a.max_nir) \
                & ~np.isin(scl, list(BAD_SCL) + [11]) & inside
            masks.append(w); used.append(d)
            print(f"  {year}: {d} clear (cloud/shadow {bad.sum() / inside.sum():.1%}, snow {snow.sum() / inside.sum():.1%})", flush=True)
            if len(masks) >= a.scenes:
                break
        if not masks:
            print(f"  {year}: no clear scene"); continue
        votes = np.sum(masks, axis=0)
        lake = votes > len(masks) / 2                         # strict majority (both scenes when there are two)
        lake = ndimage.binary_fill_holes(ndimage.binary_opening(lake, iterations=1))
        lab, nlab = ndimage.label(lake)
        sizes = ndimage.sum(lake, lab, range(1, nlab + 1))
        keep = np.isin(lab, 1 + np.flatnonzero(sizes >= a.min_px))
        lab, nlab = ndimage.label(keep)
        med_slope = ndimage.median(slope, lab, range(1, nlab + 1))
        rows = []
        for geom, v in shapes(lab.astype("int32"), mask=lab > 0, transform=tr):
            gshape = shape(geom)
            rp = gshape.representative_point()
            r, c = rasterio.transform.rowcol(tr, rp.x, rp.y)
            rows.append({"lab": int(v), "geometry": gshape, "LONGITUDE": round(rp.x, 5), "LATITUDE": round(rp.y, 5),
                         "ELEV_M": int(dem[r, c]) if 0 <= r < H and 0 <= c < W else -1,
                         "SLOPE_MED": round(float(med_slope[int(v) - 1]), 1)})
        gdf = gpd.GeoDataFrame(rows, crs="EPSG:4326").dissolve(by="lab", aggfunc="first").reset_index(drop=True)
        gdf["AREA"] = gdf.to_crs("EPSG:6933").area / 1e6
        gdf = gdf.sort_values("AREA", ascending=False)
        gdf["GLO_ID"] = [f"UZL_{lo:.5f}_{la:.5f}" for lo, la in zip(gdf.LONGITUDE, gdf.LATITUDE)]
        gdf["AREA_YEAR"], gdf["BASIN"], gdf["COUNTRY"], gdf["CONNECTIVITY"] = year, a.name, "", ""
        gdf.to_file(os.path.join(a.out, f"{year}_water_all.gpkg"), driver="GPKG")       # every object, for checks
        steep = gdf[gdf.SLOPE_MED >= a.max_slope]
        gdf = gdf[gdf.SLOPE_MED < a.max_slope]
        gdf.to_file(os.path.join(a.out, f"{year}_lakes.gpkg"), driver="GPKG")
        maps[year] = (gdf, steep)
        s = {"year": year, "scenes": ",".join(used), "n_lakes": len(gdf), "total_km2": round(gdf.AREA.sum(), 4),
             "n_steep_rejected": len(steep), "km2_steep_rejected": round(steep.AREA.sum(), 4),
             "n_ge_0.005": int((gdf.AREA >= 0.005).sum()), "km2_ge_0.005": round(gdf.AREA[gdf.AREA >= 0.005].sum(), 4),
             "n_above_2500m": int((gdf.ELEV_M >= 2500).sum()), "km2_above_2500m": round(gdf.AREA[gdf.ELEV_M >= 2500].sum(), 4),
             "largest_km2": round(gdf.AREA.max(), 4), "px_km2": round(px_km2, 6)}
        summary.append(s)
        print({k: (float(v) if isinstance(v, np.floating) else v) for k, v in s.items()}, flush=True)
    pd.DataFrame(summary).to_csv(os.path.join(a.out, "summary.csv"), index=False)
    if maps:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import LightSource
        fig, ax = plt.subplots(figsize=(11, 8.5))
        ext = (b[0], b[2], b[1], b[3])
        ax.imshow(LightSource(315, 40).hillshade(dem, vert_exag=1, dx=10, dy=10)[::4, ::4], cmap="gray", extent=ext, alpha=0.8)
        ax.imshow(np.ma.masked_where(~inside[::4, ::4], dem[::4, ::4]), cmap="terrain", alpha=0.25, extent=ext)
        gpd.GeoSeries([poly], crs="EPSG:4326").boundary.plot(ax=ax, color="k", lw=1)
        cols = ["#ffb703", "#219ebc", "#d62828", "#2a9d8f"]
        last = max(maps)
        maps[last][1].plot(ax=ax, color="#888", alpha=0.6, label="rejected as steep (shadow), " + str(last))
        for (y, (gdf, _)), c in zip(sorted(maps.items()), cols):
            gdf.boundary.plot(ax=ax, color=c, lw=1.4)
            ax.plot([], [], color=c, lw=2, label=f"{y}: {len(gdf)} lakes, {gdf.AREA.sum():.3f} km²")
        ax.legend(loc="lower left", fontsize=9)
        ax.set_xlim(b[0], b[2]); ax.set_ylim(b[1], b[3]); ax.set_aspect(1 / np.cos(np.radians(lat)))
        ax.set_xlabel("longitude (°E)"); ax.set_ylabel("latitude (°N)")
        ax.set_title(f"{a.name} catchment (upper part): lakes mapped from Sentinel-2, late summer", fontsize=11)
        fig.savefig(os.path.join(a.out, "lakes_map.png"), dpi=110, bbox_inches="tight"); plt.close(fig)


if __name__ == "__main__":
    main()
