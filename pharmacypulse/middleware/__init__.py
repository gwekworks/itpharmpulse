"""Middleware package.

Splits the former single middleware.py into logical modules. This __init__
re-exports both middleware classes so settings' dotted paths
(`pharmacypulse.middleware.GeoBlockMiddleware`) keep working unchanged.
"""
from .csp import ContentSecurityPolicyMiddleware
from .geoblock import GeoBlockMiddleware

__all__ = ["ContentSecurityPolicyMiddleware", "GeoBlockMiddleware"]