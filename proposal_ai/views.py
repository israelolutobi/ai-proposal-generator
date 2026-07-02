from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from .models import FreelancerProfile, JobPost, Proposal, ProposalOutcome  # later expand
from openai import OpenAI
from django.contrib.auth.models import User
import os
from django.contrib.auth import authenticate, login, logout
from .models import WorkExperience
from django.shortcuts import get_object_or_404
from .models import ProposalUseConfirmation
from django.utils import timezone
import json
from .models import JobPost, Proposal
from proposal_ai.models import ProposalOutcome
from .models import Proposal, ProposalOutcome, ProposalUseConfirmation


# load_dotenv()

def register_user(request):
    error_message = None

    if request.method == "POST":
        username = request.POST.get("username")
        email = request.POST.get("email")
        password = request.POST.get("password")
        confirm_password = request.POST.get("confirm_password")

        if password != confirm_password:
            error_message = "Passwords do not match."

        elif User.objects.filter(username=username).exists():
            error_message = "Username already exists."

        elif User.objects.filter(email=email).exists():
            error_message = "Email already exists."

        else:
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password
            )

            login(request, user)

            return redirect("create_freelancer_profile")

    return render(request, "register.html", {
        "error_message": error_message
    })


api_key = os.getenv("OPENAI_API_KEY")



client = OpenAI(api_key=api_key)

import requests


def login_user(request):
    error_message = None

    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")

        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)
            return redirect("dashboard")
        else:
            error_message = "Invalid username or password."

    return render(request, "login.html", {
        "error_message": error_message
    })


def logout_user(request):
    if request.method == "POST":
        logout(request)
        return redirect("public_home")

    return redirect("public_home")


def public_home(request):
    return render(request, "public_home.html")


@login_required
def add_work_experience(request):
    if request.method == "POST":

        WorkExperience.objects.create(
            user=request.user,
            job_title=request.POST.get("job_title"),
            company_or_project=request.POST.get("company_or_project"),
            tasks=request.POST.get("tasks"),
            skills_used=request.POST.get("skills_used"),
            experience_depth=request.POST.get("experience_depth"),
        )

        if "add_another" in request.POST:
            return redirect("add_work_experience")

        return redirect("my_experiences")

    return render(request, "add_experience.html")


@login_required
def my_experiences(request):
    experiences = WorkExperience.objects.filter(
        user=request.user
    ).order_by("-created_at")

    return render(request, "my_experiences.html", {
        "experiences": experiences
    })


@login_required
def edit_work_experience(request, experience_id):
    experience = get_object_or_404(
        WorkExperience,
        id=experience_id,
        user=request.user
    )

    if request.method == "POST":
        experience.job_title = request.POST.get("job_title")
        experience.company_or_project = request.POST.get("company_or_project")
        experience.tasks = request.POST.get("tasks")
        experience.skills_used = request.POST.get("skills_used")
        experience.experience_depth = request.POST.get("experience_depth")

        experience.save()

        return redirect("my_experiences")

    return render(request, "edit_experience.html", {
        "experience": experience
    })


@login_required
def create_freelancer_profile(request):
    profile = FreelancerProfile.objects.filter(user=request.user).first()

    next_page = request.GET.get("next") or request.POST.get("next")

    if profile:
        page_title = "Your Freelancer Profile"
        page_description = "Review and update your freelancer profile details. ProposalIQ uses this information when generating tailored proposals."
        button_text = "Save Profile Changes"
    else:
        page_title = "Create Your Freelancer Profile"
        page_description = "Create your freelancer profile so ProposalIQ can generate proposals that match your skills, tone and experience."
        button_text = "Create Profile"

    if request.method == "POST":
        FreelancerProfile.objects.update_or_create(
            user=request.user,
            defaults={
                "professional_title": request.POST.get("professional_title"),
                "profile_summary": request.POST.get("profile_summary"),
                "preferred_tone": request.POST.get("preferred_tone") or "professional",
            }
        )

        if next_page == "my_experiences":
            return redirect("my_experiences")

        return redirect("add_work_experience")

    return render(request, "create_profile.html", {
        "profile": profile,
        "next_page": next_page,
        "page_title": page_title,
        "page_description": page_description,
        "button_text": button_text,
    })


