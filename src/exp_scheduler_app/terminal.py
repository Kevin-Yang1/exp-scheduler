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
TASK_TERMINAL_SNAPSHOT_BYTES = 2 * 1024 * 1024
TERMINAL_SCROLLBACK_LINES = 20000
TERMINAL_CHUNK_BYTES = 4096
TERMINAL_SUBSCRIBER_QUEUE_SIZE = 128
DEFAULT_TERMINAL_COLUMNS = 160
DEFAULT_TERMINAL_ROWS = 48

ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ANSI_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)
ANSI_ESCAPE_RE = re.compile(r"\x1b[@-_]")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
PROGRESS_RETURN_RE = re.compile(r"(?:\d{1,3}%\||\|\s*\d+/\d+|\bit/s\b|[KMGT]?B/s)")
PROGRESS_PERCENT_KEY_RE = re.compile(r"^(?P<key>.*?)(?:\d{1,3}%\|)")
PROGRESS_COUNT_KEY_RE = re.compile(r"^(?P<key>.*?)(?:\|\s*\d+/\d+)")


def encode_terminal_text(text: str) -> bytes:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", "\r\n").encode("utf-8", errors="replace")


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

    def subscribe(self, *, snapshot_bytes: int | None = TERMINAL_SNAPSHOT_BYTES) -> tuple[TerminalSubscriber, bytes]:
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


def read_bytes_tail(path: Path, *, tail_bytes: int | None) -> bytes:
    if not path.exists():
        return b""
    with path.open("rb") as fh:
        if tail_bytes is not None:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
        return fh.read()


def compact_progress_terminal_bytes(data: bytes) -> bytes:
    text = data.decode("utf-8", errors="replace")
    if not has_compactable_progress_returns(text) and not has_repeated_progress_lines(text):
        return data
    compacted_text = collapse_repeated_progress_lines(
        collapse_progress_carriage_returns(text)
    )
    compacted = encode_terminal_text(compacted_text)
    return compacted if len(compacted) < len(data) else data


def compact_progress_log_file(path: Path) -> bool:
    if not path.exists():
        return False
    original = path.read_bytes()
    compacted = compact_progress_terminal_bytes(original)
    if compacted == original:
        return False
    temp_path = path.with_name(f".{path.name}.compact-{os.getpid()}.tmp")
    try:
        temp_path.write_bytes(compacted)
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
    return True


def has_compactable_progress_returns(text: str) -> bool:
    for line in text.replace("\r\n", "\n").split("\n"):
        if "\r" not in line:
            continue
        segments = [segment for segment in line.split("\r") if segment]
        if len(segments) > 1 and any(PROGRESS_RETURN_RE.search(segment) for segment in segments):
            return True
    return False


def has_repeated_progress_lines(text: str) -> bool:
    previous_key: str | None = None
    repeat_count = 0
    for line in text.replace("\r\n", "\n").split("\n"):
        key = progress_line_key(line)
        if key is None:
            previous_key = None
            repeat_count = 0
            continue
        if key == previous_key:
            repeat_count += 1
            if repeat_count >= 3:
                return True
        else:
            previous_key = key
            repeat_count = 1
    return False


def progress_line_key(line: str) -> str | None:
    clean_line = ANSI_OSC_RE.sub("", line)
    clean_line = ANSI_CSI_RE.sub("", clean_line)
    clean_line = ANSI_ESCAPE_RE.sub("", clean_line)
    clean_line = CONTROL_CHAR_RE.sub("", clean_line).strip()
    if not clean_line or clean_line.startswith("[exp-scheduler]"):
        return None
    if not PROGRESS_RETURN_RE.search(clean_line):
        return None
    match = PROGRESS_PERCENT_KEY_RE.search(clean_line) or PROGRESS_COUNT_KEY_RE.search(clean_line)
    if match is None:
        return "__progress__"
    key = match.group("key").strip()
    return key or "__progress__"


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
    text = collapse_progress_carriage_returns(text)
    text = CONTROL_CHAR_RE.sub("", text)
    return text


def collapse_progress_carriage_returns(text: str) -> str:
    text = text.replace("\r\n", "\n")
    lines: list[str] = []
    for line in text.split("\n"):
        if "\r" not in line:
            lines.append(line)
            continue
        segments = [segment for segment in line.split("\r") if segment]
        if not segments:
            lines.append("")
            continue
        if any(PROGRESS_RETURN_RE.search(segment) for segment in segments):
            lines.append(segments[-1])
        else:
            lines.extend(segments)
    return "\n".join(lines)


def collapse_repeated_progress_lines(text: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    output: list[str] = []
    run_key: str | None = None
    run_lines: list[str] = []

    def flush_run() -> None:
        nonlocal run_key, run_lines
        if not run_lines:
            return
        if len(run_lines) >= 3:
            output.append(run_lines[-1])
        else:
            output.extend(run_lines)
        run_key = None
        run_lines = []

    for line in lines:
        key = progress_line_key(line)
        if key is None:
            flush_run()
            output.append(line)
            continue
        if run_lines and key == run_key:
            run_lines.append(line)
            continue
        flush_run()
        run_key = key
        run_lines = [line]

    flush_run()
    return "\n".join(output)


def read_text_tail(path: Path, *, tail_bytes: int | None) -> str:
    return normalize_terminal_bytes_to_text(read_bytes_tail(path, tail_bytes=tail_bytes))


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return normalize_terminal_bytes_to_text(path.read_bytes())
