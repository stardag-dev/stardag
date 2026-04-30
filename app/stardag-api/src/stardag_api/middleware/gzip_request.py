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

Decompression is **streaming** with running size caps on both the
compressed input and the decompressed output, so a gzip-bomb (small
compressed, huge decompressed) can't allocate unbounded memory before
we notice — we abort the moment either limit is breached.
"""

from __future__ import annotations

import zlib

# Reject oversized inbound bodies. Compressed-size limit is the first
# line of defence: even a perfectly-compressed gzip bomb can't slip past
# this cap. Decompressed-size limit catches the case where a moderately
# sized compressed body inflates beyond what we want to handle.
# Typical bulk-register batches at our chunk size compress to <100 KB,
# so the limits are generous (>100× expected payloads).
_MAX_COMPRESSED_BYTES = 10 * 1024 * 1024
_MAX_DECOMPRESSED_BYTES = 50 * 1024 * 1024
# zlib gzip mode: ``MAX_WBITS | 16`` enables gzip header parsing.
_GZIP_WBITS = zlib.MAX_WBITS | 16


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

        # Stream-decompress: read body chunk by chunk, feed into the
        # decompressor, and bail the moment either the compressed input
        # *or* the running decompressed output crosses its cap. This
        # bounds memory at ``_MAX_DECOMPRESSED_BYTES`` even for
        # adversarial inputs (gzip bombs) — we never materialise the
        # whole decompressed body if it's going to exceed the limit.
        decompressor = zlib.decompressobj(_GZIP_WBITS)
        decompressed_chunks: list[bytes] = []
        compressed_total = 0
        decompressed_total = 0

        async def _send_error(status: int, detail: str) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": (b'{"detail":"' + detail.encode() + b'"}'),
                }
            )

        while True:
            message = await receive()
            msg_type = message["type"]
            if msg_type == "http.disconnect":
                # Client went away mid-upload; nothing useful to do.
                # Hand back to the app with the original receive so it
                # sees the disconnect itself.
                await self.app(scope, receive, send)
                return
            if msg_type != "http.request":
                # Unexpected message type; bail to the app with the
                # original receive (it'll handle whatever this is).
                await self.app(scope, receive, send)
                return

            chunk = message.get("body", b"")
            more_body = message.get("more_body", False)

            if chunk:
                compressed_total += len(chunk)
                if compressed_total > _MAX_COMPRESSED_BYTES:
                    await _send_error(413, "Compressed request body exceeds size limit")
                    return
                try:
                    decoded = decompressor.decompress(
                        chunk,
                        max_length=_MAX_DECOMPRESSED_BYTES - decompressed_total + 1,
                    )
                except zlib.error:
                    await _send_error(400, "Malformed gzip request body")
                    return
                if decoded:
                    decompressed_total += len(decoded)
                    if decompressed_total > _MAX_DECOMPRESSED_BYTES:
                        await _send_error(
                            413,
                            "Decompressed request body exceeds size limit",
                        )
                        return
                    # ``unconsumed_tail`` would indicate the decoder hit
                    # ``max_length`` mid-chunk; with our +1 budget that
                    # only happens when the decompressed output already
                    # exceeds the cap, which we just rejected above.
                    decompressed_chunks.append(decoded)

            if not more_body:
                break

        try:
            tail = decompressor.flush()
        except zlib.error:
            await _send_error(400, "Malformed gzip request body")
            return
        if tail:
            decompressed_total += len(tail)
            if decompressed_total > _MAX_DECOMPRESSED_BYTES:
                await _send_error(413, "Decompressed request body exceeds size limit")
                return
            decompressed_chunks.append(tail)

        decompressed = b"".join(decompressed_chunks)

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
        # message. Subsequent reads return another empty
        # ``http.request`` with ``more_body=False`` (a benign
        # end-of-stream signal) rather than ``http.disconnect`` — some
        # downstream readers treat disconnect as "client gave up", which
        # isn't accurate here: the body was fully delivered, just
        # already consumed.
        sent_body = False

        async def replay_receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {
                    "type": "http.request",
                    "body": decompressed,
                    "more_body": False,
                }
            return {
                "type": "http.request",
                "body": b"",
                "more_body": False,
            }

        await self.app(new_scope, replay_receive, send)
