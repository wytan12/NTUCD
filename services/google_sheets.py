import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from config import (GOOGLE_CREDENTIALS_JSON, SHEET_NAME, SHEET_TAB_NAME,
                   ATTENDANCE_TAB, WELCOME_TEA_SHEET, WELCOME_TEA_TAB,
                   ATT_NAME_COL, ATT_TOTAL_COL, ATT_LABEL_COL, ATT_FIRST_DATE_COL,
                   ATT_POLL_ROW, ATT_DATE_ROW, ATT_FIRST_MEMBER_ROW, sg_tz)
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
        creds = ServiceAccountCredentials.from_json_keyfile_dict(GOOGLE_CREDENTIALS_JSON, scope)
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

def _spreadsheet_modified_time(sheet_name):
    """Return the spreadsheet's live Drive modifiedTime string, or None on error."""
    try:
        if sheet_name not in _spreadsheets:
            _spreadsheets[sheet_name] = _get_client().open(sheet_name)
        return _spreadsheets[sheet_name].get_lastUpdateTime()
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
        stamp = _spreadsheet_modified_time(sheet_name)
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
        stamp = _spreadsheet_modified_time(sheet_name)
    _values_cache[key] = {"values": values, "stamp": stamp}
    return values

def invalidate_sheet_cache(sheet_name=None, tab_name=None):
    """Drop cached snapshots so the next read re-downloads (manual Refresh)."""
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
        print(f"[INFO] Thread ID {thread_id} ({event_name}) logged to OTHERS tab.")
        from utils.constants import OTHERS_THREAD_IDS
        OTHERS_THREAD_IDS.add(int(thread_id))
    except Exception as e:
        print(f"[ERROR] Failed to write Thread ID {thread_id} to OTHERS tab: {str(e)}")

def matric_valid(matric_number: str) -> bool:
    """Return True if the matric number exists in the Welcome Tea sheet AND
    has Attendance == "1" (i.e. the person physically attended Welcome Tea).
    """
    sheet = get_gspread_sheet(WELCOME_TEA_SHEET, WELCOME_TEA_TAB)
    rows = sheet.get_all_records()

    for row in rows:
        matric_in_row = str(row.get("Matriculation Number", "")).strip().upper()
        attendance = str(row.get("Attendance", "")).strip()

        if matric_in_row == matric_number.strip().upper():
            return attendance == "1"
    return False

def update_user_id_in_sheet(matric_number: str, telegram_user_id: int):
    """Update user ID in the welcome tea sheet"""
    sheet = get_gspread_sheet(WELCOME_TEA_SHEET, WELCOME_TEA_TAB)
    rows = sheet.get_all_records()

    for idx, row in enumerate(rows, start=2):
        matric_in_row = str(row.get("Matriculation Number", "")).strip().upper()
        if matric_in_row == matric_number.strip().upper():
            user_id_col = None
            header = sheet.row_values(1)
            for i, col_name in enumerate(header, start=1):
                if col_name.strip().lower() == "user id":
                    user_id_col = i
                    break

            if user_id_col:
                sheet.update_cell(idx, user_id_col, str(telegram_user_id))
                print(f"[INFO] User ID {telegram_user_id} saved for {matric_number} in row {idx}.")
                copy_user_to_timeline(row, telegram_user_id)
            else:
                print("[ERROR] 'User ID' column not found in sheet.")
            return
    print("[WARN] Matric number not found when trying to update User ID.")

def copy_user_to_timeline(welcome_row: dict, telegram_user_id: int):
    """Append a new member row to the PERFORMER Info sheet.

    welcome_row should be a dict from the Welcome Tea responses (or a minimal
    fallback dict with just name/nickname keys).
    """
    others_sheet = get_gspread_sheet("PERFORMER Info")
    name = welcome_row.get("Your Full Name (according to matric card)", "").strip()
    nickname = welcome_row.get("What name or nickname do you prefer to be called? ", "").strip()

    new_row = [name, nickname, str(telegram_user_id), "Join", ""]
    others_sheet.append_row(new_row, value_input_option="USER_ENTERED")
    print(f"[INFO] Copied to PERFORMER Info List: {new_row}")

