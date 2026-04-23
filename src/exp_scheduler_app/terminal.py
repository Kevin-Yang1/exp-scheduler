from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import fcntl
import os
from pathlib import Path
import re
import struct
import termios
from typing import BinaryIO


TERMINAL_SNAPSHOT_BYTES = 256 * 1024
TERMINAL_SCROLLBACK_LINES = 5000
TERMINAL_CHUNK_BYTES = 4096
TERMINAL_SUBSCRIBER_QUEUE_SIZE = 128
DEFAULT_TERMINAL_COLUMNS = 160
DEFAULT_TERMINAL_ROWS = 48

ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ANSI_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)
ANSI_ESCAPE_RE = re.compile(r"\x1b[@-_]")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass(slots=True, eq=False)
class TerminalSubscriber:
    chunk_queue: asyncio.Queue[bytes] = field(
        default_factory=lambda: asyncio.Queue(maxsize=TERMINAL_SUBSCRIBER_QUEUE_SIZE)
    )
    control_queue: asyncio.Queue[tuple[str, dict[str, object] | None]] = field(
        default_factory=asyncio.Queue
    )


@dataclass(slots=True, eq=False)
class TerminalSession:
    task_id: int
    master_fd: int
    log_path: Path
    log_file: BinaryIO
    cols: int = DEFAULT_TERMINAL_COLUMNS
    rows: int = DEFAULT_TERMINAL_ROWS
    subscribers: set[TerminalSubscriber] = field(default_factory=set)
    reader_task: asyncio.Task[None] | None = None
    closed: bool = False

    def subscribe(self, *, snapshot_bytes: int = TERMINAL_SNAPSHOT_BYTES) -> tuple[TerminalSubscriber, bytes]:
        snapshot = read_bytes_tail(self.log_path, tail_bytes=snapshot_bytes)
        subscriber = TerminalSubscriber()
        self.subscribers.add(subscriber)
        return subscriber, snapshot

    def unsubscribe(self, subscriber: TerminalSubscriber) -> None:
        self.subscribers.discard(subscriber)

    def resize(self, *, cols: int, rows: int) -> None:
        cols, rows = normalize_terminal_size(cols=cols, rows=rows)
        set_terminal_window_size(self.master_fd, cols=cols, rows=rows)
        self.cols = cols
        self.rows = rows

    def append_bytes(self, data: bytes) -> None:
        if self.closed or not data:
            return
        self.log_file.write(data)
        self.log_file.flush()
        for subscriber in list(self.subscribers):
            if subscriber.chunk_queue.full():
                subscriber.control_queue.put_nowait(("disconnect", None))
                self.subscribers.discard(subscriber)
                continue
            subscriber.chunk_queue.put_nowait(data)

    def publish_exit(self, payload: dict[str, object]) -> None:
        if self.closed:
            return
        self.closed = True
        for subscriber in list(self.subscribers):
            subscriber.control_queue.put_nowait(("exit", payload))
        self.subscribers.clear()


def read_bytes_tail(path: Path, *, tail_bytes: int) -> bytes:
    if not path.exists():
        return b""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - tail_bytes))
        return fh.read()


def normalize_terminal_size(*, cols: int, rows: int) -> tuple[int, int]:
    return max(2, int(cols)), max(1, int(rows))


def set_terminal_window_size(fd: int, *, cols: int, rows: int) -> tuple[int, int]:
    cols, rows = normalize_terminal_size(cols=cols, rows=rows)
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    return cols, rows


def normalize_terminal_bytes_to_text(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    text = ANSI_OSC_RE.sub("", text)
    text = ANSI_CSI_RE.sub("", text)
    text = ANSI_ESCAPE_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = CONTROL_CHAR_RE.sub("", text)
    return text


def read_text_tail(path: Path, *, tail_bytes: int) -> str:
    return normalize_terminal_bytes_to_text(read_bytes_tail(path, tail_bytes=tail_bytes))


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return normalize_terminal_bytes_to_text(path.read_bytes())
