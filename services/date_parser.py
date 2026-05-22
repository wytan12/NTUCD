import re
from datetime import datetime

_MONTH_MAP = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'sept': 9, 'oct': 10, 'nov': 11, 'dec': 12,
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'june': 6, 'july': 7, 'august': 8, 'september': 9,
    'october': 10, 'november': 11, 'december': 12,
}


def _parse_time_token(token: str) -> str | None:
    """Parse a single time token and return a canonical uppercase string.

    Accepted examples: '8am' → '8AM', '1030pm' → '10:30PM', '2000' → '8PM'.
    Returns None if the token is not a recognisable time.
    """
    t = token.strip().lower()
    if not t:
        return None

    # am/pm suffix present
    if t.endswith('am') or t.endswith('pm'):
        suffix = t[-2:].upper()
        num = re.sub(r'[:\.]', '', t[:-2])
        if not num.isdigit():
            return None
        if len(num) <= 2:
            h = int(num)
            if not (1 <= h <= 12):
                return None
            return f"{h}{suffix}"
        if len(num) == 3:
            h, m = int(num[0]), num[1:]
        elif len(num) == 4:
            h, m = int(num[:2]), num[2:]
        else:
            return None
        if not (0 <= int(m) <= 59):
            return None
        return f"{h}:{m}{suffix}"

    # 24-hour numeric: 2000, 20:00, 0800
    num = re.sub(r'[:\.]', '', t)
    if num.isdigit() and len(num) == 4:
        h, m = int(num[:2]), int(num[2:])
        if 0 <= h <= 23 and 0 <= m <= 59:
            suffix = 'PM' if h >= 12 else 'AM'
            h12 = h % 12 or 12
            return f"{h12}{suffix}" if m == 0 else f"{h12}:{m:02d}{suffix}"

    return None


def _parse_date_only(day: int, month_str: str, year_str: str | None) -> datetime:
    """Build a datetime from parsed day/month/year components."""
    month = _MONTH_MAP.get(month_str.lower())
    if not month:
        raise ValueError(
            f"❌ Unknown month: `{month_str}`\n"
            "👉 Use abbreviations like `aug`, `nov`, `jan`"
        )
    if not year_str:
        year = datetime.now().year
    elif len(year_str) == 2:
        year = 2000 + int(year_str)
    else:
        year = int(year_str)
    try:
        return datetime(year, month, day)
    except ValueError:
        raise ValueError(f"❌ Invalid date: `{day} {month_str} {year}`")


def parse_date_line(line: str) -> str:
    """Parse one line with a date and zero or more space-separated times.

    Format: DD MON [YY/YYYY] [TIME [TIME ...]]
    Multiple times on the same line are joined with ' / '.

    Examples:
        '28 aug 8am 1030pm'  →  '28 AUG 2026  8AM / 10:30PM'
        '30 aug 1130pm'      →  '30 AUG 2026  11:30PM'
        '31 aug 25'          →  '31 AUG 2025'
    """
    line = line.strip()
    if not line:
        raise ValueError("❌ Empty date line.")

    m = re.match(r'^(\d{1,2})\s*([a-zA-Z]{3,9})\s*(\d{2}|(?:19|20)\d{2})?\s*(.*)', line, re.IGNORECASE)
    if not m:
        raise ValueError(
            f"❌ Cannot parse: `{line}`\n"
            "👉 Start each line with a date, e.g. `28 aug 8am`"
        )

    day_s, month_s, year_s, rest = m.groups()
    dt = _parse_date_only(int(day_s), month_s, year_s)
    date_label = dt.strftime('%d %b %Y').upper()

    if not rest or not rest.strip():
        return date_label

    tokens = re.split(r'[\s,/]+', rest.strip())
    tokens = [t for t in tokens if t]

    times = []
    for token in tokens:
        parsed = _parse_time_token(token)
        if parsed:
            times.append(parsed)
        else:
            raise ValueError(
                f"❌ Cannot parse time: `{token}`\n"
                "👉 Use formats like `8am`, `1030pm`, `2000`"
            )

    if times:
        return f"{date_label}  {' / '.join(times)}"
    return date_label


def parse_flexible_date(date_str: str) -> str:
    """Backward-compatible single date/time parser. Delegates to parse_date_line."""
    return parse_date_line(date_str)


def parse_and_format_dates(dates_str: str) -> list[str]:
    """Parse a multi-line date/time input.

    Each non-empty line: DD MON [YY] [TIME [TIME ...]]
    Multiple times on the same line are separated by spaces and displayed as TIME1 / TIME2.
    Commas within a line are treated as line separators (backward compatibility).

    Returns a list of formatted strings, one per input line.
    Raises ValueError with a user-facing Markdown message on failure.
    """
    entries = []
    for raw_line in dates_str.strip().splitlines():
        for part in raw_line.split(','):
            part = part.strip()
            if part:
                entries.append(part)

    if not entries:
        raise ValueError("❌ No date entered.")

    results = []
    for entry in entries:
        results.append(parse_date_line(entry))

    return results
