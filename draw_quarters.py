#!/usr/bin/env python3
"""
Quarterly Load Profile Visualizer — SVG edition

Reads CSV filenames from stdin (as produced by quarters_to_csv.py) and generates
monthly SVG files showing energy consumption patterns.  Each day appears as a
horizontal strip; bars are grouped by 15-minute intervals with withdrawal and
injection side-by-side.

The SVG is built bottom-up using xml.etree.ElementTree (stdlib).  All x-coordinates
are integers computed from a fixed per-bar pixel width; y-coordinates are floats
rounded to two decimal places.  The axis group (grid lines + value labels) is defined
once in <defs> and <use>-cloned into every day strip, keeping the file compact.

Rendering layers (bottom → top):
  1. Weather shading  — cloud-cover staircase fill + rain bars
  2. PV production    — one polyline per plant
  3. Sun arc          — gold fill polygon + arc polyline + sunrise/sunset vlines
  4. Energy bars      — 96 × N_pairs × 2 <rect> elements
  5. Axis & labels    — <use href="#day-axis"> + day-number <text>

Usage:
    python quarters_to_csv.py *.json | python draw_quarters.py
    python draw_quarters.py --energy-types attiva --output-dir plots/ *.csv
    python draw_quarters.py --bar-width 16 --location "45.5N 12.7E" --pv "180;30;6" *.csv
"""

import sys
import re
import csv
import json
import calendar
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date as Date
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Set, Optional, Any
from collections import defaultdict
import argparse

import numpy as np
import pandas as pd
import pvlib
from timezonefinder import TimezoneFinder


# ---------------------------------------------------------------------------
# SVG namespace helpers
# ---------------------------------------------------------------------------
_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"

ET.register_namespace("", _SVG_NS)
ET.register_namespace("xlink", _XLINK_NS)


def _el(tag: str, parent: Optional[ET.Element] = None, **attrs: Any) -> ET.Element:
    """Create an SVG element (possibly appended to *parent*) with *attrs*."""
    e = ET.SubElement(parent, f"{{{_SVG_NS}}}{tag}") if parent is not None else ET.Element(f"{{{_SVG_NS}}}{tag}")
    for k, v in attrs.items():
        # Python kwarg underscore → hyphen (e.g. font_size → font-size)
        e.set(k.replace("_", "-"), str(v))
    return e


def _fmt(v: float, decimals: int = 2) -> str:
    """Format *v* to *decimals* decimal places, stripping trailing zeros."""
    s = f"{v:.{decimals}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _pts(coords: List[Tuple[float, float]]) -> str:
    """Produce a 'points' attribute string from (x, y) pairs."""
    return " ".join(f"{_fmt(x)},{_fmt(y)}" for x, y in coords)


# ---------------------------------------------------------------------------
# Layout constants  (all in pixels; override bar width via --bar-width)
# ---------------------------------------------------------------------------
_BAR_W_DEFAULT = 20     # px per individual bar (prel or imm)
_BAR_INNER_GAP = 2      # px between the prel and imm bar of the same pair
_PAIR_GAP = 8           # px between different energy pairs within a slot
_SLOT_MARGIN = 4        # px of breathing room after the last bar of a slot
_DAY_H = 120            # px height of one day's plot area
_DAY_GAP = 8            # px vertical gap between day strips
_MARGIN_LEFT = 64       # px left of the plot area (for y-axis labels)
_MARGIN_RIGHT = 24
_HEADER_H = 54          # px for title row
_XAXIS_H = 44           # px below last day for hour labels
_LEGEND_H = 72          # px at the bottom for the legend


def _slot_w(n_pairs: int, bar_w: int) -> int:
    """Total pixel width of one 15-minute slot column."""
    return n_pairs * (2 * bar_w + _BAR_INNER_GAP) + max(0, n_pairs - 1) * _PAIR_GAP + _SLOT_MARGIN


def _bar_x(slot: int, pair_idx: int, is_imm: bool, bar_w: int, slot_w: int) -> int:
    """Left-edge x-pixel of one bar, relative to the left edge of the plot area."""
    pair_offset = pair_idx * (2 * bar_w + _BAR_INNER_GAP + _PAIR_GAP)
    return slot * slot_w + pair_offset + (bar_w if is_imm else 0)


def _slot_cx(slot: int, slot_w: int) -> int:
    """Centre x-pixel of a slot column."""
    return slot * slot_w + slot_w // 2


def _val_y(v: float, overall_max: float, day_h: int = _DAY_H) -> float:
    """Map energy value *v* to y-pixel (0 = top = max value, day_h = bottom = 0)."""
    if overall_max <= 0:
        return float(day_h)
    return day_h - v / overall_max * day_h


# ---------------------------------------------------------------------------
# Shared data structures and utilities from graph_quarters.py
# ---------------------------------------------------------------------------

def print_stderr(*args: object, **kwargs: Any) -> None:
    print(*args, **kwargs, file=sys.stderr)


TOKEN_TO_SUFFIX = {
    "attiva-prelevata": "attiva_prel",
    "attiva-immessa": "attiva_imm",
    "reattiva-induttiva-prelevata": "rind_prel",
    "reattiva-induttiva-immessa": "rind_imm",
    "reattiva-capacitiva-prelevata": "rcap_prel",
    "reattiva-capacitiva-immessa": "rcap_imm",
}

SUFFIX_TO_TOKEN = {v: k for k, v in TOKEN_TO_SUFFIX.items()}

ENERGY_PAIRS = {
    "attiva": ("attiva-prelevata", "attiva-immessa"),
    "rind": ("reattiva-induttiva-prelevata", "reattiva-induttiva-immessa"),
    "rcap": ("reattiva-capacitiva-prelevata", "reattiva-capacitiva-immessa"),
}

COLORS = {
    "attiva-prelevata": "#1f77b4",
    "attiva-immessa": "#ff7f0e",
    "reattiva-induttiva-prelevata": "#2ca02c",
    "reattiva-induttiva-immessa": "#d62728",
    "reattiva-capacitiva-prelevata": "#9467bd",
    "reattiva-capacitiva-immessa": "#8c564b",
}

PV_COLORS = ["#63f5ff", "#009dff", "#557fff", "#0004ff", "#5a2dff"]


