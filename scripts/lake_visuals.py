#!/usr/bin/env python3
"""Colour figures for one lake: true-colour Sentinel-2 with outlines, area chart vs. GLO, 3D terrain view.

All inputs are open data: Sentinel-2 L2A 'visual' (true colour) and Copernicus DEM GLO-30 from AWS Earth Search.
Usage: lake_visuals.py --name "Imja Tsho" --lat 27.8986 --lon 86.9239 --areas out/imja/areas.csv \
        --glo data/glo/S2_20172024_TB_GLOID_v1_0.gpkg --glo-id GLO_86.92845_27.89838 --years 2018,2026 --out docs/img
"""
import argparse
import math
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
from matplotlib.colors import LightSource

from lake_timeseries import BAD_SCL, lake_mask, read

STAC = "https://earth-search.aws.element84.com/v1/search"
CREDIT = "Contains modified Copernicus Sentinel data and Copernicus DEM GLO-30"


from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SESSION = requests.Session()
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(total=6, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                                                        allowed_methods=None)))


def item(scene_id):
    return SESSION.get(f"https://earth-search.aws.element84.com/v1/collections/sentinel-2-l2a/items/{scene_id}", timeout=120).json()


def dem_item(lon, lat):
    body = {"collections": ["cop-dem-glo-30"], "intersects": {"type": "Point", "coordinates": [lon, lat]}, "limit": 1}
    return SESSION.post(STAC, json=body, timeout=120).json()["features"][0]


def _refl(asset, bounds, shape=None):
    """Read a reflectance band and apply the asset's own scale/offset (processing baseline >= 04.00 has offset -0.1)."""
    arr, res = read(asset["href"], bounds, shape)
    rb = (asset.get("raster:bands") or [{}])[0]
    return arr.astype("float32") * rb.get("scale", 1e-4) + rb.get("offset", 0.0), res, arr


