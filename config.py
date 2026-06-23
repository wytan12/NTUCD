import os
import pytz

# Bot Configuration
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

# Google Sheets Configuration
SHEET_NAME = "NTUFD AY26/27 Database" # Main GSheet
#SHEET_NAME = "NTUFD AY26/27 Timeline (Tele Bot Debug)" # Debug GSheet
SHEET_TAB_NAME = "PERF"
ATTENDANCE_TAB = "ATTENDANCE"
MEMBER_INFO_TAB = "MEMBER INFO AY26/27"  # holds Nickname (col B) + Tele ID (col P)

# Chat Configuration
CHAT_ID =  -1004406293828 # Main group Chat ID (AY26/27)
#CHAT_ID = -1002614985856  # Debug group Chat ID
WELCOME_TEA_GROUP_CHAT_ID = -1004392666592

# Thread Configuration
GENERAL_TOPIC_ID = None
TOPIC_VOTING_ID = 53  # Main group voting thread ID
# TOPIC_VOTING_ID = 5   # Debug group voting thread ID

# --- Attendance tab layout (1-based row/column indices) ---
# Col A = member NAME (nickname), Col B = TOTAL, Col C = row labels
# ("POLL ID" in row 1, "TRAINING DATE [REG]" in row 2),
# Cols D onward = one training date each (row 1 = poll id, row 2 = date, rows 3+ = "1"/blank)
ATT_NAME_COL = 1          # A
ATT_TOTAL_COL = 2         # B
ATT_LABEL_COL = 3         # C
ATT_FIRST_DATE_COL = 4    # D
ATT_POLL_ROW = 1
ATT_DATE_ROW = 2
ATT_FIRST_MEMBER_ROW = 3

# --- PERF TABULATION tab layout (performance-event attendance) ---
# Same grid shape as the Attendance tab, but each event column is keyed by the
# performance topic's THREAD ID (row 1) instead of a poll id, and row 2 holds the
# EVENT NAME. No training dates, no auto-poll, no modify-date here.
# Col A = member nickname (admin pre-fills the roster), Col B = Tabulation
# (sheet formula, bot never writes), Col C = labels, Cols D+ = one event each.
PERF_TAB_NAME = "PERF TABULATION"
PERF_NAME_COL = 1          # A
PERF_TOTAL_COL = 2         # B
PERF_LABEL_COL = 3         # C
PERF_FIRST_EVENT_COL = 4   # D
PERF_THREAD_ROW = 1        # row 1: thread id per event column
PERF_EVENT_ROW = 2         # row 2: event name per event column
PERF_FIRST_MEMBER_ROW = 3

WELCOME_TEA_SHEET = "NTUFD Welcome Tea Registration 2026 (Responses)"
WELCOME_TEA_TAB = "Form Responses 1"
WELCOME_TEA_ID_TAB = "WELCOME TEA"
WELCOME_TEA_STATUS_NOT_CONFIRM = "Not Confirm"
WELCOME_TEA_STATUS_ATTEND = "Attend"
WELCOME_TEA_STATUS_REJECT = "Reject"

# Timezone
sg_tz = pytz.timezone("Asia/Singapore")

# Sheet Columns
SHEET_COLUMNS = ["THREAD ID", "EVENT TYPE", "EVENT NAME", "REHEARSAL DATE | TIME", "PERF DATE | TIME", "LOCATION",
                 "OTHER INFO", "REMUNATION", "STATUS", "SUMMARY MSG ID"]

# --- Admin tiers (role-driven from MEMBER INFO Role/Position + Tele ID) ---
# MAIN admins: dashboard access + receive every admin alert/reminder DM.
# SECONDARY admins: dashboard access only (no alert/reminder DMs).
# Matching is case-insensitive "contains" — "vice chairperson" matches via
# "chairperson", "Logistics Head" via "logistic", etc.
MAIN_ADMIN_ROLE_KEYWORDS = ("chairperson", "secretary", "sde")
SECONDARY_ADMIN_ROLE_KEYWORDS = ("treasurer", "logistic", "business", "publications", "coach")

# EMERGENCY fallback only: grants dashboard access / receives alerts ONLY when
# the role lookup yields nothing at all (sheet unreachable / Role column wiped).
ADMIN_DM_USER_IDS = {1505249420, 903855240, 362804048}

# Last-resort "contact our admin" Tele ID for the join flow — used only when no
# MAIN admin is resolvable from the sheet (get_join_contact_admin_id).
JOIN_CONTACT_ADMIN_ID = 1505249420

# Join requests via an invite link whose NAME contains this keyword
# (case-insensitive) get the Welcome Tea recruitment flow; any other link runs
# the returning-member Tele-ID gate.
WELCOME_TEA_LINK_KEYWORD = "tea"
