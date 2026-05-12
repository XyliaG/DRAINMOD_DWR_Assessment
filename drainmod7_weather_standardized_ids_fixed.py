# gee2_gridded_county_GRIDMET_weatherforDRAINMOD7_native_style.py
#
# Builds DRAINMOD 7.0 weather inputs from GRIDMET using the same record layout
# style as the native Larson example files.
#
# Outputs per area:
#   - stations_<area>_<start>_<end>.csv
#   - optional daily_weather_<area>_<start>_<end>.csv
#   - TEM/<file_root>Temp<yy0>_<yy1>.TEM
#   - RAI/<file_root>Rain<yy0>_<yy1>.RAI
#   - RAD/<file_root>Rad<yy0>_<yy1>.RAD
#   - optional PET/<file_root>PET<yy0>_<yy1>.PET
#
# This version supports:
#   1. One county interactively
#   2. A list of counties from a CSV
#
# County list CSV:
#   required column: county_name or county
#   optional columns:
#       state_abbr
#       output_tag
#       limit_to_first_pixel
#
# Native-style layout changes relative to the previous script:
#   - TEM line 2 only writes the remaining actual days of the month; it does not
#     pad missing days with 0 0 pairs.
#   - RAD line 2 only writes the remaining actual days of the month; it does not
#     pad missing days with trailing zeros.
#   - RAI monthly lines use the native 14-character prefix:
#         station6 + space + year + month + space
#     There is no "L" after station6 in the 7.0 Larson example.
#   - RAI event blocks remain 8 characters wide:
#         DDHHAAAA  -> day(2), hour(2), amount(4)
#
# Notes:
#   - RAI uses daily GRIDMET precipitation and distributes each wet day over a
#     fixed within-day window.
#   - TEM follows the 2-line-per-month DRAINMOD pattern seen in the Larson file.
#   - RAD follows the 2-line-per-month DRAINMOD pattern seen in the Larson file.
#   - RAD is written from GRIDMET shortwave radiation converted to MJ m-2 d-1 and
#     then scaled by a user-selected multiplier (default 10), which is intended
#     to match the magnitude/layout of the Larson example.
#   - PET follows the native PET examples you uploaded. The monthly prefix begins
#     with a 6-character station/site code, then year and month. It is not a
#     universal constant like 12. Evans.PET uses station code 12, Minn.PET uses 29,
#     Plymouth uses 320000, and test999 uses 999999.
#   - PET is written from GRIDMET grass-reference ET (eto) converted from mm/day to
#     hundredths of inches/day and rounded to integers, which matches the common
#     integer PET style in the uploaded examples.

from __future__ import annotations

import calendar
import datetime as dt
import os
import re
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from multiprocessing import freeze_support
from pathlib import Path
from typing import Iterable, Optional

import ee
import pandas as pd
from tqdm import tqdm


PROJECT = "drainmod-gridmet"
DEFAULT_STATE_ABBR = "IL"
DEFAULT_SINGLE_COUNTY = "Champaign"

START = "1996-01-01"   # inclusive
END = "2026-01-01"     # exclusive

ELEV_FROM_SRTM = True
ASK_OUTPUT_DIR = True

EVENT_START_HOUR = 17
EVENT_DURATION_HOURS = 5
RAIN_DISTRIBUTION = "uniform"

DEFAULT_LIMIT_TO_FIRST_PIXEL = False
DEFAULT_WRITE_DAILY_CSV = False
DEFAULT_USE_PARALLEL_WRITING = True
DEFAULT_MAX_EE_FEATURES = 4500
DEFAULT_MAX_DAYS_PER_BATCH = 365
DEFAULT_RAD_MULTIPLIER = 10
DEFAULT_WRITE_PET = True

# File naming choices
DEFAULT_FILE_ROOT_MODE = "pixel_id"   # pixel_id, pixel_label, station6, or pixel_station
DEFAULT_STATION_ID_MODE = "station6"     # station6 or fixed
DEFAULT_FIXED_STATION_ID = "000001"

SRAD_WM2_TO_MJ = 0.0864
MM_TO_IN = 0.03937007874015748
K_TO_F_SCALE = 9.0 / 5.0
K_TO_F_OFFSET = -459.67

STATE_FIPS_MAP = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56",
}


@dataclass
class AreaTarget:
    county_name: str
    state_abbr: str
    output_tag: str
    limit_to_first_pixel: bool


