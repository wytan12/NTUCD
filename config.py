import os
import pytz

# Bot Configuration
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

# Google Sheets Configuration
SHEET_NAME = "NTUCD AY25/26 Timeline (Tele Debug)"
SHEET_TAB_NAME = "PERFORMANCE List"
ATTENDANCE_TAB = "ATTENDANCE List"
WELCOME_TEA_SHEET = "NTUCD Welcome Tea Registration 2025 (Responses)"
WELCOME_TEA_TAB = "Form Responses 1"

# Chat Configuration
CHAT_ID = -1002590844000  # Main group Chat ID
# CHAT_ID = -1002614985856  # Debug group Chat ID

# Timezone
sg_tz = pytz.timezone("Asia/Singapore")

# Thread Configuration
GENERAL_TOPIC_ID = None
TOPIC_VOTING_ID = 4
TOPIC_MEDIA_IDS = [25, 75]
TOPIC_BLOCKED_ID = 5  # score

# Exempted Thread IDs
EXEMPTED_THREAD_IDS = [GENERAL_TOPIC_ID, TOPIC_VOTING_ID, TOPIC_BLOCKED_ID, 11] + TOPIC_MEDIA_IDS

# Sheet Columns
SHEET_COLUMNS = ["THREAD ID", "EVENT", "PROPOSED DATE | TIME", "LOCATION", 
                 "PERFORMANCE INFO", "CONFIRMED DATE | TIME", "STATUS"]