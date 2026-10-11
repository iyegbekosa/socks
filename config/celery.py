import logging.config
import os

from celery import Celery
from celery.signals import setup_logging
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


@setup_logging.connect(weak=False)
def configure_logging(**kwargs) -> None:
    """Keep Django's JSON logging in workers: a connected receiver stops Celery's own setup."""
    from django.conf import settings

    logging.config.dictConfig(settings.LOGGING)
