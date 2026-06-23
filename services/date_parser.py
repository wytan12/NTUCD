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
    """Parse a single time token and return a canonical format (e.g., '12:30 PM', '12:00 AM')."""
    t = token.strip().lower()
    if not t:
        return None

    # 🎯 FIX 1: Handle pre-formatted colon times that might have an AM/PM suffix attached (e.g., '9:00pm', '10:30am')
    if ('am' in t or 'pm' in t) and ':' in t:
        suffix = 'PM' if 'pm' in t else 'AM'
        cleaned_num = re.sub(r'[a-zA-Z:\.]', '', t) # Extract digits only
        if len(cleaned_num) <= 2:
            return f"{int(cleaned_num)}:00 {suffix}"
        elif len(cleaned_num) >= 3:
            h = int(cleaned_num[:-2])
            m = int(cleaned_num[-2:])
            if 0 <= m <= 59:
                return f"{h}:{m:02d} {suffix}"

    # Handle standard text format tags without colons (e.g., '8am', '1030pm')
    if t.endswith('am') or t.endswith('pm'):
        suffix = t[-2:].upper()
        num = re.sub(r'[:\.]', '', t[:-2])
        if not num.isdigit():
            return None
        
        if len(num) <= 2:
            h = int(num)
            if not (1 <= h <= 12):
                return None
            return f"{h}:00 {suffix}"
        elif len(num) == 3:
            h, m = int(num[0]), num[1:]
        elif len(num) == 4:
            h, m = int(num[:2]), num[2:]
        else:
            return None
        
        if not (0 <= int(m) <= 59):
            return None
        return f"{h}:{int(m):02d} {suffix}"

    # Handle pure numeric 24-hour formats (e.g., '0000', '2000', '1230') or standard colons ('21:00', '9:00')
    num = re.sub(r'[:\.]', '', t)
    if num.isdigit():
        if len(num) == 4:
            h, m = int(num[:2]), int(num[2:])
        elif len(num) == 3:
            h, m = int(num[0]), int(num[1:])
        elif len(num) <= 2:
            h, m = int(num), 0
        else:
            return None

        if 0 <= h <= 23 and 0 <= m <= 59:
            suffix = 'PM' if h >= 12 else 'AM'
            h12 = h % 12 or 12
            return f"{h12}:{m:02d} {suffix}"

    return None


def _parse_date_only(day: int, month_str: str, year_str: str | None) -> datetime:
    """Build a datetime object applying contextual smart overrolling constraints 
    and strict n-1 to n+3 year window safety boundaries.
    """
    month = _MONTH_MAP.get(month_str.lower())
    if not month:
        raise ValueError(
            f"❌ Unknown month: `{month_str}`\n"
            "👉 Use abbreviations like `jan`, `feb`, `jul`\n\n"

            "Please re-enter!"
        )
        
    now = datetime.now()  # Current time context: May 2026
    
    if not year_str:
        # 🧠 Smart Roll-forward Engine:
        # 1. If the input month is strictly in the past (e.g., input Jan when now is May)
        # 2. OR if it's the current month but the day has already passed (e.g., input May 7 when now is May 25)
        if month < now.month or (month == now.month and day < now.day):
            year = now.year + 1
        else:
            year = now.year
    else:
        if len(year_str) == 2:
            year = 2000 + int(year_str)
        else:
            year = int(year_str)
            
    # Strict Boundary Check: (n-1 <= year <= n+3) where n=2026 -> 2025 to 2029
    if not (2025 <= year <= 2029):
        raise ValueError(
            f"❌ Year error: `{year}` falls outside the allowed window.\n"
            "👉 Allowed year window is 2025 to 2029.\n\n"

            "Please re-enter!"
        )
        
    try:
        return datetime(year, month, day)
    except ValueError:
        raise ValueError(f"❌ Invalid date fields parsed: `{day} {month_str} {year}`")