def parse_location_arg(s: str) -> Tuple[float, float]:
    s = s.strip()
    letter_pattern = r'([\d.]+)\s*([NSns])\s*[,\s]\s*([\d.]+)\s*([EWew])'
    m = re.fullmatch(letter_pattern, s.upper())
    if m:
        lat = float(m.group(1)) * (-1 if m.group(2) == 'S' else 1)
        lon = float(m.group(3)) * (-1 if m.group(4) == 'W' else 1)
        return lat, lon
    dd_pattern = r'(-?[\d.]+)\s*[,\s]\s*(-?[\d.]+)'
    m = re.fullmatch(dd_pattern, s)
    if m:
        lat = float(m.group(1))
        lon = float(m.group(2))
        if not (-90 <= lat <= 90):
            raise ValueError(f"Latitude {lat} is out of range [-90, 90]")
        if not (-180 <= lon <= 180):
            raise ValueError(f"Longitude {lon} is out of range [-180, 180]")
        return lat, lon
    raise ValueError(
        f"Unrecognised location format: '{s}'.\n"
        "Accepted formats:\n"
        "  Google Maps style : \"45.997, 12.700\"\n"
        "  Hemisphere letters: \"45.5N 9.2E\""
    )


def get_solar_elevation(date_str: str, lat: float, lon: float, tz_str: str) -> np.ndarray:
    times = pd.date_range(
        start=pd.Timestamp(date_str, tz=tz_str),
        periods=96,
        freq="15min",
    )
    loc = pvlib.location.Location(lat, lon, tz=tz_str)
    solpos = loc.get_solarposition(times)
    return solpos["apparent_elevation"].to_numpy()


def fetch_weather_for_month(
    month_str: str,
    lat: float,
    lon: float,
    tz_str: str,
) -> Dict[str, Tuple[np.ndarray, np.ndarray, np.ndarray]]:
    year, month = month_str.split("-")
    last_day = calendar.monthrange(int(year), int(month))[1]
    start_date = f"{month_str}-01"
    end_date = f"{month_str}-{last_day:02d}"
    tz_encoded = tz_str.replace("/", "%2F")
    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={start_date}&end_date={end_date}"
        f"&hourly=cloud_cover,rain,temperature_2m"
        f"&timezone={tz_encoded}"
    )
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())
    hourly = data["hourly"]
    times: List[str] = hourly["time"]
    cloud_all = np.array(hourly["cloud_cover"], dtype=float)
    rain_all = np.array(hourly["rain"], dtype=float)
    temp_all = np.array(hourly["temperature_2m"], dtype=float)
    day_cloud: Dict[str, List[float]] = defaultdict(list)
    day_rain: Dict[str, List[float]] = defaultdict(list)
    day_temp: Dict[str, List[float]] = defaultdict(list)
    for i, t in enumerate(times):
        date_str = t[:10]
        day_cloud[date_str].append(float(cloud_all[i]))
        day_rain[date_str].append(float(rain_all[i]))
        day_temp[date_str].append(float(temp_all[i]))
    return {
        date: (
            np.array(day_cloud[date]),
            np.array(day_rain[date]),
            np.array(day_temp[date]),
        )
        for date in day_cloud
    }


def parse_energy_types_arg(energy_types_str: str) -> List[Tuple[str, str]]:
    if not energy_types_str or not energy_types_str.strip():
        return [ENERGY_PAIRS["attiva"]]
    pairs: List[Tuple[str, str]] = []
    tokens = [t.strip() for t in energy_types_str.split(",")]
    for token in tokens:
        if token in ENERGY_PAIRS:
            pairs.append(ENERGY_PAIRS[token])
        elif token in TOKEN_TO_SUFFIX:
            for pair_name, (prel, imm) in ENERGY_PAIRS.items():
                if token == prel or token == imm:
                    if (prel, imm) not in pairs:
                        pairs.append((prel, imm))
                    break
            else:
                raise ValueError(f"Cannot determine pair for token '{token}'")
        else:
            raise ValueError(
                f"Unknown energy type: '{token}'. Valid shortcuts: {', '.join(ENERGY_PAIRS.keys())}"
            )
    seen: Set[Tuple[str, str]] = set()
    unique_pairs = []
    for pair in pairs:
        if pair not in seen:
            unique_pairs.append(pair)
            seen.add(pair)
    return unique_pairs


def parse_csv_filename(filename: str) -> Optional[Tuple[str, str]]:
    basename = Path(filename).stem
    match = re.search(r'(\d{4})-(\d{2})', basename)
    if not match:
        return None
    year_month = f"{match.group(1)}-{match.group(2)}"
    for suffix in SUFFIX_TO_TOKEN.keys():
        if basename.endswith(suffix):
            return (year_month, suffix)
    return None


@dataclass
class DailyProfile:
    date: str
    intervals: List[float] = field(default_factory=list)


@dataclass
class MonthlyDataset:
    month: str
    data: Dict[str, List[(str, DailyProfile)]] = field(default_factory=dict)
    days_in_month: int = 0

    def get_all_dates(self) -> List[str]:
        # It is assumed we get a coherent amount of days
        # Maybe worth checking at load, or anyway before assembly.
        dates = next(iter(self.data.values()))
        return [date for date, _ in dates]

    def get_max_value(self) -> float:
        max_val = 0.0
        for signal_data in self.data.values():
            for _, profile in signal_data:
                if profile.intervals:
                    max_val = max(max_val, max(profile.intervals))
        return max_val if max_val > 0 else 1.0


@dataclass
class PVPlant:
    azimuth: float
    tilt: float
    kwp: float
    label: str
    construction_date: Optional[Date] = None
    degradation_rate: float = 0.005


_PV_DATE_RE = re.compile(r'^\d{4}-\d{2}(-\d{2})?$')


def _parse_construction_date(s: str) -> Date:
    if re.fullmatch(r'\d{4}-\d{2}', s):
        return Date.fromisoformat(f"{s}-01")
    return Date.fromisoformat(s)


def pv_age_derating(construction_date: Date, month_str: str, annual_rate: float = 0.005) -> float:
    year, month = month_str.split("-")
    sim_mid = Date(int(year), int(month), 15)
    age_years = max((sim_mid - construction_date).days / 365.25, 0.0)
    return min((1.0 - annual_rate) ** age_years, 1.0)


