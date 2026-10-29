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


IMPORTANT_NOTICE = """CONFIDENTIALITY NOTICE
This fax and any attachments may contain confidential health information intended only for the named recipient. If you are not the intended recipient, please do not review, copy, disclose, or distribute this information. Notify the sender immediately at [phone number] and securely destroy all copies. Thank you for protecting patient privacy.
"""


def _pharmacy_block(p, fallback_name: str, phone_override: str = "", fax_override: str = "") -> str:
    """Full contact block: name / street / City, ST ZIP / phone / fax."""
    if p is None:
        parts = [fallback_name]
        if phone_override:
            parts.append(f"Phone: {phone_override}")
        if fax_override:
            parts.append(f"Fax: {fax_override}")
        return "<br/>".join(parts)
    addr2 = getattr(p, "address_2", "") or ""
    street = " ".join(x for x in [p.address, addr2] if x)
    city_st_zip = f"{p.city}, {p.state} {p.zip}".strip(", ")
    phone = (phone_override or p.phone) or ""
    fax = (fax_override or getattr(p, "fax_number", "")) or ""
    parts = [p.name, street, city_st_zip,
             f"Phone: {phone}" if phone else "",
             f"Fax: {fax}" if fax else ""]
    return "<br/>".join(x for x in parts if x and str(x).strip(", "))


def transfer_code(transfer_id: int) -> str:
    """Stable human request ID matching the template (PP-XXXXXX)."""
    try:
        n = int(transfer_id)
    except (TypeError, ValueError):
        n = 0
    return f"PP-{n:06d}"


