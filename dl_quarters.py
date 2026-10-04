#!/usr/bin/env python3
"""
E-Distribuzione Load Profile Data Downloader

Automates downloading quarterly load profile data from the E-Distribuzione 
(Italian electric infrastructure company) private portal by replicating 
Salesforce Aura API requests.
"""

import requests
import json
import argparse
import sys
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Tuple, Any, Optional, Set
from dataclasses import dataclass


def print_stderr(*args: object, **kwargs: Any) -> None:
    """Print to stderr instead of stdout."""
    print(*args, **kwargs, file=sys.stderr)


@dataclass
class SessionCredentials:
    """Salesforce session credentials extracted from browser."""
    sid: str
    aura_token: str
    aura_context: str
    user_agent: str
    page_scope_id: str


@dataclass
class PreparedRequest:
    """Prepared HTTP request components."""
    url: str
    data: dict[str, str]
    headers: dict[str, str]
    cookies: dict[str, str]


# Token mappings and conversions
TOKEN_ALIASES = {
    "R1": "reattiva-induttiva-prelevata",
    "R2": "reattiva-induttiva-immessa",
    "R3": "reattiva-capacitiva-prelevata",
    "R4": "reattiva-capacitiva-immessa",
}

TOKEN_TO_SUFFIX = {
    "attiva-prelevata": "attiva_prel",
    "attiva-immessa": "attiva_imm",
    "reattiva-induttiva-prelevata": "rind_prel",
    "reattiva-induttiva-immessa": "rind_imm",
    "reattiva-capacitiva-prelevata": "rcap_prel",
    "reattiva-capacitiva-immessa": "rcap_imm",
}

TOKEN_TO_MAGNITUDE = {
    "attiva-prelevata": "A+",
    "attiva-immessa": "A-",
    "reattiva-induttiva-prelevata": "RI+",
    "reattiva-induttiva-immessa": "RI-",
    "reattiva-capacitiva-prelevata": "RC+",
    "reattiva-capacitiva-immessa": "RC-",
}

VALID_TOKENS = set(TOKEN_TO_SUFFIX.keys()) | set(TOKEN_ALIASES.values())


def normalize_token(token: str) -> str:
    """
    Convert token aliases to their full names.
    
    Args:
        token: Token or alias (e.g., 'R1' or 'reattiva-induttiva-prelevata')
    
    Returns:
        str: The normalized token name
    
    Raises:
        ValueError: If token is not recognized
    """
    token = token.strip()
    if token in TOKEN_ALIASES:
        return TOKEN_ALIASES[token]
    if token in TOKEN_TO_SUFFIX:
        return token
    raise ValueError(f"Unknown token: '{token}'. Valid tokens are: {', '.join(sorted(VALID_TOKENS))}")


def parse_tokenized_arg(tokenized_str: str) -> Set[str]:
    """
    Parse the tokenized argument and return normalized tokens.
    
    Args:
        tokenized_str: Comma-separated string of tokens/aliases
    
    Returns:
        Set[str]: Normalized token names
    
    Raises:
        ValueError: If any token is invalid
    """
    if not tokenized_str or not tokenized_str.strip():
        raise ValueError("tokenized argument cannot be empty")
    
    tokens = [t.strip() for t in tokenized_str.split(",")]
    normalized: Set[str] = set()
    
    for token in tokens:
        if not token:
            raise ValueError("Empty token in tokenized argument")
        try:
            normalized.add(normalize_token(token))
        except ValueError as e:
            raise ValueError(f"Invalid tokenized argument: {e}")
    
    return normalized


def get_token_suffixes(tokens: Set[str]) -> str:
    """
    Generate filename suffixes from a set of tokens.
    
    Suffixes are joined in sorted order for consistency.
    
    Args:
        tokens: Set of normalized token names
    
    Returns:
        str: Suffixes joined with underscores (e.g., 'attiva_imm_attiva_prel')
    """
    suffixes = [TOKEN_TO_SUFFIX[token] for token in sorted(tokens)]
    return "_".join(suffixes)


def extract_from_cookie_header(cookie_header: str, key: str) -> str:
    """
    Extract a value from an HTTP Cookie header string.
    
    Args:
        cookie_header: Full Cookie header value (key1=val1; key2=val2; ...)
        key: The cookie name to extract
    
    Returns:
        str: The cookie value
    
    Raises:
        ValueError: If the cookie key is not found
    """
    pairs = cookie_header.split("; ")
    for pair in pairs:
        if pair.startswith(f"{key}="):
            return pair[len(f"{key}="):]
    raise ValueError(f"Cookie '{key}' not found in provided Cookie header")


