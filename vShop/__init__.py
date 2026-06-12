# vShop/__init__.py
#
# Force-load the Celery app at package import time so that
# @shared_task decorators in any app's tasks.py bind correctly.
#
# Guard: only perform the import when we are NOT already inside the
# vShop.celery module (i.e. not during its own initialisation).
# This prevents the circular-import error:
#   vShop/__init__.py -> vShop/celery.py -> vShop/__init__.py (again)
import sys
if 'vShop.celery' not in sys.modules:
    from .celery import app as celery_app
    __all__ = ('celery_app',)
