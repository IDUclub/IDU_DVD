"""Application configuration package.

The configuration class and its singleton live in ``app_config``; this module only re-exports
them for backward compatibility (``from src.common.config import Settings, settings``).
"""

from src.common.config.app_config import Settings, settings  # noqa: F401

__all__ = ["Settings", "settings"]