@login_required
def generate_proposal(request):
    generated_proposal = None

    proposals = Proposal.objects.filter(
        user=request.user
    ).order_by("-created_at")

    profile = FreelancerProfile.objects.filter(
        user=request.user
    ).first()

    if not profile:
        return redirect("create_freelancer_profile")

    pending_outcomes = []

    used_proposals = Proposal.objects.filter(
        user=request.user,
        used_by_user=True
    )

    for proposal in used_proposals:
        outcome_exists = ProposalOutcome.objects.filter(
            proposal=proposal
        ).exists()

        if not outcome_exists:
            pending_outcomes.append(proposal)

    return render(request, "home.html", {
        "generated_proposal": generated_proposal,
        "proposals": proposals,
        "pending_outcomes": pending_outcomes,
    })


@login_required
def confirm_use_proposal(request, proposal_id):
    proposal = get_object_or_404(
        Proposal,
        id=proposal_id,
        user=request.user
    )

    if request.method == "POST":
        ProposalUseConfirmation.objects.update_or_create(
            proposal=proposal,
            defaults={
                "platform": request.POST.get("platform"),
                "client_name": request.POST.get("client_name"),
                "job_url": request.POST.get("job_url"),
                "submitted_proposal_text": request.POST.get("submitted_proposal_text"),
                "notes": request.POST.get("notes"),
            }
        )

        proposal.used_by_user = True
        proposal.status = "used"
        proposal.used_at = timezone.now()
        proposal.save()

        return redirect("dashboard")

    return render(request, "confirm_use_proposal.html", {
        "proposal": proposal
    })

def validate_job_text(raw_text):
    score = 0
    issues = []

    raw_text = raw_text or ""
    lowered_text = raw_text.lower()

    if len(raw_text.strip()) < 100:
        issues.append("Job post is too short.")
    else:
        score += 20

    job_keywords = [
        "developer", "assistant", "designer", "writer", "specialist",
        "looking for", "need", "hiring", "project", "freelancer"
    ]

    skill_keywords = [
        "python", "django", "javascript", "excel", "data entry",
        "design", "api", "postgresql", "admin", "copywriting"
    ]

    budget_keywords = [
        "$", "hourly", "fixed", "budget", "per hour", "hrs/week"
    ]

    if any(keyword in lowered_text for keyword in job_keywords):
        score += 30
    else:
        issues.append("No clear job/project wording detected.")

    if any(keyword in lowered_text for keyword in skill_keywords):
        score += 25
    else:
        issues.append("No clear skills detected.")

    if any(keyword in lowered_text for keyword in budget_keywords):
        score += 15
    else:
        issues.append("No budget/payment indicators detected.")

    if len(raw_text.split()) > 50:
        score += 10
    else:
        issues.append("Job post may not contain enough detail.")

    return score, issues


