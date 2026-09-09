"""ORM models.

Importing this package registers all models on ``Base.metadata`` so database
initialisation can create the full schema.
"""

from backend.memory.vector_store import EmbeddingRow  # noqa: F401
from backend.models.activity_record import ActivityRecord  # noqa: F401
from backend.models.audit_record import AuditRecord  # noqa: F401
from backend.models.conversation import Conversation
from backend.models.fact import Fact
from backend.models.message import Message
from backend.models.monitor_trigger import MonitorTriggerRecord
from backend.models.notification import NotificationRecord
from backend.models.project import Project
from backend.models.scheduled_task import ScheduledTaskRecord
from backend.models.session import SessionRecord
from backend.models.task_run import TaskRunRecord
from backend.models.trigger_fire import TriggerFireRecord
from backend.models.user import User

__all__ = [
    "ActivityRecord",
    "AuditRecord",
    "Conversation",
    "Fact",
    "Message",
    "MonitorTriggerRecord",
    "NotificationRecord",
    "Project",
    "ScheduledTaskRecord",
    "SessionRecord",
    "TaskRunRecord",
    "TriggerFireRecord",
    "User",
    "EmbeddingRow",
]
