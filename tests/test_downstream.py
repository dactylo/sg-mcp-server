import errno
import io
import os
import sys

import pytest

from sg_mcp_server import _downstream
from sg_mcp_server._downstream import is_downstream_closure, silence_descriptor, write_stdout


class _FailingStream(io.StringIO):
    def __init__(self, error: OSError, *, fail_on: str) -> None:
        super().__init__()
        self.error = error
        self.fail_on = fail_on

    def flush(self) -> None:
        if self.fail_on == 'flush':
            raise self.error
        super().flush()

    def write(self, text: str) -> int:
        if self.fail_on == 'write':
            raise self.error
        return super().write(text)


class _FakeOs:
    O_WRONLY = os.O_WRONLY
    devnull = os.devnull

    def __init__(self, *, redirect_error: OSError | None) -> None:
        self.events: list[tuple[str, int]] = []
        self.redirect_error = redirect_error

    def close(self, fd: int) -> None:
        self.events.append(('close', fd))

    def dup2(self, source: int, _target: int) -> None:
        self.events.append(('dup2', source))
        if self.redirect_error is not None:
            raise self.redirect_error

    def open(self, _path: str, _flags: int) -> int:
        return 37


class _DescriptorStream(io.StringIO):
    def fileno(self) -> int:
        return 1


@pytest.mark.parametrize('windows', [False, True])
def test_broken_pipe_is_closure_everywhere(monkeypatch: pytest.MonkeyPatch, windows: bool) -> None:
    monkeypatch.setattr(_downstream, '_WINDOWS', windows)
    assert is_downstream_closure(BrokenPipeError())


@pytest.mark.parametrize('windows', [False, True])
def test_einval_is_closure_only_on_windows(monkeypatch: pytest.MonkeyPatch, windows: bool) -> None:
    monkeypatch.setattr(_downstream, '_WINDOWS', windows)
    error = OSError(errno.EINVAL, 'Invalid argument')
    assert is_downstream_closure(error) is windows


def test_silence_descriptor_closes_devnull_and_propagates_redirect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOs(redirect_error=OSError('injected redirect failure'))
    monkeypatch.setattr(_downstream, 'os', fake)

    with pytest.raises(OSError, match='injected redirect failure'):
        silence_descriptor(_DescriptorStream())
    assert fake.events == [('dup2', 37), ('close', 37)]


def test_silence_descriptor_leaves_a_stream_without_descriptor_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOs(redirect_error=None)
    monkeypatch.setattr(_downstream, 'os', fake)
    stream = io.StringIO()

    assert silence_descriptor(stream) is None
    assert fake.events == []
    stream.write('kept')
    assert stream.getvalue() == 'kept'


def test_silence_descriptor_redirects_a_descriptor(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeOs(redirect_error=None)
    monkeypatch.setattr(_downstream, 'os', fake)

    assert silence_descriptor(_DescriptorStream()) is None
    assert fake.events == [('dup2', 37), ('close', 37)]


def test_unrelated_errno_is_not_closure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_downstream, '_WINDOWS', True)
    assert not is_downstream_closure(OSError(errno.EBADF, 'bad'))


@pytest.mark.parametrize('fail_on', ['write', 'flush'])
@pytest.mark.parametrize(
    ('error', 'windows'),
    [(BrokenPipeError(), False), (OSError(errno.EINVAL, 'closed'), True)],
)
def test_write_stdout_silences_stdout_after_closure(
    monkeypatch: pytest.MonkeyPatch, fail_on: str, error: OSError, windows: bool
) -> None:
    silenced: list[object] = []
    stream = _FailingStream(error, fail_on=fail_on)
    monkeypatch.setattr(_downstream, '_WINDOWS', windows)
    monkeypatch.setattr(_downstream, 'silence_descriptor', silenced.append)
    monkeypatch.setattr(sys, 'stdout', stream)

    write_stdout('rendered text')

    assert silenced == [stream]
    assert stream.getvalue() == ('' if fail_on == 'write' else 'rendered text')


@pytest.mark.parametrize('fail_on', ['write', 'flush'])
def test_write_stdout_propagates_other_errors(
    monkeypatch: pytest.MonkeyPatch, fail_on: str
) -> None:
    error = OSError(errno.EIO, 'I/O error')
    silenced: list[object] = []
    stream = _FailingStream(error, fail_on=fail_on)
    monkeypatch.setattr(_downstream, '_WINDOWS', True)
    monkeypatch.setattr(_downstream, 'silence_descriptor', silenced.append)
    monkeypatch.setattr(sys, 'stdout', stream)

    with pytest.raises(OSError, match='I/O error') as raised:
        write_stdout('rendered text')

    assert raised.value is error
    assert silenced == []


def test_write_stdout_writes_and_flushes(monkeypatch: pytest.MonkeyPatch) -> None:
    class _RecordingStream(io.StringIO):
        flushed = False

        def flush(self) -> None:
            self.flushed = True
            super().flush()

    stream = _RecordingStream()
    monkeypatch.setattr(sys, 'stdout', stream)

    write_stdout('rendered text')

    assert stream.getvalue() == 'rendered text'
    assert stream.flushed
