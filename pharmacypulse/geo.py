"""Per-request location inference for personalized pharmacy results.

Resolution order (most-specific → least):
  1. `?zip=X` GET param (explicit override)
  2. `?lat=X&lng=Y` GET param (browser geolocation)
  3. `pp_loc` cookie set by the homepage's geolocation prompt
  4. authed user's stored `zip_code` field
  5. IP-based geolocation via ipapi.co (cached 24h per IP-hash)

Returns a `Location` namedtuple with whichever fields could be resolved.
Templates / page_X functions read `.zip` for ZIP-prefix filtering and
`.lat/.lng` for distance-based sorting.

Privacy: we never persist the raw IP. The cache key is sha256(IP)[:16] so
the only thing on disk is an opaque token mapped to a coarse city/ZIP.
The cache TTL is 24h.
"""
from __future__ import annotations

import hashlib
import logging
from typing import NamedTuple, Optional

import requests
from django.core.cache import cache

log = logging.getLogger(__name__)


class Location(NamedTuple):
    zip: str = ""
    city: str = ""
    state: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None
    source: str = ""  # 'param' | 'cookie' | 'user' | 'ip' | ''


_EMPTY = Location()


def _client_ip(request) -> str:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _is_private(ip: str) -> bool:
    """Skip lookups for localhost / RFC1918 / IPv6 link-local — they'll never
    resolve to a useful location and we don't want to spam the API."""
    if not ip:
        return True
    if ip.startswith(("127.", "10.", "192.168.", "169.254.", "::1", "fe80:")):
        return True
    if ip.startswith("172."):
        try:
            second = int(ip.split(".")[1])
            return 16 <= second <= 31
        except (ValueError, IndexError):
            return False
    return False


def _ip_lookup_full(ip: str) -> Optional[dict]:
    """Resolve an IP to a full ipapi.co response (location + country_code).
    Cached for 24h keyed by sha256(IP)[:16]. Returns None for unresolvable
    (private, lookup failed) — distinct from the ipapi-says-no-data case
    which is a non-None dict with empty fields."""
    if _is_private(ip):
        return None
    key = "ipfull:" + hashlib.sha256(ip.encode()).hexdigest()[:16]
    cached = cache.get(key)
    if cached is not None:
        return cached or None  # empty dict cached → known unresolvable
    try:
        r = requests.get(
            f"https://ipapi.co/{ip}/json/",
            headers={"User-Agent": "PharmacyPulse/1.0"},
            timeout=2,
        )
        if r.status_code != 200:
            cache.set(key, {}, timeout=3600)
            return None
        data = r.json()
        if data.get("error"):
            cache.set(key, {}, timeout=3600)
            return None
        cache.set(key, data, timeout=86400)
        return data
    except (requests.RequestException, ValueError) as e:
        log.warning("ip geolocation failed for hashed-ip: %s", e)
        cache.set(key, {}, timeout=600)
        return None


def _ip_lookup(ip: str) -> Optional[Location]:
    """Resolve an IP to a Location. Backed by _ip_lookup_full."""
    data = _ip_lookup_full(ip)
    if not data:
        return None
    return Location(
        zip=data.get("postal", "") or "",
        city=data.get("city", "") or "",
        state=data.get("region_code", "") or "",
        lat=data.get("latitude"),
        lng=data.get("longitude"),
        source="ip",
    )


def country_for_ip(ip: str) -> str:
    """Two-letter country code for an IP, or empty string if unresolvable.
    Empty result means 'don't block' (fail-open) — used by the geo-block
    middleware. Private IPs return ''."""
    data = _ip_lookup_full(ip)
    if not data:
        return ""
    return (data.get("country_code") or "").upper()


def client_ip(request) -> str:
    """Extract the client IP from the request, honoring X-Forwarded-For
    when present (we're behind Heroku's router which always sets it)."""
    return _client_ip(request)


def resolve(request) -> Location:
    """Best-effort location for the current request. Always returns a Location;
    fields are empty if no source could populate them.

    Priority for an AUTHED user:
      1. Explicit ?zip / ?lat&lng GET param  (per-request override)
      2. Authed user's profile zip_code      (their persistent setting)
      3. pp_loc cookie                       (browser geolocation prompt)
      4. IP fallback

    For an UNAUTHED user: same chain, just skips #2.

    The auth-zip-over-cookie priority is intentional: a logged-in user has
    explicitly told us where they are. The geolocation prompt cookie can
    drift (set on a different device, on a VPN, etc.) and shouldn't
    override the user's stated preference."""
    zip_q = (request.GET.get("zip") or "").strip()
    zip_param = zip_q[:5] if (zip_q and zip_q.isdigit() and len(zip_q) >= 5) else ""

    # Parse explicit ?lat= / ?lng= GET params.
    lat_q, lng_q = None, None
    try:
        lat_q = float(request.GET.get("lat", ""))
        lng_q = float(request.GET.get("lng", ""))
    except (TypeError, ValueError):
        pass

    # Parse pp_loc cookie.
    cookie = request.COOKIES.get("pp_loc", "")
    lat_c, lng_c = None, None
    if "," in cookie:
        try:
            c_lat, c_lng = cookie.split(",", 1)
            lat_c, lng_c = float(c_lat), float(c_lng)
        except ValueError:
            pass

    lat_res = lat_q if lat_q is not None else lat_c
    lng_res = lng_q if lng_q is not None else lng_c
    coord_src = "param" if lat_q is not None else ("cookie" if lat_c is not None else "")

    if zip_param:
        return Location(
            zip=zip_param,
            lat=lat_res,
            lng=lng_res,
            source="param"
        )

    if lat_q is not None and lng_q is not None:
        return Location(lat=lat_q, lng=lng_q, source="param")

    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        z = (getattr(user, "zip_code", "") or "").strip()
        if z and z[:5].isdigit():
            return Location(zip=z[:5], lat=lat_res, lng=lng_res, source="user")

    if lat_c is not None and lng_c is not None:
        return Location(lat=lat_c, lng=lng_c, source="cookie")

    ip = _client_ip(request)
    ip_loc = _ip_lookup(ip)
    if ip_loc is not None:
        if lat_res is not None and lng_res is not None and (ip_loc.lat is None or ip_loc.lng is None):
            return Location(
                zip=ip_loc.zip, city=ip_loc.city, state=ip_loc.state,
                lat=lat_res, lng=lng_res, source=coord_src or ip_loc.source
            )
        return ip_loc

    if lat_res is not None and lng_res is not None:
        return Location(lat=lat_res, lng=lng_res, source=coord_src)

    return _EMPTY
