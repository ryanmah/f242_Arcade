"""``python -m fs42.app`` - how the supervisor re-execs itself from source."""

import multiprocessing
import sys


def _run():
    from fs42.app.cli import dispatch

    sys.exit(dispatch(sys.argv[1:]))


if __name__ == "__main__":
    multiprocessing.freeze_support()
    _run()
