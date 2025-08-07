import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from config.settings import GOOGLE_CREDENTIALS_JSON, SHEET_NAME, SHEET_TAB_NAME, WELCOME_TEA_SHEET

class SheetsService:
    def __init__(self):
        self.scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        self.creds = self._get_credentials()
        self.client = gspread.authorize(self.creds)
    
    def _get_credentials(self):
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        return ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, self.scope)
    
    def get_performance_sheet(self, tab_name=SHEET_TAB_NAME):
        return self.client.open(SHEET_NAME).worksheet(tab_name)
    
    def get_welcome_tea_sheet(self, tab_name="Form Responses 1"):
        return self.client.open(WELCOME_TEA_SHEET).worksheet(tab_name)
    
    def append_to_others_list(self, thread_id):
        try:
            sheet = self.get_performance_sheet("OTHERS List")
            sheet.append_row([thread_id])
            print(f"[INFO] Thread ID {thread_id} written to OTHERS List.")
            return True
        except Exception as e:
            print(f"[ERROR] Failed to write Thread ID {thread_id} to OTHERS List: {e}")
            return False

# Global instance
sheets_service = SheetsService()