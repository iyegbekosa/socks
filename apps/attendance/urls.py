from django.urls import path

from apps.attendance.views import settings

app_name = "attendance"

urlpatterns = [
    path("settings/", settings.settings_page, name="settings"),
    path("holidays/", settings.holidays, name="holidays"),
]
