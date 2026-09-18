from pathlib import Path
import os
import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()

env_file = BASE_DIR / '.env'
if env_file.exists():
    environ.Env.read_env(env_file)

# --- Core ---
SECRET_KEY = env('SECRET_KEY')
DEBUG = env.bool('DEBUG', default=False)
ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['localhost', '127.0.0.1'])

# Render sets RENDER_EXTERNAL_HOSTNAME automatically — include it so prod doesn't 400
_render_host = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
if _render_host:
    ALLOWED_HOSTS.append(_render_host)

CSRF_TRUSTED_ORIGINS = [f'https://{h}' for h in ALLOWED_HOSTS if not h.startswith(('localhost', '127'))]

DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.postgres',
]

LOCAL_APPS = [
    'core',
    'ingest',
    'curation',
    'api',
]

INSTALLED_APPS = DJANGO_APPS + LOCAL_APPS

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# --- Database (Postgres required — MySQL cannot run this schema) ---
DATABASES = {
    'default': {
        **env.db('DATABASE_URL', default='postgresql://localhost/forespace'),
        'CONN_MAX_AGE': 300,        # pool_recycle equivalent
        'CONN_HEALTH_CHECKS': True, # pool_pre_ping equivalent
    }
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
USE_TZ = True

# --- Static files ---
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [d for d in [BASE_DIR / 'static'] if d.exists()]

# --- Celery ---
CELERY_BROKER_URL = env('REDIS_URL', default='redis://localhost:6379/0')
CELERY_RESULT_BACKEND = env('REDIS_URL', default='redis://localhost:6379/0')
CELERY_TASK_SERIALIZER = 'json'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'UTC'
CELERY_TASK_TRACK_STARTED = True

# --- AI — OpenRouter (same keys as astronode) ---
AI_API_KEY = env('AI_API_KEY', default='')
AI_API_URL = env('AI_API_URL', default='https://openrouter.ai/api/v1')
AI_MODEL = env('AI_MODEL', default='openai/gpt-5.6-luna')
AI_MODEL_PROSE = env('AI_MODEL_PROSE', default='openai/gpt-5.6-luna')
AI_MODEL_SONAR = env('AI_MODEL_SONAR', default='openai/gpt-5.6-luna:online')
AI_MODEL_FAST = env('AI_MODEL_FAST', default='openai/gpt-5.6-luna')  # cheap model for mechanical tasks

# --- Mail — Hostinger SMTP (same as astronode) ---
EMAIL_HOST = env('MAIL_SERVER', default='smtp.hostinger.com')
EMAIL_PORT = env.int('MAIL_PORT', default=465)
EMAIL_USE_SSL = True
EMAIL_HOST_USER = env('MAIL_USERNAME', default='')
EMAIL_HOST_PASSWORD = env('MAIL_PASSWORD', default='')
DEFAULT_FROM_EMAIL = env('MAIL_DEFAULT_SENDER', default='')

# --- Stripe (future billing) ---
STRIPE_SECRET_KEY = env('STRIPE_SECRET_KEY', default='')
STRIPE_PUBLISHABLE_KEY = env('STRIPE_PUBLISHABLE_KEY', default='')
STRIPE_WEBHOOK_SECRET = env('STRIPE_WEBHOOK_SECRET', default='')

BASE_URL = env('BASE_URL', default='http://localhost:8000')

LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/curation/'
LOGOUT_REDIRECT_URL = '/login/'

# --- Security headers (override in prod) ---
X_FRAME_OPTIONS = 'SAMEORIGIN'
