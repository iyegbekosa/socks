from django.urls import path

from apps.attendance.views import register, settings

app_name = "attendance"

urlpatterns = [
    path("settings/", settings.settings_page, name="settings"),
    path("holidays/", settings.holidays, name="holidays"),
    path("register/<str:arm_id>/", register.today, name="today"),
    path("register/<str:arm_id>/<str:day>/", register.register_page, name="register"),
]
