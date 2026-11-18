import os
import re

from pydantic import SecretStr

from pyobservability.exceptions import ConfigurationError

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def resolve_dsn(dsn: str | SecretStr | None, dsn_env: str | None, *, owner: str) -> str:
    if isinstance(dsn, SecretStr):
        return dsn.get_secret_value()
    if dsn is not None:
        return dsn
    if dsn_env is not None and (value := os.environ.get(dsn_env)):
        return value
    raise ConfigurationError(f"{owner} needs `dsn` or a set `dsn_env` variable")


def validated_identifier(name: str) -> str:
    if not _IDENTIFIER.match(name):
        raise ConfigurationError(f"invalid PostgreSQL schema name {name!r}")
    return name
