#!/usr/bin/env python3
"""mock_pedal.py - an in-process fake pedal for testing the TRANSFER TOOLS.

Speaks the same SysEx dialect a real MS Plus pedal does - the file API
(`60 ..`), PC/editor mode, and the autosave parameter write - entirely in
memory, with FAULT INJECTION: it can go dead mid-transfer, swallow single
replies, report error statuses, or corrupt data, on command.

Why it exists: the one behaviour of a transfer tool that must never be
discovered on real hardware is what it does when the transfer FAILS. A pedal
was destroyed on 2026-07-17 by exactly that gap - the install path's
failure behaviour (blocking reads, no abort) had never been exercised
anywhere but on the pedal itself. `install_selftest.py` runs the real
engine (`file_session.py`) and the real installer (`pedal_diy.py`) against
this mock through every failure mode, so the failure paths are PROVEN
before any pedal sees them.

This file NEVER touches MIDI, opens no ports, and imports no MIDI library.
The reply bytes follow the frame shapes zoomzt2.py demonstrably parses
(status words in the last 5 bytes, `60 04` data frames with 7-bit packed
payload + CRC32 tail); where the real pedal's exact reply is unknown the
mock emits a generic acknowledgement, which is precisely as much as the
engine is allowed to rely on. It is a MODEL, not ground truth: passing
against it proves the tool's own logic, not the pedal's behaviour.
"""

import binascii

import file_session as fs

GENERIC_ACK = [0x52, 0x00, 0x6E, 0x60, 0x00, 0x00]
MODE_ACK = [0x52, 0x00, 0x6E, 0x00, 0x00]
ERR_NOT_FOUND = 0x10
ERR_BAD_CRC = 0x2B
READ_CHUNK = 512


class Fault:
    """One injected failure.

    kind:
      'dead'          - from the trigger on, the pedal stops processing AND
                        replying entirely (models the 2026-07-17 wedge).
      'mute'          - from the trigger on, it keeps processing but never
                        replies again (close commands still land).
      'drop'          - swallow the reply to `times` matching messages,
                        then behave normally (models a lost packet).
      'error_status'  - the next `times` status polls report `err`.
      'corrupt_read'  - corrupt the payload of `times` data frames (CRC
                        then fails on the host side).
      'corrupt_stored'- silently flip a byte when a matching file's upload
                        is committed (readback then differs byte-wise).
      'delete_ineffective' - ACK a delete normally but do not actually
                        remove the file (models a pedal that claims success
                        without doing it - the case a verifying re-check
                        after delete exists to catch).

    Trigger: the first message where all given matchers hold -
    sub (the `60 xx` sub-command byte), name (the file involved),
    block (0-based block index within that file's upload or download).
    """

    def __init__(self, kind, sub=None, name=None, block=None,
                 times=1, err=0x2A):
        self.kind = kind
        self.sub = sub
        self.name = name
        self.block = block
        self.times = times
        self.err = err

    def matches(self, sub, name, block):
        if self.times <= 0:
            return False
        if self.sub is not None and sub != self.sub:
            return False
        if self.name is not None and name != self.name:
            return False
        if self.block is not None and block != self.block:
            return False
        return True


