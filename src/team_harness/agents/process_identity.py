"""Durable worker process identity: capture, liveness, and group kill.

Workers are spawned as leaders of their own process group (``start_new_session=True``
in ``agents/spawner.py``), and their identity — pid, pgid, and process start time —
is persisted in the run log at spawn time. That makes two things possible after the
parent process is gone (design: TH-D5, ``design/designs/process-lifecycle-and-reaping.md``):

- a **durable liveness check**: "is the group we launched still running?", answerable
  by any later process, guarded against pid reuse by comparing start times;
- a **safe group kill**: terminate the whole worker subtree (the leader and any
  helpers it spawned share the group) without ever killing a recycled pid.

Start time is captured via ``ps -o lstart=`` (identical, locale-pinned output format on
macOS and Linux), which gives second-resolution start times — two different processes
with the same pid *and* the same start second are practically impossible.

POSIX-only, like worker spawning itself.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import signal
import subprocess
import time

_PS_ENV = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}


@dataclass(frozen=True)
class GroupMember:
    pid: int
    starttime: str


@dataclass(frozen=True)
class GroupLiveness:
    """Result of a liveness probe for a persisted (pgid, starttime) identity.

    ``alive`` means "some process in the group is running". ``verdict`` refines it:

    - ``dead``: no member left; the group is gone.
    - ``ours``: the leader is present and its start time matches what we recorded
      (or the leader already exited but surviving members are its descendants —
      only descendants of the leader can ever join the group, because the leader
      created its own session).
    - ``identity_mismatch``: a process exists with the leader's pid but a different
      start time — the pid was recycled by an unrelated process. Never touch it.
    - ``unverifiable``: the leader is present but we never captured a start time at
      spawn, so its identity cannot be confirmed. Killing is not safe; waiting is.
    """

    alive: bool
    verdict: str
    members: tuple[GroupMember, ...] = ()


def capture_starttime(pid: int) -> str | None:
    """Return the process start time string for ``pid``, or None if unavailable."""
    try:
        output = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=10,
            env=_PS_ENV,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return output or None


def group_members(pgid: int) -> list[GroupMember]:
    """Return the live (non-zombie) processes whose process group id is ``pgid``.

    Zombies are excluded: an exited-but-not-yet-reaped child still shows up in
    ``ps`` (e.g. when the spawning process is alive but has not waited on it),
    but it holds no resources, cannot write to the checkout, and cannot be
    killed — for liveness purposes it is already gone.
    """
    try:
        output = subprocess.run(
            ["ps", "-eo", "pid=,pgid=,stat=,lstart="],
            capture_output=True,
            text=True,
            timeout=10,
            env=_PS_ENV,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    members: list[GroupMember] = []
    for line in output.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, line_pgid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if line_pgid != pgid:
            continue
        if parts[2].startswith("Z"):
            continue
        members.append(GroupMember(pid=pid, starttime=parts[3].strip()))
    return members


def probe_group(pgid: int, leader_starttime: str | None) -> GroupLiveness:
    """Probe whether the group identified by ``(pgid, leader_starttime)`` is ours.

    The leader's pid equals the pgid (it created the group via ``start_new_session``).
    See ``GroupLiveness`` for the verdict semantics.
    """
    members = tuple(group_members(pgid))
    if not members:
        return GroupLiveness(alive=False, verdict="dead")
    leader = next((member for member in members if member.pid == pgid), None)
    if leader is None:
        # Only descendants of our leader can be in this group (it owned the
        # session), so surviving members without the leader are still ours.
        return GroupLiveness(alive=True, verdict="ours", members=members)
    if leader_starttime is None:
        return GroupLiveness(alive=True, verdict="unverifiable", members=members)
    if leader.starttime == leader_starttime:
        return GroupLiveness(alive=True, verdict="ours", members=members)
    return GroupLiveness(alive=True, verdict="identity_mismatch", members=members)


def signal_group(pgid: int, sig: signal.Signals) -> bool:
    """Send ``sig`` to the whole group. Returns False if the group is gone."""
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
    """SIGTERM the group, wait up to ``grace_s``, then SIGKILL survivors.

    Verifies identity first and refuses to touch a recycled or unverifiable pid.
    Returns the final verdict: ``"killed"``, ``"already-exited"``,
    ``"identity-mismatch-skipped"``, or ``"identity-unverifiable-skipped"``.
    """
    liveness = probe_group(pgid, leader_starttime)
    if not liveness.alive:
        return "already-exited"
    if liveness.verdict == "identity_mismatch":
        return "identity-mismatch-skipped"
    if liveness.verdict == "unverifiable":
        return "identity-unverifiable-skipped"
    signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not group_members(pgid):
            return "killed"
        time.sleep(poll_interval_s)
    signal_group(pgid, signal.SIGKILL)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not group_members(pgid):
            return "killed"
        time.sleep(poll_interval_s)
    return "killed"


def wait_group(
    pgid: int,
    leader_starttime: str | None,
    *,
    timeout_s: float,
    poll_interval_s: float = 0.5,
) -> bool:
    """Wait for the group to exit on its own. Returns True if it exited in time.

    An ``identity_mismatch`` counts as exited: the group we launched is gone,
    something else merely recycled the pid.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        liveness = probe_group(pgid, leader_starttime)
        if not liveness.alive or liveness.verdict == "identity_mismatch":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval_s)
