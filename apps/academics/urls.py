from django.urls import path

from apps.academics import views

app_name = "academics"

urlpatterns = [
    path("", views.sessions, name="sessions"),
    path("new/", views.session_create, name="session_create"),
    path("terms/<int:term_id>/current/", views.set_current, name="set_current"),
    path("structure/", views.structure, name="structure"),
    path("structure/arms/<int:arm_id>/detail/", views.arm_detail, name="arm_detail"),
    path("structure/arms/<int:arm_id>/", views.arm_rename, name="arm_rename"),
    path("structure/arms/<int:arm_id>/delete/", views.arm_delete, name="arm_delete"),
    path("subjects/", views.subjects, name="subjects"),
    path("subjects/<int:subject_id>/", views.subject_edit, name="subject_edit"),
    path("subjects/<int:subject_id>/delete/", views.subject_delete, name="subject_delete"),
]
