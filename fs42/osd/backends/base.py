"""Interface every on-screen display backend implements."""


class OSDBackend:
    def tick(self):
        """Advance animations and redraw.  Called from the player's wait loops."""
        raise NotImplementedError

    def close(self):
        """Remove everything this backend drew."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class NullOSD(OSDBackend):
    """Used when no display is available, or the user turned the OSD off."""

    def tick(self):
        pass
