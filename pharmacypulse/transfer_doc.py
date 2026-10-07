"""Transfer authorization PDF (fax body) + signed public URL.

The fax API accepts only `media_url` (public PDF/TIFF) or a stored
`media_name` template — there is no upload endpoint. So the transfer
authorization is rendered to PDF with reportlab and served from a signed,
login-free URL (`TimestampSigner`, salt "pp-transfer-doc") that EffyMobile
can fetch. The PDF is generated deterministically from the transfer row on
each request — nothing is stored on disk.
"""
from __future__ import annotations

import io

from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

from .models import PrescriptionTransfer

_SIGN_SALT = "pp-transfer-doc"
# Fax retries + record-keeping span weeks — keep links valid 90 days.
SIGN_MAX_AGE_S = 90 * 24 * 3600


def _signer() -> TimestampSigner:
    return TimestampSigner(salt=_SIGN_SALT)


def make_doc_token(transfer_id: int) -> str:
    return _signer().sign(str(transfer_id))


def unsign_doc_token(token: str) -> int:
    return int(_signer().unsign(token, max_age=SIGN_MAX_AGE_S))


def make_doc_url(base: str, transfer_id: int) -> str:
    from urllib.parse import quote
    return f"{base.rstrip('/')}/transfer/doc/{transfer_id}.pdf?s={quote(make_doc_token(transfer_id))}"


def _row(label: str, value) -> tuple:
    return (label, str(value or "—"))


def build_transfer_pdf(t) -> bytes:
    """Render the transfer authorization for faxing. `t` is a
    PrescriptionTransfer with from_pharmacy/to_pharmacy prefetched."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=f"Prescription Transfer Authorization #{t.id}",
    )
    styles = getSampleStyleSheet()
    h1 = styles["Heading1"]
    h1.textColor = colors.HexColor("#0f172a")
    h1.fontSize = 18
    h2 = styles["Heading2"]
    h2.textColor = colors.HexColor("#008F82")
    h2.fontSize = 12
    body = styles["Normal"]
    body.fontSize = 10
    body.leading = 14
    small = styles["Normal"]
    small.fontSize = 8
    small.textColor = colors.HexColor("#64748b")

    fp = getattr(t, "from_pharmacy", None)
    tp = getattr(t, "to_pharmacy", None)

    def addr(p, fallback_name: str) -> str:
        if p is None:
            return fallback_name
        parts = [p.name, p.address,
                 f"{p.city}, {p.state} {p.zip}".strip(", "),
                 f"Ph: {p.phone}" if p.phone else "",
                 f"Fax: {p.fax_number}" if getattr(p, "fax_number", "") else ""]
        return "<br/>".join(x for x in parts if x and str(x).strip(", "))

    story = [
        Paragraph("Prescription Transfer Authorization", h1),
        Paragraph(
            f"PharmacyPulse &nbsp;•&nbsp; Request #{t.id} &nbsp;•&nbsp; "
            f"{t.created_at.strftime('%Y-%m-%d %H:%M %Z') if t.created_at else ''}",
            small,
        ),
        Spacer(1, 0.15 * inch),
        HRFlowable(width="100%", thickness=1, color=colors.HexColor("#008F82")),
        Spacer(1, 0.12 * inch),
        Paragraph("Patient", h2),
        Table([
            _row("Full name", t.patient_full_name),
            _row("Date of birth", t.patient_dob.isoformat() if t.patient_dob else ""),
            _row("Phone", t.patient_phone),
            _row("Address", t.patient_address),
        ], colWidths=[1.8 * inch, 5.2 * inch]),
        Spacer(1, 0.1 * inch),
        Paragraph("Prescription", h2),
        Table([
            _row("Medication", f"{t.medication_name} {t.medication_strength or ''}".strip()),
            _row("Directions", t.directions),
            _row("Rx number", t.rx_number),
            _row("Refills remaining",
                 t.refills_remaining if t.refills_remaining is not None else ""),
            _row("Last filled",
                 t.last_filled_date.isoformat() if t.last_filled_date else ""),
            _row("Prescriber",
                 f"{t.prescriber_name} {t.prescriber_phone or ''}".strip()),
            _row("Type", dict(
                PrescriptionTransfer.MEDICATION_TYPE_CHOICES).get(
                    t.medication_type, t.medication_type)),
        ], colWidths=[1.8 * inch, 5.2 * inch]),
        Spacer(1, 0.1 * inch),
        Paragraph("Transfer", h2),
        Table([
            _row("FROM (sending)", ""),
            ("", Paragraph(addr(fp, t.from_pharmacy_name), body)),
            _row("TO (receiving)", ""),
            ("", Paragraph(addr(tp, t.to_pharmacy_name), body)),
        ], colWidths=[1.8 * inch, 5.2 * inch]),
        Spacer(1, 0.15 * inch),
        Paragraph("Patient authorization", h2),
        Paragraph(
            "I authorize my current pharmacy to transfer this prescription "
            "(including refills) to the receiving pharmacy, void the original "
            "to prevent duplicate fills, and share the records the pharmacists "
            "need under 21 CFR 1306. I am the patient (18+) or authorized "
            "representative. This transfer must be completed "
            "pharmacist-to-pharmacist.",
            body,
        ),
        Spacer(1, 0.1 * inch),
        Table([
            _row("Signature (typed)", t.patient_signature),
            _row("Consent given", "Yes" if t.consent_given else "No"),
        ], colWidths=[1.8 * inch, 5.2 * inch]),
        Spacer(1, 0.2 * inch),
        HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cbd5e1")),
        Paragraph(
            "System-generated by PharmacyPulse from the patient's online "
            "request. Pharmacists: verify all details against the original "
            "prescription record before dispensing. Questions? Contact the "
            "patient at the phone number above.",
            small,
        ),
    ]
    for el in story:
        if isinstance(el, Table):
            el.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#64748b")),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
            ]))
    doc.build(story)
    return buf.getvalue()
