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
    path(
        "payments/initialize",
        views.PaymentsInitializeView.as_view(),
        name="payments_initialize",
    ),
    path("invoices", views.InvoiceListView.as_view(), name="invoices"),
    path("payments/manual", views.ManualPaymentView.as_view(), name="payments_manual"),
    path("messages", views.MessagesView.as_view(), name="messages"),
    path("notifications", views.NotificationListView.as_view(), name="notifications"),
    path(
        "notifications/<str:pk>/read",
        views.NotificationReadView.as_view(),
        name="notification_read",
    ),
    path(
        "attendance/<str:arm>/<str:date>",
        views.AttendanceRegisterView.as_view(),
        name="attendance_register",
    ),
    path(
        "results/<str:student>/<str:term>/report-card",
        views.ReportCardView.as_view(),
        name="report_card",
    ),
]
