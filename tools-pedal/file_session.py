#!/usr/bin/env python3
"""file_session.py - a fail-safe engine for the pedal's SysEx file API.

This module exists because of a destroyed pedal: on 2026-07-17 an install
hung mid-transfer on a blocking MIDI read with no timeout, and the
power-cycle that followed left the pedal permanently unable to boot, its
boot code hanging on the half-written internal filesystem. Full account:
SAFETY.md "An interrupted file transfer can brick".

So this engine treats every pedal file WRITE as brick-capable until it has
completed AND been read back AND the file session is closed, and it enforces
that with four mechanical layers (not procedures - code):

  1. EVERY receive has a TIMEOUT. A lost reply becomes a fast, clean error,
     never a hang. One lost reply is tolerated (a read-only status poll is
     sent and its reply accepted instead); two in a row abort the transfer.
  2. ANY failure triggers a CLEAN ABORT: the file-session close sequence
     (`60 21` + `60 09`) is sent before the tool exits, so the pedal is never
     abandoned inside an open session. After the abort the engine VERIFIES
     the pedal's state (reads the effect list back) whenever it can.
  3. Every uploaded file is READ BACK and byte-compared before it counts.
  4. The effect list FLST_SEQ.ZT2 - the one file the pedal's boot depends
     on - is rewritten through `replace_flst()`, which keeps the danger
     window as small and as guarded as the protocol allows (see below), and
     retries with the known-good bytes in hand until the readback verifies.

If, after all that, the engine still cannot verify the pedal is safe, it
prints an impossible-to-miss DO-NOT-POWER-CYCLE banner and exits with a
distinct code. A pedal wedged mid-transfer is RECOVERABLE while it stays
powered; the reboot is what commits the corruption.

Why `replace_flst` is not a true atomic swap: the known file API (the exact
command set zoomzt2.py uses, which is all anyone has confirmed on hardware)
has NO rename/swap primitive, and its upload sequence starts with `60 24` -
the same command that DELETES the named entry - so an in-place rewrite
unavoidably passes through a moment where the old file is gone and the new
one incomplete. Writing to a temporary name first would not close that
window (the temp file cannot be renamed into place) and would leave a novel
file type on the pedal if the session died, which is its own unproven risk.
The engine therefore keeps the in-place rewrite but (a) proves the link and
the readback path seconds before entering the window, (b) confirms free
space first (`60 29`), (c) holds the window only for the ~2 s the streaming
takes, and (d) retries the whole write with the good bytes on any failure.
If a rename primitive is ever discovered in the protocol, upgrade this.

The wire vocabulary is EXACTLY the one zoomzt2.py implements (SAFETY.md:
"stick to the exact sequences" - the file API is marked "Show caution!").
The only additions are extra read-only `60 05` status polls, which zoomzt2
itself uses freely. Transport is injected, so the whole engine - including
every failure path - is exercised against `mock_pedal.py` without a pedal
attached (`install_selftest.py`); the failure behaviour of this code must
NEVER get its first test on real hardware.

This module has no CLI and never opens a MIDI port itself; `pedal_diy.py`
owns the connection and the safety policy around it.
"""

import binascii
import time

ZOOM_HDR = (0x52, 0x00, 0x6E)

# 0x60 file-API sub-commands - exactly zoomzt2.py's vocabulary.
SUB_STATUS = 0x05        # read-only status/result poll
SUB_SESSION_END = 0x09   # second half of the close sequence
SUB_OPEN = 0x20          # + mode byte: 0x01 = write, 0x02 = read
SUB_CLOSE = 0x21         # first half of the close sequence
SUB_READ_BLOCK = 0x22
SUB_WRITE_BLOCK = 0x23
SUB_DELETE = 0x24        # deletes/replaces the named entry; zoomzt2's
                         # file_upload sends it before every write
SUB_CHECK_OPEN = 0x25    # file presence check, closed by...
SUB_CHECK_CLOSE = 0x27
SUB_DISK_USAGE = 0x29
REPLY_DATA = 0x04        # reply[4] == 0x04 marks a data-carrying reply

