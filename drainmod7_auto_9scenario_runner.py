# drainmod7_auto_9scenario_runner_v12_skip_completed_outputs.py
#
# Fully automatic DRAINMOD 7 county runner.
#
# What it does
# ------------
# For each county, pixel, drainage scenario, and drain-spacing class, this script:
#   1) reads the weather stations CSV and soil map CSV already created in DRAINMOD_GRID
#   2) builds a scenario-specific PRJ and GEN from working templates
#   3) stages the PRJ/GEN pair into C:\Drainmod7\inputs
#   4) launches DRAINMOD 7
#   5) triggers the simulation in the GUI
#   6) waits until <scenario_name>_MZPlantGro.OUT is larger than 50 KB
#   7) copies C:\Drainmod7\outputs into
#      C:\Drainmod7\DRAINMOD_GRID\OUTPUTS\<county>\<pixel>\<scenario>
#   8) skips scenarios that already have complete output files
#   9) cleans the runtime outputs and moves to the next scenario
#
# Default scenarios
# -----------------
# FD = free / conventional drainage
# CD = controlled drainage
# SI = subirrigation
#
# Spacing classes are set below in SCENARIO_SPACINGS_FT.
# Drain depth is set to 100 cm.
#
# Important note
# --------------
# This script patches PRJ/GEN templates rather than rebuilding files from scratch.
# That is intentional because DRAINMOD is sensitive to file structure and spacing.
# The conventional drainage part should work with the same templates used in your
# existing builder. Controlled drainage and subirrigation depend on where your
# working template stores water-management and subirrigation flags. The script
# includes conservative patch functions for common PRJ/GEN patterns and writes
# a scenario metadata file so each run is traceable.

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd
import psutil
from pywinauto import Application, Desktop

# =============================================================================
# USER CONFIGURATION
# =============================================================================

DRAINMOD_ROOT = Path(r"C:\Drainmod7")
GRID_ROOT = Path(r"C:\Drainmod7\DRAINMOD_GRID")
# County selection is requested interactively at runtime.
# Options: one typed county, or an Excel file where column 1 is order and column 2 is county name.

# Use your known-good templates here.
PRJ_TEMPLATE = GRID_ROOT / "INPUTS" / "test" / "PRJ" / "test01.prj"
GEN_TEMPLATE = GRID_ROOT / "INPUTS" / "test" / "GEN" / "test01.gen"

# Set to None for all pixels, or a list such as ["000001", "000002"] for testing.
PIXELS_TO_RUN: Optional[list[str]] = None

DEFAULT_RUN_START_DATE = "01-01-1996"
DEFAULT_RUN_END_DATE = "12-31-2025"

DRAIN_DEPTH_CM = 100.0
FT_TO_CM = 30.48

SCENARIO_SPACINGS_FT = {
    "intense": 20.0,
    "medium": 60.0,
    "low": 100.0,
}

DRAINAGE_MODES = {
    "FD": "conventional_drainage",
    "CD": "controlled_drainage",
    "SI": "subirrigation",
}

# DRAINMOD GUI radio button under Options > Subsurface Water Mgmt.
# 1 = Conventional Drainage; 2 = Controlled Drainage; 3 = Sub-irrigation Drainage; 4 = Combined.
DRAINMODE_GUI_CODE = {"FD": "1", "CD": "2", "SI": "3"}

# Controlled drainage / subirrigation board schedule, using cm below soil surface.
# DRAINMOD normally interprets the control elevation/depth according to its internal
# file format. The script stores and attempts to patch this schedule where matching
# sections exist in your template.
BOARD_SCHEDULE = [
    {"start": "01-01", "end": "04-30", "board_cm": 60.0},
    {"start": "05-01", "end": "05-14", "board_cm": 100.0},
    {"start": "05-15", "end": "08-15", "board_cm": 30.0},
    {"start": "08-16", "end": "10-15", "board_cm": 100.0},
    {"start": "10-16", "end": "12-31", "board_cm": 50.0},
]

# Runtime behavior.
MIN_MZPLANTGRO_KB = 50
MAX_RUN_MINUTES = 45
WAIT_AFTER_LAUNCH_SEC = 3.5
WAIT_AFTER_CLICK_SEC = 1.2
ENGINE_START_TIMEOUT_SEC = 30
CONTINUE_ON_ERROR = True
CLEAN_ROOT_OUTPUTS_BEFORE_EACH_RUN = True
KEEP_RUNTIME_FILES = False

# Debug behavior. This keeps a detailed log so crashes are visible after the window closes.
DEBUG_LOG_DIR = GRID_ROOT / "INPUTS" / "RUN_LOGS"
PAUSE_ON_EXIT = True

# Resume behavior. When True, the script checks the final output folder first and
# skips scenarios that already have all expected output files with size >= 1 KB.
DEFAULT_SKIP_COMPLETED_SCENARIOS = True
MIN_COMPLETED_OUTPUT_KB = 0  # resume check: each expected file must be > 0 bytes



def make_run_log_path() -> Path:
    """Create a timestamped run log path."""
    DEBUG_LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    return DEBUG_LOG_DIR / f"drainmod_auto_run_{ts}.log"


