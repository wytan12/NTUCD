import re
import json
import time
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from config import (GOOGLE_CREDENTIALS_JSON, SHEET_NAME, SHEET_TAB_NAME,
                   ATTENDANCE_TAB, WELCOME_TEA_SHEET, WELCOME_TEA_TAB,
                   WELCOME_TEA_ID_TAB, MEMBER_INFO_TAB,
                   ATT_NAME_COL, ATT_TOTAL_COL, ATT_LABEL_COL, ATT_FIRST_DATE_COL,
                   ATT_POLL_ROW, ATT_DATE_ROW, ATT_FIRST_MEMBER_ROW, sg_tz,
                   PERF_TAB_NAME, PERF_NAME_COL, PERF_TOTAL_COL, PERF_FIRST_EVENT_COL,
                   PERF_THREAD_ROW, PERF_EVENT_ROW, PERF_FIRST_MEMBER_ROW)
from utils.constants import OTHERS_THREAD_IDS

# Cached OAuth client + opened spreadsheet handles. Re-authenticating and
# re-opening the spreadsheet on every call costs ~3 network round-trips; caching
# them means we pay that once. gspread refreshes the OAuth token automatically,
# so the cached client stays valid for the life of the process.
_client = None
_spreadsheets = {}

def _get_client():
    """Return a cached authorized gspread client, creating it on first use."""
    global _client
    if _client is None:
        scope = ["https://spreadsheets.google.com/feeds",
                 "https://www.googleapis.com/auth/drive"]
        # Handle both string (Heroku) and dict (local testing) formats
        if isinstance(GOOGLE_CREDENTIALS_JSON, str):
            creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        else:
            creds_dict = GOOGLE_CREDENTIALS_JSON
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        _client = gspread.authorize(creds)
    return _client

def get_gspread_sheet(sheet_name=SHEET_NAME, tab_name=SHEET_TAB_NAME):
    """Return a gspread Worksheet object for the given sheet and tab.

    The authorized client and opened spreadsheet are cached at module level, so
    repeated calls only cost a single worksheet lookup instead of a fresh OAuth
    handshake + spreadsheet open each time.
    """
    if sheet_name not in _spreadsheets:
        _spreadsheets[sheet_name] = _get_client().open(sheet_name)
    return _spreadsheets[sheet_name].worksheet(tab_name)

def get_attendance_ws():
    """Get attendance worksheet (the ATTENDANCE_TAB tab inside the main sheet)."""
    return get_gspread_sheet(tab_name=ATTENDANCE_TAB)

# ---- Smart data cache: Drive modifiedTime freshness check -------------------
# Each entry is keyed by (sheet_name, tab_name) and stores the fetched data plus
# the spreadsheet's Drive `modifiedTime` at fetch time. Before serving, we ask
# Google only for that timestamp (a tiny request — NOT the cell contents):
#   • unchanged  -> serve the in-memory copy instantly (no full download)
#   • changed    -> someone edited the sheet, so re-download and re-stamp
#   • force=True -> always re-download (manual Refresh)
# `modifiedTime` is a per-FILE property, so an edit to ANY tab bumps it and
# refreshes every tab's cache for that spreadsheet — conservative but always
# correct. Bot writes also bump it, so the cache self-heals after any update.
_records_cache = {}
_values_cache = {}
_modified_time_cache = {}
_MODIFIED_TIME_TTL_SECONDS = 30

def _spreadsheet_modified_time(sheet_name, force=False):
    """Return the spreadsheet's Drive modifiedTime string, or None on error."""
    now = time.monotonic()
    cached = _modified_time_cache.get(sheet_name)
    if not force and cached and now - cached["checked_at"] < _MODIFIED_TIME_TTL_SECONDS:
        return cached["stamp"]

    try:
        if sheet_name not in _spreadsheets:
            _spreadsheets[sheet_name] = _get_client().open(sheet_name)
        stamp = _spreadsheets[sheet_name].get_lastUpdateTime()
        _modified_time_cache[sheet_name] = {"stamp": stamp, "checked_at": now}
        return stamp
    except Exception as e:
        print(f"[WARN] modifiedTime check failed for '{sheet_name}': {e}")
        return None

def get_cached_records(sheet_name=SHEET_NAME, tab_name=SHEET_TAB_NAME, force=False):
    """`get_all_records()` guarded by a Drive modifiedTime freshness check.

    Serves the cached rows while the sheet is unchanged; re-downloads only when
    Google reports the file changed (someone edited it) or when ``force=True``.
    """
    key = (sheet_name, tab_name)
    cached = _records_cache.get(key)
    stamp = None if force else _spreadsheet_modified_time(sheet_name)
    if cached is not None and stamp is not None and cached["stamp"] == stamp:
        return cached["records"]
    records = get_gspread_sheet(sheet_name, tab_name).get_all_records()
    if stamp is None:  # forced, or the pre-check failed — stamp it now
        stamp = _spreadsheet_modified_time(sheet_name, force=True)
    _records_cache[key] = {"records": records, "stamp": stamp}
    return records

def get_cached_values(sheet_name=SHEET_NAME, tab_name=SHEET_TAB_NAME, force=False):
    """`get_all_values()` guarded by the same modifiedTime freshness check."""
    key = (sheet_name, tab_name)
    cached = _values_cache.get(key)
    stamp = None if force else _spreadsheet_modified_time(sheet_name)
    if cached is not None and stamp is not None and cached["stamp"] == stamp:
        return cached["values"]
    values = get_gspread_sheet(sheet_name, tab_name).get_all_values()
    if stamp is None:
        stamp = _spreadsheet_modified_time(sheet_name, force=True)
    _values_cache[key] = {"values": values, "stamp": stamp}
    return values

def invalidate_sheet_cache(sheet_name=None, tab_name=None):
    """Drop cached snapshots so the next read re-downloads (manual Refresh)."""
    if sheet_name is None and tab_name is not None:
        sheet_name = SHEET_NAME

    if sheet_name is None:
        _modified_time_cache.clear()
    else:
        _modified_time_cache.pop(sheet_name, None)

    for cache in (_records_cache, _values_cache):
        if sheet_name is None:
            cache.clear()
        else:
            for k in list(cache):
                if k[0] == sheet_name and (tab_name is None or k[1] == tab_name):
                    cache.pop(k, None)

def append_to_others_list(thread_id, event_name: str = ""):
    """Append thread ID and event name to the OTHERS tab in the main sheet."""
    try:
        from config import SHEET_NAME
        sheet = get_gspread_sheet(sheet_name=SHEET_NAME, tab_name="OTHERS")
        sheet.append_row([str(thread_id), event_name], value_input_option="USER_ENTERED")
        invalidate_sheet_cache(tab_name="OTHERS")
        print(f"[INFO] Thread ID {thread_id} ({event_name}) logged to OTHERS tab.")
        from utils.constants import OTHERS_THREAD_IDS
        OTHERS_THREAD_IDS.add(int(thread_id))
    except Exception as e:
        print(f"[ERROR] Failed to write Thread ID {thread_id} to OTHERS tab: {str(e)}")

WELCOME_TEA_MEMBER_FIELDS = (
    "Full Name (as per Matric Card)",
    "Nickname",
    "Gender",
    "Nationality",
    "Matric No",
    "School",
    "Course",
    "Year",
    "Contact",
    "NTU Email",
    "Date of Birth",
)


def _normalized_record(record):
    """Return a case-insensitive header map while preserving cell values."""
    return {str(key).strip().lower(): value for key, value in record.items()}


def get_welcome_tea_registration(matric_number: str):
    """Return the latest 2026 Welcome Tea response matching `Matric No`."""
    target = (matric_number or "").strip().upper()
    if not target:
        return None

    rows = get_gspread_sheet(WELCOME_TEA_SHEET, WELCOME_TEA_TAB).get_all_records()
    for row in reversed(rows):
        normalized = _normalized_record(row)
        if str(normalized.get("matric no", "")).strip().upper() == target:
            return row
    return None


def matric_valid(matric_number: str) -> bool:
    """True when `Matric No` exists in the 2026 Welcome Tea response sheet."""
    return get_welcome_tea_registration(matric_number) is not None


