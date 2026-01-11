from stardag_api.routes.auth import router as auth_router
from stardag_api.routes.builds import router as builds_router
from stardag_api.routes.locks import router as locks_router
from stardag_api.routes.organizations import router as organizations_router
from stardag_api.routes.search import router as search_router
from stardag_api.routes.target_roots import router as target_roots_router
from stardag_api.routes.tasks import router as tasks_router
from stardag_api.routes.ui import router as ui_router

__all__ = [
    "auth_router",
    "builds_router",
    "locks_router",
    "organizations_router",
    "search_router",
    "target_roots_router",
    "tasks_router",
    "ui_router",
]
