"""Thin top-level re-export of the TradeThrone FastAPI application.

Run with ``uvicorn main:app --reload`` from the repo root (or any WSGI/ASGI
server targeting ``main:app``).  All routes, middleware, lifespan wiring and
startup/shutdown hooks live in ``app.main`` — this module exists only so the
project-root ``main.py`` entry point is a stable ASGI target.
"""

from app.main import app  # noqa: F401  (re-exported; used by uvicorn/confs)
