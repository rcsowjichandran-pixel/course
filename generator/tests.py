import json
import os
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.test import TestCase
from django.urls import reverse

from .models import (
    Answer,
    CourseTopic,
    GeneratedContent,
    Question,
    Quiz,
    Summary,
)


SAMPLE_GENERATED_CONTENT = {
    'notes': 'Water evaporates.',
    'keyPoints': ['Evaporation', 'Condensation'],
    'summary': 'Water continuously moves through the environment.',
    'quiz': {
        'title': 'Water cycle quiz',
        'description': 'Check your understanding of the water cycle.',
        'questions': [
            {
                'questionText': 'What process turns liquid water into vapor?',
                'options': [
                    {'text': 'Evaporation', 'isCorrect': True},
                    {'text': 'Condensation', 'isCorrect': False},
                    {'text': 'Precipitation', 'isCorrect': False},
                    {'text': 'Collection', 'isCorrect': False},
                ],
            }
        ],
    },
}


def gemini_response(content):
    return {
        'candidates': [
            {'content': {'parts': [{'text': json.dumps(content)}]}}
        ]
    }


class ContentGenerationTests(TestCase):
    def setUp(self):
        self.teacher = get_user_model().objects.create_user(
            username='teacher',
            password='test-password',
        )

    def create_course_with_quiz(self):
        topic = CourseTopic.objects.create(
            title='The water cycle',
            teacher=self.teacher,
        )
        quiz = Quiz.objects.create(
            course_topic=topic,
            title='Water cycle quiz',
            description='Check your understanding.',
        )
        GeneratedContent.objects.create(
            course_topic=topic,
            quiz=quiz,
            notes='Water evaporates and condenses.',
            key_points='Evaporation\nCondensation',
        )
        Summary.objects.create(
            course_topic=topic,
            summary_text='Water moves through a repeating cycle.',
        )
        question = Question.objects.create(
            quiz=quiz,
            question_text='What process turns liquid water into vapor?',
        )
        answers = Answer.objects.bulk_create(
            [
                Answer(
                    question=question,
                    answer_text='Evaporation',
                    is_correct=True,
                ),
                Answer(
                    question=question,
                    answer_text='Condensation',
                    is_correct=False,
                ),
            ]
        )
        return topic, question, answers

    def test_generate_content_saves_and_displays_gemini_result(self):
        self.client.force_login(self.teacher)
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'test-key'}):
            with patch('generator.views.requests.post') as post:
                post.return_value.json.return_value = gemini_response(
                    SAMPLE_GENERATED_CONTENT
                )
                response = self.client.post(
                    reverse('generator:generate_content'),
                    {'title': 'The water cycle'},
                )

        topic = CourseTopic.objects.get()
        content = GeneratedContent.objects.get()
        quiz = Quiz.objects.get()
        question = Question.objects.get()
        summary = Summary.objects.get()
        self.assertEqual(topic.teacher, self.teacher)
        self.assertEqual(content.course_topic, topic)
        self.assertEqual(content.quiz, quiz)
        self.assertEqual(quiz.course_topic, topic)
        self.assertEqual(content.notes, 'Water evaporates.')
        self.assertEqual(content.key_points, 'Evaporation\nCondensation')
        self.assertEqual(question.quiz, quiz)
        self.assertEqual(question.answers.count(), 4)
        self.assertEqual(
            question.answers.filter(is_correct=True).get().answer_text,
            'Evaporation',
        )
        self.assertEqual(summary.course_topic, topic)
        self.assertEqual(
            summary.summary_text,
            'Water continuously moves through the environment.',
        )
        self.assertIn(
            '/models/gemini-3.5-flash-lite:generateContent',
            post.call_args.args[0],
        )
        self.assertRedirects(
            response,
            reverse(
                'generator:content_detail',
                kwargs={'content_id': content.pk},
            ),
        )

        response = self.client.get(
            reverse(
                'generator:content_detail',
                kwargs={'content_id': content.pk},
            )
        )
        self.assertContains(response, 'Water evaporates.')
        self.assertContains(response, 'Condensation')
        self.assertContains(response, 'Water continuously moves')
        self.assertContains(response, 'Water cycle quiz')
        self.assertContains(response, 'What process turns liquid water into vapor?')
        self.assertContains(response, '(Correct)')
        post.assert_called_once()

    def test_missing_api_key_does_not_save_topic(self):
        self.client.force_login(self.teacher)
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.post(
                reverse('generator:generate_content'),
                {'title': 'The water cycle'},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(CourseTopic.objects.count(), 0)
        self.assertContains(response, 'GEMINI_API_KEY', status_code=503)

    def test_invalid_gemini_json_does_not_save_topic(self):
        self.client.force_login(self.teacher)
        invalid_response = {
            'candidates': [
                {'content': {'parts': [{'text': 'not JSON'}]}}
            ]
        }
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'test-key'}):
            with patch('generator.views.requests.post') as post:
                post.return_value.json.return_value = invalid_response
                response = self.client.post(
                    reverse('generator:generate_content'),
                    {'title': 'The water cycle'},
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(CourseTopic.objects.count(), 0)
        self.assertContains(response, 'could not be read', status_code=503)

    def test_invalid_quiz_data_does_not_save_topic(self):
        self.client.force_login(self.teacher)
        invalid_content = {
            **SAMPLE_GENERATED_CONTENT,
            'quiz': {
                **SAMPLE_GENERATED_CONTENT['quiz'],
                'questions': [
                    {
                        **SAMPLE_GENERATED_CONTENT['quiz']['questions'][0],
                        'options': [
                            {
                                'text': 'Evaporation',
                                'isCorrect': False,
                            },
                            {
                                'text': 'Condensation',
                                'isCorrect': False,
                            },
                        ],
                    }
                ],
            },
        }
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'test-key'}):
            with patch('generator.views.requests.post') as post:
                post.return_value.json.return_value = gemini_response(
                    invalid_content
                )
                response = self.client.post(
                    reverse('generator:generate_content'),
                    {'title': 'The water cycle'},
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(CourseTopic.objects.count(), 0)
        self.assertContains(response, 'invalid quiz', status_code=503)

    def test_gemini_http_error_is_reported_without_saving_topic(self):
        self.client.force_login(self.teacher)
        error_response = type('Response', (), {'status_code': 404})()
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'test-key'}):
            with patch(
                'generator.views.requests.post',
                side_effect=requests.HTTPError(response=error_response),
            ):
                response = self.client.post(
                    reverse('generator:generate_content'),
                    {'title': 'The water cycle'},
                )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(CourseTopic.objects.count(), 0)
        self.assertContains(response, 'HTTP 404', status_code=502)

    def test_database_error_rolls_back_and_displays_message(self):
        self.client.force_login(self.teacher)
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'test-key'}):
            with patch('generator.views.requests.post') as post:
                post.return_value.json.return_value = gemini_response(
                    SAMPLE_GENERATED_CONTENT
                )
                with patch(
                    'generator.views.Answer.objects.bulk_create',
                    side_effect=DatabaseError('database unavailable'),
                ):
                    with self.assertLogs(
                        'generator.views',
                        level='ERROR',
                    ) as captured_logs:
                        response = self.client.post(
                            reverse('generator:generate_content'),
                            {'title': 'The water cycle'},
                        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(CourseTopic.objects.count(), 0)
        self.assertEqual(Quiz.objects.count(), 0)
        self.assertEqual(GeneratedContent.objects.count(), 0)
        self.assertEqual(Summary.objects.count(), 0)
        self.assertIn(
            'Could not save generated course content',
            '\n'.join(captured_logs.output),
        )
        self.assertContains(response, 'could not be saved', status_code=500)

    def test_content_is_only_visible_to_its_teacher(self):
        topic = CourseTopic.objects.create(
            title='Private topic',
            teacher=self.teacher,
        )
        content = GeneratedContent.objects.create(
            course_topic=topic,
            notes='Private notes',
            key_points='Private key point',
        )
        other_teacher = get_user_model().objects.create_user(
            username='other',
            password='test-password',
        )
        self.client.force_login(other_teacher)

        response = self.client.get(
            reverse(
                'generator:content_detail',
                kwargs={'content_id': content.pk},
            )
        )
        self.assertEqual(response.status_code, 404)

    def test_student_dashboard_lists_courses_and_common_navigation(self):
        topic, _, _ = self.create_course_with_quiz()
        self.client.force_login(self.teacher)

        response = self.client.get(reverse('generator:student_dashboard'))

        self.assertContains(response, topic.title)
        self.assertContains(response, 'Open course')
        self.assertContains(response, 'Student home')
        self.assertContains(response, 'Create content')
        self.assertContains(response, 'generator/css/lms.')

    def test_student_can_view_content_and_submit_quiz(self):
        topic, question, answers = self.create_course_with_quiz()
        student = get_user_model().objects.create_user(
            username='student',
            password='test-password',
        )
        self.client.force_login(student)

        response = self.client.get(
            reverse(
                'generator:view_content',
                kwargs={'topic_id': topic.pk},
            )
        )
        self.assertContains(response, 'Water moves through a repeating cycle.')
        self.assertContains(response, 'Water evaporates and condenses.')
        self.assertContains(response, question.question_text)
        self.assertContains(response, 'name="question_%s"' % question.pk)
        self.assertContains(response, 'Evaporation')

        response = self.client.post(
            reverse(
                'generator:submit_quiz',
                kwargs={'topic_id': topic.pk},
            ),
            {f'question_{question.pk}': str(answers[0].pk)},
        )
        self.assertContains(response, '1 correct out of 1 questions')
        self.assertContains(response, '100%')
        self.assertContains(response, 'Correct')

        response = self.client.post(
            reverse(
                'generator:submit_quiz',
                kwargs={'topic_id': topic.pk},
            ),
            {f'question_{question.pk}': str(answers[1].pk)},
        )
        self.assertContains(response, '0 correct out of 1 questions')
        self.assertContains(response, 'Correct answer:')
        self.assertContains(response, 'Evaporation')

    def test_quiz_submission_rejects_non_post_requests(self):
        topic, _, _ = self.create_course_with_quiz()
        self.client.force_login(self.teacher)

        response = self.client.get(
            reverse(
                'generator:submit_quiz',
                kwargs={'topic_id': topic.pk},
            )
        )

        self.assertEqual(response.status_code, 405)


class ModelCRUDTests(TestCase):
    def setUp(self):
        self.teacher = get_user_model().objects.create_user(
            username='model-teacher',
        )

    def test_course_models_can_be_created_updated_and_deleted(self):
        topic = CourseTopic.objects.create(
            title='Original topic',
            teacher=self.teacher,
        )
        quiz = Quiz.objects.create(
            course_topic=topic,
            title='Original quiz',
            description='Original description.',
        )
        question = Question.objects.create(
            quiz=quiz,
            question_text='Original question?',
        )
        answer = Answer.objects.create(
            question=question,
            answer_text='Original answer',
            is_correct=False,
        )
        content = GeneratedContent.objects.create(
            course_topic=topic,
            quiz=quiz,
            notes='Original notes',
            key_points='Original point',
        )
        summary = Summary.objects.create(
            course_topic=topic,
            summary_text='Original summary',
        )

        topic.title = 'Updated topic'
        topic.save()
        quiz.title = 'Updated quiz'
        quiz.save()
        question.question_text = 'Updated question?'
        question.save()
        answer.answer_text = 'Updated answer'
        answer.is_correct = True
        answer.save()
        content.notes = 'Updated notes'
        content.save()
        summary.summary_text = 'Updated summary'
        summary.save()

        self.assertEqual(CourseTopic.objects.get(pk=topic.pk).title, 'Updated topic')
        self.assertEqual(Quiz.objects.get(pk=quiz.pk).title, 'Updated quiz')
        self.assertEqual(
            Question.objects.get(pk=question.pk).question_text,
            'Updated question?',
        )
        self.assertTrue(Answer.objects.get(pk=answer.pk).is_correct)
        self.assertEqual(GeneratedContent.objects.get(pk=content.pk).notes, 'Updated notes')
        self.assertEqual(
            Summary.objects.get(pk=summary.pk).summary_text,
            'Updated summary',
        )

        answer_id = answer.pk
        question_id = question.pk
        content_id = content.pk
        summary_id = summary.pk
        quiz_id = quiz.pk
        topic_id = topic.pk
        answer.delete()
        question.delete()
        content.delete()
        summary.delete()
        quiz.delete()
        topic.delete()
        self.assertFalse(Answer.objects.filter(pk=answer_id).exists())
        self.assertFalse(Question.objects.filter(pk=question_id).exists())
        self.assertFalse(GeneratedContent.objects.filter(pk=content_id).exists())
        self.assertFalse(Summary.objects.filter(pk=summary_id).exists())
        self.assertFalse(Quiz.objects.filter(pk=quiz_id).exists())
        self.assertFalse(CourseTopic.objects.filter(pk=topic_id).exists())


class PDFExportTests(TestCase):
    fixtures = ['course_materials.json']

    def setUp(self):
        self.teacher = get_user_model().objects.get(pk=1)
        self.client.force_login(self.teacher)

    def test_export_pdf_returns_downloadable_document(self):
        response = self.client.get(
            reverse(
                'generator:export_pdf',
                kwargs={'topic_id': 1},
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('attachment;', response['Content-Disposition'])
        self.assertIn('the-water-cycle.pdf', response['Content-Disposition'])
        pdf = b''.join(response.streaming_content)
        self.assertTrue(pdf.startswith(b'%PDF-'))
        self.assertGreater(len(pdf), 1000)

    def test_export_pdf_returns_404_for_missing_topic(self):
        response = self.client.get(
            reverse(
                'generator:export_pdf',
                kwargs={'topic_id': 999},
            )
        )

        self.assertEqual(response.status_code, 404)
