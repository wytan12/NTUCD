import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from config import (GOOGLE_CREDENTIALS_JSON, SHEET_NAME, SHEET_TAB_NAME, 
                   ATTENDANCE_TAB, WELCOME_TEA_SHEET, WELCOME_TEA_TAB)
from utils.constants import OTHERS_THREAD_IDS

def get_gspread_sheet(sheet_name=SHEET_NAME, tab_name=SHEET_TAB_NAME):
    """Return a gspread Worksheet object for the given sheet and tab.

    Creates a fresh OAuth2 client on every call — no session is reused, so
    callers should avoid calling this in tight loops.
    """
    # creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds_dict = GOOGLE_CREDENTIALS_JSON
    scope = ["https://spreadsheets.google.com/feeds", 
             "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client.open(sheet_name).worksheet(tab_name)

def get_attendance_ws():
    """Get attendance worksheet"""
    return get_gspread_sheet(ATTENDANCE_TAB)

def load_topic_rules():
    """Fetches rules from 'STANDARD TOPIC Rules' tab and caches them as lists."""
    try:
        from config import SHEET_NAME
        from utils.constants import TOPIC_RULES_CACHE
        
        # Using your preferred tab name
        sheet = get_gspread_sheet(sheet_name=SHEET_NAME, tab_name="STANDARD TOPIC Rules")
        records = sheet.get_all_records()
        
        TOPIC_RULES_CACHE.clear()
        for row in records:
            tid_raw = str(row.get("THREAD ID", "")).strip()
            
            # Convert ID: '0' or empty becomes None (General Topic)
            if tid_raw == "0" or not tid_raw:
                tid = None
            else:
                try: tid = int(tid_raw)
                except: continue 
                
            raw_rules = row.get("RULE PROFILE", "")
            # Split comma-string into a clean list of uppercase tags
            rule_list = [r.strip().upper() for r in str(raw_rules).split(",") if r.strip()]
            TOPIC_RULES_CACHE[tid] = rule_list
            
        print(f"[INFO] Successfully cached {len(TOPIC_RULES_CACHE)} standard topic rules.")
    except Exception as e:
        print(f"[ERROR] Failed to load topic rules: {e}")

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

def append_poll(record: dict):
    """Append poll to attendance sheet"""
    from utils.helpers import _date_label_from_display
    
    ws = get_attendance_ws()
    _find_or_create_header_rows(ws)

    ws.update_acell("A1", "Tele Poll ID")
    ws.update_acell("A2", "Training Date")

    poll_id = str(record["poll_id"])
    date_display = record.get("date", "")
    date_short = _date_label_from_display(date_display)

    col = _find_or_create_date_column(ws, date_short)
    ws.update_cell(1, col, poll_id)

def _find_or_create_header_rows(ws):
    """Ensure header rows exist"""
    values = ws.get_all_values()
    if len(values) < 2:
        need = 2 - len(values)
        for _ in range(need):
            ws.append_row([""])
        values = ws.get_all_values()

    if not values or not values[0] or (values[0][0] or "").strip() != "Tele Poll ID":
        ws.update_acell("A1", "Tele Poll ID")
    values = ws.get_all_values()
    if len(values) < 2 or not values[1] or (values[1][0] or "").strip() != "Training Date":
        ws.update_acell("A2", "Training Date")

def _get_existing_header_row(ws, row_idx: int) -> list[str]:
    """Get existing header row"""
    return ws.row_values(row_idx)

def _find_or_create_date_column(ws, short_label: str) -> int:
    """Find or create date column"""
    header_row = _get_existing_header_row(ws, 2)
    if not header_row:
        header_row = ["Training Date"]
        ws.update_acell("A2", "Training Date")

    for idx, val in enumerate(header_row, start=1):
        if (val or "").strip().lower() == short_label.strip().lower():
            return idx

    next_col = len(header_row) + 1
    ws.update_cell(2, next_col, short_label)
    return next_col

def append_standard_topic_to_sheet(tid, name, rules_string):
    """Adds a new standard topic row to the 'STANDARD TOPIC Rules' tab."""
    try:
        from config import SHEET_NAME
        sheet = get_gspread_sheet(sheet_name=SHEET_NAME, tab_name="STANDARD TOPIC Rules")
        sheet.append_row([str(tid), name, rules_string])
        print(f"✅ [GSheet] Successfully logged {name} to Rules sheet.")
    except Exception as e:
        print(f"❌ [GSheet ERROR] Failed to append standard topic: {e}")