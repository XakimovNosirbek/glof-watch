#!/usr/bin/env python3
"""Rank the lakes that drain into Uzbekistan by 2018-2026 change, flag drops, add downstream exposure, draw figures.

Inputs: the lake list from uz_select.py (plus optional extra lake lists, e.g. Shakhimardan from uz_catchment_lakes.py),
the annual areas from lakes_batch.py, OpenStreetMap settlements (uz_osm.py), HydroBASINS and Natural Earth.

Per lake: annual area 2018-2026 (normally the maximum of the clear scenes; see robust_annual for years whose scenes
disagree; years with fewer than --min-scenes clear scenes are not used), Theil-Sen trend,
growth = median(2024-2026) - median(2018-2020), and flags:
  FAST GROWTH  growth > 20 % and > 0.01 km2, and the Theil-Sen slope's lower 90 % bound above zero
  DROP         a year below 60 % of the median of the up to 3 previous years, and still below 80 % the year after
               (or in the latest year: unconfirmed). Possible drainage, or ice/shadow: check the scenes.
Exposure: the nearest OSM settlement inside Uzbekistan that lies in the lake's downstream sub-basins (HydroBASINS
level 10, from the Uzbek entry sub-basin to 60 river-km beyond it), with the straight-line distance from the lake.

Usage: uz_report.py --lakes data/uz/lakes.gpkg --extra data/uz/shakhimardan_lakes.gpkg \
                    --areas out/uz/areas.csv out/uz/areas_shakhimardan.csv \
                    --places data/osm/places.json --osm-lakes data/osm/lakes.json --hybas data/hybas/hybas_as_lev10_v1c.shp \
                    --countries data/ne/ne_10m_admin_0_countries.shp --ne-rivers data/ne/ne_10m_rivers_lake_centerlines.shp \
                    --out docs
"""
import argparse
import json
import os

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
import requests
from matplotlib.colors import LightSource, TwoSlopeNorm
from rasterio.windows import from_bounds
from scipy.stats import theilslopes
from shapely.geometry import Point

YEARS = list(range(2018, 2027))
CREDIT = "Contains modified Copernicus Sentinel data (2018-2026) and Copernicus DEM; © OpenStreetMap contributors"
STAC = "https://earth-search.aws.element84.com/v1/search"
os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")


