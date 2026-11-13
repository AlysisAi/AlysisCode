"""Compress only authenticated computer-frame responses, retaining no-store."""

import re

from starlette.middleware.gzip import GZipMiddleware
from starlette.routing import get_route_path


class ComputerFrameCompression:
    def __init__(self, app):
        self.app = app
        self.compressed = GZipMiddleware(app, minimum_size=2048, compresslevel=3)

    async def __call__(self, scope, receive, send):
        frame = (
            scope["type"] == "http"
            and scope["method"] == "GET"
            and re.fullmatch(r"/v1/bots/[a-f0-9]{32}/computer", get_route_path(scope))
        )
        await (self.compressed if frame else self.app)(scope, receive, send)
