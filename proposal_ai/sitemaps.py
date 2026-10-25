
from django.contrib.sitemaps import Sitemap
from django.urls import reverse


class PublicSitemap(Sitemap):
    changefreq = "weekly"
    priority = 1.0
    protocol = "https"

    def items(self):
        return ["public_home"]

    def location(self, item):
        return reverse(item)
