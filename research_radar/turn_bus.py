"""In-process Pub/Sub event bus and cooperative cancellation registry for streaming chat turns."""

import asyncio
import threading


class TurnEventBus:
    """Thread-safe event bus bridging sync chat workers and async FastAPI SSE streams."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}
        self._cancelled: set[str] = set()

    def subscribe(self, turn_id: str) -> asyncio.Queue:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        with self._lock:
            self._subs.setdefault(turn_id, set()).add((loop, queue))
        return queue

    def unsubscribe(self, turn_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            subs = self._subs.get(turn_id)
            if not subs:
                return
            dead = [pair for pair in subs if pair[1] is queue]
            for pair in dead:
                subs.discard(pair)
            if not subs:
                self._subs.pop(turn_id, None)

    def publish(self, turn_id: str, event_type: str, data: dict) -> None:
        with self._lock:
            targets = list(self._subs.get(turn_id, ()))
        if not targets:
            return
        packet = {"event": event_type, "data": data}
        for loop, queue in targets:
            if loop.is_closed():
                continue

            def _put(q=queue, item=packet):
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    q.put_nowait(item)
                except asyncio.QueueFull:
                    pass

            try:
                loop.call_soon_threadsafe(_put)
            except RuntimeError:
                pass

    def mark_cancelled(self, turn_id: str) -> None:
        with self._lock:
            self._cancelled.add(turn_id)

    def clear_cancelled(self, turn_id: str) -> None:
        with self._lock:
            self._cancelled.discard(turn_id)

    def is_cancelled(self, turn_id: str) -> bool:
        with self._lock:
            return turn_id in self._cancelled


TURN_BUS = TurnEventBus()
