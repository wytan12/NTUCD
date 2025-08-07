from enum import IntEnum

class ConversationStates(IntEnum):
    ASK_MATRIC = 1234
    DATE = 0
    EVENT = 1
    LOCATION = 2
    MODIFY_FIELD = 3
    MODIFY_VALUE = 4

class PollTypes:
    TRAINING = "training"
    INTEREST = "interest"

class PerformanceStatus:
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    PENDING = ""