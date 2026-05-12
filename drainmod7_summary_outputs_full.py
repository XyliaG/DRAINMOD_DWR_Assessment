from __future__ import annotations

import json
import math
import re
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import pandas as pd
except Exception as e:
    print("ERROR: pandas is required for this script.")
    print("Install with: pip install pandas openpyxl")
    raise

DEFAULT_ROOT = Path(r"C:\Drainmod7\DRAINMOD_GRID\OUTPUTS")
DEFAULT_SUMMARY = Path(r"C:\Drainmod7\DRAINMOD_GRID\SUMMARY")

TEXT_EXTS = {
    ".CRO", ".DAY", ".MON", ".MRK", ".OST", ".OUT", ".PLT", ".RNK", ".WTB", ".YLD", ".YR",
}
MZ_SUFFIXES = ["_MZOVERVIEW", "_MZPlantGro", "_MZPlantN"]


def prompt_path(message: str, default: Path) -> Path:
    raw = input(f"{message} [{default}]: ").strip().strip('"').strip("'")
    return Path(raw) if raw else default


def prompt_bool(message: str, default: bool = False) -> bool:
    shown = "y" if default else "n"
    raw = input(f"{message} (y/n) [{shown}]: ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes", "true", "1"}


def safe_read_text(path: Path) -> str:
    for enc in ["utf-8", "latin-1", "cp1252"]:
        try:
            return path.read_text(encoding=enc, errors="ignore")
        except Exception:
            continue
    return path.read_bytes().decode("latin-1", errors="ignore")


def to_num(x):
    try:
        if x is None:
            return None
        s = str(x).strip()
        if s == "" or s.upper() in {"NA", "NAN", "*"}:
            return None
        return float(s)
    except Exception:
        return None


def to_int(x):
    v = to_num(x)
    if v is None or math.isnan(v):
        return None
    return int(v)


def scenario_from_file(path: Path) -> str:
    name = path.name
    stem = path.stem
    for suf in ["_MZOVERVIEW", "_MZPlantGro", "_MZPlantN"]:
        if stem.endswith(suf):
            return stem[: -len(suf)]
    if stem == "scenario_metadata":
        return path.parent.name
    if stem.startswith("scenario_metadata"):
        return path.parent.name
    return stem


def infer_parts_from_scenario(scenario_name: str) -> dict:
    out = {}
    parts = scenario_name.split("_")
    if parts:
        out["pixel_id_from_name"] = parts[0]
    if len(parts) >= 2:
        out["mode_code_from_name"] = parts[1]
    if len(parts) >= 3:
        out["spacing_name_from_name"] = parts[2]
    return out


@dataclass
class ScenarioFiles:
    parent: Path
    scenario_name: str
    files: Dict[str, Path]
    metadata_path: Optional[Path] = None


def discover_scenarios(root: Path) -> list[ScenarioFiles]:
    candidates: dict[tuple[str, str], ScenarioFiles] = {}
    all_files = [p for p in root.rglob("*") if p.is_file()]

    metadata_by_parent: dict[Path, Path] = {}
    for p in all_files:
        if p.name.lower().startswith("scenario_metadata") and p.suffix.lower() == ".json":
            metadata_by_parent[p.parent] = p

    for p in all_files:
        if p.suffix.upper() in TEXT_EXTS or p.suffix.lower() == ".json":
            if p.name.lower().startswith("scenario_metadata"):
                continue
            scenario = scenario_from_file(p)
            key = (str(p.parent.resolve()), scenario)
            if key not in candidates:
                candidates[key] = ScenarioFiles(parent=p.parent, scenario_name=scenario, files={})
            candidates[key].files[p.name] = p

    for key, obj in candidates.items():
        if obj.parent in metadata_by_parent:
            obj.metadata_path = metadata_by_parent[obj.parent]
            obj.files[metadata_by_parent[obj.parent].name] = metadata_by_parent[obj.parent]

    with_meta_dirs = set(metadata_by_parent)
    for parent, meta in metadata_by_parent.items():
        try:
            meta_data = json.loads(safe_read_text(meta))
            scenario = str(meta_data.get("scenario_name") or parent.name)
        except Exception:
            scenario = parent.name
        key = (str(parent.resolve()), scenario)
        if key not in candidates:
            files = {p.name: p for p in parent.iterdir() if p.is_file()}
            candidates[key] = ScenarioFiles(parent=parent, scenario_name=scenario, files=files, metadata_path=meta)

    return sorted(candidates.values(), key=lambda x: (str(x.parent), x.scenario_name))


