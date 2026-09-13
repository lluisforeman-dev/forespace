web: python manage.py migrate --settings=config.settings.prod && gunicorn config.wsgi:application --workers 3 --timeout 60
worker: celery -A config worker -Q crawl,parse,triage,extract,resolve,adjudicate,project,analytics,analysis --loglevel=info