def hav_km(lon1, lat1, lon2, lat2):
    p = np.radians
    a = np.sin(p(lat2 - lat1) / 2) ** 2 + np.cos(p(lat1)) * np.cos(p(lat2)) * np.sin(p(lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * np.arcsin(np.sqrt(a))


def robust_annual(mx, mn, ns, disagree=1.3):
    """Annual value per lake-year. Normally the larger of the clear scenes (annual maximum). When the scenes of a year
    disagree by more than `disagree` (late-October shadow or snow joined to the lake, or partial ice), keep the scene
    value closer to the lake's median over the years whose scenes agree, and mark the year."""
    agree = ~((ns >= 2) & (mx > disagree * mn))
    ref = np.nanmedian(np.where(agree, mx, np.nan)) if agree.any() else np.nanmedian(mx)
    pick_min = ~agree & (np.abs(mn - ref) < np.abs(mx - ref))
    return np.where(pick_min, mn, mx), ~agree


def series_stats(s):
    yrs = np.array([y for y in YEARS if pd.notna(s.get(y))]); v = np.array([s[y] for y in yrs])
    out = {"n_years": len(yrs)}
    if len(yrs) >= 5:
        k, b, lo, hi = theilslopes(v, yrs, alpha=0.90)
        out.update(trend_km2yr=k, trend_lo=lo, trend_pct_yr=100 * k / np.median(v))
    early = [s[y] for y in (2018, 2019, 2020) if pd.notna(s.get(y))]
    late = [s[y] for y in (2024, 2025, 2026) if pd.notna(s.get(y))]
    if early and late:
        out["early_km2"], out["late_km2"] = np.median(early), np.median(late)
        out["growth_km2"] = out["late_km2"] - out["early_km2"]
        out["growth_pct"] = 100 * out["growth_km2"] / out["early_km2"]
    drops, sustained = [], False
    for i, y in enumerate(yrs[1:], 1):
        base = np.median(v[max(0, i - 3):i])                     # median of up to 3 previous years
        if base >= 0.01 and v[i] < 0.6 * base:
            nxt = v[i + 1] if i + 1 < len(v) else None
            tag = "unconfirmed (latest year)" if nxt is None else ("sustained" if nxt < 0.8 * base else "one year only")
            sustained |= tag != "one year only"
            drops.append(f"{y}: {v[i]:.3f} vs {base:.3f} ({tag})")
    out["drop_years"] = "; ".join(drops)
    out["drop_sustained"] = sustained
    return out


def hillshade(bounds, step=0.015):
    """Copernicus DEM 90 m tiles read at ~1 km, for the map background."""
    body = {"collections": ["cop-dem-glo-90"], "bbox": list(bounds), "limit": 200}
    feats = requests.post(STAC, json=body, timeout=120).json()["features"]
    W = int((bounds[2] - bounds[0]) / step); H = int((bounds[3] - bounds[1]) / step)
    dem = np.full((H, W), np.nan, "float32")
    for f in feats:
        with rasterio.open(f["assets"]["data"]["href"]) as s:
            l, b, r, t = s.bounds
            x0, x1 = max(l, bounds[0]), min(r, bounds[2]); y0, y1 = max(b, bounds[1]), min(t, bounds[3])
            if x1 <= x0 or y1 <= y0:
                continue
            c0, c1 = int(round((x0 - bounds[0]) / step)), int(round((x1 - bounds[0]) / step))
            r0, r1 = int(round((bounds[3] - y1) / step)), int(round((bounds[3] - y0) / step))
            c1, r1 = min(c1, W), min(r1, H)
            if c1 <= c0 or r1 <= r0:
                continue
            a = s.read(1, window=from_bounds(x0, y0, x1, y1, s.transform), out_shape=(r1 - r0, c1 - c0))
            dem[r0:r1, c0:c1] = a
    return dem


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--areas", nargs="+", required=True, help="one or more lakes_batch.py CSVs")
    for k in ("lakes", "places", "osm_lakes", "hybas", "countries", "ne_rivers", "out"):
        ap.add_argument("--" + k.replace("_", "-"), required=k not in ("osm_lakes",))
    ap.add_argument("--extra", nargs="*", default=[])
    ap.add_argument("--himag", help="Hi-MAG 2017 shapefile (Chen et al. 2021): adds lake type and a 2017 area")
    ap.add_argument("--min-scenes", type=int, default=2)
    a = ap.parse_args()
    img, dat = os.path.join(a.out, "img"), os.path.join(a.out, "data")
    os.makedirs(img, exist_ok=True); os.makedirs(dat, exist_ok=True)

    lakes = pd.concat([gpd.read_file(p) for p in [a.lakes] + a.extra], ignore_index=True).drop_duplicates("GLO_ID")
    lakes = gpd.GeoDataFrame(lakes, crs="EPSG:4326")
    ar = pd.concat([pd.read_csv(p) for p in a.areas], ignore_index=True)
    ar = ar[pd.to_numeric(ar.area_km2, errors="coerce").notna()].drop_duplicates(["GLO_ID", "year"])
    ok = ar[ar.n_scenes >= a.min_scenes]
    mx = ok.pivot_table(index="GLO_ID", columns="year", values="area_km2").reindex(columns=YEARS)
    mnp = ok.pivot_table(index="GLO_ID", columns="year", values="min_km2").reindex(columns=YEARS)
    nsp = ok.pivot_table(index="GLO_ID", columns="year", values="n_scenes").reindex(columns=YEARS)
    piv = mx.copy(); disagree = {}
    for g in mx.index:
        val, bad = robust_annual(mx.loc[g].values, mnp.loc[g].values, nsp.loc[g].values)
        piv.loc[g] = val
        disagree[g] = ",".join(str(y) for y, b_ in zip(YEARS, bad) if b_ and not np.isnan(mx.loc[g, y]))
    nsc = ar.pivot_table(index="GLO_ID", columns="year", values="n_scenes")
    mins = ar.pivot_table(index="GLO_ID", columns="year", values="min_km2")
    st = pd.DataFrame({g: series_stats(piv.loc[g]) for g in piv.index}).T
    d = lakes.set_index("GLO_ID").join(piv).join(st)
    d["scenes_disagree"] = pd.Series(disagree)

    if a.himag:
        from shapely.geometry import box
        hm = gpd.read_file(a.himag, bbox=gpd.GeoSeries([box(*lakes.total_bounds)], crs="EPSG:4326")).to_crs("EPSG:4326")
        j = gpd.sjoin(lakes[["GLO_ID", "geometry"]], hm[["GL_Type", "GL_Area", "geometry"]], predicate="intersects")
        j = j.sort_values("GL_Area", ascending=False).drop_duplicates("GLO_ID").set_index("GLO_ID")
        d["himag_type"] = j.GL_Type
        d["himag_2017_km2"] = j.GL_Area / 1e6

    # names from OSM water bodies within 400 m of the lake point
    d["name"] = ""
    if a.osm_lakes and os.path.exists(a.osm_lakes):
        el = json.load(open(a.osm_lakes))["elements"]
        ol = pd.DataFrame([{"name": e["tags"].get("name:en") or e["tags"].get("name"), "lon": e["center"]["lon"],
                            "lat": e["center"]["lat"]} for e in el if "center" in e])
        for g, r in d.iterrows():
            dk = hav_km(r.LONGITUDE, r.LATITUDE, ol.lon.values, ol.lat.values)
            rad = max(0.4, 1.2 * np.sqrt(r.AREA / np.pi))
            if dk.min() <= rad:
                d.at[g, "name"] = ol.name.iloc[int(dk.argmin())]

    # downstream exposure
    hb = gpd.read_file(a.hybas, bbox=(55, 35, 78, 47))
    nxt = dict(zip(hb.HYBAS_ID, hb.NEXT_DOWN)); dist = dict(zip(hb.HYBAS_ID, hb.DIST_MAIN))
    ne = gpd.read_file(a.countries)
    uz = ne[ne.ADM0_A3 == "UZB"].geometry.union_all()
    pl = json.load(open(a.places))["elements"]
    P = gpd.GeoDataFrame([{"pname": e["tags"].get("name:en") or e["tags"].get("name"), "pname_local": e["tags"].get("name"),
                           "place": e["tags"].get("place"), "geometry": Point(e["lon"], e["lat"])} for e in pl], crs="EPSG:4326")
    P = P[P.within(uz)]
    P = gpd.sjoin(P, hb[["HYBAS_ID", "geometry"]], predicate="within")
    for g, r in d.iterrows():
        if pd.isna(r.get("UZ_ENTRY_HYBAS")) or not r.get("UZ_ENTRY_HYBAS"):
            continue
        e = int(r.UZ_ENTRY_HYBAS)
        down = [int(x) for x in str(r.DOWNSTREAM_HYBAS).split(",") if x]
        h = e
        while nxt.get(h, 0) and dist[e] - dist[nxt[h]] <= 60:
            h = nxt[h]; down.append(h)
        cand = P[P.HYBAS_ID.isin(down)]
        if cand.empty:
            continue
        dk = hav_km(r.LONGITUDE, r.LATITUDE, cand.geometry.x.values, cand.geometry.y.values)
        i = int(dk.argmin())
        d.at[g, "settlement"] = cand.pname.iloc[i]
        d.at[g, "settlement_type"] = cand.place.iloc[i]
        d.at[g, "settlement_km"] = round(float(dk[i]), 1)
        d.at[g, "settlement_lon"], d.at[g, "settlement_lat"] = cand.geometry.x.iloc[i], cand.geometry.y.iloc[i]

    d["flag_fast"] = (d.growth_pct > 20) & (d.growth_km2 > 0.01) & (pd.to_numeric(d.trend_lo, errors="coerce") > 0)
    d["flag_drop"] = d.drop_sustained.fillna(False).astype(bool)
    # compare with the reference inventory where it is Kumar & Vijay (2022-2024 median outlines)
    ref = d[d.AREA_YEAR == 2023]
    ours = ref[[2022, 2023, 2024]].median(axis=1)
    okc = ours.notna() & (ref.AREA > 0)
    ratio = (ours[okc] / ref.AREA[okc])
    cal = {"n": int(okc.sum()), "median_ratio": float(ratio.median()),
           "median_abs_err_pct": float((abs(ours[okc] - ref.AREA[okc]) / ref.AREA[okc]).median() * 100),
           "r2": float(np.corrcoef(ref.AREA[okc], ours[okc])[0, 1] ** 2)}

    hm_ok = d.himag_2017_km2.notna() & d[2018].notna() if "himag_2017_km2" in d else pd.Series(False, index=d.index)
    if hm_ok.any():
        rr = d[2018][hm_ok] / d.himag_2017_km2[hm_ok]
        cal["himag_n"], cal["himag_median_ratio_2018_vs_2017"] = int(hm_ok.sum()), float(rr.median())
        cal["himag_median_abs_err_pct"] = float((abs(rr - 1)).median() * 100)
    cols = ["name", "BASIN", "COUNTRY", "LATITUDE", "LONGITUDE", "ELEV_M", "AREA", "AREA_YEAR"] + YEARS + \
           ["n_years", "trend_km2yr", "trend_pct_yr", "early_km2", "late_km2", "growth_km2", "growth_pct", "drop_years",
            "flag_fast", "flag_drop", "scenes_disagree", "himag_type", "himag_2017_km2", "RIVER_KM_TO_UZ", "settlement", "settlement_type", "settlement_km", "INV_ID"]
    out = d[[c for c in cols if c in d.columns]].copy()
    out = out.rename(columns={"AREA": "ref_area_km2", "AREA_YEAR": "ref_year", **{y: f"s2_{y}" for y in YEARS}})
    for c in out.columns:
        if out[c].dtype == object:
            try:
                out[c] = pd.to_numeric(out[c])
            except (ValueError, TypeError):
                pass
    out = out.sort_values("growth_km2", ascending=False)
    out.round(4).to_csv(os.path.join(dat, "uzbekistan_lakes.csv"))
    nsc.to_csv(os.path.join(dat, "uzbekistan_scene_counts.csv"))

    # ---- figures ----
    label = lambda g, r: (r["name"] or f"{r.LATITUDE:.3f}N {r.LONGITUDE:.3f}E") + f" · {r.BASIN}"
    # 1) map
    bounds = (66.4, 38.3, 73.2, 42.6)
    cache = os.path.join(os.path.dirname(a.lakes), "map_dem.npy")
    if os.path.exists(cache):
        dem = np.load(cache)
    else:
        dem = hillshade(bounds); np.save(cache, dem)
    ls = LightSource(azdeg=315, altdeg=40)
    fig, ax = plt.subplots(figsize=(13, 8.6))
    ax.imshow(ls.hillshade(np.nan_to_num(dem, nan=0), vert_exag=0.02, dx=1000, dy=1000), cmap="gray", alpha=0.55,
              extent=(bounds[0], bounds[2], bounds[1], bounds[3]), zorder=0)
    hi = np.ma.masked_less(dem, 2500)
    ax.imshow(hi, cmap="Blues_r", alpha=0.18, extent=(bounds[0], bounds[2], bounds[1], bounds[3]), zorder=1)
    nb = ne.cx[bounds[0]:bounds[2], bounds[1]:bounds[3]]
    nb.boundary.plot(ax=ax, color="#555", lw=0.8, zorder=2)
    gpd.GeoSeries([uz]).boundary.plot(ax=ax, color="#c1121f", lw=1.6, zorder=3)
    rv = gpd.read_file(a.ne_rivers, bbox=bounds)
    rv.plot(ax=ax, color="#2b6cb0", lw=0.9, alpha=0.8, zorder=2)
    dd = d[d.n_years.fillna(0) >= 5].copy()
    tr = pd.to_numeric(dd.trend_pct_yr, errors="coerce").clip(-8, 8)
    sc = ax.scatter(dd.LONGITUDE, dd.LATITUDE, s=12 + 260 * np.sqrt(dd.AREA.astype(float)), c=tr, cmap="RdBu_r",
                    norm=TwoSlopeNorm(0, -8, 8), edgecolor="k", lw=0.5, zorder=5)
    fig.colorbar(sc, ax=ax, shrink=0.6, label="area trend 2018-2026 (% per year, Theil-Sen)")
    top = out[out.flag_fast | out.flag_drop].head(10)
    for g, r in top.iterrows():
        ax.annotate(f"{(r['name'] or g.split('_')[2] + 'N ' + g.split('_')[1] + 'E')}", (r.LONGITUDE, r.LATITUDE), xytext=(6, 6),
                    textcoords="offset points", fontsize=7.5, zorder=6,
                    bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.75))
    st_ = out.dropna(subset=["settlement"]).loc[top.index.intersection(out.dropna(subset=["settlement"]).index)]
    for g, r in st_.iterrows():
        s = d.loc[g]
        ax.plot([s.LONGITUDE, s.settlement_lon], [s.LATITUDE, s.settlement_lat], ":", color="#9b2226", lw=1, zorder=4)
        ax.plot(s.settlement_lon, s.settlement_lat, "s", ms=4, color="#9b2226", zorder=6)
    for nm, (lo, la) in {"Tashkent": (69.28, 41.31), "Samarkand": (66.97, 39.65), "Fergana": (71.78, 40.39),
                         "Andijan": (72.34, 40.78), "Namangan": (71.67, 41.0), "Termez": (67.28, 37.22),
                         "Shakhimardan": (71.72, 39.98)}.items():
        if bounds[1] < la < bounds[3]:
            ax.plot(lo, la, "o", ms=3, color="k", zorder=6); ax.text(lo + 0.04, la - 0.08, nm, fontsize=8, zorder=6)
    ax.set_xlabel("longitude (°E)"); ax.set_ylabel("latitude (°N)")
    ax.set_xlim(bounds[0], bounds[2]); ax.set_ylim(bounds[1], bounds[3]); ax.set_aspect(1 / np.cos(np.radians(40.4)))
    ax.set_title(f"Glacial lakes draining into Uzbekistan ({len(dd)} lakes with ≥ 5 clear years): area trend 2018-2026\n"
                 "red line = Uzbekistan border; squares = nearest downstream Uzbek settlement of flagged lakes", fontsize=11)
    ax.text(0.99, 0.01, CREDIT + "; borders Natural Earth", transform=ax.transAxes, ha="right", fontsize=6.5, color="#333")
    fig.savefig(os.path.join(img, "uzbekistan_map.png"), dpi=110, bbox_inches="tight"); plt.close(fig)

    # 2) growth bars
    g15 = out.dropna(subset=["growth_km2"]).head(15)
    fig, ax = plt.subplots(figsize=(9.5, 5.6))
    cols_ = ["#9b2226" if f else "#e9c46a" for f in g15.flag_fast]
    ax.barh([label(g, r) for g, r in g15.iterrows()][::-1], g15.growth_km2[::-1], color=cols_[::-1])
    for i, (g, r) in enumerate(list(g15.iterrows())[::-1]):
        ax.text(r.growth_km2, i, f"  {r.growth_pct:+.0f} %", va="center", fontsize=8)
    ax.set_xlabel("area gained, mean 2018-2020 → mean 2024-2026 (km²)")
    ax.set_title("Fastest-growing lakes draining into Uzbekistan (dark red = FAST GROWTH flag)")
    ax.grid(axis="x", alpha=0.3)
    fig.savefig(os.path.join(img, "uzbekistan_growth.png"), dpi=110, bbox_inches="tight"); plt.close(fig)

    # 3) series of flagged and Shakhimardan lakes
    pick = list(out[out.flag_fast].head(6).index) + list(out[out.flag_drop].head(3).index)
    pick += [g for g in out[out.BASIN == "Shakhimardan"].index[:3]]
    pick = list(dict.fromkeys(pick))[:12]
    n = len(pick); nc = 3; nr = int(np.ceil(n / nc))
    fig, axs = plt.subplots(nr, nc, figsize=(13, 2.9 * nr), squeeze=False)
    for axx, g in zip(axs.flat, pick):
        r = out.loc[g]
        ys = [y for y in YEARS if pd.notna(r.get(f"s2_{y}"))]
        axx.plot(ys, [r[f"s2_{y}"] for y in ys], "o-", color="#023047", ms=4)
        if g in mins.index:
            mm = mins.loc[g]
            axx.plot(mm.index, mm.values, "v", color="#8ecae6", ms=4, label="smallest clear scene")
        axx.axhline(r.ref_area_km2, color="#fb8500", ls="--", lw=1, label="inventory outline")
        fl = ("FAST " if r.flag_fast else "") + ("DROP" if r.flag_drop else "")
        axx.set_title(f"{label(g, r)}\n{fl}", fontsize=8.5, color="#9b2226" if fl else "k")
        axx.grid(alpha=0.3); axx.set_xlim(2017.5, 2026.5); axx.tick_params(labelsize=7)
    for axx in list(axs.flat)[n:]:
        axx.axis("off")
    axs.flat[0].legend(fontsize=7)
    fig.supylabel("annual max area, Sep-Oct (km²)", fontsize=9)
    fig.tight_layout()
    fig.savefig(os.path.join(img, "uzbekistan_series.png"), dpi=105, bbox_inches="tight"); plt.close(fig)

    # 4) agreement with the inventory
    fig, ax = plt.subplots(figsize=(5.4, 5.2))
    ax.loglog(ref.AREA[okc], ours[okc], "o", ms=4, alpha=0.7, color="#2a9d8f")
    lim = [ref.AREA[okc].min() * 0.7, ref.AREA[okc].max() * 1.3]
    ax.plot(lim, lim, "k--", lw=1, label="1:1")
    ax.set_xlabel("Kumar & Vijay 2025, 2022-2024 median outline (km²)"); ax.set_ylabel("this work, median of 2022-2024 maxima (km²)")
    ax.set_title(f"Same lakes (n={cal['n']}): median ratio {cal['median_ratio']:.2f},\n"
                 f"median |difference| {cal['median_abs_err_pct']:.0f} %, R² {cal['r2']:.2f}", fontsize=10)
    ax.legend(); ax.grid(alpha=0.3, which="both")
    fig.savefig(os.path.join(img, "uzbekistan_vs_inventory.png"), dpi=110, bbox_inches="tight"); plt.close(fig)

    print(json.dumps({"lakes": int(len(d)), "with_series": int((d.n_years.fillna(0) >= 5).sum()), "calibration": cal,
                      "fast": int(out.flag_fast.sum()), "drop": int(out.flag_drop.sum())}, indent=1))


if __name__ == "__main__":
    main()
