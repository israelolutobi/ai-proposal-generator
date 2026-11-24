from django.test import SimpleTestCase, override_settings
from django.urls import reverse


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
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

    @override_settings(ALLOWED_HOSTS=["proposalq.com"])
    def test_public_product_pages_are_accessible_and_self_canonical(self):
        for name, path in [
            ("public_home", "/"),
            ("pricing", "/pricing/"),
            ("public_demo", "/demo/"),
        ]:
            with self.subTest(page=name):
                self.assertEqual(reverse(name), path)
                response = self.client.get(
                    path,
                    HTTP_HOST="proposalq.com",
                    secure=True,
                )
                self.assertEqual(response.status_code, 200)
                content = response.content.decode()
                canonical = f'<link rel="canonical" href="https://proposalq.com{path}">'
                self.assertIn(canonical, content)
                self.assertEqual(content.count('rel="canonical"'), 1)
                self.assertNotIn('name="robots" content="noindex"', content.lower())
                self.assertNotIn("noindex", response.get("X-Robots-Tag", "").lower())
