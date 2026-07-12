"""Durable worker process identity: capture, liveness, and group kill.

Workers are spawned as leaders of their own process group (``start_new_session=True``
in ``agents/spawner.py``), and their identity — pid, pgid, and a start-time token —
is persisted in the run log at spawn time. That makes two things possible after the
parent process is gone (design: TH-D5, ``design/designs/process-lifecycle-and-reaping.md``):

- a **durable liveness check**: "is the group we launched still running?", answerable
  by any later process, guarded against pid reuse by comparing start-time identity;
- a **safe group kill**: terminate the whole worker subtree (the leader and any
  helpers it spawned share the group) while refusing to touch anything whose
  identity cannot be verified.

Identity tokens, strongest available per platform:

- **Linux**: ``linux:<boot_id>:<start_ticks>`` — the kernel's exact start time in
  clock ticks since boot (``/proc/<pid>/stat`` field 22) plus the boot id, so the
  token is unique across reboots and immune to wall-clock/NTP shifts.
- **macOS / fallback**: ``lstart:<ps lstart string>`` — second-resolution wall-clock
  start time, locale- and timezone-pinned. Second-resolution reuse of the same pid
  is a documented (astronomically narrow) residual window on this path.

Probe failures are never conflated with "the group is dead": a broken/missing ``ps``
raises ``ProcessProbeError`` so callers can record ``probe_failed`` instead of
silently marking a live worker gone.

POSIX-only, like worker spawning itself.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import time

_PS_ENV = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}

_LINUX_TOKEN_PREFIX = "linux:"
_LSTART_TOKEN_PREFIX = "lstart:"


class ProcessProbeError(RuntimeError):
    """The process table could not be read (ps missing/failing, /proc unreadable).

    Callers must treat this as "unknown", never as "dead".
    """


@dataclass(frozen=True)
class GroupMember:
    pid: int
    starttime: str


@dataclass(frozen=True)
class GroupLiveness:
    """Result of a liveness probe for a persisted (pgid, starttime) identity.

    ``alive`` means "some non-zombie process in the group is running". ``verdict``
    refines it:

    - ``dead``: no member left; the group is gone.
    - ``ours``: the leader is present and its start-time identity matches what we
      recorded at spawn.
    - ``identity_mismatch``: a process exists with the leader's pid but a different
      start time — the pid was recycled by an unrelated process. Never touch it.
    - ``unverifiable``: identity cannot be confirmed — either the leader is present
      but no start time was captured at spawn, or the leader is gone and only
      non-leader members remain. Once the leader is dead, the pgid could in
      principle have been recycled by a new, unrelated session whose own leader
      also exited, so surviving members cannot be attributed to us with certainty.
      Killing is not safe; waiting is.
    """

    alive: bool
    verdict: str
    members: tuple[GroupMember, ...] = ()


def _linux_boot_id() -> str | None:
    try:
        return (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        )
    except OSError:
        return None


def _linux_start_ticks(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat: start time in clock ticks since boot."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    # comm (field 2) may contain spaces/parens; fields resume after the last ')'.
    _, _, rest = stat.rpartition(")")
    fields = rest.split()
    # rest starts at field 3 ("state"), so start_ticks (field 22) is index 19.
    if len(fields) < 20:
        return None
    return fields[19]


def _ps_lstart(pid: int) -> str | None:
    """``ps -p PID -o lstart=`` — empty output legitimately means "no such pid"."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=10,
            env=_PS_ENV,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def capture_starttime(pid: int) -> str | None:
    """Return a start-time identity token for ``pid``, or None if unavailable.

    Prefers the exact Linux kernel identity (boot id + start ticks); falls back
    to the second-resolution ``ps lstart`` string elsewhere. The platform gate
    must match ``group_members`` exactly, or capture-time and probe-time tokens
    would never compare equal.
    """
    if _proc_identity_available():
        ticks = _linux_start_ticks(pid)
        if ticks is None:
            return None
        boot_id = _linux_boot_id()
        if boot_id is None:
            return None
        return f"{_LINUX_TOKEN_PREFIX}{boot_id}:{ticks}"
    lstart = _ps_lstart(pid)
    if lstart is not None:
        return f"{_LSTART_TOKEN_PREFIX}{lstart}"
    return None