@login_required
def extract_job_features(request):
    if request.method == "POST":
        raw_job_text = request.POST.get("raw_job_text", "").strip()

        validation_score, validation_issues = validate_job_text(raw_job_text)

        if validation_score < 50 and "continue_anyway" not in request.POST:
            return render(request, "extract_job_features.html", {
                "raw_job_text": raw_job_text,
                "validation_score": validation_score,
                "validation_issues": validation_issues,
            })

        prompt = (
            "Extract structured job information from this freelance job post. "
            "Return ONLY valid JSON with these keys: "
            "platform, job_title, job_description, budget_type, hourly_min, hourly_max, "
            "experience_level, project_duration, hours_per_week, skills_required, "
            "client_location, proposal_count, interviewing_count, invites_sent. "
            "If unknown, use an empty string. "
            "Job post text: " + raw_job_text
        )

        response = client.chat.completions.create(
            model="gpt-5",
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )

        extracted_text = response.choices[0].message.content.strip()

        try:
            extracted_data = json.loads(extracted_text)
        except json.JSONDecodeError:
            extracted_data = {}

        job_post = JobPost.objects.create(
            user=request.user,
            raw_job_text=raw_job_text,
            platform=extracted_data.get("platform", ""),
            job_title=extracted_data.get("job_title", "Untitled Job") or "Untitled Job",
            job_description=extracted_data.get("job_description", raw_job_text),
            budget_type=extracted_data.get("budget_type", ""),
            hourly_min=extracted_data.get("hourly_min") or None,
            hourly_max=extracted_data.get("hourly_max") or None,
            experience_level=extracted_data.get("experience_level", ""),
            project_duration=extracted_data.get("project_duration", ""),
            hours_per_week=extracted_data.get("hours_per_week", ""),
            skills_required=extracted_data.get("skills_required", ""),
            client_location=extracted_data.get("client_location", ""),
            proposal_count=extracted_data.get("proposal_count", ""),
            interviewing_count=extracted_data.get("interviewing_count", ""),
            invites_sent=extracted_data.get("invites_sent", ""),
            confirmed_by_user=False,
        )

        return redirect("confirm_job_features", job_post_id=job_post.id)

    return render(request, "extract_job_features.html")
#
# @login_required
# def extract_job_features(request):
#     if request.method == "POST":
#         raw_job_text = request.POST.get("raw_job_text")
#
#         prompt = (
#             "Extract structured job information from this freelance job post. "
#             "Return ONLY valid JSON with these keys: "
#             "platform, job_title, job_description, budget_type, hourly_min, hourly_max, "
#             "experience_level, project_duration, hours_per_week, skills_required, "
#             "client_location, proposal_count, interviewing_count, invites_sent. "
#             "If unknown, use an empty string. "
#             "Job post text: " + raw_job_text
#         )
#
#         response = client.chat.completions.create(
#             model="gpt-5",
#             messages=[
#                 {
#                     "role": "user",
#                     "content": prompt
#                 }
#             ]
#         )
#
#         extracted_text = response.choices[0].message.content
#
#         try:
#             extracted_data = json.loads(extracted_text)
#         except json.JSONDecodeError:
#             extracted_data = {}
#
#         job_post = JobPost.objects.create(
#             user=request.user,
#             raw_job_text=raw_job_text,
#             platform=extracted_data.get("platform", ""),
#             job_title=extracted_data.get("job_title", "Untitled Job") or "Untitled Job",
#             job_description=extracted_data.get("job_description", raw_job_text),
#             budget_type=extracted_data.get("budget_type", ""),
#             hourly_min=extracted_data.get("hourly_min") or None,
#             hourly_max=extracted_data.get("hourly_max") or None,
#             experience_level=extracted_data.get("experience_level", ""),
#             project_duration=extracted_data.get("project_duration", ""),
#             hours_per_week=extracted_data.get("hours_per_week", ""),
#             skills_required=extracted_data.get("skills_required", ""),
#             client_location=extracted_data.get("client_location", ""),
#             proposal_count=extracted_data.get("proposal_count", ""),
#             interviewing_count=extracted_data.get("interviewing_count", ""),
#             invites_sent=extracted_data.get("invites_sent", ""),
#             confirmed_by_user=False,
#         )
#
#         return redirect("confirm_job_features", job_post_id=job_post.id)
#
#     return render(request, "extract_job_features.html")
#
# @login_required
# def extract_job_features(request):
#     if request.method != "POST":
#         return redirect("dashboard")
#
#     raw_job_text = request.POST.get("raw_job_text", "").strip()
#
#     validation_score, validation_issues = validate_job_text(raw_job_text)
#
#     continue_anyway = request.POST.get("continue_anyway")
#
#     if validation_score < 50 and not continue_anyway:
#         return render(request, "extract_job_features.html", {
#             "raw_job_text": raw_job_text,
#             "validation_score": validation_score,
#             "validation_issues": validation_issues,
#         })
#
#     prompt = (
#         "Extract structured job information from this freelance job post. "
#         "Return ONLY valid JSON with these keys: "
#         "platform, job_title, job_description, budget_type, hourly_min, hourly_max, "
#         "experience_level, project_duration, hours_per_week, skills_required, "
#         "client_location, proposal_count, interviewing_count, invites_sent. "
#         "If unknown, use an empty string. "
#         "Job post text: " + raw_job_text
#     )
#
#     response = client.chat.completions.create(
#         model="gpt-5",
#         messages=[
#             {
#                 "role": "user",
#                 "content": prompt
#             }
#         ]
#     )
#
#     extracted_text = response.choices[0].message.content.strip()
#
#     try:
#         extracted_data = json.loads(extracted_text)
#     except json.JSONDecodeError:
#         extracted_data = {}
#
#     job_post = JobPost.objects.create(
#         user=request.user,
#         raw_job_text=raw_job_text,
#         platform=extracted_data.get("platform", ""),
#         job_title=extracted_data.get("job_title", "Untitled Job") or "Untitled Job",
#         job_description=extracted_data.get("job_description", raw_job_text),
#         budget_type=extracted_data.get("budget_type", ""),
#         hourly_min=extracted_data.get("hourly_min") or None,
#         hourly_max=extracted_data.get("hourly_max") or None,
#         experience_level=extracted_data.get("experience_level", ""),
#         project_duration=extracted_data.get("project_duration", ""),
#         hours_per_week=extracted_data.get("hours_per_week", ""),
#         skills_required=extracted_data.get("skills_required", ""),
#         client_location=extracted_data.get("client_location", ""),
#         proposal_count=extracted_data.get("proposal_count", ""),
#         interviewing_count=extracted_data.get("interviewing_count", ""),
#         invites_sent=extracted_data.get("invites_sent", ""),
#         confirmed_by_user=False,
#     )
#
#     return redirect("confirm_job_features", job_post_id=job_post.id)
# @login_required