def parse_pv_arg(s: str) -> PVPlant:
    parts = [p.strip() for p in s.split(";")]
    if len(parts) not in (3, 4, 5):
        raise ValueError(
            f"--pv: expected 3-5 semicolon-separated fields "
            f"(azimuth;tilt;kWp[;YYYY-MM[-DD][;label]]), got: '{s}'"
        )
    try:
        azimuth = float(parts[0])
        tilt = float(parts[1])
        kwp = float(parts[2])
    except ValueError:
        raise ValueError(f"--pv: azimuth, tilt and kWp must be numeric, got: '{s}'")
    if not (0 <= azimuth <= 360):
        raise ValueError(f"--pv: azimuth must be 0-360 degrees, got {azimuth}")
    if not (0 <= tilt <= 90):
        raise ValueError(f"--pv: tilt must be 0-90 degrees, got {tilt}")
    if kwp <= 0:
        raise ValueError(f"--pv: kWp must be positive, got {kwp}")
    construction_date: Optional[Date] = None
    label: Optional[str] = None
    if len(parts) == 3:
        pass
    elif len(parts) == 4:
        if _PV_DATE_RE.match(parts[3]):
            try:
                construction_date = _parse_construction_date(parts[3])
            except ValueError:
                raise ValueError(f"--pv: invalid construction date '{parts[3]}'")
        else:
            label = parts[3]
    else:
        if not _PV_DATE_RE.match(parts[3]):
            raise ValueError(
                f"--pv: expected a date (YYYY-MM or YYYY-MM-DD) in field 4, got '{parts[3]}'"
            )
        try:
            construction_date = _parse_construction_date(parts[3])
        except ValueError:
            raise ValueError(f"--pv: invalid construction date '{parts[3]}'")
        label = parts[4]
    if label is None:
        label = f"{kwp:.1f} kWp az={azimuth:.0f}\u00b0 tilt={tilt:.0f}\u00b0"
    return PVPlant(azimuth=azimuth, tilt=tilt, kwp=kwp, label=label,
                   construction_date=construction_date)


def load_csv_file(csv_path: Path, energy_type: str) -> List[(str, DailyProfile)]:
    profiles: List[(str, DailyProfile)] = []
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f, delimiter=';')
            header = next(reader, None)
            if not header:
                raise ValueError(f"Empty CSV file: {csv_path}")
            for row in reader:
                if not row or not row[0]:
                    continue
                date = row[0]
                intervals: List[float] = []
                for value_str in row[1:]:
                    try:
                        intervals.append(float(value_str.strip().replace(',', '.')))
                    except ValueError:
                        intervals.append(0.0)
                if len(intervals) < 96:
                    intervals.extend([0.0] * (96 - len(intervals)))
                elif len(intervals) > 96:
                    intervals = intervals[:96]
                profiles.append((date, DailyProfile(date=date, intervals=intervals)))
    except Exception as e:
        raise Exception(f"Failed to load CSV {csv_path}: {e}")
    return profiles


def load_and_group_files(
    csv_files: List[Path],
    requested_pairs: List[Tuple[str, str]],
) -> Dict[str, MonthlyDataset]:
    file_map: Dict[str, Dict[str, Path]] = defaultdict(dict)
    unmatched: List[Path] = []
    for csv_file in csv_files:
        parsed = parse_csv_filename(csv_file.name)
        if parsed:
            month, suffix = parsed
            file_map[month][suffix] = csv_file
        else:
            unmatched.append(csv_file)
    if unmatched:
        print_stderr(f"WARNING: {len(unmatched)} file(s) could not be parsed:")
        for f in unmatched[:5]:
            print_stderr(f"  {f.name}")
        if len(unmatched) > 5:
            print_stderr(f"  ... and {len(unmatched) - 5} more")
    datasets: Dict[str, MonthlyDataset] = {}
    for month in sorted(file_map.keys()):
        month_files = file_map[month]
        dataset = MonthlyDataset(month=month)
        for prel_type, imm_type in requested_pairs:
            prel_suffix = TOKEN_TO_SUFFIX[prel_type]
            imm_suffix = TOKEN_TO_SUFFIX[imm_type]
            has_prel = prel_suffix in month_files
            has_imm = imm_suffix in month_files
            if has_prel:
                print_stderr(f"Loading {month} {prel_suffix} ...", end=" ", flush=True)
                try:
                    dataset.data[prel_type] = load_csv_file(month_files[prel_suffix], prel_type)
                    print_stderr(f"OK ({len(dataset.data[prel_type])} days)")
                except Exception as e:
                    print_stderr(f"FAILED: {e}")
            if has_imm:
                print_stderr(f"Loading {month} {imm_suffix} ...", end=" ", flush=True)
                try:
                    dataset.data[imm_type] = load_csv_file(month_files[imm_suffix], imm_type)
                    print_stderr(f"OK ({len(dataset.data[imm_type])} days)")
                except Exception as e:
                    print_stderr(f"FAILED: {e}")
            if has_prel != has_imm:
                type_name = prel_type.split('-')[0].capitalize()
                missing = imm_suffix if has_prel else prel_suffix
                print_stderr(f"  WARNING: {month} {type_name} pair incomplete - missing {missing}")
        if dataset.data:
            dataset.days_in_month = len(dataset.get_all_dates())
            datasets[month] = dataset
    return datasets


