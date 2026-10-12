"""Write standard output and absorb closure by its downstream reader."""

import errno
import os
import sys
from typing import TextIO

_WINDOWS = sys.platform == 'win32'


def is_downstream_closure(error: OSError) -> bool:
    """Report whether the reader closed an output stream.

    Args:
        error: Error raised by a write or flush of an output stream.

    Returns:
        Whether the error means the downstream reader is gone. On Windows,
        CPython can report a closed pipe as `EINVAL`.
    """
    return isinstance(error, BrokenPipeError) or (_WINDOWS and error.errno == errno.EINVAL)


def silence_descriptor(stream: TextIO) -> None:
    """Point a stream's descriptor at the null device when it has one.

    Args:
        stream: Stream whose descriptor the interpreter flushes at shutdown.

    Raises:
        OSError: If redirecting an existing descriptor fails.
    """
    try:
        target = stream.fileno()
    except (OSError, ValueError):
        return
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, target)
    finally:
        os.close(devnull)


def write_stdout(text: str) -> None:
    """Write and flush text, absorbing closure by the downstream reader.

    Args:
        text: Complete command output to write.

    Raises:
        OSError: If writing, flushing, or redirecting fails for another reason.
    """
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except OSError as error:
        if not is_downstream_closure(error):
            raise
        silence_descriptor(sys.stdout)
