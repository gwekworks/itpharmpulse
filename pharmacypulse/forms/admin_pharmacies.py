"""Admin pharmacy creation form."""
import re

from django import forms


class ManualPharmacyForm(forms.Form):
    name = forms.CharField(max_length=255)
    address = forms.CharField(max_length=255)
    address_2 = forms.CharField(max_length=255, required=False)
    city = forms.CharField(max_length=100)
    state = forms.RegexField(
        regex=r"^[A-Za-z]{2}$",
        error_messages={"invalid": "Enter a two-letter state code."},
    )
    zip = forms.RegexField(
        regex=r"^\d{5}(?:-\d{4})?$",
        error_messages={"invalid": "Enter a 5-digit ZIP or ZIP+4."},
    )
    phone = forms.CharField(max_length=40, required=False)
    fax_number = forms.CharField(max_length=40, required=False)
    website = forms.URLField(required=False)
    npi_number = forms.CharField(max_length=10, required=False)
    is_digital = forms.BooleanField(required=False)

    def clean_state(self):
        return self.cleaned_data["state"].upper()

    def clean_npi_number(self):
        npi_number = self.cleaned_data["npi_number"].strip()
        if npi_number and not re.fullmatch(r"\d{10}", npi_number):
            raise forms.ValidationError("NPI must contain exactly 10 digits.")
        return npi_number

    def clean_website(self):
        website = self.cleaned_data["website"]
        if website and not website.lower().startswith(("http://", "https://")):
            raise forms.ValidationError("Website URL must use HTTP or HTTPS.")
        return website
