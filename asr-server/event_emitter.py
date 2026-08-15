from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class AsyncEventEmitter:
    """Small async event emitter used by the standalone ASR package."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[..., Any]]] = {}

    def on(self, event_type: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self._handlers.setdefault(event_type, []).append(func)
            return func

        return decorator

    def off(self, event_type: str, func: Callable[..., Any]) -> None:
        handlers = self._handlers.get(event_type)
        if handlers and func in handlers:
            handlers.remove(func)

    async def emit(self, event_type: str, *args: Any, **kwargs: Any) -> None:
        for handler in list(self._handlers.get(event_type, [])):
            try:
                result = handler(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("Error in emit(%s)", event_type)
