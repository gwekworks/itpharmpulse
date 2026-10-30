"""Patient prescription transfer requests (USA 21 CFR 1306).

Patient (role=user) submits a transfer request; pharmacists at the two
pharmacies complete the actual pharmacist-to-pharmacist transfer. On submit
we (1) store the patient's authorization, (2) email the patient an HTML
receipt of exactly what was sent, and (3) fax the signed authorization PDF
to both pharmacies via EffyMobile. Fax legs are tracked per pharmacy so
the patient can follow delivery in "My transfer requests".

Mirrors claims.py (throttle, redirect-with-error, _activity log). Email +
fax failures never block the request — they are reported in the receipt.
"""
from __future__ import annotations

import logging
import urllib.parse

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.shortcuts import redirect
from django.utils import timezone as djtz
from django.utils.dateparse import parse_date

from ...models import (
    Notification, Pharmacy, PrescriptionTransfer, TransferFax,
)
from .common import _activity

log = logging.getLogger(__name__)


def _back(base="/transfer", **params):
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v})
    return redirect(f"{base}?{qs}" if qs else base)


@login_required
def submit_transfer(request):
    from ...views_extra import is_rate_limited
    if request.method != "POST":
        return _back(error="Submit the transfer form to create a request.")
    # Throttle: 10 transfer requests per IP per hour (same tier as reviews).
    if is_rate_limited(request, "transfer", limit=10, window_s=3600):
        return _back(error="Too many transfer requests. Try again in an hour.")

    # Owners act as pharmacists; admins moderate — transfers are for patients.
    role = getattr(request.user, "role", "user")
    if role == "admin":
        return _back(error="Admins cannot request transfers. Use a patient account.")
    if role == "pharmacist":
        return _back(
            error="Pharmacy owners cannot request transfers here. "
                  "Complete transfers pharmacist-to-pharmacist per 21 CFR 1306."
        )

    def g(name):
        return (request.POST.get(name) or "").strip()

    if g("consent_given") != "1":
        return _back(error="Review and agree to the transfer notices before submitting.")

    try:
        from_id = int(g("from_pharmacy_id") or 0)
        to_id = int(g("to_pharmacy_id") or 0)
    except (TypeError, ValueError):
        return _back(error="Choose both the current and new pharmacy.")

    if not from_id or not to_id:
        return _back(error="Choose both the current and new pharmacy.")
    if from_id == to_id:
        return _back(error="Sending and receiving pharmacies must be different.")

    from_p = Pharmacy.objects.filter(id=from_id).first()
    to_p = Pharmacy.objects.filter(id=to_id).first()
    if not from_p or not to_p:
        return _back(error="One of the selected pharmacies was not found.")

    patient_first_name = g("patient_first_name")
    patient_last_name = g("patient_last_name")
    patient_name = f"{patient_first_name} {patient_last_name}".strip()
    dob_raw = g("patient_dob")
    patient_phone = g("patient_phone")

    if not (patient_first_name and patient_last_name and dob_raw and patient_phone):
        return _back(error="Patient first name, last name, birth date, and phone are required.")
    dob = parse_date(dob_raw)
    if not dob:
        return _back(error="Birth date must be YYYY-MM-DD.")
    # Patient-initiated request only (not a prescription); explicit consent
    # authorizes delivery and contact at the listed phone number.
    medication_name = ""
    medication_type = "non_controlled"
    signature = patient_name
    from_phone = g("from_phone")
    from_fax = g("from_fax")
    if not from_phone or not from_fax:
        return _back(
            error="Current pharmacy needs both a phone and fax number. "
                  "Please correct the contact before sending.",
            from_pharmacy_id=str(from_id), to_pharmacy_id=str(to_id),
        )

    t = PrescriptionTransfer.objects.create(
        user=request.user,
        from_pharmacy=from_p, to_pharmacy=to_p,
        from_pharmacy_name=from_p.name, to_pharmacy_name=to_p.name,
        from_phone_override=g("from_phone"),
        from_fax_override=g("from_fax"),
        patient_full_name=patient_name, patient_dob=dob,
        patient_phone=patient_phone, patient_address="",
        medication_name="", medication_strength="",
        directions="", rx_number="",
        refills_remaining=None, last_filled_date=None,
        prescriber_name="", prescriber_phone="",
        medication_type=medication_type, consent_given=True,
        patient_signature=signature, notes="", status="pending",
    )
    _activity(
        request, "transfer_submitted",
        f"Transfer request #{t.id} "
        f"{from_p.name} -> {to_p.name} for {patient_name}",
    )
    fax_summary = _fax_transfer_to_pharmacies(request, t, from_p, to_p)
    _email_transfer_receipt(request, t, from_p, to_p, fax_summary)
    return _back(base="/transfers", msg=(
        "Transfer request sent. "
        + ("Fax queued to the current pharmacy. " if fax_summary["queued"] == 1
           else "Check your email receipt for fax status. ")
        + "Track delivery below."
    ))


