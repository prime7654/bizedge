"""Celery application.

Thin on purpose. The scheduled work lives in management commands, and the beat
schedule below just invokes them -- so the same logic can be run by hand, by
cron, or by a Kubernetes CronJob if Celery is ever dropped.
"""
import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("bizedge")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.task(name="grievances.send_due_pip_reminders")
def send_due_pip_reminders() -> str:
    """Invoke the management command.

    Deliberately a one-line wrapper: keeping the logic in the command means it
    is testable without a broker, and swapping the scheduler is a config change.
    """
    from django.core.management import call_command

    call_command("send_due_pip_reminders")
    return "ok"


app.conf.beat_schedule = {
    "send-due-pip-reminders": {
        "task": "grievances.send_due_pip_reminders",
        # 07:00 daily. Early enough to land before the working day, late enough
        # that a missed run is noticed the same morning.
        "schedule": crontab(hour=7, minute=0),
    },
}