def log_message(run_log_path: Path, message: str) -> None:
    """Write a message to screen and to the run log."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        run_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(run_log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_exception(run_log_path: Path, context: str, exc: BaseException) -> None:
    """Write a full traceback to the run log and print the short error."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        print(f"[{stamp}] ERROR in {context}: {exc}", flush=True)
    except Exception:
        pass
    try:
        run_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(run_log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[{stamp}] ERROR in {context}: {exc}\n")
            f.write(traceback.format_exc())
            f.write("\n")
    except Exception:
        pass

# Performance/process behavior.
# This lets Windows schedule DRAINMOD and its engine processes on all available CPU cores.
# It does not make a single-threaded DRAINMOD calculation truly multi-core, but it avoids
# artificial affinity limits and gives the run higher scheduling priority.
SET_PROCESS_AFFINITY_TO_ALL_CORES = True
SET_PROCESS_PRIORITY_HIGH = True

ENGINE_NAMES = {"dmhydro.exe", "dmnii.exe", "dmdssat.exe"}
DRAINMOD_GUI_NAMES = {"drainmod7.exe"}
DRAINMOD_RELATED_NAMES = ENGINE_NAMES | DRAINMOD_GUI_NAMES
BAD_TITLE_HINTS = [
    "pycharm", "visual studio", "vscode", "explorer", "powershell",
    "command prompt", "terminal", "windows terminal", "notepad", "chatgpt",
]
BAD_CLASS_HINTS = ["SunAwtFrame", "Chrome_WidgetWin", "CabinetWClass"]


# =============================================================================
# GENERAL UTILITIES
# =============================================================================

def normalize_pixel_id(value: str) -> str:
    s = str(value).strip()
    if not s:
        return s
    if s.isdigit():
        return s.zfill(6)
    return s


def winpath(pathlike) -> str:
    return str(pathlike).replace("/", "\\")


def read_clean_lines(path: Path) -> list[str]:
    text = path.read_text(errors="ignore")
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def write_ascii_crlf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"
    path.write_bytes(normalized.replace("\n", "\r\n").encode("ascii", "ignore"))


def parse_run_date(value: str, default: str) -> str:
    """Return a date string as MM-DD-YYYY. Accepts YYYY, MM-YYYY, MM-DD-YYYY, and slash variants."""
    raw = (value or "").strip() or default
    raw = raw.replace("/", "-").replace(".", "-").strip()
    raw = re.sub(r"\s+", "-", raw)

    if re.fullmatch(r"\d{4}", raw):
        year = int(raw)
        month = 1
        day = 1
    else:
        parts = raw.split("-")
        if len(parts) == 2 and re.fullmatch(r"\d{1,2}", parts[0]) and re.fullmatch(r"\d{4}", parts[1]):
            month = int(parts[0]); day = 1; year = int(parts[1])
        elif len(parts) == 2 and re.fullmatch(r"\d{4}", parts[0]) and re.fullmatch(r"\d{1,2}", parts[1]):
            year = int(parts[0]); month = int(parts[1]); day = 1
        elif len(parts) == 3 and re.fullmatch(r"\d{1,2}", parts[0]) and re.fullmatch(r"\d{1,2}", parts[1]) and re.fullmatch(r"\d{4}", parts[2]):
            month = int(parts[0]); day = int(parts[1]); year = int(parts[2])
        elif len(parts) == 3 and re.fullmatch(r"\d{4}", parts[0]) and re.fullmatch(r"\d{1,2}", parts[1]) and re.fullmatch(r"\d{1,2}", parts[2]):
            year = int(parts[0]); month = int(parts[1]); day = int(parts[2])
        else:
            raise ValueError(f"Invalid date '{value}'. Use MM-DD-YYYY, MM-YYYY, or YYYY.")

    import datetime as _dt
    _dt.date(year, month, day)
    return f"{month:02d}-{day:02d}-{year:04d}"


def date_year(date_text: str) -> int:
    return int(date_text.split("-")[2])


def date_month(date_text: str) -> int:
    return int(date_text.split("-")[0])


def patch_gen_climate_period(lines: list[str], idx_climate: int, start_date: str, end_date: str) -> tuple[list[str], list[str]]:
    notes: list[str] = []
    period_idx = idx_climate + 3
    if period_idx >= len(lines):
        raise ValueError("GEN template is missing the climate-period line after the weather-file lines.")
    old_line = lines[period_idx]
    tokens = old_line.split()
    if len(tokens) < 4:
        raise ValueError(f"Could not parse GEN climate-period line: {old_line!r}")

    sy = date_year(start_date)
    sm = date_month(start_date)
    ey = date_year(end_date)
    em = date_month(end_date)

    # DRAINMOD reads this line as fixed-width text. The original Larson line is:
    # 1991  1 2009 12 4212  51 0 450
    # The first four fields are: start year, start month, end year, end month.
    # The remaining control fields must stay in the same columns. In particular,
    # the "5" in "51" must begin at column 23, not column 22.
    rest = tokens[4:]
    new_line = f"{sy:4d}{sm:3d}{ey:5d}{em:3d}"

    if len(rest) >= 1:
        new_line += f"{int(float(rest[0])):5d}"
    if len(rest) >= 2:
        new_line += f"{int(float(rest[1])):4d}"
    if len(rest) >= 3:
        new_line += " " + " ".join(rest[2:])

    # Safety check requested after DRAINMOD crashed: for a Larson-style line,
    # character column 23 must contain the first digit of the second control field.
    if len(rest) >= 2 and len(new_line) >= 23 and str(rest[1]).strip()[0].isdigit():
        expected_digit = str(rest[1]).strip()[0]
        actual_digit = new_line[22]  # Python index 22 = column 23
        if actual_digit != expected_digit:
            raise ValueError(
                f"GEN climate-period fixed-width check failed. Expected {expected_digit!r} at column 23, "
                f"but found {actual_digit!r}. Line: {new_line!r}"
            )

    lines[period_idx] = preserve_leading_whitespace(old_line, new_line.lstrip())
    notes.append(f"Patched GEN climate period line: {old_line.strip()} -> {lines[period_idx].strip()}")
    return lines, notes


def prompt_run_period() -> tuple[str, str]:
    while True:
        start_raw = input(f"Start simulation date MM-DD-YYYY [{DEFAULT_RUN_START_DATE}]: ").strip()
        end_raw = input(f"End simulation date MM-DD-YYYY [{DEFAULT_RUN_END_DATE}]: ").strip()
        try:
            start_date = parse_run_date(start_raw, DEFAULT_RUN_START_DATE)
            end_date = parse_run_date(end_raw, DEFAULT_RUN_END_DATE)
            import datetime as _dt
            sm, sd, sy = [int(x) for x in start_date.split("-")]
            em, ed, ey = [int(x) for x in end_date.split("-")]
            if _dt.date(ey, em, ed) < _dt.date(sy, sm, sd):
                raise ValueError("End simulation date cannot be earlier than start simulation date.")
            return start_date, end_date
        except ValueError as e:
            print(e)


def read_counties_from_excel(excel_path: Path) -> list[str]:
    """Read counties from Excel: column 1 = order, column 2 = county name."""
    if not excel_path.exists() or not excel_path.is_file():
        raise FileNotFoundError(f"County Excel file not found: {excel_path}")

    df = pd.read_excel(excel_path, header=None, dtype=object)
    if df.shape[1] < 2:
        raise ValueError("County Excel file must have at least two columns: order in column 1 and county name in column 2.")

    df = df.iloc[:, :2].copy()
    df.columns = ["order", "county"]
    df["county"] = df["county"].astype(str).str.strip()
    df = df[df["county"].str.len() > 0]
    df = df[~df["county"].str.lower().isin({"nan", "county", "county name", "name"})]

    if df.empty:
        raise ValueError("No county names were found in column 2 of the Excel file.")

    order_numeric = pd.to_numeric(df["order"], errors="coerce")
    if order_numeric.notna().any():
        df = df.assign(_order=order_numeric)
        df = df.sort_values(by="_order", na_position="last", kind="stable")

    counties = []
    seen = set()
    for county in df["county"].tolist():
        county = str(county).strip()
        key = county.lower()
        if county and key not in seen:
            counties.append(county)
            seen.add(key)

    if not counties:
        raise ValueError("No valid county names were found in the Excel file.")
    return counties


def prompt_counties_to_run() -> list[str]:
    """Ask whether to run one county or an Excel list of counties."""
    while True:
        mode = input("County input mode: single county or Excel county list? (single/excel) [single]: ").strip().lower()
        if not mode:
            mode = "single"
        if mode in {"single", "s", "one", "1"}:
            county = input("County name [Champaign]: ").strip() or "Champaign"
            return [county]
        if mode in {"excel", "xlsx", "spreadsheet", "file", "2"}:
            raw = input("Excel file path with column 1 = order and column 2 = county name: ").strip().strip('"').strip("'")
            try:
                counties = read_counties_from_excel(Path(raw).expanduser())
                print(f"Counties loaded from Excel: {counties}")
                return counties
            except Exception as e:
                print(f"Could not read county Excel file: {e}")
                continue
        print("Please enter 'single' or 'excel'.")


def prompt_yes_no(message: str, default: bool = True) -> bool:
    shown = "y" if default else "n"
    while True:
        raw = input(f"{message} (y/n) [{shown}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "true", "1"}:
            return True
        if raw in {"n", "no", "false", "0"}:
            return False
        print("Please enter y or n.")


def preserve_leading_whitespace(template_line: str, new_content: str) -> str:
    m = re.match(r"^(\s*)", template_line)
    return (m.group(1) if m else "") + new_content


def _split_assignment_line(line: str):
    if "=" not in line:
        return None
    left, right = line.split("=", 1)
    key = left.strip()
    if not key or key.startswith("["):
        return None
    return key, right


def set_prj_value(lines: list[str], section_name: str, key_name: str, value: str) -> list[str]:
    section = ""
    changed = False
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip()
            continue
        parsed = _split_assignment_line(ln)
        if parsed is None:
            continue
        key, _ = parsed
        if section.lower() == section_name.lower() and key == key_name:
            lines[i] = f"{key_name}={value}"
            changed = True
            break
    if not changed:
        lines.extend(["", f"[{section_name}]", f"{key_name}={value}"])
    return lines


def patch_prj_run_period(lines: list[str], start_date: str, end_date: str) -> tuple[list[str], list[str]]:
    notes: list[str] = []
    start_year = date_year(start_date)
    end_year = date_year(end_date)

    candidate_start_date_keys = {
        "startdate", "start_date", "simulationstartdate", "simulation_start_date",
        "begindate", "begin_date", "firstdate", "first_date", "start", "begin"
    }
    candidate_end_date_keys = {
        "enddate", "end_date", "simulationenddate", "simulation_end_date",
        "lastdate", "last_date", "finaldate", "final_date", "end"
    }
    candidate_start_year_keys = {
        "startyear", "start_year", "simulationstartyear", "simulation_start_year",
        "beginyear", "begin_year", "firstyear", "first_year", "iyr1", "iyear1"
    }
    candidate_end_year_keys = {
        "endyear", "end_year", "simulationendyear", "simulation_end_year",
        "lastyear", "last_year", "finalyear", "final_year", "iyr2", "iyear2"
    }

    for i, ln in enumerate(lines):
        parsed = _split_assignment_line(ln)
        if parsed is None:
            continue
        key, _ = parsed
        low = key.strip().lower()
        if low in candidate_start_date_keys:
            lines[i] = f"{key}={start_date}"
            notes.append(f"Patched existing PRJ date key {key}={start_date}")
        elif low in candidate_end_date_keys:
            lines[i] = f"{key}={end_date}"
            notes.append(f"Patched existing PRJ date key {key}={end_date}")
        elif low in candidate_start_year_keys:
            lines[i] = f"{key}={start_year}"
            notes.append(f"Patched existing PRJ year key {key}={start_year}")
        elif low in candidate_end_year_keys:
            lines[i] = f"{key}={end_year}"
            notes.append(f"Patched existing PRJ year key {key}={end_year}")

    lines = set_prj_value(lines, "Analysis", "StartDate", start_date)
    lines = set_prj_value(lines, "Analysis", "EndDate", end_date)
    notes.append(f"Wrote [Analysis] StartDate={start_date} and EndDate={end_date}")

    date_replacements = [
        (re.compile(r"(?<!\d)\d{1,2}[-/]\d{1,2}[-/]1991(?!\d)"), start_date),
        (re.compile(r"(?<!\d)\d{1,2}[-/]\d{1,2}[-/]2009(?!\d)"), end_date),
        (re.compile(r"(?<!\d)1991[-/]\d{1,2}[-/]\d{1,2}(?!\d)"), start_date),
        (re.compile(r"(?<!\d)2009[-/]\d{1,2}[-/]\d{1,2}(?!\d)"), end_date),
    ]
    for pattern, repl in date_replacements:
        count = 0
        for i, ln in enumerate(lines):
            new_ln, n = pattern.subn(repl, ln)
            if n:
                lines[i] = new_ln
                count += n
        if count:
            notes.append(f"Replaced {count} hard-coded template date(s) with {repl}")

    year_replacements = [
        (re.compile(r"(?<!\d)1991(?!\d)"), str(start_year)),
        (re.compile(r"(?<!\d)2009(?!\d)"), str(end_year)),
    ]
    for pattern, repl in year_replacements:
        count = 0
        for i, ln in enumerate(lines):
            new_ln, n = pattern.subn(repl, ln)
            if n:
                lines[i] = new_ln
                count += n
        if count:
            notes.append(f"Replaced {count} standalone fallback year value(s) with {repl}")

    return lines, notes

def replace_station_line_keep_first_token_column(template_line: str, station_id: str, filepath: Path) -> str:
    m = re.match(r"^(\s*)\S+\s+.*$", template_line)
    prefix = m.group(1) if m else " " * 5
    return f"{prefix}{str(station_id).strip()} {winpath(filepath)}"


def _replace_token_in_template_line(template_line: str, token_index: int, new_value: str) -> str:
    matches = list(re.finditer(r"[-+]?\d+(?:\.\d+)?(?:E[+-]?\d+)?", template_line))
    if token_index >= len(matches):
        raise IndexError(f"Token index {token_index} out of range for line: {template_line}")
    m = matches[token_index]
    width = m.end() - m.start()
    value = str(new_value).rjust(width)
    if len(value) > width:
        value = value[-width:]
    chars = list(template_line)
    chars[m.start():m.end()] = list(value)
    return "".join(chars)


def is_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> None:
    if os.name != "nt":
        return
    import ctypes
    args = " ".join(f'"{a}"' for a in sys.argv)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, args, None, 1)
    if rc <= 32:
        raise RuntimeError(f"Failed to elevate process. ShellExecuteW returned {rc}.")


def ensure_admin_or_relaunch() -> None:
    if os.name == "nt" and not is_admin():
        print("Requesting Administrator privileges...")
        relaunch_as_admin()
        raise SystemExit(0)


# =============================================================================
# INPUT DISCOVERY
# =============================================================================

def auto_find_stations_csv(grid_root: Path, county: str) -> Path:
    weather_county = grid_root / "WEATHER" / county
    exact = weather_county / f"stations_{county}.csv"
    if exact.exists():
        return exact
    candidates = sorted(weather_county.glob(f"stations_{county}_*.csv"))
    if not candidates:
        candidates = sorted(weather_county.glob("stations_*.csv"))
    if not candidates:
        raise FileNotFoundError(f"No stations CSV found in: {weather_county}")
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def auto_find_soil_map_csv(grid_root: Path, county: str) -> Path:
    soil_csv = grid_root / "SOIL" / county / "drainmod7_soil_map_SOIL.csv"
    if not soil_csv.exists():
        raise FileNotFoundError(f"Soil map CSV not found: {soil_csv}")
    return soil_csv


def load_stations_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str)
    cols = {c.strip().lower(): c for c in df.columns}
    if "pixel_id" not in cols:
        raise ValueError(f"Stations CSV is missing pixel_id column: {path}")
    out = df.rename(columns={cols["pixel_id"]: "pixel_id"}).copy()
    for name in ["station6", "weather_id", "pixel_label", "lat", "lon", "elev_m"]:
        if name in cols and cols[name] != name:
            out = out.rename(columns={cols[name]: name})
    out["pixel_id"] = out["pixel_id"].astype(str).map(normalize_pixel_id)
    return out


