#!/usr/bin/env python3
"""Select glacial lakes whose outflow reaches Uzbekistan, and write them in the GLO-like layout lakes_batch.py reads.

Lakes come from a published inventory (default: Kumar & Vijay 2025, HMA glacial lakes, 2022-2024 median outlines,
CC BY 4.0, doi:10.5281/zenodo.17948783). The downstream path of each lake is followed through HydroBASINS level 10
(NEXT_DOWN links) until the first sub-basin that lies mostly inside Uzbekistan (Natural Earth 10 m border, enclaves
included). The river distance to Uzbekistan is the difference in HydroBASINS DIST_MAIN between the two sub-basins.
The basin label is the first named target river (OpenStreetMap waterway=river lines) met along that path.

Usage: uz_select.py --lakes data/kumar2025/Glacial_lakes_2022_2024_median.shp --hybas data/hybas/hybas_as_lev10_v1c.shp \
                    --countries data/ne/ne_10m_admin_0_countries.shp --rivers data/osm/rivers.json \
                    --min-area 0.05 --max-river-km 200 --out data/uz/lakes.gpkg
"""
import argparse
import json
import os
import re

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, box as shp_box

BBOX = (66.5, 38.0, 73.0, 42.5)
# basin label -> (regex on OSM name / name:en / name:ru / name:uz / name:ky / name:tg, optional lon0,lat0,lon1,lat1 box)
TARGETS = {
    "Pskem": (r"пскем|pskem|oygaing|ойгаинг|майдантал|maydontol|maidantal", None),
    "Ugam": (r"угам|ugam", None),
    "Koksu": (r"коксу|koksu|ko'ksuv", (69.8, 41.2, 70.8, 41.9)),      # Tashkent-region Koksu, not the Alay Kok-Suu
    "Chatkal": (r"чаткал|chatkal|chotqol", None),
    "Akhangaran": (r"ахангаран|ohangaron|ahangaran|akhangaran", None),
    "Shakhimardan": (r"шахимардан|шохимардон|shakhimardan|shahimardan|shohimardon", None),
    "Sokh": (r"^(сох|sokh|so'x|soh)$", None),
    "Isfayram": (r"исфайрам|isfayram|isfairam", None),
    "Isfara": (r"исфара|isfara", None),
    "Tupalang": (r"тупаланг|tupalang|to'polon", None),
    "Sangardak": (r"сангардак|sangardak", None),
    "Surkhandarya": (r"сурхандар|surxondaryo|surkhandar", None),
    "Kashkadarya": (r"кашкадар|qashqadaryo|kashkadar|яккабаг|yakkabog|танхыз|tanxiz|tankhaz|танхаз", None),
    "Zeravshan": (r"зеравшан|zarafshon|zeravshan|zarafshan|фандарья|fan darya|фондарё|ягноб|yaghnob|yagnob|искандер|iskander"
                  r"|кштут|kshtut|магиан|magian", None),
    "Kara-Darya": (r"карадар|qoradaryo|kara-darya|karadarya|кара-кулджа|kara-kulja", None),
    "Naryn": (r"нарын|naryn|norin", None),
}


