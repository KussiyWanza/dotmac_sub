# Minimal local stub, following the existing stubs/ convention (paramiko,
# pymysql, requests). Only what app/ actually calls is declared: a stub that
# guesses at the rest of PyYAML would be a second, unmaintained copy of the
# library's surface.
#
# `safe_load` genuinely returns Any -- it parses arbitrary YAML. The typing
# discipline is applied one layer up, where app.shadow.compose_contract
# immediately validates the result into closed models.
from __future__ import annotations

from typing import Any

class YAMLError(Exception): ...

def safe_load(stream: str | bytes) -> Any: ...
