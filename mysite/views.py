from django.shortcuts import render, redirect
from openai import OpenAI

import logging

logger = logging.getLogger(__name__)

import os
from dotenv import load_dotenv

from google import genai

import requests

load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
print(api_key)

client = OpenAI(api_key=api_key)

import requests


def get_all_repos(username, token=None):
    repos = []
    page = 1

    # GitHub's API endpoint for a user's repositories
    url = f"https://api.github.com/users/{username}/repos"

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    # Add your token to avoid rate limiting (60 requests/hr vs 5000/hr)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    while True:
        # Request a specific page with the maximum allowed items (100)
        params = {"per_page": 100, "page": page}
        response = requests.get(url, headers=headers, params=params)

        # Check for errors
        if response.status_code != 200:
            print(f"Error: {response.status_code}")
            break

        data = response.json()

        # If the list is empty, we've reached the end
        if not data:
            break

        repos.extend(data)
        print(f"Fetched page {page} ({len(data)} repos)...")
        page += 1

    return repos


def proposal_main_page_view(request):
    user_post_url = ""
    open_ai_response = ""
    gemini_ai_response = ""

    logger.info("This is a log message")

    user_repos = get_all_repos("ayoolutobi123456")

    for repo in user_repos:
        print(f"Name: {repo['name']} | Stars: {repo['stargazers_count']} | URL: {repo['html_url']}")

    if request.method == "POST":
        job_description_details = request.POST.get("jobDescription", "")
        user_experience = request.POST.get("experienceDescription", "")
        print("content is " + user_post_url)

        content = "Hi i'm applying for this job " + job_description_details + " my relevant experience is " + user_experience + ". can you create my an highly specified job proposal for a job like this one. "

        if job_description_details:
            response = client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "user", "content": content}
                ]
            )

            open_ai_response = response.choices[0].message.content
            request.session['open_ai_proposal'] = open_ai_response

            # The client gets the API key from the environment variable `GEMINI_API_KEY`.
            client2 = genai.Client()

            response = client2.models.generate_content(
                model="gemini-3-flash-preview", contents=content
            )
            # print(response.text)

            gemini_ai_response = response.text
            request.session['gemini_ai_response'] = gemini_ai_response

            # return redirect("proposal_result")
            # return redirect(request, "https://google.com/")

    return render(request, "proposalMainPage.html", {
        # "user_input": job_description_details,
        "open_ai_response": open_ai_response,
        "gemini_ai_response": gemini_ai_response
    })


def proposal_result(request):
    openairesult = request.session.get('open_ai_proposal', '')
    geminiresult = request.session.get('gemini_ai_response', '')
    return render(request, 'proposalMainPage.html',
                  {"open_ai_response": openairesult, "gemini_ai_response": geminiresult})

#
# def textbox_view(request):
#     user_input = ""
#
#     logger.info("This is a log message")
#
#     if request.method == "POST":
#         user_input = request.POST.get("textbox_name", "")
#
#     return render(request, "proposalMainPage.html", {"user_input": user_input})
