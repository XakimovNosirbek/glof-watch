# Automated lake monitoring

`scripts/monitor.py` checks every lake in [`watchlist.json`](../watchlist.json) against its recent Sentinel-2 history.
A GitHub Actions workflow ([`.github/workflows/monitor.yml`](../.github/workflows/monitor.yml)) runs it every Monday
at 03:00 UTC and commits [`status/STATUS.md`](../status/STATUS.md) and `status/latest.json`.

## Rules

All clear Sentinel-2 scenes from the last 120 days are used. A scene is dropped when more than 30 % of the lake's seed
box is in the L2A snow/ice class (a frozen lake).

| Status | Condition |
|---|---|
| 🔴 DRAINAGE | **both** of the last 2 clear scenes < 50 % of the median of the earlier scenes |
| ⚠️ POSSIBLE DRAINAGE | only the latest scene is below 40 % of the earlier median; wait for the next clear scene |
| 🟠 GROWTH | **both** of the last 2 clear scenes > 125 % of the earlier median |
| 🟢 OK | none of the above, with at least 4 clear scenes |
| ⚪ NO DATA | fewer than 4 clear scenes (cloud, polar night, frozen lake) |

A single scene is never enough to declare a drainage. The back-tests below show why: ice, shadow and thin cloud make
one-scene spikes and dips (see Shisper Oct 2022 and spring 2024 at lower Ngole in [RESULTS.md](RESULTS.md)).

## Back-test: the monitor run "as of" dates before and after three real outbursts

| Lake | As of | Status | Recent / baseline (km²) | What really happened |
|---|---|---|---|---|
| Shisper ice-dammed lake | 2022-04-25 | 🟠 GROWTH | 0.230 / 0.150 | lake filling; it burst on 7 May 2022 (**12 days of lead time**) |
| Shisper ice-dammed lake | 2022-05-15 | ⚠️ POSSIBLE DRAINAGE (last scene 0.01) | 0.065 / 0.220 | outburst 7 May 2022, Hassanabad bridge destroyed; confirmed by the next clear scene (25 May, 0.00) |
| Lower Ngole (Thame) | 2024-08-10 | 🟢 OK | 0.052 / 0.044 | no false alarm before the flood |
| Lower Ngole (Thame) | 2024-09-20 | 🔴 DRAINAGE | 0.017 / 0.052 | outburst 16 Aug 2024, Thame village destroyed |
| Merzbacher ice-dammed lake | 2025-06-30 | 🟠 GROWTH | 1.742 / 0.304 | lake filling |
| Merzbacher ice-dammed lake | 2025-07-25 | ⚠️ POSSIBLE DRAINAGE | 1.290 / 0.838 (last scene 0.20) | drained between 2 and 22 July 2025 |

The files are in [`status/`](../status/) (`backtest_<date>.json/.md`). They were first written with the earlier
one-scene rules; the two statuses that the "both scenes" rule changes (Shisper 2022-05-15, Merzbacher 2025-07-25)
were recomputed from the stored scene series. A live re-run of the 2022-04-25 back-test on 2026-11-25 reproduced the
stored scene series exactly.

Run one yourself:

```bash
cd scripts
../.venv/bin/python monitor.py --watchlist ../watchlist.json --out ../status --as-of 2024-09-20 --only lower_ngole
```

## Why "both scenes"

The first live run (12 Nov 2026) reported GROWTH +261 % for Merzbacher because one bright scene (25 Oct 2026, 0.85 km²
after a month of 0.04-0.16 km²) dominated a median of two. Requiring both latest scenes to cross the threshold removed
that false alarm, and every back-test outbreak is still flagged. The Merzbacher series also shows a fall from 0.54 to
0.11 km² between 24 and 29 Aug 2026. That may be a 2026 drainage, but it is not verified.

## Limits

- Optical only for now. Monsoon cloud delayed Thame's detection by 23 days, while Sentinel-1 radar saw it after 6 days
  (`scripts/s1_lake.py`, [RESULTS.md §2](RESULTS.md)). Radar did not help on ice-choked Merzbacher. Radar is the next
  input to add.
- The GROWTH warning before Shisper 2022 is a single back-test. Lead time for moraine-dammed lakes (Thame) was zero:
  nothing changed before the flood.
- Four lakes are on the watchlist so far. The 296-lake Nepal run ([RESULTS.md §5](RESULTS.md)) is not connected to it yet.