def file_inventory_row(sc: ScenarioFiles, p: Path) -> dict:
    return {
        "scenario_name": sc.scenario_name,
        "folder": str(sc.parent),
        "file_name": p.name,
        "extension": p.suffix.upper(),
        "size_bytes": p.stat().st_size if p.exists() else 0,
        "size_kb": round((p.stat().st_size if p.exists() else 0) / 1024, 2),
        "exists": p.exists(),
    }


def find_file(sc: ScenarioFiles, suffix: str) -> Optional[Path]:
    suffix_upper = suffix.upper()
    if suffix_upper.startswith("_MZ"):
        for p in sc.files.values():
            if p.stem == sc.scenario_name + suffix_upper.replace("_MZ", "_MZ"):
                return p
        for p in sc.files.values():
            if p.name.upper().startswith(sc.scenario_name.upper() + suffix_upper.upper()):
                return p
        return None
    expected = f"{sc.scenario_name}{suffix}"
    for p in sc.files.values():
        if p.name.upper() == expected.upper():
            return p
    for p in sc.files.values():
        if p.suffix.upper() == suffix_upper and p.stem.upper().startswith(sc.scenario_name.upper()):
            return p
    return None


def parse_metadata(sc: ScenarioFiles) -> dict:
    if not sc.metadata_path or not sc.metadata_path.exists():
        return {}
    try:
        data = json.loads(safe_read_text(sc.metadata_path))
        flat = {}
        for k, v in data.items():
            if isinstance(v, (list, dict)):
                flat[k] = json.dumps(v)
            else:
                flat[k] = v
        return flat
    except Exception as e:
        return {"metadata_parse_error": str(e)}


def parse_out(path: Path) -> dict:
    text = safe_read_text(path)
    out = {}
    m = re.search(r"\*\*\*\s*(FREE DRAINAGE|CONTROLLED DRAINAGE|SUBIRRIGATION-DRAINAGE SYSTEM)\s*\*\*\*", text, re.I)
    if m:
        out["out_drainage_system"] = m.group(1).strip().upper()
    patterns = {
        "out_start_year": r"STARTING YEAR OF SIMULATION.*?([0-9]{4})\s+YEAR",
        "out_start_month": r"STARTING MONTH OF SIMULATION.*?([0-9]+)\s+MONTH",
        "out_end_year": r"ENDING YEAR OF SIMULATION.*?([0-9]{4})\s+YEAR",
        "out_end_month": r"ENDING MONTH OF SIMULATION.*?([0-9]+)\s+MONTH",
        "out_drain_spacing_cm": r"SDRAIN\s*=\s*([0-9.]+)\s*CM",
        "out_drain_depth_cm": r"DDRAIN\s*=\s*([0-9.]+)\s*CM",
        "out_hydraulic_head_cm": r"HDRAIN\s*=\s*([0-9.]+)\s*CM",
    }
    for k, pat in patterns.items():
        mm = re.search(pat, text, re.I | re.S)
        if mm:
            out[k] = to_num(mm.group(1))
    return out


def parse_yr(path: Path, sc: ScenarioFiles) -> pd.DataFrame:
    lines = safe_read_text(path).splitlines()
    rows = []
    capture = False
    cols = ["year", "rainfall_cm", "infiltration_cm", "et_cm", "drain_cm", "runoff_cm", "dry_days", "work_days", "sew", "pump_cm", "slope_seep_cm", "vertical_seep_cm", "lateral_seep_cm", "drain_plus_seep_cm"]
    for line in lines:
        if line.strip().startswith("YEAR RAINFALL"):
            capture = True
            continue
        if not capture:
            continue
        s = line.strip()
        if not s:
            continue
        parts = s.split()
        if len(parts) >= 14 and (parts[0].isdigit() or parts[0].upper() == "AVG"):
            row = dict(zip(cols, parts[:14]))
            row["scenario_name"] = sc.scenario_name
            row["folder"] = str(sc.parent)
            rows.append(row)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for c in cols:
        if c == "year":
            continue
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["year"] = df["year"].astype(str)
    return df


