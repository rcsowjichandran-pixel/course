import asyncio
import json
import logging
import os
from io import BytesIO
from xml.sax.saxutils import escape

import requests
from asgiref.sync import sync_to_async
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import DatabaseError, transaction
from django.db.models import Prefetch
from django.http import FileResponse, Http404, HttpResponseNotAllowed
from django.shortcuts import redirect, render
from django.utils.text import slugify
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    KeepTogether,
)

from .forms import CourseTopicForm
from .models import (
    Answer,
    CourseTopic,
    GeneratedContent,
    Question,
    Quiz,
    Summary,
)


logger = logging.getLogger(__name__)
GEMINI_API_URL = (
    'https://generativelanguage.googleapis.com/v1beta/models/'
    'gemini-3.5-flash-lite:generateContent'
)


class GeminiAPIError(Exception):
    pass


def parse_generated_content(response):
    try:
        response_data = response.json()
        generated_text = response_data['candidates'][0]['content']['parts'][0]['text']
        content = json.loads(generated_text)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise GeminiAPIError(
            'Gemini returned a response that could not be read. Please try again.'
        ) from exc

    if not isinstance(content, dict):
        raise GeminiAPIError(
            'Gemini returned content in an unexpected format. Please try again.'
        )

    notes = content.get('notes')
    key_points = content.get('keyPoints')
    summary_text = content.get('summary')
    quiz = content.get('quiz')
    if (
        not isinstance(notes, str)
        or not isinstance(key_points, list)
        or not all(isinstance(point, str) for point in key_points)
        or not isinstance(summary_text, str)
        or not isinstance(quiz, dict)
        or not isinstance(quiz.get('title'), str)
        or not isinstance(quiz.get('description'), str)
        or not isinstance(quiz.get('questions'), list)
        or not quiz['questions']
    ):
        raise GeminiAPIError(
            'Gemini returned content in an unexpected format. Please try again.'
        )

    for question in quiz['questions']:
        if (
            not isinstance(question, dict)
            or not isinstance(question.get('questionText'), str)
            or not isinstance(question.get('options'), list)
            or len(question['options']) < 2
        ):
            raise GeminiAPIError(
                'Gemini returned an invalid quiz. Please try again.'
            )

        options = question['options']
        if any(
            not isinstance(option, dict)
            or not isinstance(option.get('text'), str)
            or not isinstance(option.get('isCorrect'), bool)
            for option in options
        ) or sum(option['isCorrect'] for option in options) != 1:
            raise GeminiAPIError(
                'Gemini returned an invalid quiz. Please try again.'
            )

    return {
        'notes': notes,
        'key_points': key_points,
        'summary': summary_text,
        'quiz': quiz,
    }


async def generate_with_gemini(topic_title):
    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        raise GeminiAPIError(
            'Content generation is not configured. Set the GEMINI_API_KEY '
            'environment variable and restart the server.'
        )

    prompt = (
        'Act as an educational content generator. For the course topic '
        f'{topic_title!r}, create accurate, well-organized study notes, '
        'concise key points, a short summary, and a multiple-choice quiz. '
        'The quiz should have five questions with four options each and '
        'exactly one correct option per question. Return only a JSON object '
        'with this structure: {"notes":"string","keyPoints":["string"],'
        '"summary":"string","quiz":{"title":"string","description":"string",'
        '"questions":[{"questionText":"string","options":['
        '{"text":"string","isCorrect":true}]}]}}. '
        'Do not include Markdown fences or text outside the JSON.'
    )
    question_schema = {
        'type': 'OBJECT',
        'properties': {
            'questionText': {'type': 'STRING'},
            'options': {
                'type': 'ARRAY',
                'items': {
                    'type': 'OBJECT',
                    'properties': {
                        'text': {'type': 'STRING'},
                        'isCorrect': {'type': 'BOOLEAN'},
                    },
                    'required': ['text', 'isCorrect'],
                },
            },
        },
        'required': ['questionText', 'options'],
    }
    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {
            'responseMimeType': 'application/json',
            'responseSchema': {
                'type': 'OBJECT',
                'properties': {
                    'notes': {'type': 'STRING'},
                    'keyPoints': {
                        'type': 'ARRAY',
                        'items': {'type': 'STRING'},
                    },
                    'summary': {'type': 'STRING'},
                    'quiz': {
                        'type': 'OBJECT',
                        'properties': {
                            'title': {'type': 'STRING'},
                            'description': {'type': 'STRING'},
                            'questions': {
                                'type': 'ARRAY',
                                'items': question_schema,
                            },
                        },
                        'required': ['title', 'description', 'questions'],
                    },
                },
                'required': ['notes', 'keyPoints', 'summary', 'quiz'],
            },
        },
    }

    response = await asyncio.to_thread(
        requests.post,
        GEMINI_API_URL,
        params={'key': api_key},
        json=payload,
        timeout=45,
    )
    response.raise_for_status()
    return parse_generated_content(response)


