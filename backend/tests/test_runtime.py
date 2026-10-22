"""Database work must leave the HTTP/WebSocket event loop responsive."""
import asyncio
import threading

from app import main


def test_quote_processing_keeps_event_loop_responsive_and_serializes_writes(monkeypatch):
    async def run():
        release = threading.Event()
        started = threading.Event()
        calls = []
        thread_ids = []
        def slow_quote(quote):
            calls.append(quote["id"])
            thread_ids.append(threading.get_ident())
            started.set()
            assert release.wait(3)
        monkeypatch.setattr(main, "write_lock", asyncio.Lock())
        monkeypatch.setattr(main, "process_quote_receipt", slow_quote)
        one = asyncio.create_task(main.quote_received({"id": 1}))
        two = asyncio.create_task(main.quote_received({"id": 2}))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(.005)
        try:
            assert started.is_set()
            assert calls == [1]
            assert not one.done() and not two.done()
            assert thread_ids[0] != threading.get_ident()
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(one, two), 3)
        assert calls == [1, 2]
    asyncio.run(run())


def test_socket_dashboard_work_does_not_block_event_loop(monkeypatch):
    async def run():
        release = threading.Event()
        started = threading.Event()
        def slow_dashboard(*args):
            started.set()
            assert release.wait(3)
            return None
        class Socket:
            headers = {}
            query_params = {}
            accepted = False
            async def accept(self): self.accepted = True
            async def close(self, code): self.code = code
        monkeypatch.setattr(main, "write_lock", asyncio.Lock())
        monkeypatch.setattr(main, "socket_identity", lambda *args: (main.time.time() + 30, "session"))
        monkeypatch.setattr(main, "feed_manager", None)
        monkeypatch.setattr(main, "socket_dashboard", slow_dashboard)
        socket = Socket()
        task = asyncio.create_task(main.websocket(socket))
        for _ in range(100):
            if started.is_set(): break
            await asyncio.sleep(.005)
        try:
            assert started.is_set() and socket.accepted and not task.done()
        finally:
            release.set()
            await asyncio.wait_for(task, 3)
    asyncio.run(run())
