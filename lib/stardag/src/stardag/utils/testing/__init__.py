from stardag import auto_namespace
from stardag.target._factory import _target_roots_override as target_roots_override

auto_namespace(__name__)  # set the namespace for this module to the module import path

__all__ = [
    "target_roots_override",
]
