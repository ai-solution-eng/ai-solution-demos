"""Minimal threaded SMTP sink for e2e (captures messages; no relay)."""
from __future__ import annotations

import socket
import threading
from dataclasses import dataclass, field


@dataclass
class CapturedMail:
    from_: str
    to: str
    subject: str
    body: str


@dataclass
class SmtpSink:
    host: str = "127.0.0.1"
    port: int = 1025
    mails: list[CapturedMail] = field(default_factory=list)
    _server: socket.socket | None = None
    _thread: threading.Thread | None = None
    _running: bool = False

    def start(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.port))
        self._server.listen(8)
        self._server.settimeout(0.5)
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass

    def _serve(self) -> None:
        while self._running:
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        data_buf = ""
        try:
            buf = ""
            conn.sendall(b"220 sink ESMTP\r\n")
            mail_from = ""
            rcpt = []
            in_data = False
            while self._running:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", "replace")
                while "\r\n" in buf:
                    line, buf = buf.split("\r\n", 1)
                    if in_data:
                        if line == ".":
                            in_data = False
                            body = data_buf
                            self.mails.append(_make_mail(mail_from, rcpt, body))
                            data_buf = ""
                            conn.sendall(b"250 OK queued\r\n")
                        else:
                            # RFC5321 dot-stuffing: a leading "." is literal data
                            # (base64 content frequently produces such lines).
                            data_buf += (line[1:] if line.startswith(".") else line) + "\n"
                        continue
                    up = line.upper()
                    if up.startswith("EHLO") or up.startswith("HELO"):
                        conn.sendall(b"250 sink\r\n")
                    elif up.startswith("MAIL FROM"):
                        mail_from = line.split(":", 1)[1].strip().strip("<>")
                        conn.sendall(b"250 OK\r\n")
                    elif up.startswith("RCPT TO"):
                        rcpt.append(line.split(":", 1)[1].strip().strip("<>"))
                        conn.sendall(b"250 OK\r\n")
                    elif up.startswith("DATA"):
                        in_data = True
                        self._data_buf = ""
                        conn.sendall(b"354 go\r\n")
                    elif up.startswith("QUIT"):
                        conn.sendall(b"221 bye\r\n")
                        return
                    elif up.startswith("RSET"):
                        mail_from, rcpt = "", []
                        conn.sendall(b"250 OK\r\n")
                    else:
                        conn.sendall(b"250 OK\r\n")
        except (OSError, ConnectionResetError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    _data_buf: str = ""


def _decode_header(value: str) -> str:
    """Decode an RFC2047-encoded header (=?utf-8?q?...?=) back to text."""
    from email.header import decode_header
    out = []
    for part, enc in decode_header(value or ""):
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", "replace"))
        else:
            out.append(part)
    return "".join(out)


def _subject(body: str) -> str:
    for line in body.split("\n"):
        if line.lower().startswith("subject:"):
            return _decode_header(line.split(":", 1)[1].strip())
    return ""


def _make_mail(mail_from: str, rcpt: list[str], raw: str) -> CapturedMail:
    """Decode the captured RFC822 message into a CapturedMail whose `body` is the
    readable (plain + html) text and `subject` is the full (possibly wrapped) header.
    Mirrors what a mail client presents, so e2e checks can search readable content."""
    from email import message_from_string
    try:
        msg = message_from_string(raw)
    except Exception:  # noqa: BLE001
        return CapturedMail(_decode_header(mail_from), _decode_header("; ".join(rcpt)),
                            _subject(raw), raw)
    subject = str(msg.get("Subject", "") or "")
    decoded_subject = _decode_header(subject)

    def _part_text(part) -> str:
        payload: bytes | None
        try:
            payload = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001
            payload = None
        if payload is None:
            raw_payload = part.get_payload()
            payload = raw_payload.encode() if isinstance(raw_payload, str) else b""
        enc = part.get_content_charset() or "utf-8"
        return payload.decode(enc, "replace")

    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_type() in ("text/plain", "text/html"):
                parts.append(_part_text(part))
    else:
        parts.append(_part_text(msg))
    return CapturedMail(_decode_header(mail_from), _decode_header("; ".join(rcpt)),
                        decoded_subject, "\n".join(parts))
