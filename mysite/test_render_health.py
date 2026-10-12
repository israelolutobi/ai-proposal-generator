"""Render probe regression checks; synthetic configuration, no network or DB."""
from django.test import SimpleTestCase

from mysite.test_production_configuration import configured_production
from scripts.start_web import render_hostname


HOSTS = "proposalq.com,www.proposalq.com,proposalq-beta.onrender.com,localhost,127.0.0.1"


class RenderHealthTests(SimpleTestCase):
    def test_saved_hosts_accept_render_probe_with_port_over_http_and_https(self):
        with configured_production(ALLOWED_HOSTS=HOSTS, TRUST_PROXY_HEADERS="True"):
            for host in HOSTS.split(","):
                for method in ("get", "head"):
                    for scheme in (None, "http", "https"):
                        with self.subTest(host=host, method=method, scheme=scheme):
                            headers = {"HTTP_HOST": host + ":10000"}
                            if scheme is not None:
                                headers["HTTP_X_FORWARDED_PROTO"] = scheme
                            response = getattr(self.client, method)("/health/live/", **headers)
                            self.assertEqual(response.status_code, 200 if scheme == "https" else 301)
                            if scheme == "https":
                                self.assertEqual(response.content, b"ok\n" if method == "get" else b"")
                                self.assertIn("no-store", response["Cache-Control"])
                                self.assertEqual(response["Strict-Transport-Security"], "max-age=300")
                            else:
                                self.assertEqual(response["Location"], f"https://{host}:10000/health/live/")

    def test_service_only_fallback_rejects_custom_domain(self):
        environment = {"RENDER": "true", "RENDER_EXTERNAL_HOSTNAME": "proposalq-beta.onrender.com"}
        render_hostname(environment)
        with configured_production(ALLOWED_HOSTS=environment["ALLOWED_HOSTS"], TRUST_PROXY_HEADERS="True"):
            for scheme in ("http", "https"):
                with self.subTest(scheme=scheme), self.assertLogs("django.security.DisallowedHost", level="ERROR"):
                    response = self.client.get("/health/live/", HTTP_HOST="proposalq.com:10000",
                                               HTTP_X_FORWARDED_PROTO=scheme)
                    self.assertEqual(response.status_code, 400)

    def test_untrusted_and_malformed_hosts_remain_rejected(self):
        with configured_production(ALLOWED_HOSTS=HOSTS, TRUST_PROXY_HEADERS="True"):
            for host in ("untrusted.invalid", "proposalq.com.attacker.invalid", "https://proposalq.com"):
                with self.subTest(host=host), self.assertLogs("django.security.DisallowedHost", level="ERROR"):
                    self.assertEqual(self.client.get("/health/live/", HTTP_HOST=host,
                                                     HTTP_X_FORWARDED_PROTO="https").status_code, 400)

    def test_forwarded_host_is_not_trusted(self):
        with configured_production(ALLOWED_HOSTS=HOSTS, TRUST_PROXY_HEADERS="True"):
            response = self.client.get("/health/live/", HTTP_HOST="proposalq.com:10000",
                                       HTTP_X_FORWARDED_HOST="untrusted.invalid", HTTP_X_FORWARDED_PROTO="https")
            self.assertEqual(response.status_code, 200)

    def test_forwarded_https_is_ignored_without_explicit_proxy_trust(self):
        with configured_production(ALLOWED_HOSTS=HOSTS, TRUST_PROXY_HEADERS="False"):
            response = self.client.get("/health/live/", HTTP_HOST="proposalq.com:10000", HTTP_X_FORWARDED_PROTO="https")
            self.assertEqual(response.status_code, 301)
