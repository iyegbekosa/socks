from django.urls import path

from apps.audit.views import audit_log

app_name = "audit"

urlpatterns = [path("", audit_log, name="log")]