@login_required
def cancel_transfer(request, transfer_id: int):
    t = PrescriptionTransfer.objects.filter(id=transfer_id, user=request.user).first()
    if not t:
        return _back(error="Transfer request not found.")
    if t.status != "pending":
        return _back(error="Only pending requests can be cancelled.")
    t.status = "cancelled"
    t.save(update_fields=["status", "updated_at"])
    _activity(request, "transfer_cancelled", f"Transfer #{t.id} cancelled by patient")
    return _back(base="/transfers", msg="Transfer request cancelled.")


# ---------------------------------------------------------------------------
# Email receipt + EffyMobile fax legs
# ---------------------------------------------------------------------------

def _transfer_type_label(medication_type: str) -> str:
    return {
        "non_controlled": "Non-controlled",
        "schedule_iii_v": "Schedule III–V (one-time transfer)",
        "schedule_ii": "Schedule II",
    }.get(medication_type, medication_type)


def _email_transfer_receipt(request, t, from_p, to_p, fax_summary) -> bool:
    """HTML receipt to the patient: what was sent, to whom, fax status."""
    user = request.user
    if not user.email:
        return False
    legs = fax_summary["legs"]
    leg_rows = "".join(
        f"<tr><td style='padding:6px 8px;border:1px solid #e5e7eb;'>"
        f"{'Sending' if leg['kind'] == 'from_pharmacy' else 'Receiving'}"
        f" — {leg['pharmacy']}</td>"
        f"<td style='padding:6px 8px;border:1px solid #e5e7eb;'>{leg['detail']}</td></tr>"
        for leg in legs
    )
    med = ""
    html = f"""\
<html><body style="font-family:Arial,sans-serif;color:#0f172a;font-size:14px;">
<p>Hi {user.first_name or 'there'},</p>
<p>Your prescription transfer request <strong>#{t.id}</strong> was sent. Here is exactly what was submitted:</p>
<table style="border-collapse:collapse;margin:12px 0;">
<tr><td style='padding:6px 8px;border:1px solid #e5e7eb;color:#64748b;'>Patient</td><td style='padding:6px 8px;border:1px solid #e5e7eb;'><strong>{t.patient_full_name}</strong></td></tr>
<tr><td style='padding:6px 8px;border:1px solid #e5e7eb;color:#64748b;'>From (current)</td><td style='padding:6px 8px;border:1px solid #e5e7eb;'>{t.from_pharmacy_name}</td></tr>
<tr><td style='padding:6px 8px;border:1px solid #e5e7eb;color:#64748b;'>To (new)</td><td style='padding:6px 8px;border:1px solid #e5e7eb;'>{t.to_pharmacy_name}</td></tr>
</table>
<p style="color:#64748b;font-size:12px;">Not a prescription — patient-initiated request only. PharmacyPulse is not a pharmacy and does not dispense or transfer prescriptions.</p>
<p><strong>Fax status</strong> (request sent to each pharmacy):</p>
<table style="border-collapse:collapse;margin:12px 0;">{leg_rows}</table>
<p style="color:#64748b;font-size:12px;">Track live delivery under “My transfer requests” on the Transfer page. The two pharmacies complete any transfer pharmacist-to-pharmacist — this receipt confirms your request was routed, not that the pharmacies have finished.</p>
<p>— The PharmacyPulse Team</p>
</body></html>"""
    text = (
        f"Hi {user.first_name or 'there'},\n\n"
        f"Your prescription transfer request #{t.id} was sent.\n"
        f"Patient: {t.patient_full_name}\nFrom: {t.from_pharmacy_name}\nTo: {t.to_pharmacy_name}\n"
        + "".join(
            f"{'Sending' if leg['kind'] == 'from_pharmacy' else 'Receiving'} "
            f"({leg['pharmacy']}): {leg['detail']}\n" for leg in legs
        )
        + "\nTrack delivery under My transfer requests. — PharmacyPulse"
    )
    try:
        send_mail(
            subject=f"Transfer request #{t.id} sent — {t.from_pharmacy_name} to {t.to_pharmacy_name}",
            message=text,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            html_message=html,
            fail_silently=False,
        )
        return True
    except Exception as exc:  # never block the transfer on email errors
        log.warning("transfer receipt email failed: %s", exc)
        return False


