from django.urls import path

from apps.results.views import cards, comments, electives, grid, settings, workflow

app_name = "results"

urlpatterns = [
    path("settings/", settings.settings_page, name="settings"),
    path("electives/<int:offering_pk>/", electives.electives_page, name="electives"),
    path("sheets/", grid.my_sheets, name="my_sheets"),
    path("grid/<int:offering_pk>/", grid.grid, name="grid"),
    path("grid/<int:offering_pk>/csv/", grid.grid_csv, name="grid_csv"),
    path("grid/<int:offering_pk>/upload/", grid.grid_upload, name="grid_upload"),
    path("comments/<int:arm_pk>/", comments.class_comments, name="class_comments"),
    path("grid/<int:offering_pk>/submit/", workflow.sheet_submit, name="sheet_submit"),
    path("grid/<int:offering_pk>/recall/", workflow.sheet_recall, name="sheet_recall"),
    path("class/<int:arm_pk>/", workflow.class_status, name="class_status"),
    path("class/<int:arm_pk>/review/", workflow.class_review, name="class_review"),
    path("class/<int:arm_pk>/approve/", workflow.class_approve, name="class_approve"),
    path("class/<int:arm_pk>/publish/", workflow.class_publish, name="class_publish"),
    path("class/<int:arm_pk>/unlock/", workflow.class_unlock, name="class_unlock"),
    path("report-card/<str:student_pk>/<str:term_pk>/", cards.report_card, name="report_card"),
]