def _member_matches_token(member_starttime: str, recorded: str) -> bool:
    """Compare a live member's start identity against the recorded token.

    Tokens written by this module are prefixed; a bare recorded value (from an
    older run.json) is compared against the lstart form for compatibility.
    """
    if recorded.startswith(_LINUX_TOKEN_PREFIX) or recorded.startswith(
        _LSTART_TOKEN_PREFIX
    ):
        return member_starttime == recorded
    # Legacy un-prefixed value: match against the raw lstart portion.
    if member_starttime.startswith(_LSTART_TOKEN_PREFIX):
        return member_starttime[len(_LSTART_TOKEN_PREFIX) :] == recorded
    return member_starttime == recorded


def _proc_identity_available() -> bool:
    return os.path.isdir("/proc") and os.path.exists("/proc/sys/kernel/random/boot_id")


def group_members(pgid: int) -> list[GroupMember]:
    """Return live (non-zombie) processes whose process group id is ``pgid``.

    Zombies are excluded: an exited-but-not-yet-reaped child still shows up in
    the process table, but it holds no resources, cannot write to the checkout,
    and cannot be killed — for liveness purposes it is already gone.

    Raises ``ProcessProbeError`` when the process table cannot be read at all —
    an empty result must always mean "genuinely no members", never "ps broke".
    """
    use_proc = _proc_identity_available()
    columns = "pid=,pgid=,stat=" if use_proc else "pid=,pgid=,stat=,lstart="
    try:
        result = subprocess.run(
            ["ps", "-eo", columns],
            capture_output=True,
            text=True,
            timeout=10,
            env=_PS_ENV,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProcessProbeError(f"cannot read the process table: {exc}") from exc
    if result.returncode != 0:
        raise ProcessProbeError(
            f"ps exited with {result.returncode}: {result.stderr.strip()[:200]}"
        )
    members: list[GroupMember] = []
    parsed_any = False
    for line in result.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < (3 if use_proc else 4):
            continue
        try:
            pid, line_pgid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        parsed_any = True
        if line_pgid != pgid:
            continue
        if parts[2].startswith("Z"):
            continue
        if use_proc:
            # Exact kernel identity; the /proc read is a cheap file read.
            identity = capture_starttime(pid) or ""
        else:
            identity = f"{_LSTART_TOKEN_PREFIX}{parts[3].strip()}"
        members.append(GroupMember(pid=pid, starttime=identity))
    if not parsed_any:
        raise ProcessProbeError("ps produced no parseable process listing")
    return members


def probe_group(pgid: int, leader_starttime: str | None) -> GroupLiveness:
    """Probe whether the group identified by ``(pgid, leader_starttime)`` is ours.

    The leader's pid equals the pgid (it created the group via
    ``start_new_session``). See ``GroupLiveness`` for the verdict semantics.
    Raises ``ProcessProbeError`` when the process table cannot be read.
    """
    members = tuple(group_members(pgid))
    if not members:
        return GroupLiveness(alive=False, verdict="dead")
    leader = next((member for member in members if member.pid == pgid), None)
    if leader is None:
        # The leader is gone. While our group exists continuously, only our
        # descendants can be members — but once the group fully empties, the
        # pgid can be recycled by an unrelated new session whose leader also
        # exited. Surviving members are therefore not attributable with
        # certainty: waiting on them is safe, killing them is not.
        return GroupLiveness(alive=True, verdict="unverifiable", members=members)
    if leader_starttime is None:
        return GroupLiveness(alive=True, verdict="unverifiable", members=members)
    if leader.starttime and _member_matches_token(leader.starttime, leader_starttime):
        return GroupLiveness(alive=True, verdict="ours", members=members)
    return GroupLiveness(alive=True, verdict="identity_mismatch", members=members)


def signal_group(pgid: int, sig: signal.Signals) -> bool:
    """Send ``sig`` to the whole group. Returns False if it could not be signalled."""
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def kill_group(
    pgid: int,
    leader_starttime: str | None,
    *,
    grace_s: float = 10.0,
    poll_interval_s: float = 0.2,
) -> str:
    """SIGTERM the group, wait up to ``grace_s``, then SIGKILL, then verify.

    Identity is verified before **every** signal (probe→TERM and TERM→KILL are
    both re-checked), and the result is honest: ``"killed"`` is returned only
    when the group is observed gone. Possible verdicts: ``killed``,
    ``already-exited``, ``identity-mismatch-skipped``,
    ``identity-unverifiable-skipped``, ``kill-failed-still-running``,
    ``probe-failed``.
    """

    def _verified_signal(sig: signal.Signals) -> str | None:
        """Re-probe, then signal only on a verified ``ours``. None = proceed."""
        try:
            liveness = probe_group(pgid, leader_starttime)
        except ProcessProbeError:
            return "probe-failed"
        if not liveness.alive:
            return "already-exited"
        if liveness.verdict == "identity_mismatch":
            return "identity-mismatch-skipped"
        if liveness.verdict == "unverifiable":
            return "identity-unverifiable-skipped"
        signal_group(pgid, sig)
        return None

    def _wait_gone(deadline: float) -> bool | None:
        """True = gone; False = still there; None = probe failed."""
        while time.monotonic() < deadline:
            try:
                if not group_members(pgid):
                    return True
            except ProcessProbeError:
                return None
            time.sleep(poll_interval_s)
        try:
            return not group_members(pgid)
        except ProcessProbeError:
            return None

    early = _verified_signal(signal.SIGTERM)
    if early is not None:
        return early
    gone = _wait_gone(time.monotonic() + grace_s)
    if gone is None:
        return "probe-failed"
    if gone:
        return "killed"
    early = _verified_signal(signal.SIGKILL)
    if early == "already-exited":
        return "killed"
    if early is not None:
        return early
    gone = _wait_gone(time.monotonic() + grace_s)
    if gone is None:
        return "probe-failed"
    if gone:
        return "killed"
    # SIGKILL delivered but members persist (e.g. uninterruptible D-state).
    return "kill-failed-still-running"


def wait_group(
    pgid: int,
    leader_starttime: str | None,
    *,
    timeout_s: float,
    poll_interval_s: float = 0.5,
) -> bool:
    """Wait for the group to exit on its own. Returns True if it exited in time.

    An ``identity_mismatch`` counts as exited: the group we launched is gone,
    something else merely recycled the pid. Raises ``ProcessProbeError`` if the
    process table cannot be read.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        liveness = probe_group(pgid, leader_starttime)
        if not liveness.alive or liveness.verdict == "identity_mismatch":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval_s)


def wait_groups(
    targets: dict[int, str | None], *, timeout_s: float, poll_interval_s: float = 0.5
) -> dict[int, bool]:
    """Wait for several groups under ONE shared deadline (not timeout × N).

    Returns pgid → exited-in-time. A probe failure marks the remaining groups
    False (unknown ≠ exited) rather than raising mid-wait, so the caller still
    receives a complete map.
    """
    pending = dict(targets)
    exited: dict[int, bool] = {}
    deadline = time.monotonic() + timeout_s
    while pending:
        for pgid, starttime in list(pending.items()):
            try:
                liveness = probe_group(pgid, starttime)
            except ProcessProbeError:
                continue  # transient probe issue: retry until the deadline
            if not liveness.alive or liveness.verdict == "identity_mismatch":
                exited[pgid] = True
                del pending[pgid]
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(poll_interval_s)
    for pgid in pending:
        exited[pgid] = False
    return exited
