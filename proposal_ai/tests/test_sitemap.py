
from django.test import SimpleTestCase, override_settings
from django.urls import reverse


class PublicSitemapTests(SimpleTestCase):

    @override_settings(ALLOWED_HOSTS=["proposalq.com"])
    def test_sitemap_contains_only_public_homepage(self):
        response = self.client.get(
            reverse("django.contrib.sitemaps.views.sitemap"),
            HTTP_HOST="proposalq.com",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)

        content = response.content.decode()

        self.assertIn(
            "<loc>https://proposalq.com/</loc>",
            content,
        )

        self.assertEqual(content.count("<loc>"), 1)

        self.assertNotIn("/dashboard/", content)
        self.assertNotIn("/my-experiences/", content)
        self.assertNotIn("/use-proposal/", content)
