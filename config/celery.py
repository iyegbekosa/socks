import os

from celery import Celery
from kombu import Exchange, Queue

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Declared here so the queue set is explicit; highest priority first (§8.1).
app.conf.task_queues = (
    Queue("payments", Exchange("payments"), routing_key="payments"),
    Queue("notifications", Exchange("notifications"), routing_key="notifications"),
    Queue("default", Exchange("default"), routing_key="default"),
    Queue("reports", Exchange("reports"), routing_key="reports"),
)