def load_soil_map_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str)
    required = {"pixel_id", "dmn_file", "sin_file", "mis_file", "wdv_file"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Soil map CSV is missing required columns: {sorted(missing)}")
    out = df.copy()
    out["pixel_id"] = out["pixel_id"].astype(str).map(normalize_pixel_id)
    return out


def find_weather_file(weather_root: Path, pixel_id: str, token: str, suffix: str) -> Optional[Path]:
    pid = normalize_pixel_id(pixel_id)
    candidates = []
    for subdir in [suffix.upper(), suffix.lower(), token.upper(), token.lower()]:
        d = weather_root / subdir
        if d.exists():
            candidates.extend(sorted(d.glob(f"{pid}*{token}*.{suffix}")))
            candidates.extend(sorted(d.glob(f"{pid}*.{suffix}")))
            candidates.extend(sorted(d.glob(f"{pid}*.{suffix.lower()}")))
            candidates.extend(sorted(d.glob(f"{pid}*.{suffix.upper()}")))
    candidates.extend(sorted(weather_root.glob(f"{pid}*{token}*.{suffix}")))
    candidates.extend(sorted(weather_root.glob(f"{pid}*.{suffix}")))
    seen = []
    for c in candidates:
        if c not in seen:
            seen.append(c)
    return seen[0] if seen else None


# =============================================================================
# TEMPLATE PATCHING
# =============================================================================

def find_section_line(lines: list[str], prefix: str) -> int:
    for idx, ln in enumerate(lines):
        if ln.strip().startswith(prefix):
            return idx
    return -1


def patch_common_control_schedule(lines: list[str], mode_code: str) -> tuple[list[str], list[str]]:
    """
    Conservative attempt to patch common control/subirrigation keys if they exist.
    It does not delete or reorder template content.
    It appends a clear metadata block at the end for traceability.
    """
    notes = []

    # PRJ-style key-value patches. If your template already has these keys, they will be replaced.
    # If not, they are added to a [ScenarioManagement] section. DRAINMOD may ignore that section,
    # but it keeps every generated file documented.
    mode_name = DRAINAGE_MODES[mode_code]
    lines = set_prj_value(lines, "ScenarioManagement", "ScenarioMode", mode_code)
    lines = set_prj_value(lines, "ScenarioManagement", "ScenarioModeName", mode_name)
    lines = set_prj_value(lines, "ScenarioManagement", "DrainDepthCm", f"{DRAIN_DEPTH_CM:.2f}")

    if mode_code == "FD":
        lines = set_prj_value(lines, "ScenarioManagement", "WaterControl", "0")
        lines = set_prj_value(lines, "ScenarioManagement", "Subirrigation", "0")
        notes.append("FD scenario: conventional/free drainage metadata written.")
    elif mode_code == "CD":
        lines = set_prj_value(lines, "ScenarioManagement", "WaterControl", "1")
        lines = set_prj_value(lines, "ScenarioManagement", "Subirrigation", "0")
        notes.append("CD scenario: controlled drainage metadata written.")
    elif mode_code == "SI":
        lines = set_prj_value(lines, "ScenarioManagement", "WaterControl", "1")
        lines = set_prj_value(lines, "ScenarioManagement", "Subirrigation", "1")
        notes.append("SI scenario: controlled drainage plus subirrigation metadata written.")

    for i, item in enumerate(BOARD_SCHEDULE, start=1):
        lines = set_prj_value(
            lines,
            "ScenarioManagement",
            f"BoardSchedule{i}",
            f"{item['start']},{item['end']},{item['board_cm']:.2f}",
        )

    # Replace common existing key names if your template contains them.
    common_keys = [
        ("WaterControl", "WaterControl"),
        ("Subirrigation", "Subirrigation"),
        ("ControlledDrainage", "WaterControl"),
        ("SubIrrigation", "Subirrigation"),
        ("Irrigation", "Subirrigation"),
    ]
    values = {
        "WaterControl": "1" if mode_code in {"CD", "SI"} else "0",
        "Subirrigation": "1" if mode_code == "SI" else "0",
    }
    section = ""
    for idx, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip()
            continue
        parsed = _split_assignment_line(ln)
        if parsed is None:
            continue
        key, _ = parsed
        for key_name, logical_name in common_keys:
            if key.lower() == key_name.lower():
                lines[idx] = f"{key}={values[logical_name]}"
                notes.append(f"Patched existing key [{section}] {key}={values[logical_name]}")

    return lines, notes


def patch_gen_mode_and_schedule(lines: list[str], mode_code: str) -> tuple[list[str], list[str]]:
    """
    Attempts minimal GEN patches without breaking a known-good template.
    Drain depth and spacing are patched elsewhere.
    """
    notes = []

    # If the template has a recognizable water-management section, append a metadata block.
    # DRAINMOD may ignore comments/unknown lines depending on the section, so these are appended
    # at the end rather than inserted into a strict numeric section.
    lines.append("")
    lines.append(f"*** Scenario metadata written by drainmod7_auto_9scenario_runner.py")
    lines.append(f"*** Scenario mode: {mode_code} ({DRAINAGE_MODES[mode_code]})")
    if mode_code in {"CD", "SI"}:
        for item in BOARD_SCHEDULE:
            lines.append(f"*** Board schedule: {item['start']} to {item['end']} = {item['board_cm']:.2f} cm")
        if mode_code == "SI":
            lines.append("*** Subirrigation enabled by scenario selection; verify template-specific SI flag if DRAINMOD does not pump water.")
    notes.append("GEN scenario metadata appended.")
    return lines, notes


def build_gen_from_template(
    gen_template: Path,
    weather_id: str,
    rain_path: Path,
    tem_path: Path,
    depth_cm: float,
    spacing_cm: float,
    outpath: Path,
    mode_code: str,
    run_start_date: str,
    run_end_date: str,
) -> tuple[str, list[str]]:
    lines = read_clean_lines(gen_template)
    notes = []

    idx_print = find_section_line(lines, "*** Printout and Input Control")
    idx_climate = find_section_line(lines, "*** Climate")
    idx_drain = find_section_line(lines, "*** Drainage System Design")
    if min(idx_print, idx_climate, idx_drain) == -1:
        raise ValueError("GEN template is missing Printout, Climate, or Drainage System Design section.")

    parts = lines[idx_print + 1].split(maxsplit=2)
    if len(parts) >= 2:
        lines[idx_print + 1] = preserve_leading_whitespace(lines[idx_print + 1], f"{parts[0]} {parts[1]} {winpath(outpath)}")

    # In the GEN climate section, the first file line is rainfall and the second file line is temperature.
    # The PET file is referenced in the PRJ file for this Larson-style template.
    lines[idx_climate + 1] = replace_station_line_keep_first_token_column(lines[idx_climate + 1], weather_id, rain_path)
    lines[idx_climate + 2] = replace_station_line_keep_first_token_column(lines[idx_climate + 2], weather_id, tem_path)
    lines, period_notes = patch_gen_climate_period(lines, idx_climate, run_start_date, run_end_date)
    notes.extend(period_notes)

    # DRAINMOD reads the GUI radio button for Subsurface Water Mgmt. from the
    # first numeric line under *** Drainage System Design *** in this GEN template.
    # 1 = Conventional Drainage, 2 = Controlled Drainage, 3 = Sub-irrigation Drainage.
    drainage_mode_gen_code = {"FD": "1", "CD": "2", "SI": "3"}[mode_code]
    old_drainage_mode_line = lines[idx_drain + 1]
    lines[idx_drain + 1] = preserve_leading_whitespace(lines[idx_drain + 1], drainage_mode_gen_code)
    notes.append(f"Patched GEN drainage mode line: {old_drainage_mode_line.strip()} -> {drainage_mode_gen_code}")
    design_line = lines[idx_drain + 2]
    design_line = _replace_token_in_template_line(design_line, 0, f"{depth_cm:.2f}")
    design_line = _replace_token_in_template_line(design_line, 2, f"{spacing_cm:.2f}")
    lines[idx_drain + 2] = design_line

    lines, mode_notes = patch_gen_mode_and_schedule(lines, mode_code)
    notes.extend(mode_notes)
    return "\r\n".join(lines) + "\r\n", notes


def build_prj_from_template(
    prj_template: Path,
    county_name: str,
    weather_root: Path,
    scenario_name: str,
    dmn_path: Path,
    sin_path: Path,
    mis_path: Path,
    wdv_path: Path,
    rain_path: Path,
    tem_path: Path,
    pet_path: Path,
    rad_path: Path,
    spacing_cm: float,
    depth_cm: float,
    mode_code: str,
    run_start_date: str,
    run_end_date: str,
) -> tuple[str, list[str]]:
    lines = read_clean_lines(prj_template)
    runtime_gen_path = DRAINMOD_ROOT / "inputs" / f"{scenario_name}.gen"

    lines = set_prj_value(lines, "General", "Hydrology", winpath(runtime_gen_path))
    lines = set_prj_value(lines, "General", "Nitrogen", winpath(dmn_path))

    lines = set_prj_value(lines, "Soils", "SoilFile", winpath(sin_path))
    lines = set_prj_value(lines, "Soils", "SoilWater", winpath(mis_path))
    lines = set_prj_value(lines, "Soils", "VolDrained", winpath(wdv_path))

    lines = set_prj_value(lines, "Weather", "Rainfall", winpath(rain_path))
    lines = set_prj_value(lines, "Weather", "Temperature", winpath(tem_path))
    lines = set_prj_value(lines, "Weather", "PET", winpath(pet_path))
    lines = set_prj_value(lines, "Weather", "RAD", winpath(rad_path))

    # This key controls the GUI radio button in Options > Subsurface Water Mgmt.
    # Without this patch, DRAINMOD keeps the template default, usually conventional drainage.
    lines = set_prj_value(lines, "Analysis", "DrainMode", DRAINMODE_GUI_CODE[mode_code])
    lines = set_prj_value(lines, "Analysis", "DrainDepth", f"{depth_cm:.2f},{depth_cm:.2f},    10")
    lines = set_prj_value(lines, "Analysis", "DrainSpace", f"  {spacing_cm:.2f},  {spacing_cm:.2f},      100")

    lines = set_prj_value(lines, "Path", "Outpath", winpath(DRAINMOD_ROOT / "outputs"))
    lines = set_prj_value(lines, "FolderSettings", "GeneralFolder", winpath(DRAINMOD_ROOT / "inputs"))
    lines = set_prj_value(lines, "FolderSettings", "SoilFolder", winpath(DRAINMOD_ROOT / "soils"))
    lines = set_prj_value(lines, "FolderSettings", "WeatherFolder", winpath(weather_root))
    lines = set_prj_value(lines, "FolderSettings", "CropFolder", winpath(GRID_ROOT / "CROP"))
    lines = set_prj_value(lines, "FolderSettings", "OutputFolder", winpath(DRAINMOD_ROOT / "outputs"))
    lines = set_prj_value(lines, "FolderSettings", "BackupFolder", winpath(DRAINMOD_ROOT / "outputs"))
    lines = set_prj_value(lines, "FolderSettings", "DssatFolder", winpath(DRAINMOD_ROOT / "dssat"))

    lines, period_notes = patch_prj_run_period(lines, run_start_date, run_end_date)
    drainmode_note = [f"Patched [Analysis] DrainMode={DRAINMODE_GUI_CODE[mode_code]} for {mode_code} GUI radio selection."]
    lines, control_notes = patch_common_control_schedule(lines, mode_code)
    return "\r\n".join(lines) + "\r\n", period_notes + drainmode_note + control_notes


# =============================================================================
# DRAINMOD RUNNER
# =============================================================================

def safe_text(ctrl) -> str:
    try:
        return (ctrl.window_text() or "").strip()
    except Exception:
        return ""


def dismiss_unexpected_error_popups(timeout: float = 0.0) -> list[str]:
    msgs = []
    end = time.time() + max(timeout, 0.0)
    for backend in ["uia", "win32"]:
        try:
            desk = Desktop(backend=backend)
            while True:
                found_any = False
                for win in desk.windows():
                    title = safe_text(win)
                    low = title.lower()
                    if "unexpected error occurred" in low or "dispatcher.unhandledexception" in low:
                        found_any = True
                        msgs.append(title)
                        # No keyboard input. Try to click an explicit dialog button instead.
                        for label in ["OK", "Close", "Cancel"]:
                            btn = find_by_exact_text(win, label)
                            if btn is not None:
                                try:
                                    btn.click_input()
                                    break
                                except Exception:
                                    pass
                        time.sleep(0.5)
                if timeout <= 0 or time.time() >= end:
                    break
                if not found_any:
                    time.sleep(0.2)
        except Exception:
            continue
    return msgs


def any_engine_running() -> bool:
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info.get("name") or "").lower() in ENGINE_NAMES:
                return True
        except Exception:
            continue
    return False


