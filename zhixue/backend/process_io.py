import os
import sys
from collections.abc import Callable
from threading import Thread


def watch_parent_disconnect(on_disconnect: Callable[[], None]) -> None:
    def wait_for_eof():
        # The service can exit while its parent still holds the pipe open.
        # A blocked buffered reader would prevent Python from finalizing stdin.
        try:
            if sys.stdin is not None:
                descriptor = sys.stdin.fileno()
                while os.read(descriptor, 4096):
                    pass
        finally:
            on_disconnect()

    Thread(target=wait_for_eof, name="parent-disconnect", daemon=True).start()
