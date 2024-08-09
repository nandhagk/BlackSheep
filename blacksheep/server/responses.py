import ntpath
from enum import Enum
from functools import lru_cache
from io import BytesIO
from typing import Any, AnyStr, AsyncIterable, Callable, Optional, Union

from blacksheep import Content, Response, StreamedContent
from blacksheep.common.files.asyncfs import FilesHandler
from blacksheep.settings.html import html_settings

MessageType = Any


class ContentDispositionType(Enum):
    """
    Represents the type for a Content-Disposition header (inline or attachment).
    The Content-Disposition response header is a header indicating if the content
    is expected to be displayed inline in the browser as part of a Web page
    or as an attachment, that is downloaded and saved locally.
    """

    INLINE = "inline"
    ATTACHMENT = "attachment"


def _ensure_bytes(value: AnyStr) -> bytes:
    if isinstance(value, str):
        return value.encode()
    if isinstance(value, bytes):
        return value
    raise ValueError("Input value must be bytes or str")


FileInput = Union[Callable[[], AsyncIterable[bytes]], str, bytes, bytearray, BytesIO]


@lru_cache(2000)
def _get_file_provider(file_path: str) -> Callable[[], AsyncIterable[bytes]]:
    async def data_provider():
        async for chunk in FilesHandler().chunks(file_path):
            yield chunk

    return data_provider


def _file(
    value: FileInput,
    content_type: str,
    content_disposition_type: ContentDispositionType,
    file_name: Optional[str] = None,
) -> Response:
    if file_name:
        exact_file_name = ntpath.basename(file_name)
        if not exact_file_name:
            raise ValueError(
                "Invalid file name: it should be an exact "
                'file name without path, for example: "foo.txt"'
            )

        content_disposition_value = (
            f'{content_disposition_type.value}; filename="{exact_file_name}"'
        )
    else:
        content_disposition_value = content_disposition_type.value

    content: Content
    content_type_value = _ensure_bytes(content_type)

    if isinstance(value, str):
        # value is treated as a path
        content = StreamedContent(content_type_value, _get_file_provider(value))
    elif isinstance(value, BytesIO):

        async def data_provider():
            try:
                value.seek(0)

                while True:
                    chunk = value.read(1024 * 64)

                    if not chunk:
                        break

                    yield chunk
                yield b""
            finally:
                if not value.closed:
                    value.close()

        content = StreamedContent(content_type_value, data_provider)
    elif callable(value):
        # value is treated as an async generator
        async def data_provider():
            async for chunk in value():
                yield chunk
            yield b""

        content = StreamedContent(content_type_value, data_provider)
    elif isinstance(value, bytes):
        content = Content(content_type_value, value)
    elif isinstance(value, bytearray):
        content = Content(content_type_value, bytes(value))
    else:
        raise ValueError(
            "Invalid value, expected one of: Callable, str, "
            "bytes, bytearray, io.BytesIO"
        )

    return Response(
        200, [(b"Content-Disposition", content_disposition_value.encode())], content
    )


def file(
    value: FileInput,
    content_type: str,
    *,
    file_name: Optional[str] = None,
    content_disposition: ContentDispositionType = ContentDispositionType.ATTACHMENT,
) -> Response:
    """
    Returns a binary file response with given content type and optional
    file name, for download (attachment)
    (default HTTP 200 OK). This method supports both call with bytes,
    or a generator yielding chunks.

    Remarks: this method does not handle cache, ETag and HTTP 304 Not Modified
    responses; when handling files it is recommended to handle cache, ETag and
    Not Modified, according to use case.
    """
    return _file(value, content_type, content_disposition, file_name)


def _create_html_response(html: str):
    """Creates a Response to serve dynamic HTML. Caching is disabled."""
    return Response(200, [(b"Cache-Control", b"no-cache")]).with_content(
        Content(b"text/html; charset=utf-8", html.encode("utf8"))
    )


def view(name: str, model: Any = None, **kwargs) -> Response:
    """
    Returns a Response object with HTML obtained using synchronous rendering.

    This method relies on the engine configured for rendering (defaults to Jinja2):
    see `blacksheep.settings.html.html_settings.renderer`
    and `blacksheep.server.rendering.abc.Renderer`.
    """
    renderer = html_settings.renderer
    if model:
        return _create_html_response(
            renderer.render(name, html_settings.model_to_params(model), **kwargs)
        )
    return _create_html_response(renderer.render(name, None, **kwargs))


async def view_async(name: str, model: Any = None, **kwargs) -> Response:
    """
    Returns a Response object with HTML obtained using asynchronous rendering.

    This method relies on the engine configured for rendering (defaults to Jinja2):
    see `blacksheep.settings.html.html_settings.renderer`
    and `blacksheep.server.rendering.abc.Renderer`.
    """
    renderer = html_settings.renderer
    if model:
        return _create_html_response(
            await renderer.render_async(
                name, html_settings.model_to_params(model), **kwargs
            )
        )
    return _create_html_response(await renderer.render_async(name, None, **kwargs))