def user_already_in_timeline(user_id: int) -> bool:
    """Check if user already exists in timeline"""
    others_sheet = get_gspread_sheet("PERFORMER Info")
    all_rows = others_sheet.get_all_records()
    for row in all_rows:
        if str(row.get("User ID", "")).strip() == str(user_id):
            return True
    return False

def mark_user_left_in_sheet(user_id: int) -> bool:
    """Mark user as left in the sheet"""
    from datetime import datetime
    
    sheet = get_gspread_sheet("PERFORMER Info")
    records = sheet.get_all_records()
    header = sheet.row_values(1)

    user_id_col = header.index("User ID") + 1
    status_col = header.index("Status") + 1 if "Status" in header else None
    leave_date_col = header.index("Leave Date") + 1 if "Leave Date" in header else None

    if not (user_id_col and status_col and leave_date_col):
        print("[ERROR] Missing required columns")
        return False

    for idx, row in enumerate(records, start=2):
        if str(row.get("User ID", "")).strip() == str(user_id):
            sheet.update_cell(idx, status_col, "Left")
            leave_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sheet.update_cell(idx, leave_date_col, leave_time)
            return True

    print(f"[WARN] User ID {user_id} not found in sheet.")
    return False

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


def record_training_poll(poll_id, training_date) -> int:
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

    ws.update_cell(ATT_POLL_ROW, target_col, str(poll_id))
    return target_col


def replace_training_date_column(col, new_date_str, poll_id=None):
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
        {"range": rowcol_to_a1(ATT_POLL_ROW, col), "values": [[str(poll_id) if poll_id else ""]]},
    ]
    if last_row >= ATT_FIRST_MEMBER_ROW:
        c_start = rowcol_to_a1(ATT_FIRST_MEMBER_ROW, col)
        c_end = rowcol_to_a1(last_row, col)
        cleared = [[""] for _ in range(ATT_FIRST_MEMBER_ROW, last_row + 1)]
        requests.append({"range": f"{c_start}:{c_end}", "values": cleared})

    ws.batch_update(requests, value_input_option="USER_ENTERED")


def is_training_poll_id(poll_id) -> bool:
    """True if `poll_id` is recorded in the attendance poll-id row (survives restarts)."""
    try:
        ws = get_attendance_ws()
        row1 = ws.row_values(ATT_POLL_ROW)
        return any((v or "").strip() == str(poll_id) for v in row1)
    except Exception as e:
        print(f"[ERROR] is_training_poll_id failed: {e}")
        return False


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

    Maps user_id → nickname (PERFORMER Info), finds the date column whose row-1
    poll id matches, locates (or creates) the member's row, and writes "1"/"".
    Column B (the count) is left entirely to the sheet's own formula — the bot
    never writes it. Returns (ok: bool, info: str).
    """
    nickname = get_nickname_by_user_id(user_id)
    if not nickname:
        return False, f"user {user_id} not in PERFORMER Info"

    ws = get_attendance_ws()
    row1 = ws.row_values(ATT_POLL_ROW)
    col = next((i for i, v in enumerate(row1, start=1)
                if (v or "").strip() == str(poll_id)), None)
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
    return True, nickname


def get_attendees_for_date(col):
    """Return [(member_row, name, total, marked_bool), ...] sorted by TOTAL desc."""
    ws = get_attendance_ws()
    values = ws.get_all_values()
    attendees = []
    for r in range(ATT_FIRST_MEMBER_ROW, len(values) + 1):
        row = values[r - 1]
        name = (row[ATT_NAME_COL - 1] if len(row) >= ATT_NAME_COL else "").strip()
        if not name:
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

def append_standard_topic_to_sheet(tid, name, rules_string):
    """Adds a new standard topic row to the 'STANDARD TOPIC Rules' tab."""
    try:
        from config import SHEET_NAME
        sheet = get_gspread_sheet(sheet_name=SHEET_NAME, tab_name="STANDARD TOPIC Rules")
        sheet.append_row([str(tid), name, rules_string])
        print(f"✅ [GSheet] Successfully logged {name} to Rules sheet.")
    except Exception as e:
        print(f"❌ [GSheet ERROR] Failed to append standard topic: {e}")