#!/usr/bin/env python3
"""3D fly-around video of a lake: Copernicus DEM draped with true-colour Sentinel-2, fading from year A to year B.

Frames are rendered off-screen with PyVista and written as MP4 (H.264) plus a small GIF for README pages.
Usage: lake_video.py --name "Imja Tsho" --lat 27.8986 --lon 86.9239 --areas out/imja/areas.csv --years 2018,2026 \
                     --out docs/video/imja_tsho [--frames 180] [--radius-m 2200]
"""
import argparse
import math
import os

import imageio.v2 as imageio
import numpy as np
import pandas as pd
import pyvista as pv
from PIL import Image
from scipy.ndimage import zoom

from lake_timeseries import read
from lake_visuals import CREDIT, dem_item, rgb_and_mask, stretch


def texture(rgb, m_new=None, m_old=None, edge_col=(1.0, 0.82, 0.25)):
    t = stretch(rgb).copy()
    if m_new is not None:
        add = m_new & ~m_old
        t[add] = t[add] * 0.25 + np.array([0.95, 0.1, 0.3]) * 0.75
    if m_old is not None:
        edge = m_old ^ np.pad(m_old, 1, mode="edge")[2:, 1:-1]
        t[edge] = edge_col
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--areas", required=True)
    ap.add_argument("--years", default="2018,2026")
    ap.add_argument("--radius-m", type=float, default=2200)
    ap.add_argument("--frames", type=int, default=180)
    ap.add_argument("--exag", type=float, default=1.5)
    ap.add_argument("--out", required=True, help="output path without extension")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    df = pd.read_csv(a.areas)
    y0, y1 = [int(y) for y in a.years.split(",")]
    s0, s1 = df[df.year == y0].iloc[0], df[df.year == y1].iloc[0]
    dlat = a.radius_m / 111320
    dlon = a.radius_m / (111320 * math.cos(math.radians(a.lat)))
    bounds = (a.lon - dlon, a.lat - dlat, a.lon + dlon, a.lat + dlat)
    rgb0, m0, _ = rgb_and_mask(s0.scene, bounds)
    rgb1, m1, _ = rgb_and_mask(s1.scene, bounds)
    dem, _ = read(dem_item(a.lon, a.lat)["assets"]["data"]["href"], bounds)
    H, W = m1.shape
    dem = zoom(dem.astype("float32"), (H / dem.shape[0], W / dem.shape[1]), order=1)[:H, :W]
    tex0 = texture(rgb0, None, m0)
    tex1 = texture(rgb1, m1, m0)
    size = 2 * a.radius_m
    X, Y = np.meshgrid(np.linspace(0, size, W), np.linspace(size, 0, H))
    grid = pv.StructuredGrid(X, Y, (dem - np.nanmin(dem)) * a.exag)

    grid.point_data["rgb"] = (np.clip(tex0, 0, 1).reshape(-1, 3, order="F") * 255).astype(np.uint8)

    def set_tex(t):
        arr = grid.point_data["rgb"]                                 # update in place so the mapper sees it
        arr[:] = (np.clip(t, 0, 1).reshape(-1, 3, order="F") * 255).astype(np.uint8)
        grid.Modified()

    pl = pv.Plotter(off_screen=True, window_size=(1600, 900))
    pl.set_background("#0f1115")
    pl.add_mesh(grid, scalars="rgb", rgb=True, smooth_shading=True, ambient=0.25, diffuse=0.8, specular=0.05)
    title = pl.add_text("", font_size=15, color="white", position="upper_left")
    pl.add_text(CREDIT + f" · heights ×{a.exag}", font_size=8, color="#999999", position="lower_right")
    pl.show(auto_close=False, interactive=False)                      # initialise the off-screen render window
    centre = np.array([size / 2, size / 2, 300.0])
    frames, small = [], []
    n = a.frames
    for i in range(n):
        f = i / n
        az = math.radians(-100 + 300 * f)                         # sweep around the valley
        dist = size * (1.05 - 0.15 * math.sin(math.pi * f))        # gentle zoom in and out
        el = math.radians(38 - 8 * math.sin(math.pi * f))
        cam = centre + np.array([dist * math.cos(el) * math.cos(az), dist * math.cos(el) * math.sin(az), dist * math.sin(el)])
        pl.camera_position = [tuple(cam), tuple(centre), (0, 0, 1)]
        mix = min(1.0, max(0.0, (f - 0.3) / 0.25))                  # fade year A -> year B between 30 % and 55 %
        set_tex(tex0 * (1 - mix) + tex1 * mix)
        if mix < 0.5:
            txt = f"{a.name} · {s0.date} · lake {s0.area_km2:.2f} km² (yellow shoreline)"
        else:
            txt = (f"{a.name} · {s1.date} · lake {s1.area_km2:.2f} km²   "
                   f"red = +{s1.area_km2 - s0.area_km2:.2f} km² of new water since {y0}")
        title.set_text("upper_left", txt)
        pl.render()
        img = pl.screenshot(return_img=True)
        frames.append(img)
        if i % 3 == 0:
            small.append(np.asarray(Image.fromarray(img).resize((640, 360), Image.LANCZOS)))
    pl.close()
    imageio.mimwrite(a.out + ".mp4", frames, fps=30, codec="libx264", quality=8, macro_block_size=8)
    imageio.mimwrite(a.out + ".gif", small, duration=100, loop=0)
    print(f"{a.out}.mp4 ({os.path.getsize(a.out + '.mp4') / 1e6:.1f} MB), {a.out}.gif ({os.path.getsize(a.out + '.gif') / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
