# Database models

# Register the autonomous-agent ORM models (agent.py) with Base.metadata so
# Alembic autogenerate and the CI schema-parity guard include the agent tables.
from app.models import agent  # noqa: F401