def parse_yld(path: Path, sc: ScenarioFiles) -> pd.DataFrame:
    rows = []
    cols = [
        "year", "sdi_excess", "sdi_drought", "plant_doy", "plant_delay_days", "harvest_doy",
        "rel_yield_excess_pct", "rel_yield_drought_pct", "rel_yield_delay_pct", "rel_yield_salinity_pct", "rel_yield_overall_pct",
    ]
    for line in safe_read_text(path).splitlines():
        s = line.strip()
        if not s:
            continue
        parts = s.split()
        if len(parts) >= 11 and (parts[0].isdigit() or parts[0].upper() == "AVG"):
            if parts[0].isdigit() and not (1900 <= int(parts[0]) <= 2100):
                continue
            row = dict(zip(cols, parts[:11]))
            row["scenario_name"] = sc.scenario_name
            row["folder"] = str(sc.parent)
            rows.append(row)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for c in cols:
        if c == "year":
            continue
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["year"] = df["year"].astype(str)
    return df


def parse_mon(path: Path, sc: ScenarioFiles) -> pd.DataFrame:
    rows = []
    current_year = None
    cols = ["month", "rain_cm", "infiltration_cm", "et_cm", "drain_cm", "runoff_cm", "dry_days", "work_days", "sew", "pump_cm", "slope_seep_cm", "vertical_seep_cm", "lateral_seep_cm", "drain_plus_seep_cm"]
    for line in safe_read_text(path).splitlines():
        m = re.search(r"YEAR\s+([0-9]{4})", line)
        if m and "MONTHLY" in line.upper():
            current_year = int(m.group(1))
            continue
        if current_year is None:
            continue
        s = line.strip()
        if not s:
            continue
        parts = s.split()
        if len(parts) >= 14 and (parts[0].isdigit() or parts[0].upper() == "TOTALS"):
            row = dict(zip(cols, parts[:14]))
            row["year"] = current_year
            row["scenario_name"] = sc.scenario_name
            row["folder"] = str(sc.parent)
            rows.append(row)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for c in cols:
        if c == "month":
            continue
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["month"] = df["month"].astype(str)
    return df


def parse_day_aggregate(path: Path, sc: ScenarioFiles) -> pd.DataFrame:
    rows = []
    current_year, current_month = None, None
    cols = ["day", "rain_cm", "infiltration_cm", "et_cm", "drain_cm", "tvol", "ddz", "dtwt_cm", "stor", "runoff_cm", "wloss", "slope_seep_cm", "vertical_seep_cm", "lateral_seep_cm", "total_q_cm"]
    data = []
    for line in safe_read_text(path).splitlines():
        s = line.strip()
        parts = s.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() and 1900 <= int(parts[0]) <= 2100 and 1 <= int(parts[1]) <= 12:
            current_year, current_month = int(parts[0]), int(parts[1])
            continue
        if current_year is None:
            continue
        if len(parts) >= 15 and parts[0].isdigit():
            rec = dict(zip(cols, parts[:15]))
            rec["year"] = current_year
            rec["month"] = current_month
            data.append(rec)
    df = pd.DataFrame(data)
    if df.empty:
        return df
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    agg = df.groupby(["year", "month"], as_index=False).agg(
        days=("day", "count"),
        rain_cm=("rain_cm", "sum"),
        infiltration_cm=("infiltration_cm", "sum"),
        et_cm=("et_cm", "sum"),
        drain_cm=("drain_cm", "sum"),
        runoff_cm=("runoff_cm", "sum"),
        total_q_cm=("total_q_cm", "sum"),
        dtwt_mean_cm=("dtwt_cm", "mean"),
        dtwt_min_cm=("dtwt_cm", "min"),
        dtwt_max_cm=("dtwt_cm", "max"),
    )
    agg["scenario_name"] = sc.scenario_name
    agg["folder"] = str(sc.parent)
    return agg


def parse_wtb(path: Path, sc: ScenarioFiles) -> pd.DataFrame:
    rows = []
    for line in safe_read_text(path).splitlines():
        parts = line.strip().split()
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            yr = int(parts[0])
            if 1900 <= yr <= 2100:
                wtd = to_num(parts[2])
                rows.append({"scenario_name": sc.scenario_name, "folder": str(sc.parent), "year": yr, "doy": int(parts[1]), "water_table_cm_signed": wtd, "water_table_depth_cm": abs(wtd) if wtd is not None else None})
    return pd.DataFrame(rows)