def prompt_for_credentials() -> SessionCredentials:
    """
    Prompt user to provide session credentials from browser DevTools.
    
    Returns:
        SessionCredentials: Session credentials for API authentication
    """
    print_stderr("\n" + "="*70)
    print_stderr("E-DISTRIBUZIONE DATA DOWNLOADER")
    print_stderr("="*70)
    print_stderr("\nTo use this script, you need to extract session credentials from")
    print_stderr("your browser's DevTools (F12 -> Network tab).")
    print_stderr("\nSteps:")
    print_stderr("  1. Open https://private.e-distribuzione.it and navigate to the")
    print_stderr("     'Curve di Carico' page")
    print_stderr("  2. Open DevTools (F12) and go to Network tab")
    print_stderr("  3. Filter by 'aura' and click the export button")
    print_stderr("  4. Copy the values below from the request headers and form data")
    print_stderr("-"*70 + "\n")
    
    cookie_header = input("Paste the full Cookie header value: ").strip()
    if not cookie_header:
        raise ValueError("Cookie header is required")
    
    try:
        sid = extract_from_cookie_header(cookie_header, "sid")
    except ValueError as e:
        raise ValueError(f"Failed to extract sid: {e}")
    
    page_scope_id = input(
        "Enter x-sfdc-page-scope-id header (from request headers): "
    ).strip()
    if not page_scope_id:
        raise ValueError("x-sfdc-page-scope-id is required")
    
    aura_token = input("Enter aura.token value (from form data): ").strip()
    if not aura_token:
        raise ValueError("aura.token is required")
    
    aura_context = input("Enter aura.context value (from form data): ").strip()
    if not aura_context:
        raise ValueError("aura.context is required")
    
    user_agent = input(
        "Enter User-Agent header (or press Enter for default): "
    ).strip()
    if not user_agent:
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 OPR/127.0.0.0"
    
    return SessionCredentials(
        sid=sid,
        aura_token=aura_token,
        aura_context=aura_context,
        user_agent=user_agent,
        page_scope_id=page_scope_id,
    )


def parse_date_range_string(date_input: str) -> Tuple[date, date]:
    """
    Parse a date range string in YYYY-MM-DD or YYYY-MM format.
    
    - YYYY-MM-DD: Single day, both start and end are that date
    - YYYY-MM: Full month, start is 1st day, end is last day of month
    
    The returned range is clamped to yesterday as the maximum date.
    
    Args:
        date_input: Date string in YYYY-MM-DD or YYYY-MM format
    
    Returns:
        Tuple[date, date]: (start_date, end_date) for the parsed range
    
    Raises:
        ValueError: If the format is invalid or date is in the future
    """
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    
    # Try YYYY-MM-DD format first
    try:
        parsed_date = datetime.strptime(date_input, "%Y-%m-%d").date()
        if parsed_date > today:
            raise ValueError(f"Date {parsed_date} is in the future (today is {today})")
        # Clamp to yesterday
        parsed_date = min(parsed_date, yesterday)
        return parsed_date, parsed_date
    except ValueError as e:
        if "in the future" in str(e):
            raise
        # If it's not the right format, try next format
        pass
    
    # Try YYYY-MM format
    try:
        parsed_date = datetime.strptime(date_input, "%Y-%m").date()
        start_date = parsed_date
        
        # Calculate last day of the month
        if parsed_date.month == 12:
            next_month = parsed_date.replace(year=parsed_date.year + 1, month=1, day=1)
        else:
            next_month = parsed_date.replace(month=parsed_date.month + 1, day=1)
        end_date = next_month - timedelta(days=1)
        
        # Check if the month is in the future
        if start_date > today:
            raise ValueError(f"Month {date_input} is in the future (today is {today})")
        
        # Clamp end date to yesterday
        end_date = min(end_date, yesterday)
        
        return start_date, end_date
    except ValueError as e:
        if "in the future" in str(e):
            raise
        raise ValueError(f"Invalid date format '{date_input}'. Use YYYY-MM-DD or YYYY-MM.")


