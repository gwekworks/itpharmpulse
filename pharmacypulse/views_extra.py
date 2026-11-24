"""Operational endpoints + rate-limit helper."""
from __future__ import annotations

import hashlib

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from .geo import client_ip


def is_rate_limited(request, action: str, *, limit: int = 20, window_s: int = 3600) -> bool:
    """Return True if the (IP, action) bucket has exceeded `limit` calls in
    the window. Increments the counter on each call. Same backing-cache
    pattern as the login throttle.

    Defaults: 20 per hour per IP — well above human use, blocks basic spam bots.
    """
    ip_hash = hashlib.sha256(client_ip(request).encode()).hexdigest()[:16]
    key = f"rl:{action}:{ip_hash}"
    current = cache.get(key)
    if current is None:
        cache.set(key, 1, timeout=window_s)
        return False
    if current >= limit:
        return True
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=window_s)
    return False


@csrf_exempt
@require_GET
def healthcheck(request):
    """JSON ping that confirms DB connectivity. Heroku / uptime monitors poll
    this; failure → 503, lets Heroku auto-restart the dyno."""
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return JsonResponse({"status": "ok", "db": "ok"}, status=200)
    except Exception as e:  # noqa: BLE001
        return JsonResponse({"status": "error", "db": str(e)[:200]}, status=503)


@require_GET
def transfer_doc(request, transfer_id: int):
    """Serve a transfer authorization PDF. Login-free so the fax provider can
    fetch it — access is gated by the signed `?s=` token (90-day validity),
    same pattern as the public newsletter-unsubscribe link."""
    from django.http import FileResponse, HttpResponseForbidden
    from .models import PrescriptionTransfer
    from .transfer_doc import build_transfer_pdf, unsign_doc_token

    try:
        if unsign_doc_token(request.GET.get("s", "")) != transfer_id:
            raise ValueError("token mismatch")
    except Exception:
        return HttpResponseForbidden("Invalid or expired document link.")
    t = (PrescriptionTransfer.objects
         .select_related("from_pharmacy", "to_pharmacy")
         .filter(id=transfer_id).first())
    if not t:
        return HttpResponseForbidden("Not found.")
    import io
    pdf = build_transfer_pdf(t)
    resp = FileResponse(io.BytesIO(pdf), content_type="application/pdf")
    resp["Content-Disposition"] = (
        f'inline; filename="transfer-authorization-{t.id}.pdf"')
    return resp


@login_required
@require_GET
def pharmacy_qr_svg(request, pharmacy_id: int):
    """Download an SVG QR code for a pharmacy the user manages."""
    import base64
    from django.conf import settings
    from django.http import Http404, HttpResponse
    from django.urls import reverse
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from xml.sax.saxutils import escape

    from .models import Pharmacy, PharmacyClaim
    from .text_utils import pharmacy_slug

    pharmacy = Pharmacy.objects.filter(id=pharmacy_id).first()
    if not pharmacy:
        raise Http404

    has_approved_claim = PharmacyClaim.objects.filter(
        pharmacy=pharmacy,
        user=request.user,
        status="approved",
    ).exists()
    is_accepted_team_member = Pharmacy.objects.filter(
        id=pharmacy_id,
        org__team_members__user=request.user,
        org__team_members__accepted_at__isnull=False,
    ).exists()
    if not (has_approved_claim or is_accepted_team_member):
        raise Http404

    public_url = request.build_absolute_uri(reverse(
        "pharmacy_slug",
        kwargs={
            "pharmacy_id": pharmacy.id,
            "slug": pharmacy_slug(pharmacy.name),
        },
    ))
    qr = QrCodeWidget(public_url, barLevel="H")
    qr.qr.make()
    modules = qr.qr.modules
    module_count = qr.qr.getModuleCount()
    qr_size = 1024
    quiet_zone = 4
    module_size = qr_size // (module_count + quiet_zone * 2)
    code_size = module_size * (module_count + quiet_zone * 2)
    offset = (qr_size - code_size) // 2
    qr_modules = []
    for row, values in enumerate(modules):
        for column, is_dark in enumerate(values):
            if is_dark:
                left = offset + (column + quiet_zone) * module_size
                top = offset + (row + quiet_zone) * module_size
                qr_modules.append(
                    f"M{left},{top}h{module_size}v{module_size}"
                    f"h-{module_size}z"
                )

    logo_path = (
        settings.BASE_DIR / "pharmacypulse" / "static" / "pharmacypulse"
        / "logo.svg"
    )
    logo_data = base64.b64encode(logo_path.read_bytes()).decode("ascii")
    logo_x, logo_y, logo_width, logo_height = 300, 469, 424, 86
    logo_backing = (
        '<rect x="287" y="457" width="450" height="110" rx="14" '
        'fill="white"/>'
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{qr_size}" height="{qr_size}" '
        f'viewBox="0 0 {qr_size} {qr_size}" role="img">'
        f'<title>QR code for {escape(pharmacy.name)}</title>'
        f'<rect width="{qr_size}" height="{qr_size}" fill="white"/>'
        f'<path d="{"".join(qr_modules)}" fill="#0f172a"/>'
        f'{logo_backing}'
        f'<image x="{logo_x}" y="{logo_y}" width="{logo_width}" '
        f'height="{logo_height}" preserveAspectRatio="xMidYMid meet" '
        f'href="data:image/svg+xml;base64,{logo_data}" '
        f'xlink:href="data:image/svg+xml;base64,{logo_data}"/>'
        f'</svg>'
    )
    response = HttpResponse(svg, content_type="image/svg+xml")
    response["Content-Disposition"] = (
        f'attachment; filename="pharmacy-{pharmacy.id}-qr-code.svg"')
    return response
