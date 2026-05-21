from django.core.management.base import BaseCommand
from django.contrib.auth.models import User

from proposal_ai.models import (
    FreelancerProfile,
    WorkExperience,
    JobPost,
    Proposal
)


class Command(BaseCommand):

    help = "Seeds demo users and fake data into the database"

    def handle(self, *args, **kwargs):

        # CREATE USER
        user, created = User.objects.get_or_create(
            username="demo_python_dev",
            defaults={
                "email": "demo_python_dev@example.com"
            }
        )

        if created:
            user.set_password("password123")
            user.save()

            self.stdout.write(
                self.style.SUCCESS("Created demo user")
            )

        else:
            self.stdout.write(
                self.style.WARNING("Demo user already exists")
            )

        # CREATE PROFILE
        profile, profile_created = FreelancerProfile.objects.get_or_create(
            user=user,
            defaults={
                "professional_title": "Python Backend Developer",
                "profile_summary": (
                    "Experienced Python developer specialising in Django, "
                    "PostgreSQL and AI-powered applications."
                ),
                "preferred_tone": "Professional"
            }
        )

        if profile_created:
            self.stdout.write(
                self.style.SUCCESS("Created freelancer profile")
            )

        else:
            self.stdout.write(
                self.style.WARNING("Freelancer profile already exists")
            )

        # CREATE WORK EXPERIENCES
        experiences = [
            {
                "job_title": "Django Backend Developer",
                "company_or_project": "AI Proposal Generator Project",
                "tasks": (
                    "Built Django views, connected PostgreSQL database, "
                    "created authentication flow, and integrated AI proposal generation."
                ),
                "skills_used": "Python, Django, PostgreSQL, HTML, CSS, OpenAI API",
                "experience_depth": (
                    "Python - 2 years\n"
                    "Django - 1 year\n"
                    "PostgreSQL - 6 months\n"
                    "OpenAI API - current project"
                )
            },
            {
                "job_title": "AI Workflow Builder",
                "company_or_project": "ProposalIQ",
                "tasks": (
                    "Designed a workflow for extracting job features, confirming structured data, "
                    "generating proposals, and tracking proposal usage."
                ),
                "skills_used": "AI workflows, prompt engineering, database design, Django",
                "experience_depth": (
                    "Prompt engineering - 1 year\n"
                    "AI workflow design - 6 months\n"
                    "Database modelling - 6 months"
                )
            }
        ]

        for item in experiences:

            experience, exp_created = WorkExperience.objects.get_or_create(
                user=user,
                job_title=item["job_title"],
                defaults=item
            )

            if exp_created:

                self.stdout.write(
                    self.style.SUCCESS(
                        f"Created work experience: {item['job_title']}"
                    )
                )

            else:

                self.stdout.write(
                    self.style.WARNING(
                        f"Work experience already exists: {item['job_title']}"
                    )
                )

        # CREATE JOB POSTS
        jobs = [
            {
                "platform": "Upwork",
                "job_title": "Python Developer for AI Document Analysis",
                "job_description": (
                    "Looking for a Python developer experienced with Django, "
                    "PostgreSQL and AI systems for PDF document analysis."
                ),
                "budget_type": "Hourly",
                "hourly_min": 19,
                "hourly_max": 30,
                "experience_level": "Intermediate",
                "project_duration": "1 to 3 months",
                "hours_per_week": "Less than 30 hrs/week",
                "skills_required": "Python, Django, PostgreSQL, JavaScript",
                "client_location": "Worldwide",
                "proposal_count": "20 to 50",
                "interviewing_count": "11",
                "invites_sent": "6",
                "confirmed_by_user": True,
            },
            {
                "platform": "Freelancer",
                "job_title": "Backend API Developer",
                "job_description": (
                    "Need a backend developer to build REST APIs using Django "
                    "and PostgreSQL."
                ),
                "budget_type": "Fixed",
                "hourly_min": None,
                "hourly_max": None,
                "experience_level": "Intermediate",
                "project_duration": "2 months",
                "hours_per_week": "Flexible",
                "skills_required": "Python, Django REST Framework, PostgreSQL",
                "client_location": "United Kingdom",
                "proposal_count": "10 to 15",
                "interviewing_count": "4",
                "invites_sent": "2",
                "confirmed_by_user": True,
            }
        ]

        created_jobs = []

        for item in jobs:

            job_post, job_created = JobPost.objects.get_or_create(
                user=user,
                job_title=item["job_title"],
                defaults=item
            )

            created_jobs.append(job_post)

            if job_created:

                self.stdout.write(
                    self.style.SUCCESS(
                        f"Created job post: {item['job_title']}"
                    )
                )

            else:

                self.stdout.write(
                    self.style.WARNING(
                        f"Job post already exists: {item['job_title']}"
                    )
                )

        # CREATE PROPOSALS
        proposals = [
            {
                "job_title": "Python Developer for AI Document Analysis",
                "proposal_text": (
                    "Hello,\n\n"
                    "I am an experienced Python and Django developer with "
                    "experience building AI-powered workflows and PostgreSQL systems..."
                )
            },
            {
                "job_title": "Backend API Developer",
                "proposal_text": (
                    "Hi,\n\n"
                    "I would be a strong fit for your API development project. "
                    "I have experience building scalable Django REST APIs..."
                )
            }
        ]

        for item in proposals:

            matching_job = None

            for job in created_jobs:

                if job.job_title == item["job_title"]:
                    matching_job = job
                    break

            if matching_job:

                proposal, proposal_created = Proposal.objects.get_or_create(
                    user=user,
                    job_post=matching_job,
                    defaults={
                        "final_text": item["proposal_text"],
                        "status": "generated"
                    }
                )

                if proposal_created:

                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Created proposal for: {matching_job.job_title}"
                        )
                    )

                else:

                    self.stdout.write(
                        self.style.WARNING(
                            f"Proposal already exists for: {matching_job.job_title}"
                        )
                    )