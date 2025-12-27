"""Test configuration that imports shared fixtures.

This file re-exports all fixtures from the stardag_integration_tests package
so they are available to all tests in this directory.
"""

# Re-export all fixtures from the package
from stardag_integration_tests.conftest import *  # noqa: F401, F403
from stardag_integration_tests.docker_fixtures import *  # noqa: F401, F403
