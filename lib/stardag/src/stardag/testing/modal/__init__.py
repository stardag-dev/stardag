"""Modal integration test utilities.

Provides pre-defined tasks and a test app factory for end-to-end Modal
integration tests. Tasks are defined inside the ``stardag`` package so
they can be deserialized in Modal containers (which install stardag from
local source).

Usage in tests::

    from stardag.testing.modal import create_test_app, make_range, sum_list

    # Module-level setup (Modal discovers the app here)
    stardag_app, finalize_result = create_test_app()
    app = stardag_app.modal_app  # for `modal deploy`

    # In a test:
    def test_build():
        root = sum_list(values=make_range(limit=5))
        result = stardag_app.build_remote(root)
        assert root.target().load() == 10
"""

from stardag.testing.modal._tasks import make_range, sum_list
from stardag.testing.modal._app import create_test_app

__all__ = [
    "create_test_app",
    "make_range",
    "sum_list",
]
