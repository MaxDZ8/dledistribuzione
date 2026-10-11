#!/usr/bin/env python3
"""
Load Profile JSON to CSV Converter

Converts E-Distribuzione load profile JSON files to CSV format suitable for
spreadsheet applications. Extracts MappaDailyLoadProfile data and formats it
as Italian-style CSV (semicolon-delimited, comma as decimal separator).

Supports two output formats:
    Default: ISO dates (YYYY-MM-DD), minimal quoting
    Original: Italian dates (dd/MM/YYYY), all values quoted, matching original edistribuzione format

Usage:
    ls *.json | python quarters_to_csv.py                         # Read from stdin, default format
    python quarters_to_csv.py file1.json file2.json               # Read from arguments, default format
    python quarters_to_csv.py --original-style file1.json file2.json  # Original edistribuzione format
"""

import json
import sys
import csv
import argparse
import io
from pathlib import Path
from typing import Any, Dict, List, Tuple


def print_stderr(*args: object, **kwargs: Any) -> None:
    """Print to stderr instead of stdout."""
    print(*args, **kwargs, file=sys.stderr)


def generate_time_intervals() -> List[str]:
    """
    Generate 96 time interval headers for a 24-hour period (15-minute intervals).
    
    Returns:
        List of time interval strings (e.g., ['00:00-00:15', '00:15-00:30', ...])
    """
    intervals: List[str] = []
    for hour in range(24):
        for minute in [0, 15, 30, 45]:
            start = f"{hour:02d}:{minute:02d}"
            
            # Calculate end time
            end_minute = minute + 15
            end_hour = hour
            if end_minute >= 60:
                end_minute = 0
                end_hour = (hour + 1) % 24
            
            end = f"{end_hour:02d}:{end_minute:02d}"
            intervals.append(f"{start}-{end}")
    
    return intervals


def parse_date_key(date_key: str) -> Tuple[int, int, int]:
    """
    Parse a date key in format YYYYMM-DD to (year, month, day).
    
    Args:
        date_key: Date key string (e.g., '202512-01')
    
    Returns:
        Tuple of (year, month, day)
    
    Raises:
        ValueError: If date format is invalid
    """
    try:
        # Format: YYYYMM-DD
        parts = date_key.split('-')
        if len(parts) != 2:
            raise ValueError(f"Invalid date key format: {date_key}")
        
        year_month = parts[0]
        day = int(parts[1])
        
        if len(year_month) != 6:
            raise ValueError(f"Invalid year-month part: {year_month}")
        
        year = int(year_month[:4])
        month = int(year_month[4:6])
        
        if not (1 <= month <= 12) or not (1 <= day <= 31):
            raise ValueError(f"Invalid date values: year={year}, month={month}, day={day}")
        
        return year, month, day
    except (ValueError, IndexError) as e:
        raise ValueError(f"Failed to parse date key '{date_key}': {e}")


def convert_date_to_iso(year: int, month: int, day: int) -> str:
    """
    Convert year, month, day to ISO 8601 format (YYYY-MM-DD).
    
    Args:
        year: 4-digit year
        month: 1-12 month
        day: 1-31 day
    
    Returns:
        ISO 8601 formatted date string
    """
    return f"{year:04d}-{month:02d}-{day:02d}"


def convert_date_to_italian(iso_date: str) -> str:
    """
    Convert ISO 8601 date format (YYYY-MM-DD) to Italian format (dd/MM/YYYY).
    
    Args:
        iso_date: Date in ISO 8601 format (YYYY-MM-DD)
    
    Returns:
        Date in Italian format (dd/MM/YYYY)
    """
    parts = iso_date.split('-')
    if len(parts) != 3:
        raise ValueError(f"Invalid ISO date format: {iso_date}")
    year, month, day = parts
    return f"{day}/{month}/{year}"