class MockPedal:
    """The responder. Use `.send(payload)` / `.recv(timeout)` as a
    drop-in transport for file_session.FileSession, or `.as_mido_ports()`
    to look like a connected mido port pair for pedal-level code."""

    def __init__(self, files=None, autosave=1, disk_available=4 * 1024 * 1024,
                 wedged=False):
        self.files = dict(files or {})
        self.autosave = autosave
        self.autosave_stuck_on = False   # a pedal that refuses to turn it off
        self.disk_available = disk_available
        self.fault = None

        self.replies = []
        self.rx_log = []          # every payload received, even when dead
        self.rx_after_fault = []  # payloads received AFTER a dead/mute trip
        self.file_api_msgs = 0    # 60-family messages PROCESSED
        self.editor = 0
        # A pedal WEDGED mid-file-session (the pedal-#1 case, 2026-07-18;
        # this is a MODEL of the observed symptom, not verified pedal
        # internals): it is stuck in the file-transfer state machine and does
        # NOT answer mode commands (0x52/0x53) or status polls, so a rescue
        # that front-loads PC-mode hangs. It DOES process the file-session
        # close; the session-end (60 09) clears the wedge. Starts wedged as
        # if a prior transfer left it that way, and was left in PC mode.
        self._wedged = wedged
        self.pcmode = 1 if wedged else 0

        self._dead = False
        self._mute = False
        self._err = 0
        self._write_name = None
        self._write_buf = None
        self._write_block = 0
        self._read_name = None
        self._read_pos = 0
        self._read_block = 0
        self._pending_read = None
        self._check_name = None

    # ---- state inspection for tests

    def wedged(self):
        """True if a reboot NOW would meet an unfinished file write - the
        state that must never survive a tool exit."""
        return self._write_name is not None

    # ---- transport interface (same contract as MidoTransport)

    def send(self, payload):
        self.feed(payload)

    def recv(self, timeout):
        if self.replies:
            return bytes(self.replies.pop(0))
        return None               # instant timeout - keeps tests fast

    def as_mido_ports(self):
        mock = self

        class _Msg:
            type = "sysex"

            def __init__(self, payload):
                self.data = tuple(payload)

        class _In:
            def poll(self):
                r = mock.recv(0)
                return _Msg(r) if r is not None else None

            def close(self):
                pass

        class _Out:
            def send(self, msg):
                mock.feed(list(msg.data))

            def close(self):
                pass

        return _In(), _Out()

    # ---- internals

    def _emit(self, payload):
        if not self._mute:
            self.replies.append(list(payload))

    def _status_frame(self, name_ctx=None, block_ctx=None):
        err = self._err
        # error_status models a WRITE failure - only fire it while a file
        # is actually being written, so it can't be consumed by the
        # read-only status polls of a preceding download.
        if self._write_name is not None \
                and self._consume("error_status", None, name_ctx, block_ctx):
            err = self.fault.err
        tail = [err & 0x7F, (err >> 7) & 0x7F, (err >> 14) & 0x7F,
                (err >> 21) & 0x7F, (err >> 28) & 0x0F]
        return [0x52, 0x00, 0x6E, 0x60, 0x01, 0x00, 0x00, 0x00] + tail

    def _data_frame(self, chunk):
        packed = list(fs.pack7(chunk))
        if self._consume("corrupt_read", None, self._read_name, None):
            packed[0] ^= 0x01     # CRC will not match on the host side
        return ([0x52, 0x00, 0x6E, 0x60, 0x04, 0x00, 0x00, 0x00,
                 len(chunk) & 0x7F, (len(chunk) >> 7) & 0x7F]
                + packed + fs.crc32_tail(chunk))

    def _consume(self, kind, sub, name, block):
        """True (and uses up one occurrence) if the active fault of `kind`
        matches here."""
        f = self.fault
        if f is None or f.kind != kind:
            return False
        if not f.matches(sub, name, block):
            return False
        f.times -= 1
        return True

    def _name_from(self, payload, offset):
        name = []
        for b in payload[offset:]:
            if b == 0:
                break
            name.append(chr(b))
        return "".join(name)

    # ---- the pedal itself

    def feed(self, payload):
        payload = list(payload)
        self.rx_log.append(bytes(payload))
        if self._dead or self._mute:
            # everything the host sends AFTER the pedal wedged - this is how
            # the selftest proves the abort's close sequence still went out
            self.rx_after_fault.append(bytes(payload))
        if self._dead:
            return
        if len(payload) < 4 or tuple(payload[:3]) != (0x52, 0x00, 0x6E):
            return

        if self._wedged:
            # Stuck mid-file-session: answer ONLY the file-session close.
            # Receiving the session-end (60 09) clears the wedge; the close
            # half (60 21) is acknowledged; everything else (PC-mode, status
            # polls) gets NO reply - the observed symptom.
            if payload[3] == 0x60 and len(payload) > 4:
                if payload[4] == fs.SUB_CLOSE:
                    self._emit(GENERIC_ACK)
                elif payload[4] == fs.SUB_SESSION_END:
                    self._wedged = False
                    self._emit(GENERIC_ACK)
            return

        cmd = payload[3]
        if cmd == 0x52:                       # PC mode on
            self.pcmode = 1
            self._emit(MODE_ACK)
        elif cmd == 0x53:                     # PC mode off
            self.pcmode = 0
            self._emit(MODE_ACK)
        elif cmd == 0x50:                     # editor on
            self.editor = 1
            self._emit(MODE_ACK)
        elif cmd == 0x51:                     # editor off
            self.editor = 0
            self._emit(MODE_ACK)
        elif cmd == 0x64 and len(payload) >= 13 and payload[4] == 0x20 \
                and payload[5] == 0x00:       # parameter write
            slot, param = payload[6], payload[7]
            value = payload[8] | (payload[9] << 7)
            if slot == 0x64 and param == 0x0F:
                if not self.autosave_stuck_on:
                    self.autosave = value
                value = self.autosave
            # system parameter writes ACK unconditionally, echoing the
            # value actually applied
            self._emit([0x52, 0x00, 0x6E, 0x64, 0x20, 0x01, slot, param,
                        value & 0x7F, (value >> 7) & 0x7F, 0, 0, 0])
        elif cmd == 0x60:
            self._file_api(payload)

    def _file_api(self, payload):
        sub = payload[4] if len(payload) > 4 else None
        name_ctx = self._write_name or self._read_name
        block_ctx = (self._write_block if self._write_name is not None
                     else self._read_block)

        # dead/mute faults trigger BEFORE the message is processed - the
        # pedal wedges exactly at the failing operation
        f = self.fault
        if f is not None and f.kind in ("dead", "mute"):
            if sub == fs.SUB_DELETE:
                n = self._name_from(payload, 5)
            elif sub == fs.SUB_OPEN:
                n = self._name_from(payload, 15)
            elif sub == fs.SUB_CHECK_OPEN:
                n = self._name_from(payload, 7)
            else:
                n = name_ctx
            if f.matches(sub, n, block_ctx):
                f.times -= 1
                if f.kind == "dead":
                    self._dead = True
                    return
                self._mute = True

        self.file_api_msgs += 1
        drop = self._consume("drop", sub, name_ctx, block_ctx)
        queued_before = len(self.replies)

        if sub == fs.SUB_CHECK_OPEN:
            self._check_name = self._name_from(payload, 7)
            self._err = 0 if self._check_name in self.files else ERR_NOT_FOUND
            self._emit(GENERIC_ACK)
        elif sub == fs.SUB_CHECK_CLOSE:
            self._check_name = None
            self._emit(GENERIC_ACK)
        elif sub == fs.SUB_DELETE:
            name = self._name_from(payload, 5)
            if not self._consume("delete_ineffective", sub, name, block_ctx):
                self.files.pop(name, None)
            self._err = 0
            self._emit(GENERIC_ACK)
        elif sub == fs.SUB_OPEN:
            mode = payload[5]
            name = self._name_from(payload, 15)
            if mode == 0x01:
                self._write_name = name
                self._write_buf = bytearray()
                self._write_block = 0
            else:
                self._read_name = name
                self._read_pos = 0
                self._read_block = 0
                self._pending_read = None
            self._err = 0
            self._emit(GENERIC_ACK)
        elif sub == fs.SUB_WRITE_BLOCK:
            length = payload[10] | (payload[11] << 7)
            packed = payload[15:15 + length + length // 7 + 1]
            chunk = bytes(fs.unpack7(packed))
            crc = fs.tail_to_int(payload[-5:])
            if len(chunk) != length or \
                    (crc ^ 0xFFFFFFFF) != binascii.crc32(chunk):
                self._err = ERR_BAD_CRC
            elif self._write_name is None:
                self._err = ERR_BAD_CRC
            else:
                self._write_buf += chunk
                # corrupt the accumulating buffer ITSELF, so the flipped
                # byte persists through the rest of the write (a copy would
                # be overwritten by the next block's commit) and shows up in
                # the readback - which is exactly what must catch it
                if self._consume("corrupt_stored", None,
                                 self._write_name, self._write_block):
                    self._write_buf[-1] ^= 0x01
                # progressive commit: a stall mid-write leaves a TRUNCATED
                # file, exactly like flash storage would
                self.files[self._write_name] = bytes(self._write_buf)
                self._write_block += 1
                self._err = 0
            self._emit(GENERIC_ACK)
        elif sub == fs.SUB_READ_BLOCK:
            if self._read_name in self.files:
                data = self.files[self._read_name]
                chunk = data[self._read_pos:self._read_pos + READ_CHUNK]
                self._read_pos += len(chunk)
                self._read_block += 1
                self._pending_read = chunk        # b"" signals EOF
            else:
                self._pending_read = b""
            self._emit(GENERIC_ACK)
        elif sub == fs.SUB_STATUS:
            if self._pending_read is not None:
                chunk = self._pending_read
                self._pending_read = None
                if chunk:
                    self._emit(self._data_frame(chunk))
                else:
                    self._emit(self._status_frame(name_ctx, block_ctx))
            else:
                self._emit(self._status_frame(name_ctx, block_ctx))
        elif sub == fs.SUB_CLOSE:
            self._write_name = None
            self._write_buf = None
            self._read_name = None
            self._pending_read = None
            self._emit(GENERIC_ACK)
        elif sub == fs.SUB_SESSION_END:
            self._write_name = None
            self._write_buf = None
            self._read_name = None
            self._pending_read = None
            self._emit(GENERIC_ACK)
        elif sub == fs.SUB_DISK_USAGE:
            m, a = 8 * 1024 * 1024, self.disk_available
            self._emit([0x52, 0x00, 0x6E, 0x60, 0x04, 0x29, 0x01, 0x00,
                        0x08, 0x00]
                       + [m & 0x7F, (m >> 7) & 0x7F, (m >> 14) & 0x7F,
                          (m >> 21) & 0x7F, (m >> 28) & 0x0F]
                       + [a & 0x7F, (a >> 7) & 0x7F, (a >> 14) & 0x7F,
                          (a >> 21) & 0x7F, (a >> 28) & 0x0F])
        else:
            self._emit(GENERIC_ACK)

        if drop and len(self.replies) > queued_before:
            self.replies.pop()    # the reply was "lost on the wire"
