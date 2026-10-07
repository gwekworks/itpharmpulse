"""Patient prescription transfer requests (USA standard, 21 CFR 1306).

A transfer is ALWAYS patient-initiated (or authorized representative) and is
completed pharmacist-to-pharmacist. PharmacyPulse only collects + routes the
patient's request — it never performs the clinical transfer itself.

USA rules encoded in validation / UI copy:
- Non-controlled legend drugs: transferable, original must be voided at the
  sending pharmacy to prevent duplicate dispensing.
- Schedule III/IV/V: one-time transfer only (unless shared real-time DB),
  directly between two licensed pharmacists, 2-year records (21 CFR 1306.25).
- Schedule II: NO refill transfer — new prescription required. Electronic
  CII-CV initial-fill transfer allowed once, electronic form only, unaltered,
  pharmacist-to-pharmacist (2023 final rule, 21 CFR 1306.08(e)-(h)).
"""
from django.db import models


class PrescriptionTransfer(models.Model):
    MEDICATION_TYPE_CHOICES = (
        ("non_controlled", "Non-controlled"),
        ("schedule_iii_v", "Schedule III–V"),
        ("schedule_ii", "Schedule II"),
    )
    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("cancelled", "Cancelled"),
        ("completed", "Completed"),
    )

    user = models.ForeignKey(
        "User", null=True, blank=True, on_delete=models.SET_NULL,
        db_column="user_id", related_name="prescription_transfers",
    )
    from_pharmacy = models.ForeignKey(
        "Pharmacy", null=True, blank=True, on_delete=models.SET_NULL,
        db_column="from_pharmacy_id", related_name="outgoing_transfers",
    )
    to_pharmacy = models.ForeignKey(
        "Pharmacy", null=True, blank=True, on_delete=models.SET_NULL,
        db_column="to_pharmacy_id", related_name="incoming_transfers",
    )
    from_pharmacy_name = models.TextField(blank=True, default="")
    to_pharmacy_name = models.TextField(blank=True, default="")

    # ---- Patient identity (1306.05 required elements) ----
    patient_full_name = models.TextField(blank=True, default="")
    patient_dob = models.DateField(blank=True, null=True)
    patient_phone = models.CharField(max_length=32, blank=True, default="")
    patient_address = models.TextField(blank=True, default="")

    # ---- Prescription identity ----
    medication_name = models.TextField(blank=True, default="")
    medication_strength = models.TextField(blank=True, default="")
    directions = models.TextField(blank=True, default="")
    rx_number = models.CharField(max_length=64, blank=True, default="")
    refills_remaining = models.IntegerField(blank=True, null=True)
    last_filled_date = models.DateField(blank=True, null=True)
    prescriber_name = models.TextField(blank=True, default="")
    prescriber_phone = models.CharField(max_length=32, blank=True, default="")
    medication_type = models.CharField(
        max_length=16, default="non_controlled", choices=MEDICATION_TYPE_CHOICES,
    )

    # ---- Consent / audit ----
    consent_given = models.BooleanField(default=False)
    patient_signature = models.TextField(blank=True, default="")
    status = models.TextField(default="pending")
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "prescription_transfers"
        ordering = ["-created_at"]


class TransferFax(models.Model):
    """Outbound fax leg for a prescription transfer via EffyMobile.

    One row per pharmacy (sending + receiving). EffyMobile returns both an
    app-level `id` and a Telnyx `fax_id` — either works for detail/cancel.
    Lifecycle: queued → processing → sending → delivered | failed | canceled.
    `skipped` covers legs we never sent (no fax on file / no API creds).
    """
    RECIPIENT_CHOICES = (
        ("from_pharmacy", "Sending pharmacy"),
        ("to_pharmacy", "Receiving pharmacy"),
    )
    transfer = models.ForeignKey(
        PrescriptionTransfer, on_delete=models.CASCADE,
        db_column="transfer_id", related_name="faxes",
    )
    recipient_kind = models.CharField(max_length=16, default="to_pharmacy")
    pharmacy_name = models.TextField(blank=True, default="")
    to_number = models.CharField(max_length=32, blank=True, default="")
    media_url = models.TextField(blank=True, default="")
    effy_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    fax_id = models.CharField(max_length=64, blank=True, default="")
    status = models.TextField(default="pending")
    page_count = models.IntegerField(blank=True, null=True)
    failure_reason = models.TextField(blank=True, default="")
    preview_url = models.TextField(blank=True, default="")
    # SMS fallback (EffyMobile /api/sms/send) when the fax leg fails.
    # Sent to the pharmacy's voice line so the transfer isn't silently lost.
    pharmacy_phone = models.CharField(max_length=32, blank=True, default="")
    sms_status = models.TextField(default="unsent")
    sms_to = models.CharField(max_length=32, blank=True, default="")
    sms_failure = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "transfer_faxes"
        ordering = ["-created_at"]

    @property
    def is_final(self):
        return self.status in ("delivered", "failed", "canceled", "skipped")
