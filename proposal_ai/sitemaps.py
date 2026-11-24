from django.contrib.sitemaps import Sitemap
from django.urls import reverse


class PublicSitemap(Sitemap):
    """List public product pages that have canonical, indexable URLs."""

    protocol = "https"

    def items(self):
        return ["public_home", "pricing", "public_demo"]

    def location(self, item):
        return reverse(item)
