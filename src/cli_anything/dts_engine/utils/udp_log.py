"""UDP log listener for EngineWorker progress.

The managed shell binds a UDP socket (default port 9527, via NetUtils.GetAPort)
and passes it to every worker invocation as ``-udp=<port>``. The worker streams
log lines back over that socket. This is the only progress channel that does not
require scraping the hidden console window.

Log lines look like::

    [ERROR]2026-08-05 09:58:31 发布失败，RetCode:201,message:影像路径为空

Encoding is the Windows ANSI codepage (GBK on Chinese systems), not UTF-8.
"""
from __future__ import annotations

import re
import socket
import threading

#: Decoding order. Engine emits GBK on zh-CN hosts; latin-1 never fails.
ENCODINGS = ("gbk", "utf-8", "latin-1")

#: Engine prefixes datagrams with a control byte (observed: 0x01) before the
#: bracketed level. Strip leading control characters before matching.
_LEAD_CONTROL = re.compile(r"^[\x00-\x08\x0b\x0c\x0e-\x1f]+")

_LINE = re.compile(
    r"^\[(?P<level>[A-Z]+)\]"
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})?\s*"
    r"(?P<body>.*)$"
)
_RETCODE = re.compile(r"RetCode:(?P<code>-?\d+)")


def decode(raw: bytes) -> str:
    """Decode a datagram, stripping Engine's leading control byte."""
    for enc in ENCODINGS:
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("latin-1", errors="replace")
    return _LEAD_CONTROL.sub("", text).strip()


def parse_line(line: str) -> dict:
    """Split a log line into level, timestamp, body, and any RetCode."""
    line = _LEAD_CONTROL.sub("", line).strip()
    m = _LINE.match(line)
    out = {"raw": line, "level": "INFO", "timestamp": None, "body": line, "retcode": None}
    if m:
        out["level"] = m.group("level")
        out["timestamp"] = m.group("ts")
        out["body"] = (m.group("body") or "").strip()
    rc = _RETCODE.search(line)
    if rc:
        out["retcode"] = int(rc.group("code"))
    return out


class UdpLogListener:
    """Bind an ephemeral UDP port and collect Engine log lines.

    Use as a context manager; read ``.port`` to build the ``-udp=`` flag::

        with UdpLogListener() as log:
            run([..., f"-udp={log.port}"])
        log.lines
    """

    def __init__(self, port: int = 0, on_line=None) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("0.0.0.0", port))
        self._sock.settimeout(0.5)
        self.port: int = self._sock.getsockname()[1]
        self.records: list[dict] = []
        self._on_line = on_line
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def lines(self) -> list[str]:
        return [r["raw"] for r in self.records]

    @property
    def retcode(self) -> int | None:
        """Last RetCode seen in the stream, if any."""
        for rec in reversed(self.records):
            if rec["retcode"] is not None:
                return rec["retcode"]
        return None

    def errors(self) -> list[dict]:
        return [r for r in self.records if r["level"] == "ERROR"]

    def start(self) -> UdpLogListener:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _ = self._sock.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                break
            rec = parse_line(decode(raw))
            self.records.append(rec)
            if self._on_line:
                self._on_line(rec)

    def stop(self, drain: float = 1.5) -> None:
        """Stop listening, after waiting ``drain`` seconds for trailing packets."""
        if drain:
            self._stop.wait(drain)
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._sock.close()

    def __enter__(self) -> UdpLogListener:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
