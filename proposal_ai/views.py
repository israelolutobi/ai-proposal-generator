from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from .models import FreelancerProfile, JobPost, Proposal, ProposalOutcome  # later expand
from openai import OpenAI
import os

# load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
print(api_key)

client = OpenAI(api_key=api_key)

import requests


@login_required
def create_freelancer_profile(request):
    profile = FreelancerProfile.objects.filter(user=request.user).first()

    if request.method == "POST":
        FreelancerProfile.objects.update_or_create(
            user=request.user,
            defaults={
                "professional_title": request.POST.get("professional_title"),
                "main_skills": request.POST.get("main_skills"),
                "years_experience": request.POST.get("years_experience") or None,
                "preferred_tone": request.POST.get("preferred_tone") or "professional",
            }
        )

        return redirect("create_freelancer_profile")

    return render(request, "create_profile.html", {
        "profile": profile
    })


@login_required
def generate_proposal(request):
    generated_proposal = None
    profile = FreelancerProfile.objects.filter(user=request.user).first()

    job_title = ""
    job_description = ""

    if not profile:
        return redirect("create_freelancer_profile")

    if request.method == "POST":
        if "proposal_id" in request.POST:
            proposal_id = request.POST.get("proposal_id")
            status = request.POST.get("status")

            proposal = Proposal.objects.get(id=proposal_id, user=request.user)

            ProposalOutcome.objects.update_or_create(
                proposal=proposal,
                defaults={"status": status}
            )

            return redirect("home")

        job_title = request.POST.get("job_title") or "Untitled Job"
        job_description = request.POST.get("job_description") or ""

        # Get saved profile
        profile = FreelancerProfile.objects.filter(user=request.user).first()

        # Build profile context safely

        profile_context = (
                "Professional Title: " + str(profile.professional_title) + ". "
                                                                           "Main Skills: " + str(
            profile.main_skills) + ". "
                                   "Years Experience: " + str(profile.years_experience) + ". "
                                                                                          "Preferred Tone: " + str(
            profile.preferred_tone) + "."
        )

        past_outcomes = ProposalOutcome.objects.filter(
            proposal__user=request.user
        ).select_related("proposal", "proposal__job_post")

        outcome_context = ""

        for outcome in past_outcomes:
            outcome_context += (
                    "Job: " + str(outcome.proposal.job_post.job_title) + ". "
                                                                         "Result: " + str(outcome.status) + ". "
            )

        # Build prompt
        prompt = (
                "You are creating a tailored freelance job proposal. "
                "Job Title: " + job_title + ". "
                                            "Job Description: " + job_description + ". "
                                                                                    "Freelancer Profile: " + profile_context + ". "
                                                                                                                               "Past performance: " + outcome_context + " "
                                                                                                                                                                        "Use successful patterns from past outcomes and avoid patterns from rejected ones. "
                                                                                                                                                                        "Write a highly specific proposal."
        )

        # 🔥 YOUR GPT CALL HERE
        # Replace this with your actual working code

        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        generated_proposal = response.choices[0].message.content

        # generated_proposal = "GPT GENERATED PROPOSAL GOES HERE"

        # Save JobPost
        job_post = JobPost.objects.create(
            user=request.user,
            job_title=job_title,
            job_description=job_description,
            platform="Upwork",
        )

        # Save Proposal
        Proposal.objects.create(
            user=request.user,
            job_post=job_post,
            final_text=generated_proposal,
        )

    saved_proposals = Proposal.objects.filter(
        user=request.user
    ).order_by("-created_at")

    return render(request, "home.html", {
        "generated_proposal": generated_proposal,
        "saved_proposals": saved_proposals
    })
