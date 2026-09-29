"""Machine-local workspace registry."""

from .home import agentflow_home_path, ensure_registry_home, registry_home_path
from .service import (
    ServiceEndpoint,
    ServiceError,
    StopResult,
    serve_foreground,
    service_status,
    start_detached,
    stop,
)

__all__ = [
    "ServiceEndpoint",
    "ServiceError",
    "StopResult",
    "agentflow_home_path",
    "ensure_registry_home",
    "registry_home_path",
    "serve_foreground",
    "service_status",
    "start_detached",
    "stop",
]
