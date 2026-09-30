from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

from .models import FreelancerProfile, WorkExperience


class RegistrationForm(UserCreationForm):
    email = forms.EmailField(max_length=254)

    class Meta(UserCreationForm.Meta):
        fields = ("username", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password2"].label = "Confirm Password"

    def clean_email(self):
        email = self.cleaned_data["email"]
        # This form check preserves the duplicate-email policy. Database-level
        # case-insensitive uniqueness (including concurrent signups) is a
        # separate future decision; Django's User.email is not unique.
        if self._meta.model.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Email already exists.", code="duplicate_email")
        return email


class LoginForm(AuthenticationForm):
    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": "Invalid username or password.",
        "inactive": "Invalid username or password.",
    }


class FreelancerProfileForm(forms.ModelForm):
    # The model has no choices; enforce the four existing UI options here.
    preferred_tone = forms.ChoiceField(
        label="Preferred Proposal Tone",
        initial=FreelancerProfile._meta.get_field("preferred_tone").get_default(),
        choices=(
            ("professional", "Professional"),
            ("friendly", "Friendly"),
            ("confident", "Confident"),
            ("technical", "Technical"),
        ),
    )

    class Meta:
        model = FreelancerProfile
        fields = ("professional_title", "profile_summary", "preferred_tone")
        labels = {
            "professional_title": "Professional Title",
            "profile_summary": "Profile Summary",
        }


class WorkExperienceForm(forms.ModelForm):
    class Meta:
        model = WorkExperience
        fields = (
            "job_title", "company_or_project", "tasks", "skills_used", "experience_depth"
        )
        labels = {
            "job_title": "Role / Project Title",
            "company_or_project": "Company or Project Name",
            "tasks": "Relevant Tasks / Responsibilities",
            "skills_used": "Skills Used",
            "experience_depth": "Experience Depth",
        }
