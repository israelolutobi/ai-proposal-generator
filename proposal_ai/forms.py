from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm


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
