"""Shared pieces of the task and artifact routes: uploads and downloads (design M4 sections 9.5, 10.2)."""

import base64
import binascii
import re
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import Response

from ..core.artifact_rules import is_text_type
from ..core.models import HubError

UPLOAD_OVERHEAD = 64 * 1024          # multipart framing and form fields on top of the file itself
CHUNK = 64 * 1024
RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def decode_content(content, encoding: str | None) -> bytes:
    if not isinstance(content, str):
        raise HubError("bad_field", "content must be text")
    if (encoding or "utf8") == "utf8":
        return content.encode("utf-8")
    if encoding == "base64":
        try:
            return base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            raise HubError("bad_encoding", "content is not valid base64") from None
    raise HubError("bad_encoding", "encoding must be utf8 or base64")


async def read_upload(request: Request, max_bytes: int) -> tuple[dict, object]:
    """A multipart upload: (form fields, the `file` part). The declared size is checked before any body
    is read; the store checks the real size again while it copies the file."""
    length = request.headers.get("content-length")
    if length is None or not length.isdigit():
        raise HubError("length_required", "send Content-Length with the upload", 411)
    if int(length) > max_bytes + UPLOAD_OVERHEAD:
        raise HubError("too_large", f"the file is larger than {max_bytes} bytes", 413)
    form = await request.form(max_files=1, max_fields=10)
    upload = form.get("file")
    if upload is None or isinstance(upload, str):
        raise HubError("missing_field", "the upload needs a file part named file")
    fields = {k: v for k, v in form.items() if isinstance(v, str)}
    return fields, upload


async def chunks_of(upload):
    while True:
        chunk = await upload.read(CHUNK)
        if not chunk:
            return
        yield chunk


def download(artifact, data: bytes, range_header: str | None) -> Response:
    """The file as an attachment, never rendered inline by the browser."""
    media_type = artifact.media_type + ("; charset=utf-8" if is_text_type(artifact.media_type) else "")
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(artifact.name, safe='')}",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cache-Control": "private, no-store",
        "Accept-Ranges": "bytes",
    }
    match = RANGE.match(range_header or "")
    if match and (match.group(1) or match.group(2)):
        size = len(data)
        if match.group(1):
            start = int(match.group(1))
            end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
        else:                                            # bytes=-N: the last N bytes
            start, end = max(size - int(match.group(2)), 0), size - 1
        if start >= size or start > end:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(data[start:end + 1], status_code=206, media_type=media_type, headers=headers)
    return Response(data, media_type=media_type, headers=headers)
