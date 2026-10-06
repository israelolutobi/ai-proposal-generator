from django.contrib.auth import get_user_model
from django.test import TestCase

from proposal_ai.models import JobPost, ResearchCase, ResearchDataset, ResearchNote, ResearchRequirement
from proposal_ai.research_intelligence import build_research_context, build_research_context_from_application


class ResearchIntelligenceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="research-test", password="test-only-password")
        self.dataset = ResearchDataset.objects.create(source_name="research.xlsx", source_sha256="a" * 64, case_count=2)
        self.django_case = ResearchCase.objects.create(
            dataset=self.dataset, case_key="C001", outcome="Hired", application_route="Cold",
            domain_niche="Python Django automation", job_title="Django automation takeover",
            primary_stack_domain="Python Django automation", required_skills="Django debugging existing codebase",
            observable_fit_signals="Prior Django takeover experience", strong_features="Direct comparable experience",
            observable_gaps="No Gmail API example", instruction_coverage="Explicit", record_status="Complete",
            source_fidelity="Full text supplied", outcome_confounds="Other applicants unknown",
        )
        ResearchRequirement.objects.create(research_case=self.django_case, requirement="Take over existing codebase",
                                           importance="Core", coverage="Explicit")
        ResearchCase.objects.create(dataset=self.dataset, case_key="C002", outcome="No response",
                                    domain_niche="graphic design branding", job_title="Logo designer")
        ResearchNote.objects.create(dataset=self.dataset, case_key="ROUND", note_type="Research rule",
                                    observation="Treat the full application event as the unit of analysis.",
                                    do_not_overclaim="Avoid causal conclusions from small observational samples.")

    def test_retrieval_selects_relevant_case_and_preserves_uncertainty(self):
        job = JobPost(user=self.user, job_title="Senior Django developer", job_description="Take over an existing Django automation codebase",
                      skills_required="Python Django debugging", budget_type="Hourly")
        context = build_research_context(job)
        self.assertEqual(context.total_cases, 2)
        self.assertEqual(context.matched_cases, 1)
        self.assertIn("C001", context.text)
        self.assertNotIn("C002", context.text)
        self.assertIn("observational", context.text)
        self.assertIn("not proof", context.text)
        self.assertNotIn("research-test", context.text)

    def test_real_application_context_uses_database_but_direct_service_context_does_not(self):
        direct = build_research_context_from_application("Exact context")
        self.assertEqual(direct.text, "")
        application = """FREELANCER PROFILE\nDeveloper\n\nCONFIRMED JOB DETAILS\nJob Title: Django developer\nJob Description:\nTake over Django automation\nBudget Type: Hourly\nSkills Required: Python Django\nClient Location: UK"""
        context = build_research_context_from_application(application)
        self.assertEqual(context.dataset_id, self.dataset.pk)
        self.assertIn("C001", context.text)

    def test_latest_active_dataset_is_used(self):
        self.dataset.active = False
        self.dataset.save(update_fields=["active"])
        newer = ResearchDataset.objects.create(source_name="new.xlsx", source_sha256="b" * 64, case_count=0)
        context = build_research_context(JobPost(job_title="Django", job_description="", skills_required="", budget_type=""))
        self.assertEqual(context.dataset_id, newer.pk)
        self.assertEqual(context.matched_cases, 0)
