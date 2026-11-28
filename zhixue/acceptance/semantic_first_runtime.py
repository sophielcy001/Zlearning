"""Run the real API with a held first commit and without fetching embedding assets."""
from pathlib import Path
import sys
from time import monotonic, sleep

from backend import launch, library_semantic, worker


def main():
    if sys.argv[1] == "--semantic-fixture-worker":
        library_semantic.SemanticIndex.step = lambda self, stopped: False
        worker.main(sys.argv[2:])
        return

    directory = Path(__file__).parent
    checkpoint = directory / ".semantic-first-before-commit"
    release = directory / ".semantic-first-release"
    request_index = library_semantic.request_index

    def hold_first_commit(db):
        request_index(db)
        if release.exists():
            return
        checkpoint.touch()
        deadline = monotonic() + 12
        while not release.exists():
            if monotonic() >= deadline:
                raise TimeoutError("Acceptance did not release the first semantic commit")
            sleep(.03)

    library_semantic.request_index = hold_first_commit
    launch.worker_command = lambda folder: [
        sys.executable, "-m", "acceptance_semantic_first_runtime", "--semantic-fixture-worker", str(folder),
    ]
    return launch.main()


if __name__ == "__main__":
    raise SystemExit(main())
