#!/usr/bin/env python3
"""Join GLO 2017-2024 with this work's 2023-2026 areas, calibrate, rank change, and build figures + an interactive map.

Outputs (in --out):
  lake_changes.csv        per lake: GLO series, our 2023-2026, calibration ratio, growth, flags
  calibration.png         our 2023/2024 vs GLO 2023/2024 (same lakes, same years)
  top_growth.png          fastest-growing lakes 2024 -> 2026 (bar chart)
  lakes_map.html          interactive map (satellite / topo / OSM base layers), lakes coloured by growth, popups
Usage: analyse_and_map.py --glo data/glo/S2_20172024_TB_GLOID_v1_0.gpkg --areas out/batch/areas.csv --out docs
"""
import argparse
import json
import os

import folium
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from branca.colormap import LinearColormap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glo", required=True)
    ap.add_argument("--areas", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-scenes", type=int, default=2)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    g = gpd.read_file(a.glo)
    glo = g.pivot_table(index="GLO_ID", columns="AREA_YEAR", values="AREA", aggfunc="sum")   # multi-part lakes: sum the parts
    glo.columns = [f"glo_{int(c)}" for c in glo.columns]
    unc = g[g.AREA_YEAR == 2024].groupby("GLO_ID")["AREA_UNCERTAINTY"].sum().rename("glo_unc")
    ours = pd.read_csv(a.areas)
    ours = ours[pd.to_numeric(ours.area_km2, errors="coerce").notna() & (ours.n_scenes >= a.min_scenes)]
    ours = ours.drop_duplicates(["GLO_ID", "year"])        # multi-part GLO lakes were queued once per part
    ow = ours.pivot_table(index="GLO_ID", columns="year", values="area_km2")
    ow.columns = [f"s2_{int(c)}" for c in ow.columns]
    mn = ours.pivot_table(index="GLO_ID", columns="year", values="min_km2")
    mn.columns = [f"s2min_{int(c)}" for c in mn.columns]
    meta = ours.groupby("GLO_ID")[["basin", "country", "connectivity", "lon", "lat"]].first()
    d = meta.join(ow).join(mn).join(glo).join(unc)

    # calibration on overlapping years
    pairs = [(d[f"s2_{y}"], d[f"glo_{y}"]) for y in (2023, 2024) if f"s2_{y}" in d and f"glo_{y}" in d]
    x = pd.concat([p[1] for p in pairs]); yv = pd.concat([p[0] for p in pairs])
    ok = x.notna() & yv.notna() & (x > 0)
    ratio = (yv[ok] / x[ok])
    med = float(ratio.median())
    r2 = float(np.corrcoef(x[ok], yv[ok])[0, 1] ** 2)
    mape = float((abs(yv[ok] - x[ok]) / x[ok]).median() * 100)
    d["s2_2026_cal"] = d.get("s2_2026") / med
    # conservative: compare 2026 with the LARGER of our 2024 value and GLO's 2024 value (calibrated), so one
    # under-measured 2024 scene cannot manufacture growth
    ref24 = np.fmax(d.get("s2_2024"), d.get("glo_2024") * med)
    d["growth_24_26_km2"] = (d.get("s2_2026") - ref24) / med
    d["growth_pct"] = 100 * (d.get("s2_2026") - ref24) / ref24
    d["glo_trend_km2yr"] = g[g.AREA_YEAR.between(2017, 2024)].groupby("GLO_ID").apply(
        lambda t: np.polyfit(t.AREA_YEAR, t.AREA, 1)[0] if len(t) >= 4 else np.nan)
    d["new_trend_km2yr"] = d["growth_24_26_km2"] / 2
    # annual maxima hide drainages (Thame 2024 lesson), so also compare the year's minimum with the previous maximum
    prev_max = d[["s2_2023", "s2_2024"]].max(axis=1)
    # robust: the whole autumn's MAXIMUM fell below 60 % of the earlier maximum (not one icy/cloudy scene)
    d["flag_drop"] = (d.get("s2_2025") < 0.6 * prev_max) | (d.get("s2_2026") < 0.6 * prev_max)
    # weaker hint: a single scene dropped by half (could be ice, shadow or a real drainage) -> check per scene
    d["hint_drop"] = ((d.get("s2min_2025") < 0.5 * prev_max) | (d.get("s2min_2026") < 0.5 * prev_max)) & ~d["flag_drop"]
    d["flag_fast"] = (d["growth_pct"] > 10) & (d["growth_24_26_km2"] > 2 * d["glo_unc"].fillna(0.02))
    d.round(5).to_csv(os.path.join(a.out, "lake_changes.csv"))

    fig, ax = plt.subplots(figsize=(5.6, 5.4))
    ax.loglog(x[ok], yv[ok], "o", ms=3.5, alpha=0.6, color="#2a9d8f")
    lim = [x[ok].min() * 0.8, x[ok].max() * 1.2]
    ax.plot(lim, lim, "k--", lw=1, label="1:1")
    ax.set_xlabel("GLO area (km²), Rawlins et al."); ax.set_ylabel("this work (km²)")
    ax.set_title(f"Same lakes, 2023-2024 (n={ok.sum()})\nmedian ratio {med:.3f}, median |error| {mape:.1f} %, R² {r2:.3f}", fontsize=10)
    ax.legend(); ax.grid(alpha=0.3, which="both")
    fig.savefig(os.path.join(a.out, "calibration.png"), dpi=110, bbox_inches="tight"); plt.close(fig)

    top = d.dropna(subset=["growth_24_26_km2"]).sort_values("growth_24_26_km2", ascending=False).head(15)
    fig, ax = plt.subplots(figsize=(9, 5.2))
    lab = [f"{i.split('_')[2]}N {i.split('_')[1]}E · {r.basin}" for i, r in top.iterrows()]
    ax.barh(lab[::-1], top.growth_24_26_km2[::-1], color="#e76f51")
    ax.set_xlabel("area gained 2024 → 2026 (km², calibrated)")
    ax.set_title("Fastest-growing glacial lakes, Nepal and transboundary basins (lakes ≥ 0.1 km²)")
    ax.grid(axis="x", alpha=0.3)
    fig.savefig(os.path.join(a.out, "top_growth.png"), dpi=110, bbox_inches="tight"); plt.close(fig)

    # interactive map
    ref = g[(g.AREA_YEAR == 2024) & g.GLO_ID.isin(d.index)].dissolve(by="GLO_ID").reset_index().to_crs("EPSG:4326")[["GLO_ID", "geometry"]]
    ref = ref.merge(d.reset_index(), on="GLO_ID")
    vmax = float(np.nanpercentile(ref.growth_pct.clip(lower=0), 95) or 10)
    cmap = LinearColormap(["#2a9d8f", "#e9c46a", "#e76f51", "#9b2226"], vmin=0, vmax=vmax,
                          caption="lake area change 2024 → 2026 (%)")
    m = folium.Map(location=[float(ref.lat.mean()), float(ref.lon.mean())], zoom_start=7, tiles=None, control_scale=True)
    folium.TileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                     attr="Esri, Maxar, Earthstar Geographics", name="Satellite").add_to(m)
    folium.TileLayer("OpenTopoMap", name="Topographic").add_to(m)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap").add_to(m)

    def popup(r):
        rows = "".join(f"<tr><td>{y}</td><td>{r.get(f'glo_{y}', np.nan):.3f}</td><td>{r.get(f's2_{y}', np.nan):.3f}</td></tr>"
                       for y in range(2017, 2027))
        gm = f"https://www.google.com/maps/@{r.lat},{r.lon},2500m/data=!3m1!1e3"
        flags = " ".join(f for f, v in (("FAST GROWTH", r.flag_fast), ("SUDDEN DROP", r.flag_drop)) if v)
        return (f"<b>{r.GLO_ID}</b><br>{r.basin}, {r.country} · {r.connectivity}<br><b style='color:#9b2226'>{flags}</b>"
                f"<table style='font-size:11px'><tr><th>year</th><th>GLO km²</th><th>this work</th></tr>{rows}</table>"
                f"change 2024→26: {r.growth_pct:+.1f} %<br><a href='{gm}' target='_blank'>Open in Google Maps</a>").replace("nan", "–")

    for _, r in ref.iterrows():
        col = cmap(max(0, min(vmax, r.growth_pct))) if pd.notna(r.growth_pct) else "#888888"
        folium.GeoJson(r.geometry.__geo_interface__, style_function=lambda _, c=col: {"color": c, "weight": 2, "fillColor": c, "fillOpacity": 0.55},
                       tooltip=f"{r.GLO_ID} · {r.growth_pct:+.1f} %", popup=folium.Popup(popup(r), max_width=330)).add_to(m)
    cmap.add_to(m)
    folium.LayerControl().add_to(m)
    m.save(os.path.join(a.out, "lakes_map.html"))
    print(json.dumps({"lakes": int(len(d)), "calib_median_ratio": round(med, 4), "median_abs_err_pct": round(mape, 2), "r2": round(r2, 4),
                      "fast": int(d.flag_fast.sum()), "drop": int(d.flag_drop.sum()), "drop_hint": int(d.hint_drop.sum())}))


if __name__ == "__main__":
    main()
