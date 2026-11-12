from django.urls import path

from . import views

# In every language, like the other pages of the site; the calendar feed is outside the languages (config/urls.py)
urlpatterns = [path("", views.page, name="meetups")]