def sync_welcome_tea_member(matric_number: str, telegram_user_id: int):
    """Upsert the latest Welcome Tea response into MEMBER INFO by Tele ID.

    All named fields are matched by header, so column order may differ between
    the two sheets. If multiple MEMBER INFO rows contain the same Tele ID, the
    first is refreshed and duplicate rows are cleared of the copied data and
    Tele ID so verification leaves one authoritative member row.
    Returns (ok, info).
    """
    from config import MEMBER_INFO_TAB
    from datetime import datetime
    from gspread.utils import rowcol_to_a1

    source = get_welcome_tea_registration(matric_number)
    if source is None:
        return False, "matric_not_found"

    source_by_header = _normalized_record(source)
    ws = get_gspread_sheet(tab_name=MEMBER_INFO_TAB)
    values = ws.get_all_values()
    _, cols = _member_info_cols(ws)

    tele_c = cols.get("tele id")
    name_c = cols.get("full name (as per matric card)")
    matric_c = cols.get("matric no")
    if not tele_c or not name_c or not matric_c:
        return False, "member_info_missing_required_columns"

    missing_fields = [field for field in WELCOME_TEA_MEMBER_FIELDS
                      if field.lower() not in source_by_header or field.lower() not in cols]
    if missing_fields:
        print(f"[VERIFY][WARN] Header mismatch for fields: {missing_fields}")
        return False, "header_mismatch"

    def cell(row_number, col_number):
        row = values[row_number - 1] if row_number - 1 < len(values) else []
        return row[col_number - 1].strip() if col_number - 1 < len(row) else ""

    matching_rows = [
        row_number for row_number in range(2, len(values) + 1)
        if cell(row_number, tele_c) == str(telegram_user_id)
    ]
    matric_rows = [
        row_number for row_number in range(2, len(values) + 1)
        if cell(row_number, matric_c).upper() == (matric_number or "").strip().upper()
    ]
    if matching_rows:
        target_row = matching_rows[0]
    elif matric_rows:
        target_row = matric_rows[0]
    else:
        target_row = next(
            (row_number for row_number in range(2, max(len(values) + 1, 101))
             if not cell(row_number, name_c) and not cell(row_number, tele_c)),
            None,
        )
    if target_row is None:
        return False, "no_empty_member_row"

    updates = []
    for field in WELCOME_TEA_MEMBER_FIELDS:
        updates.append({
            "range": rowcol_to_a1(target_row, cols[field.lower()]),
            "values": [[source_by_header[field.lower()]]],
        })
    updates.append({"range": rowcol_to_a1(target_row, tele_c), "values": [[str(telegram_user_id)]]})

    status_c = cols.get("status")
    join_c = cols.get("join date")
    leave_c = cols.get("leave date")
    if status_c:
        updates.append({"range": rowcol_to_a1(target_row, status_c), "values": [["Join"]]})
    if join_c:
        today = datetime.now(sg_tz).strftime("%d %b %Y %H:%M")
        updates.append({"range": rowcol_to_a1(target_row, join_c), "values": [[today]]})
    if leave_c:
        updates.append({"range": rowcol_to_a1(target_row, leave_c), "values": [[""]]})

    # Consolidate duplicate Tele ID rows without touching formulas or unrelated columns.
    duplicate_rows = sorted(set(matching_rows + matric_rows) - {target_row})
    for duplicate_row in duplicate_rows:
        for field in WELCOME_TEA_MEMBER_FIELDS:
            updates.append({
                "range": rowcol_to_a1(duplicate_row, cols[field.lower()]),
                "values": [[""]],
            })
        updates.append({"range": rowcol_to_a1(duplicate_row, tele_c), "values": [[""]]})
        if status_c:
            updates.append({"range": rowcol_to_a1(duplicate_row, status_c), "values": [[""]]})
        if join_c:
            updates.append({"range": rowcol_to_a1(duplicate_row, join_c), "values": [[""]]})
        if leave_c:
            updates.append({"range": rowcol_to_a1(duplicate_row, leave_c), "values": [[""]]})

    ws.batch_update(updates, value_input_option="USER_ENTERED")
    invalidate_sheet_cache(tab_name=MEMBER_INFO_TAB)
    print(f"[VERIFY] Synced {matric_number} / Tele ID {telegram_user_id} into MEMBER INFO row {target_row}.")
    return True, "updated_existing" if (matching_rows or matric_rows) else "created"


def update_user_id_in_sheet(matric_number: str, telegram_user_id: int):
    """Legacy wrapper for the new header-matched MEMBER INFO synchronization."""
    return sync_welcome_tea_member(matric_number, telegram_user_id)


def copy_user_to_timeline(welcome_row: dict, telegram_user_id: int):
    """Legacy compatibility wrapper using the response row's Matric No."""
    matric = _normalized_record(welcome_row).get("matric no", "")
    return sync_welcome_tea_member(str(matric), telegram_user_id)

def user_already_in_timeline(user_id: int) -> bool:
    """True if the user's Tele ID already has a row in the MEMBER INFO tab."""
    from config import MEMBER_INFO_TAB
    values = get_cached_values(tab_name=MEMBER_INFO_TAB)
    if not values:
        return False
    header = [h.strip().lower() for h in values[0]]
    try:
        tele_i = header.index("tele id")
    except ValueError:
        print("[WARN] 'Tele ID' column not found in member info tab.")
        return False
    return any(tele_i < len(r) and r[tele_i].strip() == str(user_id)
               for r in values[1:])

def mark_user_left_in_sheet(user_id: int) -> bool:
    """Mark the user as Left in the MEMBER INFO tab (legacy-named wrapper).

    Previously wrote to the separate "PERFORMER Info" spreadsheet; now
    delegates to `update_member_leave_in_info` (Leave Date = today,
    Status = Left, row matched by Tele ID).
    """
    return update_member_leave_in_info(user_id)

# ===================== ATTENDANCE [REG] =====================
# Layout (see config.py):
#   Col A = NAME (nickname)  | Col B = TOTAL | Col C = row labels
#   Cols D.. = one training date each; row 1 = poll id, row 2 = date, rows 3+ = "1"/blank


def ensure_attendance_headers(ws=None):
    """Write the fixed header/label cells if they're missing. Idempotent."""
    ws = ws or get_attendance_ws()
    ws.batch_update([
        {"range": "A2", "values": [["NAME"]]},
        {"range": "B2", "values": [["Tabulation"]]},
        {"range": "C1", "values": [["POLL ID"]]},
        {"range": "C2", "values": [["TRAINING DATE [REG]"]]},
    ])
    invalidate_sheet_cache(tab_name=ATTENDANCE_TAB)


def parse_sheet_date(s):
    """Parse a training-date cell into a datetime.date, or None if unparseable.

    Accepts d/m, d/m/yy, d/m/yyyy, '12 Aug', '12 Aug 2025', '12 August', etc.
    When the year is omitted, the current Singapore year is assumed.
    """
    s = (s or "").strip()
    if not s:
        return None
    from datetime import datetime
    this_year = datetime.now(sg_tz).year
    fmts = ["%d/%m/%Y", "%d/%m/%y", "%d/%m", "%d-%m-%Y", "%d-%m-%y", "%d-%m",
            "%d %b %Y", "%d %b", "%d %B %Y", "%d %B"]
    for f in fmts:
        try:
            dt = datetime.strptime(s, f)
            if "%Y" not in f and "%y" not in f:
                dt = dt.replace(year=this_year)
            return dt.date()
        except ValueError:
            continue
    return None


def get_training_date_columns():
    """Return [(col_index, date_str, present_count), ...] for polled training dates.

    Only columns with BOTH a date (row 2) and a poll id (row 1) are returned, so
    the attendance menus list just the dates that have actually been polled.
    Un-polled prefilled dates and any analysis column placed to the right of the
    date region (e.g. a 'Tabulation' header) have no poll id, so they're excluded.

    `present_count` is how many members are marked "1" in that column. The list
    is sorted by date with the **latest date first** (unparseable dates sink to
    the bottom).
    """
    from datetime import date as _date

    ws = get_attendance_ws()
    values = ws.get_all_values()
    row1 = values[ATT_POLL_ROW - 1] if len(values) >= ATT_POLL_ROW else []
    row2 = values[ATT_DATE_ROW - 1] if len(values) >= ATT_DATE_ROW else []

    out = []
    for col in range(ATT_FIRST_DATE_COL, len(row2) + 1):
        date_val = (row2[col - 1] or "").strip()
        poll_val = (row1[col - 1] or "").strip() if col - 1 < len(row1) else ""
        if not (date_val and poll_val):
            continue
        present = 0
        for r in range(ATT_FIRST_MEMBER_ROW, len(values) + 1):
            row = values[r - 1]
            if col - 1 < len(row) and (row[col - 1] or "").strip() == "1":
                present += 1
        out.append((col, date_val, present))

    out.sort(key=lambda item: parse_sheet_date(item[1]) or _date.min, reverse=True)
    return out


def format_training_poll_ref(poll_id, message_id=None):
    if not poll_id:
        return ""
    return f"{poll_id}|{message_id}" if message_id else str(poll_id)


def _parse_training_poll_ref(value):
    text = str(value or "").strip()
    if not text:
        return "", None
    poll_id, _, message_id = text.partition("|")
    message_id = message_id.strip()
    return poll_id.strip(), int(message_id) if message_id.isdigit() else None


def get_training_poll_ref(col):
    """Return (poll_id, message_id) stored in row 1 for a training-date column."""
    try:
        row1 = get_attendance_ws().row_values(ATT_POLL_ROW)
        value = row1[col - 1] if col - 1 < len(row1) else ""
        return _parse_training_poll_ref(value)
    except Exception as e:
        print(f"[ERROR] get_training_poll_ref failed for col {col}: {e}")
        return "", None


