import redis
from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Flush all Celery data from Redis (broker queues + results). Run once to recover from OOM.'

    def handle(self, *args, **options):
        r = redis.from_url(settings.CELERY_BROKER_URL)
        info = r.info('memory')
        used = info.get('used_memory_human', '?')
        self.stdout.write(f'Redis memory before flush: {used}')
        r.flushdb()
        self.stdout.write(self.style.SUCCESS('Redis flushed. Worker should now start cleanly.'))
