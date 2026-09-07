"""Form package.

Splits the former single forms.py into logical modules. This __init__ re-exports
every form so existing imports (`from .forms import SignupForm`) keep working.
"""
from .auth import LoginForm, SignupForm
from .reviews import WriteReviewForm
from .claims import ClaimPharmacyForm

__all__ = ["LoginForm", "SignupForm", "WriteReviewForm", "ClaimPharmacyForm"]