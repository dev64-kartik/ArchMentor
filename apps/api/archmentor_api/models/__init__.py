"""SQLModel entities.

Import side-effect: registers every table on `SQLModel.metadata` so
Alembic autogenerate sees them.
"""

from archmentor_api.models.brain_snapshot import BrainSnapshot
from archmentor_api.models.canvas_snapshot import CanvasSnapshot
from archmentor_api.models.interruption import Interruption
from archmentor_api.models.problem import Problem
from archmentor_api.models.report import Report
from archmentor_api.models.session import InterviewSession
from archmentor_api.models.session_event import SessionEvent, SessionEventType
from archmentor_api.models.user import User

__all__ = [
    "BrainSnapshot",
    "CanvasSnapshot",
    "Interruption",
    "InterviewSession",
    "Problem",
    "Report",
    "SessionEvent",
    "SessionEventType",
    "User",
]
