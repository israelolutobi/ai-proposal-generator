"""
URL configuration for mysite project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib.sitemaps.views import sitemap
from proposal_ai.sitemaps import PublicSitemap
from django.contrib import admin
from django.urls import path, include
from django.http import HttpResponse
from . import views
from . import health

sitemaps = {
    "public": PublicSitemap,
}


urlpatterns = [
    path("robots.txt", lambda request: HttpResponse("User-agent: *\nAllow: /\n", content_type="text/plain"), name="robots_txt"),
    path("health/live/", health.live, name="health_live"),
    path("health/ready/", health.ready, name="health_ready"),
    path('admin/', admin.site.urls),
       path(
        "sitemap.xml",
        sitemap,
        {"sitemaps": sitemaps},
        name="django.contrib.sitemaps.views.sitemap",
    ),
    path("", include("proposal_ai.urls")),
    # path("", views.proposal_main_page_view, name="proposal_main_page"),
    # path('result/', views.proposal_result, name='proposal_result'),
 
]