def parse_date_line(line: str) -> str:
    """Parse one line matching strict comma separation structures or internal processed formats."""
    # 🎯 STEP 1: MOBILE COPY-PASTE SPACE SANITIZER
    line = line.replace('\xa0', ' ')
    line = re.sub(r'\s+,\s*', ', ', line)
    line = re.sub(r' +', ' ', line)
    line = line.strip()

    if not line:
        raise ValueError("❌ Empty date line.")

    # 🎯 STEP 2: IDENTIFY DIVIDERS SAFELY
    if ',' in line:
        parts = line.split(',', 1)
        date_part = parts[0].strip()
        time_part = parts[1].strip() if len(parts) > 1 else ""
    else:
        # 🎯 ENFORCE THE RULE STRICTLY:
        # If there is NO comma, we ONLY look for a time split if it contains a colon (like 9:00) 
        # or ends with a clear 12h/24h time format suffix (like 9pm, 11am, 2300).
        # Otherwise, the entire string is treated as a pure shorthand date (e.g., "10oct", "10 oct")!
        time_match = re.search(r'(\d{1,2}:\d{2}|\b\d{1,2}\s*(?:am|pm)\b)', line, re.IGNORECASE)
        
        if time_match:
            split_idx = time_match.start()
            date_part = line[:split_idx].strip()
            time_part = line[split_idx:].strip()
        else:
            # No comma and no explicit time signature found -> It's a 100% pure date entry!
            date_part = line
            time_part = ""

    # Collapse spacing inside the date component for clean regex matching
    date_part = re.sub(r'\s+', ' ', date_part)

    # 3. Extract components from Date Part using robust spacing verification
    # Allows shorthand strings like "10oct" or "10 oct" completely safely now!
    m = re.match(r'^(\d{1,2})\s*([a-zA-Z]{3,9})(?:\s*(\d{2}|\d{4}))?$', date_part)
    if not m:
        raise ValueError(
            f"❌ Invalid format: `{date_part}`. Please re-enter.\n"
            "👉 Make sure to use the correct format oh!\n\n"
            "*Examples:*\n"
            "Single date Single time: `31 aug, 9pm`\n"
            "Single date Multiple times: `1 sep, 7pm 9pm`\n"
            "Multiple dates Single/Multiple times:\n"
            "`31 aug, 9pm`\n"
            "`1 sep, 7pm 9pm`\n\n"
            "Separate the date and time using a *comma (,)*"
        )

    day_s, month_s, year_s = m.groups()
    dt = _parse_date_only(int(day_s), month_s, year_s)
    date_label = dt.strftime('%d %b %Y')

    if not time_part:
        return date_label

    # 4. Process time component tokens safely
    time_part_cleaned = re.sub(r'[\.]', ' ', time_part)
    time_part_cleaned = time_part_cleaned.replace('/', ' ')
    
    # Combine tokens back with their adjacent AM/PM string if they were split by spaces
    time_part_cleaned = re.sub(r'\s+(am|pm)\b', r'\1', time_part_cleaned, flags=re.IGNORECASE)
    tokens = [t for t in re.split(r'[\s]+', time_part_cleaned) if t]

    times = []
    for token in tokens:
        if token.lower() in ['am', 'pm', '/']:
            continue
        parsed = _parse_time_token(token)
        if parsed:
            times.append(parsed)
        else:
            if re.match(r'^\d{1,2}:\d{2}$', token):
                continue
            raise ValueError(
                f"❌ IDK what time you wan lar: `{token}` is semo?\n"
                "👉 Use formatting like `8am`, `10:30pm`, `0000`, `1230`\n\n"
                "Please re-enter!"
            )

    if times:
        return f"{date_label}  {' / '.join(times)}"
    return date_label


def parse_flexible_date(date_str: str) -> str:
    """Backward-compatible single date/time parser."""
    return parse_date_line(date_str)


def parse_and_format_dates(dates_str: str) -> list[str]:
    """Parse a multi-line date/time input block."""
    results = []
    for raw_line in dates_str.strip().splitlines():
        if raw_line.strip():
            results.append(parse_date_line(raw_line))
    if not results:
        raise ValueError("❌ No date entered.")
    return results
