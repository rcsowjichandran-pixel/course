from django.urls import path

from . import views


app_name = 'generator'

urlpatterns = [
    path('', views.student_dashboard, name='student_dashboard'),
    path('generate/', views.generate_content, name='generate_content'),
    path(
        'content/<int:content_id>/',
        views.content_detail,
        name='content_detail',
    ),
    path(
        'topics/<int:topic_id>/',
        views.view_content,
        name='view_content',
    ),
    path(
        'topics/<int:topic_id>/quiz/submit/',
        views.submit_quiz,
        name='submit_quiz',
    ),
    path(
        'topics/<int:topic_id>/pdf/',
        views.export_pdf,
        name='export_pdf',
    ),
]
