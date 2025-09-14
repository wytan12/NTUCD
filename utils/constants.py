# Conversation states
ASK_MATRIC = 1234
DATE, EVENT, LOCATION = range(3)
MODIFY_FIELD, MODIFY_VALUE = range(3, 5)

# Global state tracking
initialized_topics = set()
OTHERS_THREAD_IDS = set()
pending_questions = {}
active_polls = {}
yes_voters = set()
interest_votes = {}
pending_users = {}