def prompt_for_dates(
    start_date_arg: Optional[str] = None,
    end_date_arg: Optional[str] = None,
) -> Tuple[datetime, datetime]:
    """
    Prompt user for start and end dates.
    
    If date arguments are provided, use them instead of prompting.
    
    Supports YYYY-MM-DD and YYYY-MM formats:
    - YYYY-MM-DD: Specifies a single day
    - YYYY-MM: Specifies the entire month (clamped to yesterday)
    
    Start date: Defaults to first day of previous month if not provided.
    End date:   Defaults to yesterday if not provided.
    
    Args:
        start_date_arg: Optional start date from command line
        end_date_arg: Optional end date from command line
    
    Returns:
        Tuple[datetime, datetime]: (start_date, end_date)
    
    Raises:
        ValueError: If dates are invalid or start is in future
    """
    
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    
    if today.month == 1:
        previous_month_start = today.replace(year=today.year - 1, month=12, day=1)
    else:
        previous_month_start = today.replace(month=today.month - 1, day=1)
    
    if start_date_arg is None:
        start_input = input(
            f"Start date (YYYY-MM-DD or YYYY-MM) or press Enter for first day of previous month ({previous_month_start}): "
        ).strip()
    else:
        start_input = start_date_arg
        print_stderr(f"Using start date from command line: {start_date_arg}")
    
    if end_date_arg is None:
        end_input = input(
            f"End date (YYYY-MM-DD or YYYY-MM) or press Enter for yesterday ({yesterday}): "
        ).strip()
    else:
        end_input = end_date_arg
        print_stderr(f"Using end date from command line: {end_date_arg}")
    
    if not start_input:
        start_date = datetime.combine(previous_month_start, datetime.min.time())
        print_stderr(f"Start date: {previous_month_start}")
    else:
        start_date_parsed, _ = parse_date_range_string(start_input)
        start_date = datetime.combine(start_date_parsed, datetime.min.time())
        print_stderr(f"Start date: {start_date_parsed}")
    
    if not end_input:
        end_date = datetime.combine(yesterday, datetime.max.time())
        print_stderr(f"End date: {yesterday}")
    else:
        _, end_date_parsed = parse_date_range_string(end_input)
        end_date = datetime.combine(end_date_parsed, datetime.max.time())
        print_stderr(f"End date: {end_date_parsed}")
    
    if start_date > end_date:
        start_date, end_date = end_date, start_date
        print_stderr("⚠ Dates were in reverse order. Swapped automatically.")
    
    date_range = (end_date - start_date).days
    print_stderr(f"\nSelected range: {start_date.date()} to {end_date.date()} ({date_range + 1} days)")
    print_stderr(f"Will be split into {len(get_monthly_chunks(start_date, end_date))} monthly request(s).")
    return start_date, end_date


def get_monthly_chunks(start_date: datetime, end_date: datetime) -> list[tuple[datetime, datetime]]:
    """
    Split a date range into monthly chunks for API requests.
    
    Each chunk represents one month, bounded by the original start and end dates.
    The first and last months may be partial.
    
    Args:
        start_date: Start of the overall range
        end_date: End of the overall range
    
    Returns:
        List of (chunk_start, chunk_end) datetime tuples
    """
    chunks: list[tuple[datetime, datetime]] = []
    current = start_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    while current <= end_date:
        if current.month == 12:
            next_month = current.replace(year=current.year + 1, month=1)
        else:
            next_month = current.replace(month=current.month + 1)
        
        month_end = next_month.replace(day=1) - timedelta(days=1)
        month_end = month_end.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        chunk_start = max(current, start_date)
        chunk_end = min(month_end, end_date)
        
        chunks.append((chunk_start, chunk_end))
        current = next_month
    
    return chunks


def is_full_month(start_date: datetime, end_date: datetime) -> bool:
    """
    Check if a date range covers a complete calendar month.
    
    Args:
        start_date: Start of the range
        end_date: End of the range
    
    Returns:
        True if the range covers the entire month (1st to last day)
    """
    start_is_first = start_date.day == 1
    if end_date.month == 12:
        next_month = end_date.replace(year=end_date.year + 1, month=1, day=1)
    else:
        next_month = end_date.replace(month=end_date.month + 1, day=1)
    last_day_of_month = (next_month - timedelta(days=1)).day
    end_is_last = end_date.day == last_day_of_month
    
    return start_is_first and end_is_last


