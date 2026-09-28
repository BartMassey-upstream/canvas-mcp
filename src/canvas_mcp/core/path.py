"""Safe construction of Canvas API paths."""

from urllib.parse import quote


def _encode_path_segment(value: str | int) -> str:
    """Encode one raw value without allowing it to change route structure."""
    text = str(value)
    if not text:
        raise ValueError("Canvas API path segments cannot be empty")

    # urllib deliberately leaves RFC-unreserved periods alone. Encoding them
    # as well keeps traversal-looking values inert and follows Canvas's own SIS
    # ID example (``CS/101.11é`` -> ``CS%2F101%2E11%C3%A9``).
    return quote(text, safe=":").replace(".", "%2E")


def canvas_path(*segments: str | int) -> str:
    """Build an API path from raw, independently encoded path segments."""
    if not segments:
        raise ValueError("A Canvas API path needs at least one segment")
    return "/" + "/".join(_encode_path_segment(segment) for segment in segments)