def simulate_pv_month(
    month_str: str,
    lat: float,
    lon: float,
    tz_str: str,
    plants: List[PVPlant],
    weather_data: Optional[Dict[str, Tuple[np.ndarray, np.ndarray, np.ndarray]]] = None,
) -> Dict[str, List[np.ndarray]]:
    if not plants:
        return {}
    year, month = month_str.split("-")
    last_day = calendar.monthrange(int(year), int(month))[1]
    times = pd.date_range(
        start=f"{month_str}-01",
        end=f"{month_str}-{last_day:02d} 23:45",
        freq="15min",
        tz=tz_str,
    )
    n = len(times)
    loc = pvlib.location.Location(lat, lon, tz=tz_str)
    cs = loc.get_clearsky(times)
    solpos = loc.get_solarposition(times)
    ambient_15min: Optional[np.ndarray] = None
    if weather_data is not None:
        temps_hourly: List[float] = []
        date_index = pd.date_range(start=f"{month_str}-01", periods=last_day, freq="D")
        for d in date_index:
            date_str = d.strftime("%Y-%m-%d")
            if date_str in weather_data:
                _, _, temp_h = weather_data[date_str]
                temps_hourly.extend(temp_h.tolist())
            else:
                temps_hourly.extend([15.0] * 24)
        expanded = np.repeat(np.array(temps_hourly, dtype=float), 4)
        if len(expanded) < n:
            expanded = np.concatenate([expanded, np.full(n - len(expanded), expanded[-1])])
        ambient_15min = expanded[:n]
    plant_month_series: List[pd.Series] = []
    for plant in plants:
        if plant.construction_date is not None:
            derating = pv_age_derating(plant.construction_date, month_str, plant.degradation_rate)
            print_stderr(
                f"  {plant.label}: age derating {derating * 100:.2f}%"
            )
        else:
            derating = 1.0
        poa = pvlib.irradiance.get_total_irradiance(
            surface_tilt=plant.tilt,
            surface_azimuth=plant.azimuth,
            solar_zenith=solpos["apparent_zenith"],
            solar_azimuth=solpos["azimuth"],
            dni=cs["dni"],
            ghi=cs["ghi"],
            dhi=cs["dhi"],
            model="isotropic",
        )
        poa_global = poa["poa_global"].fillna(0.0)
        if ambient_15min is not None:
            temp_cell = pvlib.temperature.faiman(
                poa_global=poa_global,
                temp_air=pd.Series(ambient_15min, index=times),
                wind_speed=1.0,
            )
        else:
            temp_cell = 25.0
        pdc = pvlib.pvsystem.pvwatts_dc(
            effective_irradiance=poa_global,
            temp_cell=temp_cell,
            pdc0=plant.kwp * 1000.0 * derating,
            gamma_pdc=-0.004,
        )
        wh_raw = pdc * 0.25 / 1000.0
        plant_month_series.append(
            pd.Series(np.maximum(np.asarray(wh_raw, dtype=float), 0.0), index=times)
        )
    result: Dict[str, List[np.ndarray]] = {}
    for ts in pd.date_range(start=f"{month_str}-01", periods=last_day, freq="D"):
        date_str = ts.strftime("%Y-%m-%d")
        day_start = pd.Timestamp(date_str, tz=tz_str)
        day_end = day_start + pd.Timedelta(hours=23, minutes=45)
        mask = (times >= day_start) & (times <= day_end)
        day_arrays: List[np.ndarray] = []
        for series in plant_month_series:
            arr = series[mask].to_numpy()
            if len(arr) < 96:
                arr = np.pad(arr, (0, 96 - len(arr)))
            elif len(arr) > 96:
                arr = arr[:96]
            day_arrays.append(arr)
        result[date_str] = day_arrays
    return result


def get_input_files() -> List[Path]:
    files: List[Path] = []
    if sys.stdin.isatty():
        print_stderr("No input piped. Scanning current directory for *.csv files...")
        cwd = Path('.')
        csv_files = sorted(cwd.glob('*.csv'))
        if csv_files:
            print_stderr(f"Found {len(csv_files)} CSV file(s)")
        return csv_files
    else:
        try:
            for line in sys.stdin:
                line = line.strip()
                if line:
                    files.append(Path(line))
        except KeyboardInterrupt:
            pass
        return files


# ---------------------------------------------------------------------------
# SVG construction helpers
# ---------------------------------------------------------------------------

def _idx_to_hhmm(i: int) -> str:
    return f"{i // 4:02d}:{(i % 4) * 15:02d}"


