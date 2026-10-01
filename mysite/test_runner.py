"""Block real networking for ordinary Django tests, including unmocked AI."""
from contextlib import ExitStack, contextmanager
import os
from unittest.mock import patch

from django.test import override_settings
from django.test.runner import DiscoverRunner


class ExternalNetworkBlocked(BaseException):
    # SDKs commonly catch Exception and convert connection errors into normal
    # API failures. Bypass that catch so an accidental call fails the test.
    pass


def _blocked_network(*args, **kwargs):
    raise ExternalNetworkBlocked("External network access is forbidden in tests. Mock the service boundary.")


@contextmanager
def block_external_network():
    with ExitStack() as stack:
        for target in (
            "socket.create_connection", "socket.getaddrinfo", "socket.socket.connect",
            "socket.socket.connect_ex", "socket.socket.sendto",
        ):
            stack.enter_context(patch(target, _blocked_network))
        yield


class NoNetworkDiscoverRunner(DiscoverRunner):
    def run_tests(self, test_labels, **kwargs):
        # Spawned parallel workers don't inherit process-local patches on all
        # supported hosts. Refuse that mode until a worker guard is implemented.
        if self.parallel > 1:
            raise RuntimeError("The network-guarded test runner requires serial execution.")
        with (
            block_external_network(),
            patch.dict(os.environ, {"OPENAI_API_KEY": "", "GEMINI_API_KEY": ""}),
            override_settings(OPENAI_API_KEY=""),
        ):
            return super().run_tests(test_labels, **kwargs)
