from django.urls import path

from apps.academics import views

app_name = "academics"

urlpatterns = [
    path("", views.sessions, name="sessions"),
    path("new/", views.session_create, name="session_create"),
    path("terms/<int:term_id>/current/", views.set_current, name="set_current"),
]
