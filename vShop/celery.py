# vShop/celery.py
import os
from celery import Celery

# Bind the Django settings module before any app is instantiated
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vShop.settings")

app = Celery("vShop")

# Pull all CELERY_* keys from Django settings — no separate celeryconfig.py needed
app.config_from_object("django.conf:settings", namespace="CELERY")

# Walk every INSTALLED_APPS entry and import its tasks.py automatically
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f"Request: {self.request!r}")  # safe smoke-test hook