def save_generated_content(teacher, topic_title, generated):
    with transaction.atomic():
        topic = CourseTopic.objects.create(
            title=topic_title,
            teacher=teacher,
        )
        quiz_data = generated['quiz']
        quiz = Quiz.objects.create(
            course_topic=topic,
            title=quiz_data['title'],
            description=quiz_data['description'],
        )
        generated_content = GeneratedContent.objects.create(
            course_topic=topic,
            quiz=quiz,
            notes=generated['notes'],
            key_points='\n'.join(generated['key_points']),
        )
        for question_data in quiz_data['questions']:
            question = Question.objects.create(
                quiz=quiz,
                question_text=question_data['questionText'],
            )
            Answer.objects.bulk_create(
                [
                    Answer(
                        question=question,
                        answer_text=option['text'],
                        is_correct=option['isCorrect'],
                    )
                    for option in question_data['options']
                ]
            )
        Summary.objects.create(
            course_topic=topic,
            summary_text=generated['summary'],
        )

    return generated_content.pk


def load_student_topics():
    topics = list(
        CourseTopic.objects.prefetch_related(
            Prefetch(
                'generated_contents',
                queryset=GeneratedContent.objects.select_related(
                    'quiz',
                ).order_by('-pk'),
                to_attr='student_generated_contents',
            ),
            Prefetch(
                'summaries',
                queryset=Summary.objects.order_by('-pk'),
                to_attr='student_summaries',
            ),
        ).order_by('title', 'pk')
    )
    for topic in topics:
        topic.latest_content = (
            topic.student_generated_contents[0]
            if topic.student_generated_contents
            else None
        )
        topic.latest_summary = (
            topic.student_summaries[0] if topic.student_summaries else None
        )
    return topics


@login_required
async def student_dashboard(request):
    user = await request.auser()
    topics = await sync_to_async(
        load_student_topics,
        thread_sensitive=True,
    )()
    return render(
        request,
        'generator/student_dashboard.html',
        {'topics': topics, 'user': user},
    )


@login_required
async def generate_content(request):
    user = await request.auser()
    if request.method == 'POST':
        form = CourseTopicForm(request.POST)
        if form.is_valid():
            try:
                generated = await generate_with_gemini(
                    form.cleaned_data['title']
                )
            except GeminiAPIError as exc:
                logger.warning('Gemini content generation failed: %s', exc)
                messages.error(request, str(exc))
                return render(
                    request,
                    'generator/generate_content.html',
                    {'form': form, 'user': user},
                    status=503,
                )
            except requests.HTTPError as exc:
                status_code = (
                    exc.response.status_code if exc.response else 'unknown'
                )
                logger.warning('Gemini API returned HTTP %s', status_code)
                messages.error(
                    request,
                    f'Gemini returned HTTP {status_code}. Check API key access, '
                    'model availability, and quota.',
                )
                return render(
                    request,
                    'generator/generate_content.html',
                    {'form': form, 'user': user},
                    status=502,
                )
            except requests.RequestException as exc:
                logger.warning(
                    'Gemini API request failed: %s',
                    type(exc).__name__,
                )
                messages.error(
                    request,
                    'The content service could not be reached. Please try again.',
                )
                return render(
                    request,
                    'generator/generate_content.html',
                    {'form': form, 'user': user},
                    status=502,
                )

            try:
                content_id = await sync_to_async(
                    save_generated_content,
                    thread_sensitive=True,
                )(user, form.cleaned_data['title'], generated)
            except DatabaseError:
                logger.exception('Could not save generated course content')
                messages.error(
                    request,
                    'The generated content could not be saved. Please try again.',
                )
                return render(
                    request,
                    'generator/generate_content.html',
                    {'form': form, 'user': user},
                    status=500,
                )

            return redirect(
                'generator:content_detail',
                content_id=content_id,
            )
    else:
        form = CourseTopicForm()

    return render(
        request,
        'generator/generate_content.html',
        {'form': form, 'user': user},
    )


