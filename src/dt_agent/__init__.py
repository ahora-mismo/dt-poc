"""dt_agent: AI Email & Calendar Assistant."""

import os

# PydanticAI prints an observability banner on its first model call; the app's
# output is its structured log only (D-58).
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
