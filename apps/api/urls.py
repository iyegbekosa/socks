from django.urls import path

from apps.api import views

app_name = "api"

urlpatterns = [
    path("me", views.MeView.as_view(), name="me"),
    path("students", views.StudentListView.as_view(), name="students"),
    path("students/import", views.StudentImportView.as_view(), name="students_import"),
    path("imports/<int:pk>", views.ImportDetailView.as_view(), name="import_detail"),
    path("scores", views.ScoresView.as_view(), name="scores"),
    path(
        "results/<int:class_arm>/approve",
        views.ResultsApproveView.as_view(),
        name="results_approve",
    ),
    path("payments/manual", views.ManualPaymentView.as_view(), name="payments_manual"),
    path(
        "results/<str:student>/<str:term>/report-card",
        views.ReportCardView.as_view(),
        name="report_card",
    ),
]