def resolve_base() -> Path:
    env = os.getenv("DRAINMOD_GRID_BASE", "").strip() or os.getenv("DSSAT_GRID_BASE", "").strip()
    if env:
        p = Path(env).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    if os.name == "nt":
        e_root = Path("E:/")
        if e_root.exists():
            p = e_root / "DRAINMOD_GRID"
            p.mkdir(parents=True, exist_ok=True)
            return p

    home = Path.home()
    win_onedrive_candidates = [
        home / "OneDrive - University of Illinois - Urbana",
        home / "OneDrive - University of Illinois",
        home / "OneDrive - University of Illinois Urbana",
        home / "OneDrive-UniversityofIllinois-Urbana",
    ]
    mac_onedrive_candidates = [
        home / "Library" / "CloudStorage" / "OneDrive-UniversityofIllinois-Urbana",
        home / "Library" / "CloudStorage" / "OneDrive - University of Illinois - Urbana",
        home / "Library" / "CloudStorage" / "OneDrive - University of Illinois",
    ]
    for od in win_onedrive_candidates + mac_onedrive_candidates:
        if od.exists():
            p = od / "ABE" / "1TENURE & PROMOTION" / "Projects" / "DRAINMOD_GRID"
            p.mkdir(parents=True, exist_ok=True)
            return p

    p = home / "DRAINMOD_GRID"
    p.mkdir(parents=True, exist_ok=True)
    return p


BASE = resolve_base()
OUT_ROOT = BASE / "WEATHER"
OUT_ROOT.mkdir(parents=True, exist_ok=True)


def prompt_text(message: str, default: Optional[str] = None) -> str:
    prompt = f"{message}: " if default is None else f"{message} [{default}]: "
    try:
        raw = input(prompt).strip()
    except EOFError:
        raw = ""
    if raw:
        return raw
    return "" if default is None else str(default)


def prompt_bool(message: str, default: bool) -> bool:
    shown = "y" if default else "n"
    raw = prompt_text(f"{message} (y/n)", shown).strip().lower()
    return raw in {"y", "yes", "true", "1"}


def prompt_int(message: str, default: int, min_value: int = 0) -> int:
    while True:
        raw = prompt_text(message, str(default))
        try:
            value = int(raw)
            if value >= min_value:
                return value
        except ValueError:
            pass
        print(f"Please enter an integer >= {min_value}.")


def prompt_existing_file(message: str, default: Optional[Path] = None) -> Path:
    while True:
        raw = prompt_text(message, str(default) if default else None).strip().strip('"').strip("'")
        p = Path(raw).expanduser()
        if p.exists() and p.is_file():
            return p
        print(f"File not found: {p}")


def file_safe_label(label: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]+", "_", str(label).strip())
    clean = clean.strip("_")
    return clean or "AOI"


def normalize_station_id(value: str) -> str:
    raw = str(value).strip()
    raw = "".join(ch for ch in raw if ch.isalnum())
    if not raw:
        raise ValueError("Station ID cannot be blank.")
    if len(raw) > 6:
        raw = raw[:6]
    return raw.rjust(6, "0")


def _truthy(x) -> bool:
    return str(x).strip().lower() in {"y", "yes", "true", "1", "t"}


def load_county_targets_from_csv(path: Path, default_state_abbr: str, default_limit_first_pixel: bool) -> list[AreaTarget]:
    df = pd.read_csv(path)
    lookup = {c.strip().lower(): c for c in df.columns}

    county_col = None
    for name in ["county_name", "county", "name"]:
        if name in lookup:
            county_col = lookup[name]
            break
    if county_col is None:
        raise ValueError("County list CSV must contain a county_name or county column.")

    state_col = lookup.get("state_abbr")
    tag_col = lookup.get("output_tag")
    limit_col = lookup.get("limit_to_first_pixel")

    targets: list[AreaTarget] = []
    for _, row in df.iterrows():
        county_name = str(row[county_col]).strip()
        if not county_name or county_name.lower() == "nan":
            continue
        state_abbr = str(row[state_col]).strip().upper() if state_col and pd.notna(row[state_col]) else default_state_abbr
        output_tag = str(row[tag_col]).strip() if tag_col and pd.notna(row[tag_col]) else county_name
        limit_first = _truthy(row[limit_col]) if limit_col and pd.notna(row[limit_col]) else default_limit_first_pixel
        targets.append(
            AreaTarget(
                county_name=county_name,
                state_abbr=state_abbr,
                output_tag=file_safe_label(output_tag),
                limit_to_first_pixel=limit_first,
            )
        )
    if not targets:
        raise ValueError("No valid counties were found in the CSV.")
    return targets


