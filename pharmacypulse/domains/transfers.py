"""Prescription transfer page provider: /transfer (patient only)."""
from __future__ import annotations

from .common import Row, with_get_params, _safe_int, _pharmacy_dict
from ..models import Pharmacy, PrescriptionTransfer


@with_get_params
def page_transfer(request):
    """Data for transfer.html (the request form). Prefills pharmacies from
    ?from_id / ?to_id and patient fields from profile + last request."""
    from_id = _safe_int(request.GET.get("from_id") or request.GET.get("from_pharmacy_id"))
    to_id = _safe_int(request.GET.get("to_id") or request.GET.get("to_pharmacy_id"))
    from_p = Pharmacy.objects.filter(id=from_id).first() if from_id else None
    to_p = Pharmacy.objects.filter(id=to_id).first() if to_id else None

    prefill_name = ""
    prefill_phone = ""
    prefill_dob = ""
    prefill_address = ""
    transfer_count = 0
    if request.user.is_authenticated:
        u = request.user
        full_name = f"{u.first_name or ''} {u.last_name or ''}".strip()
        prefill_name = full_name
        prefill_phone = u.phone or ""
        # User stores only name/phone/ZIP — DOB + street address come from
        # the patient's most recent transfer request, if any.
        latest = (PrescriptionTransfer.objects.filter(user=u)
                  .order_by("-created_at").first())
        if latest:
            if not prefill_name:
                prefill_name = latest.patient_full_name or ""
            if not prefill_phone:
                prefill_phone = latest.patient_phone or ""
            if latest.patient_dob:
                prefill_dob = latest.patient_dob.isoformat()
            prefill_address = latest.patient_address or ""
        if not prefill_address:
            prefill_address = u.zip_code or ""
        transfer_count = PrescriptionTransfer.objects.filter(user=u).count()

    return {
        "from_pharmacy": Row(_pharmacy_dict(from_p)) if from_p else None,
        "to_pharmacy": Row(_pharmacy_dict(to_p)) if to_p else None,
        "from_id": from_p.id if from_p else "",
        "to_id": to_p.id if to_p else "",
        "from_name": from_p.name if from_p else "",
        "to_name": to_p.name if to_p else "",
        "prefill_name": prefill_name,
        "prefill_phone": prefill_phone,
        "prefill_dob": prefill_dob,
        "prefill_address": prefill_address,
        "transfer_count": transfer_count,
        "error": request.GET.get("error"),
        "msg": request.GET.get("msg"),
    }


def _my_transfers_list(user):
    """Newest-first transfer rows with per-pharmacy fax legs."""
    rows = []
    qs = (PrescriptionTransfer.objects.filter(user=user)
          .prefetch_related("faxes")
          .order_by("-created_at")[:30])
    for t in qs:
        rows.append({
            "id": t.id,
            "status": t.status,
            "medication_name": t.medication_name,
            "medication_strength": t.medication_strength,
            "medication_type": t.medication_type,
            "from_pharmacy_name": t.from_pharmacy_name,
            "to_pharmacy_name": t.to_pharmacy_name,
            "rx_number": t.rx_number,
            "created_at": t.created_at,
                "faxes": [{
                    "id": f.id,
                    "kind": f.recipient_kind,
                    "pharmacy": f.pharmacy_name,
                    "to_number": f.to_number,
                    "status": f.status,
                    "is_final": f.is_final,
                    "page_count": f.page_count,
                    "failure_reason": f.failure_reason,
                    "preview_url": f.preview_url,
                    "sms_status": f.sms_status,
                    "sms_to": f.sms_to,
                    "sms_failure": f.sms_failure,
                } for f in t.faxes.all()],
        })
    return rows


@with_get_params
def page_transfers(request):
    """Data for transfers.html — the patient's standalone tracking page."""
    my_transfers = (_my_transfers_list(request.user)
                    if request.user.is_authenticated else [])
    return {
        "my_transfers": my_transfers,
        "error": request.GET.get("error"),
        "msg": request.GET.get("msg"),
    }
