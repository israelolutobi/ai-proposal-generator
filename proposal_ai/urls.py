from django.urls import path
from . import views

urlpatterns = [
    path("", views.public_home, name="public_home"),
    path("register/", views.register_user, name="register"),
    path("login/", views.login_user, name="login"),
    path("logout/", views.logout_user, name="logout"),
    path("dashboard/", views.generate_proposal, name="dashboard"),
    # path("", views.generate_proposal, name="create_freelancer_profile"),
    path("create-profile/", views.create_freelancer_profile, name="create_freelancer_profile"),
    path("add-experience/", views.add_work_experience, name="add_work_experience"),
    path("my-experiences/", views.my_experiences, name="my_experiences"),
    path("edit-experience/<int:experience_id>/", views.edit_work_experience, name="edit_work_experience"),
    path("use-proposal/<int:proposal_id>/", views.confirm_use_proposal, name="confirm_use_proposal"),
    path("extract-job/", views.extract_job_features, name="extract_job_features"),
    path("confirm-job/<int:job_post_id>/", views.confirm_job_features, name="confirm_job_features"),
]
