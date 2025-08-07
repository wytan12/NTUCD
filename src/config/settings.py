import os
from typing import List

# Bot Configuration
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

# Sheet Configuration
SHEET_NAME = "NTUCD AY25/26 Timeline"
SHEET_TAB_NAME = "PERFORMANCE List"
WELCOME_TEA_SHEET = "NTUCD Welcome Tea Registration 2025 (Responses)"

# Chat Configuration
CHAT_ID = -1002590844000
GENERAL_TOPIC_ID = None
TOPIC_VOTING_ID = 4
TOPIC_MEDIA_IDS = [25, 75]
TOPIC_BLOCKED_ID = 5
EXEMPTED_THREAD_IDS = [GENERAL_TOPIC_ID, TOPIC_VOTING_ID, TOPIC_BLOCKED_ID, 11] + TOPIC_MEDIA_IDS

# Sheet Columns
SHEET_COLUMNS = ["THREAD ID", "EVENT", "PROPOSED DATE | TIME", "LOCATION", "PERFORMANCE INFO", "CONFIRMED DATE | TIME", "STATUS"]

# Form Links
WELCOME_TEA_FORM = "https://docs.google.com/forms/d/e/1FAIpQLSdZkIn2NC3TkLCLJpgB-jynKSlAKZg_vqw0bu3vywu4tqTzIg/viewform?usp=header"