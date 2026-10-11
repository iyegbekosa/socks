from django.urls import path

from apps.accounts import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("reset/", views.reset_request_view, name="reset"),
    path("reset/verify/", views.reset_verify_view, name="reset_verify"),
    path(
        "invite/<uidb64>/<token>/",
        views.invite_accept_view,
        name="invite_accept",
    ),
    path("mfa/", views.mfa_verify_view, name="mfa"),
    path("mfa/setup/", views.mfa_setup_view, name="mfa_setup"),
]
