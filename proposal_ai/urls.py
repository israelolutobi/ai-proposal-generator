from django.urls import path
from . import views

urlpatterns = [
    path("", views.generate_proposal, name="create_freelancer_profile"),
    path("create-profile/", views.create_freelancer_profile, name="create_freelancer_profile"),
]