def record_training_poll(poll_id, training_date, poll_message_id=None) -> int:
    """Store a poll id in row 1 under the column matching `training_date` (a date).

    If no column matches, a new date column is appended to the right.
    Returns the column index used.
    """
    ws = get_attendance_ws()
    row2 = ws.row_values(ATT_DATE_ROW)

    # Scan from col D: reuse a column whose date already matches; otherwise
    # remember the first EMPTY slot. We deliberately do NOT use len(row2)+1,
    # because unrelated columns to the right (e.g. a "Tabulation" column) would
    # otherwise push new dates far past the date region.
    target_col = None
    first_empty = None
    for col in range(ATT_FIRST_DATE_COL, len(row2) + 1):
        cell = (row2[col - 1] or "").strip()
        if not cell:
            if first_empty is None:
                first_empty = col
            continue
        d = parse_sheet_date(cell)
        if d and d == training_date:
            target_col = col
            break

    if target_col is None:
        target_col = first_empty or max(len(row2) + 1, ATT_FIRST_DATE_COL)
        ws.update_cell(ATT_DATE_ROW, target_col, f"{training_date.day}/{training_date.month}")

    ws.update_cell(ATT_POLL_ROW, target_col, format_training_poll_ref(poll_id, poll_message_id))
    invalidate_sheet_cache(tab_name=ATTENDANCE_TAB)
    return target_col


def replace_training_date_column(col, new_date_str, poll_id=None, poll_message_id=None):
    """Replace an existing training-date column in place.

    Writes `new_date_str` into row 2 of `col`, and clears every member mark in
    that column (the old marks belonged to the old date, so a date change starts
    the column fresh). Row 1 (the poll id) is set to `poll_id` if one is given,
    otherwise it is CLEARED — so `auto_poll_check` will post the poll itself once
    the new date is 2 days away (same rule as every other date). The TOTAL column
    (B) is a sheet formula and is never touched. Done in a single batch_update.
    """
    from gspread.utils import rowcol_to_a1

    ws = get_attendance_ws()
    values = ws.get_all_values()
    last_row = len(values)

    requests = [
        {"range": rowcol_to_a1(ATT_DATE_ROW, col), "values": [[new_date_str]]},
        {"range": rowcol_to_a1(ATT_POLL_ROW, col), "values": [[format_training_poll_ref(poll_id, poll_message_id)]]},
    ]
    if last_row >= ATT_FIRST_MEMBER_ROW:
        c_start = rowcol_to_a1(ATT_FIRST_MEMBER_ROW, col)
        c_end = rowcol_to_a1(last_row, col)
        cleared = [[""] for _ in range(ATT_FIRST_MEMBER_ROW, last_row + 1)]
        requests.append({"range": f"{c_start}:{c_end}", "values": cleared})

    ws.batch_update(requests, value_input_option="USER_ENTERED")
    invalidate_sheet_cache(tab_name=ATTENDANCE_TAB)


def is_training_poll_id(poll_id) -> bool:
    """True if `poll_id` is recorded in the attendance poll-id row (survives restarts)."""
    try:
        ws = get_attendance_ws()
        row1 = ws.row_values(ATT_POLL_ROW)
        return any(_parse_training_poll_ref(v)[0] == str(poll_id) for v in row1)
    except Exception as e:
        print(f"[ERROR] is_training_poll_id failed: {e}")
        return False


# Sentinel "year" for the Graduates group (Year cell == "-") — sorts above Year 4.
GRADUATE_YEAR = 99

def _classify_member_group(year_raw: str):
    """Map a `Year` cell to (sort_key:int, token:str, label:str).

    - "-"               → 🎩 Graduates (top)
    - contains a number → 🎓 Year N (first number extracted, e.g. "postgraduate/1" → 1)
    - other text        → its own named group, e.g. "exchange" → 🌏 Exchange (below years)
    - blank             → 🎓 Year — (bottom)
    `token` is a short, pipe-free id used in the drill-down callback. Sort order
    (desc): Graduates (99) > Year N > named groups (0) > blank (-1).
    """
    raw = (year_raw or "").strip()
    raw_lower = raw.lower()

    if raw == "-":
        return (99, "Y99", "🎩 Graduates")

    if raw_lower == "exchange":
        return (98, "Gexchange", "🌏 Exchange")

    m = re.search(r"\d+", raw)
    if m:
        y = int(m.group())
        return (y, f"Y{y}", f"🎓 Year {y}")

    if raw:
        slug = re.sub(r"[^a-z0-9]+", "", raw_lower)[:24] or "grp"
        return (0, f"G{slug}", f"🌏 {raw.title()}")

    return (-1, "Ynone", "❓ Unassigned")


def get_active_members():
    """Return active members from the MEMBER INFO tab, most senior first.

    Reads the `MEMBER INFO AY26/27` tab (smart-cached), keeps rows whose
    `Status` is `Active`/`Join`, and returns
    `[(sort_key:int, group_token:str, group_label:str, display_name:str), ...]`
    sorted by group seniority descending, then label, then name. Grouping is by
    `_classify_member_group` (Graduates / Year N / named text group like
    "Exchange" / blank "Year —"). Display name = Nickname, falling back to Full
    Name. Header lookup is index-based and case-insensitive.
    """
    from config import MEMBER_INFO_TAB
    values = get_cached_values(tab_name=MEMBER_INFO_TAB)
    if not values:
        return []
    header = [h.strip().lower() for h in values[0]]

    def col_idx(name):
        try:
            return header.index(name)
        except ValueError:
            return None

    name_i = col_idx("nickname")
    full_i = col_idx("full name (as per matric card)")
    year_i = col_idx("year")
    status_i = col_idx("status")
    if status_i is None:
        print("[WARN] 'Status' column not found in member info tab.")
        return []

    def cell(row, i):
        return row[i].strip() if (i is not None and i < len(row)) else ""

    members = []
    for row in values[1:]:
        # "Active" (manual) and "Join" (bot-stamped on join/auto-approve) both
        # count as active members; "Left" / blank are excluded.
        status = cell(row, status_i).lower().strip()

        if status not in ("active", "join"):
            continue
        name = cell(row, name_i) or cell(row, full_i)
        if not name:
            continue
        sort_key, token, label = _classify_member_group(cell(row, year_i))
        members.append((sort_key, token, label, name))

    members.sort(key=lambda m: (-m[0], m[2].lower(), m[3].lower()))
    return members


# ---- Role-based admin tiers (from MEMBER INFO Role/Position + Tele ID) ------
# MAIN admins get dashboard access AND every admin alert/reminder DM.
# SECONDARY admins get dashboard access only — no alert/reminder DMs (they
# still see anything broadcast to the whole group, like everyone else).
# Keyword lists live in config.py.
from config import MAIN_ADMIN_ROLE_KEYWORDS, SECONDARY_ADMIN_ROLE_KEYWORDS


def _config_admin_ids():
    """Hardcoded fallback ids from config.ADMIN_DM_USER_IDS (never lock out)."""
    from config import ADMIN_DM_USER_IDS
    ids = set()
    if isinstance(ADMIN_DM_USER_IDS, dict):
        for k, v in ADMIN_DM_USER_IDS.items():
            if str(k).isdigit():
                ids.add(int(k))
            if str(v).isdigit():
                ids.add(int(v))
    elif isinstance(ADMIN_DM_USER_IDS, (list, set, tuple)):
        for x in ADMIN_DM_USER_IDS:
            if str(x).isdigit():
                ids.add(int(x))
    return ids


def get_admin_role_ids(force=False):
    """Return (main_ids, secondary_ids) — Tele IDs from the MEMBER INFO tab
    whose Role/Position contains a tier keyword (case-insensitive).

    Reads through the smart cache, so it's a tiny check per call. Returns two
    empty sets on any sheet failure (callers fall back to config ids).
    """
    from config import MEMBER_INFO_TAB
    main, secondary = set(), set()
    try:
        values = get_cached_values(tab_name=MEMBER_INFO_TAB, force=force)
        if not values:
            return main, secondary
        header = [h.strip().lower() for h in values[0]]
        try:
            role_i = header.index("role/position")
            tele_i = header.index("tele id")
        except ValueError:
            print("[ADMIN][WARN] Role/Position or Tele ID column missing in member info tab.")
            return main, secondary
        for row in values[1:]:
            role = row[role_i].strip().lower() if role_i < len(row) else ""
            tid = row[tele_i].strip() if tele_i < len(row) else ""
            if not role or not tid.isdigit():
                continue
            if any(k in role for k in MAIN_ADMIN_ROLE_KEYWORDS):
                main.add(int(tid))
            elif any(k in role for k in SECONDARY_ADMIN_ROLE_KEYWORDS):
                secondary.add(int(tid))
    except Exception as e:
        print(f"[ADMIN][WARN] Role lookup failed: {e}")
    return main, secondary