def confirm_job_features(request, job_post_id):
    job_post = get_object_or_404(
        JobPost,
        id=job_post_id,
        user=request.user
    )

    profile = FreelancerProfile.objects.filter(user=request.user).first()

    if not profile:
        return redirect("create_freelancer_profile")

    if request.method == "POST":
        job_post.platform = request.POST.get("platform")
        job_post.job_title = request.POST.get("job_title") or "Untitled Job"
        job_post.job_description = request.POST.get("job_description")
        job_post.budget_type = request.POST.get("budget_type")
        job_post.hourly_min = request.POST.get("hourly_min") or None
        job_post.hourly_max = request.POST.get("hourly_max") or None
        job_post.experience_level = request.POST.get("experience_level")
        job_post.project_duration = request.POST.get("project_duration")
        job_post.hours_per_week = request.POST.get("hours_per_week")
        job_post.skills_required = request.POST.get("skills_required")
        job_post.client_location = request.POST.get("client_location")
        job_post.proposal_count = request.POST.get("proposal_count")
        job_post.interviewing_count = request.POST.get("interviewing_count")
        job_post.invites_sent = request.POST.get("invites_sent")
        job_post.confirmed_by_user = True
        job_post.save()

        profile_context = (
                "Professional Title: " + str(profile.professional_title) + ". "
                                                                           "Profile Summary: " + str(
            profile.profile_summary) + ". "
                                       "Preferred Tone: " + str(profile.preferred_tone) + "."
        )

        job_context = (
                "Platform: " + str(job_post.platform) + ". "
                                                        "Job Title: " + str(job_post.job_title) + ". "
                                                                                                  "Job Description: " + str(
            job_post.job_description) + ". "
                                        "Budget Type: " + str(job_post.budget_type) + ". "
                                                                                      "Hourly Range: " + str(
            job_post.hourly_min) + " - " + str(job_post.hourly_max) + ". "
                                                                      "Experience Level: " + str(
            job_post.experience_level) + ". "
                                         "Skills Required: " + str(job_post.skills_required) + ". "
        )

        prompt = (
                "You are an expert freelance proposal writer. "
                "Use the freelancer profile and confirmed job details below to write a highly tailored proposal. "
                "Freelancer Profile: " + profile_context + " "
                                                           "Confirmed Job Details: " + job_context
        )

        response = client.chat.completions.create(
            model="gpt-5",
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )

        generated_proposal = response.choices[0].message.content

        Proposal.objects.create(
            user=request.user,
            job_post=job_post,
            final_text=generated_proposal,
            status="generated"
        )

        return redirect("dashboard")

    return render(request, "confirm_job_features.html", {
        "job_post": job_post
    })