@login_required
async def content_detail(request, content_id):
    user = await request.auser()
    try:
        content = await GeneratedContent.objects.select_related(
            'course_topic',
            'quiz',
        ).prefetch_related(
            'quiz__questions__answers',
        ).aget(
            pk=content_id,
            course_topic__teacher=user,
        )
    except GeneratedContent.DoesNotExist as exc:
        raise Http404 from exc

    summary = await Summary.objects.filter(
        course_topic_id=content.course_topic_id,
    ).afirst()
    return render(
        request,
        'generator/content_detail.html',
        {'content': content, 'summary': summary, 'user': user},
    )


@login_required
async def view_content(request, topic_id):
    user = await request.auser()
    try:
        topic = await CourseTopic.objects.aget(pk=topic_id)
    except CourseTopic.DoesNotExist as exc:
        raise Http404 from exc

    content = await GeneratedContent.objects.filter(
        course_topic=topic,
    ).select_related(
        'quiz',
    ).prefetch_related(
        'quiz__questions__answers',
    ).order_by(
        '-pk',
    ).afirst()
    summary = await Summary.objects.filter(
        course_topic=topic,
    ).order_by(
        '-pk',
    ).afirst()

    return render(
        request,
        'generator/view_content.html',
        {
            'topic': topic,
            'content': content,
            'summary': summary,
            'user': user,
            'key_points': (
                content.key_points.splitlines() if content is not None else []
            ),
        },
    )


@login_required
async def submit_quiz(request, topic_id):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    user = await request.auser()

    content = await GeneratedContent.objects.filter(
        course_topic_id=topic_id,
        quiz__isnull=False,
    ).select_related(
        'course_topic',
        'quiz',
    ).prefetch_related(
        'quiz__questions__answers',
    ).order_by(
        '-pk',
    ).afirst()
    if content is None:
        raise Http404

    questions = [
        question async for question in content.quiz.questions.all()
    ]
    if not questions:
        raise Http404

    results = []
    score = 0
    for question in questions:
        answers = list(question.answers.all())
        selected_id = request.POST.get(f'question_{question.pk}')
        selected_answer = next(
            (answer for answer in answers if str(answer.pk) == selected_id),
            None,
        )
        correct_answer = next(
            (answer for answer in answers if answer.is_correct),
            None,
        )
        is_correct = (
            selected_answer is not None
            and correct_answer is not None
            and selected_answer.pk == correct_answer.pk
        )
        score += int(is_correct)
        results.append(
            {
                'question': question,
                'selected_answer': selected_answer,
                'correct_answer': correct_answer,
                'is_correct': is_correct,
            }
        )

    return render(
        request,
        'generator/quiz_results.html',
        {
            'topic': content.course_topic,
            'quiz': content.quiz,
            'user': user,
            'results': results,
            'score': score,
            'total': len(questions),
            'percentage': round(score * 100 / len(questions)),
        },
    )