def get_alert_admin_ids(force=False):
    """Tele IDs that receive admin alert/reminder DMs: MAIN admins only.
    Falls back to config.ADMIN_DM_USER_IDS if no main admin is resolvable."""
    main, _ = get_admin_role_ids(force=force)
    return main or _config_admin_ids()


def get_dashboard_admin_ids(force=False):
    """Tele IDs allowed to use the DM dashboard: MAIN + SECONDARY admins only.

    The config ADMIN_DM_USER_IDS ids are used **only as an emergency fallback**
    when the role lookup yields nothing at all (sheet unreachable / Role column
    wiped) — they are NOT granted access alongside the role-holders, so a plain
    member listed in the old config set cannot reach the dashboard."""
    main, secondary = get_admin_role_ids(force=force)
    role_ids = main | secondary
    return role_ids if role_ids else _config_admin_ids()


# Throttle for force=True role checks: at most one real MEMBER INFO download
# per this many seconds, no matter how fast admins click through the Cockpit.
# Lower = fresher role checks on major clicks but more ~1s download pauses
# during navigation. 2s = max 30 forced reads/min against the 60/min API
# limit — safe. Do NOT go below 2s: click bursts could 429 the whole bot.
_ROLE_FORCE_THROTTLE_SECONDS = 30
_last_role_force_at = 0.0


def _get_user_role(user_id: int, force: bool = False) -> str | None:
    """Return "main", "secondary", or None for this Tele ID.

    Re-derived from the sheet data on EVERY call (no per-user answer cache).

    force=False (in-task actions: toggles, wizard steps, field edits) — scans
    the smart-cached MEMBER INFO copy in memory. Instant; freshness rides on
    the Drive modifiedTime check, which can lag a minute or two behind web-UI
    edits because Google bumps that timestamp lazily.

    force=True (MAJOR clicks: /start, /threadid, Cockpit `DASH_VIEW|` navigation,
    ♻️ Refresh) — re-downloads the MEMBER INFO tab right now, bypassing the
    timestamp, so a role granted/removed in the web UI takes effect on the very
    next major click. Throttled to one real download per
    `_ROLE_FORCE_THROTTLE_SECONDS` so rapid navigation can't spam the API; the
    fresh copy lands in the shared cache, so in-task checks benefit too.
    """
    global _last_role_force_at
    from config import MEMBER_INFO_TAB
    uid = int(user_id)
    try:
        do_force = False
        if force:
            now = time.monotonic()
            if now - _last_role_force_at >= _ROLE_FORCE_THROTTLE_SECONDS:
                _last_role_force_at = now
                do_force = True
        values = get_cached_values(tab_name=MEMBER_INFO_TAB, force=do_force)
        if not values:
            return None
        header = [h.strip().lower() for h in values[0]]
        try:
            role_i = header.index("role/position")
            tele_i = header.index("tele id")
        except ValueError:
            print("[ADMIN][WARN] Role/Position or Tele ID column not found in MEMBER INFO.")
            return None
        role = None
        for row in values[1:]:
            tid = row[tele_i].strip() if tele_i < len(row) else ""
            if not tid.isdigit() or int(tid) != uid:
                continue
            r = row[role_i].strip().lower() if role_i < len(row) else ""
            if any(k in r for k in MAIN_ADMIN_ROLE_KEYWORDS):
                role = "main"
            elif any(k in r for k in SECONDARY_ADMIN_ROLE_KEYWORDS):
                role = "secondary"
            break  # found the user's row — stop scanning
        return role
    except Exception as e:
        print(f"[ADMIN][WARN] Role lookup failed for {user_id}: {e}")
        return None


def is_dashboard_admin(user_id, force=False) -> bool:
    """True if `user_id` may use the admin DM dashboard (MAIN or SECONDARY tier).

    The role is re-checked against the sheet data on every call. Pass
    force=True on MAJOR interactions (/start, Cockpit navigation) to re-download
    the sheet live so web-UI role edits apply on that very click; leave
    force=False for in-task actions so flows stay instant (see _get_user_role).
    Falls back to config.ADMIN_DM_USER_IDS only when MEMBER INFO is unreachable.
    """
    try:
        uid = int(user_id)
        role = _get_user_role(uid, force=force)
        if role in ("main", "secondary"):
            return True
        # Emergency fallback: config ids used only when sheet is unreachable
        # (role would be None due to exception, not because no row matched)
        if uid in _config_admin_ids() and not get_cached_values(tab_name=MEMBER_INFO_TAB):
            return True
        return False
    except (TypeError, ValueError):
        return False


def is_main_admin(user_id, force=False) -> bool:
    """True if `user_id` has a MAIN admin role."""
    try:
        return _get_user_role(int(user_id), force=force) == "main"
    except (TypeError, ValueError):
        return False


def get_join_contact_admin_id():
    """Tele ID of the preferred "contact our admin" person for join issues.

    Picks the first MAIN admin found in role-priority order (chairperson →
    secretary → sde) from the MEMBER INFO tab. Returns None if none is
    resolvable (callers fall back to their hardcoded constant)."""
    from config import MEMBER_INFO_TAB
    try:
        values = get_cached_values(tab_name=MEMBER_INFO_TAB)
        if not values:
            return None
        header = [h.strip().lower() for h in values[0]]
        role_i = header.index("role/position")
        tele_i = header.index("tele id")
        for kw in MAIN_ADMIN_ROLE_KEYWORDS:  # priority order
            for row in values[1:]:
                role = row[role_i].strip().lower() if role_i < len(row) else ""
                tid = row[tele_i].strip() if tele_i < len(row) else ""
                if kw in role and tid.isdigit():
                    return int(tid)
    except Exception as e:
        print(f"[ADMIN][WARN] Contact-admin lookup failed: {e}")
    return None


def is_member_in_ay2526(user_id) -> bool:
    """True if `user_id` appears in the `Tele ID` column of MEMBER INFO AY25/26.

    Used by the join-request gate: existing AY25/26 members are auto-approved
    into the new group; unknown ids are told to contact the admin instead.
    """
    try:
        values = get_cached_values(tab_name="MEMBER INFO AY25/26")
        if not values:
            return False
        header = [h.strip().lower() for h in values[0]]
        try:
            tele_i = header.index("tele id")
        except ValueError:
            print("[WARN] 'Tele ID' column not found in MEMBER INFO AY25/26.")
            return False
        return any(tele_i < len(r) and r[tele_i].strip() == str(user_id)
                   for r in values[1:])
    except Exception as e:
        print(f"[ERROR] is_member_in_ay2526 failed: {e}")
        return False


def _member_info_cols(ws):
    """Return (header_row, {lower_header: col_index_1based}) for the MEMBER INFO tab."""
    header = ws.row_values(1)
    return header, {h.strip().lower(): i for i, h in enumerate(header, start=1)}


def update_member_join_in_info(user_id, full_name: str = "", nickname: str = ""):
    """Stamp a (re)join in MEMBER INFO AY26/27: Join Date = today, Leave Date
    cleared, Status = Join.

    The member's row is matched by `Tele ID`. A rejoin therefore reuses their
    old row — the previous Leave Date is wiped and the Join Date refreshed. If
    no row matches, a minimal new row (Full Name + Nickname + Tele ID +
    Status/Join Date) is written into the first row whose Name and Tele ID
    cells are both empty, so it lands inside the formula region and the C:K
    lookups can auto-fill.
    """
    from config import MEMBER_INFO_TAB
    from datetime import datetime
    from gspread.utils import rowcol_to_a1

    ws = get_gspread_sheet(tab_name=MEMBER_INFO_TAB)
    values = ws.get_all_values()
    _, cols = _member_info_cols(ws)
    tele_c = cols.get("tele id")
    join_c = cols.get("join date")
    leave_c = cols.get("leave date")
    status_c = cols.get("status")
    name_c = cols.get("full name (as per matric card)", 1)
    nick_c = cols.get("nickname")
    if not (tele_c and join_c and leave_c and status_c):
        print("[MEMBER INFO][WARN] Missing Tele ID/Join Date/Leave Date/Status column.")
        return False

    today = datetime.now(sg_tz).strftime("%d %b %Y %H:%M")

    def cell(r, c):
        row = values[r - 1] if r - 1 < len(values) else []
        return row[c - 1].strip() if c - 1 < len(row) else ""

    # Find the member's existing row by Tele ID
    target = next((r for r in range(2, len(values) + 1)
                   if cell(r, tele_c) == str(user_id)), None)

    updates = []
    if target is None:
        # New member: first row with both Name and Tele ID empty (works inside
        # the pre-filled formula region; never appends past unrelated rows).
        target = next((r for r in range(2, max(len(values) + 1, 101))
                       if not cell(r, name_c) and not cell(r, tele_c)), None)
        if target is None:
            print("[MEMBER INFO][WARN] No empty row found for new member.")
            return False
        if full_name:
            updates.append({"range": rowcol_to_a1(target, name_c), "values": [[full_name]]})
        if nickname and nick_c:
            updates.append({"range": rowcol_to_a1(target, nick_c), "values": [[nickname]]})
        updates.append({"range": rowcol_to_a1(target, tele_c), "values": [[str(user_id)]]})

    updates += [
        {"range": rowcol_to_a1(target, join_c), "values": [[today]]},
        {"range": rowcol_to_a1(target, leave_c), "values": [[""]]},   # clear old leave date
        {"range": rowcol_to_a1(target, status_c), "values": [["Join"]]},
    ]
    ws.batch_update(updates, value_input_option="USER_ENTERED")
    invalidate_sheet_cache(tab_name=MEMBER_INFO_TAB)
    print(f"[MEMBER INFO] Join stamped for {user_id} (row {target}): {today}, leave cleared, Join.")
    return True


