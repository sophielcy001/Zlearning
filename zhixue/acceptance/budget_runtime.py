"""Give the isolated launcher and generation Worker the same test clock."""

from datetime import UTC, datetime
from functools import partial
from pathlib import Path
import sys

from backend import launch, worker
from backend.project_activity import SHANGHAI


def main() -> int:
    role, clock_path, *arguments = sys.argv[1:]
    clock = Path(clock_path)

    def now():
        stamp = max(clock.iterdir()).name
        return datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)

    if role == "worker":
        worker.utc_now = now
        worker.dispatch = partial(worker.dispatch, now=now)
        worker.main(arguments)
        return 0

    launch.create_app = partial(
        launch.create_app, today=lambda: now().astimezone(SHANGHAI).date(),
        now=lambda: now().replace(tzinfo=None),
    )
    launch.worker_command = lambda folder: [
        sys.executable, "-m", "acceptance_budget", "worker", str(clock), str(folder),
    ]
    sys.argv[1:] = arguments
    return launch.main()


if __name__ == "__main__":
    raise SystemExit(main())
