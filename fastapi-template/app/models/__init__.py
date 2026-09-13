# Database models

# Register the autonomous-agent ORM models (agent.py + agent_intent.py +
# agent_control.py) with Base.metadata so Alembic autogenerate and the CI
# schema-parity guard include the agent tables.
from app.models import agent  # noqa: F401
from app.models import agent_intent  # noqa: F401
from app.models import agent_control  # noqa: F401
