from django.urls import path

from apps.results.views import comments, electives, grid, settings

app_name = "results"

urlpatterns = [
    path("settings/", settings.settings_page, name="settings"),
    path("electives/<int:offering_pk>/", electives.electives_page, name="electives"),
    path("sheets/", grid.my_sheets, name="my_sheets"),
    path("grid/<int:offering_pk>/", grid.grid, name="grid"),
    path("grid/<int:offering_pk>/csv/", grid.grid_csv, name="grid_csv"),
    path("grid/<int:offering_pk>/upload/", grid.grid_upload, name="grid_upload"),
    path("comments/<int:arm_pk>/", comments.class_comments, name="class_comments"),
]
