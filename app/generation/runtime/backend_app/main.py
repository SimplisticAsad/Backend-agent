"""ASGI entry point:  uvicorn backend_app.main:app"""
from .app_factory import create_app

app = create_app()
