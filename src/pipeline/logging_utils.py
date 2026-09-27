import logging
import sys
from pathlib import Path
from typing import Optional, Union


LogPath = Union[str, Path]


def setup_logger(
    name: str,
    log_level: str = "INFO",
    log_file: Optional[LogPath] = None,
) -> logging.Logger:
    """Configure a named logger with console and optional file output."""
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    # Configure this logger deterministically. logger.hasHandlers() also sees
    # root handlers and could incorrectly skip installation of our handlers.
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if log_file:
        log_path = Path(log_file)
        # Path("pipeline.log").parent is ".", so filename-only paths work.
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path)
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
