import sys

import pytest

from libretranslate.app import create_app
from libretranslate.main import get_args


@pytest.fixture()
def app():
    sys.argv = ['', '--load-only', 'en,es']
    app = create_app(get_args())

    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def make_app():
    """Factory fixture for creating apps with custom CLI arguments."""
    def _make_app(extra_args=None):
        argv = ['', '--load-only', 'en,es']
        if extra_args:
            argv.extend(extra_args)
        sys.argv = argv
        return create_app(get_args())
    return _make_app


@pytest.fixture()
def make_client(make_app):
    """Factory fixture for creating test clients with custom CLI arguments."""
    def _make_client(extra_args=None):
        app = make_app(extra_args)
        return app.test_client()
    return _make_client
