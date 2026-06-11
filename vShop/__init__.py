# vShop/__init__.py

# Force-load the Celery app at package import time so that
# @shared_task decorators in any app's tasks.py bind correctly.
from .celery import app as celery_app

__all__ = ("celery_app",)