def extract_load_profile(json_data: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    """
    Extract MappaDailyLoadProfile from JSON response.
    
    Args:
        json_data: Parsed JSON data from e-distribuzione API
    
    Returns:
        Dictionary with date keys and interval data
    
    Raises:
        ValueError: If MappaDailyLoadProfile is not found
    """
    try:
        # Navigate through the response structure
        actions = json_data.get('actions')
        if not isinstance(actions, list) or not actions:
            raise ValueError("No valid 'actions' field in JSON")
        
        first_action = actions[0]
        if not isinstance(first_action, dict):
            raise ValueError("Invalid action entry in JSON")
        
        return_value = first_action.get('returnValue', {})
        if not isinstance(return_value, dict):
            raise ValueError("Invalid 'returnValue' in JSON")
        
        load_profile_raw = return_value.get('MappaDailyLoadProfile')
        if not isinstance(load_profile_raw, dict):
            raise ValueError("MappaDailyLoadProfile not found in response")
        
        load_profile: Dict[str, Dict[str, float]] = {}
        for date_key, intervals in load_profile_raw.items():
            if not isinstance(date_key, str) or not isinstance(intervals, dict):
                continue
            
            parsed_intervals: Dict[str, float] = {}
            for interval_key, value in intervals.items():
                if isinstance(interval_key, str):
                    try:
                        parsed_intervals[interval_key] = float(value)
                    except (TypeError, ValueError):
                        parsed_intervals[interval_key] = 0.0
            
            load_profile[date_key] = parsed_intervals
        
        return load_profile
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"Failed to extract load profile: {e}")


def convert_json_to_csv(json_file: Path, original_style: bool = False) -> Path:
    """
    Convert a single JSON file to CSV format.
    
    Args:
        json_file: Path to the JSON file
        original_style: If True, output in original edistribuzione format (quoted values, Italian dates)
    
    Raises:
        Exception: On any processing error (caught by caller)
    """
    # Read JSON file
    try:
        with open(json_file, 'r', encoding='utf-8') as f:
            parsed_json = json.load(f)
            if not isinstance(parsed_json, dict):
                raise ValueError("JSON root must be an object")
            json_data: Dict[str, Any] = parsed_json
    except FileNotFoundError:
        raise FileNotFoundError(f"File not found: {json_file}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}")
    
    # Extract load profile
    load_profile = extract_load_profile(json_data)
    
    # Sort dates
    dates: List[Tuple[str, str]] = []
    for date_key in load_profile.keys():
        try:
            year, month, day = parse_date_key(date_key)
            iso_date = convert_date_to_iso(year, month, day)
            dates.append((iso_date, date_key))
        except ValueError as e:
            raise ValueError(f"Invalid date key '{date_key}': {e}")
    
    dates.sort(key=lambda x: x[0])  # Sort by ISO date
    
    # Prepare CSV data
    time_intervals = generate_time_intervals()
    
    # Build rows
    rows: List[List[str]] = []
    for iso_date, date_key in dates:
        # Convert date format based on style
        if original_style:
            date_str = convert_date_to_italian(iso_date)
        else:
            date_str = iso_date
        
        day_profile = load_profile[date_key]
        
        # DST fall-back day: the API returns 100 intervals instead of 96.
        # The extra four are keyed 97-100 and represent the repeated 02:00-03:00 hour
        # (the clock is set back one hour at 03:00, so that hour occurs twice).
        # Interval layout for the DST day:
        #   keys  1-12  → 00:00-03:00  (first pass, before clock change)
        #   keys 97-100 → 02:00-03:00  (second pass, after clock change)
        #   keys 13-96  → 03:00-24:00  (remainder of the day, unambiguous)
        # We emit two 96-column rows so no data is lost:
        #   Row 1: intervals 1-12, then 84 zeros  (first 3 h + blank second half)
        #   Row 2: 8 zeros, intervals 97-100, intervals 13-96  (blank first 2 h +
        #          repeated hour + rest of day)
        # The 8 leading zeros in row 2 keep column alignment: positions 1-8 cover
        # 00:00-02:00 which genuinely did not occur a second time.
        if "97" in day_profile:
            row1: List[str] = [date_str]
            for i in range(1, 13):
                v = float(day_profile.get(str(i), 0.0))
                row1.append(str(int(v)) if v == int(v) else str(v).replace('.', ','))
            row1.extend(["0"] * 84)
            rows.append(row1)
            row2: List[str] = [date_str]
            row2.extend(["0"] * 8)
            for i in list(range(97, 101)) + list(range(13, 97)):
                v = float(day_profile.get(str(i), 0.0))
                row2.append(str(int(v)) if v == int(v) else str(v).replace('.', ','))
            rows.append(row2)
            continue
        
        row: List[str] = [date_str]
        
        # Extract intervals 1-96
        for interval_num in range(1, 97):
            interval_key = str(interval_num)
            value = day_profile.get(interval_key, 0.0)
            
            float_value = float(value)
            
            # Check if value is an accurate integer (no fractional part)
            if float_value == int(float_value):
                value_str = str(int(float_value))
            else:
                # Format with comma as decimal separator (Italian CSV format)
                value_str = str(float_value).replace('.', ',')
            
            row.append(value_str)
        
        rows.append(row)
    
    # Write CSV file
    csv_file = json_file.with_suffix('.csv')
    
    try:
        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            # Write header without quoting
            time_intervals = generate_time_intervals()
            header = ['Giorno'] + time_intervals
            f.write(';'.join(header) + '\n')
            
            # Write data rows with appropriate quoting
            if original_style:
                # Original style: quote all values and add trailing separator
                for row in rows:
                    row_buffer = io.StringIO()
                    row_writer = csv.writer(row_buffer, delimiter=';', quoting=csv.QUOTE_ALL)
                    row_writer.writerow(row)
                    row_str = row_buffer.getvalue().rstrip('\r\n')  # Remove trailing newline
                    f.write(row_str + ';\n')  # Add trailing semicolon
            else:
                # Default style: minimal quoting, no trailing separator
                writer = csv.writer(f, delimiter=';', quoting=csv.QUOTE_MINIMAL)
                writer.writerows(rows)
    except IOError as e:
        raise IOError(f"Failed to write CSV file {csv_file}: {e}")
    
    return csv_file


