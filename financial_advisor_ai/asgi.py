"""
ASGI config for financial_advisor_ai project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.0/howto/deployment/asgi/
"""

import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
import advisor_agent.routing

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'financial_advisor_ai.settings')

application = ProtocolTypeRouter({
    'http': get_asgi_application(),
    'websocket': AuthMiddlewareStack(
        URLRouter(
            advisor_agent.routing.websocket_urlpatterns
        )
    ),
})