#
# def validate_job_text(raw_text):
#     score = 0
#     issues = []
#
#     if not raw_text or len(raw_text.strip()) < 100:
#         issues.append("Job post is too short.")
#     else:
#         score += 20
#
#     job_keywords = [
#         "developer", "assistant", "designer", "writer", "specialist",
#         "looking for", "need", "hiring", "project", "freelancer"
#     ]
#
#     skill_keywords = [
#         "python", "django", "javascript", "excel", "data entry",
#         "design", "api", "postgresql", "admin", "copywriting"
#     ]
#
#     budget_keywords = [
#         "$", "hourly", "fixed", "budget", "per hour", "hrs/week"
#     ]
#
#     if any(word.lower() in raw_text.lower() for word in job_keywords):
#         score += 30
#     else:
#         issues.append("No clear job/project wording detected.")
#
#     if any(word.lower() in raw_text.lower() for word in skill_keywords):
#         score += 25
#     else:
#         issues.append("No clear skills detected.")
#
#     if any(word.lower() in raw_text.lower() for word in budget_keywords):
#         score += 15
#     else:
#         issues.append("No budget/payment indicators detected.")
#
#     if len(raw_text.split()) > 50:
#         score += 10
#     else:
#         issues.append("Job post may not contain enough detail.")
#
#     return score, issues

def validate_job_text(raw_text):
    score = 0
    issues = []

    if len(raw_text.strip()) < 100:
        issues.append("The job description appears very short.")
    else:
        score += 20

    job_keywords = [
        "developer",
        "designer",
        "assistant",
        "engineer",
        "project",
        "freelancer",
        "hiring",
        "looking for",
    ]

    if any(keyword in raw_text.lower() for keyword in job_keywords):
        score += 30
    else:
        issues.append("No common job-related keywords detected.")

    if "$" in raw_text or "hourly" in raw_text.lower() or "budget" in raw_text.lower():
        score += 20
    else:
        issues.append("No pricing or budget information detected.")

    if len(raw_text.split()) > 50:
        score += 30
    else:
        issues.append("The description contains very little detail.")

    return score, issues

@login_required
def update_outcome(request, proposal_id):
    proposal = get_object_or_404(
        Proposal,
        id=proposal_id,
        user=request.user
    )

    use_confirmation = ProposalUseConfirmation.objects.filter(
        proposal=proposal
    ).first()

    if request.method == "POST":
        outcome_status = request.POST.get("outcome_status")
        notes = request.POST.get("notes")

        ProposalOutcome.objects.update_or_create(
            proposal=proposal,
            defaults={
                "status": outcome_status,
                "notes": notes,
            }
        )

        proposal.status = outcome_status
        proposal.save()

        return redirect("dashboard")

    return render(request, "update_outcome.html", {
        "proposal": proposal,
        "use_confirmation": use_confirmation,
    })
