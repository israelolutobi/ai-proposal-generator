from django.test import SimpleTestCase, override_settings
from django.urls import reverse


class PublicSitemapTests(SimpleTestCase):

    @override_settings(ALLOWED_HOSTS=["proposalq.com"])
    def test_sitemap_contains_only_public_product_pages(self):
        response = self.client.get(
            reverse("django.contrib.sitemaps.views.sitemap"),
            HTTP_HOST="proposalq.com",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()

        expected_paths = ["/", "/pricing/", "/demo/"]
        for path in expected_paths:
            self.assertIn(f"<loc>https://proposalq.com{path}</loc>", content)
        self.assertEqual(content.count("<loc>"), len(expected_paths))

        for path in [
            "/dashboard/",
            "/my-experiences/",
            "/use-proposal/",
            "/login/",
            "/register/",
            "/billing/checkout/",
            "/billing/webhook/",
        ]:
            self.assertNotIn(f"<loc>https://proposalq.com{path}</loc>", content)
