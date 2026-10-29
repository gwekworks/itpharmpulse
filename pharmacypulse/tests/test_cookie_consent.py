from django.test import TestCase
from django.urls import reverse


class CookieConsentTests(TestCase):
    def test_analytics_is_not_loaded_before_consent(self):
        response = self.client.get(reverse("privacy"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="pp-cookie-consent"')
        self.assertContains(response, 'class="pp-cookie-banner"')
        self.assertContains(response, "Accept all")
        self.assertContains(response, "Reject optional")
        self.assertContains(response, 'name="functional"')
        self.assertContains(response, 'name="analytics"')
        self.assertContains(response, 'name="advertising"')
        self.assertContains(response, "Advertising")
        self.assertContains(response, "pp-sticky-copilot")
        self.assertContains(response, 'id="pp-ai-modal"')
        self.assertContains(response, "if (!window.PP_COOKIE_CONSENT.set) return;")
        self.assertNotContains(response, 'class="pp-ai-modal pp-ai-open"')
        self.assertContains(response, "window.ppSetAnalyticsConsent(false)")
        self.assertNotContains(response, 'src="https://www.googletagmanager.com/gtag/js')

    def test_accepting_analytics_loads_it_on_the_next_page(self):
        response = self.client.post(
            reverse("cookie_consent"),
            {"choice": "accept", "next": reverse("privacy")},
        )

        self.assertRedirects(response, reverse("privacy"))
        consent_cookie = response.cookies["pp_cookie_consent"]
        self.assertEqual(consent_cookie.value, "v3:f1:a1:m1")
        self.assertTrue(consent_cookie["httponly"])

        page = self.client.get(reverse("privacy"))
        self.assertContains(page, "window.ppSetAnalyticsConsent(true)")
        self.assertContains(page, 'id="pp-ai-modal"')
        self.assertContains(page, 'name="functional" value="1" checked')
        self.assertContains(page, 'name="analytics" value="1" checked')
        self.assertContains(page, 'name="advertising" value="1" checked')
        self.assertNotContains(page, 'class="pp-cookie-consent"')
        self.assertNotContains(page, "Cookie settings</summary>")
        self.assertContains(page, 'id="pp-cookie-preferences-page"')

    def test_rejecting_analytics_keeps_it_disabled_and_clears_ga_cookies(self):
        self.client.cookies["pp_cookie_consent"] = "v3:f1:a1:m1"
        self.client.cookies["pp_loc"] = "40.0,-75.0"
        self.client.cookies["lang"] = "es"
        self.client.cookies["_ga"] = "existing-analytics-id"

        response = self.client.post(
            reverse("cookie_consent"),
            {"choice": "reject", "next": reverse("privacy")},
        )

        self.assertRedirects(response, reverse("privacy"))
        self.assertEqual(response.cookies["pp_cookie_consent"].value, "v3:f0:a0:m0")
        self.assertEqual(response.cookies["_ga"].value, "")
        self.assertEqual(response.cookies["pp_loc"].value, "")
        self.assertEqual(response.cookies["lang"].value, "")
        page = self.client.get(reverse("privacy"))
        self.assertContains(page, "window.ppSetAnalyticsConsent(false)")
        self.assertNotContains(page, 'class="pp-cookie-consent"')
        self.assertContains(page, 'id="pp-cookie-preferences-page"')

    def test_saving_functional_preferences_without_analytics(self):
        response = self.client.post(
            reverse("cookie_consent"),
            {"choice": "save", "functional": "1", "next": reverse("privacy")},
        )

        self.assertRedirects(response, reverse("privacy"))
        self.assertEqual(response.cookies["pp_cookie_consent"].value, "v3:f1:a0:m0")
        page = self.client.get(reverse("privacy"))
        self.assertContains(page, 'name="functional" value="1" checked')
        self.assertContains(page, "window.ppSetAnalyticsConsent(false)")
        self.assertNotContains(page, 'class="pp-cookie-consent"')

    def test_saving_analytics_without_functional_preferences(self):
        response = self.client.post(
            reverse("cookie_consent"),
            {"choice": "save", "analytics": "1", "next": reverse("privacy")},
        )

        self.assertRedirects(response, reverse("privacy"))
        self.assertEqual(response.cookies["pp_cookie_consent"].value, "v3:f0:a1:m0")
        page = self.client.get(reverse("privacy"))
        self.assertContains(page, 'name="analytics" value="1" checked')
        self.assertNotContains(page, 'name="functional" value="1" checked')
        self.assertContains(page, "window.ppSetAnalyticsConsent(true)")
        self.assertNotContains(page, 'class="pp-cookie-consent"')
        self.assertNotContains(page, 'class="pp-cookie-consent"')

    def test_ajax_consent_saves_cookie_without_redirect(self):
        response = self.client.post(
            reverse("cookie_consent"),
            {"choice": "accept"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "ok": True,
            "set": True,
            "functional": True,
            "analytics": True,
            "advertising": True,
        })
        self.assertEqual(response.cookies["pp_cookie_consent"].value, "v3:f1:a1:m1")

    def test_ajax_rejection_clears_analytics_cookies_without_redirect(self):
        self.client.cookies["_ga"] = "existing-analytics-id"
        response = self.client.post(
            reverse("cookie_consent"),
            {"choice": "reject"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["analytics"], False)
        self.assertEqual(response.cookies["pp_cookie_consent"].value, "v3:f0:a0:m0")
        self.assertEqual(response.cookies["_ga"].value, "")

    def test_saving_advertising_preference_does_not_load_ad_technology(self):
        response = self.client.post(
            reverse("cookie_consent"),
            {"choice": "save", "advertising": "1", "next": reverse("privacy")},
        )

        self.assertRedirects(response, reverse("privacy"))
        self.assertEqual(response.cookies["pp_cookie_consent"].value, "v3:f0:a0:m1")
        page = self.client.get(reverse("privacy"))
        self.assertContains(page, 'name="advertising" value="1" checked')
        self.assertNotContains(page, "doubleclick")
        self.assertNotContains(page, 'class="pp-cookie-consent"')

    def test_invalid_choice_is_rejected(self):
        response = self.client.post(
            reverse("cookie_consent"),
            {"choice": "maybe", "next": reverse("privacy")},
        )

        self.assertEqual(response.status_code, 400)
