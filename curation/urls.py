from django.urls import path
from curation import views

app_name = 'curation'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('stubs/', views.stubs, name='stubs'),
    path('stubs/<uuid:entity_id>/promote/', views.promote_stub, name='promote_stub'),
    path('conflicts/', views.conflicts, name='conflicts'),
    path('conflicts/<int:conflict_id>/resolve/', views.resolve_conflict, name='resolve_conflict'),
    path('candidates/', views.candidates, name='candidates'),
    path('candidates/<int:assertion_id>/review/', views.review_assertion, name='review_assertion'),
]
