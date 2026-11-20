import asyncio
import socket
import sys
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from backend import launch


def test_launcher_closes_an_accepted_connection_that_registers_during_shutdown(monkeypatch):
    pending, peer = socket.socketpair()
    closed_on_registration = []

    class HandoffLoop(asyncio.SelectorEventLoop):
        async def create_server(self, protocol_factory, *args, **kwargs):
            listener = await super().create_server(protocol_factory, *args, **kwargs)
            close_listener = listener.close

            async def register_accepted_peer():
                transport, _ = await self.connect_accepted_socket(protocol_factory, pending)
                closed_on_registration.append(transport.is_closing())
                transport.close()

            def close():
                if listener.is_serving():
                    # An accept is already queued when the server stops polling the listener.
                    self.create_task(register_accepted_peer())
                close_listener()

            listener.close = close
            return listener

    monkeypatch.setattr(launch.asyncio, "SelectorEventLoop", HandoffLoop)
    stopping = Event()
    with pending, peer, launch.bind_browser_listener(0) as (listener, _):
        assert launch.serve(FastAPI(), listener, stopping, True, stopping.set)
    assert closed_on_registration == [True]


def test_frozen_service_requires_explicit_writable_data_root(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["zhixue-service"])
    def should_not_start(*args):
        pytest.fail("A frozen service cannot use its temporary resource directory for user data")
    monkeypatch.setattr(launch, "run", should_not_start)
    with pytest.raises(SystemExit) as error:
        launch.main()
    assert error.value.code == 2


def test_worker_launch_uses_the_same_service_binary_when_frozen(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert launch.worker_command(tmp_path) == [sys.executable, "--worker", str(tmp_path)]
    monkeypatch.setattr(sys, "frozen", False)
    assert launch.worker_command(tmp_path) == [sys.executable, "-m", "backend.worker", str(tmp_path)]


@pytest.mark.parametrize(("requested", "expected", "attempts"), [(0, 15321, 2), (3220, 3220, 1)])
def test_local_launcher_announces_a_browser_compatible_port(tmp_path, monkeypatch, capsys, requested, expected, attempts):
    assigned = iter([6000, 15321])
    released = []

    class Listener:
        def __enter__(self):
            return self

        def __exit__(self, *_error):
            released.append(self.port)

        def setsockopt(self, *_args):
            pass

        def bind(self, address):
            host, port = address
            assert host == "127.0.0.1" and port == requested
            self.port = port or next(assigned)

        def getsockname(self):
            return "127.0.0.1", self.port

    def serve(_app, listener, _stopped, _stdio, ready):
        ready()
        assert listener.getsockname()[1] == expected
        return True

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").touch()
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    monkeypatch.setattr(launch, "socket", SimpleNamespace(socket=Listener, SOL_SOCKET=1, SO_EXCLUSIVEADDRUSE=2))
    monkeypatch.setattr(launch, "serve", serve)
    monkeypatch.setattr(launch, "watch_parent_disconnect", lambda _callback: None)

    assert launch.run(tmp_path / "data", requested, stdio=True, open_browser=False) == 0
    assert f"http://127.0.0.1:{expected}/" in capsys.readouterr().out
    assert len(released) == attempts and released[-1] == expected
