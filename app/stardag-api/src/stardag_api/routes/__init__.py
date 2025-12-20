from stardag_api.routes.builds import router as builds_router
from stardag_api.routes.tasks import router as tasks_router
from stardag_api.routes.ui import router as ui_router

__all__ = ["builds_router", "tasks_router", "ui_router"]