def update_member_leave_in_info(user_id):
    """Stamp a leave in MEMBER INFO AY26/27: Leave Date = today, Status = Left.
    The row is matched by `Tele ID`; Join Date is left as-is (history)."""
    from config import MEMBER_INFO_TAB
    from datetime import datetime
    from gspread.utils import rowcol_to_a1

    ws = get_gspread_sheet(tab_name=MEMBER_INFO_TAB)
    values = ws.get_all_values()
    _, cols = _member_info_cols(ws)
    tele_c = cols.get("tele id")
    leave_c = cols.get("leave date")
    status_c = cols.get("status")
    if not (tele_c and leave_c and status_c):
        print("[MEMBER INFO][WARN] Missing Tele ID/Leave Date/Status column.")
        return False

    def cell(r, c):
        row = values[r - 1] if r - 1 < len(values) else []
        return row[c - 1].strip() if c - 1 < len(row) else ""

    target = next((r for r in range(2, len(values) + 1)
                   if cell(r, tele_c) == str(user_id)), None)
    if target is None:
        print(f"[MEMBER INFO][WARN] Leave: Tele ID {user_id} not found.")
        return False

    today = datetime.now(sg_tz).strftime("%d %b %Y %H:%M")
    ws.batch_update([
        {"range": rowcol_to_a1(target, leave_c), "values": [[today]]},
        {"range": rowcol_to_a1(target, status_c), "values": [["Left"]]},
    ], value_input_option="USER_ENTERED")
    invalidate_sheet_cache(tab_name=MEMBER_INFO_TAB)
    print(f"[MEMBER INFO] Leave stamped for {user_id} (row {target}): {today}, Left.")
    return True


def get_nickname_by_user_id(user_id):
    """Look up a member's nickname in the MEMBER INFO tab by their Telegram id.

    Matches the 'Tele ID' column against `user_id`; returns 'Nickname' (falling
    back to the full-name column). Returns None if not found.
    """
    from config import MEMBER_INFO_TAB
    try:
        ws = get_gspread_sheet(tab_name=MEMBER_INFO_TAB)
        values = ws.get_all_values()
        if not values:
            return None
        header = [h.strip().lower() for h in values[0]]

        def col_idx(name):
            try:
                return header.index(name)
            except ValueError:
                return None

        tele_i = col_idx("tele id")
        nick_i = col_idx("nickname")
        name_i = col_idx("full name (as per matric card)")
        if tele_i is None:
            print("[WARN] 'Tele ID' column not found in member info tab.")
            return None

        for row in values[1:]:
            if tele_i < len(row) and row[tele_i].strip() == str(user_id):
                if nick_i is not None and nick_i < len(row) and row[nick_i].strip():
                    return row[nick_i].strip()
                if name_i is not None and name_i < len(row):
                    return row[name_i].strip() or None
                return None
    except Exception as e:
        print(f"[ERROR] get_nickname_by_user_id failed: {e}")
    return None


def _find_member_row(ws, nickname):
    """Return the 1-based row of `nickname` in col A (rows 3+), or None."""
    names = ws.col_values(ATT_NAME_COL)
    target = nickname.strip().lower()
    for r in range(ATT_FIRST_MEMBER_ROW, len(names) + 1):
        if (names[r - 1] or "").strip().lower() == target:
            return r
    return None


def set_attendance(poll_id, user_id, present: bool):
    """Mark/clear attendance for a poll voter.

    Maps user_id → nickname (MEMBER INFO tab), finds the date column whose row-1
    poll id matches, locates (or creates) the member's row, and writes "1"/"".
    Column B (the count) is left entirely to the sheet's own formula — the bot
    never writes it. Returns (ok: bool, info: str).
    """
    nickname = get_nickname_by_user_id(user_id)
    if not nickname:
        return False, f"user {user_id} not in MEMBER INFO tab"

    ws = get_attendance_ws()
    row1 = ws.row_values(ATT_POLL_ROW)
    col = next((i for i, v in enumerate(row1, start=1)
                if _parse_training_poll_ref(v)[0] == str(poll_id)), None)
    if not col:
        return False, f"poll id {poll_id} not found in sheet"

    member_row = _find_member_row(ws, nickname)
    if not member_row:
        if not present:
            return True, f"{nickname} (no row, nothing to clear)"
        member_row = len(ws.col_values(ATT_NAME_COL)) + 1
        if member_row < ATT_FIRST_MEMBER_ROW:
            member_row = ATT_FIRST_MEMBER_ROW
        ws.update_cell(member_row, ATT_NAME_COL, nickname)

    ws.update_cell(member_row, col, "1" if present else "")
    invalidate_sheet_cache(tab_name=ATTENDANCE_TAB)
    return True, nickname


def get_attendees_for_date(col):
    """Return [(member_row, name, total, marked_bool), ...] sorted by TOTAL desc."""
    ws = get_attendance_ws()
    values = ws.get_all_values()
    attendees = []
    for r in range(ATT_FIRST_MEMBER_ROW, len(values) + 1):
        row = values[r - 1]
        name = (row[ATT_NAME_COL - 1] if len(row) >= ATT_NAME_COL else "").strip()
        if not name or name.lower() == "member":
            continue
        total_raw = (row[ATT_TOTAL_COL - 1] if len(row) >= ATT_TOTAL_COL else "").strip()
        try:
            total = int(total_raw)
        except ValueError:
            total = 0
        marked = len(row) >= col and (row[col - 1] or "").strip() == "1"
        attendees.append((r, name, total, marked))
    attendees.sort(key=lambda x: (-x[2], x[1].lower()))
    return attendees


def commit_attendance_column(col, marks_by_row: dict):
    """Batch-write a full date column. The TOTAL column is left untouched — it
    is a display-only COUNTIF formula maintained by the sheet, not the bot.

    marks_by_row maps {member_row: bool}. Rows not present keep their existing
    value. The whole date column is written in a single batch_update.
    """
    from gspread.utils import rowcol_to_a1

    ws = get_attendance_ws()
    values = ws.get_all_values()
    last_row = len(values)
    if last_row < ATT_FIRST_MEMBER_ROW:
        return

    col_cells = []
    for r in range(ATT_FIRST_MEMBER_ROW, last_row + 1):
        row = values[r - 1]
        marked = marks_by_row.get(r)
        if marked is None:
            marked = len(row) >= col and (row[col - 1] or "").strip() == "1"
        col_cells.append(["1" if marked else ""])

    c_start = rowcol_to_a1(ATT_FIRST_MEMBER_ROW, col)
    c_end = rowcol_to_a1(last_row, col)
    ws.batch_update([
        {"range": f"{c_start}:{c_end}", "values": col_cells},
    ], value_input_option="USER_ENTERED")
    invalidate_sheet_cache(tab_name=ATTENDANCE_TAB)

# ---- PERF TABULATION (performance-event attendance, keyed by thread id) ------

def get_perf_tab_ws():
    """Worksheet for the PERF TABULATION tab (performance attendance grid)."""
    return get_gspread_sheet(tab_name=PERF_TAB_NAME)