def generate_filename(start_date: datetime, end_date: datetime, token: str, pod: Optional[str] = None) -> str:
    """
    Generate filename for a data export, optionally with POD prefix, dates, and token suffix.
    
    Format:
    - Full month with POD: POD_YYYY-MM_token_suffix.json
    - Full month without POD: YYYY-MM_token_suffix.json
    - Partial month with POD: POD_YYYYMMDD_YYYYMMDD_token_suffix.json
    - Partial month without POD: YYYYMMDD_YYYYMMDD_token_suffix.json
    
    Args:
        start_date: Start of the date range
        end_date: End of the date range
        token: Normalized token name
        pod: Optional POD (Point of Delivery) identifier for filename prefix
    
    Returns:
        Filename string
    """
    suffix = TOKEN_TO_SUFFIX[token]
    pod_prefix = f"{pod}_" if pod else ""
    
    if is_full_month(start_date, end_date):
        return f"{pod_prefix}{start_date.strftime('%Y-%m')}_{suffix}.json"
    else:
        start_str = start_date.strftime("%Y%m%d")
        end_str = end_date.strftime("%Y%m%d")
        return f"{pod_prefix}{start_str}_{end_str}_{suffix}.json"


def build_request_payload(start_date: datetime, end_date: datetime, magnitude: str) -> str:
    """
    Build the Salesforce Aura API request message as JSON.
    
    Args:
        start_date: Start date for the data query
        end_date: End date for the data query
        magnitude: The magnitude/time-series to query (e.g., 'A+', 'A-', 'RI+')
    
    Returns:
        str: JSON-encoded message action for the Aura API
    """
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    
    return json.dumps({
        "actions": [
            {
                "id": "507;a",
                "descriptor": "apex://PED_CurveDiCaricoController/ACTION$QueryLoadProfile",
                "callingDescriptor": "markup://c:PED_Curva_di_carico_main",
                "params": {
                    "Startdate": start_str,
                    "Enddate": end_str,
                    "Magnitude": magnitude,
                    "isDelegate": False,
                },
                "longRunning": True,
            }
        ]
    })


def build_request(
    credentials: SessionCredentials,
    start_date: datetime,
    end_date: datetime,
    magnitude: str,
) -> PreparedRequest:
    """
    Build HTTP request components for the Salesforce Aura API.
    
    Args:
        credentials: Session credentials for authentication
        start_date: Start date for the query
        end_date: End date for the query
        magnitude: The magnitude/time-series to query (e.g., 'A+', 'A-', 'RI+')
    
    Returns:
        PreparedRequest: URL, form data, headers, and cookies ready to send
    """
    url = (
        "https://private.e-distribuzione.it/PortaleClienti/s/sfsites/aura"
        "?r=35&other.PED_CurveDiCarico.QueryLoadProfile=1"
    )
    
    message_payload = build_request_payload(start_date, end_date, magnitude)
    form_data = {
        "message": message_payload,
        "aura.context": credentials.aura_context,
        "aura.pageURI": "/PortaleClienti/s/curvedicarico",
        "aura.token": credentials.aura_token,
    }
    
    headers = {
        "User-Agent": credentials.user_agent,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en,it;q=0.9,en-US;q=0.8,es;q=0.7,sv;q=0.6",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://private.e-distribuzione.it",
        "Referer": "https://private.e-distribuzione.it/PortaleClienti/s/curvedicarico",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-SFDC-Page-Scope-Id": credentials.page_scope_id,
        "Sec-CH-UA": '"Opera";v="127", "Chromium";v="143", "Not A(Brand";v="24"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
    }
    
    cookies = {
        "sid": credentials.sid,
    }
    
    return PreparedRequest(url=url, data=form_data, headers=headers, cookies=cookies)


def parse_response(response: requests.Response) -> dict[str, Any]:
    """
    Parse and validate the API response.
    
    Args:
        response: HTTP response from the Aura API
    
    Returns:
        dict: Parsed JSON response data
    
    Raises:
        ValueError: If HTTP status indicates error or response is malformed
    """
    if response.status_code == 401:
        raise ValueError(
            "HTTP 401 Unauthorized. Your session credentials may have expired. "
            "Please extract fresh credentials from the browser."
        )
    elif response.status_code == 403:
        raise ValueError(
            "HTTP 403 Forbidden. You may not have permission to access this resource."
        )
    elif response.status_code >= 400:
        raise ValueError(
            f"HTTP {response.status_code} error. Response: {response.text[:200]}"
        )
    
    try:
        data = response.json()
    except json.JSONDecodeError:
        raise ValueError(
            f"Failed to parse response as JSON. Response text: {response.text[:200]}"
        )
    
    if "error" in data:
        raise ValueError(f"Aura error: {data['error']}")
    
    return data


