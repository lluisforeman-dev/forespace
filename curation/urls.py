from django.urls import path
from curation import views

app_name = 'curation'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('auto-news/', views.auto_news, name='auto_news'),
    path('company-research/', views.company_research, name='company_research'),
    path('question-research/', views.question_research, name='question_research'),
    path('stop/', views.stop_all_tasks, name='stop_all'),
    path('ingest/', views.ingest_trigger, name='ingest_trigger'),
    path('stubs/', views.stubs, name='stubs'),
    path('stubs/<uuid:entity_id>/promote/', views.promote_stub, name='promote_stub'),
    path('candidates/', views.candidates, name='candidates'),
    path('candidates/<int:assertion_id>/review/', views.review_assertion, name='review_assertion'),
    path('entity/<uuid:entity_id>/', views.entity_profile, name='entity_profile'),
    path('connections/', views.connections, name='connections'),
    path('research/', views.research, name='research'),
    path('taxonomy/', views.taxonomy, name='taxonomy'),
    path('taxonomy/proposals/', views.taxonomy_proposals, name='taxonomy_proposals'),
    path('taxonomy/evolve/', views.run_evolve_taxonomy, name='run_evolve_taxonomy'),
]