def _fax_transfer_to_pharmacies(request, t, from_p, to_p) -> dict:
    """Queue an EffyMobile fax leg for the CURRENT (sending) pharmacy only.

    The receiving pharmacy is not faxed. If the sending pharmacy has no
    usable fax, the form is blocked upstream in submit_transfer and here
    we record skipped.
    Returns {"queued": n, "legs": [{"kind", "pharmacy", "detail"}]}.
    """
    from ... import effymobile
    from ...transfer_doc import make_doc_url

    legs = []
    queued = 0
    media_url = make_doc_url(
        settings.SITE_URL, t.id,
    )
    kind, pharm = "from_pharmacy", from_p
    raw_fax = ((getattr(t, "from_fax_override", "") or "").strip()
               or (getattr(pharm, "fax_number", "") or "").strip())
    if not raw_fax:
        TransferFax.objects.create(
            transfer=t, recipient_kind=kind,
            pharmacy_name=pharm.name, to_number="",
            media_url=media_url, status="skipped",
            failure_reason="No fax number on file for this pharmacy.",
        )
        legs.append({"kind": kind, "pharmacy": pharm.name,
                      "detail": "Not faxed — no fax number on file."})
        return {"queued": queued, "legs": legs}
    try:
        to_number = effymobile.normalize_us_fax(raw_fax)
    except ValueError:
        TransferFax.objects.create(
            transfer=t, recipient_kind=kind,
            pharmacy_name=pharm.name, to_number=raw_fax,
            media_url=media_url, status="skipped",
            failure_reason=f"Fax number {raw_fax!r} is not dialable.",
        )
        legs.append({"kind": kind, "pharmacy": pharm.name,
                      "detail": f"Not faxed — invalid fax number ({raw_fax})."})
        return {"queued": queued, "legs": legs}
    try:
        fax = effymobile.send_fax(
            to_number, media_url, quality="high",
            from_display_name="PharmacyPulse",
        )
        TransferFax.objects.create(
            transfer=t, recipient_kind=kind,
            pharmacy_name=pharm.name, to_number=to_number,
            media_url=media_url,
            effy_id=str(fax.get("id", "")),
            fax_id=str(fax.get("fax_id", "")),
            status=effymobile.STATUS_MAP.get(
                str(fax.get("status", "queued")), "queued"),
            page_count=fax.get("page_count"),
            preview_url=str(fax.get("preview_url", "")
                             or fax.get("stored_media_url", "") or ""),
        )
        queued += 1
        legs.append({"kind": kind, "pharmacy": pharm.name,
                      "detail": f"Fax queued to {to_number}."})
        _activity(request, "transfer_fax_queued",
                  f"Transfer #{t.id} fax queued to {pharm.name} ({to_number})")
    except effymobile.EffyDisabledError as exc:
        TransferFax.objects.create(
            transfer=t, recipient_kind=kind,
            pharmacy_name=pharm.name, to_number=to_number,
            media_url=media_url, status="skipped",
            failure_reason=str(exc),
        )
        legs.append({"kind": kind, "pharmacy": pharm.name,
                      "detail": "Not faxed — fax service not configured."})
    except effymobile.EffyApiError as exc:
        TransferFax.objects.create(
            transfer=t, recipient_kind=kind,
            pharmacy_name=pharm.name, to_number=to_number,
            media_url=media_url, status="failed",
            failure_reason=str(exc)[:500],
        )
        legs.append({"kind": kind, "pharmacy": pharm.name,
                      "detail": f"Fax failed to queue: {exc}"})
    return {"queued": queued, "legs": legs}


