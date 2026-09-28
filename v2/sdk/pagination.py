"""Bound native pagination without returning incomplete aggregates."""

import asyncio
from contextlib import asynccontextmanager

from ..errors import V2Error


@asynccontextmanager
async def page_deadline():
    try:
        async with asyncio.timeout(120):
            yield
    except TimeoutError:
        raise V2Error("pagination_incomplete", "Native pagination timed out.", status=504) from None