def get_perf_event_list():
    """Return [(thread_id, event_name, present_count), ...] of performances.

    Events are pulled from the PERF tab (every performance topic, by thread id);
    `present_count` is read from that event's column in PERF TABULATION (0 if it
    hasn't been marked yet). Listed latest-first (PERF tab order, newest rows
    last, so reversed).
    """
    ws = get_perf_tab_ws()
    values = ws.get_all_values()
    row1 = values[PERF_THREAD_ROW - 1] if len(values) >= PERF_THREAD_ROW else []

    present_by_tid = {}
    for col in range(PERF_FIRST_EVENT_COL, len(row1) + 1):
        tid = (row1[col - 1] or "").strip()
        if not tid:
            continue
        cnt = sum(1 for r in range(PERF_FIRST_MEMBER_ROW, len(values) + 1)
                  if col - 1 < len(values[r - 1]) and (values[r - 1][col - 1] or "").strip() == "1")
        present_by_tid[tid] = cnt

    import re
    from datetime import datetime

    # Helper to parse the date for sorting
    def get_sort_date(row):
        date_str = str(row.get("PERF DATE | TIME", "")).strip()
        if not date_str or date_str == "-": 
            return datetime.min
        # Grab the first line and extract the date part
        first_line = date_str.splitlines()[0].strip()
        match = re.match(r'^(\d{1,2}\s+[a-zA-Z]{3,9}\s+\d{4})', first_line)
        if match:
            try:
                return datetime.strptime(match.group(1), "%d %b %Y")
            except ValueError:
                pass
        return datetime.min

    records = get_cached_records()
    # Sort descending (Latest date at the top)
    sorted_records = sorted(records, key=get_sort_date, reverse=True)

    events = []
    for rec in sorted_records:
        tid = str(rec.get("THREAD ID", "")).strip()
        if tid.isdigit():
            events.append((int(tid), rec.get("EVENT NAME", "Unnamed Event"),
                           present_by_tid.get(tid, 0)))
                           
    return events


def get_perf_event_column(thread_id, create=False, event_name=""):
    """Return the PERF TABULATION column whose row-1 holds `thread_id`.

    If absent and `create` is True, a new column is written (thread id in row 1,
    event name in row 2) in the first empty slot from col D and its index is
    returned. Otherwise returns None.
    """
    ws = get_perf_tab_ws()
    row1 = ws.row_values(PERF_THREAD_ROW)
    for col in range(PERF_FIRST_EVENT_COL, len(row1) + 1):
        if (row1[col - 1] or "").strip() == str(thread_id):
            return col
    if not create:
        return None

    target = None
    for col in range(PERF_FIRST_EVENT_COL, len(row1) + 1):
        if not (row1[col - 1] or "").strip():
            target = col
            break
    if target is None:
        target = max(len(row1) + 1, PERF_FIRST_EVENT_COL)
    ws.update_cell(PERF_THREAD_ROW, target, str(thread_id))
    if event_name:
        ws.update_cell(PERF_EVENT_ROW, target, event_name)
    invalidate_sheet_cache(tab_name=PERF_TAB_NAME)
    return target


def get_perf_attendees(col):
    """[(member_row, name, total, marked_bool), ...] for a PERF event column.

    Members are read from col A (the admin pre-fills the roster). Sorted by the
    Tabulation count (col B) descending, like the regular-training view.
    """
    ws = get_perf_tab_ws()
    values = ws.get_all_values()
    out = []
    for r in range(PERF_FIRST_MEMBER_ROW, len(values) + 1):
        row = values[r - 1]
        name = (row[PERF_NAME_COL - 1] if len(row) >= PERF_NAME_COL else "").strip()
        if not name or name.lower() == "member":
            continue
        total_raw = (row[PERF_TOTAL_COL - 1] if len(row) >= PERF_TOTAL_COL else "").strip()
        try:
            total = int(total_raw)
        except ValueError:
            total = 0
        marked = len(row) >= col and (row[col - 1] or "").strip() == "1"
        out.append((r, name, total, marked))
    out.sort(key=lambda x: (-x[2], x[1].lower()))
    return out


def commit_perf_column(col, marks_by_row: dict):
    """Batch-write a full PERF event column. Mirrors commit_attendance_column;
    the Tabulation column (B) is a sheet formula and is never written."""
    from gspread.utils import rowcol_to_a1

    ws = get_perf_tab_ws()
    values = ws.get_all_values()
    last_row = len(values)
    if last_row < PERF_FIRST_MEMBER_ROW:
        return

    col_cells = []
    for r in range(PERF_FIRST_MEMBER_ROW, last_row + 1):
        row = values[r - 1]
        marked = marks_by_row.get(r)
        if marked is None:
            marked = len(row) >= col and (row[col - 1] or "").strip() == "1"
        col_cells.append(["1" if marked else ""])

    c_start = rowcol_to_a1(PERF_FIRST_MEMBER_ROW, col)
    c_end = rowcol_to_a1(last_row, col)
    ws.batch_update([
        {"range": f"{c_start}:{c_end}", "values": col_cells},
    ], value_input_option="USER_ENTERED")
    invalidate_sheet_cache(tab_name=PERF_TAB_NAME)


def get_all_perf_performers() -> dict:
    """Return {thread_id_str: [name, ...]} for every member marked '1' in PERF TABULATION.

    Reads the sheet once; callers can look up performers for any event by thread id.
    Events with no column or no marks return an empty list.
    """
    try:
        ws = get_perf_tab_ws()
        values = ws.get_all_values()
    except Exception as e:
        print(f"[WARN] Could not read PERF TABULATION for performer list: {e}")
        return {}

    row1 = values[PERF_THREAD_ROW - 1] if len(values) >= PERF_THREAD_ROW else []
    result = {}
    for col in range(PERF_FIRST_EVENT_COL, len(row1) + 1):
        tid = (row1[col - 1] or "").strip()
        if not tid:
            continue
        names = []
        for r in range(PERF_FIRST_MEMBER_ROW, len(values) + 1):
            row = values[r - 1]
            name = (row[PERF_NAME_COL - 1] if len(row) >= PERF_NAME_COL else "").strip()
            if not name or name.lower() == "member":
                continue
            if len(row) >= col and (row[col - 1] or "").strip() == "1":
                names.append(name)
        result[tid] = names
    return result


def append_standard_topic_to_sheet(tid, name, rules_string):
    """Adds a new standard topic row to the 'STANDARD TOPIC Rules' tab."""
    try:
        from config import SHEET_NAME
        sheet = get_gspread_sheet(sheet_name=SHEET_NAME, tab_name="STANDARD TOPIC Rules")
        sheet.append_row([str(tid), name, rules_string])
        invalidate_sheet_cache(tab_name="STANDARD TOPIC Rules")
        print(f"✅ [GSheet] Successfully logged {name} to Rules sheet.")
    except Exception as e:
        print(f"❌ [GSheet ERROR] Failed to append standard topic: {e}")

def _welcome_tea_ws():
    """Worksheet for QR-scan IDs and confirmation status."""
    from config import SHEET_NAME, WELCOME_TEA_ID_TAB
    return get_gspread_sheet(sheet_name=SHEET_NAME, tab_name=WELCOME_TEA_ID_TAB)


def _normalize_welcome_tea_status(status: str) -> str:
    """Return the canonical Welcome Tea status, defaulting blanks to Not Confirm."""
    from config import (
        WELCOME_TEA_STATUS_ATTEND,
        WELCOME_TEA_STATUS_NOT_CONFIRM,
        WELCOME_TEA_STATUS_REJECT,
        WELCOME_TEA_STATUS_STILL_COMING,
    )
    raw = (status or "").strip()
    if not raw:
        return WELCOME_TEA_STATUS_NOT_CONFIRM
    lowered = raw.lower()
    status_map = {
        WELCOME_TEA_STATUS_NOT_CONFIRM.lower(): WELCOME_TEA_STATUS_NOT_CONFIRM,
        WELCOME_TEA_STATUS_ATTEND.lower(): WELCOME_TEA_STATUS_ATTEND,
        WELCOME_TEA_STATUS_REJECT.lower(): WELCOME_TEA_STATUS_REJECT,
        WELCOME_TEA_STATUS_STILL_COMING.lower(): WELCOME_TEA_STATUS_STILL_COMING,
    }
    return status_map.get(lowered, raw)


def _welcome_tea_layout(values=None):
    """Return (first_data_row, col_indexes) for WELCOME TEA ID tab.

    Scans the first 6 rows for a header row to support the new layout where
    rows 1-5 are config/header and user data begins at row 6.
    Falls back to the default A-N column positions if no header is detected.
    """
    values = values or []
    default_cols = {
        "tele_id": 1,
        "tele_handle": 2,
        "timestamp": 3,
        "status": 4,
        "dietary": 5,
        "attendance": 6,
        "cutoff_msg_id": 7,
        "final_cutoff_msg_id": 8,
        "details_sent": 9,
        "reminder_sent": 10,
        "approval_sent": 11,
        "pre_cutoff_sent": 12,
        "cutoff_sent": 13,
        "wtd_reminder_sent": 14,
        "followup_sent": 15,
    }
    if not values:
        return 1, default_cols

    aliases = {
        "tele_id": ("tele id", "telegram id", "user id"),
        "tele_handle": ("tele handle", "username", "tele username", "telegram handle"),
        "timestamp": ("timestamp", "time stamp", "req to join timestamp"),
        "status": ("status",),
        "dietary": ("dietary",),
        "attendance": ("attendance",),
        "cutoff_msg_id": ("cutoff msg id",),
        "final_cutoff_msg_id": ("final cutoff msg id",),
        "details_sent": ("details send", "details sent"),
        "reminder_sent": ("reminder send", "reminder sent"),
        "approval_sent": ("approval send", "approval sent"),
        "pre_cutoff_sent": ("pre cutoff send", "pre cutoff sent"),
        "cutoff_sent": ("cutoff send", "cutoff sent"),
        "wtd_reminder_sent": ("wtd reminder send", "wtd reminder sent"),
        "followup_sent": ("followup send", "followup sent", "follow up send", "follow-up send"),
    }
    all_alias_names = {name for names in aliases.values() for name in names}

    header_row_idx = None
    for i, row in enumerate(values[:6]):
        normalized = [str(cell).strip().lower() for cell in row]
        if any(name in normalized for name in all_alias_names):
            header_row_idx = i
            break

    if header_row_idx is None:
        return 1, default_cols

    header = [str(cell).strip().lower() for cell in values[header_row_idx]]
    cols = {}
    for key, names in aliases.items():
        cols[key] = next(
            (header.index(name) + 1 for name in names if name in header),
            default_cols[key],
        )
    return header_row_idx + 2, cols