def build_transfer_pdf(t) -> bytes:
    """Render the faxed form to match the approved HTML template.

    Sections: header (logo + Request ID) / Patient box / request text /
    alert box / Current (Transferring) Pharmacy box / Comment (generation
    details) / Important Notice. Patient-initiated request only — no
    medication fields.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    BLUE = colors.HexColor("#005a7d")
    INK = colors.HexColor("#000000")
    GRAY = colors.HexColor("#555555")
    BOX_BG = colors.HexColor("#f7f7f7")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
        title=f"PharmacyPulse - Prescription Transfer Request {transfer_code(getattr(t, 'id', 0))}",
    )
    styles = getSampleStyleSheet()
    s_title = ParagraphStyle("ppTitle", parent=styles["Normal"], fontSize=20,
                             leading=22, textColor=BLUE, fontName="Helvetica-Bold")
    s_sub = ParagraphStyle("ppSub", parent=styles["Normal"], fontSize=10,
                           leading=12, textColor=GRAY)
    s_h2 = ParagraphStyle("ppH2", parent=styles["Normal"], fontSize=15,
                          leading=17, textColor=INK, fontName="Helvetica-Bold",
                          alignment=2)  # right
    s_h2sub = ParagraphStyle("ppH2sub", parent=styles["Normal"], fontSize=11,
                             leading=13, textColor=GRAY, alignment=2)
    s_label = ParagraphStyle("ppLabel", parent=styles["Normal"], fontSize=9,
                             leading=11, textColor=BLUE, fontName="Helvetica-Bold")
    s_field_label = ParagraphStyle("ppFieldLabel", parent=styles["Normal"],
                                   fontSize=9, leading=11, textColor=GRAY)
    s_val = ParagraphStyle("ppVal", parent=styles["Normal"], fontSize=11,
                           leading=14, textColor=INK)
    s_val_big = ParagraphStyle("ppValBig", parent=s_val, fontSize=13,
                               fontName="Helvetica-Bold")
    s_text = ParagraphStyle("ppText", parent=styles["Normal"], fontSize=11,
                            leading=17, textColor=INK)
    s_alert = ParagraphStyle("ppAlert", parent=styles["Normal"], fontSize=11,
                             leading=14, textColor=INK, fontName="Helvetica-Bold")
    s_notice_h = ParagraphStyle("ppNoticeH", parent=styles["Normal"], fontSize=9,
                                leading=11, textColor=INK, fontName="Helvetica-Bold")
    s_notice = ParagraphStyle("ppNotice", parent=styles["Normal"], fontSize=8,
                              leading=11, textColor=colors.HexColor("#222222"))

    fp = getattr(t, "from_pharmacy", None)
    code = transfer_code(getattr(t, "id", 0))

    def _esc(v) -> str:
        from xml.sax.saxutils import escape
        return escape(str(v or ""))

    # Dates: DOB MM/DD/YYYY, generated date/time in America/Los_Angeles.
    dob_str = t.patient_dob.strftime("%m/%d/%Y") if getattr(t, "patient_dob", None) else ""
    gen_dt = getattr(t, "created_at", None)
    if gen_dt is not None:
        try:
            from zoneinfo import ZoneInfo
            gen_dt = gen_dt.astimezone(ZoneInfo("America/Los_Angeles"))
        except Exception:
            pass
        gen_date = gen_dt.strftime("%m/%d/%Y")
        try:
            gen_time = gen_dt.strftime("%I:%M %p %Z").lstrip("0")
        except Exception:
            gen_time = ""
    else:
        gen_date, gen_time = "", ""

    # Current (transferring) pharmacy contact: patient overrides win.
    if fp is not None:
        addr2 = getattr(fp, "address_2", "") or ""
        street = " ".join(x for x in [fp.address, addr2] if x)
        city_st_zip = f"{fp.city}, {fp.state} {fp.zip}".strip(", ")
        ph_name = fp.name
        ph_phone = ((getattr(t, "from_phone_override", "") or "").strip()
                    or (fp.phone or ""))
        ph_fax = ((getattr(t, "from_fax_override", "") or "").strip()
                  or (getattr(fp, "fax_number", "") or ""))
    else:
        street = ""
        city_st_zip = ""
        ph_name = t.from_pharmacy_name or ""
        ph_phone = (getattr(t, "from_phone_override", "") or "")
        ph_fax = (getattr(t, "from_fax_override", "") or "")

    try:
        from django.conf import settings as _dj_settings
        support_contact = (_dj_settings.DEFAULT_FROM_EMAIL or "support@pharmacypulse.co")
    except Exception:
        support_contact = "support@pharmacypulse.co"

    def _field(label: str, value: str):
        return [Paragraph(f"<font color=\"#555555\">{_esc(label)}</font>", s_field_label),
                Paragraph(_esc(value) or "&nbsp;", s_val)]

    story = [
        Table([
            [Paragraph("PharmacyPulse<br/><font size=9 color=\"#555555\">pharmacypulse.co</font>", s_title),
             Paragraph(f"Prescription Transfer Request<br/><font size=10 color=\"#555555\">Request ID: {code}</font>",
                       s_h2)],
        ], colWidths=[3.5 * inch, 3.7 * inch]),
        HRFlowable(width="100%", thickness=3, color=BLUE, spaceAfter=12, spaceBefore=6),
        Paragraph("PATIENT", s_label),
        Table([[
            _field("Patient name", t.patient_full_name),
            _field("Date of birth", dob_str or "MM/DD/YYYY"),
            _field("Phone", t.patient_phone),
        ]], colWidths=[2.4 * inch, 2.4 * inch, 2.4 * inch],
            style=TableStyle([("BOX", (0, 0), (-1, -1), 1, INK),
                              ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                              ("LEFTPADDING", (0, 0), (-1, -1), 8),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                              ("TOPPADDING", (0, 0), (-1, -1), 8),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 8)])),
        Spacer(1, 0.15 * inch),
        Paragraph(
            f"The above patient is requesting that <b>{_esc(t.from_pharmacy_name)}</b> contact<br/>"
            f"<b>{_esc(t.to_pharmacy_name)}</b> to transfer their prescription(s).",
            s_text),
        Spacer(1, 0.12 * inch),
        Table([[
            Paragraph("\u260e", ParagraphStyle("ph", parent=s_text, fontSize=16)),
            Paragraph("Please contact the patient to confirm the prescriptions to be transferred.", s_alert),
        ]], colWidths=[0.5 * inch, 6.7 * inch],
            style=TableStyle([("BOX", (0, 0), (-1, -1), 1.5, INK),
                              ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 10),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                              ("TOPPADDING", (0, 0), (-1, -1), 10),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 10)])),
        Spacer(1, 0.15 * inch),
        Paragraph("CURRENT (TRANSFERRING) PHARMACY", s_label),
        Table([[
            [Paragraph(_esc(ph_name) or "&nbsp;", s_val_big),
             Spacer(1, 0.08 * inch),
             Paragraph("<font color=\"#555555\">Street address</font>", s_field_label),
             Paragraph(_esc(street) or "&nbsp;", s_val),
             Spacer(1, 0.06 * inch),
             Paragraph("<font color=\"#555555\">City, State ZIP</font>", s_field_label),
             Paragraph(_esc(city_st_zip) or "&nbsp;", s_val)],
            [Paragraph("<font color=\"#555555\">Phone</font>", s_field_label),
             Paragraph(_esc(ph_phone) or "&nbsp;", s_val),
             Spacer(1, 0.1 * inch),
             Paragraph("<font color=\"#555555\">Fax</font>", s_field_label),
             Paragraph(_esc(ph_fax) or "&nbsp;", s_val)],
        ]], colWidths=[3.6 * inch, 3.6 * inch],
            style=TableStyle([("BOX", (0, 0), (-1, -1), 1, INK),
                              ("LINEBELOW", (0, 0), (-1, 0), 0, INK),
                              ("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 10),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                              ("TOPPADDING", (0, 0), (-1, -1), 10),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 10)])),
        Spacer(1, 0.15 * inch),
        Paragraph("COMMENT", s_label),
        Paragraph(
            f"This form was generated on PharmacyPulse by <b>{_esc(t.patient_full_name)}</b> "
            f"on <b>{_esc(gen_date)}</b> at <b>{_esc(gen_time)}</b>.",
            s_text),
        Spacer(1, 0.15 * inch),
        HRFlowable(width="100%", thickness=0.75, color=INK, spaceAfter=8, spaceBefore=4),
        Paragraph("IMPORTANT NOTICE", s_notice_h),
        Spacer(1, 0.06 * inch),
    ]
    notice_paras = [
        "<b>Not a prescription.</b> This form is a patient-initiated request only. It is not a prescription, "
        "a prescription transfer, or an order to dispense, and it does not authorize any pharmacy to fill or "
        "release any medication.",
        "<b>PharmacyPulse is not a pharmacy.</b> PharmacyPulse (operated by BeyondRx) does not dispense, store, "
        "transfer, or verify prescriptions or prescription records, and does not provide medical or pharmacy advice.",
        "<b>Patient-supplied information.</b> All information on this form was entered by the patient or their "
        "representative. PharmacyPulse has not verified the identity of the patient, the accuracy of the "
        "information, or the existence, validity, or transferability of any prescription.",
        "<b>Pharmacist responsibility.</b> The receiving pharmacist must verify patient identity and complete any "
        "transfer in accordance with applicable federal and state laws, board of pharmacy rules, and pharmacy "
        "policy. Controlled substance prescriptions are subject to additional federal and state restrictions and "
        "may not be eligible for transfer. Each pharmacy retains sole professional discretion to accept or decline "
        "the request.",
        "<b>Patient authorization.</b> By generating this form, the patient directed PharmacyPulse to deliver it "
        "to the receiving pharmacy and consented to being contacted by that pharmacy at the phone number listed.",
        "<b>No warranty; limitation of liability.</b> This form is provided \"as is\" without warranty of any kind. "
        "To the fullest extent permitted by law, PharmacyPulse and BeyondRx are not liable for any delay, error, "
        "missed dose, denial of transfer, or other outcome arising from the use of this form or any action taken "
        "or not taken by a pharmacy.",
        f"If you received this form in error, please destroy it and notify PharmacyPulse at {_esc(support_contact)}.",
    ]
    for para in notice_paras:
        story.append(Paragraph(para, s_notice))
        story.append(Spacer(1, 0.06 * inch))
    doc.build(story)
    return buf.getvalue()