def _build_defs(
    defs: ET.Element,
    plot_w: int,
    day_h: int,
    overall_max: float,
) -> None:
    """
    Populate <defs> with:
      - #day-clip   — clip rect covering the plot area
      - #day-axis   — shared axis group (baseline, top gridline, value labels, left spine)
    """
    # Clip path
    cp = _el("clipPath", defs, id="day-clip")
    _el("rect", cp, x="0", y="0", width=str(plot_w), height=str(day_h))

    # Axis group (coordinates relative to top-left of a day's plot rectangle)
    g = _el("g", defs, id="day-axis")

    # Horizontal grid lines
    dash = "4 3"
    # Top gridline (max value) — dashed
    _el("line", g,
        x1="0", y1="0", x2=str(plot_w), y2="0",
        stroke="#bbb", stroke_width="0.5", stroke_dasharray=dash)
    # Left spine + baseline as one path so the corner joint is seamless
    _el("path", g,
        d=f"M {plot_w} {day_h} L 0 {day_h} L 0 0",
        fill="none", stroke="#000", stroke_width="1",
        stroke_linejoin="miter")

    # Y-axis ticks at 0 and max value
    _el("path", g,
        d=f"M -4 0 L 0 0 M -4 {day_h} L 0 {day_h}",
        fill="none", stroke="#000", stroke_width="1")

    # Value labels on the left (max and zero only)
    max_label = f"{overall_max:.3g}"
    for y_px, label in ((0, max_label), (day_h, "0")):
        _el("text", g,
            x="-4", y=str(y_px),
            text_anchor="end", dominant_baseline="middle",
            font_size="9", font_family="sans-serif", fill="#444").text = label

    # X-axis ticks at the bottom (one per hour) - labels only at the very bottom
    tick_d_parts = []
    for hour in range(24):
        cx = _slot_cx(hour * 4, plot_w // 96)
        tick_d_parts.append(f"M {cx} {day_h} V {day_h + 5}")
    _el("path", g,
        d=" ".join(tick_d_parts),
        fill="none", stroke="#000", stroke_width="1")


def _build_weather_layer(
    g: ET.Element,
    date: str,
    weather_data: Dict[str, Tuple[np.ndarray, np.ndarray, np.ndarray]],
    plot_w: int,
    day_h: int,
    slot_w: int,
) -> None:
    """
    Append weather elements to *g* (in the clipped coordinate space):
      - Cloud-cover staircase polygon (gray, from top, opacity 0.20)
      - Rain bars (light blue, from bottom, opacity 0.65)
    """
    if date not in weather_data:
        return
    cloud_24, rain_24, _ = weather_data[date]
    if len(cloud_24) != 24 or len(rain_24) != 24:
        return

    # Expand hourly → quarter-hourly (96 values)
    cloud_96 = np.repeat(cloud_24, 4).tolist()
    rain_96 = (np.repeat(rain_24, 4) / 4.0).tolist()

    # --- Cloud staircase (fills from y=0 downward proportionally) ---
    # Build a closed staircase path along the top edge
    path_parts: List[str] = []
    first = True
    for i, pct in enumerate(cloud_96):
        depth = pct / 100.0 * day_h
        x_left = i * slot_w
        x_right = (i + 1) * slot_w
        if first:
            path_parts.append(f"M {x_left} 0")
            first = False
        path_parts.append(f"L {x_left} {_fmt(depth)}")
        path_parts.append(f"H {x_right}")
    path_parts.append(f"L {plot_w} 0 Z")
    _el("path", g,
        d=" ".join(path_parts),
        fill="#b0bec5", opacity="0.50")

    # --- Rain bars (from the bottom upward, capped at 40% of day_h ≈ 10 mm) ---
    max_rain_px = day_h * 0.40   # 10 mm maps to this height
    for i, rain_mm in enumerate(rain_96):
        if rain_mm <= 0:
            continue
        bar_h = min(rain_mm / 10.0, 1.0) * max_rain_px
        _el("rect", g,
            x=str(i * slot_w),
            y="0",
            width=str(slot_w),
            height=_fmt(bar_h),
            fill="#29b6f6", opacity="0.65")


def _build_pv_layer(
    g: ET.Element,
    date: str,
    pv_data: Dict[str, List[np.ndarray]],
    pv_plants: List[PVPlant],
    overall_max: float,
    day_h: int,
    slot_w: int,
    pv_peak_power: bool,
    anno_g: Optional[ET.Element] = None,
) -> None:
    """
    Append one polyline per PV plant plus a peak annotation dot+text.
    If anno_g is provided, annotations are rendered on that layer instead.
    """
    if date not in pv_data:
        return
    day_arrays = pv_data[date]
    for pi, (plant, arr) in enumerate(zip(pv_plants, day_arrays)):
        color = PV_COLORS[pi % len(PV_COLORS)]
        # Trim polyline to first→last slot with meaningful production
        _EPS = 1e-6
        nonzero = [i for i, v in enumerate(arr) if v > _EPS]
        if nonzero:
            lo, hi = nonzero[0], nonzero[-1]
            pts: List[Tuple[float, float]] = []
            # Touch baseline at the left edge of the first active slot
            pts.append((_slot_cx(lo, slot_w), float(day_h)))
            for i in range(lo, hi + 1):
                pts.append((_slot_cx(i, slot_w), _val_y(float(arr[i]), overall_max, day_h)))
            # Touch baseline at the right edge of the last active slot
            pts.append((_slot_cx(hi, slot_w), float(day_h)))
            _el("polyline", g,
                points=_pts(pts),
                fill="none",
                stroke=color,
                stroke_width="1",
                stroke_dasharray="3 2",
                opacity="0.85",
                stroke_linejoin="round",
                stroke_linecap="round")
        # Peak annotation
        if arr.any():
            peak_idx = int(np.argmax(arr))
            peak_val = float(arr[peak_idx])
            cx = _slot_cx(peak_idx, slot_w)
            py = _val_y(peak_val, overall_max, day_h)
            # Render annotation on anno_g if provided, otherwise on g
            anno_parent = anno_g if anno_g is not None else g
            _el("circle", anno_parent,
                cx=_fmt(cx), cy=_fmt(py), r="2.5",
                fill=color)
            if pv_peak_power:
                display_val = peak_val * 4.0
                ann_time = _idx_to_hhmm(peak_idx)
                ann_val = f"{display_val:.2f}kW"
            else:
                ann_time = _idx_to_hhmm(peak_idx)
                ann_val = f"{peak_val:.2f}kWh"
            y_offset = overall_max * 0.10 / overall_max * day_h if overall_max > 0 else 12.0
            t = _el("text", anno_parent,
                    x=_fmt(cx), y=_fmt(py - y_offset - 4),
                    text_anchor="middle", dominant_baseline="middle",
                    font_size="7", font_family="sans-serif",
                    fill=color)
            t.text = ann_time
            tspan = _el("tspan", t, x=_fmt(cx), dy="10")
            tspan.text = ann_val


def _build_sun_layer(
    g: ET.Element,
    date: str,
    lat: float,
    lon: float,
    tz_str: str,
    plot_w: int,
    day_h: int,
    slot_w: int,
    anno_g: Optional[ET.Element] = None,
) -> None:
    """
    Append sun arc elements:
      - Gold filled polygon (elevation area above horizon)
      - Darker gold polyline (arc outline)
      - Dashed sunrise / sunset vertical lines
      - Text labels for sunrise, sunset, solar noon
    If anno_g is provided, peak annotation (noon) is rendered on that layer instead.
    The solar elevation is mapped onto a secondary scale 0–80°→ 0–day_h.
    """
    elev = get_solar_elevation(date, lat, lon, tz_str)
    above = elev > 0
    if not above.any():
        return

    # Map elevation degrees to y-pixels (80° → top of strip)
    elev_scale = day_h / 80.0

    # Build polygon + polyline for visible arc
    poly_pts: List[Tuple[float, float]] = []
    line_pts: List[Tuple[float, float]] = []

    # Walking left-to-right through positive-elevation slots
    prev_above = False
    for i in range(96):
        cx = _slot_cx(i, slot_w)
        if above[i]:
            y = day_h - elev[i] * elev_scale
            if not prev_above:
                # Touch baseline at left edge of this slot (approximate sunrise x)
                poly_pts.append((i * slot_w, day_h))
            poly_pts.append((cx, y))
            line_pts.append((cx, y))
            prev_above = True
        else:
            if prev_above:
                # Touch baseline at right edge of previous slot
                poly_pts.append(((i) * slot_w - 1, day_h))
            prev_above = False

    if not poly_pts:
        return

    # Close the polygon at the baseline
    poly_pts.append((poly_pts[-1][0], day_h))

    _el("polygon", g,
        points=_pts(poly_pts),
        fill="gold", opacity="0.12")
    _el("polyline", g,
        points=_pts(line_pts),
        fill="none",
        stroke="#f5a623",
        stroke_width="1",
        opacity="0.85")

    # Sunrise / sunset indices
    sunrise_idx = int(np.argmax(above))
    sunset_idx = int(len(above) - 1 - np.argmax(above[::-1]))
    noon_idx = int(np.argmax(elev))
    noon_elev = float(elev[noon_idx])

    # Dashed vertical lines (full strip height, drawn as lines in SVG user space)
    for idx, color in ((sunrise_idx, "gold"), (sunset_idx, "#f5a623")):
        cx = _slot_cx(idx, slot_w)
        _el("line", g,
            x1=str(cx), y1="0", x2=str(cx), y2=str(day_h),
            stroke=color, stroke_width="0.8",
            stroke_dasharray="4 3", opacity="0.8")

    # Solar noon annotation
    noon_cx = _slot_cx(noon_idx, slot_w)
    noon_y = day_h - noon_elev * elev_scale
    _el("circle", g,
        cx=_fmt(noon_cx), cy=_fmt(noon_y), r="2",
        fill="#c87000")
    noon_text = _el("text", g,
                    x=_fmt(noon_cx), y=_fmt(noon_y - 14),
                    text_anchor="middle", dominant_baseline="middle",
                    font_size="7", font_family="sans-serif",
                    fill="#c87000")
    noon_text.text = _idx_to_hhmm(noon_idx)
    noon_tspan = _el("tspan", noon_text, x=_fmt(noon_cx), dy="10")
    noon_tspan.text = f"{noon_elev:.0f}\u00b0"

    # Sunrise label
    sr_cx = _slot_cx(sunrise_idx, slot_w)
    sr_t = _el("text", g,
               x=_fmt(sr_cx - 2), y=_fmt(day_h - 2),
               text_anchor="start", dominant_baseline="auto",
               font_size="7", font_family="sans-serif",
               fill="goldenrod", opacity="0.9",
               transform=f"rotate(-90,{_fmt(sr_cx - 2)},{_fmt(day_h - 2)})")
    sr_t.text = _idx_to_hhmm(sunrise_idx)

    # Sunset label
    ss_cx = _slot_cx(sunset_idx, slot_w)
    ss_t = _el("text", g,
               x=_fmt(ss_cx + 2), y=_fmt(day_h - 2),
               text_anchor="start", dominant_baseline="auto",
               font_size="7", font_family="sans-serif",
               fill="#c87000", opacity="0.9",
               transform=f"rotate(-90,{_fmt(ss_cx + 2)},{_fmt(day_h - 2)})")
    ss_t.text = _idx_to_hhmm(sunset_idx)


def _build_energy_bars(
    g: ET.Element,
    day_idx: int,
    dataset: MonthlyDataset,
    requested_pairs: List[Tuple[str, str]],
    overall_max: float,
    bar_w: int,
    day_h: int,
    slot_w: int,
) -> None:
    """
    Append all energy bar <rect> elements for one day.
    """
    n_pairs = len(requested_pairs)
    for pair_idx, (prel_type, imm_type) in enumerate(requested_pairs):
        prel_vals = (
            dataset.data[prel_type][day_idx][1].intervals
            if prel_type in dataset.data
            else [0.0] * 96
        )
        imm_vals = (
            dataset.data[imm_type][day_idx][1].intervals
            if imm_type in dataset.data
            else [0.0] * 96
        )
        prel_color = COLORS[prel_type]
        imm_color = COLORS[imm_type]

        for slot in range(96):
            for is_imm, vals, color in (
                (False, prel_vals, prel_color),
                (True, imm_vals, imm_color),
            ):
                v = vals[slot]
                if v <= 0:
                    continue
                bar_h = v / overall_max * day_h
                bx = _bar_x(slot, pair_idx, is_imm, bar_w, slot_w)
                by = day_h - bar_h
                _el("rect", g,
                    x=str(bx),
                    y=_fmt(by),
                    width=str(bar_w),
                    height=_fmt(bar_h),
                    fill=color,
                    opacity="0.85")


# ---------------------------------------------------------------------------
# Main SVG assembly
# ---------------------------------------------------------------------------

def draw_svg_month(
    dataset: MonthlyDataset,
    requested_pairs: List[Tuple[str, str]],
    output_path: Path,
    location: Optional[Tuple[float, float, str]] = None,
    weather_data: Optional[Dict[str, Tuple[np.ndarray, np.ndarray, np.ndarray]]] = None,
    pv_data: Optional[Dict[str, List[np.ndarray]]] = None,
    pv_plants: Optional[List[PVPlant]] = None,
    pv_peak_power: bool = False,
    bar_w: int = _BAR_W_DEFAULT,
) -> None:
    """
    Build and write one month's SVG file.
    """
    dates = dataset.get_all_dates()
    if not dates:
        print_stderr(f"WARNING: No data for {dataset.month}, skipping")
        return

    n_pairs = len(requested_pairs)
    num_days = len(dates)

    # Y-axis scale
    max_value = dataset.get_max_value()
    pv_max = 0.0
    if pv_data:
        for day_arrays in pv_data.values():
            for arr in day_arrays:
                if len(arr):
                    pv_max = max(pv_max, float(arr.max()))
    overall_max = max(max_value, pv_max)
    if overall_max <= 0:
        overall_max = 1.0

    # Derived layout integers
    sw = _slot_w(n_pairs, bar_w)                    # slot width in px
    plot_w = 96 * sw                                # total plot width in px
    total_w = _MARGIN_LEFT + plot_w + _MARGIN_RIGHT
    total_h = (_HEADER_H
               + num_days * _DAY_H
               + max(0, num_days - 1) * _DAY_GAP
               + _XAXIS_H
               + _LEGEND_H)

    # Create root SVG
    root = _el("svg")
    root.set("width", str(total_w))
    root.set("height", str(total_h))
    root.set("viewBox", f"0 0 {total_w} {total_h}")

    # <defs>
    defs = _el("defs", root)
    _build_defs(defs, plot_w, _DAY_H, overall_max)

    # Title
    t = _el("text", root,
            x=str(total_w // 2),
            y=str(_HEADER_H - 10),
            text_anchor="middle",
            dominant_baseline="auto",
            font_size="22",
            font_weight="bold",
            font_family="sans-serif",
            fill="#222")
    t.text = f"Energy Profile \u2013 {dataset.month}"

    # One group per day
    for day_idx, date in enumerate(dates):
        day_top = _HEADER_H + day_idx * (_DAY_H + _DAY_GAP)
        # Translate so (0,0) = top-left of this day's plot rectangle
        day_g = _el("g", root,
                    transform=f"translate({_MARGIN_LEFT},{day_top})")

        # Clipped group for weather, PV, sun arc (layers 1-3)
        bg_g = _el("g", day_g,
                   clip_path="url(#day-clip)")

        # Create annotation group (will append to day_g last, to render on top)
        anno_g = ET.Element(f"{{{_SVG_NS}}}g")

        # Layer 1: weather
        if weather_data is not None:
            _build_weather_layer(bg_g, date, weather_data, plot_w, _DAY_H, sw)

        # Layer 2: PV production
        if pv_data is not None and pv_plants:
            _build_pv_layer(bg_g, date, pv_data, pv_plants, overall_max,
                            _DAY_H, sw, pv_peak_power, anno_g=anno_g)

        # Layer 3: sun arc
        if location is not None:
            s_lat, s_lon, s_tz = location
            _build_sun_layer(bg_g, date, s_lat, s_lon, s_tz,
                             plot_w, _DAY_H, sw, anno_g=anno_g)

        # Layer 4: energy bars (no clip needed — rects stay within [0, DAY_H])
        _build_energy_bars(day_g, day_idx, dataset, requested_pairs,
                           overall_max, bar_w, _DAY_H, sw)

        # Layer 5: axis clone
        _el("use", day_g, href="#day-axis")

        # Layer 6: parabola peak annotations (append to day_g LAST so they render on top)
        if anno_g is not None and len(anno_g) > 0:
            day_g.append(anno_g)
        dd_text = _el("text", day_g,
                      x="-16",
                      y=str(_DAY_H // 2),
                      text_anchor="end",
                      dominant_baseline="middle",
                      font_size="11",
                      font_weight="bold",
                      font_family="sans-serif",
                      fill="#333")
        dd_text.text = date.split("-")[-1]

    # X-axis hour labels (below last day) - includes baseline, ticks, and hour labels
    xaxis_y = _HEADER_H + num_days * _DAY_H + max(0, num_days - 1) * _DAY_GAP
    xaxis_g = _el("g", root,
                  transform=f"translate({_MARGIN_LEFT},{xaxis_y})")
    # Baseline + tick marks as one consolidated path
    tick_d_parts = [f"M 0 0 H {plot_w}"]
    for hour in range(24):
        cx = _slot_cx(hour * 4, sw)
        tick_d_parts.append(f"M {cx} 0 V 5")
    _el("path", xaxis_g,
        d=" ".join(tick_d_parts),
        fill="none", stroke="#000", stroke_width="1",
        stroke_linejoin="miter")
    # Hour labels (00-23) at the bottom
    for hour in range(24):
        cx = _slot_cx(hour * 4, sw)
        xt = _el("text", xaxis_g,
                 x=str(cx),
                 y="18",
                 text_anchor="middle",
                 dominant_baseline="auto",
                 font_size="10",
                 font_family="sans-serif",
                 fill="#555",
                 transform=f"rotate(45 {cx} 18)")
        xt.text = f"{hour:02d}"
    # "Hour of day" axis label
    lbl = _el("text", xaxis_g,
              x=str(plot_w // 2),
              y="36",
              text_anchor="middle",
              dominant_baseline="auto",
              font_size="11",
              font_weight="bold",
              font_family="sans-serif",
              fill="#444")
    lbl.text = "Hour of day"

    # Legend
    legend_y = xaxis_y + _XAXIS_H
    legend_g = _el("g", root,
                   transform=f"translate({_MARGIN_LEFT},{legend_y})")

    legend_items: List[Tuple[str, str]] = []  # (color, label)
    for prel_type, imm_type in requested_pairs:
        legend_items.append((COLORS[prel_type], TOKEN_TO_SUFFIX[prel_type]))
        legend_items.append((COLORS[imm_type], TOKEN_TO_SUFFIX[imm_type]))
    if pv_plants:
        for pi, plant in enumerate(pv_plants):
            legend_items.append((PV_COLORS[pi % len(PV_COLORS)], plant.label))

    swatch_w, swatch_h = 18, 12
    item_w = 160
    items_per_row = max(1, plot_w // item_w)
    for li, (color, label) in enumerate(legend_items):
        col = li % items_per_row
        row = li // items_per_row
        lx = col * item_w
        ly = row * 20
        _el("rect", legend_g,
            x=str(lx), y=str(ly),
            width=str(swatch_w), height=str(swatch_h),
            fill=color, opacity="0.85")
        lt = _el("text", legend_g,
                 x=str(lx + swatch_w + 5),
                 y=str(ly + swatch_h - 1),
                 dominant_baseline="auto",
                 font_size="11",
                 font_family="sans-serif",
                 fill="#333")
        lt.text = label

    # Serialise
    tree = ET.ElementTree(root)
    ET.indent(tree, space="")  # compact: no extra whitespace
    try:
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            tree.write(fh, encoding="unicode", xml_declaration=False,
                       short_empty_elements=True)
        print(str(output_path.absolute()))
        print_stderr(f"  Saved to {output_path.name}")
    except Exception as e:
        print_stderr(f"ERROR writing SVG to {output_path}: {e}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate monthly SVG energy plots from quarterly CSV data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python quarters_to_csv.py *.json | python draw_quarters.py
  python draw_quarters.py --energy-types attiva,rind
  python draw_quarters.py --bar-width 16 --location "45.5N 12.7E" --pv "180;30;6" *.csv
        """,
    )
    parser.add_argument(
        "--energy-types",
        type=str,
        default="attiva",
        help="Comma-separated energy types or shortcuts (default: attiva). "
             "Shortcuts: attiva, rind, rcap.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=".",
        help="Directory to save SVG files (default: current directory)",
    )
    parser.add_argument(
        "--location",
        type=str,
        default=None,
        metavar="LAT LON",
        help='Geographic location for solar / weather overlay, e.g. "45.5N 9.2E".',
    )
    parser.add_argument(
        "--pv",
        type=str,
        action="append",
        default=[],
        metavar="AZ;TILT;KWP[;DATE[;LABEL]]",
        help='PV plant spec: "azimuth;tilt;kWp[;YYYY-MM[-DD][;label]]". '
             "Requires --location. Can be repeated.",
    )
    parser.add_argument(
        "--pv-derate",
        type=str,
        default=None,
        metavar="RATE[,RATE,...]",
        help="Comma-separated annual degradation rates aligned with --pv order.",
    )
    parser.add_argument(
        "--pv-pp",
        action="store_true",
        default=False,
        help="Annotate PV peaks as instantaneous power (kW) instead of kWh/slot.",
    )
    parser.add_argument(
        "--bar-width",
        type=int,
        default=_BAR_W_DEFAULT,
        metavar="PX",
        help=f"Width in pixels of each individual bar (default: {_BAR_W_DEFAULT}).",
    )

    args = parser.parse_args()

    # Energy types
    try:
        requested_pairs = parse_energy_types_arg(args.energy_types)
    except ValueError as e:
        print_stderr(f"ERROR: {e}")
        return 1
    pair_labels = [f"{p[0].split('-')[0]}/{p[1].split('-')[0]}" for p in requested_pairs]
    print_stderr(f"Selected energy type pairs: {pair_labels}")

    # Location + timezone
    solar_location: Optional[Tuple[float, float, str]] = None
    if args.location:
        try:
            s_lat, s_lon = parse_location_arg(args.location)
            tf = TimezoneFinder()
            s_tz = tf.timezone_at(lat=s_lat, lng=s_lon)
            if s_tz is None:
                print_stderr("WARNING: Could not determine timezone. Solar overlay skipped.")
            else:
                solar_location = (s_lat, s_lon, s_tz)
                print_stderr(f"Solar overlay: lat={s_lat}, lon={s_lon}, tz={s_tz}")
        except ValueError as e:
            print_stderr(f"ERROR: Invalid --location: {e}")
            return 1
    else:
        print_stderr("NOTE: --location not provided. Solar elevation overlay will be skipped.")

    # PV plants
    pv_plants: List[PVPlant] = []
    if args.pv:
        if solar_location is None:
            print_stderr("ERROR: --pv requires --location.")
            return 1
        for pv_str in args.pv:
            try:
                pv_plants.append(parse_pv_arg(pv_str))
            except ValueError as e:
                print_stderr(f"ERROR: {e}")
                return 1
        print_stderr(f"PV plants: {[p.label for p in pv_plants]}")

    # Per-plant degradation rate overrides
    if args.pv_derate and pv_plants:
        parts = args.pv_derate.split(",")
        for i, part in enumerate(parts):
            if i >= len(pv_plants):
                print_stderr(
                    f"WARNING: --pv-derate has more entries than plants; extras ignored."
                )
                break
            part = part.strip()
            if not part:
                continue
            try:
                rate = float(part)
            except ValueError:
                print_stderr(f"ERROR: --pv-derate entry {i + 1} is not a number: '{part}'")
                return 1
            if not (0.0 <= rate <= 1.0):
                print_stderr(f"ERROR: --pv-derate entry {i + 1} must be in [0,1], got {rate}")
                return 1
            pv_plants[i].degradation_rate = rate
            print_stderr(f"  {pv_plants[i].label}: degradation rate {rate * 100:.3f}%/yr")

    # Input files
    csv_files = get_input_files()
    if not csv_files:
        print_stderr("ERROR: No CSV files found")
        print_stderr("  Place *.csv files in the current directory or pipe filenames.")
        return 1
    print_stderr(f"Processing {len(csv_files)} input file(s)...")

    # Load data
    datasets = load_and_group_files(csv_files, requested_pairs)
    if not datasets:
        print_stderr("ERROR: No valid datasets created")
        return 1
    print_stderr(f"Generated {len(datasets)} monthly dataset(s)")

    # Fetch weather per month (requires location)
    weather_by_month: Dict[str, Any] = {}
    if solar_location is not None:
        s_lat, s_lon, s_tz = solar_location
        for month in sorted(datasets.keys()):
            print_stderr(f"Fetching weather for {month}...", end=" ", flush=True)
            try:
                weather_by_month[month] = fetch_weather_for_month(month, s_lat, s_lon, s_tz)
                print_stderr("OK")
            except Exception as e:
                print_stderr(f"WARNING: {e}")

    # Simulate PV
    pv_by_month: Dict[str, Any] = {}
    if pv_plants and solar_location is not None:
        s_lat, s_lon, s_tz = solar_location
        for month in sorted(datasets.keys()):
            print_stderr(f"Simulating PV for {month}...", end=" ", flush=True)
            try:
                pv_by_month[month] = simulate_pv_month(
                    month, s_lat, s_lon, s_tz,
                    pv_plants,
                    weather_data=weather_by_month.get(month),
                )
                print_stderr("OK")
            except Exception as e:
                print_stderr(f"WARNING: PV simulation failed for {month}: {e}")

    # Output directory
    output_dir = Path(args.output_dir)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print_stderr(f"ERROR creating output directory: {e}")
        return 1

    # Generate SVGs
    for month in sorted(datasets.keys()):
        dataset = datasets[month]
        plot_path = output_dir / f"{month}_energy.svg"
        print_stderr(
            f"Generating SVG for {month} ({dataset.days_in_month} days)...",
            end=" ", flush=True,
        )
        try:
            draw_svg_month(
                dataset, requested_pairs, plot_path,
                location=solar_location,
                weather_data=weather_by_month.get(month),
                pv_data=pv_by_month.get(month),
                pv_plants=pv_plants if pv_plants else None,
                pv_peak_power=args.pv_pp,
                bar_w=args.bar_width,
            )
        except Exception as e:
            print_stderr(f"FAILED: {e}")
            import traceback
            traceback.print_exc()

    print_stderr("Done")
    return 0


if __name__ == "__main__":
    exit(main())