def rgb_and_mask(scene_id, bounds, thr=0.2):
    A = item(scene_id)["assets"]
    green_r, res, green = _refl(A["green"], bounds)
    nir_r, _, nir = _refl(A["nir"], bounds, green.shape)
    red_r, _, _ = _refl(A["red"], bounds, green.shape)
    blue_r, _, _ = _refl(A["blue"], bounds, green.shape)
    scl, _ = read(A["scl"]["href"], bounds, green.shape)
    rgb = np.dstack([red_r, green_r, blue_r])               # surface reflectance, not the clipped TCI
    seed = (green.shape[0] // 2, green.shape[1] // 2)
    m, _ = lake_mask(green, nir, scl, seed, thr)
    return rgb, m, res


def stretch(rgb, white=0.35, gamma=0.75):
    """Reflectance -> display colour with a high white point, so bright turbid water and snow are not clipped."""
    return np.clip(np.clip(rgb, 0, None) / white, 0, 1) ** gamma


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--areas", required=True)
    ap.add_argument("--glo")
    ap.add_argument("--glo-id")
    ap.add_argument("--years", default="2018,2026")
    ap.add_argument("--radius-m", type=float, default=2200)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    slug = a.name.lower().replace(" ", "_")
    df = pd.read_csv(a.areas)
    dlat = a.radius_m / 111320
    dlon = a.radius_m / (111320 * math.cos(math.radians(a.lat)))
    bounds = (a.lon - dlon, a.lat - dlat, a.lon + dlon, a.lat + dlat)
    y0, y1 = [int(y) for y in a.years.split(",")]
    s0 = df[df.year == y0].iloc[0]; s1 = df[df.year == y1].iloc[0]
    rgb0, m0, res = rgb_and_mask(s0.scene, bounds)
    rgb1, m1, _ = rgb_and_mask(s1.scene, bounds)
    ext_km = 2 * a.radius_m / 1000

    # 1) true colour, side by side + change map
    fig, ax = plt.subplots(1, 3, figsize=(16, 5.6), facecolor="white")
    for axx, rgb, m, s, col in ((ax[0], rgb0, m0, s0, "#ffd166"), (ax[1], rgb1, m1, s1, "#06d6a0")):
        axx.imshow(stretch(rgb), extent=(0, ext_km, 0, ext_km))
        axx.contour(np.flipud(m), levels=[0.5], colors=col, linewidths=1.6, extent=(0, ext_km, 0, ext_km))
        axx.set_title(f"{s.date}: {s.area_km2:.2f} km²", fontsize=12)
    ax[2].imshow(stretch(rgb1), extent=(0, ext_km, 0, ext_km))
    grow = m1 & ~m0
    over = np.zeros((*grow.shape, 4)); over[grow] = (1, 0.15, 0.3, 0.85)
    ax[2].imshow(over, extent=(0, ext_km, 0, ext_km))
    ax[2].contour(np.flipud(m0), levels=[0.5], colors="#ffd166", linewidths=1.4, extent=(0, ext_km, 0, ext_km))
    ax[2].set_title(f"New water {y0}→{y1}: +{(s1.area_km2 - s0.area_km2):.2f} km² (red)", fontsize=12)
    for axx in ax:
        axx.set_xlabel("km"); axx.set_ylabel("km")
    fig.suptitle(f"{a.name}: true-colour Sentinel-2, lake outline {y0} (yellow) and {y1} (green)", fontsize=13)
    fig.text(0.99, 0.01, CREDIT, ha="right", fontsize=7, color="#666")
    fig.savefig(os.path.join(a.out, f"{slug}_truecolour.png"), dpi=110, bbox_inches="tight"); plt.close(fig)

    # 2) area chart vs GLO
    fig, ax = plt.subplots(figsize=(9, 4.4))
    if a.glo and a.glo_id:
        import geopandas as gpd
        g = gpd.read_file(a.glo, where=f"GLO_ID = '{a.glo_id}'").sort_values("AREA_YEAR")
        ax.fill_between(g.AREA_YEAR, g.AREA - g.AREA_UNCERTAINTY, g.AREA + g.AREA_UNCERTAINTY, color="#8ecae6", alpha=0.45,
                        label="GLO uncertainty")
        ax.plot(g.AREA_YEAR, g.AREA, "s-", color="#023047", label="GLO (Rawlins et al.), May–Nov max")
    ok = df[df.year != 2025] if 2025 in df.year.values else df
    ax.plot(df.year, df.area_km2, "o-", color="#fb8500", label="this work, Sentinel-2 autumn max")
    if 2025 in df.year.values:
        r = df[df.year == 2025].iloc[0]
        ax.annotate("2025: ice/snow\non lake (flagged)", (2025, r.area_km2), xytext=(2022.3, r.area_km2 - 0.12),
                    arrowprops=dict(arrowstyle="->", color="#c1121f"), color="#c1121f", fontsize=9)
    k, b = np.polyfit(ok.year, ok.area_km2, 1)
    ax.plot(ok.year, np.polyval([k, b], ok.year), "--", color="#fb8500", alpha=0.6, label=f"trend {k:+.3f} km²/yr")
    ax.set_ylabel("lake area (km²)"); ax.set_xlabel("year"); ax.grid(alpha=0.3); ax.legend(fontsize=9, loc="upper left")
    ax.set_title(f"{a.name}: lake area, two independent Sentinel-2 series")
    fig.savefig(os.path.join(a.out, f"{slug}_area_vs_glo.png"), dpi=110, bbox_inches="tight"); plt.close(fig)

    # 3) 3D terrain (Copernicus DEM 30 m resampled to the 10 m image grid) draped with true colour, rendered by PyVista
    import pyvista as pv
    from scipy.ndimage import zoom
    dem, _ = read(dem_item(a.lon, a.lat)["assets"]["data"]["href"], bounds)
    H, W = m1.shape
    dem = zoom(dem.astype("float32"), (H / dem.shape[0], W / dem.shape[1]), order=1)[:H, :W]
    tex = stretch(rgb1).copy()
    tex[m1 & ~m0] = tex[m1 & ~m0] * 0.25 + np.array([0.95, 0.1, 0.3]) * 0.75     # new water since y0
    edge0 = m0 ^ np.pad(m0, 1, mode="edge")[2:, 1:-1]
    tex[edge0] = (1.0, 0.82, 0.25)                                               # y0 shoreline in yellow
    xs = np.linspace(0, 2 * a.radius_m, W); ys = np.linspace(2 * a.radius_m, 0, H)
    X, Y = np.meshgrid(xs, ys)
    grid = pv.StructuredGrid(X, Y, (dem - np.nanmin(dem)) * 1.5)
    grid.point_data["rgb"] = (tex.reshape(-1, 3, order="F") * 255).astype(np.uint8)
    pl = pv.Plotter(off_screen=True, window_size=(1800, 1100))
    pl.set_background("#0f1115")
    pl.add_mesh(grid, scalars="rgb", rgb=True, smooth_shading=True, ambient=0.25, diffuse=0.8, specular=0.05)
    pl.camera_position = [(2 * a.radius_m * 0.15, -2 * a.radius_m * 0.75, 2 * a.radius_m * 0.9),
                          (a.radius_m, a.radius_m * 1.05, 400), (0, 0, 1)]
    pl.add_text(f"{a.name}: 3D view from the south, Sentinel-2 {s1.date}", font_size=14, color="white", position="upper_left")
    pl.add_text(f"red = lake area gained since {y0} (+{(s1.area_km2 - s0.area_km2):.2f} km²) · yellow = {y0} shoreline · heights ×1.5",
                font_size=10, color="#dddddd", position="lower_left")
    pl.add_text(CREDIT, font_size=8, color="#999999", position="lower_right")
    pl.screenshot(os.path.join(a.out, f"{slug}_3d.png")); pl.close()
    print("->", a.out)


if __name__ == "__main__":
    main()
