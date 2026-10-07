"""Prescription transfer request form (patient-initiated, USA 21 CFR 1306)."""
from django import forms


class PrescriptionTransferForm(forms.Form):
    MEDICATION_TYPE_CHOICES = (
        ("non_controlled", "Non-controlled (regular prescription)"),
        ("schedule_iii_v", "Schedule III–V controlled substance"),
        ("schedule_ii", "Schedule II controlled substance"),
        ("unsure", "Not sure"),
    )

    from_pharmacy_id = forms.IntegerField()
    to_pharmacy_id = forms.IntegerField()
    patient_full_name = forms.CharField(max_length=255)
    patient_dob = forms.DateField(
        input_formats=["%Y-%m-%d"], error_messages={"invalid": "Use YYYY-MM-DD."}
    )
    patient_phone = forms.CharField(max_length=32)
    patient_address = forms.CharField(required=False)
    medication_name = forms.CharField(max_length=255)
    medication_strength = forms.CharField(required=False)
    directions = forms.CharField(required=False)
    rx_number = forms.CharField(required=False, max_length=64)
    refills_remaining = forms.IntegerField(required=False, min_value=0, max_value=99)
    prescriber_name = forms.CharField(required=False)
    prescriber_phone = forms.CharField(required=False, max_length=32)
    medication_type = forms.ChoiceField(choices=MEDICATION_TYPE_CHOICES)
    consent_given = forms.BooleanField(required=True)
    patient_signature = forms.CharField(max_length=255)

    def clean(self):
        cleaned = super().clean()
        from_id = cleaned.get("from_pharmacy_id")
        to_id = cleaned.get("to_pharmacy_id")
        if from_id and to_id and from_id == to_id:
            raise forms.ValidationError(
                "Sending and receiving pharmacies must be different."
            )
        med_type = cleaned.get("medication_type")
        if med_type == "schedule_ii":
            raise forms.ValidationError(
                "Schedule II prescriptions cannot be transferred — ask your "
                "prescriber for a new prescription (21 CFR 1306.08 / 1306.25). "
                "You can still save this request and contact the new pharmacy."
            )
        if med_type == "unsure":
            # Treat as non-controlled for routing but force pharmacist review.
            cleaned["medication_type"] = "non_controlled"
            cleaned["needs_pharmacist_review"] = True
        return cleaned
