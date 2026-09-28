"""Shared pytest configuration.

Blocks any real LLM request globally (D-52): unit tests must run offline,
using PydanticAI's ``TestModel`` / ``FunctionModel`` instead of live models.
"""

import pydantic_ai.models

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
