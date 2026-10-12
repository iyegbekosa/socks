from django.urls import path

from apps.students import views

app_name = "students"

urlpatterns = [
    path("", views.student_list, name="list"),
    path("admit/", views.admit, name="admit"),
    path("passwords/", views.passwords, name="passwords"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/admitted/", views.admitted, name="admitted"),
    path("<int:pk>/edit/", views.edit, name="edit"),
    path("<int:pk>/withdraw/", views.withdraw, name="withdraw"),
    path("<int:pk>/photo/", views.photo, name="photo"),
]