def chain(hid, nxt, limit=400):
    out = [hid]
    while nxt.get(out[-1], 0) and len(out) < limit:
        out.append(nxt[out[-1]])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lakes", required=True)
    ap.add_argument("--hybas", required=True)
    ap.add_argument("--countries", required=True)
    ap.add_argument("--rivers", required=True, help="Overpass JSON of waterway=river ways with geometry")
    ap.add_argument("--min-area", type=float, default=0.05)
    ap.add_argument("--max-river-km", type=float, default=200)
    ap.add_argument("--small-basins", default="Shakhimardan", help="basins where smaller lakes are kept too (comma list)")
    ap.add_argument("--small-min", type=float, default=0.01)
    ap.add_argument("--min-elev", type=float, default=2500, help="lower lakes are mostly landslide-dammed, not glacial")
    ap.add_argument("--exclude-basins", default="Naryn", help="e.g. Naryn: a flood would reach Uzbekistan only through Toktogul")
    ap.add_argument("--ref-year", type=int, default=2023, help="year the reference outlines stand for")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.out), exist_ok=True)

    lakes = gpd.read_file(a.lakes, bbox=BBOX).to_crs("EPSG:4326")
    lakes["AREA"] = lakes.to_crs("EPSG:6933").area / 1e6                 # equal-area km2
    if "ID" not in lakes:                                                   # lake maps from uz_catchment_lakes.py
        lakes["ID"], lakes["Lake_Elev"] = lakes["GLO_ID"], lakes["ELEV_M"]
    lakes = lakes.drop(columns=[c for c in ("GLO_ID", "ELEV_M", "AREA_YEAR", "BASIN", "COUNTRY", "CONNECTIVITY",
                                            "LONGITUDE", "LATITUDE") if c in lakes])
    lakes = lakes[lakes.AREA >= min(a.min_area, a.small_min)].copy()
    rp = lakes.geometry.representative_point()
    lakes["LONGITUDE"], lakes["LATITUDE"] = rp.x.round(5), rp.y.round(5)
    print(f"{len(lakes)} inventory lakes >= {min(a.min_area, a.small_min)} km2 in bbox", flush=True)

    hb = gpd.read_file(a.hybas, bbox=(55, 35, 78, 47))
    nxt = dict(zip(hb.HYBAS_ID, hb.NEXT_DOWN))
    dist = dict(zip(hb.HYBAS_ID, hb.DIST_MAIN))
    ne = gpd.read_file(a.countries)
    uz = ne[ne.ADM0_A3 == "UZB"].geometry.union_all()
    # a sub-basin counts as "in Uzbekistan" when > 30 % or > 10 km2 of it is inside (enclaves: Shakhimardan, Sokh)
    hb_m = hb.to_crs("EPSG:6933")
    uz_m = gpd.GeoSeries([uz], crs="EPSG:4326").to_crs("EPSG:6933").iloc[0]
    inside = hb_m.intersection(uz_m).area
    in_uz = set(hb.HYBAS_ID[(inside / hb_m.area > 0.3) | (inside > 10e6)])

    # OSM target rivers -> which sub-basins each crosses
    ways = json.load(open(a.rivers))["elements"]
    riv, allr = [], []
    for w in ways:
        t = w.get("tags", {})
        if len(w.get("geometry", [])) < 2:
            continue
        line = LineString([(p["lon"], p["lat"]) for p in w["geometry"]])
        names = [str(t.get(k, "")).strip().lower() for k in ("name", "name:en", "name:ru", "name:uz", "name:ky", "name:tg")]
        allr.append({"name": t.get("name:en") or t.get("name"), "geometry": line})
        for k, (rx, box) in TARGETS.items():
            if any(n and re.search(rx, n) for n in names):
                if box is None or line.intersects(shp_box(*box)):
                    riv.append({"label": k.strip(), "geometry": line})
                break
    riv = gpd.GeoDataFrame(riv, crs="EPSG:4326")
    allr = gpd.GeoDataFrame(allr, crs="EPSG:4326")
    hit = gpd.sjoin(hb[["HYBAS_ID", "geometry"]], riv, predicate="intersects")
    labels_by_basin = hit.groupby("HYBAS_ID")["label"].agg(lambda s: sorted(set(s)))
    any_hit = gpd.sjoin(hb[["HYBAS_ID", "geometry"]], allr, predicate="intersects")
    names_by_basin = any_hit.groupby("HYBAS_ID")["name"].agg(lambda s: set(s.dropna()))
    print(f"{len(riv)} OSM target river ways, crossing {len(labels_by_basin)} sub-basins", flush=True)

    j = gpd.sjoin(gpd.GeoDataFrame(lakes.drop(columns="geometry"), geometry=rp, crs="EPSG:4326"),
                  hb[["HYBAS_ID", "geometry"]], predicate="within", how="left")
    lakes["HYBAS_ID"] = j["HYBAS_ID"].values
    ctry = gpd.sjoin(gpd.GeoDataFrame(geometry=rp, crs="EPSG:4326"), ne[["NAME", "geometry"]], predicate="within", how="left")
    lakes["COUNTRY"] = ctry.groupby(level=0)["NAME"].first().reindex(lakes.index).values

    rows = []
    for i, r in lakes.iterrows():
        if pd.isna(r.HYBAS_ID):
            rows.append((None, np.nan, None, "")); continue
        c = chain(int(r.HYBAS_ID), nxt)
        entry = next((h for h in c if h in in_uz), None)
        km = (dist[c[0]] - dist[entry]) if entry else np.nan
        lab = ""
        # look a few sub-basins past the entry too: an enclave sub-basin can sit above the named main stem
        for h in c[: c.index(entry) + 4 if entry else len(c)]:
            if h in labels_by_basin.index:
                ls = labels_by_basin[h]
                # the most upstream (most specific) target river wins; tributary names beat main stems
                pri = [k.strip() for k in TARGETS]
                lab = sorted(ls, key=lambda x: pri.index(x))[0] if len(ls) > 1 else ls[0]
                break
        if not lab and entry and len(c) > 1:
            # unlabelled: name the OSM river that crosses into Uzbekistan (shared by the last two sub-basins);
            # for a lake already inside Uzbekistan, the river leaving its own sub-basin
            k = max(c.index(entry), 1)
            both = names_by_basin.get(c[k - 1], set()) & names_by_basin.get(c[k], set())
            lab = "other: " + "/".join(sorted(both)[:2]) if both else ""
        down = c[: c.index(entry) + 1] if entry else []
        rows.append((entry, km, ",".join(map(str, down)), lab))
    lakes["UZ_ENTRY_HYBAS"], lakes["RIVER_KM_TO_UZ"], lakes["DOWNSTREAM_HYBAS"], lakes["BASIN"] = zip(*rows)
    small = lakes.BASIN.isin(a.small_basins.split(","))
    cand = lakes.RIVER_KM_TO_UZ.notna() & (lakes.RIVER_KM_TO_UZ <= a.max_river_km) \
        & ((lakes.AREA >= a.min_area) | (small & (lakes.AREA >= a.small_min)))
    keep_ok = (lakes.Lake_Elev >= a.min_elev) & ~lakes.BASIN.isin(a.exclude_basins.split(","))
    excl = lakes[cand & ~keep_ok]
    excl[["ID", "Lake_Elev", "AREA", "BASIN", "COUNTRY", "LONGITUDE", "LATITUDE", "RIVER_KM_TO_UZ"]].round(4).to_csv(
        a.out.replace(".gpkg", "_excluded.csv"), index=False)
    print(f"excluded {len(excl)} lakes (below {a.min_elev:.0f} m or in {a.exclude_basins})")
    sel = lakes[cand & keep_ok].copy()
    sel["BASIN"] = sel.BASIN.replace("", "other")
    sel["GLO_ID"] = [f"UZL_{lo:.5f}_{la:.5f}" for lo, la in zip(sel.LONGITUDE, sel.LATITUDE)]
    sel["AREA_YEAR"] = a.ref_year                                           # Kumar & Vijay: 2022-2024 median outline
    sel["CONNECTIVITY"] = ""
    sel["INV_ID"], sel["ELEV_M"] = sel["ID"], sel["Lake_Elev"]
    keep = ["GLO_ID", "INV_ID", "ELEV_M", "AREA_YEAR", "AREA", "BASIN", "COUNTRY", "CONNECTIVITY", "LONGITUDE", "LATITUDE",
            "RIVER_KM_TO_UZ", "UZ_ENTRY_HYBAS", "DOWNSTREAM_HYBAS", "HYBAS_ID", "geometry"]
    sel = sel[keep].sort_values("AREA", ascending=False)
    sel.to_file(a.out, driver="GPKG")
    print(sel.groupby(["BASIN"]).agg(n=("AREA", "size"), km2=("AREA", "sum"), max_km2=("AREA", "max"),
                                     river_km=("RIVER_KM_TO_UZ", "median")).round(3).to_string())
    print(f"-> {a.out}: {len(sel)} lakes")


if __name__ == "__main__":
    main()
