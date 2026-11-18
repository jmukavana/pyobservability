from typing import Any

from pyobservability.bootstrap import configure
from pyobservability.exceptions import MissingExtraError

try:
    from django.apps import AppConfig
    from django.conf import settings
except ImportError as error:
    raise MissingExtraError("django", "the Django integration") from error

SETTINGS_NAME = "PYOBSERVABILITY"


class PyObservabilityAppConfig(AppConfig):
    """Configures the SDK once Django's settings are loaded."""

    name = "pyobservability.integrations.django"
    label = "pyobservability"
    verbose_name = "PyObservability"

    def ready(self) -> None:
        options: dict[str, Any] | None = getattr(settings, SETTINGS_NAME, None)
        if options is not None:
            configure(**options)
