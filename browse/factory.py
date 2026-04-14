"""Application factory for Parallel ArXiv."""

import logging
from flask.logging import default_handler
from flask import Flask
from arxiv.base import Base

from browse.config import Settings
from browse.routes import api_papers
from browse.routes import ui
from browse.routes import webhook
from browse.services.database import init_db


def create_web_app(**kwargs) -> Flask:
    """Initialize the Parallel ArXiv web application."""
    root = logging.getLogger()
    root.addHandler(default_handler)

    settings = Settings(**kwargs)

    app = Flask('browse',
                static_url_path=f'/static/browse/{settings.APP_VERSION}')
    app.config.from_object(settings)

    Base(app)
    init_db(app)
    app.register_blueprint(ui.blueprint)
    app.register_blueprint(webhook.blueprint)
    app.register_blueprint(api_papers.blueprint)

    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True

    return app
