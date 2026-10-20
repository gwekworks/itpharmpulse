"""Pharmacy claim form."""
from django import forms


class ClaimPharmacyForm(forms.Form):
    pharmacy_name = forms.CharField()
    npi_number = forms.CharField()
    license_number = forms.CharField()
    business_phone = forms.CharField(required=False)
    supporting_docs = forms.CharField(required=False)