def get_run_configuration() -> dict:
    print("Choose whether you want weather for one county or for a county list CSV.")
    mode = prompt_text("Run mode (single_county or county_list_csv)", "single_county").strip().lower()
    if mode not in {"single_county", "county_list_csv"}:
        raise ValueError("Run mode must be 'single_county' or 'county_list_csv'.")

    if ASK_OUTPUT_DIR:
        out_root_text = prompt_text("Root folder where the outputs should be saved", str(OUT_ROOT))
    else:
        out_root_text = str(OUT_ROOT)
    out_root = Path(out_root_text).expanduser()
    out_root.mkdir(parents=True, exist_ok=True)

    default_state = prompt_text("Default state abbreviation", DEFAULT_STATE_ABBR).strip().upper()

    limit_to_first_pixel_default = prompt_bool(
        "Limit each area to only the first GRIDMET pixel",
        DEFAULT_LIMIT_TO_FIRST_PIXEL,
    )

    if mode == "single_county":
        county_name = prompt_text("County name", DEFAULT_SINGLE_COUNTY)
        targets = [
            AreaTarget(
                county_name=county_name,
                state_abbr=default_state,
                output_tag=file_safe_label(county_name),
                limit_to_first_pixel=limit_to_first_pixel_default,
            )
        ]
    else:
        csv_path = prompt_existing_file("County list CSV file")
        targets = load_county_targets_from_csv(csv_path, default_state, limit_to_first_pixel_default)

    write_daily_csv = prompt_bool("Write the daily weather CSV", DEFAULT_WRITE_DAILY_CSV)
    use_parallel_writing = prompt_bool("Use multiple processors for file writing", DEFAULT_USE_PARALLEL_WRITING)

    event_start = prompt_int("Rain event start hour (1-24)", EVENT_START_HOUR, 1)
    event_duration = prompt_int("Rain event duration in hours", EVENT_DURATION_HOURS, 1)
    if event_start > 24 or event_start + event_duration - 1 > 24:
        raise ValueError("Choose start hour 1..24 and keep start + duration <= 24.")

    max_ee_features = prompt_int("Maximum Earth Engine features per request", DEFAULT_MAX_EE_FEATURES, 100)
    max_days_per_batch = prompt_int("Maximum days per Earth Engine batch", DEFAULT_MAX_DAYS_PER_BATCH, 1)
    rad_multiplier = prompt_int("RAD scale factor applied to daily MJ m-2 values (10 = tenths of MJ, recommended)", DEFAULT_RAD_MULTIPLIER, 1)
    write_pet = prompt_bool("Write PET files from GRIDMET eto", DEFAULT_WRITE_PET)

    station_id_mode = prompt_text(
        "Weather station ID mode for TEM/RAI/RAD/PET prefixes (station6 or fixed)",
        DEFAULT_STATION_ID_MODE,
    ).strip().lower()
    if station_id_mode not in {"station6", "fixed"}:
        raise ValueError("Weather station ID mode must be station6 or fixed.")
    fixed_station_id = normalize_station_id(
        prompt_text("Fixed 6-character weather station ID", DEFAULT_FIXED_STATION_ID)
    ) if station_id_mode == "fixed" else ""

    file_root_mode = prompt_text(
        "File root mode (pixel_id, pixel_label, station6, or pixel_station)",
        DEFAULT_FILE_ROOT_MODE,
    ).strip().lower()
    if file_root_mode not in {"pixel_id", "pixel_label", "station6", "pixel_station"}:
        raise ValueError("File root mode must be pixel_id, pixel_label, station6, or pixel_station.")

    worker_count = max(1, (os.cpu_count() or 2) - 1)
    if use_parallel_writing:
        worker_count = prompt_int("Number of worker processes for file writing", worker_count, 1)

    return {
        "targets": targets,
        "out_root": out_root,
        "write_daily_csv": write_daily_csv,
        "use_parallel_writing": use_parallel_writing,
        "worker_count": worker_count,
        "event_start_hour": event_start,
        "event_duration": event_duration,
        "max_ee_features": max_ee_features,
        "max_days_per_batch": max_days_per_batch,
        "rad_multiplier": rad_multiplier,
        "write_pet": write_pet,
        "station_id_mode": station_id_mode,
        "fixed_station_id": fixed_station_id,
        "file_root_mode": file_root_mode,
    }


def ee_init():
    try:
        ee.Initialize(project=PROJECT)
    except Exception:
        ee.Authenticate()
        ee.Initialize(project=PROJECT)


def get_county_geom(state_abbr: str, county_name: str):
    state_abbr = state_abbr.upper()
    if state_abbr not in STATE_FIPS_MAP:
        raise ValueError(f"Unknown state abbreviation: {state_abbr}")
    state_fips = STATE_FIPS_MAP[state_abbr]
    counties = ee.FeatureCollection("TIGER/2018/Counties")
    geom = (
        counties
        .filter(ee.Filter.eq("STATEFP", state_fips))
        .filter(ee.Filter.eq("NAME", county_name))
        .geometry()
    )
    return geom


