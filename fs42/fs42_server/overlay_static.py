"""StaticFiles that searches several directories in order.

Starlette's StaticFiles serves exactly one directory.  A frozen build needs
two: the read-only bundled asset tree, and a writable overlay in the user's
data directory where custom themes, bump videos and PPV art live.  The first
directory that contains a requested name wins, so a user file shadows the
bundled one without either being copied.
"""

import os

from starlette.staticfiles import StaticFiles


class OverlayStaticFiles(StaticFiles):
    def __init__(self, directories, **kwargs):
        self._directories = [str(d) for d in directories if d is not None]
        kwargs.pop("directory", None)
        super().__init__(directory=None, **kwargs)

    def get_directories(self, directory=None, packages=None):
        # Called by StaticFiles.__init__ before our attribute exists on some
        # Starlette versions, so tolerate that and fill in later.
        return list(getattr(self, "_directories", []))

    @property
    def all_directories(self):
        return list(self._directories)

    @all_directories.setter
    def all_directories(self, value):
        # StaticFiles.__init__ assigns the result of get_directories() here.
        if value:
            self._directories = [str(v) for v in value]

    def lookup_path(self, path):
        for directory in self._directories:
            joined = os.path.realpath(os.path.join(directory, path))
            root = os.path.realpath(directory)
            if os.path.commonpath([joined, root]) != root:
                # Traversal attempt - skip this root rather than serving it.
                continue
            try:
                stat_result = os.stat(joined)
            except (FileNotFoundError, NotADirectoryError):
                continue
            return joined, stat_result
        return "", None
