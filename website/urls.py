from django.urls import path
from django.views.generic import RedirectView

from . import views

# The home page has a single address, /<language>/, as in the sitemap: /<language>/home/ redirects to it
urlpatterns = [path("", views.page, {"slug": "home"}, name="home"),
               path("home/", RedirectView.as_view(pattern_name="home", permanent=True, query_string=True)),
               path("<slug:slug>/", views.page, name="page")]
