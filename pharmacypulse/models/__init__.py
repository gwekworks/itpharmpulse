"""Model package for the pharmacypulse app.

Splits the former single models.py into logical modules so related models
live together. This __init__ re-exports every model so existing imports
(`from pharmacypulse.models import Pharmacy`, `from ..models import ...`) keep
working unchanged.
"""
from .user import (
    ActivityLog,
    DataRequest,
    Notification,
    User,
    UserConsent,
)
from .organization import (
    PharmacyOrg,
    PharmacyTeamMember,
)
from .pharmacy import (
    Pharmacy,
    PharmacyCoveragePlan,
    PharmacyHours,
    PharmacyInsuranceProvider,
    PharmacyPublishable,
    PharmacyService,
    ZipCentroid,
)
from .reviews import (
    ResponseCount,
    Review,
    ReviewResponse,
)
from .claims import (
    PharmacyClaim,
)
from .shortages import (
    DrugShortage,
    PinnedShortage,
)
from .community import (
    ModerationKeyword,
    NewsletterSubscriber,
    SavedComparison,
)
from .facts import PharmacyFact

__all__ = [
    "User",
    "Notification",
    "ActivityLog",
    "DataRequest",
    "UserConsent",
    "PharmacyOrg",
    "PharmacyTeamMember",
    "Pharmacy",
    "PharmacyCoveragePlan",
    "PharmacyPublishable",
    "PharmacyHours",
    "PharmacyInsuranceProvider",
    "PharmacyService",
    "ZipCentroid",
    "Review",
    "ReviewResponse",
    "ResponseCount",
    "PharmacyClaim",
    "DrugShortage",
    "PinnedShortage",
    "ModerationKeyword",
    "NewsletterSubscriber",
    "SavedComparison",
    "PharmacyFact",
]