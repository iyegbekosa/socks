from django.urls import path

from apps.attendance.views import register, reports, settings

app_name = "attendance"

urlpatterns = [
    path("settings/", settings.settings_page, name="settings"),
    path("holidays/", settings.holidays, name="holidays"),
    path("register/<str:arm_id>/", register.today, name="today"),
    path("register/<str:arm_id>/<str:day>/", register.register_page, name="register"),
    path("class/<str:arm_id>/", reports.class_page, name="class"),
    path("school/", reports.school_page, name="school"),
    path("me/", reports.my_attendance, name="me"),
]