def get_input_files() -> Tuple[List[Path], bool]:
    """
    Get list of input JSON files from stdin or command-line arguments.
    
    Also parses the --original-style flag if present.
    
    Returns:
        Tuple of (list of Path objects for JSON files, original_style flag)
    """
    parser = argparse.ArgumentParser(
        description="Convert E-Distribuzione load profile JSON files to CSV format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ls *.json | python quarters_to_csv.py                         # Read from stdin, default format
  python quarters_to_csv.py file1.json file2.json               # Read from arguments, default format
  python quarters_to_csv.py --original-style file1.json file2.json  # Original edistribuzione format
  ls *.json | python quarters_to_csv.py --original-style         # Piped input with original format
        """
    )
    
    parser.add_argument(
        '--original-style',
        action='store_true',
        help='Output in original edistribuzione format (quoted values, Italian dates)'
    )
    
    parser.add_argument(
        'files',
        nargs='*',
        type=str,
        help='JSON files to convert (if not provided, reads from stdin)'
    )
    
    args = parser.parse_args()
    
    files: List[Path] = []
    
    # If files were provided as arguments, use those
    if args.files:
        files = [Path(f) for f in args.files]
    else:
        # Otherwise read from stdin
        try:
            for line in sys.stdin:
                line = line.strip()
                if line:
                    files.append(Path(line))
        except KeyboardInterrupt:
            pass
    
    return files, args.original_style


def main():
    """Main entry point for the script."""
    files, original_style = get_input_files()
    
    if not files:
        print_stderr("ERROR: No input files provided")
        print_stderr("Use --help for usage information")
        return 1
    
    error_count = 0
    success_count = 0
    
    for json_file in files:
        try:
            created = convert_json_to_csv(json_file, original_style=original_style)
            print(created.absolute())
            success_count += 1
        except Exception as e:
            error_count += 1
            print_stderr(f"ERROR processing {json_file}: {e}")
    
    if error_count > 0:
        print_stderr(f"Completed with {error_count} error(s) and {success_count} successful conversion(s)")
    
    return 0


if __name__ == "__main__":
    exit(main())
