from django.urls import path

from apps.comms.views import inbox

app_name = "comms"

urlpatterns = [
    path("", inbox.inbox, name="inbox"),
    path("read-all/", inbox.inbox_read_all, name="inbox_read_all"),
    path("<str:pk>/", inbox.inbox_item, name="inbox_item"),
]
