"""gunicorn 入口。"""
from .web import create_app

app = create_app()