def _sync_fax_row(row: TransferFax) -> TransferFax:
    """Live-sync one fax row from EffyMobile (non-final rows only)."""
    from ... import effymobile
    if row.is_final or not (row.effy_id or row.fax_id):
        return row
    try:
        fax = effymobile.get_fax(row.effy_id or row.fax_id)
    except Exception as exc:  # surface as failure detail, keep old status
        log.warning("fax live-sync failed for #%s: %s", row.id, exc)
        return row
    row.status = effymobile.STATUS_MAP.get(str(fax.get("status", "")), row.status)
    if fax.get("page_count") is not None:
        try:
            row.page_count = int(fax["page_count"])
        except (TypeError, ValueError):
            pass
    for key in ("failure_reason", "failed_reason", "error"):
        if fax.get(key):
            row.failure_reason = str(fax[key])[:500]
            break
    preview = (fax.get("preview_url") or fax.get("stored_media_url")
               or fax.get("preview_urls") or "")
    if isinstance(preview, list):
        preview = preview[0] if preview else ""
    if preview:
        row.preview_url = str(preview)[:500]
    row.save()
    return row


@login_required
def refresh_transfer_fax(request, fax_id: int):
    """Live-sync one fax leg (owner only) and report back to /transfers."""
    row = TransferFax.objects.filter(
        id=fax_id, transfer__user=request.user).first()
    if not row:
        return _back(base="/transfers", error="Fax record not found.")
    before = row.status
    _sync_fax_row(row)
    if row.status != before:
        _activity(request, "transfer_fax_updated",
                  f"Transfer #{row.transfer_id} fax to {row.pharmacy_name}: "
                  f"{before} → {row.status}")
        if row.is_final and row.status in ("delivered", "failed"):
            Notification.objects.create(
                user=request.user,
                title=f"Fax {row.status}: {row.pharmacy_name}",
                body=(f"Transfer #{row.transfer_id} fax to "
                      f"{row.pharmacy_name} is {row.status}."
                      + (f" Reason: {row.failure_reason}" if row.status == "failed" and row.failure_reason else "")),
            )
    label = {"delivered": "delivered ✓", "failed": "failed",
             "sending": "sending…", "processing": "processing…",
             "queued": "queued…", "canceled": "canceled"}.get(
                 row.status, row.status)
    extra = f" {row.failure_reason}" if row.status == "failed" and row.failure_reason else ""
    return _back(base="/transfers", msg=f"Fax to {row.pharmacy_name}: {label}.{extra}"[:500])


@login_required
def cancel_transfer_fax(request, fax_id: int):
    """Cancel an in-flight fax leg (owner only; no-op once final)."""
    from ... import effymobile
    row = TransferFax.objects.filter(
        id=fax_id, transfer__user=request.user).first()
    if not row:
        return _back(base="/transfers", error="Fax record not found.")
    if row.is_final:
        return _back(base="/transfers", error="That fax is already final and cannot be cancelled.")
    if not (row.effy_id or row.fax_id):
        row.status = "canceled"
        row.save(update_fields=["status", "updated_at"])
        return _back(base="/transfers", msg=f"Fax to {row.pharmacy_name} cancelled.")
    try:
        effymobile.cancel_fax(row.effy_id or row.fax_id)
    except Exception as exc:
        return _back(base="/transfers", error=f"Cancel failed: {exc}"[:300])
    _sync_fax_row(row)
    if not row.is_final:
        row.status = "canceled"
        row.save(update_fields=["status", "updated_at"])
    _activity(request, "transfer_fax_updated",
              f"Transfer #{row.transfer_id} fax to {row.pharmacy_name} cancelled by patient")
    return _back(base="/transfers", msg=f"Fax to {row.pharmacy_name} cancelled.")
