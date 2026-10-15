from django.urls import path

from apps.comms.views import compose, history, inbox, reminders

app_name = "comms"

urlpatterns = [
    path("inbox/", inbox.inbox, name="inbox"),
    path("inbox/read-all/", inbox.inbox_read_all, name="inbox_read_all"),
    path("inbox/<str:pk>/", inbox.inbox_item, name="inbox_item"),
    path("messages/", history.history, name="history"),
    path("messages/new/", compose.compose, name="compose"),
    path("messages/reminders/", reminders.reminders_page, name="reminders"),
    path("messages/reminders/send/", reminders.reminders_send, name="reminders_send"),
    path(
        "messages/reminders/dates/<str:pk>/delete/",
        reminders.reminder_date_delete,
        name="reminder_date_delete",
    ),
    path("messages/<str:pk>/", history.message_detail, name="message"),
    path("messages/<str:pk>/retry/", history.retry_failed, name="retry_failed"),
]
