from django.urls import path

from apps.results.views import settings

app_name = "results"

urlpatterns = [
    path("settings/", settings.settings_page, name="settings"),
]
