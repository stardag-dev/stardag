"""ASGI middleware that decompresses gzipped request bodies.

Starlette's built-in ``GZipMiddleware`` only handles *response*
compression. The SDK's bulk-register path gzips request bodies above a
small threshold to keep payload sizes manageable for large batches
(repeated JSON keys / structure compress 5–10×). This middleware
recognises ``Content-Encoding: gzip`` on the way in, decompresses the
body once, and replays the decoded bytes to the downstream app —
transparently, so route handlers (and Pydantic body parsers) see the
same JSON they would have without compression.

Pass-through for non-gzipped requests: a request without the encoding
header (or with an unknown encoding) goes through untouched, so older
SDK versions and direct ``curl`` callers keep working.
"""

from __future__ import annotations

import gzip

# Refuse to decompress oversized bodies. Bounds memory; the SDK is
# expected to chunk well under this. 50 MB compressed is generous —
# typical bulk-register batches at our chunk size compress to <1 MB.
_MAX_DECOMPRESSED_BYTES = 50 * 1024 * 1024


class GZipRequestMiddleware:
    """Decompress gzipped request bodies before they reach route handlers.

    Implemented as a pure ASGI middleware (rather than
    ``BaseHTTPMiddleware``) so we can replay the receive callable with
    the decompressed body cleanly, without buffering through Starlette's
    HTTP-message abstraction twice.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Headers are a list of (name_bytes, value_bytes); search for
        # content-encoding case-insensitively.
        content_encoding = b""
        for name, value in scope["headers"]:
            if name.lower() == b"content-encoding":
                content_encoding = value
                break
        if content_encoding.strip().lower() != b"gzip":
            await self.app(scope, receive, send)
            return

        # Drain the entire request body into memory. Bulk-register
        # requests are bounded by the SDK's chunk size (50 tasks ×
        # task_data limit), so this is safe.
        body_chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                if chunk:
                    body_chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                # Client went away mid-upload; bail out.
                await self.app(scope, receive, send)
                return
            else:
                # Unknown message type — pass it through and stop reading.
                break

        compressed = b"".join(body_chunks)
        try:
            decompressed = gzip.decompress(compressed)
        except (OSError, EOFError):
            # Malformed gzip — return 400 directly. We can't pass the
            # body through to the app in any sensible form.
            await send(
                {
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [
                        (b"content-type", b"application/json"),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"detail":"Malformed gzip request body"}',
                }
            )
            return

        if len(decompressed) > _MAX_DECOMPRESSED_BYTES:
            await send(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [
                        (b"content-type", b"application/json"),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"detail":"Decompressed request body exceeds size limit"}',
                }
            )
            return

        # Build a new headers list: drop content-encoding (otherwise
        # downstream might double-decompress) and replace content-length
        # with the decompressed size so any downstream body() reader
        # behaves as expected.
        new_headers = [
            (name, value)
            for name, value in scope["headers"]
            if name.lower() not in (b"content-encoding", b"content-length")
        ]
        new_headers.append((b"content-length", str(len(decompressed)).encode()))
        new_scope = {**scope, "headers": new_headers}

        # Replay the decompressed body as a single ``http.request``
        # message. After that, downstream callers reading further get
        # ``http.disconnect`` (matching what they'd see at end-of-stream).
        sent_body = False
        sent_disconnect = False

        async def replay_receive():
            nonlocal sent_body, sent_disconnect
            if not sent_body:
                sent_body = True
                return {
                    "type": "http.request",
                    "body": decompressed,
                    "more_body": False,
                }
            if not sent_disconnect:
                sent_disconnect = True
                return {"type": "http.disconnect"}
            # Defensive — avoid hot-looping if called again.
            return {"type": "http.disconnect"}

        await self.app(new_scope, replay_receive, send)
