"""Isolated acceptance entry that stops the real Worker at a controlled dispatch boundary."""
import os
from pathlib import Path
import sys
from time import monotonic, sleep

from backend import launch, worker


def interrupt(folder):
    (folder / ".acceptance-draft-checkpoint").touch()
    release = folder / ".acceptance-stop-worker"
    deadline = monotonic() + 30
    while not release.exists():
        if monotonic() >= deadline:
            raise TimeoutError("Acceptance did not release the Worker checkpoint")
        sleep(0.05)
    os._exit(83)


def stop_at_draft(folder, boundary):
    dispatch = worker.dispatch

    def dispatch_with_checkpoint(engine, claim, *, vault, transport):
        if claim.kind == "draft":
            if boundary == "reserved":
                interrupt(folder)

            def stop_after_response(address, headers, payload):
                transport(address, headers, payload)
                interrupt(folder)

            dispatch(engine, claim, vault=vault, transport=stop_after_response)
        else:
            dispatch(engine, claim, vault=vault, transport=transport)

    worker.dispatch = dispatch_with_checkpoint
    worker.main([str(folder)])


def main():
    boundary, *arguments = sys.argv[1:]
    if boundary not in {"reserved", "sending"}:
        raise ValueError("Invalid Worker restart boundary")
    if arguments[0] == "--checkpoint-worker":
        stop_at_draft(Path(arguments[1]), boundary)
        return
    launch.worker_command = lambda folder: [
        sys.executable, "-m", "acceptance_restart_runtime", boundary, "--checkpoint-worker", str(folder),
    ]
    sys.argv[1:] = arguments
    return launch.main()


if __name__ == "__main__":
    raise SystemExit(main())
