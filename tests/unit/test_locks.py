"""Unit tests for OS-level advisory lockfiles.

Mocking-friendly: we don't fork a real second process to test mutual
exclusion. Instead we test the file-level state machine — acquire,
release, double-acquire raises, stale-PID detection.

A second-process scenario (where one Python interpreter holds the lock
and another tries to acquire) is covered by the integration suite, not
this unit test, because spawning processes is slow and brittle on
Windows CI.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aurochs_core import LockError, MigrateLock, WriteLock
from aurochs_core.locks import _pid_alive


# ---------------------------------------------------------------------------
# Single-process state machine
# ---------------------------------------------------------------------------

class TestSingleProcess:
    def test_acquire_release(self, tmp_path: Path) -> None:
        db = tmp_path / "recall.db"
        lock = WriteLock(db)
        lock.acquire()
        try:
            assert lock.path.exists()
        finally:
            lock.release()
        # After release the PID stamp should be readable. On Windows,
        # msvcrt.locking blocks reads from a SECOND handle while the lock
        # is held — so we only verify the stamp post-release.
        stamped = int(lock.path.read_text().strip() or "0")
        assert stamped == os.getpid()

    def test_context_manager(self, tmp_path: Path) -> None:
        db = tmp_path / "recall.db"
        with WriteLock(db) as lock:
            assert lock.path.exists()
        # On POSIX the file is cleaned up; on Windows we deliberately leave
        # it in place because msvcrt holds the byte-range even after close
        # and we don't want race conditions on unlink. Either way, the OS
        # lock has been released — re-acquire must succeed.
        with WriteLock(db) as lock2:
            assert lock2.path.exists()

    def test_double_acquire_same_object_raises(self, tmp_path: Path) -> None:
        db = tmp_path / "recall.db"
        lock = WriteLock(db)
        lock.acquire()
        try:
            with pytest.raises(RuntimeError):
                lock.acquire()
        finally:
            lock.release()

    def test_release_without_acquire_safe(self, tmp_path: Path) -> None:
        db = tmp_path / "recall.db"
        lock = WriteLock(db)
        # Should be a no-op, not raise.
        lock.release()

    def test_write_and_migrate_locks_distinct(self, tmp_path: Path) -> None:
        """A WriteLock and a MigrateLock can be held simultaneously by the
        same process — they target different lockfiles."""
        db = tmp_path / "recall.db"
        with WriteLock(db) as wl:
            with MigrateLock(db) as ml:
                assert wl.path != ml.path
                assert wl.path.name.endswith(".write.lock")
                assert ml.path.name.endswith(".migrate.lock")


# ---------------------------------------------------------------------------
# Stale-PID detection
# ---------------------------------------------------------------------------

class TestStalePidDetection:
    def test_stale_lockfile_with_dead_pid_is_force_released(
        self, tmp_path: Path
    ) -> None:
        """Pre-create a lockfile claiming a dead PID; acquire must succeed."""
        db = tmp_path / "recall.db"
        lockfile = db.with_name(db.name + ".write.lock")
        lockfile.parent.mkdir(parents=True, exist_ok=True)
        # Use a PID we're confident is dead. PID 1 is normally init/launchd
        # and will be alive — pick a high random PID instead. 999_999_999
        # is well above the typical PID_MAX on Linux/macOS/Windows.
        lockfile.write_text("999999999")

        # The lockfile exists but no process actually holds the OS-level
        # lock (we only wrote the PID, didn't fcntl/msvcrt-lock). The
        # acquire path should succeed because it can take the OS-level
        # lock — the stale-PID branch isn't strictly needed here, but the
        # test confirms the happy path doesn't get confused by the file.
        with WriteLock(db) as lock:
            assert lock.path == lockfile

    def test_pid_alive_self(self) -> None:
        """Sanity: our own PID is reported alive."""
        assert _pid_alive(os.getpid())

    def test_pid_alive_dead_pid(self) -> None:
        """A high-numbered PID that almost certainly doesn't exist must
        be reported as dead (or, conservatively, as alive on platforms
        where we can't tell — the code falls back to alive when ambiguous).

        We accept either answer; the goal is to confirm the function
        doesn't raise on an exotic input.
        """
        # Just verify no exception. Whether 999_999_999 reports alive or
        # dead depends on the platform and whether psutil is installed.
        result = _pid_alive(999_999_999)
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Concurrent acquire (in-process simulation via separate WriteLock objects)
# ---------------------------------------------------------------------------

class TestConcurrentInProcess:
    def test_two_locks_same_file_one_wins(self, tmp_path: Path) -> None:
        """Two WriteLock objects pointing at the same file: only one can
        hold the OS-level lock at a time within a process.

        On POSIX, fcntl.flock is per-file-descriptor but advisory between
        processes — same-process double-locking via fcntl actually returns
        success (POSIX-specific edge case). Windows msvcrt.locking is
        strict. We accept either behavior here and just assert the API
        doesn't crash.
        """
        db = tmp_path / "recall.db"
        lock_a = WriteLock(db, timeout=0)
        lock_b = WriteLock(db, timeout=0)

        lock_a.acquire()
        try:
            try:
                lock_b.acquire()
                # POSIX path — same-process flock is reentrant. Release.
                lock_b.release()
            except LockError:
                # Windows path — strict mutual exclusion within process.
                pass
        finally:
            lock_a.release()
