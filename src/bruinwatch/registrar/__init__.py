"""Framework-free access to the UCLA Schedule of Classes."""

from .client import RegistrarClient, RegistrarError
from .types import (
    Course,
    EnrollmentNumbers,
    EnrollmentStatus,
    Section,
    SubjectArea,
    Term,
    WaitlistStatus,
)

__all__ = [
    "Course",
    "EnrollmentNumbers",
    "EnrollmentStatus",
    "RegistrarClient",
    "RegistrarError",
    "Section",
    "SubjectArea",
    "Term",
    "WaitlistStatus",
]
