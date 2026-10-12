from django.urls import path

from apps.imports import views

app_name = "imports"

urlpatterns = [
    path("", views.new, name="new"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/errors.csv", views.errors_csv, name="errors_csv"),
]