def proc_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name()
    except Exception:
        return ""


def proc_exe(pid: int) -> str:
    try:
        return psutil.Process(pid).exe()
    except Exception:
        return ""


def normalize_title(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def connect_main_window(proc, timeout: int = 30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for backend in ["uia", "win32"]:
            try:
                app = Application(backend=backend).connect(process=proc.pid, timeout=3)
                ranked = []
                for w in app.windows():
                    title = safe_text(w)
                    cls = ""
                    try:
                        cls = getattr(w, "class_name", lambda: "")() or ""
                    except Exception:
                        pass
                    norm = normalize_title(title)
                    if not norm:
                        continue
                    if any(b in norm for b in BAD_TITLE_HINTS):
                        continue
                    if any(b.lower() in cls.lower() for b in BAD_CLASS_HINTS):
                        continue
                    pname = proc_name(proc.pid).lower()
                    pexe = proc_exe(proc.pid).lower()
                    if "drainmod7.exe" not in pexe and pname != "drainmod7.exe":
                        continue
                    score = -1
                    if norm.startswith("drainmod 7"):
                        score = 100
                    elif "drainmod 7" in norm:
                        score = 90
                    elif "project:" in norm and r"c:\drainmod7\inputs" in norm:
                        score = 80
                    elif "drainmod" in norm:
                        score = 50
                    if score >= 0:
                        ranked.append((score, len(title), w))
                if ranked:
                    ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
                    win = ranked[0][2]
                    try:
                        win.wait("visible", timeout=3)
                    except Exception:
                        pass
                    return app, win
            except Exception:
                continue
        time.sleep(0.5)
    raise RuntimeError("Could not connect to the DRAINMOD main window for the launched process.")


def descendants(root):
    try:
        return root.descendants()
    except Exception:
        return []


def find_by_exact_text(root, target: str):
    tgt = target.strip().lower()
    for ctrl in descendants(root):
        if safe_text(ctrl).lower() == tgt:
            return ctrl
    return None


def click_if_found(root, target: str) -> bool:
    ctrl = find_by_exact_text(root, target)
    if ctrl is None:
        return False
    for method in ["click_input", "invoke", "click"]:
        try:
            getattr(ctrl, method)()
            return True
        except Exception:
            pass
    return False


def open_run_page_and_click_run_probe_sequence(main_win, wait_after_click_sec: float) -> list[dict]:
    """Trigger the run using only mouse/control clicks. No keyboard typing or hotkeys."""
    actions = []
    try:
        main_win.set_focus()
    except Exception:
        pass
    time.sleep(0.3)

    ok = click_if_found(main_win, "Simulate")
    actions.append({"action": 'click "Simulate"', "success": ok})
    time.sleep(wait_after_click_sec)

    ok = click_if_found(main_win, "Run Simulations")
    actions.append({"action": 'click "Run Simulations"', "success": ok})
    time.sleep(wait_after_click_sec)

    ok = click_if_found(main_win, "Run DRAINMOD")
    actions.append({"action": 'click "Run DRAINMOD"', "success": ok})
    time.sleep(wait_after_click_sec)
    return actions


def close_project_or_app(main_win) -> None:
    if click_if_found(main_win, "Close Project"):
        time.sleep(1.0)
        return
    try:
        main_win.close()
        time.sleep(1.0)
    except Exception:
        pass


def clean_tree_files(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for p in sorted(root.rglob("*"), reverse=True):
        if p.is_file():
            try:
                p.unlink()
            except Exception:
                pass


def clean_runtime_logs(runtime_inputs: Path) -> None:
    for name in ["dmhydro.LOG", "dmhydro.log", "dmnii.LOG", "dmnii.log", "dmdssat.LOG", "dmdssat.log"]:
        p = runtime_inputs / name
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass


def copy_entire_tree(src_root: Path, dest_root: Path) -> int:
    copied = 0
    if not src_root.exists():
        return copied
    dest_root.mkdir(parents=True, exist_ok=True)
    for src in src_root.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(src_root)
        dst = dest_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
            copied += 1
        except Exception:
            pass
    return copied


def expected_output_files_for_scenario(scenario_name: str) -> list[str]:
    """Expected files visible in the DRAINMOD output folder for a completed scenario."""
    return [
        f"{scenario_name}.CRO",
        f"{scenario_name}.DAY",
        f"{scenario_name}.MON",
        f"{scenario_name}.MRK",
        f"{scenario_name}.OST",
        f"{scenario_name}.OUT",
        f"{scenario_name}.PLT",
        f"{scenario_name}.RNK",
        f"{scenario_name}.WTB",
        f"{scenario_name}.YLD",
        f"{scenario_name}.YR",
        f"{scenario_name}_MZOVERVIEW.OUT",
        f"{scenario_name}_MZPlantGro.OUT",
        f"{scenario_name}_MZPlantN.OUT",
        "scenario_metadata.json",
    ]


def check_completed_output_folder(output_dir: Path, scenario_name: str, min_kb: int = MIN_COMPLETED_OUTPUT_KB) -> tuple[bool, list[str], dict[str, float]]:
    """Check whether a scenario output folder is complete enough to skip.

    A scenario is complete only when every expected file exists and each file is
    at least min_kb. This lets the script resume after a shutdown without
    rerunning scenarios that already finished correctly.
    """
    missing_or_small: list[str] = []
    sizes_kb: dict[str, float] = {}
    # For resume checking, accept any non-empty file.
    # A small file such as 0.2 KB is valid for outputs like RNK/YLD/YR,
    # but a true 0-byte file indicates an incomplete or failed copy/run.
    min_bytes = max(1, int(min_kb * 1024))

    for filename in expected_output_files_for_scenario(scenario_name):
        p = output_dir / filename
        if not p.exists() or not p.is_file():
            missing_or_small.append(f"{filename} missing")
            sizes_kb[filename] = 0.0
            continue
        try:
            size = p.stat().st_size
        except Exception:
            size = 0
        sizes_kb[filename] = round(size / 1024, 1)
        if size < min_bytes:
            missing_or_small.append(f"{filename} empty or incomplete ({size / 1024:.1f} KB)")

    return len(missing_or_small) == 0, missing_or_small, sizes_kb


def find_output_file_by_name(root: Path, filename: str) -> Optional[Path]:
    direct = root / filename
    if direct.exists() and direct.is_file():
        return direct
    matches = list(root.rglob(filename)) if root.exists() else []
    return matches[0] if matches else None


def output_file_ready(root: Path, scenario_name: str, min_kb: int) -> tuple[bool, Optional[Path], int]:
    target_name = f"{scenario_name}_MZPlantGro.OUT"
    p = find_output_file_by_name(root, target_name)
    if p is None:
        return False, None, 0
    try:
        size_bytes = p.stat().st_size
    except Exception:
        size_bytes = 0
    return size_bytes >= min_kb * 1024, p, size_bytes


def wait_for_run_to_finish(runtime_outputs: Path, scenario_name: str) -> dict:
    start = time.time()
    engine_started = False
    last_report = 0.0

    while True:
        popup_messages = dismiss_unexpected_error_popups(timeout=0)
        if popup_messages:
            raise RuntimeError("DRAINMOD showed an unexpected error popup: " + " || ".join(popup_messages))

        elapsed = time.time() - start
        running = any_engine_running()
        if running:
            engine_started = True

        ready, ready_file, ready_size = output_file_ready(runtime_outputs, scenario_name, MIN_MZPLANTGRO_KB)
        if ready:
            time.sleep(1.0)
            return {
                "elapsed_sec": round(time.time() - start, 1),
                "engine_started": engine_started,
                "ready_output_file": str(ready_file),
                "ready_output_size_kb": round(ready_size / 1024, 1),
            }

        if elapsed - last_report >= 10.0:
            _, candidate_file, candidate_size = output_file_ready(runtime_outputs, scenario_name, 0)
            candidate_txt = (
                f"out_file={candidate_file.name} | out_kb={candidate_size / 1024:.1f}"
                if candidate_file else "out_file=missing"
            )
            print(f"      Running... elapsed={elapsed / 60:.1f} min | engine_started={engine_started} | {candidate_txt}")
            last_report = elapsed

        if elapsed > MAX_RUN_MINUTES * 60:
            raise TimeoutError(
                f"Timed out after {MAX_RUN_MINUTES} minutes waiting for "
                f"{scenario_name}_MZPlantGro.OUT to exceed {MIN_MZPLANTGRO_KB} KB."
            )
        time.sleep(1.0)


def launch_project(drainmod_exe: Path, runtime_prj: Path, cwd: Path):
    return subprocess.Popen([str(drainmod_exe), str(runtime_prj)], cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def stage_runtime_pair(master_prj: Path, master_gen: Path, runtime_inputs: Path, runtime_outputs: Path) -> tuple[Path, Path]:
    scenario_name = master_gen.stem
    runtime_prj = runtime_inputs / f"{scenario_name}.prj"
    runtime_gen = runtime_inputs / f"{scenario_name}.gen"
    shutil.copy2(master_prj, runtime_prj)
    shutil.copy2(master_gen, runtime_gen)

    # Re-patch runtime-sensitive paths after copying.
    lines = read_clean_lines(runtime_prj)
    lines = set_prj_value(lines, "General", "Hydrology", winpath(runtime_gen))
    lines = set_prj_value(lines, "Path", "Outpath", winpath(runtime_outputs))
    lines = set_prj_value(lines, "FolderSettings", "GeneralFolder", winpath(runtime_inputs))
    lines = set_prj_value(lines, "FolderSettings", "OutputFolder", winpath(runtime_outputs))
    lines = set_prj_value(lines, "FolderSettings", "BackupFolder", winpath(runtime_outputs))
    lines = set_prj_value(lines, "FolderSettings", "DssatFolder", winpath(DRAINMOD_ROOT / "dssat"))
    write_ascii_crlf(runtime_prj, "\r\n".join(lines) + "\r\n")

    # Patch GEN printout output folder.
    gen_lines = read_clean_lines(runtime_gen)
    idx_print = find_section_line(gen_lines, "*** Printout and Input Control")
    if idx_print >= 0 and idx_print + 1 < len(gen_lines):
        parts = gen_lines[idx_print + 1].split(maxsplit=2)
        if len(parts) >= 2:
            gen_lines[idx_print + 1] = preserve_leading_whitespace(
                gen_lines[idx_print + 1], f"{parts[0]} {parts[1]} {winpath(runtime_outputs)}"
            )
            write_ascii_crlf(runtime_gen, "\r\n".join(gen_lines) + "\r\n")

    return runtime_prj, runtime_gen



# =============================================================================
# =============================================================================
# V10: NO PRE-START CLEANUP, NO KEYBOARD TYPING
# =============================================================================

def _terminate_process_tree(pid: int, timeout: float = 4.0) -> None:
    """Terminate only the process tree started by this script."""
    try:
        parent = psutil.Process(pid)
    except Exception:
        return
    procs = []
    try:
        procs.extend(parent.children(recursive=True))
    except Exception:
        pass
    procs.append(parent)
    for pproc in procs:
        try:
            pproc.terminate()
        except Exception:
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for pproc in alive:
        try:
            pproc.kill()
        except Exception:
            pass


def close_launched_drainmod_process(proc, main_win=None) -> None:
    """Close only the DRAINMOD process launched for the current scenario.

    This does not scan the desktop, does not close pre-existing windows, and
    does not send keyboard input. It first tries a normal close on the launched
    DRAINMOD window, then terminates only the launched process tree if needed.
    """
    if main_win is not None:
        try:
            main_win.close()
            time.sleep(1.0)
        except Exception:
            pass
    try:
        if proc is not None and proc.poll() is None:
            _terminate_process_tree(proc.pid, timeout=3.0)
    except Exception:
        pass


def close_project_or_app(main_win) -> None:
    """Backward-compatible no-keyboard close. Prefer close_launched_drainmod_process()."""
    try:
        main_win.close()
        time.sleep(1.0)
    except Exception:
        pass


def _tune_process_for_speed(proc) -> None:
    """Give the launched DRAINMOD process all logical cores and high priority."""
    try:
        pproc = psutil.Process(proc.pid)
        if SET_PROCESS_AFFINITY_TO_ALL_CORES and hasattr(pproc, "cpu_affinity"):
            pproc.cpu_affinity(list(range(psutil.cpu_count(logical=True) or 1)))
        if SET_PROCESS_PRIORITY_HIGH and os.name == "nt":
            pproc.nice(psutil.HIGH_PRIORITY_CLASS)
    except Exception:
        pass


def launch_project(drainmod_exe: Path, runtime_prj: Path, cwd: Path):
    proc = subprocess.Popen(
        [str(drainmod_exe), str(runtime_prj)],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _tune_process_for_speed(proc)
    return proc

# =============================================================================
# MAIN WORKFLOW
# =============================================================================

def build_county_dataframe(county: str) -> pd.DataFrame:
    stations_csv = auto_find_stations_csv(GRID_ROOT, county)
    soil_map_csv = auto_find_soil_map_csv(GRID_ROOT, county)
    stations = load_stations_csv(stations_csv)
    soil_map = load_soil_map_csv(soil_map_csv)
    df = stations.merge(soil_map, on="pixel_id", how="inner")
    if PIXELS_TO_RUN:
        selected = {normalize_pixel_id(x) for x in PIXELS_TO_RUN}
        df = df[df["pixel_id"].isin(selected)].copy()
    df.attrs["stations_csv"] = str(stations_csv)
    df.attrs["soil_map_csv"] = str(soil_map_csv)
    return df.reset_index(drop=True)


def write_metadata(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> None:
    ensure_admin_or_relaunch()

    run_log_path = make_run_log_path()
    log_message(run_log_path, "Starting DRAINMOD automatic 9-scenario runner.")

    # This version resumes by default: completed scenario folders are skipped.
    skip_completed_scenarios = DEFAULT_SKIP_COMPLETED_SCENARIOS

    drainmod_exe = DRAINMOD_ROOT / "Drainmod7.exe"
    if not drainmod_exe.exists():
        raise FileNotFoundError(f"DRAINMOD executable not found: {drainmod_exe}")
    if not PRJ_TEMPLATE.exists():
        raise FileNotFoundError(f"PRJ template not found: {PRJ_TEMPLATE}")
    if not GEN_TEMPLATE.exists():
        raise FileNotFoundError(f"GEN template not found: {GEN_TEMPLATE}")

    counties_to_run = prompt_counties_to_run()
    run_start_date, run_end_date = prompt_run_period()
    print(f"Simulation period: {run_start_date} to {run_end_date}")
    print(f"Run log: {run_log_path}")

    runtime_inputs = DRAINMOD_ROOT / "inputs"
    runtime_outputs = DRAINMOD_ROOT / "outputs"
    runtime_inputs.mkdir(parents=True, exist_ok=True)
    runtime_outputs.mkdir(parents=True, exist_ok=True)

    # Do not close anything before starting. This avoids touching PyCharm or other open windows.

    master_root = GRID_ROOT / "INPUTS"
    output_root = GRID_ROOT / "OUTPUTS"

    summary_rows = []
    issue_rows = []

    total_counties = len(counties_to_run)
    print(f"Counties: {counties_to_run}")
    print(f"Drainage modes: {list(DRAINAGE_MODES.keys())}")
    print(f"Spacing classes: {SCENARIO_SPACINGS_FT}")
    print(f"Run period: {run_start_date} to {run_end_date}")
    print(f"Skip completed scenarios: {skip_completed_scenarios}")

    for county_idx, county in enumerate(counties_to_run, start=1):
        print(f"\nCounty {county_idx}/{total_counties}: {county}")
        try:
            df = build_county_dataframe(county)
        except Exception as e:
            issue_rows.append({"county": county, "pixel_id": "", "scenario": "", "issue": str(e)})
            print(f"  Failed to load county inputs: {e}")
            if not CONTINUE_ON_ERROR:
                break
            continue

        if df.empty:
            issue_rows.append({"county": county, "pixel_id": "", "scenario": "", "issue": "No matching pixels after joining weather and soil."})
            print("  No matching pixels after joining weather and soil.")
            continue

        print(f"  Pixels to run: {len(df)}")
        weather_root = GRID_ROOT / "WEATHER" / county
        prj_dir = master_root / county / "PRJ"
        gen_dir = master_root / county / "GEN"
        prj_dir.mkdir(parents=True, exist_ok=True)
        gen_dir.mkdir(parents=True, exist_ok=True)

        for pixel_idx, row in df.iterrows():
            pixel_id = str(row["pixel_id"])
            weather_id = str(row["weather_id"]) if "weather_id" in row and pd.notna(row["weather_id"]) else str(row.get("station6", pixel_id))
            print(f"\n  Pixel {pixel_idx + 1}/{len(df)}: {pixel_id}")

            dmn_path = Path(str(row["dmn_file"])).expanduser()
            sin_path = Path(str(row["sin_file"])).expanduser()
            mis_path = Path(str(row["mis_file"])).expanduser()
            wdv_path = Path(str(row["wdv_file"])).expanduser()

            rain_path = find_weather_file(weather_root, pixel_id, "Rain", "RAI")
            tem_path = find_weather_file(weather_root, pixel_id, "Temp", "TEM")
            pet_path = find_weather_file(weather_root, pixel_id, "PET", "PET")
            rad_path = find_weather_file(weather_root, pixel_id, "Rad", "RAD")

            missing = []
            for name, p in [("DMN", dmn_path), ("SIN", sin_path), ("MIS", mis_path), ("WDV", wdv_path), ("RAI", rain_path), ("TEM", tem_path), ("PET", pet_path), ("RAD", rad_path)]:
                if p is None or not Path(p).exists():
                    missing.append(name)
            if missing:
                issue = f"Missing required file(s): {', '.join(missing)}"
                issue_rows.append({"county": county, "pixel_id": pixel_id, "scenario": "", "issue": issue})
                print(f"    Skipping pixel: {issue}")
                if not CONTINUE_ON_ERROR:
                    break
                continue

            for mode_code, mode_name in DRAINAGE_MODES.items():
                for spacing_name, spacing_ft in SCENARIO_SPACINGS_FT.items():
                    scenario_name = f"{pixel_id}_{mode_code}_{spacing_name}"
                    spacing_cm = round(spacing_ft * FT_TO_CM, 2)
                    scenario_output_dir = output_root / county / pixel_id / f"{mode_code}_{spacing_name}"
                    master_prj = prj_dir / f"{scenario_name}.prj"
                    master_gen = gen_dir / f"{scenario_name}.gen"

                    print(f"    Scenario: {scenario_name} | spacing={spacing_ft:.1f} ft ({spacing_cm:.2f} cm) | mode={mode_name}")

                    scenario_output_dir.mkdir(parents=True, exist_ok=True)
                    if skip_completed_scenarios:
                        is_complete, problems, sizes_kb = check_completed_output_folder(
                            scenario_output_dir,
                            scenario_name,
                            MIN_COMPLETED_OUTPUT_KB,
                        )
                        if is_complete:
                            summary_rows.append({
                                "county": county,
                                "pixel_id": pixel_id,
                                "scenario": scenario_name,
                                "mode_code": mode_code,
                                "mode_name": mode_name,
                                "spacing_name": spacing_name,
                                "spacing_ft": spacing_ft,
                                "spacing_cm": spacing_cm,
                                "drain_depth_cm": DRAIN_DEPTH_CM,
                                "run_start_date": run_start_date,
                                "run_end_date": run_end_date,
                                "master_prj": str(master_prj),
                                "master_gen": str(master_gen),
                                "output_dir": str(scenario_output_dir),
                                "copied_files": 0,
                                "trigger_actions": "[]",
                                "skipped_existing_complete": True,
                                "completed_file_count": len(expected_output_files_for_scenario(scenario_name)),
                                "completion_rule": "all expected files present and > 0 bytes",
                            })
                            print(f"      Skipped: existing output folder is complete ({len(expected_output_files_for_scenario(scenario_name))} files > 0 bytes).")
                            continue
                        else:
                            preview = "; ".join(problems[:4])
                            extra = "" if len(problems) <= 4 else f"; plus {len(problems) - 4} more"
                            print(f"      Will run: output folder incomplete ({preview}{extra}).")

                    try:

                        gen_txt, gen_notes = build_gen_from_template(
                            GEN_TEMPLATE,
                            weather_id,
                            rain_path,
                            tem_path,
                            DRAIN_DEPTH_CM,
                            spacing_cm,
                            scenario_output_dir,
                            mode_code,
                            run_start_date,
                            run_end_date,
                        )
                        write_ascii_crlf(master_gen, gen_txt)

                        prj_txt, prj_notes = build_prj_from_template(
                            PRJ_TEMPLATE,
                            county,
                            weather_root,
                            scenario_name,
                            dmn_path,
                            sin_path,
                            mis_path,
                            wdv_path,
                            rain_path,
                            tem_path,
                            pet_path,
                            rad_path,
                            spacing_cm,
                            DRAIN_DEPTH_CM,
                            mode_code,
                            run_start_date,
                            run_end_date,
                        )
                        write_ascii_crlf(master_prj, prj_txt)

                        write_metadata(
                            scenario_output_dir / "scenario_metadata.json",
                            {
                                "county": county,
                                "pixel_id": pixel_id,
                                "scenario_name": scenario_name,
                                "mode_code": mode_code,
                                "mode_name": mode_name,
                                "spacing_name": spacing_name,
                                "spacing_ft": spacing_ft,
                                "spacing_cm": spacing_cm,
                                "drain_depth_cm": DRAIN_DEPTH_CM,
                                "run_start_date": run_start_date,
                                "run_end_date": run_end_date,
                                "board_schedule": BOARD_SCHEDULE if mode_code in {"CD", "SI"} else [],
                                "stations_csv": df.attrs.get("stations_csv", ""),
                                "soil_map_csv": df.attrs.get("soil_map_csv", ""),
                                "master_prj": str(master_prj),
                                "master_gen": str(master_gen),
                                "notes": prj_notes + gen_notes,
                            },
                        )

                        clean_runtime_logs(runtime_inputs)
                        if CLEAN_ROOT_OUTPUTS_BEFORE_EACH_RUN:
                            clean_tree_files(runtime_outputs)

                        runtime_prj, runtime_gen = stage_runtime_pair(master_prj, master_gen, runtime_inputs, runtime_outputs)
                        proc = launch_project(drainmod_exe, runtime_prj, DRAINMOD_ROOT)
                        time.sleep(WAIT_AFTER_LAUNCH_SEC)
                        dismiss_unexpected_error_popups(timeout=0.5)

                        app, main_win = connect_main_window(proc, timeout=30)
                        actions = open_run_page_and_click_run_probe_sequence(main_win, WAIT_AFTER_CLICK_SEC)
                        runtime_status = wait_for_run_to_finish(runtime_outputs, scenario_name)
                        copied_files = copy_entire_tree(runtime_outputs, scenario_output_dir)
                        close_launched_drainmod_process(proc, main_win)

                        if not KEEP_RUNTIME_FILES:
                            for p in [runtime_prj, runtime_gen]:
                                try:
                                    p.unlink(missing_ok=True)
                                except Exception:
                                    pass

                        summary_rows.append({
                            "county": county,
                            "pixel_id": pixel_id,
                            "scenario": scenario_name,
                            "mode_code": mode_code,
                            "mode_name": mode_name,
                            "spacing_name": spacing_name,
                            "spacing_ft": spacing_ft,
                            "spacing_cm": spacing_cm,
                            "drain_depth_cm": DRAIN_DEPTH_CM,
                            "run_start_date": run_start_date,
                            "run_end_date": run_end_date,
                            "master_prj": str(master_prj),
                            "master_gen": str(master_gen),
                            "output_dir": str(scenario_output_dir),
                            "copied_files": copied_files,
                            "trigger_actions": json.dumps(actions),
                            "skipped_existing_complete": False,
                            **runtime_status,
                        })
                        log_message(run_log_path, f"Finished {county} | {pixel_id} | {scenario_name}. Copied files: {copied_files}")

                    except Exception as e:
                        issue_rows.append({"county": county, "pixel_id": pixel_id, "scenario": scenario_name, "issue": str(e)})
                        log_exception(run_log_path, f"{county} | {pixel_id} | {scenario_name}", e)
                        try:
                            dismiss_unexpected_error_popups(timeout=0.5)
                        except Exception:
                            pass
                        try:
                            close_launched_drainmod_process(locals().get("proc"), locals().get("main_win"))
                        except Exception:
                            pass
                        if not CONTINUE_ON_ERROR:
                            raise

    summary_path = GRID_ROOT / "INPUTS" / "_auto_9scenario_run_summary.csv"
    if summary_rows:
        fieldnames = []
        seen_fields = set()
        for row in summary_rows:
            for key in row.keys():
                if key not in seen_fields:
                    fieldnames.append(key)
                    seen_fields.add(key)
        with open(summary_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(summary_rows)
        print(f"\nSummary written to: {summary_path}")

    issues_path = GRID_ROOT / "INPUTS" / "_auto_9scenario_run_issues.csv"
    if issue_rows:
        with open(issues_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(issue_rows[0].keys()))
            writer.writeheader()
            writer.writerows(issue_rows)
        print(f"Issues written to: {issues_path}")

    log_message(run_log_path, "Done.")
    log_message(run_log_path, f"Final log file: {run_log_path}")


def run_with_visible_crash_report():
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        try:
            run_log_path = make_run_log_path()
            log_exception(run_log_path, "fatal startup or main-loop crash", e)
            print(f"\nFatal crash. Full details were written to: {run_log_path}", flush=True)
        except Exception:
            print("\nFatal crash:", flush=True)
            traceback.print_exc()
        raise
    finally:
        if PAUSE_ON_EXIT:
            try:
                input("\nPress Enter to close this runner window...")
            except Exception:
                pass


if __name__ == "__main__":
    run_with_visible_crash_report()
