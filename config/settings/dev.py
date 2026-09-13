from .base import *  # noqa

DEBUG = True
ALLOWED_HOSTS = ['*']

# Print emails to console in dev
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {'class': 'logging.StreamHandler'},
    },
    'root': {
        'handlers': ['console'],
        'level': 'DEBUG',
    },
    'loggers': {
        'django.db.backends': {
            'level': 'WARNING',  # set to DEBUG to see all SQL
        },
    },
}