FLST_NAME = "FLST_SEQ.ZT2"
BLOCK = 512                  # upload block size, as zoomzt2 uses
RECV_TIMEOUT = 5.0           # normal replies arrive in milliseconds
ABORT_TIMEOUT = 2.0          # per-receive budget during a clean abort
FLST_WRITE_ATTEMPTS = 3      # retries of the critical effect-list rewrite
DISK_MARGIN = 4096           # required free bytes beyond the FLST size


class TransferError(Exception):
    """Any protocol-level failure. The engine converts it into a clean
    abort; it never escapes to the caller raw."""


class TransferTimeout(TransferError):
    """A receive timed out (after the one tolerated recovery poll)."""


class FlstBaselineChanged(TransferError):
    """The effect list on the pedal no longer matches the copy this
    operation was computed from (something else wrote it). Carries the
    pedal's actual current list so the unwind can verify against reality
    instead of the stale expectation."""

    def __init__(self, msg, current):
        super().__init__(msg)
        self.current = current


class TransferAborted(Exception):
    """The operation failed BUT the pedal was verified back in a safe
    state: file session closed, effect list confirmed byte-identical to
    the expected baseline. Nothing needs rescuing; retry when ready."""


class PedalUnresolved(Exception):
    """The operation failed AND the engine could NOT verify the pedal is
    safe. The DO-NOT-POWER-CYCLE banner has been printed by the time this
    reaches the caller. The pedal must stay powered until a rescue
    (clean close + effect-list verification) succeeds."""


# ---------------------------------------------------------------- codecs

def pack7(data):
    """8-bit -> 7-bit MIDI packing, byte-equivalent to zoomzt2.pack():
    groups of 7 data bytes preceded by one byte carrying their MSBs."""
    packet = bytearray()
    encode = bytearray(b"\x00")
    for byte in data:
        encode[0] = encode[0] + ((byte & 0x80) >> len(encode))
        encode.append(byte & 0x7F)
        if len(encode) > 7:
            packet += encode
            encode = bytearray(b"\x00")
    if len(encode) > 1:
        packet += encode
    return packet


def unpack7(packet):
    """7-bit -> 8-bit, byte-equivalent to zoomzt2.unpack()."""
    data = bytearray()
    loop = -1
    hibits = 0
    for byte in packet:
        if loop != -1:
            if hibits & (2 ** loop):
                data.append(128 + byte)
            else:
                data.append(byte)
            loop -= 1
        else:
            hibits = byte
            loop = 6
    return data


def crc32_tail(block):
    """The 5-byte 7-bit-LE inverted CRC32 that terminates a data block,
    exactly as zoomzt2 computes it."""
    crc = binascii.crc32(bytes(block)) ^ 0xFFFFFFFF
    return [crc & 0x7F, (crc >> 7) & 0x7F, (crc >> 14) & 0x7F,
            (crc >> 21) & 0x7F, (crc >> 28) & 0x0F]


def tail_to_int(tail5):
    """Decode a trailing 5-byte 7-bit-LE field (status codes, CRCs)."""
    return (tail5[0] | (tail5[1] << 7) | (tail5[2] << 14)
            | (tail5[3] << 21) | ((tail5[4] & 0x0F) << 28))


# ------------------------------------------------------------- transport

