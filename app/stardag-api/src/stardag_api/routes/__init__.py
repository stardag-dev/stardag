from stardag_api.routes.auth import router as auth_router
from stardag_api.routes.registry_v2 import router as registry_v2_router
from stardag_api.routes.target_roots import router as target_roots_router
from stardag_api.routes.ui import router as ui_router
from stardag_api.routes.workspaces import router as workspaces_router

__all__ = [
    "auth_router",
    "registry_v2_router",
    "target_roots_router",
    "ui_router",
    "workspaces_router",
]
