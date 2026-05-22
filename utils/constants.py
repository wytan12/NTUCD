# Conversation states
ASK_MATRIC = 1234
DATE, EVENT, LOCATION = range(3)
MODIFY_FIELD, MODIFY_VALUE = range(3, 5)
WAITING_TOPIC_TITLE = 50
WAITING_PERF_DETAILS = 51
WAITING_TOPIC_TYPE = 52

# Global state tracking
initialized_topics = set()
OTHERS_THREAD_IDS = set()
pending_questions = {}
active_polls = {}
yes_voters = set()
interest_votes = {}
pending_users = {}
# RAM Cache for Standard Topics: {thread_id: ['TEXT', 'MEDIA', ...]}
# Thread ID 0 or None represents the 'General' topic.
TOPIC_RULES_CACHE = {}