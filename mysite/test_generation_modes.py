from django.test import SimpleTestCase

from proposal_ai import services


class GenerationModeConfigurationTests(SimpleTestCase):
    def test_quick_configuration(self):
        config = services.proposal_generation_config("quick")

        self.assertEqual(config["model"], "gpt-5-mini")
        self.assertEqual(config["reasoning_effort"], "low")
        self.assertEqual(config["completion_tokens"], 4096)

    def test_advanced_configuration(self):
        config = services.proposal_generation_config("advanced")

        self.assertEqual(config["model"], "gpt-5")
        self.assertEqual(config["reasoning_effort"], "medium")
        self.assertEqual(config["completion_tokens"], 6144)

    def test_invalid_mode_rejected(self):
        for mode in ("premium", "unknown", "", None):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    services.proposal_generation_config(mode)

    def test_configuration_cannot_be_mutated_externally(self):
        config = services.proposal_generation_config("quick")
        config["model"] = "modified"

        self.assertEqual(
            services.proposal_generation_config("quick")["model"],
            "gpt-5-mini",
        )

    def test_quick_telemetry(self):
        telemetry = services.request_telemetry(
            "proposal_generation",
            generation_mode="quick",
        )

        self.assertEqual(telemetry.requested_model, "gpt-5-mini")
        self.assertEqual(telemetry.completion_token_cap, 4096)

    def test_advanced_telemetry(self):
        telemetry = services.request_telemetry(
            "proposal_generation",
            generation_mode="advanced",
        )

        self.assertEqual(telemetry.requested_model, "gpt-5")
        self.assertEqual(telemetry.completion_token_cap, 6144)

    def test_mode_not_allowed_for_other_operations(self):
        for operation in ("job_extraction", "profile_summary"):
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError):
                    services.request_telemetry(
                        operation,
                        generation_mode="quick",
                    )


from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class GenerationModeProviderTests(SimpleTestCase):
    def _run_mode(self, mode):
        config = services.proposal_generation_config(mode)

        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="A tailored proposal."),
                )
            ],
            model=config["model"],
            usage=SimpleNamespace(
                prompt_tokens=30,
                completion_tokens=20,
                total_tokens=50,
            ),
        )

        client = MagicMock()
        client.__enter__.return_value = client
        client.chat.completions.create.return_value = response

        with (
            patch("proposal_ai.services._create_client", return_value=client),
            patch("proposal_ai.services._check_dispatch"),
        ):
            result = services.generate_proposal(
                "Write an accurate proposal.",
                "Client needs a Django developer.",
                generation_mode=mode,
            )

        kwargs = client.chat.completions.create.call_args.kwargs

        self.assertEqual(kwargs["model"], config["model"])
        self.assertEqual(
            kwargs["max_completion_tokens"],
            config["completion_tokens"],
        )
        self.assertEqual(
            kwargs["reasoning_effort"],
            config["reasoning_effort"],
        )
        self.assertEqual(
            result.telemetry.requested_model,
            config["model"],
        )
        self.assertEqual(
            result.telemetry.completion_token_cap,
            config["completion_tokens"],
        )
        self.assertEqual(result.value, "A tailored proposal.")
        client.chat.completions.create.assert_called_once()

    def test_quick_provider_request(self):
        self._run_mode("quick")

    def test_advanced_provider_request(self):
        self._run_mode("advanced")
