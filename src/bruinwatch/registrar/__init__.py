"""Framework-free access to the UCLA Schedule of Classes."""

from .client import RegistrarBlocked, RegistrarClient, RegistrarError, RegistrarRateLimited
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
    "RegistrarBlocked",
    "RegistrarClient",
    "RegistrarError",
    "RegistrarRateLimited",
    "Section",
    "SubjectArea",
    "Term",
    "WaitlistStatus",
]
