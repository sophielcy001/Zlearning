from pathlib import Path
from time import sleep


def replace_report(temporary: Path, destination: Path) -> None:
    """Publish a closed report, allowing brief Windows file contention to clear."""
    for delay in (0.02, 0.04, 0.08, 0.16, 0.32, None):
        try:
            temporary.replace(destination)
            return
        except PermissionError as error:
            # SMB may report access denied while a closed file is still busy.
            if delay is None or getattr(error, "winerror", None) not in {5, 32, 33}:
                raise
            sleep(delay)

