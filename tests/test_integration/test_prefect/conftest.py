import pytest

try:
    from prefect.testing.utilities import prefect_test_harness
except ImportError:
    # If the user doesn't have prefect installed, we'll provide a dummy harness
    from contextlib import contextmanager

    @contextmanager
    def prefect_test_harness():
        yield



@pytest.fixture(autouse=True, scope="session")
def prefect_test_fixture():
    with prefect_test_harness():
        yield
