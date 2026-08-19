"""
platform_operator/handlers/__init__.py
Exports all Kopf handler modules so that main.py can import them
and trigger handler registration via @kopf.on.* decorators.
"""

from . import postgres   # noqa: F401
from . import istio      # noqa: F401
from . import monitoring # noqa: F401
from . import airflow    # noqa: F401
