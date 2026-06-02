from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone

from proposal_ai.models import (
    FreelancerProfile,
    WorkExperience,
    JobPost,
    Proposal,
    ProposalUseConfirmation,
    ProposalOutcome,
)


class Command(BaseCommand):
    help = "Seeds multiple demo users and realistic workflow data"

    def handle(self, *args, **kwargs):

        demo_users = [
            {
                "username": "demo_python_dev",
                "email": "python@example.com",
                "title": "Python Backend Developer",
                "summary": "Django and PostgreSQL developer focused on APIs and AI-powered web applications.",
                "tone": "Professional",
                "experiences": [
                    {
                        "job_title": "Django Backend Developer",
                        "company_or_project": "AI Proposal Generator",
                        "tasks": "Built Django views, authentication, PostgreSQL models, and OpenAI API proposal generation.",
                        "skills_used": "Python, Django, PostgreSQL, OpenAI API, HTML, CSS",
                        "experience_depth": "Python - 2 years\nDjango - 1 year\nPostgreSQL - 6 months",
                    },
                    {
                        "job_title": "API Workflow Builder",
                        "company_or_project": "ProposalIQ",
                        "tasks": "Designed job extraction, proposal generation, and usage confirmation workflows.",
                        "skills_used": "Django, database design, AI workflow design",
                        "experience_depth": "API workflows - 1 year\nDatabase design - 6 months",
                    },
                ],
                "jobs": [
                    {
                        "platform": "Upwork",
                        "job_title": "Python Developer for AI Document Analysis",
                        "job_description": "Need a Python developer to improve an AI-powered document analysis tool using Django and PostgreSQL.",
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
                        "proposal_text": "Hello, I can help stabilise and expand your Django-based AI document analysis tool. My experience with Python, PostgreSQL and AI workflows makes me a strong fit for improving both backend reliability and proposal-facing functionality.",
                        "used": True,
                        "platform_used": "Upwork",
                        "client_name": "AI Document Client",
                        "job_url": "https://example.com/job/python-ai-document-analysis",
                        "outcome": "interview",
                    },
                    {
                        "platform": "Freelancer",
                        "job_title": "Build REST API with Django",
                        "job_description": "Looking for a Django developer to build secure REST APIs connected to PostgreSQL.",
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
                        "proposal_text": "Hi, I can build the Django REST API you need with clean database models, authentication and PostgreSQL integration. I have worked directly on similar backend workflows.",
                        "used": True,
                        "platform_used": "Freelancer",
                        "client_name": "UK API Founder",
                        "job_url": "https://example.com/job/django-rest-api",
                        "outcome": "reply",
                    },
                    {
                        "platform": "Upwork",
                        "job_title": "Fix Django Database Bugs",
                        "job_description": "Need help debugging Django migrations and PostgreSQL relationship issues.",
                        "budget_type": "Hourly",
                        "hourly_min": 15,
                        "hourly_max": 25,
                        "experience_level": "Intermediate",
                        "project_duration": "Less than 1 month",
                        "hours_per_week": "Less than 10 hrs/week",
                        "skills_required": "Django, PostgreSQL, Debugging",
                        "client_location": "Europe",
                        "proposal_count": "5 to 10",
                        "interviewing_count": "1",
                        "invites_sent": "0",
                        "proposal_text": "I can help debug your Django migration and PostgreSQL issues. I have recently worked through similar database relationship problems and can identify the root cause quickly.",
                        "used": False,
                        "outcome": "generated",
                    },
                ],
            },
            {
                "username": "demo_data_entry",
                "email": "dataentry@example.com",
                "title": "Data Entry Specialist",
                "summary": "Detail-oriented freelancer focused on accurate data entry, spreadsheet organisation and online admin tasks.",
                "tone": "Friendly and professional",
                "experiences": [
                    {
                        "job_title": "Data Entry Assistant",
                        "company_or_project": "Remote Admin Tasks",
                        "tasks": "Entered, cleaned and organised spreadsheet data from online sources.",
                        "skills_used": "Excel, Google Sheets, data accuracy, typing",
                        "experience_depth": "Data entry - 1 year\nGoogle Sheets - 1 year",
                    },
                    {
                        "job_title": "Virtual Admin Support",
                        "company_or_project": "Small Business Support",
                        "tasks": "Organised customer records, maintained spreadsheets and handled simple admin workflows.",
                        "skills_used": "Administration, spreadsheets, communication",
                        "experience_depth": "Admin support - 1 year\nSpreadsheets - 1 year",
                    },
                ],
                "jobs": [
                    {
                        "platform": "Upwork",
                        "job_title": "Data Entry for Beginners",
                        "job_description": "Need individuals for simple online data entry tasks. No experience required.",
                        "budget_type": "Hourly",
                        "hourly_min": 5,
                        "hourly_max": 10,
                        "experience_level": "Entry Level",
                        "project_duration": "1 month",
                        "hours_per_week": "Less than 30 hrs/week",
                        "skills_required": "Data Entry, Google Sheets, Accuracy",
                        "client_location": "United States",
                        "proposal_count": "50+",
                        "interviewing_count": "3",
                        "invites_sent": "0",
                        "proposal_text": "Hello, I am detail-oriented and comfortable with online data entry tasks. I can follow instructions carefully and keep spreadsheet data organised and accurate.",
                        "used": True,
                        "platform_used": "Upwork",
                        "client_name": "Admin Client",
                        "job_url": "https://example.com/job/data-entry-beginner",
                        "outcome": "no_response",
                    },
                    {
                        "platform": "PeoplePerHour",
                        "job_title": "Spreadsheet Cleanup Assistant",
                        "job_description": "Clean and organise messy spreadsheet records.",
                        "budget_type": "Fixed",
                        "hourly_min": None,
                        "hourly_max": None,
                        "experience_level": "Entry Level",
                        "project_duration": "Less than 1 month",
                        "hours_per_week": "Flexible",
                        "skills_required": "Excel, Google Sheets, Data Cleaning",
                        "client_location": "United Kingdom",
                        "proposal_count": "10 to 15",
                        "interviewing_count": "2",
                        "invites_sent": "1",
                        "proposal_text": "Hi, I can help clean and organise your spreadsheet records with accuracy and clear formatting. I am comfortable working carefully through repetitive admin tasks.",
                        "used": True,
                        "platform_used": "PeoplePerHour",
                        "client_name": "Spreadsheet Client",
                        "job_url": "https://example.com/job/spreadsheet-cleanup",
                        "outcome": "hired",
                    },
                ],
            },
            {
                "username": "demo_designer",
                "email": "designer@example.com",
                "title": "Graphic Designer",
                "summary": "Creative designer focused on social media graphics, brand assets and clean modern visuals.",
                "tone": "Creative and confident",
                "experiences": [
                    {
                        "job_title": "Social Media Designer",
                        "company_or_project": "Instagram Brand Pack",
                        "tasks": "Created social media templates, promotional graphics and simple brand visuals.",
                        "skills_used": "Canva, Photoshop, branding, layout design",
                        "experience_depth": "Canva - 2 years\nBrand graphics - 1 year",
                    },
                    {
                        "job_title": "Logo Concept Designer",
                        "company_or_project": "Startup Logo Concepts",
                        "tasks": "Designed simple logo concepts and visual identity ideas for small businesses.",
                        "skills_used": "Logo design, typography, colour selection",
                        "experience_depth": "Logo design - 1 year\nTypography - 1 year",
                    },
                ],
                "jobs": [
                    {
                        "platform": "Fiverr",
                        "job_title": "Create Social Media Graphics",
                        "job_description": "Need 10 Instagram graphics for a fitness brand.",
                        "budget_type": "Fixed",
                        "hourly_min": None,
                        "hourly_max": None,
                        "experience_level": "Intermediate",
                        "project_duration": "Less than 1 month",
                        "hours_per_week": "Flexible",
                        "skills_required": "Canva, Instagram Design, Branding",
                        "client_location": "Canada",
                        "proposal_count": "15 to 20",
                        "interviewing_count": "5",
                        "invites_sent": "2",
                        "proposal_text": "Hi, I can create clean and engaging Instagram graphics for your fitness brand, using consistent colours, strong layout and scroll-stopping visuals.",
                        "used": True,
                        "platform_used": "Fiverr",
                        "client_name": "Fitness Brand Owner",
                        "job_url": "https://example.com/job/instagram-graphics",
                        "outcome": "reply",
                    },
                    {
                        "platform": "Upwork",
                        "job_title": "Simple Logo Design Needed",
                        "job_description": "Need a clean logo concept for a small online business.",
                        "budget_type": "Fixed",
                        "hourly_min": None,
                        "hourly_max": None,
                        "experience_level": "Entry Level",
                        "project_duration": "Less than 1 month",
                        "hours_per_week": "Flexible",
                        "skills_required": "Logo Design, Typography, Branding",
                        "client_location": "Australia",
                        "proposal_count": "20 to 50",
                        "interviewing_count": "6",
                        "invites_sent": "3",
                        "proposal_text": "Hello, I can design a clean logo concept that fits your online business and provide a simple visual direction that feels professional and memorable.",
                        "used": False,
                        "outcome": "generated",
                    },
                ],
            },
            {
                "username": "demo_virtual_assistant",
                "email": "va@example.com",
                "title": "Virtual Assistant",
                "summary": "Organised virtual assistant helping small businesses with inbox management, scheduling and admin support.",
                "tone": "Helpful and reliable",
                "experiences": [
                    {
                        "job_title": "Virtual Assistant",
                        "company_or_project": "Small Business Admin Support",
                        "tasks": "Managed inbox tasks, calendar updates, customer records and simple admin workflows.",
                        "skills_used": "Email management, scheduling, admin support",
                        "experience_depth": "Virtual assistance - 2 years\nInbox management - 1 year",
                    },
                    {
                        "job_title": "Customer Support Assistant",
                        "company_or_project": "Remote Support Project",
                        "tasks": "Handled basic customer queries and organised support tickets.",
                        "skills_used": "Customer service, communication, ticket management",
                        "experience_depth": "Customer service - 2 years\nRemote support - 1 year",
                    },
                ],
                "jobs": [
                    {
                        "platform": "Upwork",
                        "job_title": "Virtual Assistant Needed for Email Management",
                        "job_description": "Looking for a reliable VA to manage emails, scheduling and simple admin tasks.",
                        "budget_type": "Hourly",
                        "hourly_min": 8,
                        "hourly_max": 15,
                        "experience_level": "Entry Level",
                        "project_duration": "3 to 6 months",
                        "hours_per_week": "Less than 30 hrs/week",
                        "skills_required": "Email Management, Scheduling, Admin Support",
                        "client_location": "United States",
                        "proposal_count": "20 to 50",
                        "interviewing_count": "8",
                        "invites_sent": "4",
                        "proposal_text": "Hello, I can help manage your emails, scheduling and admin tasks reliably. I am organised, responsive and comfortable following clear processes.",
                        "used": True,
                        "platform_used": "Upwork",
                        "client_name": "Small Business Owner",
                        "job_url": "https://example.com/job/va-email-management",
                        "outcome": "interview",
                    }
                ],
            },
            {
                "username": "demo_security_cctv",
                "email": "security@example.com",
                "title": "Security and CCTV Specialist",
                "summary": "Security professional with experience in CCTV monitoring, incident reporting and front-of-house protection duties.",
                "tone": "Professional and trustworthy",
                "experiences": [
                    {
                        "job_title": "Protection Officer",
                        "company_or_project": "University Security Contract",
                        "tasks": "Monitored premises, handled access control, created incident reports and supported public safety.",
                        "skills_used": "Security, CCTV, communication, incident reporting",
                        "experience_depth": "Security operations - 4 years\nCCTV monitoring - 2 years",
                    },
                    {
                        "job_title": "CCTV Operator",
                        "company_or_project": "Control Room Monitoring",
                        "tasks": "Monitored CCTV feeds, escalated incidents and maintained accurate logs.",
                        "skills_used": "CCTV, observation, reporting, escalation",
                        "experience_depth": "CCTV operations - 2 years\nIncident logging - 2 years",
                    },
                ],
                "jobs": [
                    {
                        "platform": "Freelancer",
                        "job_title": "CCTV Monitoring Procedure Writer",
                        "job_description": "Need someone with security experience to write CCTV monitoring procedures and incident response notes.",
                        "budget_type": "Fixed",
                        "hourly_min": None,
                        "hourly_max": None,
                        "experience_level": "Intermediate",
                        "project_duration": "Less than 1 month",
                        "hours_per_week": "Flexible",
                        "skills_required": "CCTV, Security Procedures, Report Writing",
                        "client_location": "United Kingdom",
                        "proposal_count": "5 to 10",
                        "interviewing_count": "2",
                        "invites_sent": "1",
                        "proposal_text": "Hello, I have practical security and CCTV experience and can help write clear monitoring procedures and incident response notes based on real operational practice.",
                        "used": True,
                        "platform_used": "Freelancer",
                        "client_name": "Security Consultant",
                        "job_url": "https://example.com/job/cctv-procedure-writer",
                        "outcome": "hired",
                    }
                ],
            },
        ]

        for demo in demo_users:
            user, created = User.objects.get_or_create(
                username=demo["username"],
                defaults={"email": demo["email"]},
            )

            if created:
                user.set_password("password123")
                user.save()
                self.stdout.write(self.style.SUCCESS(f"Created user: {demo['username']}"))
            else:
                self.stdout.write(self.style.WARNING(f"User already exists: {demo['username']}"))

            FreelancerProfile.objects.update_or_create(
                user=user,
                defaults={
                    "professional_title": demo["title"],
                    "profile_summary": demo["summary"],
                    "preferred_tone": demo["tone"],
                },
            )

            for exp in demo["experiences"]:
                WorkExperience.objects.update_or_create(
                    user=user,
                    job_title=exp["job_title"],
                    defaults=exp,
                )

            for job in demo["jobs"]:
                job_post, _ = JobPost.objects.update_or_create(
                    user=user,
                    job_title=job["job_title"],
                    defaults={
                        "platform": job["platform"],
                        "job_description": job["job_description"],
                        "budget_type": job["budget_type"],
                        "hourly_min": job["hourly_min"],
                        "hourly_max": job["hourly_max"],
                        "experience_level": job["experience_level"],
                        "project_duration": job["project_duration"],
                        "hours_per_week": job["hours_per_week"],
                        "skills_required": job["skills_required"],
                        "client_location": job["client_location"],
                        "proposal_count": job["proposal_count"],
                        "interviewing_count": job["interviewing_count"],
                        "invites_sent": job["invites_sent"],
                        "confirmed_by_user": True,
                        "raw_job_text": job["job_description"],
                    },
                )

                proposal, _ = Proposal.objects.update_or_create(
                    user=user,
                    job_post=job_post,
                    defaults={
                        "final_text": job["proposal_text"],
                        "status": "used" if job["used"] else "generated",
                        "used_by_user": job["used"],
                        "used_at": timezone.now() if job["used"] else None,
                    },
                )

                if job["used"]:
                    ProposalUseConfirmation.objects.update_or_create(
                        proposal=proposal,
                        defaults={
                            "platform": job["platform_used"],
                            "client_name": job["client_name"],
                            "job_url": job["job_url"],
                            "submitted_proposal_text": job["proposal_text"],
                            "notes": f"Demo usage record. Simulated outcome: {job['outcome']}",
                        },
                    )

                if job["outcome"] != "generated":
                    ProposalOutcome.objects.update_or_create(
                        proposal=proposal,
                        defaults={
                            "status": job["outcome"]
                        },
                    )

        self.stdout.write(
            self.style.SUCCESS("Demo data seeding completed successfully.")
        )