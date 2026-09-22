import redis
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse
from django.urls import path, include
from django.views.generic import RedirectView


@staff_member_required
def flush_redis_view(request):
    r = redis.from_url(settings.CELERY_BROKER_URL)
    info = r.info('memory')
    used_before = info.get('used_memory_human', '?')
    r.flushdb()
    return HttpResponse(f'Redis flushed (was {used_before}). Worker should now start cleanly.')


urlpatterns = [
    path('', RedirectView.as_view(url='/curation/', permanent=False)),
    path('login/', auth_views.LoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('admin/flush-redis/', flush_redis_view),
    path('admin/', admin.site.urls),
    path('curation/', include('curation.urls', namespace='curation')),
]
