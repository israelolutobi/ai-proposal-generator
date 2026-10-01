from decimal import Decimal, InvalidOperation
import json
import re

from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.core.validators import MaxLengthValidator, MinValueValidator, URLValidator

from .models import (
    FreelancerProfile, JobPost, Proposal, ProposalOutcome,
    ProposalUseConfirmation, WorkExperience,
)
from .platform_config import PLATFORM_CONFIGS, normalize_platform_name
from . import ai_limits


# The existing outcome UI is a subset of Proposal's declared status choices.
OUTCOME_STATUS_CHOICES = tuple(
    (value, "Reply Received" if value == "reply" else dict(Proposal.STATUS_CHOICES)[value])
    for value in ("no_response", "reply", "interview", "hired", "rejected")
)


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


class ProfileSummaryGenerationForm(forms.Form):
    professional_title = forms.CharField(max_length=ai_limits.SUMMARY_TITLE_CHARACTERS)
    key_skills = forms.CharField(max_length=ai_limits.SUMMARY_SKILLS_CHARACTERS)


class JobPasteForm(forms.Form):
    raw_job_text = forms.CharField(
        label="Full Job Description", widget=forms.Textarea,
        max_length=ai_limits.JOB_PASTE_CHARACTERS,
    )


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
        for name, maximum in (
            ("job_description", ai_limits.JOB_DESCRIPTION_CHARACTERS),
            ("skills_required", ai_limits.JOB_SKILLS_CHARACTERS),
        ):
            self.fields[name].max_length = maximum
            self.fields[name].validators.append(MaxLengthValidator(maximum))
        for name in ("hourly_min", "hourly_max", "fixed_budget"):
            self.fields[name].validators.append(MinValueValidator(0))
            self.fields[name].widget.attrs["min"] = "0"

    def clean(self):
        cleaned = super().clean()
        minimum, maximum = cleaned.get("hourly_min"), cleaned.get("hourly_max")
        if minimum is not None and maximum is not None and minimum > maximum:
            self.add_error("hourly_max", "Hourly maximum must be at least the hourly minimum.")
        return cleaned


class ExperienceSelectionField(forms.ModelMultipleChoiceField):
    def clean(self, value):
        if value:
            if len(value) > ai_limits.EXPERIENCE_COUNT:
                raise forms.ValidationError(
                    f"Choose no more than {ai_limits.EXPERIENCE_COUNT} experiences.",
                    code="ai_experience_count",
                )
            try:
                ids = [int(pk) for pk in value]
            except (TypeError, ValueError):
                raise forms.ValidationError("Choose valid owned experiences.", code="invalid_choice") from None
            if len(ids) != len(set(ids)):
                raise forms.ValidationError("Choose each experience only once.", code="duplicate_experience")
        selected = super().clean(value)
        ai_limits.experience_context(selected)
        return selected

    def label_from_instance(self, obj):
        return f"{obj.job_title} ({obj.company_or_project or 'No company/project'})"


