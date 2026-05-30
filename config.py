import os
import pytz

# Bot Configuration
# BOT_TOKEN = os.environ.get("BOT_TOKEN")
# GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

BOT_TOKEN = ""
GOOGLE_CREDENTIALS_JSON = {
  
}

# Google Sheets Configuration
SHEET_NAME = "NTUFD AY26/27 Timeline (Tele Bot Debug)"
SHEET_TAB_NAME = "PERF"
ATTENDANCE_TAB = "ATTENDANCE"
WELCOME_TEA_SHEET = "NTUCD Welcome Tea Registration 2025 (Responses)"
WELCOME_TEA_TAB = "Form Responses 1"

# Chat Configuration
# CHAT_ID =  # Main group Chat ID
CHAT_ID = -1002614985856  # Debug group Chat ID

# Timezone
sg_tz = pytz.timezone("Asia/Singapore")

# Thread Configuration
GENERAL_TOPIC_ID = None
# TOPIC_VOTING_ID = 4  # Main group voting thread ID
TOPIC_VOTING_ID = 5   # Debug group voting thread ID

# Threads exempt from /remind and /modify (non-performance threads)
EXEMPTED_THREAD_IDS = [GENERAL_TOPIC_ID, TOPIC_VOTING_ID]

# Sheet Columns
SHEET_COLUMNS = ["THREAD ID", "EVENT TYPE", "EVENT NAME", "REHEARSAL DATE | TIME", "PERF DATE | TIME", "LOCATION",
                 "OTHER INFO", "REMUNATION", "STATUS", "SUMMARY MSG ID"]

# Telegram user IDs allowed to control the bot via private DM (WY, WF, PH, MN, JURI)
ADMIN_DM_USER_IDS = {}