def _row_cell(row, one_based_col):
    return row[one_based_col - 1].strip() if one_based_col - 1 < len(row) else ""




def _parse_sheet_date(value):
    from datetime import date, datetime

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%d/%m/%Y",
        "%m/%d/%y",
        "%d/%m/%y",
        "%d %b %Y",
        "%d %B %Y",
        "%b %d, %Y",
        "%B %d, %Y",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_sheet_time(value):
    from datetime import time as dt_time, datetime

    if isinstance(value, datetime):
        return value.time().replace(second=0, microsecond=0)
    if isinstance(value, dt_time):
        return value.replace(second=0, microsecond=0)
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.upper().replace(".", "")
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M:%S %p"):
        try:
            return datetime.strptime(normalized, fmt).time().replace(second=0, microsecond=0)
        except ValueError:
            continue
    return None


def _parse_sheet_datetime(date_value, time_value, fallback_dt):
    from datetime import datetime

    date_text = str(date_value or "").strip()
    time_text = str(time_value or "").strip()
    for fmt in (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%m/%d/%Y %I:%M %p",
        "%d/%m/%Y %I:%M %p",
    ):
        try:
            parsed = datetime.strptime(date_text, fmt)
            return sg_tz.localize(parsed)
        except ValueError:
            continue

    parsed_date = _parse_sheet_date(date_text) or (fallback_dt.date() if fallback_dt else None)
    parsed_time = _parse_sheet_time(time_text) or _parse_sheet_time(date_text)
    if parsed_date and parsed_time:
        return sg_tz.localize(datetime.combine(parsed_date, parsed_time))
    return fallback_dt


def get_welcome_tea_settings():
    """Read Welcome Tea settings from the WELCOME TEA ID tab.

    New layout (rows 1-4 are config, row 5 is headers, row 6+ is user data):
      Row 1, col B: Event date
      Row 2, col B: WT group invite link
      Row 3, col B: Main group invite link
      Row 4, col B: Signup form link
      Row 3, cols I-N: TIME for 6 scheduled jobs
      Row 4, cols I-N: DATE for 6 scheduled jobs
      Job col order: I=Details, J=Reminder, K=Approval, L=PreCutoff, M=Cutoff, N=WTDReminder
    """
    try:
        values = _welcome_tea_ws().get_all_values()
    except Exception as e:
        print(f"[WELCOME TEA][WARN] Failed to read sheet settings: {e}")
        return {}

    def _cell(row_idx, col_1based):
        row = values[row_idx] if row_idx < len(values) else []
        return _row_cell(row, col_1based)

    event_date = _parse_sheet_date(_cell(0, 2))   # B1
    wt_join_link = _cell(1, 2).strip()             # B2
    main_group_link = _cell(2, 2).strip()          # B3
    signup_form_link = _cell(3, 2).strip()         # B4

    # Job schedule: time from row 3 (index 2), date from row 4 (index 3), cols I-N (9-14)
    job_col_map = {
        "details_send":  9,
        "reminder_send": 10,
        "approval":      11,
        "pre_cutoff":    12,
        "cutoff":        13,
        "wtd_reminder":  14,
        "followup_send": 15,   # O3 = time, O4 = date
    }

    job_datetimes = {}
    for job_key, col in job_col_map.items():
        if col is None:
            job_datetimes[job_key] = None
            continue
        time_val = _cell(2, col)   # row 3
        date_val = _cell(3, col)   # row 4
        job_datetimes[job_key] = _parse_sheet_datetime(date_val, time_val, None)

    # Final cleanup: WT day at 19:30 SGT by default; C1 overrides the time for testing
    final_cleanup_at = None
    if event_date:
        from datetime import datetime, time as dt_time
        override_time = _parse_sheet_time(_cell(0, 3))  # C1
        cleanup_time = override_time if override_time else dt_time(19, 30)
        final_cleanup_at = sg_tz.localize(datetime.combine(event_date, cleanup_time))

    return {
        "event_date": event_date,
        "welcome_tea_join_request_link": wt_join_link,
        "main_group_welcome_tea_invite_link": main_group_link,
        "signup_form_link": signup_form_link,
        "details_send_at":  job_datetimes["details_send"],
        "reminder_send_at": job_datetimes["reminder_send"],
        "approval_at":      job_datetimes["approval"],
        "pre_cutoff_at":    job_datetimes["pre_cutoff"],
        "cutoff_at":        job_datetimes["cutoff"],
        "wtd_reminder_at":  job_datetimes["wtd_reminder"],
        "followup_send_at": job_datetimes["followup_send"],
        "final_cleanup_at": final_cleanup_at,
    }

def mark_welcome_tea_setting_sent(row_number: int):
    """Legacy: writes 'SENT' into Column J (10) for a specific setting row."""
    if not row_number:
        return False
    try:
        ws = _welcome_tea_ws()
        ws.update_cell(row_number, 10, "SENT")
        invalidate_sheet_cache(tab_name=WELCOME_TEA_ID_TAB)
        print(f"[SYSTEM] Successfully logged 'SENT' in row {row_number}.")
        return True
    except Exception as e:
        print(f"[ERROR] Failed to mark setting as SENT on row {row_number}: {e}")
        return False


def _update_wt_user_cell(user_id: int, col_index: int, value) -> bool:
    """Write `value` to a specific 1-based column for the given user_id row."""
    try:
        ws = _welcome_tea_ws()
        values = ws.get_all_values()
        first_data_row, cols = _welcome_tea_layout(values)
        target = str(user_id)
        for row_number, row in enumerate(values[first_data_row - 1:], start=first_data_row):
            if _row_cell(row, cols["tele_id"]) == target:
                ws.update_cell(row_number, col_index, value)
                invalidate_sheet_cache(tab_name=WELCOME_TEA_ID_TAB)
                return True
        print(f"[WELCOME TEA][WARN] User {user_id} not found for col {col_index} update.")
        return False
    except Exception as e:
        print(f"[WELCOME TEA][ERROR] Failed to update col {col_index} for {user_id}: {e}")
        return False


def get_wt_write_context():
    """Return `(worksheet, {tele_id: row_number})` for batched Welcome Tea writes.

    Costs one worksheet lookup + one read. Pass the result to
    `batch_update_wt_cells(..., ctx=...)` so a broadcast that flushes several
    times still only pays for that read once. Returns None if the sheet can't be
    reached (callers then fall back to a self-contained write).
    """
    try:
        ws = _welcome_tea_ws()
        values = ws.get_all_values()
        first_data_row, cols = _welcome_tea_layout(values)
        row_by_uid = {}
        for row_number, row in enumerate(values[first_data_row - 1:], start=first_data_row):
            uid = _row_cell(row, cols["tele_id"])
            if uid:
                row_by_uid[uid] = row_number
        return ws, row_by_uid
    except Exception as e:
        print(f"[WELCOME TEA][ERROR] Could not build write context: {e}")
        return None


def batch_update_wt_cells(updates, ctx=None) -> int:
    """Write many per-user cells in ONE batch_update call.

    `updates` is a list of `(user_id, col_index, value)` tuples. With a `ctx`
    from `get_wt_write_context()` the call costs exactly 1 write; without one it
    also pays for its own read. Either way it replaces the 2 reads + 2 writes
    *per user* that `_update_wt_user_cell` in a loop would cost — a 100-member
    broadcast would otherwise blow Google's 60-requests-per-minute quota partway
    through and silently drop the remaining SENT markers.

    Returns the number of cells actually written.
    """
    if not updates:
        return 0
    try:
        from gspread.utils import rowcol_to_a1

        if ctx is None:
            ctx = get_wt_write_context()
            if ctx is None:
                return 0
        ws, row_by_uid = ctx

        batch = []
        for user_id, col_index, value in updates:
            row_number = row_by_uid.get(str(user_id))
            if not row_number:
                print(f"[WELCOME TEA][WARN] User {user_id} not found for batch update (col {col_index}).")
                continue
            batch.append({
                "range": rowcol_to_a1(row_number, col_index),
                "values": [[str(value)]],
            })

        if not batch:
            return 0
        ws.batch_update(batch, value_input_option="USER_ENTERED")  # 1 WRITE for the whole batch
        invalidate_sheet_cache(tab_name=WELCOME_TEA_ID_TAB)
        print(f"[WELCOME TEA] Batch-wrote {len(batch)} cells in 1 API call.")
        return len(batch)
    except Exception as e:
        print(f"[WELCOME TEA][ERROR] Batch cell update failed: {e}")
        return 0


