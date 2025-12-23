from stardag_api.routes.builds import router as builds_router
from stardag_api.routes.organizations import router as organizations_router
from stardag_api.routes.target_roots import router as target_roots_router
from stardag_api.routes.tasks import router as tasks_router
from stardag_api.routes.ui import router as ui_router

__all__ = [
    "builds_router",
    "organizations_router",
    "target_roots_router",
    "tasks_router",
    "ui_router",
]
