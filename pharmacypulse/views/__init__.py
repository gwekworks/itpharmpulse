"""Page views package.

Splits the former single views.py into logical modules. This __init__ re-exports
every view so existing imports (`from . import views; views.xxx`, or the url
module's `views.index`) keep working unchanged.
"""
from .seo import robots_txt, sitemap_xml
from .public import (
    compare,
    for_pharmacies,
    index,
    insights,
    online_pharmacies,
    pharmacy_detail,
    pharmacy_list,
    pharmacy_map,
    privacy,
    reviews,
    shortages,
    terms,
    widget,
    write_review,
)
from .member import account, claim, home, pharmacist_dashboard
from .admin import (
    admin_analytics,
    admin_claims,
    admin_dashboard,
    admin_moderation,
    admin_pharmacies,
    admin_pharmacy_facts,
    admin_shortages,
    admin_users,
)

__all__ = [
    "robots_txt",
    "sitemap_xml",
    "index",
    "pharmacy_list",
    "pharmacy_map",
    "online_pharmacies",
    "pharmacy_detail",
    "reviews",
    "write_review",
    "compare",
    "shortages",
    "insights",
    "for_pharmacies",
    "widget",
    "privacy",
    "terms",
    "home",
    "account",
    "claim",
    "pharmacist_dashboard",
    "admin_dashboard",
    "admin_analytics",
    "admin_claims",
    "admin_moderation",
    "admin_pharmacies",
    "admin_shortages",
    "admin_users",
    "admin_pharmacy_facts",
]