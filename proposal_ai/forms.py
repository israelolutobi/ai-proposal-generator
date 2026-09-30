from decimal import Decimal, InvalidOperation
import json

from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.core.validators import MinValueValidator

from .models import FreelancerProfile, JobPost, WorkExperience


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


class JobPasteForm(forms.Form):
    # TextField/UI define no length limit. Token limits belong to AI quotas.
    raw_job_text = forms.CharField(label="Full Job Description", widget=forms.Textarea)


class JobConfirmationForm(forms.ModelForm):
    class Meta:
        model = JobPost
        # Ownership, raw text and confirmation state are controlled by the view.
        # Platform/budget/count fields are free text in the existing schema;
        # unknown platforms intentionally retain the generic platform fallback.
        fields = (
            "platform", "job_title", "job_description", "budget_type",
            "hourly_min", "hourly_max", "fixed_budget", "experience_level",
            "project_duration", "hours_per_week", "skills_required",
            "client_location", "proposal_count", "interviewing_count", "invites_sent",
        )
        labels = {
            "job_title": "Job / Opportunity Title",
            "job_description": "Job / Opportunity Description",
            "fixed_budget": "Fixed Price Budget",
            "hourly_min": "Hourly Minimum",
            "hourly_max": "Hourly Maximum",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("hourly_min", "hourly_max", "fixed_budget"):
            self.fields[name].validators.append(MinValueValidator(0))
            self.fields[name].widget.attrs["min"] = "0"

    def clean(self):
        cleaned = super().clean()
        minimum, maximum = cleaned.get("hourly_min"), cleaned.get("hourly_max")
        if minimum is not None and maximum is not None and minimum > maximum:
            self.add_error("hourly_max", "Hourly maximum must be at least the hourly minimum.")
        return cleaned


class JobExtractionForm(JobConfirmationForm):
    """Validate original JSON types as well as Django's model constraints."""

    @classmethod
    def from_json(cls, content):
        def reject_constant(value):
            raise ValueError("Nonstandard JSON numeric constant.")

        def unique_object(pairs):
            data = {}
            for key, value in pairs:
                if key in data:
                    raise ValueError("Duplicate JSON field.")
                data[key] = value
            return data

        if not isinstance(content, str) or not content.strip():
            raise forms.ValidationError("Invalid job extraction.")
        try:
            data = json.loads(content, parse_float=Decimal, parse_constant=reject_constant,
                              object_pairs_hook=unique_object)
        except (ValueError, InvalidOperation, RecursionError):
            raise forms.ValidationError("Invalid job extraction.") from None
        if not isinstance(data, dict):
            raise forms.ValidationError("Invalid job extraction.")
        form = cls(data=data)
        if not form.is_valid():
            raise forms.ValidationError("Invalid job extraction.")
        return form

    def clean(self):
        cleaned = super().clean()
        for name, field in self.fields.items():
            value = self.data.get(name)
            if value is None:
                continue  # Required fields still fail Django's required checks.
            if isinstance(field, forms.DecimalField):
                # JSON numbers are parsed as int/Decimal; strings allow "25.50".
                valid_type = not isinstance(value, bool) and isinstance(value, (str, int, Decimal))
            else:
                valid_type = isinstance(value, str)
            if not valid_type:
                self.add_error(name, "The extracted field has an invalid type.")
        return cleaned