def save_response(
    response_data: dict[str, Any],
    start_date: datetime,
    end_date: datetime,
    token: str,
    use_pod_prefix: bool = True,
) -> Path:
    """
    Save the API response to a JSON file.
    
    Args:
        response_data: The parsed JSON response
        start_date: Start date of the query
        end_date: End date of the query
        token: Normalized token name for filename
        use_pod_prefix: Whether to include POD prefix in filename
    
    Returns:
        Path: Path to the saved file
    """
    pod = None
    if use_pod_prefix:
        try:
            # Extract POD from response: actions[0].returnValue.DD01[0].pod
            pod = response_data.get("actions", [{}])[0].get("returnValue", {}).get("DD01", [{}])[0].get("pod")
        except (IndexError, KeyError, TypeError):
            # POD extraction failed, continue without prefix
            pass
    
    filename = generate_filename(start_date, end_date, token, pod)
    filepath = Path.cwd() / filename
    
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(response_data, f, indent=2, ensure_ascii=False)
    
    return filepath


def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description="Download quarterly load profile data from E-Distribuzione"
    )
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="Start date in YYYY-MM-DD or YYYY-MM format (optional)",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="End date in YYYY-MM-DD or YYYY-MM format (optional)",
    )
    parser.add_argument(
        "--energy-types",
        type=str,
        default="attiva-prelevata,attiva-immessa",
        help="Comma-separated list of tokens to retrieve (default: attiva-prelevata,attiva-immessa). "
             "Valid tokens: attiva-prelevata, attiva-immessa, reattiva-induttiva-prelevata (R1), "
             "reattiva-induttiva-immessa (R2), reattiva-capacitiva-prelevata (R3), "
             "reattiva-capacitiva-immessa (R4)",
    )
    parser.add_argument(
        "--no-pod-prefix",
        action="store_true",
        help="Omit the POD (Point of Delivery) prefix from output filenames",
    )
    args = parser.parse_args()
    
    # Validate command line date arguments
    if args.start is not None:
        try:
            parse_date_range_string(args.start)
        except ValueError as e:
            print_stderr(f"ERROR: Invalid start date: {e}")
            return 1
    
    if args.end is not None:
        try:
            parse_date_range_string(args.end)
        except ValueError as e:
            print_stderr(f"ERROR: Invalid end date: {e}")
            return 1
    
    try:
        tokens = parse_tokenized_arg(args.energy_types)
        print_stderr(f"Selected tokens: {', '.join(sorted(tokens))}")
    except ValueError as e:
        print_stderr(f"ERROR: {e}")
        return 1
    
    try:
        credentials = prompt_for_credentials()
        start_date, end_date = prompt_for_dates(args.start, args.end)
        
        chunks = get_monthly_chunks(start_date, end_date)
        use_pod_prefix = not args.no_pod_prefix
        
        for chunk_start, chunk_end in chunks:
            for token in sorted(tokens):
                magnitude = TOKEN_TO_MAGNITUDE[token]
                prepared = build_request(credentials, chunk_start, chunk_end, magnitude)
                try:
                    response = requests.post(
                        prepared.url,
                        data=prepared.data,
                        headers=prepared.headers,
                        cookies=prepared.cookies,
                        timeout=30,
                        verify=True,
                    )
                except requests.exceptions.ConnectionError as e:
                    raise ValueError(f"Connection error: {e}")
                except requests.exceptions.Timeout:
                    raise ValueError("Request timed out. The server took too long to respond.")
                except requests.exceptions.RequestException as e:
                    raise ValueError(f"Request failed: {e}")
                
                response_data = parse_response(response)
                filepath = save_response(response_data, chunk_start, chunk_end, token, use_pod_prefix)
                print(f"{filepath}")
        
        return 0
        
    except ValueError as e:
        print_stderr(f"ERROR: {e}")
        return 1
    except KeyboardInterrupt:
        print_stderr("\nCancelled by user")
        return 1
    except Exception as e:
        print_stderr(f"ERROR: Unexpected error: {e}")
        return 1


if __name__ == "__main__":
    exit(main())
