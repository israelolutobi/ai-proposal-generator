from django.shortcuts import render, redirect
from openai import OpenAI

import logging

logger = logging.getLogger(__name__)

import os
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
print(api_key)

client = OpenAI(api_key=api_key)

def proposal_main_page_view(request):
    user_post_url = ""
    ai_response = ""

    logger.info("This is a log message")

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

            ai_response = response.choices[0].message.content

            request.session['proposal'] = ai_response

            # return redirect("proposal_result")
            # return redirect(request, "https://google.com/")

    return render(request, "proposalMainPage.html", {
        # "user_input": job_description_details,
        "ai_response": ai_response
    })


def proposal_result(request):
    result = request.session.get('proposal', '')
    return render(request, 'proposalMainPage.html', {"ai_response": result})

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
