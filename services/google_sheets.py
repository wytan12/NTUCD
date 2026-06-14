import re
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from config import (GOOGLE_CREDENTIALS_JSON, SHEET_NAME, SHEET_TAB_NAME,
                   ATTENDANCE_TAB, WELCOME_TEA_SHEET, WELCOME_TEA_TAB,
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
    """Record a new member in the MEMBER INFO tab (legacy-named wrapper).

    Previously appended to the separate "PERFORMER Info" spreadsheet; now
    delegates to `update_member_join_in_info` on `MEMBER_INFO_TAB` (Status =
    Join, Join Date = today, row matched/created by Tele ID).
    welcome_row is a dict from the Welcome Tea responses (or a minimal
    fallback dict with just name/nickname keys).
    """
    name = welcome_row.get("Your Full Name (according to matric card)", "").strip()
    nickname = welcome_row.get("What name or nickname do you prefer to be called? ", "").strip()
    update_member_join_in_info(telegram_user_id, full_name=name, nickname=nickname)

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


def get_admin_role_ids():
    """Return (main_ids, secondary_ids) — Tele IDs from the MEMBER INFO tab
    whose Role/Position contains a tier keyword (case-insensitive).

    Reads through the smart cache, so it's a tiny check per call. Returns two
    empty sets on any sheet failure (callers fall back to config ids).
    """
    from config import MEMBER_INFO_TAB
    main, secondary = set(), set()
    try:
        values = get_cached_values(tab_name=MEMBER_INFO_TAB)
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


def get_alert_admin_ids():
    """Tele IDs that receive admin alert/reminder DMs: MAIN admins only.
    Falls back to config.ADMIN_DM_USER_IDS if no main admin is resolvable."""
    main, _ = get_admin_role_ids()
    return main or _config_admin_ids()


def get_dashboard_admin_ids():
    """Tele IDs allowed to use the DM dashboard: MAIN + SECONDARY admins only.

    The config ADMIN_DM_USER_IDS ids are used **only as an emergency fallback**
    when the role lookup yields nothing at all (sheet unreachable / Role column
    wiped) — they are NOT granted access alongside the role-holders, so a plain
    member listed in the old config set cannot reach the dashboard."""
    main, secondary = get_admin_role_ids()
    role_ids = main | secondary
    return role_ids if role_ids else _config_admin_ids()


def is_dashboard_admin(user_id) -> bool:
    """True if `user_id` may use the admin DM dashboard (either tier)."""
    try:
        return int(user_id) in get_dashboard_admin_ids()
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
    invalidate_sheet_cache()
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
    invalidate_sheet_cache()
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
        if not name:
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


def append_standard_topic_to_sheet(tid, name, rules_string):
    """Adds a new standard topic row to the 'STANDARD TOPIC Rules' tab."""
    try:
        from config import SHEET_NAME
        sheet = get_gspread_sheet(sheet_name=SHEET_NAME, tab_name="STANDARD TOPIC Rules")
        sheet.append_row([str(tid), name, rules_string])
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
    )
    raw = (status or "").strip()
    if not raw:
        return WELCOME_TEA_STATUS_NOT_CONFIRM
    lowered = raw.lower()
    status_map = {
        WELCOME_TEA_STATUS_NOT_CONFIRM.lower(): WELCOME_TEA_STATUS_NOT_CONFIRM,
        WELCOME_TEA_STATUS_ATTEND.lower(): WELCOME_TEA_STATUS_ATTEND,
        WELCOME_TEA_STATUS_REJECT.lower(): WELCOME_TEA_STATUS_REJECT,
    }
    return status_map.get(lowered, raw)


def get_welcome_tea_rows():
    """Return row dicts from WELCOME TEA ID.

    The tab intentionally has no header row:
      A = Tele ID, B = username, C = timestamp, D = status.
    Blank statuses are treated as Not Confirm for backward compatibility.
    """
    rows = []
    try:
        values = _welcome_tea_ws().get_all_values()
        for row_number, row in enumerate(values, start=1):
            tele_id = row[0].strip() if len(row) >= 1 else ""
            if not tele_id:
                continue
            status = row[3] if len(row) >= 4 else ""
            rows.append({
                "row_number": row_number,
                "user_id": tele_id,
                "username": row[1].strip() if len(row) >= 2 else "",
                "timestamp": row[2].strip() if len(row) >= 3 else "",
                "status": _normalize_welcome_tea_status(status),
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
    """Update column D status for the row matching `user_id`."""
    try:
        ws = _welcome_tea_ws()
        values = ws.get_all_values()
        target = str(user_id)
        for row_number, row in enumerate(values, start=1):
            if row and row[0].strip() == target:
                ws.update_cell(row_number, 4, _normalize_welcome_tea_status(status))
                invalidate_sheet_cache()
                print(f"[WELCOME TEA] Status for {user_id} updated to {status}.")
                return True
        print(f"[WELCOME TEA][WARN] Tele ID {user_id} not found for status update.")
        return False
    except Exception as e:
        print(f"[ERROR] Failed to update Welcome Tea status for {user_id}: {e}")
        return False


def append_welcome_tea_id(user_id: int, username: str = ""):
    """Insert/update a Telegram user in WELCOME TEA ID with status in col D."""
    try:
        from config import WELCOME_TEA_STATUS_NOT_CONFIRM
        from datetime import datetime
        from gspread.utils import rowcol_to_a1
        
        ws = _welcome_tea_ws()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        values = ws.get_all_values()
        target = str(user_id)

        for row_number, row in enumerate(values, start=1):
            if row and row[0].strip() == target:
                ws.batch_update([{
                    "range": f"{rowcol_to_a1(row_number, 1)}:{rowcol_to_a1(row_number, 4)}",
                    "values": [[target, username or "", timestamp, WELCOME_TEA_STATUS_NOT_CONFIRM]],
                }], value_input_option="USER_ENTERED")
                invalidate_sheet_cache()
                print(f"[INFO] Welcome Tea ID {user_id} ({username}) refreshed in WELCOME TEA ID.")
                return True

        ws.append_row(
            [target, username or "", timestamp, WELCOME_TEA_STATUS_NOT_CONFIRM],
            value_input_option="USER_ENTERED",
        )
        invalidate_sheet_cache()
        print(f"[INFO] Welcome Tea ID {user_id} ({username}) logged to WELCOME TEA ID tab.")
        return True
    except Exception as e:
        print(f"[ERROR] Failed to append Welcome Tea ID {user_id}: {str(e)}")
        return False
