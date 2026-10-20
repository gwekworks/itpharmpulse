"""Auth forms: signup + login."""
from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

User = get_user_model()


class SignupForm(forms.Form):
    email = forms.EmailField()
    password = forms.CharField(min_length=8)
    full_name = forms.CharField(max_length=128)
    zip_code = forms.CharField(max_length=16, required=False)

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("An account with that email already exists.")
        return email

    def clean_full_name(self):
        # Collapse internal whitespace so "  Jane   Doe " → "Jane Doe".
        name = " ".join(self.cleaned_data["full_name"].split())
        if not name:
            raise ValidationError("Please enter your name.")
        return name

    def clean(self):
        # Split the single Full Name field into the first_name / last_name the
        # User model + greetings + avatar initials still rely on. First token is
        # the first name; everything after is the last name (mononyms get a
        # blank last name).
        cleaned = super().clean()
        full = cleaned.get("full_name", "")
        if full:
            first, _, last = full.partition(" ")
            cleaned["first_name"] = first
            cleaned["last_name"] = last
        return cleaned


class LoginForm(forms.Form):
    email = forms.EmailField()
    password = forms.CharField()