def get_gridmet_collection(aoi_geom):
    return (
        ee.ImageCollection("IDAHO_EPSCOR/GRIDMET")
        .filterDate(START, END)
        .select(["pr", "tmmx", "tmmn", "srad", "eto"])
        .filterBounds(aoi_geom)
        .sort("system:time_start")
    )


def make_pixel_label(i: int) -> str:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    block = i // 999
    num = (i % 999) + 1
    return f"{letters[block]}{num:03d}"


def make_station6(i: int) -> str:
    return f"{i + 1:06d}"


def sample_fc_to_points_df(fc) -> pd.DataFrame:
    info = fc.getInfo()
    features = info.get("features", []) if isinstance(info, dict) else []
    rows = []
    for feat in features:
        geom = feat.get("geometry", {}) or {}
        coords = geom.get("coordinates", []) or []
        props = feat.get("properties", {}) or {}
        if len(coords) < 2:
            continue
        rows.append({"lat": float(coords[1]), "lon": float(coords[0]), "px": int(props.get("x")), "py": int(props.get("y"))})
    if not rows:
        return pd.DataFrame(columns=["lat", "lon", "px", "py"])
    return pd.DataFrame(rows).drop_duplicates(subset=["px", "py"]).reset_index(drop=True)


def choose_dominant_intersecting_pixel(sample_img, coord_img, aoi_geom, scale_m: float) -> pd.DataFrame:
    fine_scale = max(30.0, min(scale_m / 8.0, 250.0))
    dense_fc = coord_img.sample(region=aoi_geom, scale=fine_scale, geometries=False)
    dense_info = dense_fc.getInfo()
    dense_features = dense_info.get("features", []) if isinstance(dense_info, dict) else []
    pairs: list[tuple[int, int]] = []
    for feat in dense_features:
        props = feat.get("properties", {}) or {}
        x = props.get("x")
        y = props.get("y")
        if x is None or y is None:
            continue
        pairs.append((int(x), int(y)))
    if not pairs:
        centroid_fc = coord_img.sample(region=aoi_geom.centroid(1), scale=scale_m, geometries=False)
        centroid_info = centroid_fc.getInfo()
        centroid_features = centroid_info.get("features", []) if isinstance(centroid_info, dict) else []
        for feat in centroid_features:
            props = feat.get("properties", {}) or {}
            x = props.get("x")
            y = props.get("y")
            if x is not None and y is not None:
                pairs.append((int(x), int(y)))
    if not pairs:
        return pd.DataFrame(columns=["lat", "lon", "px", "py"])
    best_px, best_py = Counter(pairs).most_common(1)[0][0]
    search_region = aoi_geom.buffer(scale_m * 2.5)
    cand_fc = sample_img.sample(region=search_region, scale=scale_m, geometries=True)
    cand_df = sample_fc_to_points_df(cand_fc)
    if cand_df.empty:
        return cand_df
    best = cand_df[(cand_df["px"] == best_px) & (cand_df["py"] == best_py)].copy()
    if best.empty:
        return cand_df.iloc[[0]].copy()
    return best.iloc[[0]].copy()


def get_pixel_points_from_first_image(col, aoi_geom):
    first = ee.Image(col.first())
    proj = first.select("pr").projection()
    scale_m = float(proj.nominalScale().getInfo())
    coord_img = ee.Image.pixelCoordinates(proj)
    sample_img = first.select("pr").addBands(coord_img)

    pts_fc = sample_img.sample(region=aoi_geom, scale=scale_m, geometries=True)
    df = sample_fc_to_points_df(pts_fc)

    fallback_used = False
    if df.empty:
        df = choose_dominant_intersecting_pixel(sample_img, coord_img, aoi_geom, scale_m)
        fallback_used = not df.empty

    if df.empty:
        return df, scale_m, fallback_used

    df = df.sort_values(["lat", "lon"]).reset_index(drop=True)
    df["pixel_label"] = [make_pixel_label(i) for i in range(len(df))]
    df["station6"] = [make_station6(i) for i in range(len(df))]
    df["pixel_id"] = df["station6"]
    return df[["lat", "lon", "pixel_id", "pixel_label", "station6", "px", "py"]], scale_m, fallback_used


def add_elevation(df_points: pd.DataFrame) -> pd.DataFrame:
    if not ELEV_FROM_SRTM:
        df_points["elev_m"] = -99.0
        return df_points
    srtm = ee.Image("USGS/SRTMGL1_003")
    feats = []
    for _, r in df_points.iterrows():
        pt = ee.Geometry.Point([float(r["lon"]), float(r["lat"])])
        feats.append(ee.Feature(pt, {"pixel_id": r["pixel_id"]}))
    fc = ee.FeatureCollection(feats)
    sampled = srtm.sampleRegions(fc, scale=90, geometries=False)
    ids = sampled.aggregate_array("pixel_id").getInfo()
    elev = sampled.aggregate_array("elevation").getInfo()
    dfe = pd.DataFrame({"pixel_id": ids, "elev_m": elev})
    out = df_points.merge(dfe, on="pixel_id", how="left")
    out["elev_m"] = out["elev_m"].fillna(-99.0)
    return out


