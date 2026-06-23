"""Application dependencies package.

- ``dependencies`` — declaration: the ``Dependencies`` singleton container and its getters;
- ``init_dependencies`` — construction and wiring of the modules at application startup.
"""

from src.dependencies.dependencies import Dependencies, get_dependencies  # noqa: F401
from src.dependencies.init_dependencies import init_dependencies  # noqa: F401

__all__ = ["Dependencies", "get_dependencies", "init_dependencies"]
