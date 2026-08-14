from django.test import TestCase, override_settings
from django.urls import reverse

from pharmacypulse.models import (
    DrugShortage,
    Pharmacy,
    PharmacyClaim,
    PharmacyCoveragePlan,
    PharmacyHours,
    PharmacyInsuranceProvider,
    PharmacyOrg,
    PharmacyPublishable,
    PharmacyService,
    PharmacyTeamMember,
    ResponseCount,
    Review,
    SavedComparison,
    User,
)


@override_settings(
    GOOGLE_PLACES_KEY="",
    GOOGLE_OAUTH_CLIENT_ID="",
    GOOGLE_OAUTH_CLIENT_SECRET="",
    STRIPE_SECRET_KEY="",
    STRIPE_PRICE_ID="",
)
class ApplicationSmokeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="patient@example.com",
            email="patient@example.com",
            password="demo1234",
            first_name="Pat",
            last_name="Ient",
            zip_code="19103",
        )
        self.pharmacist = User.objects.create_user(
            username="pharmacist@example.com",
            email="pharmacist@example.com",
            password="demo1234",
            first_name="Pharm",
            last_name="Owner",
            role="pharmacist",
        )
        self.admin = User.objects.create_user(
            username="admin@example.com",
            email="admin@example.com",
            password="demo1234",
            first_name="Admin",
            last_name="User",
            role="admin",
        )
        self.pharmacy = Pharmacy.objects.create(
            name="Smoke Pharmacy",
            address="123 Market St",
            city="Philadelphia",
            state="PA",
            zip="19103",
            status="active",
            avg_service_rating=4.5,
            avg_wait_time=2,
            stock_confidence=88,
            total_reviews=1,
            latitude=39.95,
            longitude=-75.16,
        )
        self.online = Pharmacy.objects.create(
            name="Smoke Online Pharmacy",
            address="Online",
            city="Online",
            state="US",
            zip="00000",
            status="active",
            is_digital=1,
            avg_service_rating=4.2,
        )
        self.org = PharmacyOrg.objects.create(name="Smoke Org", owner=self.pharmacist)
        self.pharmacy.org = self.org
        self.pharmacy.claimed_by = self.pharmacist.id
        self.pharmacy.save(update_fields=["org", "claimed_by"])
        PharmacyTeamMember.objects.create(
            org=self.org,
            user=self.pharmacist,
            email=self.pharmacist.email,
            role="owner",
        )
        self.review = Review.objects.create(
            pharmacy=self.pharmacy,
            pharmacy_name=self.pharmacy.name,
            user=self.user,
            user_name="Pat Ient",
            service_rating=5,
            wait_time_rating=4,
            stock_available=1,
            comment="Helpful team",
            moderation_status="approved",
        )
        self.shortage = DrugShortage.objects.create(
            drug_name="Smoke Drug",
            manufacturer="Smoke Labs",
            status="current",
        )
        # Search surfaces read from pharmacies_publishable; mirror the
        # fixtures into it (as the sync_publish_pharmacies command would).
        self._publish(self.pharmacy)
        self._publish(self.online)

    def _publish(self, p):
        return PharmacyPublishable.objects.create(
            pharmacy_id=p.id, publish_status="published",
            name=p.name, address=p.address, city=p.city, state=p.state, zip=p.zip,
            phone=p.phone, latitude=p.latitude, longitude=p.longitude,
            stock_confidence=p.stock_confidence, avg_wait_time=p.avg_wait_time,
            avg_service_rating=p.avg_service_rating, total_reviews=p.total_reviews,
            is_digital=p.is_digital, website=p.website, npi_number=p.npi_number,
            taxonomy_code=p.taxonomy_code, chain_size=p.chain_size, status="active",
        )

    def assert_ok(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)

    def test_pharmacies_publishable_gates_search(self):
        """Hidden pharmacies only appear in search when published. Rows on
        pharmacies_publishable with publish_status='unpublished' must not show
        in the public list/search/autocomplete."""
        pharm = PharmacyPublishable.objects.get(pharmacy_id=self.pharmacy.id)
        pharm.publish_status = "unpublished"
        pharm.save(update_fields=["publish_status"])

        resp = self.client.get("/api/flow/search", {"q": "Smoke"})
        self.assertTrue(
            all(r["name"] != "Smoke Pharmacy" for r in resp.json()["results"]))

        resp = self.client.get("/list", {"q": "Smoke"})
        self.assertNotContains(resp, "Smoke Pharmacy")

        # Restore so other tests see it published.
        pharm.publish_status = "published"
        pharm.save(update_fields=["publish_status"])

    def test_publish_sync_preserves_admin_edited_rows(self):
        """sync_publish_pharmacies creates missing rows, refreshes untouched
        rows, and never overwrites rows the admin has edited."""
        from django.core.management import call_command

        row = PharmacyPublishable.objects.get(pharmacy_id=self.pharmacy.id)
        row.admin_edited = True
        row.save(update_fields=["admin_edited"])

        self.pharmacy.name = "Smoke Pharmacy (moved)"
        self.pharmacy.save(update_fields=["name"])

        # Untouched row (online) should be refreshed; admin-edited (self.pharmacy)
        # should keep its old name.
        call_command("sync_publish_pharmacies")

        edited = PharmacyPublishable.objects.get(pharmacy_id=self.pharmacy.id)
        self.assertEqual(edited.name, "Smoke Pharmacy")  # preserved
        untouched = PharmacyPublishable.objects.get(pharmacy_id=self.online.id)
        self.assertEqual(untouched.name, self.online.name)

    def test_public_pages_render(self):
        for path in [
            "/",
            "/list",
            "/map",
            "/online",
            f"/pharmacy?id={self.pharmacy.id}",
            f"/review?pharmacy_id={self.pharmacy.id}",
            "/reviews",
            "/compare",
            "/shortages",
            "/for-pharmacies",
            f"/widget?id={self.pharmacy.id}",
            "/privacy",
            "/terms",
            "/login",
            "/signup",
            "/forgot-password",
            "/robots.txt",
            "/sitemap.xml",
        ]:
            self.assert_ok(path)

    def test_list_location_banner_and_distance_sort(self):
        """/list asks the visitor to enable location when none is available,
        and switches to closest-first once a browser location (pp_loc cookie)
        is present."""
        # No location available → "turn on your location" modal markup is
        # rendered and results are not marked as distance-sorted.
        resp = self.client.get("/list")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="loc-modal"')
        self.assertContains(resp, "Turn on your location")
        self.assertNotContains(resp, "Sorted by nearest to you")

        # Browser location shared via the pp_loc cookie (set by the homepage
        # geolocation prompt) → no modal, results sorted closest-first.
        self.client.cookies["pp_loc"] = "39.9526,-75.1652"
        resp = self.client.get("/list")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'id="loc-modal"')
        self.assertContains(resp, "Sorted by nearest to you")
        self.assertContains(resp, "Smoke Pharmacy")
        self.assertContains(resp, "mi")

    def test_authed_user_zip_without_local_pharmacies_shows_list(self):
        """A logged-in user whose profile ZIP has no pharmacies nearby (exact
        city misses AND the ~10 mi radius is empty) still gets results — the
        localization falls back to the global list instead of "No pharmacies
        found"."""
        user = User.objects.create_user(
            username="far@example.com", email="far@example.com",
            password="demo1234", zip_code="97330",
        )
        self.client.force_login(user)
        resp = self.client.get("/list")
        self.assertEqual(resp.status_code, 200)
        # Note: the raw string "No pharmacies found" also lives in the topbar's
        # JS i18n dict, so assert against the actual empty-state block + rows.
        self.assertNotContains(resp, 'class="lp-empty"')
        self.assertContains(resp, "Smoke Pharmacy")

    def test_list_empty_results_keeps_map_panel(self):
        """An empty search still shows the map panel (right column persists)
        so the visitor can see where they searched."""
        resp = self.client.get("/list", {"q": "zzz-no-such-pharmacy"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "No pharmacies found")
        # The map panel (even the "Map not available" placeholder when no
        # API key is configured) must remain in the page.
        self.assertContains(resp, "lp-map-panel")
        self.assertContains(resp, "0 pharmacies shown")

        # With a known location the map label reflects the searched area.
        self.client.cookies["pp_loc"] = "39.9526,-75.1652"
        resp = self.client.get("/list", {"q": "zzz-no-such-pharmacy"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "No pharmacies found")
        self.assertContains(resp, "0 pharmacies shown")

        # With a Maps API key configured, the map container and its init
        # script render even when there are zero results.
        with self.settings(GOOGLE_PLACES_KEY="test-key"):
            resp = self.client.get("/list", {"q": "zzz-no-such-pharmacy"})
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, 'id="lp-map"')
            self.assertContains(resp, "function initLpMap()")
            self.assertContains(resp, "maps.googleapis.com/maps/api/js")

    def test_review_submit_button_auth_gated(self):
        """Step 3's "Submit Review" button renders only for signed-in users."""
        url = f"/pharmacy?id={self.pharmacy.id}"

        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Submit Review ✓")

        self.client.force_login(self.user)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Submit Review ✓")

    def test_authenticated_pages_render(self):
        self.client.force_login(self.user)
        for path in ["/home", "/account", f"/claim?pharmacy_id={self.pharmacy.id}"]:
            self.assert_ok(path)

        # Header nav: Account link is only shown to logged-in users.
        resp = self.client.get("/home")
        self.assertContains(resp, 'id="top-account"', status_code=200)
        anon = self.client.__class__()
        anon_resp = anon.get("/")
        self.assertContains(anon_resp, 'Log in', status_code=200)
        self.assertNotContains(anon_resp, 'id="top-account"')

        self.client.force_login(self.pharmacist)
        self.assert_ok("/pharmacist-dashboard")
        PharmacyClaim.objects.create(
            pharmacy=self.pharmacy,
            pharmacy_name=self.pharmacy.name,
            user=self.pharmacist,
            user_name="Pharm Owner",
            npi_number="1234567890",
            license_number="RX123",
            status="approved",
        )

        # Listing-setup stepper: sticky bottom-right vertical progress list,
        # appears until all steps are done. No services/insurance/hours and
        # an unanswered review → 0%.
        resp = self.client.get("/pharmacist-dashboard")
        self.assertContains(resp, "Complete your listing")
        self.assertContains(resp, "0% complete")
        self.assertContains(resp, "Add your services")
        self.assertContains(resp, "position: fixed; right: 1.25rem; bottom: 1.25rem")
        self.assertContains(resp, "Setup%20hours")
        # Stepper follows the user to every dashboard tab, not just overview.
        resp = self.client.get("/pharmacist-dashboard?tab=insurance")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Complete your listing")
        self.assertContains(resp, "position: fixed")
        resp = self.client.get("/pharmacist-dashboard?tab=insurance-lg")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Insurance Providers")
        self.assertContains(resp, "Add Government Plan")
        resp = self.client.get("/pharmacist-dashboard?tab=hours")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Save Hours")

        # Add services + insurance → 50% and next step is hours.
        PharmacyService.objects.create(pharmacy=self.pharmacy, service_type="delivery")
        PharmacyInsuranceProvider.objects.create(
            pharmacy=self.pharmacy, provider_name="Aetna")
        PharmacyCoveragePlan.objects.create(
            pharmacy=self.pharmacy, plan_name="Medicare Part D", plan_type="Medicare")
        resp = self.client.get("/pharmacist-dashboard")
        self.assertContains(resp, "50% complete")
        self.assertContains(resp, "Set operating hours")

        # Add hours → 75%; remaining step is responding to reviews.
        PharmacyHours.objects.create(
            pharmacy=self.pharmacy, day_of_week=0, day_name="Monday",
            open_time="9:00 AM", close_time="5:00 PM")
        resp = self.client.get("/pharmacist-dashboard")
        self.assertContains(resp, "75% complete")
        self.assertContains(resp, "Respond to reviews")

        # Answer the pending review → tracker hidden entirely (card only
        # renders while setup_pct < 100). The listing is now 100% complete.
        self.review.response_text = "Thanks for the feedback!"
        self.review.save()
        resp = self.client.get("/pharmacist-dashboard")
        self.assertNotContains(resp, "Complete your listing")
        self.assertNotContains(resp, "% complete")

    def test_listing_completion_confetti_and_badge(self):
        """Crossing 100% on the listing checklist fires a one-time confetti
        celebration, persists listing_completed_at, and shows the Verified
        badge next to the pharmacy name on the dashboard and public listing."""
        self.client.force_login(self.pharmacist)
        PharmacyClaim.objects.create(
            pharmacy=self.pharmacy,
            pharmacy_name=self.pharmacy.name,
            user=self.pharmacist,
            user_name="Pharm Owner",
            npi_number="1234567890",
            license_number="RX123",
            status="approved",
        )
        PharmacyService.objects.create(pharmacy=self.pharmacy, service_type="delivery")
        PharmacyInsuranceProvider.objects.create(
            pharmacy=self.pharmacy, provider_name="Aetna")
        PharmacyCoveragePlan.objects.create(
            pharmacy=self.pharmacy, plan_name="Medicare Part D", plan_type="Medicare")
        PharmacyHours.objects.create(
            pharmacy=self.pharmacy, day_of_week=0, day_name="Monday",
            open_time="9:00 AM", close_time="5:00 PM")

        # Before the final step: no confetti, no badge.
        resp = self.client.get("/pharmacist-dashboard")
        self.assertContains(resp, "75% complete")
        self.assertNotContains(resp, "Congratulations!")
        self.assertNotContains(resp, "pp-listing-verified-badge")

        # Answer the pending review → 100%. Confetti fires once and the
        # completion is persisted to both the pharmacy and its publishable row.
        self.review.response_text = "Thanks for the feedback!"
        self.review.save()
        resp = self.client.get("/pharmacist-dashboard")
        self.assertContains(resp, "Congratulations!")
        self.assertContains(resp, "pp-confetti-celebration")
        self.assertContains(resp, "pp-listing-verified-badge")
        self.pharmacy.refresh_from_db()
        self.assertIsNotNone(self.pharmacy.listing_completed_at)
        pub = PharmacyPublishable.objects.get(pharmacy_id=self.pharmacy.id)
        self.assertIsNotNone(pub.listing_completed_at)

        # Confetti is one-time; the badge persists.
        resp = self.client.get("/pharmacist-dashboard")
        self.assertNotContains(resp, "Congratulations!")
        self.assertContains(resp, "pp-listing-verified-badge")

        # The badge also appears on the public listing page.
        resp = self.client.get(f"/pharmacy?id={self.pharmacy.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "pp-listing-verified-badge")

    def test_admin_pages_render(self):
        self.client.force_login(self.admin)
        for path in [
            "/admin-dashboard",
            "/admin-analytics",
            "/admin-claims",
            "/admin-moderation",
            "/admin-pharmacies",
            "/admin-shortages",
            "/admin-users",
            "/admin-pharmacy-facts",
        ]:
            self.assert_ok(path)

    def test_pharmacy_slug_url_renders(self):
        """SEO slug URLs (/pharmacy/<id>/<slug>/) render the same pharmacy as
        the legacy /pharmacy?id= query form, and are 301-consistent canonical
        locations."""
        from django.utils.text import slugify
        slug = slugify("Smoke Pharmacy")
        resp = self.client.get(f"/pharmacy/{self.pharmacy.id}/{slug}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Smoke Pharmacy")
        self.assertContains(resp, "Open in Google Maps")

        legacy = self.client.get(f"/pharmacy?id={self.pharmacy.id}")
        self.assertEqual(legacy.status_code, 200)
        self.assertContains(legacy, "google.com/maps")

    def test_pharmacy_detail_map_falls_back_to_address(self):
        """A pharmacy without coordinates still renders a working map using its
        address as the query, instead of a 0,0 / blank embed."""
        self.pharmacy.latitude = None
        self.pharmacy.longitude = None
        self.pharmacy.save(update_fields=["latitude", "longitude"])
        resp = self.client.get(f"/pharmacy?id={self.pharmacy.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "123%20Market%20St")
        self.assertContains(resp, "Open in Google Maps")

    def test_signup_and_login_forms_work(self):
        response = self.client.post(
            "/signup",
            {
                "full_name": "New User",
                "email": "new-user@example.com",
                "zip_code": "19104",
                "password": "demo1234",
            },
        )
        self.assertRedirects(response, "/home", fetch_redirect_response=False)
        new_user = User.objects.filter(email="new-user@example.com").first()
        self.assertIsNotNone(new_user)
        # full_name is split into first/last on save.
        self.assertEqual((new_user.first_name, new_user.last_name), ("New", "User"))

        self.client.logout()
        response = self.client.post(
            "/login",
            {"email": "new-user@example.com", "password": "demo1234", "next": "/home"},
        )
        self.assertRedirects(response, "/home", fetch_redirect_response=False)

    def test_core_api_flows_work(self):
        self.client.force_login(self.user)

        response = self.client.get("/api/flow/search", {"q": "Smoke"})
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.json()["results"]), 1)

        response = self.client.post("/api/flow/compare/add", {"pharmacy_id": self.pharmacy.id})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(SavedComparison.objects.filter(user=self.user, pharmacy=self.pharmacy).exists())

        response = self.client.post("/api/flow/compare/remove", {"pharmacy_id": self.pharmacy.id})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SavedComparison.objects.filter(user=self.user, pharmacy=self.pharmacy).exists())

        response = self.client.post(
            "/api/flow/submit-review",
            {
                "pharmacy_id": self.online.id,
                "pharmacy_name": self.online.name,
                "stock_available": 1,
                "wait_time_rating": 5,
                "service_rating": 5,
                "comment": "Fast service",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Review.objects.filter(comment="Fast service", pharmacy=self.online).exists())

        response = self.client.post("/api/flow/newsletter/subscribe", {"zip_code": "19103"})
        self.assertEqual(response.status_code, 200)
        response = self.client.post("/api/flow/newsletter/unsubscribe")
        self.assertEqual(response.status_code, 200)

    def test_one_review_per_user_per_pharmacy(self):
        """A user account cannot review the same pharmacy more than once."""
        self.client.force_login(self.user)
        base = {
            "pharmacy_id": self.online.id,
            "pharmacy_name": self.online.name,
            "stock_available": 1,
            "wait_time_rating": 4,
            "service_rating": 4,
        }

        # First review succeeds.
        resp = self.client.post("/api/flow/submit-review", dict(base, comment="Great experience"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Review.objects.filter(pharmacy_id=self.online.id, user=self.user).count(), 1)

        # Plain-form duplicate → friendly redirect, nothing new written.
        resp = self.client.post("/api/flow/submit-review", dict(base, comment="Second attempt"))
        self.assertRedirects(
            resp,
            f"/review?pharmacy_id={self.online.id}"
            f"&pharmacy_name=Smoke+Online+Pharmacy"
            f"&error=You+have+already+reviewed+this+pharmacy.",
            fetch_redirect_response=False,
        )
        self.assertEqual(Review.objects.filter(pharmacy_id=self.online.id, user=self.user).count(), 1)

        # AJAX duplicate → 409 JSON with the same message.
        resp = self.client.post(
            "/api/flow/submit-review", dict(base, comment="Ajax duplicate"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(resp.json()["error"], "You have already reviewed this pharmacy.")
        self.assertEqual(Review.objects.filter(pharmacy_id=self.online.id, user=self.user).count(), 1)

        # A different user can still review the same pharmacy.
        other = User.objects.create_user(
            username="other@example.com", email="other@example.com", password="demo1234")
        self.client.force_login(other)
        resp = self.client.post("/api/flow/submit-review", dict(base, comment="From another user"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Review.objects.filter(pharmacy_id=self.online.id, user=other).count(), 1)

    def test_pharmacist_flows_work(self):
        self.client.force_login(self.pharmacist)

        response = self.client.post(
            f"/api/flow/respond-review/{self.review.id}",
            {"response_text": "Thanks for your feedback."},
        )
        self.assertEqual(response.status_code, 302)
        self.review.refresh_from_db()
        self.assertEqual(self.review.response_text, "Thanks for your feedback.")
        self.assertEqual(
            ResponseCount.objects.get(pharmacy=self.pharmacy).count,
            1,
        )

        response = self.client.post(
            "/api/flow/team/invite",
            {"invite_email": "staff@example.com", "invite_role": "member"},
        )
        self.assertEqual(response.status_code, 302)
        member = PharmacyTeamMember.objects.get(email="staff@example.com")
        response = self.client.post(
            f"/api/flow/team/role/{member.id}",
            {"new_role": "manager"},
        )
        self.assertEqual(response.status_code, 302)
        member.refresh_from_db()
        self.assertEqual(member.role, "manager")
        response = self.client.post(f"/api/flow/team/remove/{member.id}")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PharmacyTeamMember.objects.filter(id=member.id).exists())

        response = self.client.post(
            "/api/pharmacy_services",
            {"pharmacy_id": self.pharmacy.id,
             "service_type": ["delivery", "vaccines"],
             "service_type_custom": "Drive-thru"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            PharmacyService.objects.filter(pharmacy=self.pharmacy).count(), 3)
        service = PharmacyService.objects.get(pharmacy=self.pharmacy, service_type="delivery")
        self.assertTrue(PharmacyService.objects.filter(
            pharmacy=self.pharmacy, service_type="Drive-thru").exists())
        response = self.client.post(f"/api/pharmacy_services/{service.id}/remove")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PharmacyService.objects.filter(id=service.id).exists())

        response = self.client.post(
            "/api/coverage_plans",
            {"pharmacy_id": self.pharmacy.id,
             "plan_name": ["Medicare Part D", "Medicaid"],
             "plan_name_custom": "SilverScript",
             "plan_type": "Medicare"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            PharmacyCoveragePlan.objects.filter(pharmacy=self.pharmacy).count(), 3)
        plan = PharmacyCoveragePlan.objects.get(
            pharmacy=self.pharmacy, plan_name="Medicare Part D")
        self.assertEqual(plan.plan_type, "Medicare")
        custom = PharmacyCoveragePlan.objects.get(
            pharmacy=self.pharmacy, plan_name="SilverScript")
        self.assertEqual(custom.plan_type, "Medicare")
        response = self.client.post(f"/api/coverage_plans/{plan.id}/remove")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PharmacyCoveragePlan.objects.filter(id=plan.id).exists())

        response = self.client.post(
            "/api/insurance_providers",
            {"pharmacy_id": self.pharmacy.id,
             "provider_name": ["Cigna", "Aetna"],
             "provider_name_custom": "Highmark"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            PharmacyInsuranceProvider.objects.filter(pharmacy=self.pharmacy).count(), 3)
        provider = PharmacyInsuranceProvider.objects.get(
            pharmacy=self.pharmacy, provider_name="Cigna")
        self.assertTrue(PharmacyInsuranceProvider.objects.filter(
            pharmacy=self.pharmacy, provider_name="Highmark").exists())
        response = self.client.post(f"/api/insurance_providers/{provider.id}/remove")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PharmacyInsuranceProvider.objects.filter(id=provider.id).exists())

        # Combined Insurance tab form: providers + government plans in one POST.
        PharmacyInsuranceProvider.objects.filter(pharmacy=self.pharmacy).delete()
        PharmacyCoveragePlan.objects.filter(pharmacy=self.pharmacy).delete()
        response = self.client.post(
            "/api/insurance",
            {"pharmacy_id": self.pharmacy.id,
             "provider_name": ["Cigna"],
             "plan_name": ["Medicare Part D"],
             "plan_name_custom": "SilverScript",
             "plan_type": "Medicare"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(PharmacyInsuranceProvider.objects.filter(
            pharmacy=self.pharmacy, provider_name="Cigna").exists())
        self.assertEqual(PharmacyCoveragePlan.objects.filter(
            pharmacy=self.pharmacy).count(), 2)
        self.assertEqual(PharmacyCoveragePlan.objects.get(
            pharmacy=self.pharmacy, plan_name="SilverScript").plan_type, "Medicare")

        response = self.client.post(
            "/api/pharmacy_hours",
            {"pharmacy_id": self.pharmacy.id,
             "day_0_open": "9:00 AM", "day_0_close": "9:00 PM",
             "day_6_closed": "1"},
        )
        self.assertEqual(response.status_code, 302)
        from pharmacypulse.models import PharmacyHours
        mon = PharmacyHours.objects.get(pharmacy=self.pharmacy, day_of_week=0)
        self.assertEqual(mon.open_time, "9:00 AM")
        self.assertEqual(mon.close_time, "9:00 PM")
        self.assertEqual(mon.is_closed, 0)
        sun = PharmacyHours.objects.get(pharmacy=self.pharmacy, day_of_week=6)
        self.assertEqual(sun.is_closed, 1)

    def test_admin_api_flows_work(self):
        pending_claim = PharmacyClaim.objects.create(
            pharmacy=self.online,
            pharmacy_name=self.online.name,
            user=self.user,
            user_name="Pat Ient",
            npi_number="1234567890",
            license_number="RX123",
        )
        pending_review = Review.objects.create(
            pharmacy=self.online,
            pharmacy_name=self.online.name,
            user=self.user,
            user_name="Pat Ient",
            service_rating=3,
            wait_time_rating=3,
            moderation_status="pending",
        )

        self.client.force_login(self.admin)

        response = self.client.post("/api/flow/add-shortage", {"drug_name": "Admin Smoke Drug"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(DrugShortage.objects.filter(drug_name="Admin Smoke Drug").exists())

        response = self.client.post(f"/api/flow/moderate-review/{pending_review.id}", {"action": "approved"})
        self.assertEqual(response.status_code, 200)
        pending_review.refresh_from_db()
        self.assertEqual(pending_review.moderation_status, "approved")

        response = self.client.post(f"/api/flow/suspend-user/{self.user.id}")
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.deactivated_at)
        response = self.client.post(f"/api/flow/unsuspend-user/{self.user.id}")
        self.assertEqual(response.status_code, 200)

        response = self.client.post(f"/api/flow/approve-claim/{pending_claim.id}")
        self.assertEqual(response.status_code, 200)
        pending_claim.refresh_from_db()
        self.assertEqual(pending_claim.status, "approved")

        rejected_claim = PharmacyClaim.objects.create(
            pharmacy=self.pharmacy,
            pharmacy_name=self.pharmacy.name,
            user=self.user,
            user_name="Pat Ient",
            npi_number="0987654321",
            license_number="RX456",
        )
        response = self.client.post(
            f"/api/flow/reject-claim/{rejected_claim.id}",
            {"reason": "Missing docs"},
        )
        self.assertEqual(response.status_code, 200)
        rejected_claim.refresh_from_db()
        self.assertEqual(rejected_claim.status, "rejected")

    def test_admin_edits_publishable_pharmacy(self):
        """Admin edit of a pharmacy lands on pharmacies_publishable, flags it
        admin_edited, and toggles publish_status. Non-admins are blocked."""
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/api/flow/edit-pharmacy/{self.pharmacy.id}",
            {
                "name": "Renamed Pharmacy",
                "address": "999 Edited St",
                "city": "New City",
                "state": "CA",
                "zip": "90210",
                "phone": "555-0100",
                "website": "https://example.test",
                "is_digital": "1",
                "publish_status": "unpublished",
            },
        )
        self.assertEqual(response.status_code, 302)
        row = PharmacyPublishable.objects.get(pk=self.pharmacy.id)
        self.assertEqual(row.name, "Renamed Pharmacy")
        self.assertEqual(row.city, "New City")
        self.assertEqual(row.is_digital, 1)
        self.assertEqual(row.publish_status, "unpublished")
        self.assertTrue(row.admin_edited)
        # Source pharmacy reflects the unpublished -> soft-deleted status.
        self.pharmacy.refresh_from_db()
        self.assertEqual(self.pharmacy.status, "deleted")

        # Non-admin is forbidden.
        self.client.force_login(self.user)
        response = self.client.post(
            f"/api/flow/edit-pharmacy/{self.pharmacy.id}", {"name": "Hacked"}
        )
        self.assertEqual(response.status_code, 403)
