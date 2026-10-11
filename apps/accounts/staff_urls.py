from django.urls import path

from apps.accounts.views import staff

app_name = "staff"

urlpatterns = [
    path("", staff.staff_list_view, name="list"),
    path("invite/", staff.staff_invite_view, name="invite"),
    path("edit/<int:pk>/", staff.staff_edit_view, name="edit"),
]
