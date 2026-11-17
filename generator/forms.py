from django import forms

from .models import CourseTopic


class CourseTopicForm(forms.ModelForm):
    class Meta:
        model = CourseTopic
        fields = ['title']
        widgets = {
            'title': forms.TextInput(
                attrs={
                    'autofocus': True,
                    'placeholder': 'e.g. The water cycle',
                }
            ),
        }
