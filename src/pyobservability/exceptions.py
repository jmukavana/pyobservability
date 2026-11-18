class PyObservabilityError(Exception):
    pass


class ConfigurationError(PyObservabilityError):
    pass


class AlreadyConfiguredError(ConfigurationError):
    pass


class SecurityPolicyError(PyObservabilityError):
    pass


class MissingExtraError(PyObservabilityError, ImportError):
    def __init__(self, extra: str, feature: str) -> None:
        super().__init__(
            f'{feature} needs an optional extra: pip install "pyobservability[{extra}]"'
        )
        self.extra = extra


class NotConfiguredError(PyObservabilityError):
    pass


class AuditValidationError(PyObservabilityError, ValueError):
    pass


class AuditDurabilityError(PyObservabilityError):
    pass


class PyObservabilityWarning(UserWarning):
    pass
