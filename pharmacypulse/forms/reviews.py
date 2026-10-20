"""Review write form."""
from django import forms


class WriteReviewForm(forms.Form):
    pharmacy_id = forms.IntegerField()
    pharmacy_name = forms.CharField(required=False)
    stock_available = forms.IntegerField(min_value=0, max_value=2)  # 0 out, 1 in, 2 partial
    wait_time_rating = forms.IntegerField(min_value=1, max_value=5)
    service_rating = forms.IntegerField(min_value=1, max_value=5)
    comment = forms.CharField(required=False)
    is_caregiver = forms.IntegerField(required=False)