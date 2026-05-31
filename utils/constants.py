# Conversation states
ASK_MATRIC = 1234
DATE, EVENT, LOCATION = range(3)
MODIFY_FIELD, MODIFY_VALUE = range(3, 5)
WAITING_TOPIC_TITLE = 50
WAITING_PERF_DETAILS = 51
WAITING_TOPIC_TYPE = 52
WAITING_EVENT_TYPE = 53
PERF_EVENT_NAME = 54
PERF_REHEARSAL = 55
PERF_DATE = 56
PERF_LOCATION = 57
PERF_OTHER_INFO = 58

# Global state tracking
initialized_topics = set()
OTHERS_THREAD_IDS = set()
pending_questions = {}
active_polls = {}
yes_voters = set()
interest_votes = {}
pending_users = {}