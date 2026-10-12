from django.urls import path

from apps.results.views import electives, settings

app_name = "results"

urlpatterns = [
    path("settings/", settings.settings_page, name="settings"),
    path("electives/<int:offering_pk>/", electives.electives_page, name="electives"),
]
