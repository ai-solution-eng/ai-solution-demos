import logging

# Add VERBOSE logging level
VERBOSE_LEVEL_NUM = 15
logging.addLevelName(VERBOSE_LEVEL_NUM, "VERBOSE")
logging.VERBOSE = VERBOSE_LEVEL_NUM  # type: ignore[attr-defined]

__all__ = ["logging", "setup_logger"]


def verbose(self, message, *args, **kws):
    if self.isEnabledFor(VERBOSE_LEVEL_NUM):
        self._log(VERBOSE_LEVEL_NUM, message, args, **kws)


logging.Logger.verbose = verbose  # type: ignore[attr-defined]


def setup_logger(string_logger: bool = True, log_path: str | None = None, level: str = "INFO") -> None:
    """
    Builds a logger object with optional logging to a file.
    """

    handler: list[logging.Handler] = []
    if log_path is not None:
        handler.append(logging.FileHandler(log_path))

    if string_logger:
        handler.append(logging.StreamHandler())

    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handler,
        force=True,
    )