def get_wt_user(user_id: int):
    """Return the full row dict for a user, or None if not found."""
    target = str(user_id)
    for row in get_welcome_tea_rows():
        if str(row["user_id"]) == target:
            row["user_id"] = int(row["user_id"])
            return row
    return None


def update_wt_dietary(user_id: int, dietary: str) -> bool:
    """Write dietary preference to col E (5) for the user."""
    return _update_wt_user_cell(user_id, 5, dietary)


def update_wt_attendance(user_id: int) -> bool:
    """Write '1' to col F (6) to mark WT-day attendance."""
    return _update_wt_user_cell(user_id, 6, "1")


def update_wt_msg_id(user_id: int, col_index: int, msg_id: int) -> bool:
    """Store a message ID in col G (7=Cutoff Msg) or H (8=Final Cutoff Msg)."""
    return _update_wt_user_cell(user_id, col_index, str(msg_id))


def mark_wt_user_col_sent(user_id: int, col_index: int) -> bool:
    """Write 'SENT' to a per-user tracking column (I=9 through N=14)."""
    return _update_wt_user_cell(user_id, col_index, "SENT")

def get_welcome_tea_rows():
    """Return row dicts from WELCOME TEA ID (all columns A-N)."""
    rows = []
    try:
        values = _welcome_tea_ws().get_all_values()
        first_data_row, cols = _welcome_tea_layout(values)
        for row_number, row in enumerate(values[first_data_row - 1:], start=first_data_row):
            tele_id = _row_cell(row, cols["tele_id"])
            if not tele_id:
                continue
            rows.append({
                "row_number": row_number,
                "user_id": tele_id,
                "username": _row_cell(row, cols["tele_handle"]),
                "timestamp": _row_cell(row, cols["timestamp"]),
                "status": _normalize_welcome_tea_status(_row_cell(row, cols["status"])),
                "dietary": _row_cell(row, cols.get("dietary", 5)),
                "attendance": _row_cell(row, cols.get("attendance", 6)),
                "cutoff_msg_id": _row_cell(row, cols.get("cutoff_msg_id", 7)),
                "final_cutoff_msg_id": _row_cell(row, cols.get("final_cutoff_msg_id", 8)),
                "details_sent": _row_cell(row, cols.get("details_sent", 9)),
                "reminder_sent": _row_cell(row, cols.get("reminder_sent", 10)),
                "approval_sent": _row_cell(row, cols.get("approval_sent", 11)),
                "pre_cutoff_sent": _row_cell(row, cols.get("pre_cutoff_sent", 12)),
                "cutoff_sent": _row_cell(row, cols.get("cutoff_sent", 13)),
                "wtd_reminder_sent": _row_cell(row, cols.get("wtd_reminder_sent", 14)),
                "followup_sent": _row_cell(row, cols.get("followup_sent", 15)),
            })
    except Exception as e:
        print(f"[ERROR] Failed to read Welcome Tea IDs: {e}")
    return rows


def get_welcome_tea_recipients(statuses=None):
    """Return Welcome Tea rows whose status is in `statuses`.

    `statuses` may be None to return every row. Rows with non-numeric Tele IDs
    are skipped because Telegram chat IDs must be integers.
    """
    wanted = {_normalize_welcome_tea_status(s) for s in statuses} if statuses else None
    recipients = []
    for row in get_welcome_tea_rows():
        if wanted is not None and row["status"] not in wanted:
            continue
        if not str(row["user_id"]).isdigit():
            print(f"[WELCOME TEA][WARN] Skipping non-numeric Tele ID: {row['user_id']}")
            continue
        row = dict(row)
        row["user_id"] = int(row["user_id"])
        recipients.append(row)
    return recipients


def update_welcome_tea_status(user_id: int, status: str) -> bool:
    """Update Status for the row matching `user_id`."""
    try:
        ws = _welcome_tea_ws()
        values = ws.get_all_values()
        first_data_row, cols = _welcome_tea_layout(values)
        target = str(user_id)
        for row_number, row in enumerate(values[first_data_row - 1:], start=first_data_row):
            if _row_cell(row, cols["tele_id"]) == target:
                ws.update_cell(row_number, cols["status"], _normalize_welcome_tea_status(status))
                invalidate_sheet_cache(tab_name=WELCOME_TEA_ID_TAB)
                print(f"[WELCOME TEA] Status for {user_id} updated to {status}.")
                return True
        print(f"[WELCOME TEA][WARN] Tele ID {user_id} not found for status update.")
        return False
    except Exception as e:
        print(f"[ERROR] Failed to update Welcome Tea status for {user_id}: {e}")
        return False


def append_welcome_tea_id(user_id: int, username: str = ""):
    """Insert/update a Telegram user in WELCOME TEA ID with header-aware columns."""
    try:
        from config import WELCOME_TEA_STATUS_NOT_CONFIRM
        from datetime import datetime
        from gspread.utils import rowcol_to_a1
        
        ws = _welcome_tea_ws()
        timestamp = datetime.now(sg_tz).strftime("%Y-%m-%d %H:%M:%S")
        values = ws.get_all_values()
        first_data_row, cols = _welcome_tea_layout(values)
        target = str(user_id)

        for row_number, row in enumerate(values[first_data_row - 1:], start=first_data_row):
            if _row_cell(row, cols["tele_id"]) == target:
                existing_status = _normalize_welcome_tea_status(_row_cell(row, cols["status"]))
                ws.batch_update([{
                    "range": rowcol_to_a1(row_number, cols["tele_id"]),
                    "values": [[target]],
                }, {
                    "range": rowcol_to_a1(row_number, cols["tele_handle"]),
                    "values": [[username or ""]],
                }, {
                    "range": rowcol_to_a1(row_number, cols["timestamp"]),
                    "values": [[timestamp]],
                }, {
                    "range": rowcol_to_a1(row_number, cols["status"]),
                    "values": [[existing_status or WELCOME_TEA_STATUS_NOT_CONFIRM]],
                }], value_input_option="USER_ENTERED")
                invalidate_sheet_cache(tab_name=WELCOME_TEA_ID_TAB)
                print(f"[INFO] Welcome Tea ID {user_id} ({username}) refreshed in WELCOME TEA ID.")
                return True

        for row_number, row in enumerate(values[first_data_row - 1:], start=first_data_row):
            if _row_cell(row, 6) == target:
                misplaced_status = _normalize_welcome_tea_status(_row_cell(row, 9))
                target_row = next(
                    (
                        candidate for candidate in range(first_data_row, max(len(values) + 2, 101))
                        if not _row_cell(
                            values[candidate - 1] if candidate - 1 < len(values) else [],
                            cols["tele_id"],
                        )
                    ),
                    len(values) + 1,
                )
                ws.batch_update([{
                    "range": f"{rowcol_to_a1(target_row, cols['tele_id'])}:{rowcol_to_a1(target_row, cols['status'])}",
                    "values": [[target, username or _row_cell(row, 7), timestamp, misplaced_status]],
                }, {
                    "range": f"{rowcol_to_a1(row_number, 6)}:{rowcol_to_a1(row_number, 9)}",
                    "values": [["", "", "", ""]],
                }], value_input_option="USER_ENTERED")
                invalidate_sheet_cache(tab_name=WELCOME_TEA_ID_TAB)
                print(f"[INFO] Misplaced Welcome Tea ID {user_id} moved to A:D.")
                return True

        target_row = next(
            (
                row_number for row_number in range(first_data_row, max(len(values) + 2, 101))
                if not _row_cell(
                    values[row_number - 1] if row_number - 1 < len(values) else [],
                    cols["tele_id"],
                )
            ),
            len(values) + 1,
        )
        ws.batch_update([{
            "range": f"{rowcol_to_a1(target_row, cols['tele_id'])}:{rowcol_to_a1(target_row, cols['status'])}",
            "values": [[target, username or "", timestamp, WELCOME_TEA_STATUS_NOT_CONFIRM]],
        }], value_input_option="USER_ENTERED")
        invalidate_sheet_cache(tab_name=WELCOME_TEA_ID_TAB)
        print(f"[INFO] Welcome Tea ID {user_id} ({username}) logged to WELCOME TEA ID tab.")
        return True
    except Exception as e:
        print(f"[ERROR] Failed to append Welcome Tea ID {user_id}: {str(e)}")
        return False