def build_points_fc(df_pts: pd.DataFrame):
    feats = []
    for _, r in df_pts.iterrows():
        pt = ee.Geometry.Point([float(r["lon"]), float(r["lat"])])
        feats.append(ee.Feature(pt, {"pixel_id": r["pixel_id"]}))
    return ee.FeatureCollection(feats)


def day_windows(start_date: dt.date, end_date: dt.date, n_pixels: int, max_ee_features: int, max_days_per_batch: int):
    safe_days = max(1, min(max_days_per_batch, max_ee_features // max(1, n_pixels)))
    cur = start_date
    while cur < end_date:
        nxt = min(cur + dt.timedelta(days=safe_days), end_date)
        yield cur, nxt, safe_days
        cur = nxt


def sample_batch_to_records(col_batch, points_fc, scale, rad_multiplier: int):
    def per_image(img):
        img = ee.Image(img)
        tmax_f = img.select("tmmx").multiply(K_TO_F_SCALE).add(K_TO_F_OFFSET).rename("TMAX_F")
        tmin_f = img.select("tmmn").multiply(K_TO_F_SCALE).add(K_TO_F_OFFSET).rename("TMIN_F")
        rain_in = img.select("pr").multiply(MM_TO_IN).rename("RAIN_IN")
        rad_mj = img.select("srad").multiply(SRAD_WM2_TO_MJ).rename("RAD_MJ")
        rad_val = rad_mj.multiply(rad_multiplier).round().rename("RAD_VAL")
        pet_hundredths_in = img.select("eto").multiply(MM_TO_IN).multiply(100.0).round().rename("PET_VAL")
        img2 = ee.Image.cat([tmax_f, tmin_f, rain_in, rad_mj, rad_val, pet_hundredths_in])
        date = ee.Date(img.get("system:time_start")).format("YYYY-MM-dd")
        fc = img2.sampleRegions(collection=points_fc, scale=scale, geometries=False)
        def add_date(f):
            return ee.Feature(f).set("date", date)
        return fc.map(add_date)

    fc_batch = ee.FeatureCollection(col_batch.map(per_image)).flatten()
    features = fc_batch.getInfo().get("features", [])
    records = []
    for feat in features:
        p = feat.get("properties", {})
        records.append(
            {
                "date": p.get("date"),
                "pixel_id": p.get("pixel_id"),
                "TMAX_F": p.get("TMAX_F"),
                "TMIN_F": p.get("TMIN_F"),
                "RAIN_IN": p.get("RAIN_IN"),
                "RAD_MJ": p.get("RAD_MJ"),
                "RAD_VAL": p.get("RAD_VAL"),
                "PET_VAL": p.get("PET_VAL"),
            }
        )
    return records


def split_integer_total(total: int, n_parts: int) -> list[int]:
    base = total // n_parts
    rem = total % n_parts
    out = [base] * n_parts
    for i in range(rem):
        out[i] += 1
    return out


def build_hourly_rain_events(day: int, total_inches: float, start_hour: int, duration_hours: int) -> list[tuple[int, int, int]]:
    amount = max(0, int(round(float(total_inches) * 100.0)))
    if amount == 0:
        return []
    if RAIN_DISTRIBUTION != "uniform":
        raise ValueError(f"Unsupported rainfall distribution: {RAIN_DISTRIBUTION}")
    split = split_integer_total(amount, duration_hours)
    hours = list(range(start_hour, start_hour + duration_hours))
    return [(day, h, amt) for h, amt in zip(hours, split) if amt > 0]


def format_tem_month_lines(weather_id: str, year: int, month: int, rows_month: pd.DataFrame) -> list[str]:
    rows_month = rows_month.sort_values("date").copy()
    n_days = calendar.monthrange(year, month)[1]
    pair_by_day: dict[int, str] = {}
    for _, r in rows_month.iterrows():
        day = int(pd.Timestamp(r["date"]).day)
        tmax = int(round(float(r["TMAX_F"])))
        tmin = int(round(float(r["TMIN_F"])))
        pair_by_day[day] = f"{tmax:3d}{tmin:3d}"
    records = [pair_by_day.get(day, f"{0:3d}{0:3d}") for day in range(1, n_days + 1)]
    line1 = f"{weather_id} {year:4d}{month:2d}" + " " * 5 + "".join(records[:14])
    lines = [line1]
    if n_days > 14:
        lines.append("".join(records[14:]))
    return lines


def format_rad_month_lines(weather_id: str, year: int, month: int, rows_month: pd.DataFrame) -> list[str]:
    rows_month = rows_month.sort_values("date").copy()
    n_days = calendar.monthrange(year, month)[1]
    val_by_day: dict[int, int] = {}
    for _, r in rows_month.iterrows():
        day = int(pd.Timestamp(r["date"]).day)
        val_by_day[day] = int(round(float(r["RAD_VAL"])))
    values = [val_by_day.get(day, 0) for day in range(1, n_days + 1)]
    line1 = f"{weather_id} {year:4d}{month:2d}" + " " * 3 + "".join(f"{v:4d}" for v in values[:14])
    lines = [line1]
    if n_days > 14:
        lines.append("".join(f"{v:4d}" for v in values[14:]))
    return lines


def format_pet_month_lines(weather_id: str, year: int, month: int, rows_month: pd.DataFrame) -> list[str]:
    rows_month = rows_month.sort_values("date").copy()
    n_days = calendar.monthrange(year, month)[1]
    val_by_day: dict[int, int] = {}
    for _, r in rows_month.iterrows():
        day = int(pd.Timestamp(r["date"]).day)
        val_by_day[day] = int(round(float(r["PET_VAL"])))
    values = [val_by_day.get(day, 0) for day in range(1, n_days + 1)]
    # Native PET style prefix: 6-char station/site code + year + month, then spaces.
    line1 = f"{weather_id:>6s} {year:4d}{month:2d}" + " " * 5 + "".join(f"{v:4d}" for v in values[:14])
    lines = [line1]
    if n_days > 14:
        lines.append("".join(f"{v:4d}" for v in values[14:]))
    return lines


def format_rai_month_lines(weather_id: str, year: int, month: int, rows_month: pd.DataFrame, start_hour: int, duration_hours: int) -> list[str]:
    rows_month = rows_month.sort_values("date")
    events: list[str] = []
    for _, r in rows_month.iterrows():
        day = int(pd.Timestamp(r["date"]).day)
        ev = build_hourly_rain_events(day, float(r["RAIN_IN"]), start_hour, duration_hours)
        for d, h, amt in ev:
            events.append(f"{d:2d}{h:2d}{amt:4d}")
    if not events:
        return []
    prefix = f"{weather_id} {year:4d}{month:2d} "
    chunk_size = 12
    lines: list[str] = []
    for i in range(0, len(events), chunk_size):
        lines.append(prefix + "".join(events[i:i + chunk_size]))
    return lines


def write_ascii_crlf(path: Path, lines: Iterable[str]):
    text = "\r\n".join(lines) + "\r\n"
    with open(path, "wb") as f:
        f.write(text.encode("ascii", "ignore"))


def build_file_root(pixel_id: str, pixel_label: str, station6: str, mode: str) -> str:
    if mode == "pixel_id":
        return pixel_id
    if mode == "pixel_label":
        return pixel_label
    if mode == "station6":
        return station6
    if mode == "pixel_station":
        return f"{pixel_id}_{station6}"
    raise ValueError(f"Unknown file root mode: {mode}")


def choose_weather_id(station6: str, mode: str, fixed_station_id: str) -> str:
    if mode == "station6":
        return normalize_station_id(station6)
    if mode == "fixed":
        return normalize_station_id(fixed_station_id)
    raise ValueError(f"Unknown station ID mode: {mode}")


def _write_one_pixel_files(task: dict) -> str:
    pixel_id = task["pixel_id"]
    station6 = task["station6"]
    weather_id = task["weather_id"]
    file_root = task["file_root"]
    records = task["records"]
    out_tem = Path(task["out_tem"])
    out_rai = Path(task["out_rai"])
    out_rad = Path(task["out_rad"])
    out_pet = Path(task["out_pet"]) if task.get("out_pet") else None
    event_start_hour = int(task["event_start_hour"])
    event_duration = int(task["event_duration"])
    write_pet = bool(task.get("write_pet"))

    df = pd.DataFrame.from_records(records)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    start_year = int(df["date"].dt.year.min())
    end_year = int(df["date"].dt.year.max())
    yy0 = f"{start_year % 100:02d}"
    yy1 = f"{end_year % 100:02d}"

    tem_lines: list[str] = []
    rai_lines: list[str] = []
    rad_lines: list[str] = []
    pet_lines: list[str] = []
    for (year, month), gm in df.groupby([df["date"].dt.year, df["date"].dt.month]):
        tem_lines.extend(format_tem_month_lines(weather_id, int(year), int(month), gm))
        rai_lines.extend(format_rai_month_lines(weather_id, int(year), int(month), gm, event_start_hour, event_duration))
        rad_lines.extend(format_rad_month_lines(weather_id, int(year), int(month), gm))
        if write_pet:
            pet_lines.extend(format_pet_month_lines(weather_id, int(year), int(month), gm))

    write_ascii_crlf(out_tem / f"{file_root}Temp{yy0}_{yy1}.TEM", tem_lines)
    write_ascii_crlf(out_rai / f"{file_root}Rain{yy0}_{yy1}.RAI", rai_lines)
    write_ascii_crlf(out_rad / f"{file_root}Rad{yy0}_{yy1}.RAD", rad_lines)
    if write_pet and out_pet is not None:
        write_ascii_crlf(out_pet / f"{file_root}PET{yy0}_{yy1}.PET", pet_lines)
    return pixel_id


def process_area(target: AreaTarget, cfg: dict):
    area_desc = f"{target.county_name}, {target.state_abbr}"
    out_area = cfg["out_root"] / target.output_tag
    out_tem = out_area / "TEM"
    out_rai = out_area / "RAI"
    out_rad = out_area / "RAD"
    out_pet = out_area / "PET" if cfg["write_pet"] else None
    out_area.mkdir(parents=True, exist_ok=True)
    out_tem.mkdir(parents=True, exist_ok=True)
    out_rai.mkdir(parents=True, exist_ok=True)
    out_rad.mkdir(parents=True, exist_ok=True)
    if out_pet is not None:
        out_pet.mkdir(parents=True, exist_ok=True)

    steps = tqdm(total=6, desc=f"{target.output_tag} tasks", unit="step", leave=False)

    aoi_geom = get_county_geom(target.state_abbr, target.county_name)
    col = get_gridmet_collection(aoi_geom)
    if col.size().getInfo() == 0:
        raise RuntimeError(f"No GRIDMET images found for {area_desc}.")
    steps.update(1)

    df_pts, scale, fallback_used = get_pixel_points_from_first_image(col, aoi_geom)
    if df_pts.empty:
        raise RuntimeError(f"No GRIDMET pixels found for {area_desc}.")
    if target.limit_to_first_pixel:
        df_pts = df_pts.iloc[[0]].copy().reset_index(drop=True)
    df_pts = add_elevation(df_pts)
    df_pts["weather_id"] = [
        choose_weather_id(s, cfg["station_id_mode"], cfg["fixed_station_id"])
        for s in df_pts["station6"]
    ]
    steps.update(1)

    start_tag = START.replace("-", "")
    end_tag = (dt.datetime.strptime(END, "%Y-%m-%d") - dt.timedelta(days=1)).strftime("%Y%m%d")
    stations_csv = out_area / f"stations_{target.output_tag}_{start_tag}_{end_tag}.csv"
    df_pts.to_csv(stations_csv, index=False)
    steps.update(1)

    if fallback_used:
        print(f"{target.output_tag}: no centroid fell inside the area. Using the dominant intersecting GRIDMET pixel.")

    print(f"\nArea: {area_desc}")
    print(f"Pixels in area: {len(df_pts)}")
    print(f"Stations file: {stations_csv}")
    print("Weather ID column written to stations CSV for consistency across TEM/RAI/RAD/PET.")
    print(f"TEM output folder: {out_tem}")
    print(f"RAI output folder: {out_rai}")
    print(f"RAD output folder: {out_rad}")
    if out_pet is not None:
        print(f"PET output folder: {out_pet}")

    points_fc = build_points_fc(df_pts)
    start_dt = dt.datetime.strptime(START, "%Y-%m-%d").date()
    end_dt = dt.datetime.strptime(END, "%Y-%m-%d").date()

    windows = list(day_windows(start_dt, end_dt, len(df_pts), cfg["max_ee_features"], cfg["max_days_per_batch"]))
    fetch_bar = tqdm(windows, desc=f"{target.output_tag} fetch", unit="batch", leave=False)

    all_records = []
    for d0, d1, safe_days in fetch_bar:
        col_batch = col.filterDate(d0.isoformat(), d1.isoformat())
        records = sample_batch_to_records(col_batch, points_fc, scale, cfg["rad_multiplier"])
        all_records.extend(records)
        fetch_bar.set_postfix(days=safe_days, records=len(records))
    steps.update(1)

    df = pd.DataFrame.from_records(all_records).dropna(how="all")
    if df.empty:
        raise RuntimeError(f"No weather records returned for {area_desc}.")
    df["date"] = pd.to_datetime(df["date"])
    for c in ["TMAX_F", "TMIN_F", "RAIN_IN", "RAD_MJ", "RAD_VAL", "PET_VAL"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    req_cols = ["pixel_id", "date", "TMAX_F", "TMIN_F", "RAIN_IN", "RAD_VAL"]
    if cfg["write_pet"]:
        req_cols.append("PET_VAL")
    df = df.dropna(subset=req_cols)
    df = df.sort_values(["pixel_id", "date"]).reset_index(drop=True)
    df = df.drop_duplicates(subset=["pixel_id", "date"], keep="first")
    df = df.merge(df_pts[["pixel_id", "station6"]], on="pixel_id", how="left")
    steps.update(1)

    if cfg["write_daily_csv"]:
        daily_csv = out_area / f"daily_weather_{target.output_tag}_{start_tag}_{end_tag}.csv"
        df.to_csv(daily_csv, index=False)
        print(f"Daily weather CSV: {daily_csv}")

    tasks = []
    for pid, gpid in df.groupby("pixel_id"):
        station6 = str(df_pts.loc[df_pts["pixel_id"] == pid, "station6"].iloc[0])
        pixel_label = str(df_pts.loc[df_pts["pixel_id"] == pid, "pixel_label"].iloc[0])
        weather_id = str(df_pts.loc[df_pts["pixel_id"] == pid, "weather_id"].iloc[0])
        file_root = build_file_root(pid, pixel_label, station6, cfg["file_root_mode"])
        tasks.append(
            {
                "pixel_id": pid,
                "station6": station6,
                "pixel_label": pixel_label,
                "weather_id": weather_id,
                "file_root": file_root,
                "records": gpid[["date", "TMAX_F", "TMIN_F", "RAIN_IN", "RAD_VAL", "PET_VAL"]].to_dict("records"),
                "out_tem": str(out_tem),
                "out_rai": str(out_rai),
                "out_rad": str(out_rad),
                "out_pet": str(out_pet) if out_pet is not None else "",
                "write_pet": cfg["write_pet"],
                "event_start_hour": cfg["event_start_hour"],
                "event_duration": cfg["event_duration"],
            }
        )

    write_bar = tqdm(total=len(tasks), desc=f"{target.output_tag} write", unit="pixel", leave=False)
    if cfg["use_parallel_writing"] and len(tasks) > 1:
        with ProcessPoolExecutor(max_workers=cfg["worker_count"]) as ex:
            futures = [ex.submit(_write_one_pixel_files, task) for task in tasks]
            for _ in as_completed(futures):
                write_bar.update(1)
    else:
        for task in tasks:
            _write_one_pixel_files(task)
            write_bar.update(1)
    steps.update(1)

    steps.close()
    write_bar.close()
    print(f"Finished {area_desc}.")
    return {
        "area": area_desc,
        "pixels": len(df_pts),
        "stations_csv": str(stations_csv),
        "tem_dir": str(out_tem),
        "rai_dir": str(out_rai),
        "rad_dir": str(out_rad),
        "pet_dir": str(out_pet) if out_pet is not None else "",
    }


def main():
    freeze_support()
    ee_init()
    t0 = time.time()
    cfg = get_run_configuration()

    print(f"\nBASE: {BASE}")
    print(f"Output root: {cfg['out_root']}")
    print(f"Areas to run: {len(cfg['targets'])}")
    print(f"Rain event window: start={cfg['event_start_hour']}, duration={cfg['event_duration']} h")
    print(f"RAD output unit: integer tenths of MJ m-2 d-1")
    print(f"Weather station ID mode: {cfg['station_id_mode']}")
    if cfg["station_id_mode"] == "fixed":
        print(f"Fixed weather station ID: {cfg['fixed_station_id']}")
    print(f"Write PET: {cfg['write_pet']}")
    if cfg["write_pet"]:
        src = "same weather ID used in TEM/RAI/RAD"
        print(f"PET prefix source: {src}")
    print(f"File root mode: {cfg['file_root_mode']}")
    print("pixel_id is now the 6-digit ID used by DRAINMOD, such as 000001 or 000152.")
    print("pixel_label keeps the A001-style label only for bookkeeping in the stations CSV.")
    print(f"Parallel writing: {cfg['use_parallel_writing']}")
    if cfg["use_parallel_writing"]:
        print(f"Worker processes: {cfg['worker_count']}")

    summaries = []
    areas_bar = tqdm(cfg["targets"], desc="Areas", unit="area")
    for target in areas_bar:
        areas_bar.set_postfix(area=target.output_tag)
        summaries.append(process_area(target, cfg))

    summary_df = pd.DataFrame(summaries)
    summary_path = cfg["out_root"] / f"weather_run_summary_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    summary_df.to_csv(summary_path, index=False)

    print(f"\nSummary file: {summary_path}")
    print(f"Done in {(time.time() - t0) / 60:.1f} minutes")


if __name__ == "__main__":
    main()
