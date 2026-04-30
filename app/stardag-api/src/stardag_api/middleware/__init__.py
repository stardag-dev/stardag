"""ASGI middleware for the stardag API."""

from stardag_api.middleware.gzip_request import GZipRequestMiddleware

__all__ = ["GZipRequestMiddleware"]
