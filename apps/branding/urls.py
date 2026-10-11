from django.urls import path

from apps.branding.views import branding_settings

app_name = "branding"

urlpatterns = [path("", branding_settings, name="settings")]
