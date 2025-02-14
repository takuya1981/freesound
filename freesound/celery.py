from __future__ import absolute_import
import os
from celery import Celery
from django.conf import settings

# set the default Django settings module for the 'celery' program.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freesound.settings')

app = Celery('freesound')

# Using a string here means the worker don't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Load tasks from only the clustering app.
app.autodiscover_tasks('clustering', related_name='tasks')


@app.task(bind=True)
def debug_task(self):
    print('Request: {0!r}'.format(self.request))