def aggregate_wtb(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    def p05(x): return x.quantile(0.05)
    def p95(x): return x.quantile(0.95)
    out = df.groupby(["scenario_name", "folder", "year"], as_index=False).agg(
        wtb_days=("doy", "count"),
        wtb_depth_mean_cm=("water_table_depth_cm", "mean"),
        wtb_depth_min_cm=("water_table_depth_cm", "min"),
        wtb_depth_max_cm=("water_table_depth_cm", "max"),
        wtb_depth_p05_cm=("water_table_depth_cm", p05),
        wtb_depth_p95_cm=("water_table_depth_cm", p95),
        days_wt_shallower_30cm=("water_table_depth_cm", lambda x: (x <= 30).sum()),
        days_wt_shallower_50cm=("water_table_depth_cm", lambda x: (x <= 50).sum()),
        days_wt_deeper_100cm=("water_table_depth_cm", lambda x: (x > 100).sum()),
    )
    return out


def parse_mz_table(path: Path, sc: ScenarioFiles, source: str) -> pd.DataFrame:
    lines = safe_read_text(path).splitlines()
    header = None
    rows = []
    for line in lines:
        if line.startswith("@"):
            header = line[1:].split()
            continue
        if header and line.strip() and line.strip()[0].isdigit():
            parts = line.split()
            if len(parts) >= len(header):
                rec = dict(zip(header, parts[:len(header)]))
                rec["scenario_name"] = sc.scenario_name
                rec["folder"] = str(sc.parent)
                rec["source"] = source
                rows.append(rec)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for c in df.columns:
        if c not in {"scenario_name", "folder", "source"}:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def aggregate_mz_growth(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    cols_to_max = [c for c in ["LAID", "LWAD", "SWAD", "GWAD", "RWAD", "CWAD", "G#AD", "HIAD", "CHTD", "RDPD"] if c in df.columns]
    group_cols = ["scenario_name", "folder", "YEAR"]
    agg_dict = {c: ["max"] for c in cols_to_max}
    agg_dict.update({"DAP": ["max"], "DOY": ["min", "max"]})
    out = df.groupby(group_cols).agg(agg_dict)
    out.columns = ["mz_growth_" + "_".join([str(x) for x in c if x]) for c in out.columns]
    out = out.reset_index().rename(columns={"YEAR": "year"})
    return out


def aggregate_mz_n(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    cols_to_max = [c for c in ["CNAD", "GNAD", "VNAD", "NUPC", "LNAD", "SNAD", "GN%D", "VN%D"] if c in df.columns]
    group_cols = ["scenario_name", "folder", "YEAR"]
    agg_dict = {c: ["max"] for c in cols_to_max}
    agg_dict.update({"DAP": ["max"], "DOY": ["min", "max"]})
    out = df.groupby(group_cols).agg(agg_dict)
    out.columns = ["mz_n_" + "_".join([str(x) for x in c if x]) for c in out.columns]
    out = out.reset_index().rename(columns={"YEAR": "year"})
    return out


def parse_cro(path: Path, sc: ScenarioFiles) -> pd.DataFrame:
    text = safe_read_text(path)
    rows = []
    blocks = re.split(r"\*{10,}", text)
    for b in blocks:
        yrm = re.search(r"year\s*=\s*([0-9]{4})", b, re.I)
        if not yrm:
            continue
        year = int(yrm.group(1))
        hyd = re.search(r"Hydrology for Growing Season.*?\n\s*RAINFALL.*?\n\s*([-+0-9.Ee\s]+)", b, re.I | re.S)
        yld = re.search(r"Yield Results for Growing Season.*?overall\s*\n\s*([-+0-9.Ee\s]+)", b, re.I | re.S)
        row = {"scenario_name": sc.scenario_name, "folder": str(sc.parent), "year": year}
        if hyd:
            vals = hyd.group(1).split()
            hcols = ["gs_rain_cm", "gs_pet_cm", "gs_aet_cm", "gs_drain_cm", "gs_runoff_cm", "gs_dry_days", "gs_work_days", "gs_sew", "gs_avg_salinity"]
            for c, v in zip(hcols, vals):
                row[c] = to_num(v)
        if yld:
            vals = yld.group(1).split()
            ycols = ["gs_sdi_excess", "gs_sdi_drought", "gs_plant_doy", "gs_plant_delay_days", "gs_harvest_doy", "gs_rel_yield_excess_pct", "gs_rel_yield_drought_pct", "gs_rel_yield_delay_pct", "gs_rel_yield_salinity_pct", "gs_rel_yield_overall_pct"]
            for c, v in zip(ycols, vals):
                row[c] = to_num(v)
        rows.append(row)
    return pd.DataFrame(rows)


def collect_expected_files(sc: ScenarioFiles) -> dict:
    expected = [".CRO", ".DAY", ".MON", ".MRK", ".OST", ".OUT", ".PLT", ".RNK", ".WTB", ".YLD", ".YR"]
    flags = {}
    for ext in expected:
        p = find_file(sc, ext)
        flags[f"has_{ext[1:].lower()}"] = p is not None and p.exists() and p.stat().st_size > 0
        flags[f"size_{ext[1:].lower()}_kb"] = round(p.stat().st_size / 1024, 2) if p and p.exists() else 0
    for suffix in ["_MZOVERVIEW.OUT", "_MZPlantGro.OUT", "_MZPlantN.OUT"]:
        p = None
        for f in sc.files.values():
            if f.name.upper() == (sc.scenario_name + suffix).upper():
                p = f
                break
        key = suffix.replace(".", "_").replace("_", "").lower()
        flags[f"has_{key}"] = p is not None and p.exists() and p.stat().st_size > 0
        flags[f"size_{key}_kb"] = round(p.stat().st_size / 1024, 2) if p and p.exists() else 0
    return flags


def summarize_scenario(sc: ScenarioFiles, metadata: dict, out_info: dict, yr_df: pd.DataFrame, yld_df: pd.DataFrame, wtb_stats: pd.DataFrame) -> dict:
    row = {"scenario_name": sc.scenario_name, "folder": str(sc.parent)}
    row.update(infer_parts_from_scenario(sc.scenario_name))
    for k in ["county", "pixel_id", "mode_code", "mode_name", "spacing_name", "spacing_ft", "spacing_cm", "drain_depth_cm", "run_start_date", "run_end_date"]:
        if k in metadata:
            row[k] = metadata[k]
    row.update(out_info)
    row.update(collect_expected_files(sc))

    yy = yr_df[yr_df["year"].ne("AVG")].copy() if not yr_df.empty else pd.DataFrame()
    avgy = yr_df[yr_df["year"].eq("AVG")].copy() if not yr_df.empty and "year" in yr_df.columns else pd.DataFrame()
    if not avgy.empty:
        for c in avgy.columns:
            if c not in {"scenario_name", "folder", "year"}:
                row[f"avg_{c}"] = avgy.iloc[0][c]
    elif not yy.empty:
        for c in yy.select_dtypes(include="number").columns:
            row[f"avg_{c}"] = yy[c].mean()

    yd = yld_df[yld_df["year"].ne("AVG")].copy() if not yld_df.empty else pd.DataFrame()
    avgyld = yld_df[yld_df["year"].eq("AVG")].copy() if not yld_df.empty and "year" in yld_df.columns else pd.DataFrame()
    if not avgyld.empty:
        for c in avgyld.columns:
            if c not in {"scenario_name", "folder", "year"}:
                row[f"avg_{c}"] = avgyld.iloc[0][c]
    elif not yd.empty:
        for c in yd.select_dtypes(include="number").columns:
            row[f"avg_{c}"] = yd[c].mean()
    if not yd.empty and "rel_yield_overall_pct" in yd.columns:
        best = yd.loc[yd["rel_yield_overall_pct"].idxmax()]
        worst = yd.loc[yd["rel_yield_overall_pct"].idxmin()]
        row["best_yield_year"] = best["year"]
        row["best_rel_yield_pct"] = best["rel_yield_overall_pct"]
        row["worst_yield_year"] = worst["year"]
        row["worst_rel_yield_pct"] = worst["rel_yield_overall_pct"]
        row["years_rel_yield_below_50"] = int((yd["rel_yield_overall_pct"] < 50).sum())
        row["years_rel_yield_below_75"] = int((yd["rel_yield_overall_pct"] < 75).sum())
    if not yy.empty:
        for c in ["rainfall_cm", "et_cm", "drain_cm", "runoff_cm", "dry_days", "sew", "pump_cm"]:
            if c in yy.columns:
                row[f"total_{c}"] = yy[c].sum()
    if not wtb_stats.empty:
        ws = wtb_stats[wtb_stats["year"].astype(str).ne("AVG")]
        for c in ["wtb_depth_mean_cm", "days_wt_shallower_30cm", "days_wt_shallower_50cm", "days_wt_deeper_100cm"]:
            if c in ws.columns:
                row[f"avg_{c}"] = ws[c].mean()
    return row


def safe_sheet_name(name: str) -> str:
    name = re.sub(r"[\\/*?:\[\]]", "_", name)[:31]
    return name or "Sheet"


def write_outputs(summary_dir: Path, tables: dict[str, pd.DataFrame]) -> None:
    summary_dir.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        if df is None or df.empty:
            continue
        csv_path = summary_dir / f"{name}.csv"
        df.to_csv(csv_path, index=False)
    xlsx_path = summary_dir / "drainmod_full_summary.xlsx"
    try:
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            for name, df in tables.items():
                if df is None or df.empty:
                    continue
                if len(df) > 1_048_000:
                    small = df.head(1_048_000)
                    small.to_excel(writer, sheet_name=safe_sheet_name(name), index=False)
                else:
                    df.to_excel(writer, sheet_name=safe_sheet_name(name), index=False)
        print(f"Excel summary written: {xlsx_path}")
    except Exception as e:
        print("Could not write Excel workbook. CSV files were still written.")
        print(f"Excel error: {e}")


def main():
    try:
        root = prompt_path("DRAINMOD output root folder", DEFAULT_ROOT)
        summary_dir = prompt_path("Summary output folder", DEFAULT_SUMMARY)
        parse_daily = prompt_bool("Also export DAY file monthly aggregates? This can be slower", False)
        export_wtb_daily = prompt_bool("Also export full daily water table table? This can be large", False)
        root = root.expanduser()
        summary_dir = summary_dir.expanduser()
        if not root.exists():
            raise FileNotFoundError(f"Output root does not exist: {root}")

        scenarios = discover_scenarios(root)
        print(f"Found {len(scenarios)} scenario groups.")

        inventory_rows = []
        overview_rows = []
        metadata_rows = []
        errors = []
        annual_water_tables = []
        annual_yield_tables = []
        monthly_tables = []
        day_monthly_tables = []
        wtb_daily_tables = []
        wtb_stats_tables = []
        crop_growing_tables = []
        mz_growth_tables = []
        mz_n_tables = []

        for i, sc in enumerate(scenarios, 1):
            print(f"[{i}/{len(scenarios)}] {sc.scenario_name}")
            try:
                for p in sorted(sc.files.values(), key=lambda x: x.name):
                    inventory_rows.append(file_inventory_row(sc, p))
                meta = parse_metadata(sc)
                if meta:
                    mrow = {"scenario_name": sc.scenario_name, "folder": str(sc.parent)}
                    mrow.update(meta)
                    metadata_rows.append(mrow)

                out_info = {}
                out_file = find_file(sc, ".OUT")
                if out_file:
                    out_info = parse_out(out_file)

                yr_df = pd.DataFrame()
                yr_file = find_file(sc, ".YR")
                if yr_file:
                    yr_df = parse_yr(yr_file, sc)
                    if not yr_df.empty:
                        annual_water_tables.append(yr_df)

                yld_df = pd.DataFrame()
                yld_file = find_file(sc, ".YLD")
                if yld_file:
                    yld_df = parse_yld(yld_file, sc)
                    if not yld_df.empty:
                        annual_yield_tables.append(yld_df)

                mon_file = find_file(sc, ".MON")
                if mon_file:
                    mon_df = parse_mon(mon_file, sc)
                    if not mon_df.empty:
                        monthly_tables.append(mon_df)

                if parse_daily:
                    day_file = find_file(sc, ".DAY")
                    if day_file:
                        day_df = parse_day_aggregate(day_file, sc)
                        if not day_df.empty:
                            day_monthly_tables.append(day_df)

                wtb_stats = pd.DataFrame()
                wtb_file = find_file(sc, ".WTB")
                if wtb_file:
                    wtb_df = parse_wtb(wtb_file, sc)
                    if not wtb_df.empty:
                        if export_wtb_daily:
                            wtb_daily_tables.append(wtb_df)
                        wtb_stats = aggregate_wtb(wtb_df)
                        if not wtb_stats.empty:
                            wtb_stats_tables.append(wtb_stats)

                cro_file = find_file(sc, ".CRO")
                if cro_file:
                    cro_df = parse_cro(cro_file, sc)
                    if not cro_df.empty:
                        crop_growing_tables.append(cro_df)

                mzgro = None
                for p in sc.files.values():
                    if p.name.upper() == (sc.scenario_name + "_MZPLANTGRO.OUT").upper():
                        mzgro = p
                        break
                if mzgro:
                    mz_df = parse_mz_table(mzgro, sc, "MZPlantGro")
                    mz_agg = aggregate_mz_growth(mz_df)
                    if not mz_agg.empty:
                        mz_growth_tables.append(mz_agg)

                mzn = None
                for p in sc.files.values():
                    if p.name.upper() == (sc.scenario_name + "_MZPLANTN.OUT").upper():
                        mzn = p
                        break
                if mzn:
                    mzn_df = parse_mz_table(mzn, sc, "MZPlantN")
                    mzn_agg = aggregate_mz_n(mzn_df)
                    if not mzn_agg.empty:
                        mz_n_tables.append(mzn_agg)

                overview_rows.append(summarize_scenario(sc, meta, out_info, yr_df, yld_df, wtb_stats))
            except Exception as e:
                errors.append({"scenario_name": sc.scenario_name, "folder": str(sc.parent), "error": str(e), "traceback": traceback.format_exc()})
                print(f"  Error: {e}")

        def cat(tables):
            return pd.concat(tables, ignore_index=True) if tables else pd.DataFrame()

        annual_water = cat(annual_water_tables)
        annual_yield = cat(annual_yield_tables)
        monthly_water = cat(monthly_tables)
        day_monthly = cat(day_monthly_tables)
        wtb_stats = cat(wtb_stats_tables)
        crop_growing = cat(crop_growing_tables)
        mz_growth = cat(mz_growth_tables)
        mz_n = cat(mz_n_tables)
        inventory = pd.DataFrame(inventory_rows)
        metadata = pd.DataFrame(metadata_rows)
        errors_df = pd.DataFrame(errors)
        overview = pd.DataFrame(overview_rows)

        annual_combined = pd.DataFrame()
        if not annual_water.empty:
            annual_combined = annual_water[annual_water["year"].ne("AVG")].copy()
            annual_combined["year"] = annual_combined["year"].astype(int)
        if not annual_yield.empty:
            y = annual_yield[annual_yield["year"].ne("AVG")].copy()
            y["year"] = y["year"].astype(int)
            keys = ["scenario_name", "folder", "year"]
            annual_combined = y if annual_combined.empty else annual_combined.merge(y, on=keys, how="outer", suffixes=("", "_yield"))
        for extra in [wtb_stats, crop_growing, mz_growth, mz_n]:
            if extra is not None and not extra.empty:
                keys = ["scenario_name", "folder", "year"]
                extra2 = extra.copy()
                extra2["year"] = extra2["year"].astype(int)
                annual_combined = extra2 if annual_combined.empty else annual_combined.merge(extra2, on=keys, how="outer")

        tables = {
            "scenario_overview": overview,
            "annual_combined": annual_combined,
            "annual_water_YR": annual_water,
            "annual_yield_YLD": annual_yield,
            "monthly_water_MON": monthly_water,
            "day_monthly_from_DAY": day_monthly,
            "water_table_stats_WTB": wtb_stats,
            "crop_growing_CRO": crop_growing,
            "mz_growth_summary": mz_growth,
            "mz_n_summary": mz_n,
            "metadata": metadata,
            "file_inventory": inventory,
            "errors": errors_df,
        }
        if export_wtb_daily:
            tables["water_table_daily_WTB"] = cat(wtb_daily_tables)

        write_outputs(summary_dir, tables)
        print("\nMain files written:")
        print(f"  {summary_dir / 'drainmod_full_summary.xlsx'}")
        print(f"  {summary_dir / 'scenario_overview.csv'}")
        print(f"  {summary_dir / 'annual_combined.csv'}")
        print(f"  {summary_dir / 'monthly_water_MON.csv'}")
        print("\nDone.")
    except Exception as e:
        print("\nFATAL ERROR")
        print(e)
        traceback.print_exc()
    finally:
        input("\nPress Enter to close this window...")


if __name__ == "__main__":
    main()