class MidoTransport:
    """The real transport: a pair of already-open mido ports.

    recv() polls instead of blocking, so a timeout is ALWAYS possible -
    the unconditional blocking receive is the exact defect that wedged the
    transfer on 2026-07-17. This class never opens or closes ports; the
    caller owns the connection (one process, one connection, no handoff).
    """

    def __init__(self, inport, outport):
        self.inport = inport
        self.outport = outport

    def send(self, payload):
        import mido
        self.outport.send(mido.Message("sysex", data=list(payload)))

    def recv(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = self.inport.poll()
            if msg is not None:
                if msg.type == "sysex":
                    return bytes(msg.data)
                continue        # ignore non-SysEx traffic (clock, etc.)
            time.sleep(0.002)
        return None


# ---------------------------------------------------------- file session

class FileSession:
    """Timeout-guarded, verifying implementation of the pedal file API.

    Wire-compatible with zoomzt2's sequences (including its quirks - the
    open-for-read message really is sent twice), plus: every receive is
    timeout-bounded, every status word is CHECKED (zoomzt2 ignores them),
    and downloads FAIL on a CRC mismatch instead of silently dropping the
    block.
    """

    def __init__(self, transport, log=None, recv_timeout=RECV_TIMEOUT):
        self.t = transport
        self.recv_timeout = recv_timeout
        self._log_cb = log

    def log(self, line):
        if self._log_cb:
            self._log_cb(line)

    # ---- primitives

    def _send(self, body):
        payload = list(ZOOM_HDR) + list(body)
        self.log("tx " + bytes(payload).hex())
        self.t.send(payload)

    def _recv_any(self, what):
        payload = self.t.recv(self.recv_timeout)
        if payload is None:
            self.log("TIMEOUT waiting for %s" % what)
            raise TransferTimeout("no reply to %s within %.1fs"
                                  % (what, self.recv_timeout))
        self.log("rx " + bytes(payload).hex())
        return payload

    def _recv60(self, what):
        """Receive the next file-API (`60`-family) reply, skipping any
        unrelated SysEx (late parameter ACKs and the like)."""
        deadline = time.monotonic() + self.recv_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.log("TIMEOUT waiting for %s" % what)
                raise TransferTimeout("no reply to %s within %.1fs"
                                      % (what, self.recv_timeout))
            payload = self.t.recv(remaining)
            if payload is None:
                self.log("TIMEOUT waiting for %s" % what)
                raise TransferTimeout("no reply to %s within %.1fs"
                                      % (what, self.recv_timeout))
            self.log("rx " + bytes(payload).hex())
            if (len(payload) >= 5 and tuple(payload[:3]) == ZOOM_HDR
                    and payload[3] == 0x60):
                return payload
            self.log("   (skipped unrelated reply)")

    def _ack_or_poll(self, what):
        """Receive an acknowledgement; tolerate ONE lost reply by sending a
        read-only status poll and accepting its reply instead. Never
        re-sends the original command (a data block sent twice could be
        appended twice); a second silence aborts the transfer."""
        try:
            return self._recv60(what)
        except TransferTimeout:
            self.log("no reply to %s - one recovery status poll" % what)
            self._send([0x60, SUB_STATUS, 0x00])
            return self._recv60(what + " (recovery poll)")

    def _named(self, body, name):
        return list(body) + [ord(c) for c in name] + [0x00]

    def _status_code(self):
        self._send([0x60, SUB_STATUS, 0x00])
        reply = self._recv60("status poll")
        if len(reply) < 10:
            raise TransferError("malformed status reply: %s"
                                % bytes(reply).hex())
        return tail_to_int(list(reply[-5:]))

    def _status_note(self, context):
        """Poll the post-write status and LOG a nonzero code, but never abort
        on it. zoomzt2's proven file_upload sends this same poll and DISCARDS
        the reply - whether the pedal always returns 0 after a write is
        unknown, so treating nonzero as fatal here would risk false aborts
        (and, on the effect-list rewrite, a false DO-NOT-POWER-CYCLE alarm).
        The readback-verify (download + byte-compare) is the real correctness
        gate; it catches a genuinely bad write reliably, whatever the status
        said."""
        try:
            code = self._status_code()
        except TransferError as e:
            self.log("note: could not read status after %s (%s)"
                     % (context, e))
            return
        if code != 0:
            self.log("note: pedal status 0x%x after %s (continuing; the "
                     "readback verify is the correctness gate)"
                     % (code, context))

    # ---- session mode (what zoomzt2's CLI does around file operations)

    def pcmode_on(self):
        self._send([0x52])
        self._recv_any("PC-mode on")

    def pcmode_off(self):
        self._send([0x53])
        # best-effort: sent at teardown; a lost ack must not mask a result
        try:
            self._recv_any("PC-mode off")
        except TransferTimeout:
            pass

    # ---- file operations (zoomzt2-exact sequences, guarded)

    def file_check(self, name):
        """True iff `name` is present on the pedal. Read-only."""
        self._send(self._named([0x60, SUB_CHECK_OPEN, 0x00, 0x00], name))
        self._ack_or_poll("presence check of %s" % name)
        code = self._status_code()
        self._send([0x60, SUB_CHECK_CLOSE])
        self._ack_or_poll("presence-check close")
        return code == 0

    def download(self, name):
        """Download a file. CRC-verified: a bad block FAILS the transfer
        (zoomzt2 silently drops bad blocks, which corrupts the result)."""
        open_read = self._named(
            [0x60, SUB_OPEN, 0x02] + [0x00] * 9, name)
        self._send(open_read)
        self._ack_or_poll("open-for-read of %s" % name)
        self._send(open_read)          # zoomzt2 sends it twice; mirror it
        self._ack_or_poll("open-for-read of %s (2nd)" % name)

        data = bytearray()
        while True:
            self._send([0x60, SUB_STATUS, 0x00])
            self._recv60("pre-read status")
            self._send([0x60, SUB_READ_BLOCK, 0x14, 0x2F, 0x60, 0x00,
                        0x0C, 0x00, 0x04, 0x00, 0x00, 0x00])
            self._recv60("read-block request ack")
            self._send([0x60, SUB_STATUS, 0x00])
            reply = self._recv60("read-block data")
            if len(reply) < 10 or reply[4] != REPLY_DATA:
                break
            length = reply[9] * 128 + reply[8]
            if length == 0:
                break
            block = unpack7(reply[10:10 + length + length // 7 + 1])
            if (tail_to_int(list(reply[-5:])) ^ 0xFFFFFFFF) \
                    != binascii.crc32(bytes(block)):
                raise TransferError(
                    "CRC mismatch downloading %s at offset %d"
                    % (name, len(data)))
            data += block
        return bytes(data)

    def upload(self, name, data):
        """Stream `name` onto the pedal. NOTE: opens with `60 24`, which
        DELETES any existing entry of that name - between this call and a
        completed close() the old file is GONE and the new one partial.
        Callers own that window; close() and verify afterwards."""
        self._send(self._named([0x60, SUB_DELETE], name))
        self._ack_or_poll("delete/replace entry for %s" % name)
        self._send(self._named(
            [0x60, SUB_OPEN, 0x01] + [0x00] * 9, name))
        self._ack_or_poll("open-for-write of %s" % name)
        self._status_note("open-for-write of %s" % name)

        sent = 0
        data = bytes(data)
        while sent < len(data):
            chunk = data[sent:sent + BLOCK]
            body = [0x60, SUB_WRITE_BLOCK, 0x40, 0x00, 0x00, 0x00, 0x00,
                    len(chunk) & 0x7F, (len(chunk) >> 7) & 0x7F,
                    0x00, 0x00, 0x00]
            body += list(pack7(chunk))
            body += crc32_tail(chunk)
            self._send(body)
            self._ack_or_poll("write block at %d of %s" % (sent, name))
            self._status_note("write block at %d of %s" % (sent, name))
            sent += len(chunk)

    def delete(self, name):
        self._send(self._named([0x60, SUB_DELETE], name))
        self._ack_or_poll("delete of %s" % name)

    def close(self):
        """The clean end of any file operation: `60 21` + `60 09`."""
        self._send([0x60, SUB_CLOSE, 0x40, 0x00, 0x00, 0x00, 0x00])
        self._ack_or_poll("file close")
        self._send([0x60, SUB_SESSION_END])
        self._ack_or_poll("session end")

    def disk_usage(self):
        """(maximum, available) bytes of pedal storage. Read-only."""
        self._send([0x60, SUB_DISK_USAGE, 0x00, 0x00, 0x00, 0x00, 0x00])
        reply = self._recv60("disk usage")
        if len(reply) < 20:
            raise TransferError("malformed disk-usage reply: %s"
                                % bytes(reply).hex())
        maximum = tail_to_int(list(reply[10:15]))
        available = tail_to_int(list(reply[15:20]))
        return maximum, available

    def abort(self):
        """Best-effort clean close after a failure. Sends the FULL close
        sequence (`60 21` then `60 09`) UNCONDITIONALLY - both bytes go out
        even if the first is never acknowledged, because on a wedged pedal
        the whole session-close should land if the pedal can process it
        at all, not stop at the first silence. Returns True iff the pedal
        acknowledged both, so callers know whether a verification pass is
        worth attempting."""
        saved = self.recv_timeout
        self.recv_timeout = ABORT_TIMEOUT
        acked = True
        try:
            for body, what in (
                    ([0x60, SUB_CLOSE, 0x40, 0x00, 0x00, 0x00, 0x00],
                     "abort close"),
                    ([0x60, SUB_SESSION_END], "abort session end")):
                self._send(body)
                try:
                    self._ack_or_poll(what)
                except TransferError:
                    acked = False
            self.log("abort: full close sequence SENT (%s)"
                     % ("acknowledged" if acked else "no/partial ack"))
            return acked
        finally:
            self.recv_timeout = saved


# ------------------------------------------------- the guarded operations

def banner(log, reason, recovery_hint):
    """The P4 rule as tool output: printed at the exact moment a transfer
    failure leaves the pedal's state unverified. The prose version of this
    rule was overridden once, and a pedal died; the tool now says it."""
    lines = [
        "",
        "!" * 72,
        "!!  TRANSFER FAILED - THE PEDAL'S STATE COULD NOT BE VERIFIED",
        "!!",
        "!!  *** DO NOT POWER-CYCLE THE PEDAL. DO NOT UNPLUG ITS POWER. ***",
        "!!",
        "!!  Why: %s" % reason,
        "!!",
        "!!  The pedal may be wedged inside an open file-transfer session",
        "!!  with its internal storage mid-write. While it stays POWERED",
        "!!  this is recoverable. A REBOOT in this state can make the pedal",
        "!!  PERMANENTLY UNBOOTABLE - this exact sequence destroyed a pedal",
        "!!  on 2026-07-17 (SAFETY.md: 'An interrupted file transfer can",
        "!!  brick').",
        "!!",
        "!!  Next steps, in order:",
        "!!   1. LEAVE THE PEDAL POWERED and connected.",
        "!!   2. %s" % recovery_hint,
        "!!   3. Only reboot after the effect list has been read back and",
        "!!      verified intact.",
        "!" * 72,
        "",
    ]
    for line in lines:
        print(line)
        if log:
            log(line)


def verified_upload(sess, name, data):
    """Upload + close + read back + byte-compare. Raises TransferError on
    any mismatch; returns silently only when the pedal provably holds
    exactly `data` under `name`."""
    sess.upload(name, data)
    sess.close()
    back = sess.download(name)
    sess.close()
    if back != bytes(data):
        raise TransferError(
            "readback of %s differs from what was sent (%d vs %d bytes)"
            % (name, len(back), len(data)))


def replace_flst(sess, flst_old, flst_new, log=None):
    """Rewrite the effect list - THE most dangerous moment of any install
    or uninstall (see the module docstring for why it cannot be atomic).
    Returns the number of retries used (0 = clean first pass).

    Raises PedalUnresolved (banner already printed) if the window cannot
    be exited with a verified effect list; the caller must NOT catch that
    into a generic cleanup - there is nothing safer to do than what this
    function already tried, and the pedal must stay powered."""
    if bytes(flst_new) == bytes(flst_old):
        return 0

    # Pre-window assurance, all read-only: the link and the readback path
    # work RIGHT NOW, nothing else rewrote the list, and space exists.
    current = sess.download(FLST_NAME)
    sess.close()
    if current != bytes(flst_old):
        raise FlstBaselineChanged(
            "%s on the pedal no longer matches the copy this operation "
            "was computed from - something else wrote it; start over "
            "from a fresh download" % FLST_NAME, current)
    _, available = sess.disk_usage()
    if available < len(flst_new) + DISK_MARGIN:
        raise TransferError(
            "only %d bytes free on the pedal; refusing to enter the "
            "effect-list rewrite with less than %d"
            % (available, len(flst_new) + DISK_MARGIN))

    last_error = "unknown"
    for attempt in range(1, FLST_WRITE_ATTEMPTS + 1):
        try:
            sess.upload(FLST_NAME, flst_new)
            sess.close()
            back = sess.download(FLST_NAME)
            sess.close()
            if back == bytes(flst_new):
                return attempt - 1
            last_error = ("readback after write differs (%d bytes)"
                          % len(back))
        except TransferError as e:
            last_error = str(e)
        sess.log("effect-list write attempt %d/%d failed: %s"
                 % (attempt, FLST_WRITE_ATTEMPTS, last_error))
        sess.abort()
    banner(log,
           "the rewrite of %s failed %d times (last error: %s); the "
           "pedal's effect list may be missing or truncated"
           % (FLST_NAME, FLST_WRITE_ATTEMPTS, last_error),
           "Re-run with 'rescue' (see below) to close the session, retry "
           "the effect-list write from the saved copy, and verify it.")
    raise PedalUnresolved("effect-list rewrite unresolved: %s" % last_error)


def _unwind(sess, failure, cleanup_names, flst_expect, log):
    """After a failure OUTSIDE the effect-list window: close the session,
    remove any partial/now-orphaned files, and verify the effect list
    matches the expected baseline. Raises TransferAborted (pedal verified
    safe) or PedalUnresolved."""
    sess.log("FAILURE: %s - starting clean abort" % failure)
    acked = sess.abort()
    if acked:
        try:
            for name in cleanup_names:
                if sess.file_check(name):
                    sess.delete(name)
                    sess.close()
                else:
                    sess.close()   # every file_check gets its file_close
            back = sess.download(FLST_NAME)
            sess.close()
            if back == bytes(flst_expect):
                raise TransferAborted(
                    "%s - aborted cleanly; file session closed, partial "
                    "files removed, effect list verified intact. The "
                    "pedal is SAFE; fix the cause and retry." % failure)
            failure += ("; and the effect list read back DIFFERENT from "
                        "the expected baseline")
        except TransferError as e:
            failure += ("; and post-abort verification failed (%s)" % e)
    else:
        failure += "; and the pedal did not acknowledge the close"
    banner(log, failure,
           "Re-run with 'rescue' to attempt the session close again and "
           "verify the effect list.")
    raise PedalUnresolved(failure)


def install_effect(sess, files, flst_old, flst_new, log=None):
    """Install: upload + verify every file in `files` ([(name, bytes)],
    icon first), THEN rewrite the effect list. Files-then-list order means
    a failure before the list rewrite leaves the pedal logically unchanged
    (at worst an orphan file, which is harmless and removable).

    Returns {'flst_retries': n}. Raises TransferAborted / PedalUnresolved.
    """
    uploaded = []
    try:
        # presence pre-check of EVERY file before writing ANYTHING, so a
        # same-name refusal can never leave a half-installed pair behind
        # (and so an install can never silently skip an upload - the
        # zoomzt2 behaviour that left old code running, SAFETY.md)
        for name, data in files:
            # zoomzt2 always pairs a file_check with a file_close; mirror that
            # exactly (one close per check) so the wire sequence matches the
            # proven tool even when the file is absent.
            present = sess.file_check(name)
            sess.close()
            if present:
                raise TransferAborted(
                    "'%s' is already on the pedal - uninstall first "
                    "(nothing was written)" % name)
        for name, data in files:
            uploaded.append(name)
            verified_upload(sess, name, data)
            sess.log("%s uploaded and verified (%d bytes)"
                     % (name, len(data)))
    except TransferError as e:
        _unwind(sess, "installing: %s" % e, uploaded, flst_old, log)
    try:
        retries = replace_flst(sess, flst_old, flst_new, log=log)
    except FlstBaselineChanged as e:
        # The pedal's list is different but INTACT - verify against what
        # is actually there, not the stale expectation.
        _unwind(sess, "preparing the effect-list rewrite: %s" % e,
                uploaded, e.current, log)
    except TransferError as e:
        # replace_flst raises TransferError only from its read-only
        # pre-window checks; the effect list is still untouched. Remove
        # the files uploaded above so a retry starts from scratch.
        _unwind(sess, "preparing the effect-list rewrite: %s" % e,
                uploaded, flst_old, log)
    return {"flst_retries": retries}


def writetest_effect(sess, name, data, flst_expect, log=None):
    """The effect-list-free WRITE TEST (the first-hardware-run rung):
    upload `name`, read it back and byte-compare, then delete it and
    verify it is gone - never computing, never writing the effect list.
    It exercises every write primitive an install uses (the delete/replace
    open, the block stream, the close, the readback, the bare delete)
    while the one file the pedal's boot depends on stays untouched BY
    CONSTRUCTION: there is no code path in this function that could
    address it.

    `flst_expect` is the pedal's current effect list, downloaded by the
    caller moments before. It is used only to VERIFY: on success the list
    is read back and must be byte-identical (a change during a test that
    never writes it is treated as an emergency), and on failure the
    unwind verifies against it as usual. The caller must also have
    checked that `name` has NO entry in that list - deleting a listed
    file would strand its entry.

    Returns {} on success. Raises TransferAborted (pedal verified safe)
    or PedalUnresolved (banner printed; keep the pedal powered)."""
    try:
        present = sess.file_check(name)
        sess.close()
        if present:
            raise TransferAborted(
                "'%s' is already on the pedal - refusing the write test "
                "(deleting it at the end could break what is there); "
                "nothing was written" % name)
        verified_upload(sess, name, data)
        sess.log("%s uploaded, read back, byte-verified (%d bytes)"
                 % (name, len(data)))
        sess.delete(name)
        sess.close()
        still_present = sess.file_check(name)
        sess.close()
        if still_present:
            raise TransferError("%s still present after delete" % name)
        sess.log("%s deleted and verified absent" % name)
        back = sess.download(FLST_NAME)
        sess.close()
        if back != bytes(flst_expect):
            raise TransferError(
                "the effect list read back DIFFERENT after a test that "
                "never writes it - stop all pedal work and investigate")
    except TransferError as e:
        _unwind(sess, "write test: %s" % e, [name], flst_expect, log)
    return {}


def uninstall_effect(sess, names, flst_old, flst_new, log=None):
    """Uninstall: rewrite the effect list FIRST (entry removed), then
    delete the files in `names`. List-then-files order means a failure
    after the rewrite leaves only orphan files (harmless; re-run to
    clean), never a list that references nothing.

    Returns {'flst_retries': n, 'orphans': [...]}. Raises TransferAborted
    / PedalUnresolved.
    """
    try:
        retries = replace_flst(sess, flst_old, flst_new, log=log)
    except FlstBaselineChanged as e:
        _unwind(sess, "preparing the effect-list rewrite: %s" % e,
                [], e.current, log)
    except TransferError as e:
        # Pre-window failure: the effect list is still the old one.
        _unwind(sess, "preparing the effect-list rewrite: %s" % e,
                [], flst_old, log)
    try:
        for name in names:
            if sess.file_check(name):
                sess.delete(name)
                sess.close()
                still_present = sess.file_check(name)   # verify it is gone
                sess.close()
                if still_present:
                    raise TransferError(
                        "%s still present after delete" % name)
                sess.log("%s deleted" % name)
            else:
                sess.close()   # every file_check gets its file_close
                sess.log("%s was not on the pedal" % name)
    except TransferError as e:
        # The new effect list is already committed and verified; a failed
        # file delete leaves an orphan, which is benign - but the session
        # state still must be unwound and verified like any failure.
        _unwind(sess, "deleting files after the list rewrite: %s" % e,
                [], flst_new, log)
    return {"flst_retries": retries, "orphans": []}