class ProposalGenerationForm(JobConfirmationForm):
    selected_experiences = ExperienceSelectionField(
        queryset=WorkExperience.objects.none(), required=False,
        widget=forms.CheckboxSelectMultiple, label="Work experiences included in this application",
    )

    def __init__(self, data=None, *args, user, profile, **kwargs):
        self.profile = profile
        owned = WorkExperience.objects.filter(user=user).order_by("-created_at", "-pk")
        candidates = list(owned[:ai_limits.EXPERIENCE_COUNT + 1])
        try:
            ai_limits.experience_context(candidates)
        except forms.ValidationError:
            self.selection_required = True
        else:
            self.selection_required = False
        explicit_selection = data is not None and (
            "experience_selection_submitted" in data or "selected_experiences" in data
        )
        self.missing_selection = data is not None and not explicit_selection and self.selection_required
        initial = kwargs.setdefault("initial", {})
        if not self.selection_required:
            ids = [str(record.pk) for record in candidates]
            initial.setdefault("selected_experiences", ids)
            if data is not None and not explicit_selection:
                data = data.copy()
                if hasattr(data, "setlist"):
                    data.setlist("selected_experiences", ids)
                else:
                    data["selected_experiences"] = ids
        super().__init__(data, *args, **kwargs)
        self.fields["selected_experiences"].queryset = owned
        self.fields["selected_experiences"].help_text = (
            f"Choose up to {ai_limits.EXPERIENCE_COUNT} experiences, with at most "
            f"{ai_limits.EXPERIENCE_CONTEXT_CHARACTERS:,} formatted characters in total. "
            "Only checked records are sent. Stored records are kept. "
            + ("Your full history does not fit; choose a subset before generating." if self.selection_required
               else "Your full history fits and is selected by default; you can change the selection.")
        )

    def clean(self):
        cleaned = super().clean()
        if self.missing_selection:
            self.add_error("selected_experiences", "Your full history does not fit. Explicitly choose which experiences to include.")
        try:
            ai_limits.check_fields(self.profile, ai_limits.PROFILE_FIELD_LIMITS)
        except forms.ValidationError as error:
            self.add_error(None, error)
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

        if not isinstance(content, str) or len(content) > ai_limits.EXTRACTION_RESPONSE_CHARACTERS or not content.strip():
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


class SubmissionConfirmationForm(forms.ModelForm):
    job_url = forms.URLField(
        label="Opportunity URL", required=False,
        max_length=ProposalUseConfirmation._meta.get_field("job_url").max_length,
        validators=[URLValidator(schemes=["http", "https"])],
    )

    class Meta:
        model = ProposalUseConfirmation
        fields = ("platform", "client_name", "job_url", "submitted_proposal_text", "notes")
        labels = {
            "platform": "Freelance Platform", "client_name": "Client Name / Author",
            "submitted_proposal_text": "Final Submitted Content",
        }

    def __init__(self, data=None, *args, **kwargs):
        instance = kwargs.get("instance")
        if data is not None and instance and instance.pk:
            data = data.copy()
            for name in ("client_name", "job_url", "submitted_proposal_text", "notes"):
                if name not in data:
                    data[name] = getattr(instance, name) or ""
        super().__init__(data, *args, **kwargs)

    def clean_platform(self):
        platform = self.cleaned_data["platform"]
        # Known aliases and normal HTTP(S) platform URLs keep their current
        # terminology. Unrecognised names retain the configured generic fallback.
        if platform.lower().startswith(("http://", "https://")):
            URLValidator(schemes=["http", "https"])(platform)
        elif not re.fullmatch(r"[\w .&'()+-]+", platform) or not any(c.isalnum() for c in platform):
            raise forms.ValidationError("Enter a platform name or an HTTP/HTTPS platform URL.")
        key = normalize_platform_name(platform)
        if key not in PLATFORM_CONFIGS and not any(c.isalnum() for c in key):
            raise forms.ValidationError("Enter a platform name.")
        return platform


class ProposalOutcomeForm(forms.ModelForm):
    # Retain the existing POST field name while mapping it explicitly to status.
    outcome_status = forms.ChoiceField(
        label="What happened after submission?",
        choices=(("", "Select outcome"),) + OUTCOME_STATUS_CHOICES,
    )

    class Meta:
        model = ProposalOutcome
        fields = ("notes",)
        labels = {"notes": "Outcome Notes"}

    def __init__(self, data=None, *args, **kwargs):
        instance = kwargs.get("instance")
        if data is not None and instance and instance.pk and "notes" not in data:
            data = data.copy()
            data["notes"] = instance.notes or ""
        super().__init__(data, *args, **kwargs)
        if self.instance.pk:
            self.initial["outcome_status"] = self.instance.status

    def clean(self):
        cleaned = super().clean()
        if "outcome_status" in cleaned:
            self.instance.status = cleaned["outcome_status"]
        return cleaned

    @property
    def unsupported_status(self):
        value = self["outcome_status"].value()
        return value if value and value not in dict(OUTCOME_STATUS_CHOICES) else ""