@login_required
def export_pdf(request, topic_id):
    try:
        topic = CourseTopic.objects.select_related('teacher').get(pk=topic_id)
    except CourseTopic.DoesNotExist as exc:
        raise Http404 from exc

    content = GeneratedContent.objects.filter(
        course_topic=topic,
    ).select_related(
        'quiz',
    ).prefetch_related(
        'quiz__questions__answers',
    ).order_by(
        '-pk',
    ).first()
    if content is None:
        raise Http404

    summary = Summary.objects.filter(
        course_topic=topic,
    ).order_by(
        '-pk',
    ).first()
    teacher_name = topic.teacher.get_full_name() or topic.teacher.get_username()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'CourseTitle',
        parent=styles['Title'],
        alignment=TA_CENTER,
        fontSize=28,
        leading=35,
        textColor=colors.HexColor('#14213d'),
        spaceAfter=18,
    )
    subtitle_style = ParagraphStyle(
        'CourseSubtitle',
        parent=styles['Normal'],
        alignment=TA_CENTER,
        fontSize=14,
        leading=20,
        textColor=colors.HexColor('#52627a'),
    )
    heading_style = ParagraphStyle(
        'CourseHeading',
        parent=styles['Heading1'],
        textColor=colors.HexColor('#193e9e'),
        spaceBefore=10,
        spaceAfter=9,
        keepWithNext=True,
    )
    question_style = ParagraphStyle(
        'QuizQuestion',
        parent=styles['Heading3'],
        textColor=colors.HexColor('#17243b'),
        spaceBefore=8,
        spaceAfter=5,
        keepWithNext=True,
    )
    body_style = ParagraphStyle(
        'CourseBody',
        parent=styles['BodyText'],
        fontSize=10.5,
        leading=16,
        spaceAfter=8,
    )

    def paragraphs(text):
        return [
            Paragraph(
                escape(part).replace('\n', '<br/>'),
                body_style,
            )
            for part in text.split('\n\n')
            if part.strip()
        ]

    story = [
        Spacer(1, 1.8 * inch),
        Paragraph(escape(topic.title), title_style),
        Paragraph('Course study materials', subtitle_style),
        Spacer(1, 0.8 * inch),
        Paragraph(f'Teacher: {escape(teacher_name)}', subtitle_style),
        PageBreak(),
    ]

    story.append(Paragraph('Summary', heading_style))
    story.extend(
        paragraphs(summary.summary_text)
        if summary
        else [Paragraph('No summary is available.', body_style)]
    )

    story.append(Paragraph('Notes', heading_style))
    story.extend(paragraphs(content.notes))

    story.append(Paragraph('Key points', heading_style))
    key_points = [
        point.strip()
        for point in content.key_points.splitlines()
        if point.strip()
    ]
    if key_points:
        story.append(
            ListFlowable(
                [
                    ListItem(Paragraph(escape(point), body_style))
                    for point in key_points
                ],
                bulletType='bullet',
                start='circle',
                leftIndent=20,
            )
        )
    else:
        story.append(Paragraph('No key points are available.', body_style))

    if content.quiz:
        questions = list(content.quiz.questions.all())
        story.append(Paragraph(escape(content.quiz.title), heading_style))
        story.extend(paragraphs(content.quiz.description))
        for index, question in enumerate(questions, start=1):
            option_paragraphs = [
                Paragraph(
                    f'{chr(64 + option_index)}. {escape(answer.answer_text)}',
                    body_style,
                )
                for option_index, answer in enumerate(
                    question.answers.all(),
                    start=1,
                )
            ]
            story.append(
                KeepTogether(
                    [
                        Paragraph(
                            f'{index}. {escape(question.question_text)}',
                            question_style,
                        ),
                        *option_paragraphs,
                    ]
                )
            )

        story.append(PageBreak())
        story.append(Paragraph('Quiz answer key', heading_style))
        for index, question in enumerate(questions, start=1):
            correct_answer = next(
                (
                    answer
                    for answer in question.answers.all()
                    if answer.is_correct
                ),
                None,
            )
            answer_text = (
                correct_answer.answer_text
                if correct_answer
                else 'No correct answer is marked.'
            )
            story.append(
                Paragraph(
                    f'{index}. {escape(question.question_text)}',
                    question_style,
                )
            )
            story.append(Paragraph(escape(answer_text), body_style))
    else:
        story.append(Paragraph('No quiz is available for this topic.', body_style))

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=0.8 * inch,
        rightMargin=0.8 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        title=topic.title,
        author=teacher_name,
    )

    def add_page_footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor('#dce5ef'))
        canvas.line(
            doc.leftMargin,
            0.55 * inch,
            letter[0] - doc.rightMargin,
            0.55 * inch,
        )
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.HexColor('#64748b'))
        canvas.drawString(doc.leftMargin, 0.35 * inch, 'Course Studio')
        canvas.drawRightString(
            letter[0] - doc.rightMargin,
            0.35 * inch,
            str(doc.page),
        )
        canvas.restoreState()

    document.build(
        story,
        onFirstPage=add_page_footer,
        onLaterPages=add_page_footer,
    )
    buffer.seek(0)
    filename = slugify(topic.title) or f'course-topic-{topic.pk}'
    return FileResponse(
        buffer,
        as_attachment=True,
        filename=f'{filename}.pdf',
        content_type='application/pdf',
    )
