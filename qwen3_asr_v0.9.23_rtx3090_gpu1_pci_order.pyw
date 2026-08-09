# -*- coding: utf-8 -*-
# Main launcher revision: v0.9.23 (Qwen3-ASR official streaming + fixed WSL GPU index 1 RTX 3090)

import sys
import multiprocessing
import time
import os
import tkinter as tk
from tkinter import messagebox, ttk
from threading import Condition, Event, Thread, Lock, current_thread, main_thread
import queue
import re
import logging
from logging.handlers import RotatingFileHandler
from collections import deque, OrderedDict
from dataclasses import dataclass, field, asdict, is_dataclass
from difflib import SequenceMatcher
from typing import Optional, List, Dict, Any, Callable
import argparse
import base64
import json
import uuid
import wave
import subprocess
import hashlib
import shutil
import shlex
import tempfile
import urllib.request
import urllib.parse
import urllib.error
from math import gcd
from enum import Enum

# ================== 生产级命名常量 ==================
JOURNAL_MAX_BYTES: int = 50 * 1024 * 1024
JOURNAL_RETAIN_ACKNOWLEDGED_SECONDS: float = 86400.0

DEDUP_FUZZY_THRESHOLD: float = 0.88
DEDUP_MIN_MATCHING_COVERAGE: float = 0.78
DEDUP_VARIABLE_MIN_COVERAGE: float = 0.74
DEDUP_VARIABLE_RELAXED_OFFSET: float = 0.05
DEDUP_VARIABLE_RELAXED_FLOOR: float = 0.82

SESSION_ACTOR_IDEMPOTENCY_CACHE_SIZE: int = 2048
SESSION_ACTOR_COMMAND_QUEUE_SIZE: int = 2048
PREVIEW_SCHEDULE_MAX_ENTRIES: int = 128
PREVIEW_RESULT_CACHE_MAX: int = 128
EVENT_SUBSCRIBER_CONDITION_TIMEOUT: float = 0.5

# ================== 内嵌生产级事件/恢复基础设施 ==================
class ApplyResult(Enum):
    APPLIED = "applied"
    STALE = "stale"
    REJECTED = "rejected"
    FAILED = "failed"


class RecoveryJournal:
    """Crash-safe append-only journal with idempotent final records and O(1) metrics."""

    def __init__(self, path: str, *, max_bytes: int = JOURNAL_MAX_BYTES):
        self.path = os.path.abspath(path)
        self.max_bytes = max(1024 * 1024, int(max_bytes))
        self._lock = Lock()
        self._loaded = False
        self._records: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._pending_keys: Dict[str, str] = {}
        self._pending_count = 0
        self._write_failures = 0
        self._consecutive_write_failures = 0
        self._last_error = ""
        self._last_success_at = 0.0
        self._last_failure_at = 0.0
        # Create the durable directory once instead of on every append/ACK hot path.
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def _serialize(self, record: Dict[str, Any]) -> str:
        return json.dumps(record, ensure_ascii=False, default=str, sort_keys=True) + "\n"

    @staticmethod
    def _event_key(event_name: str, payload: Dict[str, Any]) -> str:
        identity = {
            "event_name": str(event_name),
            "session_id": payload.get("session_id", ""),
            "utterance_id": payload.get("utterance_id", 0),
            "source_revision": payload.get("source_revision", payload.get("revision", 0)),
            "request_id": payload.get("request_id", 0),
            "is_final": bool(payload.get("is_final", False)),
            "complete": bool(payload.get("complete", True)),
            "text": payload.get("text", ""),
        }
        raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _ensure_loaded_locked(self) -> None:
        if self._loaded:
            return
        self._records.clear()
        self._pending_keys.clear()
        self._pending_count = 0
        if os.path.isfile(self.path):
            acknowledged: set[str] = set()
            with open(self.path, "r", encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, 1):
                    try:
                        item = json.loads(line)
                        if not isinstance(item, dict):
                            continue
                        if item.get("record_type") == "ack":
                            acknowledged.update(str(v) for v in item.get("record_ids", []) if v)
                            continue
                        rid = str(item.get("record_id") or hashlib.sha256(line.encode("utf-8")).hexdigest())
                        item["record_id"] = rid
                        item.setdefault("status", "pending")
                        item.setdefault(
                            "event_key",
                            self._event_key(str(item.get("event_name", "")), dict(item.get("event", {}))),
                        )
                        self._records[rid] = item
                    except Exception:
                        logger.warning(
                            "[RECOVERY_JOURNAL] corrupt line skipped path=%s line=%d",
                            self.path,
                            line_no,
                        )
            for rid, record in self._records.items():
                if rid in acknowledged:
                    record["status"] = "acknowledged"
                if record.get("status", "pending") == "pending":
                    key = str(record.get("event_key", ""))
                    if key:
                        self._pending_keys[key] = rid
                    self._pending_count += 1
        self._loaded = True

    @property
    def pending_count(self) -> int:
        with self._lock:
            self._ensure_loaded_locked()
            return self._pending_count

    def append(self, reason: str, event_name: str, event: Any) -> bool:
        try:
            payload = asdict(event) if is_dataclass(event) else dict(getattr(event, "__dict__", {}))
            event_key = self._event_key(event_name, payload)
            now = time.time()
            with self._lock:
                self._ensure_loaded_locked()
                if event_key in self._pending_keys:
                    return True
                if os.path.isfile(self.path) and os.path.getsize(self.path) >= self.max_bytes:
                    self._compact_locked(retain_acknowledged_seconds=JOURNAL_RETAIN_ACKNOWLEDGED_SECONDS)
                record = {
                    "record_id": uuid.uuid4().hex,
                    "event_key": event_key,
                    "recorded_at": now,
                    "updated_at": now,
                    "status": "pending",
                    "reason": str(reason),
                    "event_name": str(event_name),
                    "event_type": type(event).__name__,
                    "event": payload,
                }
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(self._serialize(record))
                    handle.flush()
                    os.fsync(handle.fileno())
                self._records[record["record_id"]] = record
                self._pending_keys[event_key] = record["record_id"]
                self._pending_count += 1
                self._last_success_at = now
                self._consecutive_write_failures = 0
                self._last_error = ""
            return True
        except Exception as exc:
            self._write_failures += 1
            self._consecutive_write_failures += 1
            self._last_failure_at = time.time()
            self._last_error = str(exc)
            logger.error("[RECOVERY_JOURNAL] append failed path=%s", self.path, exc_info=True)
            return False

    def load_pending(self, *, limit: int = 1000) -> List[Dict[str, Any]]:
        with self._lock:
            self._ensure_loaded_locked()
            pending = [dict(r) for r in self._records.values() if r.get("status", "pending") == "pending"]
        return pending[-max(1, int(limit)):]

    def acknowledge_with_status(self, record_ids: List[str]) -> tuple[int, bool]:
        """Acknowledge records and report whether the operation itself succeeded.

        ``count == 0`` is a valid idempotent no-op when all records were already
        acknowledged.  The boolean avoids a costly full-journal scan just to
        distinguish that case from an I/O failure.
        """
        wanted = sorted({str(item) for item in record_ids if item})
        if not wanted:
            return 0, True
        try:
            with self._lock:
                self._ensure_loaded_locked()
                actual = [
                    rid for rid in wanted
                    if rid in self._records and self._records[rid].get("status") == "pending"
                ]
                if not actual:
                    return 0, True
                marker = {"record_type": "ack", "recorded_at": time.time(), "record_ids": actual}
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(self._serialize(marker))
                    handle.flush()
                    os.fsync(handle.fileno())
                now = time.time()
                for rid in actual:
                    record = self._records[rid]
                    record["status"] = "acknowledged"
                    record["updated_at"] = now
                    self._pending_keys.pop(str(record.get("event_key", "")), None)
                    self._pending_count = max(0, self._pending_count - 1)
                self._last_success_at = now
                self._consecutive_write_failures = 0
                self._last_error = ""
                return len(actual), True
        except Exception as exc:
            self._write_failures += 1
            self._consecutive_write_failures += 1
            self._last_failure_at = time.time()
            self._last_error = str(exc)
            logger.error("[RECOVERY_JOURNAL] acknowledge failed path=%s", self.path, exc_info=True)
            return 0, False

    def acknowledge(self, record_ids: List[str]) -> int:
        count, _ok = self.acknowledge_with_status(record_ids)
        return count

    def compact(self, *, retain_acknowledged_seconds: float = JOURNAL_RETAIN_ACKNOWLEDGED_SECONDS) -> int:
        with self._lock:
            self._ensure_loaded_locked()
            return self._compact_locked(retain_acknowledged_seconds=retain_acknowledged_seconds)

    def _write_locked(self, records: List[Dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temporary = f"{self.path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(self._serialize(record))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def _compact_locked(self, *, retain_acknowledged_seconds: float) -> int:
        cutoff = time.time() - max(0.0, float(retain_acknowledged_seconds))
        records = list(self._records.values())
        kept = [
            r for r in records
            if r.get("status") != "acknowledged" or float(r.get("updated_at", 0)) >= cutoff
        ]
        removed = len(records) - len(kept)
        self._write_locked(kept)
        self._loaded = False
        self._ensure_loaded_locked()
        return removed

    def health_snapshot(self) -> Dict[str, Any]:
        return {
            "component": "recovery_journal",
            "state": "failed" if self._consecutive_write_failures else "running",
            "pending": self.pending_count,
            "write_failures": int(self._write_failures),
            "consecutive_write_failures": int(self._consecutive_write_failures),
            "last_error": self._last_error,
            "last_success_at": self._last_success_at,
            "last_failure_at": self._last_failure_at,
        }


class RecoverySpooler:
    """Dedicated durable-writer lane so EventBus, SessionActor and Tk never fsync."""

    def __init__(self, journal: RecoveryJournal, *, max_queue_size: int = 4096):
        self.journal = journal
        self._queue: queue.Queue = queue.Queue(maxsize=max(128, int(max_queue_size)))
        self._stop = Event()
        self._closed = Event()
        self._write_failures = 0
        self._consecutive_write_failures = 0
        self._thread = Thread(target=self._run, name="RecoveryJournalWriter", daemon=False)
        self._thread.start()

    def submit(self, reason: str, event_name: str, event: Any) -> bool:
        if self._closed.is_set():
            return False
        try:
            self._queue.put(("append", reason, event_name, event), timeout=0.05)
            return True
        except queue.Full:
            # Emergency fallback is intentionally outside every EventBus/session lock.
            logger.critical("[RECOVERY_SPOOL] queue full; using synchronous emergency write")
            return self.journal.append(reason, event_name, event)

    def submit_durable(
        self, reason: str, event_name: str, event: Any, *, timeout: float = 5.0
    ) -> bool:
        """Return success only after the journal append has reached fsync.

        Final spill is an exceptional path. Waiting for the dedicated writer keeps
        fsync off EventBus/session locks while closing the crash window where an
        in-memory spool enqueue was previously mistaken for durable persistence.
        """
        if self._closed.is_set():
            return self.journal.append(reason, event_name, event)
        done = Event()
        result: Dict[str, bool] = {}
        try:
            self._queue.put(("append_durable", reason, event_name, event, done, result), timeout=0.05)
        except queue.Full:
            logger.critical("[RECOVERY_SPOOL] durable queue full; using synchronous emergency write")
            return self.journal.append(reason, event_name, event)
        if done.wait(timeout=max(0.1, float(timeout))):
            return bool(result.get("ok", False))
        logger.error("[RECOVERY_SPOOL] durable append timeout; using idempotent direct fallback")
        return self.journal.append(reason, event_name, event)

    def acknowledge(self, record_ids: List[str]) -> bool:
        ids = [str(item) for item in record_ids if item]
        if not ids or self._closed.is_set():
            return False
        try:
            self._queue.put(("ack", ids), timeout=0.05)
            return True
        except queue.Full:
            logger.critical("[RECOVERY_SPOOL] ACK queue full; using synchronous emergency write")
            _count, ok = self.journal.acknowledge_with_status(ids)
            return ok

    def acknowledge_durable(self, record_ids: List[str], *, timeout: float = 5.0) -> bool:
        """Idempotently ACK records and wait until the ACK marker is fsynced."""
        ids = [str(item) for item in record_ids if item]
        if not ids:
            return True
        if self._closed.is_set():
            _count, ok = self.journal.acknowledge_with_status(ids)
            return ok
        done = Event()
        result: Dict[str, bool] = {}
        try:
            self._queue.put(("ack_durable", ids, done, result), timeout=0.05)
        except queue.Full:
            logger.critical("[RECOVERY_SPOOL] durable ACK queue full; using synchronous emergency write")
            _count, ok = self.journal.acknowledge_with_status(ids)
            return ok
        if done.wait(timeout=max(0.1, float(timeout))):
            return bool(result.get("ok", False))
        logger.error("[RECOVERY_SPOOL] durable ACK timeout; using idempotent direct fallback")
        _count, ok = self.journal.acknowledge_with_status(ids)
        return ok

    def _run(self) -> None:
        while True:
            if self._stop.is_set() and self._queue.empty():
                self._closed.set()
                return
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if item[0] in ("append", "append_durable"):
                    if item[0] == "append_durable":
                        _, reason, event_name, event, done, result = item
                    else:
                        _, reason, event_name, event = item
                        done = None
                        result = None
                    ok = self.journal.append(reason, event_name, event)
                    if not ok:
                        self._write_failures += 1
                        self._consecutive_write_failures += 1
                    else:
                        self._consecutive_write_failures = 0
                    if result is not None:
                        result["ok"] = bool(ok)
                    if done is not None:
                        done.set()
                elif item[0] in ("ack", "ack_durable"):
                    if item[0] == "ack_durable":
                        _, record_ids, done, result = item
                    else:
                        _, record_ids = item
                        done = None
                        result = None
                    _count, ok = self.journal.acknowledge_with_status(record_ids)
                    if not ok:
                        self._write_failures += 1
                        self._consecutive_write_failures += 1
                    else:
                        # Includes idempotent re-ACK (count == 0).
                        self._consecutive_write_failures = 0
                    if result is not None:
                        result["ok"] = bool(ok)
                    if done is not None:
                        done.set()
            except Exception:
                self._write_failures += 1
                self._consecutive_write_failures += 1
                logger.error("[RECOVERY_SPOOL] writer task failed", exc_info=True)
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return self._queue.unfinished_tasks == 0

    def close(self, *, drain: bool = True, timeout: float = 5.0) -> bool:
        if not drain:
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
                else:
                    self._queue.task_done()
        elif not self.flush(timeout=max(0.1, timeout * 0.8)):
            logger.error("[RECOVERY_SPOOL] drain timeout pending=%d", self._queue.qsize())
        self._stop.set()
        if current_thread() is not self._thread:
            self._thread.join(timeout=max(0.1, timeout))
        self._closed.set()
        return not self._thread.is_alive()

    def metrics(self) -> Dict[str, Any]:
        return {
            "component": "recovery_spooler",
            "state": "failed" if self._consecutive_write_failures else "running",
            "pending_tasks": self._queue.qsize(),
            "write_failures": int(self._write_failures),
            "consecutive_write_failures": int(self._consecutive_write_failures),
            "worker_alive": self._thread.is_alive(),
        }




class KeyedLockPool:
    """Stable per-key locks; callers serialize one utterance transaction at a time."""

    def __init__(self):
        self._guard = Lock()
        self._locks: Dict[Any, Lock] = {}

    def get(self, key: Any) -> Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = Lock()
                self._locks[key] = lock
            return lock

    def discard(self, key: Any) -> None:
        with self._guard:
            self._locks.pop(key, None)

    def clear(self) -> None:
        with self._guard:
            self._locks.clear()


@dataclass
class AudioCursorState:
    received_sample: int = 0
    processed_sample: int = 0
    decoded_sample: int = 0
    confirmed_sample: int = 0
    retained_from_sample: int = 0

    def update(
        self,
        *,
        received: int = -1,
        processed: int = -1,
        decoded: int = -1,
        confirmed: int = -1,
        retention_samples: int = 0,
    ) -> None:
        if received >= 0:
            self.received_sample = max(self.received_sample, int(received))
        if processed >= 0:
            self.processed_sample = max(self.processed_sample, int(processed))
        if decoded >= 0:
            self.decoded_sample = max(self.decoded_sample, int(decoded))
        if confirmed >= 0:
            self.confirmed_sample = max(self.confirmed_sample, int(confirmed))
        self.retained_from_sample = max(
            0, self.confirmed_sample - max(0, int(retention_samples))
        )


class PipelineHealth:
    NORMAL = "normal"
    WARNING = "warning"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    STOPPED = "stopped"
    _SEVERITY = {NORMAL: 0, WARNING: 1, DEGRADED: 2, CRITICAL: 3, STOPPED: 4}

    def __init__(self, warning: int = 20, degraded: int = 50, critical: int = 100):
        self.warning = int(warning)
        self.degraded = int(degraded)
        self.critical = int(critical)
        self.state = self.NORMAL
        self.last_transition_at = time.time()
        self.reason = ""
        self._lock = Lock()

    def evaluate(
        self,
        pending_finals: int,
        oldest_final_age_ms: int = 0,
        recovery_pending: int = 0,
        component_snapshots: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        value = max(int(pending_finals), int(recovery_pending))
        queue_state = (
            self.CRITICAL if value >= self.critical
            else self.DEGRADED if value >= self.degraded
            else self.WARNING if value >= self.warning
            else self.NORMAL
        )
        if oldest_final_age_ms >= 30000 and queue_state == self.NORMAL:
            queue_state = self.WARNING
        chosen = queue_state
        component_reason = ""
        for snapshot in component_snapshots or []:
            raw_state = str(snapshot.get("state", self.NORMAL)).lower()
            mapped = {
                "failed": self.CRITICAL,
                "critical": self.CRITICAL,
                "degraded": self.DEGRADED,
                "warning": self.WARNING,
                "stopped": self.STOPPED,
                "running": self.NORMAL,
                "normal": self.NORMAL,
                "starting": self.NORMAL,
            }.get(raw_state, self.WARNING)
            if self._SEVERITY.get(mapped, 1) > self._SEVERITY.get(chosen, 0):
                chosen = mapped
                component_reason = f"component={snapshot.get('component', 'unknown')} state={raw_state}"
        reason = (
            component_reason
            or f"pending_finals={pending_finals} recovery_pending={recovery_pending} oldest_ms={oldest_final_age_ms}"
        )
        with self._lock:
            if chosen != self.state:
                self.state = chosen
                self.last_transition_at = time.time()
            self.reason = reason
            return self.state

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self.state,
                "reason": self.reason,
                "last_transition_at": self.last_transition_at,
            }


def _is_final_event(event: Any) -> bool:
    return bool(getattr(event, "is_final", False) and getattr(event, "complete", True))


def _preview_event_key(event_name: str, event: Any) -> tuple[Any, ...]:
    return (
        str(event_name),
        getattr(event, "session_id", ""),
        getattr(event, "utterance_id", 0),
        type(event).__name__,
    )


class _EventSubscriberWorker:
    def __init__(
        self,
        event_name: str,
        callback: Callable[[Any], None],
        *,
        max_pending_finals: int,
        recovery_spill: Optional[Callable[[str, str, Any], bool]],
        session_guard: Optional[Callable[[Any], bool]],
        final_burst: int,
    ):
        self.event_name = str(event_name)
        self.callback = callback
        self._condition = Condition(Lock())
        self._finals: deque[Any] = deque()
        self._previews: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        self._accepting = True
        self._drain = True
        self._max_pending_finals = max(8, int(max_pending_finals))
        self._spill = recovery_spill
        self._session_guard = session_guard
        self._final_burst = max(1, int(final_burst))
        self._finals_since_preview = 0
        self._thread = Thread(
            target=self._run,
            name=f"SubtitleEventSubscriber-{self.event_name}",
            daemon=True,
        )
        self._thread.start()

    def _spill_final(self, reason: str, event: Any) -> bool:
        if not _is_final_event(event) or self._spill is None:
            return False
        return bool(self._spill(reason, self.event_name, event))

    def enqueue(self, event: Any, *, timeout: float = 0.0) -> bool:
        del timeout
        spill_reason = ""
        with self._condition:
            if not self._accepting:
                spill_reason = "subscriber_not_accepting"
            elif _is_final_event(event):
                if len(self._finals) >= self._max_pending_finals:
                    spill_reason = "subscriber_final_backlog"
                else:
                    self._finals.append(event)
            else:
                key = _preview_event_key(self.event_name, event)
                self._previews[key] = event
                self._previews.move_to_end(key)
            if not spill_reason:
                self._condition.notify()
                return True
        self._spill_final(spill_reason, event)
        return False

    def _next_locked(self) -> Any:
        if self._previews and self._finals_since_preview >= self._final_burst:
            self._finals_since_preview = 0
            _, event = self._previews.popitem(last=False)
            return event
        if self._finals:
            self._finals_since_preview += 1
            return self._finals.popleft()
        if self._previews:
            self._finals_since_preview = 0
            _, event = self._previews.popitem(last=False)
            return event
        return None

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._accepting and not self._finals and not self._previews:
                    self._condition.wait(timeout=EVENT_SUBSCRIBER_CONDITION_TIMEOUT)
                abandoned: List[Any] = []
                close_without_drain = False
                if not self._accepting:
                    if not self._drain:
                        abandoned = list(self._finals)
                        self._finals.clear()
                        self._previews.clear()
                        item = None
                        close_without_drain = True
                    elif not self._finals and not self._previews:
                        return
                    else:
                        item = self._next_locked()
                else:
                    item = self._next_locked()
            if close_without_drain:
                for abandoned_item in abandoned:
                    self._spill_final("subscriber_close_without_drain", abandoned_item)
                return
            if item is None:
                continue
            if self._session_guard is not None and not self._session_guard(item):
                self._spill_final("subscriber_guard_rejected", item)
                continue
            try:
                self.callback(item)
            except Exception:
                logger.error(
                    "[SESSION_EVENT] event=%s callback=%r failed",
                    self.event_name,
                    self.callback,
                    exc_info=True,
                )
                self._spill_final("subscriber_callback_failed", item)

    def close(self, *, drain: bool, timeout: float) -> bool:
        with self._condition:
            self._accepting = False
            self._drain = bool(drain)
            pending_if_no_drain = list(self._finals) if not drain else []
            if not drain:
                self._finals.clear()
                self._previews.clear()
            self._condition.notify_all()
        for event in pending_if_no_drain:
            self._spill_final("subscriber_close_without_drain", event)
        if current_thread() is not self._thread:
            self._thread.join(timeout=max(0.0, float(timeout)))
        if self._thread.is_alive():
            with self._condition:
                remaining = list(self._finals)
                self._finals.clear()
            for event in remaining:
                self._spill_final("subscriber_close_timeout", event)
        return not self._thread.is_alive()

    def metrics(self) -> Dict[str, Any]:
        with self._condition:
            oldest_final_age_ms = 0
            if self._finals:
                emitted = float(getattr(self._finals[0], "emitted_at", time.monotonic()))
                oldest_final_age_ms = max(0, int((time.monotonic() - emitted) * 1000))
            return {
                "pending_finals": len(self._finals),
                "pending_previews": len(self._previews),
                "oldest_final_age_ms": oldest_final_age_ms,
                "worker_alive": self._thread.is_alive(),
            }


class SessionEventBus:
    RUNNING = "running"
    DRAINING = "draining"
    CLOSED = "closed"

    def __init__(
        self,
        max_queue_size: int = 512,
        *,
        max_pending_finals: int = 200,
        recovery_path: str = "",
        final_burst: int = 8,
    ):
        self._condition = Condition(Lock())
        self._subscribers: Dict[str, Dict[Callable[[Any], None], _EventSubscriberWorker]] = {}
        self._state = self.RUNNING
        self._finals: deque[tuple[str, Any]] = deque()
        self._previews: OrderedDict[tuple[Any, ...], tuple[str, Any]] = OrderedDict()
        self._max_previews = max(32, int(max_queue_size))
        self._max_pending_finals = max(8, int(max_pending_finals))
        self._journal = RecoveryJournal(recovery_path) if recovery_path else None
        self._recovery_spooler = RecoverySpooler(self._journal) if self._journal is not None else None
        self._final_burst = max(1, int(final_burst))
        self._finals_since_preview = 0
        self._dispatcher = Thread(
            target=self._dispatch_loop,
            name="SubtitleSessionEvents",
            daemon=False,
        )
        try:
            self._dispatcher.start()
        except BaseException:
            self._state = self.CLOSED
            if self._recovery_spooler is not None:
                self._recovery_spooler.close(drain=True, timeout=2.0)
            raise

    @property
    def recovery_journal(self) -> Optional[RecoveryJournal]:
        return self._journal

    def spill_final(self, reason: str, event_name: str, event: Any) -> bool:
        if not _is_final_event(event) or self._journal is None:
            return False
        if self._recovery_spooler is not None:
            return self._recovery_spooler.submit_durable(reason, event_name, event)
        # Late shutdown fallback; never invoked while holding EventBus/session locks.
        return self._journal.append(reason, event_name, event)

    def acknowledge_recovery(self, record_ids: List[str]) -> bool:
        if self._journal is None:
            return False
        if self._recovery_spooler is not None:
            return self._recovery_spooler.acknowledge_durable(record_ids)
        # Idempotent ACK: zero newly acknowledged rows is still success.
        _count, ok = self._journal.acknowledge_with_status(record_ids)
        return ok

    def pending_recovery_records(self, *, limit: int = 1000) -> List[Dict[str, Any]]:
        if self._journal is None:
            return []
        if self._recovery_spooler is not None:
            self._recovery_spooler.flush(timeout=5.0)
        return self._journal.load_pending(limit=limit)

    def subscribe(
        self,
        event_name: str,
        callback: Callable[[Any], None],
        *,
        session_guard: Optional[Callable[[Any], bool]] = None,
    ) -> None:
        event_name = str(event_name)
        with self._condition:
            if self._state != self.RUNNING:
                raise RuntimeError("SessionEventBus 已停止接收订阅")
            workers = self._subscribers.setdefault(event_name, {})
            if callback not in workers:
                workers[callback] = _EventSubscriberWorker(
                    event_name,
                    callback,
                    max_pending_finals=self._max_pending_finals,
                    recovery_spill=self.spill_final,
                    session_guard=session_guard,
                    final_burst=self._final_burst,
                )

    def unsubscribe(self, event_name: str, callback: Callable[[Any], None]) -> None:
        with self._condition:
            worker = self._subscribers.get(str(event_name), {}).pop(callback, None)
        if worker is not None:
            worker.close(drain=False, timeout=0.5)

    def publish(self, event_name: str, event: Any) -> bool:
        name = str(event_name)
        spill_reason = ""
        accepted = False
        with self._condition:
            if self._state != self.RUNNING:
                spill_reason = "eventbus_not_running"
            elif _is_final_event(event):
                if len(self._finals) >= self._max_pending_finals:
                    spill_reason = "central_final_backlog"
                else:
                    self._finals.append((name, event))
                    accepted = True
            else:
                key = _preview_event_key(name, event)
                self._previews[key] = (name, event)
                self._previews.move_to_end(key)
                while len(self._previews) > self._max_previews:
                    self._previews.popitem(last=False)
                accepted = True
            if accepted:
                self._condition.notify()
        if spill_reason:
            self.spill_final(spill_reason, name, event)
            if spill_reason == "central_final_backlog":
                logger.error(
                    "[SESSION_FINAL_BACKLOG] event=%s pending=%d",
                    name,
                    self._max_pending_finals,
                )
        return accepted

    def _next_locked(self) -> Optional[tuple[str, Any]]:
        if self._previews and self._finals_since_preview >= self._final_burst:
            self._finals_since_preview = 0
            _, item = self._previews.popitem(last=False)
            return item
        if self._finals:
            self._finals_since_preview += 1
            return self._finals.popleft()
        if self._previews:
            self._finals_since_preview = 0
            _, item = self._previews.popitem(last=False)
            return item
        return None

    def _dispatch_loop(self) -> None:
        while True:
            with self._condition:
                while self._state == self.RUNNING and not self._finals and not self._previews:
                    self._condition.wait(timeout=0.5)
                if self._state in (self.DRAINING, self.CLOSED) and not self._finals and not self._previews:
                    self._state = self.CLOSED
                    self._condition.notify_all()
                    return
                item = self._next_locked()
                workers = list(self._subscribers.get(item[0], {}).values()) if item else []
            if item is None:
                continue
            event_name, event = item
            if not workers:
                self.spill_final("no_subscriber", event_name, event)
                continue
            for worker in workers:
                if not worker.enqueue(event):
                    logger.error(
                        "[SESSION_SUBSCRIBER_REJECT] event=%s callback=%r",
                        event_name,
                        worker.callback,
                    )

    def metrics(self) -> Dict[str, Any]:
        with self._condition:
            subscribers = {
                name: [worker.metrics() for worker in workers.values()]
                for name, workers in self._subscribers.items()
            }
            central_oldest = 0
            if self._finals:
                emitted = float(getattr(self._finals[0][1], "emitted_at", time.monotonic()))
                central_oldest = max(0, int((time.monotonic() - emitted) * 1000))
            spooler_metrics = self._recovery_spooler.metrics() if self._recovery_spooler is not None else {}
            recovery_pending = (self._journal.pending_count if self._journal is not None else 0) + int(
                spooler_metrics.get("pending_tasks", 0)
            )
            subscriber_finals = sum(
                int(item.get("pending_finals", 0))
                for group in subscribers.values()
                for item in group
            )
            subscriber_previews = sum(
                int(item.get("pending_previews", 0))
                for group in subscribers.values()
                for item in group
            )
            subscriber_oldest = max(
                [
                    int(item.get("oldest_final_age_ms", 0))
                    for group in subscribers.values()
                    for item in group
                ]
                or [0]
            )
            return {
                "state": self._state,
                "central_pending_finals": len(self._finals),
                "subscriber_pending_finals": subscriber_finals,
                "pending_finals": len(self._finals) + subscriber_finals,
                "pending_previews": len(self._previews) + subscriber_previews,
                "oldest_final_age_ms": max(central_oldest, subscriber_oldest),
                "recovery_pending": recovery_pending,
                "dispatcher_alive": self._dispatcher.is_alive(),
                "recovery_spooler": spooler_metrics,
                "subscribers": subscribers,
            }

    def close(self, *, drain: bool = True, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.1, float(timeout))
        spill: List[tuple[str, Any]] = []
        with self._condition:
            if self._state != self.CLOSED:
                self._state = self.DRAINING
                if not drain:
                    spill = list(self._finals)
                    self._previews.clear()
                    self._finals.clear()
                self._condition.notify_all()
        for event_name, event in spill:
            self.spill_final("eventbus_close_without_drain", event_name, event)
        if current_thread() is not self._dispatcher:
            self._dispatcher.join(timeout=max(0.0, deadline - time.monotonic()))
        dispatcher_stopped = not self._dispatcher.is_alive()
        with self._condition:
            workers = [w for group in self._subscribers.values() for w in group.values()]
            self._subscribers.clear()
            self._state = self.CLOSED
            self._condition.notify_all()
        workers_stopped = True
        for worker in workers:
            stopped = worker.close(
                drain=drain,
                timeout=max(0.0, deadline - time.monotonic()),
            )
            workers_stopped = stopped and workers_stopped
        spooler_stopped = True
        if self._recovery_spooler is not None:
            # Recovery durability is independent from business-event draining:
            # even close(drain=False) must persist every Final already spilled.
            spooler_stopped = self._recovery_spooler.close(
                drain=True,
                timeout=max(0.1, deadline - time.monotonic()),
            )
        return dispatcher_stopped and workers_stopped and spooler_stopped


# .pyw 下 sys.stdout/stderr 为 None，防止第三方库 print 崩溃
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')


def configure_hidden_windows_child_processes() -> None:
    """Use the windowless Python executable for multiprocessing children."""
    if (
        not sys.platform.startswith("win")
        or getattr(sys, "frozen", False)
        or "__compiled__" in globals()
    ):
        return

    candidates = []
    executable_dir = os.path.dirname(os.path.abspath(sys.executable))
    candidates.append(os.path.join(executable_dir, "pythonw.exe"))
    candidates.append(os.path.join(sys.exec_prefix, "pythonw.exe"))
    for candidate in candidates:
        if os.path.isfile(candidate):
            multiprocessing.set_executable(candidate)
            return


configure_hidden_windows_child_processes()


# ================== 路径与日志（兼容源码、安装版和便携版）==================
APP_NAME = "NemoSubtitle"
APP_VERSION = "0.9.22-qwen3-asr-official-streaming-rtx3090-gpu1"
_source_dir = os.path.dirname(os.path.realpath(__file__))
_is_compiled = bool(getattr(sys, "frozen", False) or "__compiled__" in globals())
_resource_dir = (
    os.path.dirname(os.path.abspath(sys.executable)) if _is_compiled else _source_dir
)
_portable_mode = os.path.isfile(os.path.join(_resource_dir, "portable.flag"))


def _default_user_data_dir() -> str:
    if sys.platform.startswith("win"):
        base = os.environ.get(
            "LOCALAPPDATA",
            os.path.join(os.path.expanduser("~"), "AppData", "Local"),
        )
        return os.path.join(base, APP_NAME)
    base = os.environ.get(
        "XDG_DATA_HOME",
        os.path.join(os.path.expanduser("~"), ".local", "share"),
    )
    return os.path.join(base, APP_NAME)


def _directory_is_writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(prefix=".write-test-", dir=path, delete=True)
        handle.close()
        return True
    except Exception:
        return False


_preferred_data_dir = _resource_dir if _portable_mode else _default_user_data_dir()
if _directory_is_writable(_preferred_data_dir):
    _data_dir = _preferred_data_dir
else:
    _data_dir = os.path.join(tempfile.gettempdir(), APP_NAME)
    os.makedirs(_data_dir, exist_ok=True)

_logs_dir = os.path.join(_data_dir, "logs")
_models_dir = os.path.join(_data_dir, "models")
_legacy_models_dir = os.path.join(_resource_dir, "models")
os.makedirs(_logs_dir, exist_ok=True)
os.makedirs(_models_dir, exist_ok=True)
# Source-mode users commonly already have ASR models in Hugging Face's default
# cache (for example ~/.cache/huggingface/hub). Do not redirect HF_HOME in
# source mode, otherwise a valid existing snapshot is falsely treated as missing.
# Installed builds keep their isolated per-user cache; an explicit user HF_HOME
# always wins because setdefault() never overwrites it.
if _is_compiled and not _portable_mode:
    os.environ.setdefault("HF_HOME", os.path.join(_data_dir, "huggingface"))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
_script_dir = _resource_dir
_log_file = os.path.join(_logs_dir, "subtitle.log")
_settings_path = os.path.join(_data_dir, "settings.json")
_settings_lock = Lock()
























_is_main_process = multiprocessing.current_process().name == "MainProcess"
_logging_handlers: List[logging.Handler] = [logging.NullHandler()]
if _is_main_process:
    try:
        _logging_handlers = [
            RotatingFileHandler(
                _log_file,
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        ]
    except Exception:
        # A read-only installation directory must never make a .pyw process
        # disappear before Tk can show an actionable error.
        _logging_handlers = [logging.NullHandler()]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(processName)s/%(threadName)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_logging_handlers,
)
logger = logging.getLogger(__name__)


def _load_application_settings() -> Dict[str, Any]:
    """Load non-sensitive UI/runtime preferences and quarantine malformed files."""
    with _settings_lock:
        try:
            with open(_settings_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                raise ValueError("设置文件顶层必须是 JSON 对象")
            return payload
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning("设置文件损坏或不可读，将使用默认值：%s", exc)
            try:
                suffix = time.strftime("%Y%m%d-%H%M%S")
                os.replace(_settings_path, f"{_settings_path}.corrupt-{suffix}")
            except OSError:
                logger.debug("损坏设置文件隔离失败", exc_info=True)
            return {}


def _save_application_settings(payload: Dict[str, Any]) -> None:
    """Atomically persist whitelisted preferences; never write model text or secrets."""
    with _settings_lock:
        os.makedirs(os.path.dirname(_settings_path), exist_ok=True)
        temporary = f"{_settings_path}.{os.getpid()}.tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, _settings_path)
        except Exception:
            logger.error("设置保存失败", exc_info=True)
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise


def _apply_persisted_settings_to_args(
    args: argparse.Namespace,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Apply saved audio/display preferences while preserving explicit CLI overrides."""
    if not payload:
        return {}
    candidate = argparse.Namespace(**vars(args))
    explicit = set(getattr(args, "_explicit_dests", set()) or set())
    audio = payload.get("audio", {})
    display = payload.get("display", {})
    if not isinstance(audio, dict) or not isinstance(display, dict):
        logger.warning("设置文件结构无效，已忽略")
        return {}
    direct_audio = {
        "vad_threshold": float,
        "min_silence_duration": float,
        "min_speech_duration": float,
        "speech_preroll_ms": int,
        "max_utterance_seconds": float,
        "forced_segment_overlap_ms": int,
        "endpoint_punctuation_hold_ms": int,
        "endpoint_min_utterance_ms": int,
        "max_drain_chunks": int,
    }
    try:
        for key, converter in direct_audio.items():
            if key in audio and key not in explicit:
                setattr(candidate, key, converter(audio[key]))
        if "enable_hybrid_endpoint" in audio and "disable_hybrid_endpoint" not in explicit:
            candidate.disable_hybrid_endpoint = not bool(audio["enable_hybrid_endpoint"])
        validate_args(candidate)
    except Exception as exc:
        logger.warning("已保存的运行参数无效，整组忽略：%s", exc)
        return {}
    for key in direct_audio:
        setattr(args, key, getattr(candidate, key))
    args.disable_hybrid_endpoint = candidate.disable_hybrid_endpoint
    safe_display = {
        "source_font_size": max(12, min(72, int(display.get("source_font_size", 26)))),
        "opacity": max(0.50, min(1.00, float(display.get("opacity", 0.95)))),
        "topmost": bool(display.get("topmost", True)),
    }
    logger.info("已恢复保存的实时字幕设置")
    return safe_display


QWEN3_ASR_MODEL_ID = "Qwen/Qwen3-ASR-1.7B"
QWEN3_ASR_DEFAULT_LANGUAGE = ""  # official streaming demo uses automatic language detection
QWEN3_ASR_DEFAULT_SERVER_URL = "http://127.0.0.1:8000"
QWEN3_ASR_DEFAULT_CHUNK_SIZE_SEC = 1.0
QWEN3_ASR_DEFAULT_UNFIXED_CHUNK_NUM = 4
QWEN3_ASR_DEFAULT_UNFIXED_TOKEN_NUM = 5
QWEN3_ASR_DEFAULT_PUSH_INTERVAL_MS = 500  # official browser demo pushes 500 ms PCM blocks
QWEN3_ASR_RTX3090_GPU_INDEX = "1"  # nvidia-smi physical index: user-verified RTX 3090
QWEN3_ASR_RTX3090_DEVICE_ORDER = "PCI_BUS_ID"  # align CUDA numeric ordinals with NVIDIA physical ordering
QWEN3_ASR_RTX3090_GPU_MEMORY_UTILIZATION = 0.90  # Qwen official streaming README uses 0.9
QWEN3_ASR_RTX3090_CPU_OFFLOAD_GB = 0.0  # no CPU offload needed on 24GB card; WSL/vLLM 0.14 pin-memory limitation
QWEN3_ASR_RTX3070_GPU_MEMORY_UTILIZATION = 0.85  # deployment profile for 8GB RTX 3070 Laptop; official vLLM argument
QWEN3_ASR_RTX3070_CPU_OFFLOAD_GB = 0.0  # vLLM 0.14.0 disables pin_memory on WSL; V1 CPU offload therefore cannot use UVA safely
QWEN3_ASR_RTX3070_MAX_NUM_SEQS = 1  # Qwen streaming is single-stream/no-batch
QWEN3_ASR_RTX3070_MAX_MODEL_LEN = -1  # vLLM 0.14 auto-fit context length to available KV memory
QWEN3_ASR_RTX3070_ENFORCE_EAGER = True  # documented vLLM memory-saving option
QWEN3_ASR_RTX3070_LIMIT_AUDIO_ITEMS = 1  # official Qwen streaming request has one audio item

# ================== Qwen3-ASR 官方 Streaming 运行时 ==================
QWEN_RUNTIME_CONFIG_PATH = os.path.join(_data_dir, "qwen-runtime.json")
# v0.9.18: Qwen's official demo_streaming API/state machine remains unchanged.
# A real-file bootstrap injects only documented vLLM 0.14 memory-conservation
# kwargs for an 8GB single-stream deployment. CPU offload stays disabled on WSL;
# no safety checks or Qwen inference internals are bypassed.
QWEN_OFFICIAL_STREAMING_MODULE = "qwen_asr.cli.demo_streaming"
QWEN_OFFICIAL_8GB_MEMORY_BOOTSTRAP = r'''
import vllm

# Top-level import preserves Qwen's normal vLLM model registration under spawn.
from qwen_asr.cli import demo_streaming as _official_demo


def main():
    original_llm = vllm.LLM

    def _llm_8gb_memory_fit(*args, **kwargs):
        # vLLM 0.14 documented deployment knobs only. Qwen streaming logic is untouched.
        kwargs["max_num_seqs"] = 1
        kwargs["max_model_len"] = -1
        kwargs["enforce_eager"] = True
        kwargs["limit_mm_per_prompt"] = {"audio": 1}
        return original_llm(*args, **kwargs)

    vllm.LLM = _llm_8gb_memory_fit
    try:
        _official_demo.main()
    finally:
        vllm.LLM = original_llm


if __name__ == "__main__":
    main()
'''.strip()


class PreparedRuntimeLauncher:
    """只负责读取前置检测结果并启动 WSL sidecar；不安装、不下载、不做完整环境自检。"""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.root: Optional[tk.Tk] = None
        self.status_var: Optional[tk.StringVar] = None
        self.detail_widget: Optional[tk.Text] = None
        self.sidecar_process: Optional[subprocess.Popen] = None
        self.sidecar_owned = False
        self.sidecar_reused = False
        self.wsl_was_running_before_start = False
        self.wsl_distro = str(getattr(args, "wsl_distro", "") or "").strip()
        self.wsl_python = str(getattr(args, "wsl_python", "") or "").strip()
        self.remote_pid_file = ""
        self.remote_wrapper_file = ""
        self.instance_token = uuid.uuid4().hex
        self.sidecar_log = os.path.join(_logs_dir, "qwen-sidecar.log")
        self.runtime_config_path = QWEN_RUNTIME_CONFIG_PATH
        self._cancelled = False

    @staticmethod
    def _decode_output(raw: bytes) -> str:
        if not raw:
            return ""
        if raw.count(b"\x00") > max(2, len(raw) // 8):
            for encoding in ("utf-16-le", "utf-16"):
                try:
                    return raw.decode(encoding).replace("\x00", "").strip()
                except UnicodeDecodeError:
                    pass
        for encoding in ("utf-8", "utf-8-sig", "cp932", "mbcs"):
            try:
                return raw.decode(encoding).replace("\x00", "").strip()
            except (UnicodeDecodeError, LookupError):
                pass
        return raw.decode("utf-8", errors="replace").replace("\x00", "").strip()

    @staticmethod
    def _hidden_kwargs() -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            kwargs["startupinfo"] = startupinfo
        return kwargs

    def _open_ui(self) -> None:
        if self.root is not None:
            return
        root = tk.Tk()
        root.title("Qwen3 实时字幕 - 启动 ASR 服务")
        root.geometry("680x300")
        root.resizable(True, True)
        root.protocol("WM_DELETE_WINDOW", self._request_cancel)
        self.status_var = tk.StringVar(value="正在启动 Qwen3-ASR WSL sidecar...")
        tk.Label(root, textvariable=self.status_var, anchor="w", font=("Microsoft YaHei UI", 11, "bold")).pack(
            fill="x", padx=16, pady=(16, 8)
        )
        progress = ttk.Progressbar(root, mode="indeterminate")
        progress.pack(fill="x", padx=16, pady=(0, 10))
        progress.start(12)
        detail = tk.Text(root, height=10, wrap="word", state="disabled")
        detail.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        self.root = root
        self.detail_widget = detail
        root.update_idletasks()
        root.update()

    def _request_cancel(self) -> None:
        if messagebox.askyesno("取消启动", "确定取消 Qwen3-ASR 服务启动吗？", parent=self.root):
            self._cancelled = True
            self.close()
            if self.root is not None:
                try:
                    self.root.destroy()
                except tk.TclError:
                    pass
                self.root = None

    def _pump_ui(self) -> None:
        if self.root is None:
            return
        try:
            self.root.update_idletasks()
            self.root.update()
        except tk.TclError:
            self._cancelled = True
            self.root = None

    def _status(self, text: str) -> None:
        logger.info("[RUNTIME_LAUNCH] %s", text)
        if self.status_var is not None:
            self.status_var.set(text)
        widget = self.detail_widget
        if widget is not None:
            try:
                widget.config(state="normal")
                widget.insert("end", str(text).rstrip() + "\n")
                widget.see("end")
                widget.config(state="disabled")
            except tk.TclError:
                pass
        self._pump_ui()

    def _wsl_prefix(self) -> List[str]:
        command = ["wsl.exe"]
        if self.wsl_distro:
            command += ["--distribution", self.wsl_distro]
        return command

    def _run_capture(self, command: List[str], *, timeout: float = 30.0, input_bytes: bytes = b"") -> tuple[int, str]:
        try:
            completed = subprocess.run(
                command,
                input=input_bytes if input_bytes else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                **self._hidden_kwargs(),
            )
            return int(completed.returncode), self._decode_output(completed.stdout or b"")
        except subprocess.TimeoutExpired as exc:
            return 124, self._decode_output(exc.stdout or b"") + "\n命令执行超时"
        except Exception as exc:
            return 127, str(exc)

    def _run_wsl_capture(self, linux_args: List[str], *, timeout: float = 30.0) -> tuple[int, str]:
        return self._run_capture(self._wsl_prefix() + ["--exec"] + list(linux_args), timeout=timeout)

    def _run_wsl_shell(self, script: str, *, timeout: float = 30.0, input_bytes: bytes = b"") -> tuple[int, str]:
        return self._run_capture(
            self._wsl_prefix() + ["--exec", "sh", "-lc", script],
            timeout=timeout,
            input_bytes=input_bytes,
        )

    @staticmethod
    def _tail_file(path: str, max_lines: int = 30) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return "".join(deque(handle, maxlen=max_lines)).strip()
        except OSError:
            return ""

    def _load_prepared_config(self) -> Dict[str, Any]:
        try:
            with open(self.runtime_config_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _apply_prepared_config(self) -> Dict[str, Any]:
        cfg = self._load_prepared_config()
        explicit = set(getattr(self.args, "_explicit_dests", set()))
        configured_distro = str(cfg.get("wsl_distro", "") or "").strip()
        if "wsl_distro" not in explicit and configured_distro:
            self.args.wsl_distro = configured_distro
        if "wsl_python" not in explicit:
            selected = str(getattr(self.args, "wsl_distro", "") or "").strip()
            if not selected or not configured_distro or selected == configured_distro:
                stored_python = str(cfg.get("wsl_python", "") or "").strip()
                if stored_python:
                    self.args.wsl_python = stored_python
        if "qwen_wsl_hf_home" not in explicit:
            stored_hf = str(cfg.get("qwen_wsl_hf_home", "") or "").strip()
            if stored_hf:
                self.args.qwen_wsl_hf_home = stored_hf
        if "vad_model_path" not in explicit:
            stored_vad = str(cfg.get("vad_model_path", "") or "").strip()
            if stored_vad and os.path.isfile(stored_vad):
                self.args.vad_model_path = stored_vad
        self.wsl_distro = str(getattr(self.args, "wsl_distro", "") or "").strip()
        self.wsl_python = str(getattr(self.args, "wsl_python", "") or "").strip()
        return cfg

    @staticmethod
    def _parse_nvidia_smi_inventory(out: str) -> List[Dict[str, Any]]:
        """Parse the physical GPU inventory reported by NVIDIA's WSL nvidia-smi.

        Retain UUIDs only for diagnostics/inventory correlation. This launcher passes
        numeric GPU indices to CUDA_VISIBLE_DEVICES because vLLM 0.14.0's WSL CUDA
        platform path expects an integer physical device id for this workload. CUDA_DEVICE_ORDER
        is resolved separately so CUDA and NVML/nvidia-smi refer to the same adapter.
        """
        result: List[Dict[str, Any]] = []
        for raw_line in str(out or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = [part.strip() for part in line.split(",", 4)]
            if len(parts) != 5:
                continue
            try:
                index = int(parts[0])
                total_mib = int(float(parts[3]))
                free_mib = int(float(parts[4]))
            except (TypeError, ValueError):
                continue
            result.append({
                "index": index,
                "uuid": parts[1],
                "name": parts[2],
                "total_mib": total_mib,
                "free_mib": free_mib,
                "source": "nvidia-smi",
            })
        return result

    def _query_wsl_gpu_inventory_via_nvidia_smi(self) -> List[Dict[str, Any]]:
        """Enumerate physical WSL GPUs before consulting torch.

        Prefer /usr/lib/wsl/lib/nvidia-smi, NVIDIA's documented WSL location.
        Clear stale visibility variables so this inventory is not accidentally filtered.
        """
        errors: List[str] = []
        for binary in ("/usr/lib/wsl/lib/nvidia-smi", "nvidia-smi"):
            code, out = self._run_wsl_capture([
                "env", "-u", "CUDA_VISIBLE_DEVICES", "-u", "CUDA_DEVICE_ORDER",
                binary,
                "--query-gpu=index,uuid,name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ], timeout=15)
            if code == 0:
                inventory = self._parse_nvidia_smi_inventory(out)
                if inventory:
                    return inventory
            errors.append(f"{binary}: {out[-400:]}")
        logger.warning(
            "[RUNTIME_LAUNCH] physical WSL GPU inventory via nvidia-smi failed: %s",
            " | ".join(errors),
        )
        return []

    def _query_wsl_gpu_inventory_via_torch(self) -> List[Dict[str, Any]]:
        """Fallback CUDA enumeration with stale visibility variables explicitly cleared."""
        if not self.wsl_python:
            return []
        probe = """import json, torch
items=[]
for i in range(torch.cuda.device_count()):
    p=torch.cuda.get_device_properties(i)
    total=int(p.total_memory // (1024*1024))
    free=-1
    try:
        f,t=torch.cuda.mem_get_info(i)
        free=int(f // (1024*1024))
        total=int(t // (1024*1024))
    except Exception:
        pass
    items.append({"index":i,"uuid":"","name":p.name,"total_mib":total,"free_mib":free,"source":"torch"})
print("__QWEN_GPU_JSON__"+json.dumps(items, ensure_ascii=False))"""
        code, out = self._run_wsl_capture([
            "env", "-u", "CUDA_VISIBLE_DEVICES", "-u", "CUDA_DEVICE_ORDER",
            self.wsl_python, "-c", probe,
        ], timeout=30)
        if code != 0:
            logger.warning("[RUNTIME_LAUNCH] torch CUDA inventory failed: %s", out[-1200:])
            return []
        marker = "__QWEN_GPU_JSON__"
        payload = ""
        for line in out.splitlines():
            if line.startswith(marker):
                payload = line[len(marker):].strip()
        if not payload:
            return []
        try:
            raw_items = json.loads(payload)
        except json.JSONDecodeError:
            return []
        result: List[Dict[str, Any]] = []
        if not isinstance(raw_items, list):
            return result
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            try:
                result.append({
                    "index": int(item.get("index")),
                    "uuid": str(item.get("uuid") or ""),
                    "name": str(item.get("name") or "CUDA GPU"),
                    "total_mib": int(item.get("total_mib") or 0),
                    "free_mib": int(item.get("free_mib") if item.get("free_mib") is not None else -1),
                    "source": "torch",
                })
            except (TypeError, ValueError):
                continue
        return result

    def _query_wsl_gpu_inventory(self) -> List[Dict[str, Any]]:
        """Return the broadest reliable GPU inventory visible to this WSL distro."""
        inventory = self._query_wsl_gpu_inventory_via_nvidia_smi()
        if inventory:
            return inventory
        return self._query_wsl_gpu_inventory_via_torch()

    def _verify_wsl_cuda_selection(self, selected: str, device_order: str = "") -> Dict[str, Any]:
        """Verify the exact adapter CUDA will expose as logical cuda:0.

        On mixed laptop+dGPU systems, CUDA's default FASTEST_FIRST ordering can differ
        from the physical index shown by nvidia-smi. vLLM 0.14 still expects a numeric
        CUDA_VISIBLE_DEVICES value, so use CUDA_DEVICE_ORDER=PCI_BUS_ID to make the two
        namespaces agree, then verify the result before launching vLLM.
        """
        if not self.wsl_python or not selected:
            return {}
        probe = """import json, torch
if not torch.cuda.is_available() or torch.cuda.device_count() < 1:
    raise SystemExit("CUDA unavailable after CUDA_VISIBLE_DEVICES selection")
p=torch.cuda.get_device_properties(0)
free,total=torch.cuda.mem_get_info(0)
print("__QWEN_GPU_VERIFY__"+json.dumps({"name":p.name,"total_mib":int(total//(1024*1024)),"free_mib":int(free//(1024*1024))}, ensure_ascii=False))"""
        env_args = ["env"]
        if device_order:
            env_args.append(f"CUDA_DEVICE_ORDER={device_order}")
        else:
            env_args.extend(["-u", "CUDA_DEVICE_ORDER"])
        env_args.append(f"CUDA_VISIBLE_DEVICES={selected}")
        code, out = self._run_wsl_capture(env_args + [self.wsl_python, "-c", probe], timeout=30)
        if code != 0:
            raise RuntimeError(
                "选择的 GPU 无法被 WSL CUDA 使用：CUDA_VISIBLE_DEVICES=" + selected +
                (f" CUDA_DEVICE_ORDER={device_order}" if device_order else "") + "\n\n" + out[-2000:]
            )
        marker = "__QWEN_GPU_VERIFY__"
        payload = ""
        for line in out.splitlines():
            if line.startswith(marker):
                payload = line[len(marker):].strip()
        if not payload:
            raise RuntimeError(
                "GPU 选择验证没有返回 CUDA 设备信息：CUDA_VISIBLE_DEVICES=" + selected
            )
        try:
            item = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GPU 选择验证返回了无效 JSON") from exc
        return {
            "name": str(item.get("name") or "CUDA GPU"),
            "total_mib": int(item.get("total_mib") or 0),
            "free_mib": int(item.get("free_mib") or 0),
            "device_order": str(device_order or ""),
        }

    def _rtx3090_gpu_memory_utilization(self) -> float:
        """Use the Qwen official streaming example memory fraction on RTX 3090."""
        requested = float(getattr(
            self.args,
            "qwen_gpu_memory_utilization",
            QWEN3_ASR_RTX3090_GPU_MEMORY_UTILIZATION,
        ))
        target = float(QWEN3_ASR_RTX3090_GPU_MEMORY_UTILIZATION)
        if abs(requested - target) > 1e-9:
            logger.warning(
                "[RUNTIME_LAUNCH] RTX 3090 GPU1 profile: ignoring stale gpu_memory_utilization=%.3f; using %.3f",
                requested,
                target,
            )
        self.args.qwen_gpu_memory_utilization = target
        return target

    def _rtx3090_cpu_offload_gb(self) -> float:
        """Keep CPU offload disabled for the WSL/vLLM 0.14 official stack."""
        requested = float(getattr(self.args, "qwen_cpu_offload_gb", 0.0) or 0.0)
        if requested > 0.0:
            logger.warning(
                "[RUNTIME_LAUNCH] RTX 3090 24GB 不需要 CPU offload，且 WSL + vLLM 0.14.0 不支持安全的 V1 CPU offload；"
                "忽略 cpu_offload_gb=%.3f，使用 0.0 GiB",
                requested,
            )
        self.args.qwen_cpu_offload_gb = 0.0
        return 0.0

    @staticmethod
    def _is_rtx3090(info: Dict[str, Any]) -> bool:
        name = str(info.get("name") or "").lower()
        total = int(info.get("total_mib") or 0)
        return ("3090" in name) and (22000 <= total <= 26000 or total == 0)

    def _resolve_cuda_visible_devices(self) -> str:
        """Pin Qwen/vLLM to nvidia-smi physical GPU 1 (RTX 3090) safely.

        NVIDIA exposes two independent ordering controls here: nvidia-smi's physical
        index and CUDA's enumeration order. The observed machine has nvidia-smi GPU 1
        = RTX 3090, while CUDA's default order can map numeric 1 to the 3070. Keep the
        vLLM-compatible numeric selector ``1`` but force ``CUDA_DEVICE_ORDER=PCI_BUS_ID``
        and verify that logical cuda:0 is really the RTX 3090 before launch.
        """
        selected = str(QWEN3_ASR_RTX3090_GPU_INDEX)
        requested = str(getattr(self.args, "qwen_cuda_visible_devices", selected) or selected).strip()
        if requested != selected:
            logger.warning(
                "[RUNTIME_LAUNCH] RTX 3090 GPU1 fixed profile: ignoring CUDA_VISIBLE_DEVICES=%s; using physical GPU %s",
                requested, selected,
            )

        inventory = self._query_wsl_gpu_inventory()
        physical: Dict[str, Any] = {}
        if inventory:
            summary = " | ".join(
                "%s:%s %s total=%dMiB free=%dMiB"
                % (
                    item.get("index", "?"),
                    item.get("uuid") or "no-uuid",
                    item.get("name") or "GPU",
                    int(item.get("total_mib") or 0),
                    int(item.get("free_mib") or -1),
                )
                for item in inventory
            )
            logger.info("[RUNTIME_LAUNCH] WSL physical GPU inventory: %s", summary)
            physical = next((item for item in inventory if int(item.get("index", -1)) == int(selected)), None) or {}
            if not physical:
                raise RuntimeError(
                    "固定 RTX 3090 模式验证失败：nvidia-smi 当前没有暴露物理 GPU 1。\n\n"
                    f"WSL 当前 GPU：{summary}\n\n"
                    "请在 PowerShell 执行：\n"
                    "wsl -d Ubuntu -- /usr/lib/wsl/lib/nvidia-smi --query-gpu=index,name,pci.bus_id,memory.total --format=csv,noheader"
                )
            if not self._is_rtx3090(physical):
                raise RuntimeError(
                    "固定 RTX 3090 模式验证失败：nvidia-smi 物理 GPU 1 不是 RTX 3090。\n\n"
                    f"GPU 1：{physical.get('name') or 'unknown'} total={int(physical.get('total_mib') or 0)}MiB\n\n"
                    "程序不会自动回退到 RTX 3070。"
                )

        # Critical: CUDA's default FASTEST_FIRST order can differ from nvidia-smi.
        # vLLM 0.14.0 cannot safely use a GPU UUID in CUDA_VISIBLE_DEVICES on this path,
        # therefore align numeric IDs with PCI ordering and verify the actual cuda:0.
        device_order = str(QWEN3_ASR_RTX3090_DEVICE_ORDER)
        verified = self._verify_wsl_cuda_selection(selected, device_order)
        if not self._is_rtx3090(verified):
            # Collect the default-order result only for a precise diagnostic. Do not use it.
            default_seen: Dict[str, Any] = {}
            try:
                default_seen = self._verify_wsl_cuda_selection(selected, "")
            except Exception:
                pass
            raise RuntimeError(
                "RTX 3090 GPU 映射验证失败，拒绝启动 Qwen/vLLM。\n\n"
                f"nvidia-smi 物理 GPU {selected}：{physical.get('name') or 'RTX 3090'}\n"
                f"CUDA_DEVICE_ORDER={device_order}\n"
                f"CUDA_VISIBLE_DEVICES={selected}\n"
                f"CUDA 实际看到：{verified.get('name') or 'unknown'} total={int(verified.get('total_mib') or 0)}MiB\n"
                f"默认 CUDA 顺序下看到：{default_seen.get('name') or 'unknown'}\n\n"
                "程序不会误用 RTX 3070。请把新的日志发给我继续定位。"
            )

        verified_total = int(verified.get("total_mib") or 0)
        verified_free = int(verified.get("free_mib") or 0)
        util = float(QWEN3_ASR_RTX3090_GPU_MEMORY_UTILIZATION)
        required_mib = int(round(verified_total * util)) if verified_total else 0
        if verified_total and verified_free and verified_free < required_mib + 64:
            raise RuntimeError(
                "RTX 3090 当前空闲显存不足以按 gpu_memory_utilization=0.90 启动 Qwen3-ASR。\n\n"
                f"总显存：{verified_total} MiB\n"
                f"当前空闲：{verified_free} MiB\n"
                f"vLLM 0.90 预算：约 {required_mib} MiB（另保留 64 MiB 启动余量）\n\n"
                "请关闭占用 RTX 3090 显存的程序后重试；程序不会改用 RTX 3070。"
            )

        self.args.qwen_cuda_visible_devices = selected
        self._resolved_cuda_device_order = device_order
        self._status(
            "WSL GPU 固定：RTX 3090；nvidia-smi GPU 1；CUDA_DEVICE_ORDER=%s；CUDA_VISIBLE_DEVICES=%s；"
            "CUDA验证=%s total=%dMiB free=%dMiB；vLLM=0.90；CPU offload=disabled"
            % (device_order, selected, verified.get("name") or "unknown", verified_total, verified_free)
        )
        return selected

    def _windows_model_to_wsl(self, value: str) -> str:
        original = str(value or "").strip()
        if not original:
            return original
        if sys.platform.startswith("win") and original.startswith("/"):
            return original
        raw = os.path.expanduser(original)
        drive, _tail = os.path.splitdrive(raw)
        if not (os.path.isabs(raw) or bool(drive)):
            return raw
        absolute = os.path.abspath(raw)
        if not os.path.exists(absolute):
            raise RuntimeError(f"ASR 本地模型路径不存在：{absolute}")
        code, out = self._run_wsl_capture(["wslpath", "-a", "-u", absolute], timeout=30)
        if code != 0 or not out.strip():
            raise RuntimeError(f"Windows 模型路径转换为 WSL 路径失败：{out}")
        return out.splitlines()[-1].strip()

    def _prepare_runtime_paths(self) -> bool:
        """Prepare launcher-owned runtime metadata; Qwen implementation stays upstream."""
        code, home = self._run_wsl_shell(
            'mkdir -p "$HOME/.cache/qwen3-subtitle"; printf %s "$HOME"', timeout=20
        )
        if code != 0 or not home.strip():
            messagebox.showerror("WSL HOME 检测失败", home, parent=self.root)
            return False
        home = home.splitlines()[-1].strip().rstrip("/")
        runtime_dir = f"{home}/.cache/qwen3-subtitle"
        self.remote_pid_file = f"{runtime_dir}/qwen_official_streaming_{self.instance_token}.pid"
        self.remote_wrapper_file = f"{runtime_dir}/qwen_official_streaming_8gb_memory_fit_v0918.py"
        return True

    def _install_8gb_memory_bootstrap(self) -> bool:
        """Write and compile the spawn-safe wrapper containing only vLLM memory knobs."""
        if not self.remote_wrapper_file:
            messagebox.showerror("Qwen 8GB 启动器错误", "WSL bootstrap 路径尚未初始化。", parent=self.root)
            return False
        payload = base64.b64encode(QWEN_OFFICIAL_8GB_MEMORY_BOOTSTRAP.encode("utf-8")).decode("ascii")
        writer = (
            "import base64,pathlib,sys; "
            "p=pathlib.Path(sys.argv[1]); "
            "p.write_bytes(base64.b64decode(sys.argv[2])); "
            "p.chmod(0o600)"
        )
        code, out = self._run_wsl_capture(
            [self.wsl_python, "-c", writer, self.remote_wrapper_file, payload], timeout=20
        )
        if code != 0:
            messagebox.showerror("Qwen 8GB 启动器写入失败", out[-3000:], parent=self.root)
            return False
        code, out = self._run_wsl_capture(
            [self.wsl_python, "-m", "py_compile", self.remote_wrapper_file], timeout=30
        )
        if code != 0:
            messagebox.showerror("Qwen 8GB 启动器语法错误", out[-3000:], parent=self.root)
            return False
        return True

    def _health_url(self) -> str:
        return self.args.qwen_server_url.rstrip("/") + "/health"

    def _official_api_url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        url = self.args.qwen_server_url.rstrip("/") + path
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if query:
                url += "?" + query
        return url

    def _probe_official_streaming_api(self, timeout: float = 1.5) -> tuple[bool, Dict[str, Any], str]:
        """Validate the exact /api/start -> /api/finish contract from Qwen's official demo."""
        session_id = ""
        try:
            start_req = urllib.request.Request(
                self._official_api_url("/api/start"), data=b"", method="POST"
            )
            with urllib.request.urlopen(start_req, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                return False, {}, "official /api/start returned non-object JSON"
            session_id = str(payload.get("session_id", "") or "")
            if not session_id:
                return False, payload, "official /api/start did not return session_id"
            finish_req = urllib.request.Request(
                self._official_api_url("/api/finish", {"session_id": session_id}),
                data=b"",
                method="POST",
            )
            with urllib.request.urlopen(finish_req, timeout=timeout) as response:
                final_payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(final_payload, dict) or final_payload.get("error"):
                return False, final_payload if isinstance(final_payload, dict) else {}, "official /api/finish failed"
            return True, {
                "ok": True,
                "backend": "vllm",
                "protocol": "qwen3-asr-official-demo-streaming",
            }, ""
        except Exception as exc:
            return False, {}, str(exc)

    def _is_wsl_distro_running(self) -> bool:
        if shutil.which("wsl.exe") is None:
            return False
        distro = str(self.wsl_distro or getattr(self.args, "wsl_distro", "") or "").strip()
        if not distro:
            return False
        code, running = self._run_capture(["wsl.exe", "--list", "--running", "--quiet"], timeout=5)
        if code != 0:
            return False
        names = {line.strip().lstrip("* ").strip() for line in running.splitlines() if line.strip()}
        return distro in names

    def _probe_health(self, timeout: float = 1.5) -> tuple[bool, Dict[str, Any], str]:
        """Probe either v0.9.x /health or Qwen's official streaming HTTP contract."""
        try:
            request = urllib.request.Request(self._health_url(), headers={"User-Agent": "Qwen3Subtitle/main"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict) and bool(payload.get("ok")):
                backend = str(payload.get("backend", "") or "").lower()
                if backend and backend != "vllm":
                    return False, payload, f"health backend must be vllm, got {backend}"
                return True, payload, ""
        except Exception:
            pass
        return self._probe_official_streaming_api(timeout=timeout)

    def _start_sidecar(self) -> bool:
        healthy, payload, _ = self._probe_health(timeout=1.0)
        if healthy:
            self.sidecar_reused = True
            self.sidecar_owned = False
            self._status(
                "Qwen 官方 streaming 服务已运行："
                + str(payload.get("model") or payload.get("protocol") or "official API")
            )
            return True

        parsed = urllib.parse.urlparse(self.args.qwen_server_url)
        host = (parsed.hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            messagebox.showerror(
                "远程 Qwen 服务不可用",
                f"{self.args.qwen_server_url} 当前不可访问；主程序只会自动启动本机 WSL 服务。",
                parent=self.root,
            )
            return False
        if bool(getattr(self.args, "no_qwen_auto_start", False)):
            messagebox.showerror(
                "Qwen streaming 服务未启动",
                f"{self.args.qwen_server_url} 不可用，并且已禁用自动启动。",
                parent=self.root,
            )
            return False
        if shutil.which("wsl.exe") is None:
            messagebox.showerror("WSL 不可用", "未找到 wsl.exe。请先运行独立的环境检测程序。", parent=self.root)
            return False
        if not self.wsl_python:
            messagebox.showerror(
                "尚未准备运行环境",
                "没有已保存的 WSL Python。请先运行独立环境检测程序。\n\n"
                f"配置文件位置：{self.runtime_config_path}",
                parent=self.root,
            )
            return False

        # Verify the exact upstream module rather than only checking broad imports.
        verify_code = (
            'import importlib.util; '
            'import qwen_asr, vllm, flask; '
            'from qwen_asr import Qwen3ASRModel; '
            'from qwen_asr.core.vllm_backend import Qwen3ASRForConditionalGeneration as _Q; '
            'from vllm import ModelRegistry; '
            'ModelRegistry.register_model("Qwen3ASRForConditionalGeneration", _Q); '
            'assert importlib.util.find_spec("qwen_asr.cli.demo_streaming") is not None; '
            'import inspect; assert "kwargs" in inspect.signature(Qwen3ASRModel.LLM).parameters'
        )
        code, check_out = self._run_wsl_capture([self.wsl_python, "-c", verify_code], timeout=30)
        if code != 0:
            messagebox.showerror(
                "Qwen3-ASR 官方 streaming 环境失效",
                "未检测到官方 qwen_asr.cli.demo_streaming/vLLM 环境。\n\n"
                "请在 WSL 环境安装：pip install -U 'qwen-asr[vllm]'\n\n"
                + check_out[-3000:],
                parent=self.root,
            )
            return False

        port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
        try:
            model_path = self._windows_model_to_wsl(self.args.asr_model)
        except Exception as exc:
            messagebox.showerror("ASR 模型路径错误", str(exc), parent=self.root)
            return False
        if not self._prepare_runtime_paths():
            return False

        # RTX 3090 24GB: launch Qwen's upstream streaming demo directly.
        # No 8GB memory-fit wrapper and no CPU offload monkeypatch are used.
        official_args = [
            self.wsl_python,
            "-m", QWEN_OFFICIAL_STREAMING_MODULE,
            "--asr-model-path", model_path,
            "--host", "0.0.0.0",
            "--port", str(port),
            "--gpu-memory-utilization", str(self._rtx3090_gpu_memory_utilization()),
            "--unfixed-chunk-num", str(int(self.args.qwen_unfixed_chunk_num)),
            "--unfixed-token-num", str(int(self.args.qwen_unfixed_token_num)),
            "--chunk-size-sec", str(float(self.args.qwen_chunk_size_sec)),
        ]
        command_text = " ".join(shlex.quote(item) for item in official_args)
        env_parts = []
        try:
            cuda_visible = self._resolve_cuda_visible_devices()
        except Exception as exc:
            messagebox.showerror(
                "WSL GPU 选择失败",
                str(exc) + "\n\n可在 PowerShell 检查 WSL 实际暴露的 GPU：\n"
                "wsl -d " + (self.wsl_distro or "Ubuntu") + " -- /usr/lib/wsl/lib/nvidia-smi -L",
                parent=self.root,
            )
            return False
        cpu_offload_gb = self._rtx3090_cpu_offload_gb()
        logger.info(
            "[RUNTIME_LAUNCH] effective Qwen official args: gpu_memory_utilization=%.2f cpu_offload_gb=%.1fGiB(disabled) CUDA_DEVICE_ORDER=%s CUDA_VISIBLE_DEVICES=%s",
            float(self.args.qwen_gpu_memory_utilization),
            cpu_offload_gb,
            str(getattr(self, "_resolved_cuda_device_order", QWEN3_ASR_RTX3090_DEVICE_ORDER) or "<default>"),
            cuda_visible or "<CUDA default>",
        )
        cuda_device_order = str(getattr(self, "_resolved_cuda_device_order", QWEN3_ASR_RTX3090_DEVICE_ORDER) or "").strip()
        if cuda_device_order:
            env_parts.append("CUDA_DEVICE_ORDER=" + shlex.quote(cuda_device_order))
        if cuda_visible:
            env_parts.append("CUDA_VISIBLE_DEVICES=" + shlex.quote(cuda_visible))
        hf_home = str(getattr(self.args, "qwen_wsl_hf_home", "") or "").strip()
        if hf_home:
            env_parts.append("HF_HOME=" + shlex.quote(hf_home))
        # IMPORTANT: shell variable assignments must appear before `exec`, not after it.
        # `exec CUDA_VISIBLE_DEVICES=1 python ...` makes POSIX sh try to execute a
        # program literally named `CUDA_VISIBLE_DEVICES=1` and exits with code 127.
        env_prefix = (" ".join(env_parts) + " ") if env_parts else ""
        pid_file = shlex.quote(self.remote_pid_file)
        shell = f'echo $$ > {pid_file}; {env_prefix}exec {command_text}'
        command = self._wsl_prefix() + ["--exec", "sh", "-lc", shell]

        os.makedirs(os.path.dirname(self.sidecar_log), exist_ok=True)
        log_handle = None
        try:
            # This log belongs to one launcher attempt. Truncate it so a failed new
            # startup cannot display stale HTTP 200 lines from an older server run.
            log_handle = open(self.sidecar_log, "w", encoding="utf-8", errors="replace")
            log_handle.write("===== official qwen-asr-demo-streaming + RTX3090 physical GPU1 + PCI_BUS_ID + no CPU offload start " + time.strftime("%Y-%m-%d %H:%M:%S") + " =====\n")
            log_handle.flush()
            self.sidecar_process = subprocess.Popen(
                command, stdout=log_handle, stderr=subprocess.STDOUT, **self._hidden_kwargs()
            )
        except Exception as exc:
            messagebox.showerror("Qwen 官方 streaming 服务启动失败", str(exc), parent=self.root)
            return False
        finally:
            if log_handle is not None:
                log_handle.close()

        self.sidecar_owned = True
        self.sidecar_reused = False
        timeout = max(30.0, float(getattr(self.args, "qwen_startup_timeout", 900.0)))
        deadline = time.monotonic() + timeout
        last_tail = ""
        while time.monotonic() < deadline:
            if self._cancelled:
                return False
            exit_code = self.sidecar_process.poll() if self.sidecar_process is not None else None
            if exit_code is not None:
                tail = self._tail_file(self.sidecar_log, 60)
                diagnosis = ""
                if "Free memory on device" in tail and "desired GPU memory utilization" in tail:
                    diagnosis = (
                        "\n\n检测到 RTX 3090 的 vLLM 启动前显存检查失败。程序已固定使用 WSL GPU 1 RTX 3090，"
                        "不会切换到 RTX 3070。请关闭占用 RTX 3090 显存的程序后重试。"
                    )
                elif "No available memory for the cache blocks" in tail:
                    util = float(getattr(self.args, "qwen_gpu_memory_utilization", 0.85))
                    diagnosis = (
                        "\n\n检测到 vLLM KV cache 显存预算不足。当前 "
                        f"gpu_memory_utilization={util:.2f}。"
                        "Qwen3-ASR 官方 Streaming Demo 使用 vLLM；当前已固定 WSL GPU 1 RTX 3090，请关闭 RTX 3090 上的其他高显存进程后重试。"
                    )
                messagebox.showerror(
                    "Qwen 官方 streaming 服务异常退出",
                    f"服务在就绪前退出，exit code={exit_code}\n\n"
                    + (tail[-6000:] if tail else "sidecar 日志没有产生输出。")
                    + diagnosis
                    + f"\n\n日志：{self.sidecar_log}",
                    parent=self.root,
                )
                return False
            healthy, payload, _ = self._probe_health(timeout=1.0)
            if healthy:
                self._status("Qwen3-ASR 官方 streaming 服务就绪")
                return True
            tail = self._tail_file(self.sidecar_log, 8)
            if tail and tail != last_tail:
                last_tail = tail
                self._status("Qwen3-ASR 官方服务启动中：" + tail.splitlines()[-1][:180])
            self._pump_ui()
            time.sleep(0.5)

        messagebox.showerror(
            "Qwen 官方 streaming 服务启动超时",
            f"在 {timeout:.0f} 秒内未通过官方 /api/start → /api/finish 探测。\n\n"
            + self._tail_file(self.sidecar_log, 40)[-5000:],
            parent=self.root,
        )
        return False

    def start(self) -> bool:
        cfg = self._apply_prepared_config()
        healthy, payload, _ = self._probe_health(timeout=0.8)
        if healthy:
            self.sidecar_reused = True
            self.sidecar_owned = False
            logger.info("[RUNTIME_LAUNCH] reuse official-compatible Qwen streaming service protocol=%s", payload.get("protocol", "health"))
            return True
        if not cfg and not self.wsl_python:
            messagebox.showerror(
                "请先运行环境检测",
                "主程序与前置环境检测已经分离。\n\n首次使用或环境变化后，请先运行环境检测程序。\n\n"
                f"检测成功后会生成：{self.runtime_config_path}",
            )
            return False
        vad_path = os.path.abspath(os.path.expanduser(str(self.args.vad_model_path)))
        if not os.path.isfile(vad_path):
            messagebox.showerror(
                "TEN-VAD 未准备",
                f"未找到：{vad_path}\n\n请先运行独立环境检测程序。",
            )
            return False
        self.wsl_was_running_before_start = self._is_wsl_distro_running()
        self._open_ui()
        try:
            self._status(
                f"使用已准备环境：WSL={self.wsl_distro or 'default'} Python={self.wsl_python}; "
                "启动 Qwen 官方 demo_streaming（固定 WSL GPU 1 = RTX 3090；CPU offload disabled）"
            )
            return self._start_sidecar()
        finally:
            if self.root is not None:
                try:
                    self.root.destroy()
                except tk.TclError:
                    pass
                self.root = None

    def _terminate_used_wsl_distro(self) -> None:
        """Stop only the distro used by this app so VmmemWSL can release memory.

        Do not use ``wsl --shutdown`` by default because that would terminate unrelated
        distros/jobs.  The preflight persists the concrete WSL_DISTRO_NAME, so normal
        launches have an exact distro name available here.
        """
        if bool(getattr(self.args, "keep_wsl_running", False)):
            logger.info("[RUNTIME_LAUNCH] keep_wsl_running requested; skip distro termination")
            return
        if shutil.which("wsl.exe") is None:
            return
        distro = str(self.wsl_distro or getattr(self.args, "wsl_distro", "") or "").strip()
        if not distro:
            # Avoid a global shutdown when the exact distro cannot be identified.
            logger.warning("[RUNTIME_LAUNCH] cannot terminate WSL: distro name is empty")
            return
        self._status_safe(f"正在关闭 WSL 发行版：{distro}")
        try:
            code, out = self._run_capture(["wsl.exe", "--terminate", distro], timeout=30)
            if code != 0:
                logger.warning("[RUNTIME_LAUNCH] wsl --terminate failed distro=%s code=%s out=%s", distro, code, out[-1000:])
                return
            # Wait briefly until the distro is no longer reported as running.
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                list_code, running = self._run_capture(["wsl.exe", "--list", "--running", "--quiet"], timeout=5)
                if list_code != 0:
                    break
                names = {line.strip().lstrip("* ").strip() for line in running.splitlines() if line.strip()}
                if distro not in names:
                    logger.info("[RUNTIME_LAUNCH] WSL distro stopped: %s", distro)
                    return
                time.sleep(0.25)
            logger.warning("[RUNTIME_LAUNCH] WSL distro still reported running after terminate: %s", distro)
        except Exception:
            logger.exception("[RUNTIME_LAUNCH] WSL distro termination failed distro=%s", distro)

    def _status_safe(self, text: str) -> None:
        """Log cleanup progress without requiring the startup Tk window to exist."""
        logger.info("[RUNTIME_LAUNCH] %s", text)
        if self.root is not None:
            try:
                self._status(text)
            except Exception:
                logger.debug("cleanup status UI update failed", exc_info=True)

    def close(self) -> None:
        keep_server = bool(getattr(self.args, "keep_qwen_server", False))
        if keep_server:
            logger.info("[RUNTIME_LAUNCH] keep_qwen_server requested; leave official Qwen service/WSL running")
            return

        # A healthy service discovered before launch is not owned by this application.
        if self.sidecar_reused and not self.sidecar_owned:
            logger.info("[RUNTIME_LAUNCH] reused Qwen service is not owned; leave service and WSL untouched")
            return

        if self.sidecar_owned:
            pid_file = self.remote_pid_file
            if pid_file and shutil.which("wsl.exe"):
                quoted = shlex.quote(pid_file)
                script = (
                    f'if [ -f {quoted} ]; then '
                    f'pid=$(cat {quoted} 2>/dev/null || true); '
                    'if [ -n "$pid" ]; then '
                    'kill -TERM "$pid" 2>/dev/null || true; '
                    'sleep 0.8; '
                    'kill -KILL "$pid" 2>/dev/null || true; '
                    'fi; '
                    f'rm -f {quoted}; fi'
                )
                try:
                    self._run_wsl_shell(script, timeout=10)
                except Exception:
                    logger.debug("official Qwen WSL cleanup failed", exc_info=True)

            process = self.sidecar_process
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=3)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass

        self.sidecar_owned = False
        self.sidecar_process = None

        if bool(getattr(self.args, "keep_wsl_running", False)):
            logger.info("[RUNTIME_LAUNCH] keep_wsl_running requested; leave distro running")
            return
        if self.wsl_was_running_before_start:
            logger.info("[RUNTIME_LAUNCH] WSL distro pre-existed this app; do not terminate it")
            return
        self._terminate_used_wsl_distro()





















def ensure_first_run_assets(root: tk.Tk, args: argparse.Namespace) -> bool:
    """Validate the local desktop assets. Qwen weights are owned by the WSL sidecar."""
    vad_path = os.path.abspath(os.path.expanduser(str(args.vad_model_path)))
    if not os.path.isfile(vad_path):
        messagebox.showerror(
            "TEN-VAD 未准备",
            f"未找到：{vad_path}\n\n请先运行独立环境检测程序。",
            parent=root,
        )
        return False
    args.asr_model_load_path = args.asr_model
    logger.info("[RUNTIME_ASSET] ASR sidecar model=%s VAD=%s", args.asr_model, vad_path)
    return True

np: Any = None
_runtime_import_error = None
_audio_import_error = None
pyaudio_backend: Any = None
sherpa_onnx: Any = None
scipy_signal: Any = None

TAG_PATTERN = re.compile(r'<\|.*?\|>')
JP_SPACE_PATTERN = re.compile(r'(?<=[\u3040-\u30ff\u3400-\u9fff])\s+(?=[\u3040-\u30ff\u3400-\u9fff])')
JP_PUNCT_BEFORE_SPACE_PATTERN = re.compile(r'\s+([、。！？!?」』）】〉》])')
JP_PUNCT_AFTER_SPACE_PATTERN = re.compile(r'([「『（【〈《])\s+')
JP_PUNCT_TO_JP_SPACE_PATTERN = re.compile(r'([、。！？!?])\s+(?=[\u3040-\u30ff\u3400-\u9fff])')
SENTENCE_END_PATTERN = re.compile(r'(?<=[。！？!?])')


def normalize_subtitle_text(text: str, max_chars: Optional[int] = None) -> str:
    text = TAG_PATTERN.sub('', text or '')
    text = text.replace('\u3000', ' ')
    text = JP_SPACE_PATTERN.sub('', text)
    text = JP_PUNCT_BEFORE_SPACE_PATTERN.sub(r'\1', text)
    text = JP_PUNCT_AFTER_SPACE_PATTERN.sub(r'\1', text)
    text = JP_PUNCT_TO_JP_SPACE_PATTERN.sub(r'\1', text)
    text = re.sub(r'\s{2,}', ' ', text).strip()
    if max_chars is not None and len(text) > max_chars:
        text = text[-max_chars:]
    return text


def remove_repeated_segment_prefix(
    previous: str,
    current: str,
    max_chars: int = 40,
    fuzzy_threshold: float = DEDUP_FUZZY_THRESHOLD,
    min_fuzzy_chars: int = 6,
) -> tuple[str, int]:
    """Remove ASR text duplicated by forced-segment audio overlap.

    The boundary matcher is deliberately conservative: exact suffix/prefix matches
    win first, then equal-length fuzzy matching, then a variable-length alignment
    that tolerates a few kana/kanji insertions or deletions. Short repetitions are
    never fuzzily removed so legitimate phrases such as 「はい、はい」 survive.
    """
    previous = normalize_subtitle_text(previous)
    current = normalize_subtitle_text(current)
    max_chars = max(0, int(max_chars))

    # Semantics-preserving fast path: only bypass the loops when the whole text is
    # inside the configured overlap cap.  Removing an identical string longer than
    # max_chars would violate the original safety bound.
    if previous and previous == current and len(current) <= max_chars:
        return "", len(current)

    upper = min(max_chars, len(previous), len(current))
    for size in range(upper, 0, -1):
        if previous[-size:] == current[:size]:
            return current[size:].lstrip("、，,。！？!? "), size

    threshold = min(0.99, max(0.5, float(fuzzy_threshold)))
    minimum = max(6, int(min_fuzzy_chars))
    best_size = 0
    best_ratio = 0.0
    for size in range(upper, minimum - 1, -1):
        left = previous[-size:]
        right = current[:size]
        matcher = SequenceMatcher(None, left, right, autojunk=False)
        blocks = [block for block in matcher.get_matching_blocks() if block.size]
        ratio = matcher.ratio()
        matching = sum(block.size for block in blocks)
        if blocks:
            first = blocks[0]
            last = blocks[-1]
            anchored_start = first.a <= 2 and first.b <= 2
            anchored_end = (size - (last.a + last.size) <= 2) and (
                size - (last.b + last.size) <= 2
            )
        else:
            anchored_start = anchored_end = False
        if (
            anchored_start
            and anchored_end
            and ratio >= threshold
            and matching >= max(minimum, int(size * DEDUP_MIN_MATCHING_COVERAGE))
        ):
            best_size = size
            best_ratio = ratio
            break

    if not best_size and upper >= minimum + 2:
        previous_limit = min(max_chars, len(previous))
        current_limit = min(max_chars, len(current))
        best_candidate: Optional[tuple[float, int, int]] = None
        for left_size in range(previous_limit, minimum - 1, -1):
            left = previous[-left_size:]
            delta_limit = max(2, min(5, left_size // 4))
            right_min = max(minimum, left_size - delta_limit)
            right_max = min(current_limit, left_size + delta_limit)
            for right_size in range(right_max, right_min - 1, -1):
                right = current[:right_size]
                matcher = SequenceMatcher(None, left, right, autojunk=False)
                blocks = [block for block in matcher.get_matching_blocks() if block.size]
                if not blocks:
                    continue
                ratio = matcher.ratio()
                matching = sum(block.size for block in blocks)
                first = blocks[0]
                last = blocks[-1]
                anchored_start = first.a <= 2 and first.b <= 2
                anchored_end = (left_size - (last.a + last.size) <= 2) and (
                    right_size - (last.b + last.size) <= 2
                )
                coverage = matching / max(left_size, right_size)
                relaxed_threshold = max(
                    DEDUP_VARIABLE_RELAXED_FLOOR, threshold - DEDUP_VARIABLE_RELAXED_OFFSET
                )
                if (
                    anchored_start
                    and anchored_end
                    and ratio >= relaxed_threshold
                    and coverage >= DEDUP_VARIABLE_MIN_COVERAGE
                    and matching >= minimum
                ):
                    trim_size = last.b + last.size
                    score = ratio + coverage * 0.20 + min(trim_size, 40) / 1000.0
                    candidate = (score, trim_size, matching)
                    if best_candidate is None or candidate > best_candidate:
                        best_candidate = candidate
        if best_candidate is not None:
            best_ratio = best_candidate[0]
            best_size = best_candidate[1]

    if best_size:
        logger.debug(
            "[ASR_OVERLAP] fuzzy_dedup chars=%d score=%.3f",
            best_size,
            best_ratio,
        )
        return current[best_size:].lstrip("、，,。！？!? "), best_size
    return current, 0




def longest_common_prefix(texts: List[str]) -> str:
    if not texts:
        return ""
    prefix = texts[0]
    for text in texts[1:]:
        common_length = 0
        for old_char, new_char in zip(prefix, text):
            if old_char != new_char:
                break
            common_length += 1
        prefix = prefix[:common_length]
        if not prefix:
            break
    return prefix


def stable_semantic_prefix(
    stable_prefix: str,
    current_text: str,
    tail_guard_chars: int = 0,
) -> str:
    """Commit stable source text without freezing after the first sentence boundary.

    A completed sentence boundary is a strong anchor, but it must not become a
    permanent ceiling.  Earlier versions returned only the last complete sentence
    whenever *any* sentence-ending punctuation existed in the agreed prefix.  That
    made long speech look permanently committed at (for example) 16 characters
    even while LocalAgreement had confirmed dozens of characters after that point.

    Keep the strongest sentence boundary immediately, then allow the confirmed tail
    to advance once at least four post-boundary characters survive the normal tail
    guard.  This preserves a small revisable suffix while letting the source frontier
    continue moving through multi-sentence/continuous speech.
    """
    stable = normalize_subtitle_text(stable_prefix)
    current = normalize_subtitle_text(current_text)
    if not stable or not current.startswith(stable):
        return ""
    if stable == current:
        return stable

    last_boundary = 0
    for index, char in enumerate(stable):
        if char in "。！？!?":
            last_boundary = index + 1

    guard = max(0, int(tail_guard_chars))
    guarded_end = len(stable) - guard if guard else len(stable)
    guarded_end = max(0, guarded_end)

    if last_boundary > 0:
        # Commit the complete sentence immediately.  Advance beyond it only when a
        # meaningful post-boundary tail is itself stable; otherwise one or two fresh
        # characters after punctuation would flicker into the immutable prefix.
        if guarded_end >= last_boundary + 4:
            return stable[:guarded_end]
        return stable[:last_boundary]

    if guard and guarded_end >= 4:
        # Streaming RNNT output commonly grows one token at a time. Waiting for
        # an unchanged full hypothesis delays subtitles until the endpoint;
        # keep a short revisable tail while exposing the confirmed prefix.
        return stable[:guarded_end]
    return ""


@dataclass(frozen=True)
class SubtitleRevisionState:
    revision: int
    state: str
    text: str
    stable_text: str
    unstable_text: str

    @property
    def committed_text(self) -> str:
        return self.stable_text

    @property
    def revisable_text(self) -> str:
        return self.unstable_text


@dataclass(frozen=True)
class TranscriptEvent:
    """Immutable event carrying one revision of recognized subtitle text."""
    session_id: str
    utterance_id: int
    revision: int
    state: str
    text: str
    committed_text: str
    revisable_text: str
    is_final: bool
    emitted_at: float
    @property
    def stable_text(self) -> str:
        return self.committed_text
    @property
    def unstable_text(self) -> str:
        return self.revisable_text




class LocalAgreementCommitPolicy:
    """Commit only the prefix shared by consecutive streaming hypotheses.

    The committed prefix is monotonic during normal growth. If an ASR revision
    contradicts already committed text, the policy explicitly starts a new
    agreement window instead of silently combining incompatible hypotheses.
    """

    def __init__(
        self,
        stability_window: int = 3,
        *,
        tail_guard_chars: int = 4,
        min_commit_chars: int = 3,
        normalizer: Callable[[str], str] = normalize_subtitle_text,
    ):
        self.stability_window = max(2, int(stability_window))
        self.tail_guard_chars = max(0, int(tail_guard_chars))
        self.min_commit_chars = max(1, int(min_commit_chars))
        self._normalizer = normalizer
        self._history: deque[str] = deque(maxlen=self.stability_window)
        self._revision = 0
        self._committed_text = ""
        self._last_text = ""
        self._lock = Lock()

    def reset(self) -> None:
        with self._lock:
            self._history.clear()
            self._committed_text = ""
            self._last_text = ""

    def snapshot(self) -> tuple[list[str], int, str, str]:
        with self._lock:
            return (list(self._history), self._revision, self._committed_text, self._last_text)

    def restore(self, snapshot: tuple[list[str], int, str, str]) -> None:
        history, revision, committed, last_text = snapshot
        with self._lock:
            self._history.clear()
            self._history.extend(history)
            self._revision = int(revision)
            self._committed_text = str(committed)
            self._last_text = str(last_text)

    def observe(self, text: str, is_final: bool = False) -> SubtitleRevisionState:
        normalized = self._normalizer(text or "")
        with self._lock:
            if is_final:
                self._revision += 1
                revision = self._revision
                self._history.clear()
                self._committed_text = normalized
                self._last_text = normalized
                return SubtitleRevisionState(
                    revision=revision,
                    state="final",
                    text=normalized,
                    stable_text=normalized,
                    unstable_text="",
                )

            if self._committed_text and not normalized.startswith(self._committed_text):
                # A committed prefix is immutable. Streaming hypotheses sometimes
                # revise older text transiently; publishing that revision would make
                # stable subtitles jump backwards and would violate the committed-prefix contract.
                # Ignore the contradictory interim hypothesis and wait for either a
                # compatible update or the authoritative final result.
                logger.info(
                    "[LOCAL_AGREEMENT_CONFLICT] committed=%r hypothesis=%r action=hold",
                    self._committed_text[-80:],
                    normalized[-80:],
                )
                held = self._last_text or (self._committed_text + normalized)
                revisable = held[len(self._committed_text):] if held.startswith(self._committed_text) else ""
                return SubtitleRevisionState(
                    revision=self._revision,
                    state="stable",
                    text=held,
                    stable_text=self._committed_text,
                    unstable_text=revisable,
                )

            self._revision += 1
            revision = self._revision
            self._history.append(normalized)
            candidate = ""
            if len(self._history) >= self.stability_window:
                common = longest_common_prefix(list(self._history))
                candidate = stable_semantic_prefix(
                    common,
                    normalized,
                    tail_guard_chars=self.tail_guard_chars,
                )
                if len(candidate) < self.min_commit_chars:
                    candidate = ""

            if candidate:
                if not self._committed_text:
                    self._committed_text = candidate
                elif candidate.startswith(self._committed_text):
                    self._committed_text = candidate
                elif not normalized.startswith(self._committed_text):
                    self._committed_text = candidate

            committed = (
                self._committed_text
                if self._committed_text and normalized.startswith(self._committed_text)
                else ""
            )
            revisable = normalized[len(committed):] if committed else normalized
            self._last_text = normalized
            return SubtitleRevisionState(
                revision=revision,
                state="stable" if committed else "interim",
                text=normalized,
                stable_text=committed,
                unstable_text=revisable,
            )


class RealtimeSubtitleSession:
    """Per-session authoritative ASR subtitle state with transactional publication."""
    TRANSCRIPT_EVENT = "transcript"
    def __init__(
        self,
        session_id: str,
        event_bus: SessionEventBus,
        *,
        source_stability_window: int = 3,
        retention_seconds: float = 300.0,
        max_completed_utterances: int = 512,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.session_id = str(session_id)
        self.event_bus = event_bus
        self.source_stability_window = max(2, int(source_stability_window))
        self.retention_seconds = max(30.0, float(retention_seconds))
        self.max_completed_utterances = max(64, int(max_completed_utterances))
        self.clock = clock
        self._lock = Lock()
        self._utterance_locks = KeyedLockPool()
        self._source_trackers: OrderedDict[int, LocalAgreementCommitPolicy] = OrderedDict()
        self._latest_source: OrderedDict[int, TranscriptEvent] = OrderedDict()
        self._last_activity: OrderedDict[int, float] = OrderedDict()
        self._closed = False
    def _source_tracker(self, utterance_id: int) -> LocalAgreementCommitPolicy:
        key = int(utterance_id)
        with self._lock:
            tracker = self._source_trackers.get(key)
            if tracker is None:
                tracker = LocalAgreementCommitPolicy(stability_window=self.source_stability_window)
                self._source_trackers[key] = tracker
            self._source_trackers.move_to_end(key)
            return tracker
    def _cleanup_locked(self) -> None:
        now = self.clock()
        removable = [
            key for key, at in self._last_activity.items()
            if now - float(at) >= self.retention_seconds
        ]
        finals = [key for key, event in self._latest_source.items() if event.is_final]
        excess = max(0, len(finals) - self.max_completed_utterances)
        removable.extend(finals[:excess])
        for key in dict.fromkeys(removable):
            self._source_trackers.pop(key, None)
            self._latest_source.pop(key, None)
            self._last_activity.pop(key, None)
            self._utterance_locks.discard(key)
    def latest_source(self, utterance_id: int) -> Optional[TranscriptEvent]:
        with self._lock:
            return self._latest_source.get(int(utterance_id))
    def ingest_asr(self, text: str, *, is_final: bool, utterance_id: int) -> TranscriptEvent:
        key = int(utterance_id)
        with self._utterance_locks.get(key):
            normalized = normalize_subtitle_text(text)
            with self._lock:
                if self._closed:
                    raise RuntimeError("字幕 Session 已关闭")
                previous = self._latest_source.get(key)
                if previous is not None and previous.text == normalized and previous.is_final == bool(is_final):
                    self._last_activity[key] = self.clock()
                    return previous
            tracker = self._source_tracker(key)
            snapshot = tracker.snapshot()
            state = tracker.observe(normalized, is_final=is_final)
            event = TranscriptEvent(
                session_id=self.session_id,
                utterance_id=key,
                revision=state.revision,
                state=state.state,
                text=state.text,
                committed_text=state.stable_text,
                revisable_text=state.unstable_text,
                is_final=bool(is_final),
                emitted_at=self.clock(),
            )
            with self._lock:
                previous = self._latest_source.get(key)
            if previous is not None and (
                event.revision == previous.revision and event.text == previous.text
                and event.committed_text == previous.committed_text
                and event.revisable_text == previous.revisable_text
                and event.is_final == previous.is_final
            ):
                return previous
            if not self.event_bus.publish(self.TRANSCRIPT_EVENT, event):
                tracker.restore(snapshot)
                logger.error(
                    "[TRANSCRIPT_EVENT_ROLLBACK] session_id=%s utterance=%d revision=%d final=%s",
                    self.session_id, key, event.revision, event.is_final,
                )
                return previous if previous is not None else event
            with self._lock:
                self._latest_source[key] = event
                self._latest_source.move_to_end(key)
                self._last_activity[key] = self.clock()
                self._last_activity.move_to_end(key)
                self._cleanup_locked()
            return event
    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._source_trackers.clear()
            self._latest_source.clear()
            self._last_activity.clear()
            self._utterance_locks.clear()



def should_accept_source_update(
    current_text: str,
    current_is_final: bool,
    new_text: str,
    is_final: bool,
    age_seconds: float,
    same_utterance: bool = True,
) -> bool:
    current_text = current_text or ""
    new_text = new_text or ""
    if not new_text:
        return False
    if not same_utterance:
        return True
    if not current_text:
        return True
    if new_text == current_text:
        return is_final and not current_is_final
    if is_final:
        return True

    age_seconds = max(0.0, float(age_seconds))
    if current_is_final and age_seconds < 0.8 and new_text in current_text:
        return False
    if age_seconds < 2.5 and len(new_text) + 4 < len(current_text):
        if new_text in current_text:
            return False
        if SequenceMatcher(None, new_text, current_text).ratio() >= 0.64:
            return False
    return True


def validate_args(args: argparse.Namespace) -> argparse.Namespace:
    checks = {
        "sample_rate": args.sample_rate == 16000,
        "min_silence_duration": args.min_silence_duration > 0,
        "min_speech_duration": args.min_speech_duration > 0,
        "vad_threshold": 0.0 < args.vad_threshold < 1.0,
        "vad_buffer_size": args.vad_buffer_size > 0,
        "max_utterance_seconds": args.max_utterance_seconds > 0,
        "speech_preroll_ms": args.speech_preroll_ms >= 0,
        "forced_segment_overlap_ms": 0 <= args.forced_segment_overlap_ms <= 1000,
        "audio_queue_size": args.audio_queue_size > 0,
        "max_drain_chunks": args.max_drain_chunks > 0,
        "audio_latency_budget_ms": args.audio_latency_budget_ms >= 200,
        "audio_backpressure_mode": args.audio_backpressure_mode in ("live", "buffered"),
        "audio_recovery_preroll_ms": 100 <= args.audio_recovery_preroll_ms <= args.audio_latency_budget_ms,
        "stable_partial_threshold": args.stable_partial_threshold >= 2,
        "qwen_chunk_size_sec": 0.1 <= args.qwen_chunk_size_sec <= 10.0,
        "qwen_unfixed_chunk_num": args.qwen_unfixed_chunk_num >= 0,
        "qwen_unfixed_token_num": args.qwen_unfixed_token_num >= 0,
        "qwen_push_interval_ms": 40 <= args.qwen_push_interval_ms <= 2000,
        "qwen_http_timeout": args.qwen_http_timeout > 0,
        "qwen_gpu_memory_utilization": 0.05 <= args.qwen_gpu_memory_utilization <= 1.0,
        "qwen_cpu_offload_gb": 0.0 <= args.qwen_cpu_offload_gb <= 64.0,
        "qwen_startup_timeout": args.qwen_startup_timeout > 0,
        "endpoint_punctuation_hold_ms": args.endpoint_punctuation_hold_ms >= 0,
        "endpoint_min_utterance_ms": args.endpoint_min_utterance_ms >= 0,
    }
    invalid = [name for name, ok in checks.items() if not ok]
    if invalid:
        raise ValueError(f"参数必须在有效范围内：{', '.join(invalid)}")
    return args


def should_force_utterance_segment(
    sample_count: int,
    sample_rate: int,
    max_utterance_seconds: float,
    vad_has_endpoint: bool,
) -> bool:
    if vad_has_endpoint or sample_rate <= 0 or max_utterance_seconds <= 0:
        return False
    return sample_count >= round(sample_rate * max_utterance_seconds)


def supports_vad_reset(vad) -> bool:
    return callable(getattr(vad, "reset", None))


def ensure_runtime_dependencies() -> bool:
    """Lazy-load NumPy; Qwen3-ASR/vLLM lives exclusively in the official WSL service."""
    global np, _runtime_import_error
    try:
        if np is None:
            started = time.monotonic()
            import numpy as _np
            np = _np
            logger.info("[RUNTIME_IMPORT] module=numpy ms=%.0f", (time.monotonic() - started) * 1000)
        return True
    except Exception as exc:
        _runtime_import_error = exc
        logger.error("运行时依赖加载失败：%s", exc, exc_info=True)
        return False






def configure_current_process_priority(level: str) -> None:
    """Best-effort process priority control for the desktop ASR application."""
    try:
        if sys.platform.startswith("win"):
            import ctypes
            classes = {
                "idle": 0x00000040,
                "below_normal": 0x00004000,
                "normal": 0x00000020,
                "above_normal": 0x00008000,
                "high": 0x00000080,
            }
            value = classes.get(level, classes["normal"])
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if not ctypes.windll.kernel32.SetPriorityClass(handle, value):
                raise OSError("SetPriorityClass failed")
        elif level == "below_normal":
            try:
                os.nice(5)
            except OSError:
                pass
        logger.info("[PROCESS] pid=%d priority=%s", os.getpid(), level)
    except Exception as exc:
        logger.debug("[PROCESS] 无法设置进程优先级 %s: %s", level, exc)


# ================== 音频设备与实时采集（单文件内置） ==================

AUDIO_FRAME_SECONDS = 0.05


def ensure_audio_dependencies() -> bool:
    """Lazy-load audio dependencies without mutating the Python environment."""
    global pyaudio_backend, sherpa_onnx, scipy_signal, np, _audio_import_error
    try:
        if pyaudio_backend is None:
            try:
                import pyaudiowpatch as _pyaudio
            except ImportError:
                import pyaudio as _pyaudio
            pyaudio_backend = _pyaudio
        if sherpa_onnx is None:
            import sherpa_onnx as _sherpa_onnx
            sherpa_onnx = _sherpa_onnx
        if scipy_signal is None:
            from scipy import signal as _signal
            scipy_signal = _signal
        if np is None:
            import numpy as _np
            np = _np
        return True
    except Exception as exc:
        _audio_import_error = exc
        logger.error("音频依赖加载失败：%s", exc, exc_info=True)
        return False


def get_audio_devices(p_audio) -> List[tuple[int, Dict[str, Any]]]:
    """Return MME devices for the legacy microphone list used by the UI."""
    device_count = p_audio.get_device_count()
    if device_count <= 0:
        return []
    host_api = 0
    for index in range(p_audio.get_host_api_count()):
        info = p_audio.get_host_api_info_by_index(index)
        if "MME" in info.get("name", ""):
            host_api = index
            break
    devices = []
    for index in range(device_count):
        info = p_audio.get_device_info_by_index(index)
        if info.get("hostApi") == host_api:
            devices.append((index, info))
    return devices


def _read_probe_chunk(stream, frames: int, timeout: float) -> None:
    finished = Event()
    errors: List[BaseException] = []

    def read_once() -> None:
        try:
            stream.read(frames, exception_on_overflow=False)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    Thread(target=read_once, daemon=True).start()
    if not finished.wait(timeout):
        raise TimeoutError(f"设备试读超过 {timeout:.1f}s")
    if errors:
        raise errors[0]


def probe_audio_devices(
    p_audio,
    device_indices,
    chunk_seconds: float = AUDIO_FRAME_SECONDS,
    read_timeout: float = 1.0,
):
    """Open all selected devices together and verify that they can be started."""
    valid_devices: List[int] = []
    failures: Dict[int, str] = {}
    streams: Dict[int, Any] = {}
    frames_by_device: Dict[int, int] = {}
    loopback_devices: set[int] = set()
    try:
        for device_idx in device_indices:
            try:
                info = p_audio.get_device_info_by_index(device_idx)
                native_rate = int(info["defaultSampleRate"])
                channels = int(info.get("maxInputChannels", 0) or info.get("maxOutputChannels", 0))
                if native_rate <= 0 or channels <= 0:
                    raise ValueError("设备没有可用声道或采样率")
                frames = max(1, int(native_rate * chunk_seconds))
                streams[device_idx] = p_audio.open(
                    format=pyaudio_backend.paFloat32,
                    channels=channels,
                    rate=native_rate,
                    input=True,
                    input_device_index=device_idx,
                    frames_per_buffer=frames,
                    start=False,
                )
                frames_by_device[device_idx] = frames
                if info.get("isLoopbackDevice", False):
                    loopback_devices.add(device_idx)
            except Exception as exc:
                failures[device_idx] = str(exc)

        for device_idx, stream in streams.items():
            try:
                stream.start_stream()
                _read_probe_chunk(stream, frames_by_device[device_idx], read_timeout)
                valid_devices.append(device_idx)
            except TimeoutError as exc:
                # An idle WASAPI loopback may not yield data until playback starts.
                if device_idx in loopback_devices:
                    valid_devices.append(device_idx)
                else:
                    failures[device_idx] = str(exc)
            except Exception as exc:
                failures[device_idx] = str(exc)
    finally:
        for stream in streams.values():
            try:
                if stream.is_active():
                    stream.stop_stream()
                stream.close()
            except Exception:
                pass
    return valid_devices, failures


def _probe_audio_devices_worker(device_indices, chunk_seconds, result_queue) -> None:
    if not ensure_audio_dependencies():
        result_queue.put(([], {idx: f"音频依赖不可用：{_audio_import_error}" for idx in device_indices}))
        return
    p_audio = pyaudio_backend.PyAudio()
    try:
        result_queue.put(probe_audio_devices(p_audio, device_indices, chunk_seconds))
    finally:
        p_audio.terminate()


def probe_audio_devices_isolated(device_indices, timeout: float = 8.0):
    """Protect the UI process from PortAudio hangs and native probe crashes."""
    result_queue = multiprocessing.Queue(maxsize=1)
    process = multiprocessing.Process(
        target=_probe_audio_devices_worker,
        args=(list(device_indices), AUDIO_FRAME_SECONDS, result_queue),
    )
    try:
        process.start()
        process.join(timeout=max(0.1, timeout))
        if process.is_alive():
            process.terminate()
            process.join(timeout=1.0)
            return [], {idx: "设备组合预检超时" for idx in device_indices}
        if process.exitcode != 0:
            return [], {idx: f"设备组合预检进程异常退出：{process.exitcode}" for idx in device_indices}
        try:
            return result_queue.get(timeout=0.5)
        except queue.Empty:
            return [], {idx: "设备组合预检未返回结果" for idx in device_indices}
    finally:
        try:
            result_queue.close()
            result_queue.join_thread()
        except Exception:
            pass


class StreamingAudioResampler:
    """Overlap-preserving polyphase resampler for continuous live audio.

    scipy.signal.resample_poly() is stateless. Applying it independently to
    every 50ms block repeats the FIR startup transient at every boundary. This
    wrapper retains a ratio-aligned input history and discards the history's
    output, approximating overlap-save behavior while keeping dependencies
    limited to SciPy.
    """

    def __init__(self, native_rate: int, target_rate: int):
        self.native_rate = int(native_rate)
        self.target_rate = int(target_rate)
        if self.native_rate <= 0 or self.target_rate <= 0:
            raise ValueError("重采样采样率必须大于 0")
        divisor = gcd(self.native_rate, self.target_rate)
        self.up = self.target_rate // divisor
        self.down = self.native_rate // divisor
        history = max(self.down * 32, round(self.native_rate * 0.02))
        self.history_samples = max(self.down, ((history + self.down - 1) // self.down) * self.down)
        self._history = np.zeros(self.history_samples, dtype=np.float32)

    def process(self, samples):
        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        if self.native_rate == self.target_rate or audio.size == 0:
            return audio.copy()
        combined = np.concatenate((self._history, audio))
        converted = scipy_signal.resample_poly(combined, self.up, self.down)
        discard = self.history_samples * self.up // self.down
        expected = max(1, round(len(audio) * self.target_rate / self.native_rate))
        result = np.asarray(converted[discard:discard + expected], dtype=np.float32)
        if len(result) < expected:
            result = np.pad(result, (0, expected - len(result)))
        elif len(result) > expected:
            result = result[:expected]
        self._history = combined[-self.history_samples:].copy()
        return result


def _limit_audio(samples, ceiling: float = 0.98):
    samples = np.nan_to_num(samples, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)
    if samples.size == 0:
        return samples
    peak = float(np.max(np.abs(samples)))
    if peak > ceiling:
        samples = samples * (ceiling / peak)
    return np.clip(samples, -ceiling, ceiling).astype(np.float32, copy=False)


class ConservativeSpeechLeveler:
    """Stateful low-level speech gain control shared by VAD and ASR.

    It only raises gain when the frame already contains meaningful energy, so
    silence/background noise is not aggressively amplified. Gain changes are
    smoothed to avoid pumping and clipped with the same ceiling used elsewhere.
    """

    def __init__(
        self,
        target_rms: float = 0.075,
        gate_rms: float = 0.004,
        min_gain: float = 0.75,
        max_gain: float = 3.0,
        attack: float = 0.30,
        release: float = 0.08,
        ceiling: float = 0.95,
    ):
        self.target_rms = max(1e-4, float(target_rms))
        self.gate_rms = max(0.0, float(gate_rms))
        self.min_gain = max(0.1, float(min_gain))
        self.max_gain = max(self.min_gain, float(max_gain))
        self.attack = min(1.0, max(0.001, float(attack)))
        self.release = min(1.0, max(0.001, float(release)))
        self.ceiling = min(0.999, max(0.1, float(ceiling)))
        self.gain = 1.0
        self.dc_estimate = 0.0

    def process(self, samples):
        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        if audio.size == 0:
            return audio.copy()
        audio = np.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0)
        mean = float(np.mean(audio))
        self.dc_estimate = 0.98 * self.dc_estimate + 0.02 * mean
        audio = audio - self.dc_estimate
        rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
        if rms >= self.gate_rms:
            desired = min(self.max_gain, max(self.min_gain, self.target_rms / max(rms, 1e-6)))
        else:
            desired = 1.0
        # Increase gain faster than we release it; both remain deliberately slow
        # enough for 50 ms live frames to avoid audible/feature-domain pumping.
        coeff = self.attack if desired > self.gain else self.release
        self.gain += (desired - self.gain) * coeff
        leveled = audio * self.gain
        peak = float(np.max(np.abs(leveled)))
        if peak > self.ceiling:
            leveled = leveled * (self.ceiling / peak)
        return np.clip(leveled, -self.ceiling, self.ceiling).astype(np.float32, copy=False)


class AdaptiveAudioClockAligner:
    """Track per-device capture-clock offsets without comparing raw clocks directly.

    Independent audio devices have distinct hardware clocks.  Their monotonic
    capture timestamps therefore contain both a fixed startup offset and a slow
    drift.  The aligner learns the fixed offset on the first accepted frame and
    follows only small in-window drift; whole-frame jumps remain visible to the
    caller and are handled as stale/ahead packets instead of corrupting the clock
    estimate.
    """

    def __init__(self, tolerance_seconds: float, adaptation: float = 0.05):
        self.tolerance_seconds = max(0.001, float(tolerance_seconds))
        self.adaptation = min(0.25, max(0.001, float(adaptation)))
        self._offsets: Dict[int, float] = {}

    def delta(self, device_idx: int, anchor_time: float, candidate_time: float) -> float:
        raw_offset = float(candidate_time) - float(anchor_time)
        known = self._offsets.get(int(device_idx))
        if known is None:
            self._offsets[int(device_idx)] = raw_offset
            return 0.0
        return raw_offset - known

    def accept(self, device_idx: int, anchor_time: float, candidate_time: float) -> None:
        raw_offset = float(candidate_time) - float(anchor_time)
        key = int(device_idx)
        known = self._offsets.get(key)
        if known is None:
            self._offsets[key] = raw_offset
            return
        error = raw_offset - known
        if abs(error) <= self.tolerance_seconds:
            max_step = self.tolerance_seconds * 0.1
            correction = max(-max_step, min(max_step, error)) * self.adaptation
            self._offsets[key] = known + correction


def _mix_audio_tracks(
    tracks,
    mode: str = "average",
    *,
    expected_track_count: Optional[int] = None,
):
    """Mix tracks with stable gain even when one device temporarily misses a frame."""
    normalized = [np.asarray(track, dtype=np.float32) for track in tracks if len(track)]
    if not normalized:
        return np.array([], dtype=np.float32)
    if mode == "add":
        return _limit_audio(np.sum(normalized, axis=0))
    active = []
    for track in normalized:
        rms = float(np.sqrt(np.mean(np.square(track, dtype=np.float64))))
        if rms >= 1e-5:
            active.append(track)
    selected = active or normalized
    # Multi-device callers pass the configured track count so a temporary missing
    # source cannot make the remaining source jump in level. Single-device callers
    # retain the original unity-gain behavior.
    normalization_count = max(
        1,
        int(expected_track_count) if expected_track_count is not None else len(selected),
    )
    mixed = np.sum(selected, axis=0) / max(1.0, normalization_count ** 0.5)
    return _limit_audio(mixed)


def trim_audio_packets_to_latency_budget(
    packets: List[Any],
    now: float,
    latency_budget_seconds: float,
    recovery_preroll_seconds: float,
) -> tuple[List[Any], int, float]:
    """Keep the live edge when queued capture audio exceeds the real-time budget."""
    audio_packets = [
        packet for packet in packets
        if isinstance(packet, dict) and packet.get("type", "audio") == "audio"
    ]
    if not audio_packets:
        return packets, 0, 0.0
    oldest_at = min(float(packet.get("captured_at", now)) for packet in audio_packets)
    newest_at = max(float(packet.get("captured_at", now)) for packet in audio_packets)
    max_age = max(0.0, now - oldest_at)
    if max_age <= max(0.05, float(latency_budget_seconds)):
        return packets, 0, max_age
    cutoff = newest_at - max(0.05, float(recovery_preroll_seconds))
    kept = [
        packet for packet in packets
        if not isinstance(packet, dict)
        or packet.get("type", "audio") != "audio"
        or float(packet.get("captured_at", newest_at)) >= cutoff
    ]
    return kept, max(0, len(packets) - len(kept)), max_age


class GrowableAudioBuffer:
    """Exponentially growing contiguous buffer without O(n²) append copies."""

    def __init__(self, initial_capacity: int = 32000):
        self._data = np.empty(max(1, int(initial_capacity)), dtype=np.float32)
        self._size = 0

    def clear(self) -> None:
        self._size = 0

    def append(self, samples) -> None:
        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        required = self._size + len(audio)
        if required > len(self._data):
            capacity = max(required, len(self._data) * 2)
            grown = np.empty(capacity, dtype=np.float32)
            grown[:self._size] = self._data[:self._size]
            self._data = grown
        self._data[self._size:required] = audio
        self._size = required

    def discard_prefix(self, count: int) -> int:
        count = min(self._size, max(0, int(count)))
        if count <= 0: return 0
        remaining = self._size - count
        if remaining > 0: self._data[:remaining] = self._data[count:self._size]
        self._size = remaining
        return count

    def __len__(self) -> int:
        return self._size

    def __getitem__(self, item):
        return self._data[:self._size].__getitem__(item)


def _put_audio_packet(
    output_queue,
    packet,
    on_drop: Callable[[], None],
    mode: str = "live",
    stop_event: Optional[Any] = None,
) -> bool:
    """Publish audio using either live-edge shedding or buffered backpressure."""
    if mode == "buffered":
        while stop_event is None or not stop_event.is_set():
            try:
                output_queue.put(packet, timeout=0.1)
                return True
            except queue.Full:
                continue
            except (EOFError, OSError, ValueError):
                return False
        return False

    try:
        output_queue.put_nowait(packet)
        return True
    except queue.Full:
        on_drop()
        try:
            output_queue.get(timeout=0.05)
        except (queue.Empty, OSError):
            return False
        try:
            output_queue.put_nowait(packet)
            return True
        except queue.Full:
            return False


def start_recording(
    device_indices,
    output_queue,
    stop_event,
    mix_mode: str = "average",
    debug_save_audio: str = "",
    target_sample_rate: int = 16000,
    emit_metadata: bool = False,
    backpressure_mode: str = "buffered",
) -> None:
    """Capture, resample and mix audio with a zero-wait single-device fast path."""
    if not device_indices:
        raise ValueError("没有选择任何音频设备")
    if not ensure_audio_dependencies():
        raise RuntimeError(f"缺少音频依赖：{_audio_import_error}")

    p_audio = pyaudio_backend.PyAudio()
    device_streams: Dict[int, Any] = {}
    device_queues: Dict[int, queue.Queue] = {}
    device_threads: Dict[int, Thread] = {}
    device_info_map: Dict[int, Dict[str, Any]] = {}
    device_resamplers: Dict[int, StreamingAudioResampler] = {}
    device_drop_counts: Dict[int, int] = {int(idx): 0 for idx in device_indices}
    capture_errors: queue.Queue = queue.Queue()
    capture_start_event = Event()
    output_sequence = 0
    dropped_output_chunks = 0
    sync_missing_chunks = 0
    frame_samples = max(1, int(target_sample_rate * AUDIO_FRAME_SECONDS))
    debug_wav_file = None

    if debug_save_audio:
        debug_wav_file = wave.open(debug_save_audio, "wb")
        debug_wav_file.setnchannels(1)
        debug_wav_file.setsampwidth(2)
        debug_wav_file.setframerate(target_sample_rate)

    def capture_device(device_idx: int, native_rate: int, channels: int) -> None:
        samples_per_read = max(1, int(AUDIO_FRAME_SECONDS * native_rate))
        device_sequence = 0
        try:
            capture_start_event.wait()
            while not stop_event.is_set():
                data = device_streams[device_idx].read(samples_per_read, exception_on_overflow=False)
                captured_at = time.monotonic()
                samples = np.frombuffer(data, dtype=np.float32)
                if channels > 1:
                    samples = samples.reshape(-1, channels).mean(axis=1)
                samples = np.asarray(samples, dtype=np.float32).copy()
                device_sequence += 1
                packet = (device_sequence, captured_at, samples, native_rate)
                if backpressure_mode == "buffered":
                    queued = False
                    while not stop_event.is_set():
                        try:
                            device_queues[device_idx].put(packet, timeout=0.1)
                            queued = True
                            break
                        except queue.Full:
                            continue
                    if not queued:
                        break
                else:
                    try:
                        device_queues[device_idx].put_nowait(packet)
                    except queue.Full:
                        device_drop_counts[device_idx] += 1
                        try:
                            device_queues[device_idx].get_nowait()
                            device_queues[device_idx].put_nowait(packet)
                        except (queue.Empty, queue.Full) as exc:
                            raise RuntimeError(
                                f"设备 {device_idx} 音频队列无法恢复实时位置"
                            ) from exc
        except Exception as exc:
            if not stop_event.is_set():
                capture_errors.put((device_idx, str(exc)))

    def emit(samples, captured_at: float) -> None:
        nonlocal output_sequence, dropped_output_chunks
        samples = _limit_audio(samples)
        if debug_wav_file is not None:
            pcm = np.int16(np.clip(samples, -1.0, 1.0) * 32767)
            debug_wav_file.writeframes(pcm.tobytes())
        output_sequence += 1
        packet: Any = samples
        if emit_metadata:
            packet = {
                "type": "audio",
                "sequence": output_sequence,
                "captured_at": captured_at,
                "samples": samples,
                "dropped_before": dropped_output_chunks,
                "device_drops": dict(device_drop_counts),
                "sync_missing_chunks": sync_missing_chunks,
            }

        def on_drop() -> None:
            nonlocal dropped_output_chunks
            dropped_output_chunks += 1
            if isinstance(packet, dict):
                packet["dropped_before"] = dropped_output_chunks

        if not _put_audio_packet(
            output_queue,
            packet,
            on_drop,
            mode=backpressure_mode,
            stop_event=stop_event,
        ):
            raise RuntimeError("录音输出队列不可用，停止识别以避免静默丢帧")

    try:
        for raw_device_idx in device_indices:
            device_idx = int(raw_device_idx)
            info = p_audio.get_device_info_by_index(device_idx)
            device_info_map[device_idx] = info
            native_rate = int(info["defaultSampleRate"])
            channels = int(info.get("maxInputChannels", 0) or info.get("maxOutputChannels", 0))
            if native_rate <= 0 or channels <= 0:
                raise RuntimeError(f"设备 {device_idx} 没有可用声道或采样率")
            frames = max(1, int(AUDIO_FRAME_SECONDS * native_rate))
            device_streams[device_idx] = p_audio.open(
                format=pyaudio_backend.paFloat32,
                channels=channels,
                rate=native_rate,
                input=True,
                input_device_index=device_idx,
                frames_per_buffer=frames,
                start=False,
            )
            device_queues[device_idx] = queue.Queue(maxsize=120)
            device_resamplers[device_idx] = StreamingAudioResampler(native_rate, target_sample_rate)
            thread = Thread(target=capture_device, args=(device_idx, native_rate, channels), daemon=True)
            thread.start()
            device_threads[device_idx] = thread
            logger.info(
                "[Audio] device=%d name=%s rate=%d channels=%d",
                device_idx,
                info.get("name", "unknown"),
                native_rate,
                channels,
            )

        # All streams and capture threads are ready before any reader proceeds,
        # giving their per-device sequence numbers a common origin.
        for stream in device_streams.values():
            stream.start_stream()
        capture_start_event.set()

        selected = [int(idx) for idx in device_indices]
        if len(selected) == 1:
            # Fast path: no artificial multi-device synchronization delay.
            device_idx = selected[0]
            while not stop_event.is_set():
                try:
                    failed_device, message = capture_errors.get_nowait()
                    error_packet = {"type": "audio_error", "device": failed_device, "message": message}
                    _put_audio_packet(output_queue, error_packet, lambda: None)
                    stop_event.set()
                    break
                except queue.Empty:
                    pass
                try:
                    _, captured_at, samples, native_rate = device_queues[device_idx].get(timeout=0.1)
                except queue.Empty:
                    continue
                emit(device_resamplers[device_idx].process(samples), captured_at)
        else:
            # Multi-device path: the first selected device is the real-time
            # cadence anchor. Device-local sequence numbers cannot be compared
            # across streams: a slow first read would otherwise leave the two
            # queues permanently one slot apart. Consume one frame from every
            # device per anchor frame and use silence only for a genuine timeout.
            anchor_device = selected[0]
            sync_wait = AUDIO_FRAME_SECONDS * 2.0
            sync_tolerance = AUDIO_FRAME_SECONDS * 0.75
            clock_aligner = AdaptiveAudioClockAligner(sync_tolerance)
            pending_by_device: Dict[int, Any] = {}
            while not stop_event.is_set():
                try:
                    failed_device, message = capture_errors.get_nowait()
                    error_packet = {"type": "audio_error", "device": failed_device, "message": message}
                    _put_audio_packet(output_queue, error_packet, lambda: None)
                    stop_event.set()
                    break
                except queue.Empty:
                    pass

                try:
                    _, anchor_time, anchor_samples, anchor_rate = device_queues[anchor_device].get(timeout=0.1)
                except queue.Empty:
                    continue

                tracks = []
                capture_times = []
                for device_idx in selected:
                    if device_idx == anchor_device:
                        captured_at, samples, native_rate = anchor_time, anchor_samples, anchor_rate
                    else:
                        packet = pending_by_device.pop(device_idx, None)
                        deadline = time.monotonic() + sync_wait
                        while True:
                            if packet is None:
                                remaining = max(0.0, deadline - time.monotonic())
                                if remaining <= 0:
                                    break
                                try:
                                    packet = device_queues[device_idx].get(timeout=remaining)
                                except queue.Empty:
                                    packet = None
                                    break
                            _, candidate_at, candidate_samples, candidate_rate = packet
                            aligned_delta = clock_aligner.delta(
                                device_idx, anchor_time, candidate_at
                            )
                            if aligned_delta < -sync_tolerance:
                                sync_missing_chunks += 1
                                packet = None
                                continue
                            if aligned_delta > sync_tolerance:
                                pending_by_device[device_idx] = packet
                                packet = None
                                break
                            clock_aligner.accept(device_idx, anchor_time, candidate_at)
                            captured_at, samples, native_rate = (
                                candidate_at, candidate_samples, candidate_rate
                            )
                            break
                        if packet is None:
                            sync_missing_chunks += 1
                            tracks.append(np.zeros(frame_samples, dtype=np.float32))
                            continue
                    capture_times.append(captured_at)
                    resampled = device_resamplers[device_idx].process(samples)
                    if len(resampled) < frame_samples:
                        resampled = np.pad(resampled, (0, frame_samples - len(resampled)))
                    elif len(resampled) > frame_samples:
                        resampled = resampled[:frame_samples]
                    tracks.append(resampled)

                if not tracks:
                    continue
                mixed = _mix_audio_tracks(
                    tracks, mix_mode, expected_track_count=len(selected)
                )
                emit(mixed, min(capture_times) if capture_times else anchor_time)
    finally:
        stop_event.set()
        capture_start_event.set()
        for stream in device_streams.values():
            try:
                if stream.is_active():
                    stream.stop_stream()
                stream.close()
            except Exception:
                pass
        for thread in device_threads.values():
            if thread.is_alive():
                thread.join(timeout=1.0)
        try:
            p_audio.terminate()
        except Exception:
            pass
        if debug_wav_file is not None:
            try:
                debug_wav_file.close()
            except Exception:
                pass


class MyPrinter:
    """Console-compatible final transcript sink; harmless under .pyw."""

    def __init__(self):
        self.prev_result = ""

    def do_print(self, result) -> None:
        if result and self.prev_result != result:
            self.prev_result = result
            print(result, end="\n", flush=True)

    def on_endpoint(self) -> None:
        print("\n", end="", flush=True)


def _single_device_recording_worker(
    device_idx: int,
    output_queue,
    stop_event,
    target_sample_rate: int,
    backpressure_mode: str,
) -> None:
    """Capture one device in its own PortAudio process.

    PyAudioWPatch can stall a WASAPI loopback stream when a microphone stream is
    open in the same process, and its multi-stream teardown can access invalid
    native state. Process isolation avoids both PortAudio limitations.
    """
    configure_current_process_priority("above_normal")
    try:
        start_recording(
            [device_idx],
            output_queue,
            stop_event,
            target_sample_rate=target_sample_rate,
            emit_metadata=True,
            backpressure_mode=backpressure_mode,
        )
    except BaseException as exc:
        try:
            output_queue.put_nowait({"type": "audio_error", "device": device_idx, "message": str(exc)})
        except Exception:
            pass


def _audio_packet_device_drop_total(packet: Dict[str, Any], device_idx: int) -> int:
    """Combine capture-thread drops and child-output-queue drops for one device."""
    per_device = packet.get("device_drops") or {}
    capture_drops = per_device.get(device_idx, per_device.get(str(device_idx), 0))
    return max(0, int(packet.get("dropped_before", 0))) + max(0, int(capture_drops or 0))


def _record_multiple_devices_isolated(
    device_indices,
    output_queue,
    stop_event,
    mix_mode: str,
    target_sample_rate: int,
    backpressure_mode: str,
) -> None:
    """Mix device streams captured by independent child processes."""
    ctx = multiprocessing.get_context("spawn")
    selected = [int(item) for item in device_indices]
    device_queues = {idx: ctx.Queue(maxsize=120) for idx in selected}
    processes = {
        idx: ctx.Process(
            target=_single_device_recording_worker,
            args=(idx, device_queues[idx], stop_event, target_sample_rate, backpressure_mode),
            name=f"SubtitleAudioDevice-{idx}",
        )
        for idx in selected
    }
    output_sequence = 0
    dropped_output_chunks = 0
    sync_missing_chunks = 0
    anchor_device = selected[0]
    sync_tolerance = AUDIO_FRAME_SECONDS * 0.75
    clock_aligner = AdaptiveAudioClockAligner(sync_tolerance)
    frame_samples = max(1, int(target_sample_rate * AUDIO_FRAME_SECONDS))
    pending_by_device: Dict[int, Any] = {}
    try:
        for process in processes.values():
            process.start()
        while not stop_event.is_set():
            try:
                anchor_packet = device_queues[anchor_device].get(timeout=0.1)
            except queue.Empty:
                dead = [idx for idx, process in processes.items() if not process.is_alive()]
                if dead:
                    raise RuntimeError(f"音频设备子进程异常退出：{dead}")
                continue
            if anchor_packet.get("type") == "audio_error":
                _put_audio_packet(output_queue, anchor_packet, lambda: None)
                stop_event.set()
                break

            packets_by_device = {anchor_device: anchor_packet}
            anchor_time = float(anchor_packet["captured_at"])
            # All secondary devices share one deadline. Per-device deadlines made
            # latency grow linearly with the number of selected inputs.
            sync_deadline = time.monotonic() + AUDIO_FRAME_SECONDS * 2.0
            for device_idx in selected[1:]:
                packet = pending_by_device.pop(device_idx, None)
                while True:
                    if packet is None:
                        remaining = max(0.0, sync_deadline - time.monotonic())
                        if remaining <= 0:
                            break
                        try:
                            packet = device_queues[device_idx].get(timeout=remaining)
                        except queue.Empty:
                            packet = None
                            break
                    if packet.get("type") == "audio_error":
                        _put_audio_packet(output_queue, packet, lambda: None)
                        stop_event.set()
                        break
                    candidate_at = float(packet["captured_at"])
                    aligned_delta = clock_aligner.delta(
                        device_idx, anchor_time, candidate_at
                    )
                    if aligned_delta < -sync_tolerance:
                        sync_missing_chunks += 1
                        packet = None
                        continue
                    if aligned_delta > sync_tolerance:
                        pending_by_device[device_idx] = packet
                        packet = None
                        break
                    clock_aligner.accept(device_idx, anchor_time, candidate_at)
                    packets_by_device[device_idx] = packet
                    break
                if stop_event.is_set():
                    break
                if device_idx not in packets_by_device:
                    sync_missing_chunks += 1
            if stop_event.is_set():
                break

            packets = list(packets_by_device.values())
            tracks = []
            for device_idx in selected:
                device_packet = packets_by_device.get(device_idx)
                if device_packet is None:
                    tracks.append(np.zeros(frame_samples, dtype=np.float32))
                    continue
                track = np.asarray(device_packet["samples"], dtype=np.float32)
                if len(track) < frame_samples:
                    track = np.pad(track, (0, frame_samples - len(track)))
                elif len(track) > frame_samples:
                    track = track[:frame_samples]
                tracks.append(track)
            mixed = _mix_audio_tracks(
                tracks, mix_mode, expected_track_count=len(selected)
            )
            output_sequence += 1
            packet = {
                "type": "audio",
                "sequence": output_sequence,
                "captured_at": min(float(item["captured_at"]) for item in packets),
                "samples": mixed,
                "dropped_before": dropped_output_chunks,
                "device_drops": {
                    idx: _audio_packet_device_drop_total(packets_by_device[idx], idx)
                    if idx in packets_by_device else 0
                    for idx in selected
                },
                "sync_missing_chunks": sync_missing_chunks,
            }

            def on_drop() -> None:
                nonlocal dropped_output_chunks
                dropped_output_chunks += 1
                packet["dropped_before"] = dropped_output_chunks

            if not _put_audio_packet(
                output_queue,
                packet,
                on_drop,
                mode=backpressure_mode,
                stop_event=stop_event,
            ):
                raise RuntimeError("多设备混音输出队列不可用，停止识别以避免静默丢帧")
    finally:
        stop_event.set()
        for process in processes.values():
            process.join(timeout=1.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=0.5)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(timeout=0.2)
            try:
                process.close()
            except Exception:
                pass
        for device_queue in device_queues.values():
            try:
                device_queue.close()
                device_queue.join_thread()
            except Exception:
                try:
                    device_queue.cancel_join_thread()
                except Exception:
                    pass


def recording_process_entrypoint(
    device_indices,
    output_queue,
    stop_event,
    mix_mode="average",
    debug_save_audio="",
    target_sample_rate=16000,
    backpressure_mode="buffered",
) -> None:
    """Child-process entrypoint with explicit error reporting."""
    configure_current_process_priority("above_normal")
    try:
        if not ensure_audio_dependencies():
            raise RuntimeError(f"缺少音频依赖：{_audio_import_error}")
        if len(device_indices) > 1 and not debug_save_audio:
            _record_multiple_devices_isolated(
                device_indices,
                output_queue,
                stop_event,
                mix_mode,
                target_sample_rate,
                backpressure_mode,
            )
        else:
            start_recording(
                device_indices,
                output_queue,
                stop_event,
                mix_mode,
                debug_save_audio,
                target_sample_rate,
                emit_metadata=True,
                backpressure_mode=backpressure_mode,
            )
    except Exception as exc:
        logger.error("录音进程异常退出：%s", exc, exc_info=True)
        try:
            output_queue.put(
                {"type": "audio_error", "device": -1, "message": str(exc)},
                timeout=1.0,
            )
        except Exception:
            pass


class RollingAudioBuffer:
    def __init__(self, max_samples: int):
        self.max_samples = max(1, int(max_samples))
        self._chunks: deque = deque()
        self._sample_count = 0

    def clear(self) -> None:
        self._chunks.clear()
        self._sample_count = 0

    def append(self, samples) -> None:
        chunk = np.asarray(samples, dtype=np.float32).reshape(-1).copy()
        if chunk.size == 0:
            return
        if len(chunk) > self.max_samples:
            chunk = chunk[-self.max_samples:]
        self._chunks.append(chunk)
        self._sample_count += len(chunk)
        overflow = self._sample_count - self.max_samples
        while overflow > 0 and self._chunks:
            oldest = self._chunks[0]
            if len(oldest) <= overflow:
                self._chunks.popleft()
                self._sample_count -= len(oldest)
                overflow -= len(oldest)
            else:
                self._chunks[0] = oldest[overflow:].copy()
                self._sample_count -= overflow
                overflow = 0

    def to_array(self):
        if not self._chunks:
            return np.array([], dtype=np.float32)
        return np.concatenate(list(self._chunks))

    def __bool__(self) -> bool:
        return bool(self._chunks)

    @property
    def sample_count(self) -> int:
        return self._sample_count


def _disable_child_file_logging() -> None:
    """Avoid multiple spawned processes rotating/writing the same log file."""
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        try:
            handler.close()
        except Exception:
            pass
        root_logger.removeHandler(handler)
    root_logger.addHandler(logging.NullHandler())




# ================== 2. ASR 异步推理 ==================










class Qwen3ASRStreamingHTTPRecognizer:
    """Desktop proxy for Qwen3-ASR's official streaming demo HTTP contract.

    Model inference remains entirely upstream in qwen_asr.cli.demo_streaming:
    Qwen3ASRModel.LLM -> init_streaming_state -> streaming_transcribe ->
    finish_streaming_transcribe.  This class only batches 16 kHz float32 PCM and
    serializes /api/start, /api/chunk and /api/finish calls.
    """

    def __init__(
        self,
        model_name: str,
        on_result: Callable[[str, bool, int], None],
        *,
        server_url: str = QWEN3_ASR_DEFAULT_SERVER_URL,
        language: str = QWEN3_ASR_DEFAULT_LANGUAGE,
        sample_rate: int = 16000,
        chunk_size_sec: float = QWEN3_ASR_DEFAULT_CHUNK_SIZE_SEC,
        unfixed_chunk_num: int = QWEN3_ASR_DEFAULT_UNFIXED_CHUNK_NUM,
        unfixed_token_num: int = QWEN3_ASR_DEFAULT_UNFIXED_TOKEN_NUM,
        push_interval_ms: int = QWEN3_ASR_DEFAULT_PUSH_INTERVAL_MS,
        request_timeout: float = 60.0,
        on_error: Optional[Callable[[str], None]] = None,
        on_metrics: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_discontinuity: Optional[Callable[[int], None]] = None,
        preserve_audio: bool = True,
        audio_enqueue_timeout_seconds: float = 5.0,
        utterance_id_offset: int = 0,
    ):
        if int(sample_rate) != 16000:
            raise ValueError("Qwen3-ASR official streaming requires 16 kHz mono PCM")
        self.model_name = str(model_name or QWEN3_ASR_MODEL_ID)
        self.on_result = on_result
        self.on_error = on_error
        self.on_metrics = on_metrics
        self.on_discontinuity = on_discontinuity
        self.sample_rate = 16000
        self.server_url = str(server_url or QWEN3_ASR_DEFAULT_SERVER_URL).rstrip("/")
        self.language = str(language or "").strip()
        self.chunk_size_sec = max(0.1, float(chunk_size_sec))
        self.unfixed_chunk_num = max(0, int(unfixed_chunk_num))
        self.unfixed_token_num = max(0, int(unfixed_token_num))
        self.request_timeout = max(1.0, float(request_timeout))
        self._preserve_audio = bool(preserve_audio)
        self._audio_enqueue_timeout_seconds = max(0.5, float(audio_enqueue_timeout_seconds))
        self._utterance_id_offset = max(0, int(utterance_id_offset))
        self._id_lock = Lock()
        self._next_utterance_id = 0
        self._active_utterance_id = 0
        self._active_lock = Lock()
        self._command_queue: queue.Queue = queue.Queue(maxsize=64)
        self._stop = Event()
        self._closed = Event()
        self._ready = Event()
        self._startup_error = ""
        self._fatal_reported = False
        self._sequence = 0
        self._audio_batch_lock = Lock()
        self._audio_batch: List[Any] = []
        self._audio_batch_sample_count = 0
        self._audio_batch_target_samples = max(
            1, round(self.sample_rate * max(0.04, float(push_interval_ms) / 1000.0))
        )
        self._sessions: Dict[int, Dict[str, Any]] = {}
        self._result_condition = Condition(Lock())
        self._result_finals: deque[tuple[str, bool, int]] = deque()
        self._result_previews: OrderedDict[int, tuple[str, bool, int]] = OrderedDict()
        self._result_accepting = True
        if self.language:
            logger.warning(
                "[QWEN_STREAM] official qwen-asr-demo-streaming does not expose a forced-language "
                "parameter; configured language=%s is ignored and upstream auto-detection is used",
                self.language,
            )
        self._result_dispatcher = Thread(
            target=self._dispatch_results, name="Qwen3ASRResultDispatcher", daemon=True
        )
        self._result_dispatcher.start()
        self._worker_thread = Thread(
            target=self._run, name="Qwen3ASROfficialHTTPWorker", daemon=False
        )
        self._worker_thread.start()

    def _url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        url = self.server_url + path
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if query:
                url += "?" + query
        return url

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        raw_body: Optional[bytes] = None,
        content_type: str = "application/json",
    ) -> Dict[str, Any]:
        headers: Dict[str, str] = {}
        if raw_body is not None:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(
            self._url(path, params), data=raw_body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                detail = str(exc)
            raise RuntimeError(f"Qwen official streaming HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"无法连接 Qwen3-ASR official streaming server：{self.server_url} ({exc.reason})"
            ) from exc
        if not payload:
            return {}
        try:
            value = json.loads(payload.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError("Qwen official streaming server 返回了无效 JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Qwen official streaming server 返回格式错误")
        if value.get("error"):
            raise RuntimeError(str(value.get("error")))
        return value

    def _probe_official_protocol(self) -> None:
        """Probe the exact upstream start/finish endpoints with an empty session."""
        started = self._request_json("/api/start", method="POST", raw_body=b"")
        session_id = str(started.get("session_id", "") or "")
        if not session_id:
            raise RuntimeError("Qwen official /api/start 未返回 session_id")
        self._request_json(
            "/api/finish", method="POST", params={"session_id": session_id}, raw_body=b""
        )

    def _report_fatal(self, message: str) -> None:
        if self._fatal_reported:
            return
        self._fatal_reported = True
        self._startup_error = str(message)
        self._ready.set()
        self._stop.set()
        if self.on_error is not None:
            try:
                self.on_error(str(message))
            except Exception:
                logger.error("Qwen ASR 错误回调异常", exc_info=True)

    def _enqueue_result(self, text: str, is_final: bool, utterance_id: int) -> None:
        item = (str(text or ""), bool(is_final), int(utterance_id))
        with self._result_condition:
            if not self._result_accepting:
                return
            if is_final:
                self._result_finals.append(item)
                self._result_previews.pop(int(utterance_id), None)
            else:
                self._result_previews[int(utterance_id)] = item
                self._result_previews.move_to_end(int(utterance_id))
                while len(self._result_previews) > PREVIEW_RESULT_CACHE_MAX:
                    self._result_previews.popitem(last=False)
            self._result_condition.notify()

    def _dispatch_results(self) -> None:
        while True:
            with self._result_condition:
                while self._result_accepting and not self._result_finals and not self._result_previews:
                    self._result_condition.wait(timeout=0.2)
                if not self._result_accepting and not self._result_finals:
                    self._result_previews.clear()
                    return
                if self._result_finals:
                    item = self._result_finals.popleft()
                elif self._result_previews:
                    _, item = self._result_previews.popitem(last=False)
                else:
                    continue
            try:
                self.on_result(*item)
            except Exception:
                logger.error("Qwen ASR 结果回调异常", exc_info=True)

    def _stop_result_dispatcher(self, *, drain_finals: bool, timeout: float) -> bool:
        with self._result_condition:
            self._result_accepting = False
            if not drain_finals:
                self._result_finals.clear()
            self._result_previews.clear()
            self._result_condition.notify_all()
        if current_thread() is not self._result_dispatcher:
            self._result_dispatcher.join(timeout=max(0.1, float(timeout)))
        return not self._result_dispatcher.is_alive()

    def _emit_metrics(
        self,
        utterance_id: int,
        *,
        state: str,
        audio_samples: int,
        enqueued_at: float,
        inference_started_at: float,
        has_text: bool,
    ) -> None:
        if self.on_metrics is None:
            return
        self._sequence += 1
        now = time.monotonic()
        metrics = {
            "sequence": self._sequence,
            "state": str(state),
            "utterance_id": int(utterance_id),
            "audio_duration_ms": round(max(0, int(audio_samples)) / self.sample_rate * 1000),
            "queue_latency_ms": max(0, round((inference_started_at - enqueued_at) * 1000)),
            "inference_latency_ms": max(0, round((now - inference_started_at) * 1000)),
            "has_text": bool(has_text),
            "feature_cursor": 0,
            "backend": "qwen3_asr_official_demo_streaming",
        }
        try:
            self.on_metrics(metrics)
        except Exception:
            logger.error("Qwen ASR 指标回调异常", exc_info=True)

    @staticmethod
    def _float32_bytes(samples: Any) -> tuple[bytes, int]:
        import numpy as numpy_for_qwen
        audio = numpy_for_qwen.asarray(samples, dtype=numpy_for_qwen.float32).reshape(-1)
        if audio.size == 0:
            return b"", 0
        audio = numpy_for_qwen.ascontiguousarray(audio)
        return audio.tobytes(), int(audio.size)

    def _push_audio(self, utterance_id: int, samples: Any, enqueued_at: float) -> None:
        session = self._sessions.get(int(utterance_id))
        if session is None:
            raise RuntimeError(f"Qwen utterance {utterance_id} 尚未建立 server session")
        raw, sample_count = self._float32_bytes(samples)
        if not raw:
            return
        started_at = time.monotonic()
        result = self._request_json(
            "/api/chunk",
            method="POST",
            params={"session_id": session["session_id"]},
            raw_body=raw,
            content_type="application/octet-stream",
        )
        text = str(result.get("text", "") or "")
        if text != session.get("last_text", ""):
            session["last_text"] = text
            self._enqueue_result(text, False, int(utterance_id))
        self._emit_metrics(
            int(utterance_id),
            state="interim",
            audio_samples=sample_count,
            enqueued_at=enqueued_at,
            inference_started_at=started_at,
            has_text=bool(text),
        )

    def _finish_session(self, utterance_id: int, reason: str, enqueued_at: float) -> None:
        session = self._sessions.get(int(utterance_id))
        if session is None:
            return
        started_at = time.monotonic()
        result = self._request_json(
            "/api/finish",
            method="POST",
            params={"session_id": session["session_id"]},
            raw_body=b"",
        )
        text = str(result.get("text", "") or "")
        self._enqueue_result(text, True, int(utterance_id))
        self._emit_metrics(
            int(utterance_id),
            state="final",
            audio_samples=0,
            enqueued_at=enqueued_at,
            inference_started_at=started_at,
            has_text=bool(text),
        )
        self._sessions.pop(int(utterance_id), None)
        logger.info(
            "[QWEN_STREAM] utterance=%d finalized reason=%s chars=%d",
            int(utterance_id), reason, len(text),
        )

    def _run(self) -> None:
        try:
            self._probe_official_protocol()
            self._ready.set()
            while not self._stop.is_set():
                try:
                    command = self._command_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    kind = command[0]
                    if kind == "shutdown":
                        now = time.monotonic()
                        for utterance_id in list(self._sessions):
                            try:
                                self._finish_session(int(utterance_id), "shutdown", now)
                            except Exception:
                                logger.error(
                                    "Qwen shutdown finalize failed utterance=%s",
                                    utterance_id,
                                    exc_info=True,
                                )
                        return
                    if kind == "begin":
                        _, utterance_id, samples, enqueued_at = command
                        started_at = time.monotonic()
                        # Official demo: POST /api/start with no configuration body.
                        result = self._request_json("/api/start", method="POST", raw_body=b"")
                        session_id = str(result.get("session_id", "") or "")
                        if not session_id:
                            raise RuntimeError("Qwen official streaming server 未返回 session_id")
                        self._sessions[int(utterance_id)] = {
                            "session_id": session_id,
                            "last_text": "",
                        }
                        self._emit_metrics(
                            int(utterance_id), state="begin", audio_samples=0,
                            enqueued_at=enqueued_at, inference_started_at=started_at, has_text=False,
                        )
                        self._push_audio(int(utterance_id), samples, enqueued_at)
                    elif kind == "audio":
                        _, utterance_id, samples, enqueued_at = command
                        self._push_audio(int(utterance_id), samples, enqueued_at)
                    elif kind == "final":
                        _, utterance_id, reason, enqueued_at = command
                        self._finish_session(int(utterance_id), str(reason), enqueued_at)
                    else:
                        logger.warning("未知 Qwen ASR command：%s", kind)
                finally:
                    self._command_queue.task_done()
        except Exception as exc:
            if not self._stop.is_set():
                self._report_fatal(f"Qwen3-ASR official streaming backend 异常：{exc}")
        finally:
            self._closed.set()
            self._ready.set()

    def warmup(self) -> float:
        started = time.monotonic()
        if not self._ready.wait(timeout=max(10.0, min(300.0, self.request_timeout + 5.0))):
            raise TimeoutError("Qwen3-ASR official streaming server 连接超时")
        if self._startup_error:
            raise RuntimeError(self._startup_error)
        if self._closed.is_set() and not self._worker_thread.is_alive():
            raise RuntimeError("Qwen3-ASR official streaming worker 已退出")
        return max(0.0, time.monotonic() - started)

    def _enqueue_command(self, command: tuple, *, critical: bool) -> bool:
        if self._stop.is_set() or self._closed.is_set():
            raise RuntimeError("Qwen3-ASR official streaming worker 未运行")
        try:
            if critical or self._preserve_audio:
                self._command_queue.put(
                    command,
                    timeout=self._audio_enqueue_timeout_seconds if not critical else 2.0,
                )
            else:
                self._command_queue.put_nowait(command)
            return True
        except queue.Full:
            if critical or self._preserve_audio:
                message = "Qwen official HTTP 队列持续满载，无法保证音频/句界完整性"
                self._report_fatal(message)
                raise RuntimeError(message)
            return False

    def begin_utterance(self, samples, sample_rate: Optional[int] = None) -> int:
        if int(sample_rate or self.sample_rate) != self.sample_rate:
            raise ValueError("Qwen3-ASR official streaming only accepts 16 kHz PCM")
        with self._id_lock:
            self._next_utterance_id += 1
            utterance_id = self._utterance_id_offset + self._next_utterance_id
        with self._active_lock:
            self._active_utterance_id = utterance_id
        import numpy as numpy_for_qwen
        audio = numpy_for_qwen.asarray(samples, dtype=numpy_for_qwen.float32).reshape(-1)
        self._enqueue_command(("begin", utterance_id, audio, time.monotonic()), critical=True)
        return utterance_id

    def accept_audio(self, samples, sample_rate: Optional[int] = None) -> None:
        if int(sample_rate or self.sample_rate) != self.sample_rate:
            raise ValueError("Qwen3-ASR official streaming only accepts 16 kHz PCM")
        import numpy as numpy_for_qwen
        audio = numpy_for_qwen.asarray(samples, dtype=numpy_for_qwen.float32).reshape(-1)
        if audio.size == 0:
            return
        with self._active_lock:
            utterance_id = int(self._active_utterance_id)
        if utterance_id <= 0:
            return
        batch = None
        with self._audio_batch_lock:
            self._audio_batch.append(audio)
            self._audio_batch_sample_count += int(audio.size)
            if self._audio_batch_sample_count >= self._audio_batch_target_samples:
                batch = self._audio_batch
                self._audio_batch = []
                self._audio_batch_sample_count = 0
        if batch:
            combined = batch[0] if len(batch) == 1 else numpy_for_qwen.concatenate(batch)
            if not self._enqueue_command(("audio", utterance_id, combined, time.monotonic()), critical=False):
                if self.on_discontinuity is not None:
                    self.on_discontinuity(int(combined.size))

    def _flush_audio_batch(self, utterance_id: int) -> None:
        import numpy as numpy_for_qwen
        with self._audio_batch_lock:
            batch = self._audio_batch
            self._audio_batch = []
            self._audio_batch_sample_count = 0
        if batch:
            combined = batch[0] if len(batch) == 1 else numpy_for_qwen.concatenate(batch)
            self._enqueue_command(("audio", int(utterance_id), combined, time.monotonic()), critical=True)

    def finalize_utterance(self, reason: str = "endpoint") -> None:
        with self._active_lock:
            utterance_id = int(self._active_utterance_id)
            self._active_utterance_id = 0
        if utterance_id <= 0:
            return
        self._flush_audio_batch(utterance_id)
        self._enqueue_command(("final", utterance_id, str(reason), time.monotonic()), critical=True)

    def is_alive(self) -> bool:
        return bool(not self._stop.is_set() and self._worker_thread.is_alive())

    def shutdown(self) -> bool:
        """Gracefully finish every official streaming session before stopping the worker."""
        with self._active_lock:
            active_utterance_id = int(self._active_utterance_id)
            self._active_utterance_id = 0
        if active_utterance_id > 0 and not self._closed.is_set():
            try:
                self._flush_audio_batch(active_utterance_id)
            except Exception:
                logger.error("Qwen shutdown audio flush failed", exc_info=True)
        else:
            with self._audio_batch_lock:
                self._audio_batch = []
                self._audio_batch_sample_count = 0

        if not self._closed.is_set():
            try:
                self._command_queue.put(("shutdown", time.monotonic()), timeout=2.0)
            except Exception:
                logger.error("Qwen shutdown command enqueue failed", exc_info=True)

        if current_thread() is not self._worker_thread:
            self._worker_thread.join(timeout=min(10.0, self.request_timeout + 1.0))
        if self._worker_thread.is_alive():
            self._stop.set()
            logger.error("Qwen official streaming worker did not stop within graceful timeout")
        else:
            self._stop.set()
        stopped_dispatch = self._stop_result_dispatcher(drain_finals=True, timeout=5.0)
        return bool(not self._worker_thread.is_alive() and stopped_dispatch)

@dataclass
class _ResultSlot:
    """Lightweight one-shot result holder for SessionActor commands."""

    ok: bool = False
    value: Any = None
    ready: Event = field(default_factory=Event)

    def set(self, ok: bool, value: Any) -> None:
        self.ok = bool(ok)
        self.value = value
        self.ready.set()

    def get(self, timeout: float) -> tuple[bool, Any]:
        if not self.ready.wait(timeout=max(0.0, float(timeout))):
            raise queue.Empty()
        return self.ok, self.value


class SessionActor:
    """Single-threaded deadline-aware actor for subtitle session mutations."""
    def __init__(self, session: "RealtimeSubtitleSession"):
        self._session = session
        self._commands: queue.Queue = queue.Queue(maxsize=SESSION_ACTOR_COMMAND_QUEUE_SIZE)
        self._closed = Event()
        self._stop_requested = Event()
        self._thread = Thread(target=self._run, name="SubtitleSessionActor", daemon=False)
        self._thread.start()
    def _run(self) -> None:
        while True:
            if self._stop_requested.is_set() and self._commands.empty():
                return
            try:
                item = self._commands.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                self._commands.task_done(); return
            fn, args, kwargs, result_slot, deadline, cancelled = item
            try:
                if cancelled.is_set() or time.monotonic() > deadline:
                    result_slot.set(False, TimeoutError("Session command expired before execution"))
                    continue
                result_slot.set(True, fn(*args, **kwargs))
            except BaseException as exc:
                result_slot.set(False, exc)
            finally:
                self._commands.task_done()
    def _call(self, method: str, *args, timeout: float = 15.0, **kwargs):
        if self._closed.is_set() or self._stop_requested.is_set():
            raise RuntimeError("Subtitle Session Actor 已关闭")
        timeout = max(0.1, float(timeout))
        deadline = time.monotonic() + timeout
        cancelled = Event()
        result_slot = _ResultSlot()
        self._commands.put((getattr(self._session, method), args, kwargs, result_slot, deadline, cancelled), timeout=min(2.0, timeout))
        try:
            ok, value = result_slot.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty as exc:
            cancelled.set()
            raise TimeoutError(f"Subtitle Session Actor command timeout: {method}") from exc
        if ok:
            return value
        raise value
    def ingest_asr(self, *args, **kwargs):
        return self._call("ingest_asr", *args, **kwargs)
    def latest_source(self, *args, **kwargs):
        return self._call("latest_source", *args, **kwargs)
    def close(self, timeout: float = 5.0) -> bool:
        if self._closed.is_set():
            return not self._thread.is_alive()
        try:
            self._call("close", timeout=max(1.0, timeout / 2))
        except Exception:
            logger.error("Subtitle Session Actor underlying close failed", exc_info=True)
        finally:
            self._closed.set()
            self._stop_requested.set()
            while True:
                try:
                    pending = self._commands.get_nowait()
                except queue.Empty:
                    break
                try:
                    if pending is not None:
                        _fn, _args, _kwargs, result_slot, _deadline, cancelled = pending
                        cancelled.set()
                        result_slot.set(False, RuntimeError("Session Actor closed before execution"))
                finally:
                    self._commands.task_done()
            if current_thread() is not self._thread:
                self._thread.join(timeout=max(0.1, timeout))
        return not self._thread.is_alive()


# ================== 3. 主应用类 ==================

class SubtitleApp:

    def __init__(self, args):
            self.args = args
            required_runtime_args = (
                "vad_threshold", "min_silence_duration", "min_speech_duration",
                "speech_preroll_ms", "max_utterance_seconds", "forced_segment_overlap_ms",
                "disable_hybrid_endpoint", "endpoint_punctuation_hold_ms",
                "endpoint_min_utterance_ms", "max_drain_chunks",
            )
            missing = [name for name in required_runtime_args if not hasattr(args, name)]
            if missing:
                raise ValueError("SubtitleApp 缺少已验证的运行参数：" + ", ".join(missing))

            self._persisted_settings = _load_application_settings()
            persisted_display = _apply_persisted_settings_to_args(args, self._persisted_settings)
            configure_current_process_priority("above_normal")
            self.session_id = uuid.uuid4().hex[:12]
            self.killed = False
            self._lifecycle_lock = Lock()
            self._lifecycle_state = "running"
            self.event_bus = SessionEventBus(
                max_queue_size=512,
                max_pending_finals=200,
                recovery_path=os.path.join(_data_dir, "recovery", "transcript-final.journal"),
                final_burst=8,
            )
            raw_session = RealtimeSubtitleSession(
                self.session_id,
                self.event_bus,
                source_stability_window=getattr(args, "stable_partial_threshold", 3),
            )
            self.subtitle_session = SessionActor(raw_session)
            self._transcript_event_queue: queue.Queue = queue.Queue(maxsize=512)
            self._transcript_controller_stop = Event()
            self._transcript_controller_thread: Optional[Thread] = None
            self.event_bus.subscribe(
                RealtimeSubtitleSession.TRANSCRIPT_EVENT,
                self._queue_transcript_event,
                session_guard=self._event_session_guard,
            )
            self._start_transcript_controller()

            self.audio_cursor = AudioCursorState()
            self._audio_cursor_lock = Lock()
            self._shutdown_event = Event()
            self._cleanup_started = False
            self._cleanup_lock = Lock()
            self._cleanup_complete = Event()
            self._worker_stopped_event = Event()
            self._cleanup_succeeded = False
            self._cleanup_failures: List[str] = []
            self._close_requested = False
            self._endpoint_lock = Lock()
            self._endpoint_text = ""
            self._endpoint_last_change_at = 0.0
            self._endpoint_stable_punctuation = ""
            self._endpoint_stable_punctuation_at = 0.0
            self._asr_failure_event = Event()
            self._asr_failure_message = ""
            self._live_asr_lock = Lock()
            self._live_asr_utterance_id = 0
            self._segment_overlap_lock = Lock()
            self._forced_continuation_from: Dict[int, int] = {}
            self._final_source_by_utterance: OrderedDict[int, str] = OrderedDict()
            self._forced_overlap_dedup_max: Dict[int, int] = {}
            self.stop_event: Optional[Any] = None
            self.recording_process: Optional[multiprocessing.Process] = None
            self.worker_thread: Optional[Thread] = None
            self.dependency_loader_thread: Optional[Thread] = None

            self._runtime_settings_lock = Lock()
            self._runtime_settings_changed = Event()
            self._runtime_settings: Dict[str, Any] = {
                "vad_threshold": float(args.vad_threshold),
                "min_silence_duration": float(args.min_silence_duration),
                "min_speech_duration": float(args.min_speech_duration),
                "speech_preroll_ms": int(args.speech_preroll_ms),
                "max_utterance_seconds": float(args.max_utterance_seconds),
                "forced_segment_overlap_ms": int(args.forced_segment_overlap_ms),
                "enable_hybrid_endpoint": not bool(args.disable_hybrid_endpoint),
                "endpoint_punctuation_hold_ms": int(args.endpoint_punctuation_hold_ms),
                "endpoint_min_utterance_ms": int(args.endpoint_min_utterance_ms),
                "max_drain_chunks": int(args.max_drain_chunks),
            }
            self.settings_window: Optional[tk.Toplevel] = None
            self._ui_preferences: Dict[str, Any] = {
                "source_font_size": 26,
                "opacity": 0.95,
                "topmost": True,
            }
            self._ui_preferences.update(persisted_display)

            self.ui_buffer_lock = Lock()
            self.ui_update_buffer: Dict[str, Any] = {
                "SRC": None,
                "SRC_FULL_TEXT": "",
                "SRC_DISPLAY_TEXT": "",
                "SRC_DISPLAY_IS_FINAL": False,
                "SRC_DISPLAY_AT": 0.0,
                "SRC_UTTERANCE_ID": 0,
                "IS_FINAL": False,
                "SOURCE_REVISION": 0,
                "SOURCE_STATE": "interim",
                "STABLE_SOURCE_TEXT": "",
                "UNSTABLE_SOURCE_TEXT": "",
            }
            self.ui_event_queue: queue.Queue = queue.Queue(maxsize=100)
            self.ui_error_queue: queue.Queue = queue.Queue(maxsize=32)
            self._last_wrap_width = 0
            self._last_rendered: Dict[str, tuple] = {
                "status": (None, None),
                "source": (None, None),
            }
            self._last_source_render_at = 0.0

            self.root: Optional[tk.Tk] = None
            self.subtitle_label: Optional[tk.Label] = None
            self.status_label: Optional[tk.Label] = None
            self.settings_button: Optional[tk.Button] = None
            self.async_asr: Optional[Any] = None
            self.vad = None
            self.sample_rate = 16000
            self.selected_devices: List[int] = []
            self._last_audio_packet_at = 0.0
            self._last_asr_result_at = 0.0
            self._stats_lock = Lock()
            self.stats = {
                "recognize_count": 0,
                "audio_chunks": 0,
                "dropped_audio_chunks": 0,
                "max_audio_queue_depth": 0,
                "max_audio_age_ms": 0,
                "audio_discontinuity_count": 0,
                "asr_tasks": 0,
                "max_asr_queue_latency_ms": 0,
                "max_asr_inference_latency_ms": 0,
                "asr_restart_count": 0,
                "subtitle_interim_count": 0,
                "subtitle_stable_count": 0,
                "subtitle_final_count": 0,
                "subtitle_revision_count": 0,
                "subtitle_flicker_count": 0,
                "dropped_transcript_previews": 0,
                "hybrid_endpoint_count": 0,
                "forced_endpoint_count": 0,
                "forced_overlap_deduplicated_chars": 0,
                "ui_render_count": 0,
                "ui_skipped_render_count": 0,
            }


    def _get_loopback_devices(self, p_audio) -> List[Dict[str, Any]]:
            try:
                return list(p_audio.get_loopback_device_info_generator())
            except Exception as e:
                logger.warning(f"无法枚举 WASAPI loopback 设备：{e}")
                return []

    def _find_default_loopback(self, p_audio, loopbacks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            if not loopbacks:
                return None
            try:
                import pyaudiowpatch as pyaudio_wp
                wasapi_info = p_audio.get_host_api_info_by_type(pyaudio_wp.paWASAPI)
                default_speakers = p_audio.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
                default_name = default_speakers.get("name", "")
                if default_speakers.get("isLoopbackDevice", False):
                    return default_speakers
                for loopback in loopbacks:
                    loopback_name = loopback.get("name", "")
                    if default_name and (default_name in loopback_name or loopback_name in default_name):
                        return loopback
            except Exception as e:
                logger.warning(f"查找默认系统播放设备失败：{e}")

            for loopback in loopbacks:
                if loopback.get("isDefaultLoopbackDevice", False):
                    return loopback
            return loopbacks[0]

    def select_devices_dialog(self, p_audio) -> List[int]:
            """
            在主线程中用 Toplevel 弹出设备选择窗口。
            阻塞直到用户点击确定，返回选中的设备索引列表。
            必须在 self.root 创建之后、mainloop 之前调用。
            """
            devices = get_audio_devices(p_audio)
            capture_source = self.args.capture_source

            selected_devices: List[int] = []
            device_vars: Dict[int, tk.BooleanVar] = {}
            mic_device_ids = set()
            loopback_device_ids = set()

            # 用 Toplevel 而不是新建 Tk()，避免多个 Tk 实例冲突
            dialog = tk.Toplevel(self.root)
            dialog.title("选择音频来源（默认监听系统声音）")
            sw, sh = dialog.winfo_screenwidth(), dialog.winfo_screenheight()
            width = min(920, max(640, int(sw * 0.88)))
            height = min(560, max(420, int(sh * 0.72)))
            x = max(0, int((sw - width) / 2))
            y = max(0, int((sh - height) / 2))
            dialog.geometry(f"{width}x{height}+{x}+{y}")
            dialog.minsize(640, 420)
            dialog.transient(self.root)
            dialog.grab_set()  # 模态：锁定主窗口
            dialog.focus_set()

            selection_summary = tk.StringVar(value="")
            ok_button_ref: Dict[str, Optional[tk.Button]] = {"button": None}

            def selected_now() -> List[int]:
                return [idx for idx, var in device_vars.items() if var.get()]

            def refresh_selection_summary() -> None:
                chosen = selected_now()
                mic_count = sum(1 for idx in chosen if idx in mic_device_ids)
                system_count = sum(1 for idx in chosen if idx in loopback_device_ids)
                if chosen:
                    selection_summary.set(
                        f"已选择 {len(chosen)} 个设备：系统声音 {system_count} 个，麦克风 {mic_count} 个"
                    )
                else:
                    selection_summary.set("未选择音频来源")

                ok_button = ok_button_ref["button"]
                if ok_button is not None:
                    ok_button.config(state=tk.NORMAL if chosen else tk.DISABLED)

            def set_selected(groups: List[set]) -> None:
                allowed = set()
                for group in groups:
                    allowed.update(group)
                for idx, var in device_vars.items():
                    var.set(idx in allowed)
                refresh_selection_summary()

            def add_device_option(
                parent,
                group: set,
                idx: int,
                name: str,
                selected: bool,
                prefix: str = "",
                bold: bool = False,
            ) -> None:
                group.add(idx)
                if idx not in device_vars:
                    device_vars[idx] = tk.BooleanVar(value=selected)
                elif selected:
                    device_vars[idx].set(True)
                label = f"{prefix}{idx}: {name}"
                checkbutton_options = {
                    "text": label,
                    "variable": device_vars[idx],
                    "command": refresh_selection_summary,
                    "wraplength": max(260, int(width * 0.42)),
                    "justify": tk.LEFT,
                    "anchor": "w",
                }
                if bold:
                    checkbutton_options["font"] = ("Arial", 9, "bold")
                tk.Checkbutton(parent, **checkbutton_options).pack(anchor='w', fill="x", padx=10, pady=2)

            def on_cancel():
                selected_devices.clear()
                try:
                    dialog.grab_release()
                except Exception:
                    pass
                dialog.destroy()

            tk.Label(
                dialog,
                text="默认已选择系统播放声音（WASAPI Loopback）。关闭麦克风/录音设备不会影响系统声音识别。",
                font=("Microsoft YaHei", 10),
                fg="#0044AA",
                wraplength=width - 40,
                justify=tk.LEFT,
            ).pack(fill="x", padx=12, pady=(10, 0))

            def on_ok():
                chosen = selected_now()
                if not chosen:
                    messagebox.showwarning("请选择音频来源", "请至少选择一个系统声音或麦克风设备。", parent=dialog)
                    return
                selected_devices.extend(chosen)
                try:
                    dialog.grab_release()
                except Exception:
                    pass
                dialog.destroy()

            main_frame = tk.Frame(dialog)
            main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

            # ---- 左侧：麦克风输入设备 ----
            left_frame = tk.Frame(main_frame, relief=tk.RIDGE, borderwidth=2)
            left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
            tk.Label(left_frame, text="麦克风/输入设备（默认不选）", font=("Arial", 12, "bold")).pack(pady=5)

            left_canvas = tk.Canvas(left_frame)
            left_sb = tk.Scrollbar(left_frame, orient="vertical", command=left_canvas.yview)
            left_inner = tk.Frame(left_canvas)
            left_inner.bind("<Configure>", lambda e: left_canvas.configure(scrollregion=left_canvas.bbox("all")))
            left_window = left_canvas.create_window((0, 0), window=left_inner, anchor="nw")
            left_canvas.bind("<Configure>", lambda e: left_canvas.itemconfigure(left_window, width=e.width))
            left_canvas.configure(yscrollcommand=left_sb.set)

            # 默认输入设备。主场景是系统声音，所以麦克风默认不勾选。
            default_mic_added = False
            try:
                default_input = p_audio.get_default_input_device_info()
                didx = default_input['index']
                mic_selected = capture_source in ("mic", "both")
                add_device_option(
                    left_inner,
                    mic_device_ids,
                    didx,
                    default_input['name'],
                    mic_selected,
                    prefix="[默认麦克风] ",
                    bold=True,
                )
                tk.Frame(left_inner, height=2, bg="gray").pack(fill=tk.X, padx=10, pady=5)
                default_mic_added = True
            except Exception:
                didx = -1

            input_devices = [(i, d) for i, d in devices if d['maxInputChannels'] > 0]
            mic_fallback_selected = capture_source == "mic" and didx == -1
            for idx, dev in input_devices:
                if idx == didx:
                    continue
                add_device_option(left_inner, mic_device_ids, idx, dev['name'], mic_fallback_selected)
                mic_fallback_selected = False

            if not default_mic_added and not input_devices:
                tk.Label(
                    left_inner,
                    text="未找到麦克风输入设备。",
                    fg="#888888",
                    wraplength=max(260, int(width * 0.42)),
                    justify=tk.LEFT
                ).pack(anchor='w', padx=10, pady=8)

            left_canvas.pack(side="left", fill="both", expand=True)
            left_sb.pack(side="right", fill="y")

            # ---- 右侧：WASAPI Loopback 内录设备 ----
            right_frame = tk.Frame(main_frame, relief=tk.RIDGE, borderwidth=2)
            right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=5)
            tk.Label(right_frame, text="系统声音/WASAPI Loopback（默认选择）", font=("Arial", 12, "bold")).pack(pady=5)

            right_canvas = tk.Canvas(right_frame)
            right_sb = tk.Scrollbar(right_frame, orient="vertical", command=right_canvas.yview)
            right_inner = tk.Frame(right_canvas)
            right_inner.bind("<Configure>", lambda e: right_canvas.configure(scrollregion=right_canvas.bbox("all")))
            right_window = right_canvas.create_window((0, 0), window=right_inner, anchor="nw")
            right_canvas.bind("<Configure>", lambda e: right_canvas.itemconfigure(right_window, width=e.width))
            right_canvas.configure(yscrollcommand=right_sb.set)

            loopbacks = self._get_loopback_devices(p_audio)
            default_loopback = self._find_default_loopback(p_audio, loopbacks)
            default_loopback_idx = default_loopback["index"] if default_loopback else -1
            system_selected = capture_source in ("system", "both")

            if default_loopback is not None:
                add_device_option(
                    right_inner,
                    loopback_device_ids,
                    default_loopback_idx,
                    default_loopback['name'],
                    system_selected,
                    prefix="[默认系统声音] ",
                    bold=True,
                )
                tk.Frame(right_inner, height=2, bg="gray").pack(fill=tk.X, padx=10, pady=5)

            for loopback in loopbacks:
                idx = loopback['index']
                if idx == default_loopback_idx:
                    continue
                add_device_option(right_inner, loopback_device_ids, idx, loopback['name'], False)

            if not loopbacks:
                tk.Label(
                    right_inner,
                    text="未找到系统声音 loopback 设备。请安装 PyAudioWPatch，并确认 Windows 默认播放设备可用。",
                    fg="#CC0000",
                    wraplength=max(260, int(width * 0.42)),
                    justify=tk.LEFT
                ).pack(anchor='w', padx=10, pady=8)

            right_canvas.pack(side="left", fill="both", expand=True)
            right_sb.pack(side="right", fill="y")

            footer = tk.Frame(dialog)
            footer.pack(fill="x", padx=12, pady=(0, 10))

            tk.Label(
                footer,
                textvariable=selection_summary,
                anchor="w",
                fg="#555555"
            ).pack(fill="x", pady=(0, 6))

            quick_frame = tk.Frame(footer)
            quick_frame.pack(fill="x")

            tk.Button(
                quick_frame,
                text="仅系统声音",
                command=lambda: set_selected([loopback_device_ids]),
                state=tk.NORMAL if loopback_device_ids else tk.DISABLED
            ).pack(side=tk.LEFT, padx=(0, 6))
            tk.Button(
                quick_frame,
                text="仅麦克风",
                command=lambda: set_selected([mic_device_ids]),
                state=tk.NORMAL if mic_device_ids else tk.DISABLED
            ).pack(side=tk.LEFT, padx=(0, 6))
            tk.Button(
                quick_frame,
                text="系统+麦克风",
                command=lambda: set_selected([loopback_device_ids, mic_device_ids]),
                state=tk.NORMAL if loopback_device_ids and mic_device_ids else tk.DISABLED
            ).pack(side=tk.LEFT, padx=(0, 6))

            tk.Button(
                quick_frame,
                text="取消",
                command=on_cancel,
                width=10
            ).pack(side=tk.RIGHT, padx=(6, 0))
            ok_button = tk.Button(
                quick_frame,
                text="确定开始",
                command=on_ok,
                font=("Arial", 11, "bold"),
                width=12
            )
            ok_button.pack(side=tk.RIGHT)
            ok_button_ref["button"] = ok_button
            refresh_selection_summary()

            dialog.protocol("WM_DELETE_WINDOW", on_cancel)
            dialog.bind("<Return>", lambda _event: on_ok())
            dialog.bind("<Escape>", lambda _event: on_cancel())

            # 等待弹窗关闭（阻塞主线程，但 mainloop 还没开始，所以用 wait_window）
            root = self.root
            if root is None:
                return []
            root.wait_window(dialog)

            logger.info(f"用户选择的设备：{selected_devices}")
            return selected_devices

    def build_ui(self) -> None:
            self.root = tk.Tk()
            self.root.title("实时字幕（Qwen3-ASR-1.7B Native Streaming）")
            self.root.attributes("-topmost", bool(self._ui_preferences.get("topmost", True)))
            self.root.attributes("-alpha", float(self._ui_preferences.get("opacity", 0.95)))
            self.root.configure(bg="black")
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            initial_width = int(sw * 0.85)
            initial_wrap = max(360, initial_width - 60)
            self.root.geometry(f"{initial_width}x165+{int(sw*0.075)}+{sh-235}")
            self.root.resizable(True, True)
            header = tk.Frame(self.root, bg="black")
            header.pack(fill="x", padx=10, pady=(5, 0))
            self.status_label = tk.Label(
                header, text="● 初始化中...", font=("SimHei", 11),
                fg="#FFAA00", bg="black", anchor="w"
            )
            self.status_label.pack(side=tk.LEFT, fill="x", expand=True)
            self.settings_button = tk.Button(
                header, text="⚙ 设置", command=self.open_settings_dialog,
                font=("Microsoft YaHei", 9), fg="#FFFFFF", bg="#303030",
                activeforeground="#FFFFFF", activebackground="#505050",
                relief=tk.FLAT, padx=10, pady=2, cursor="hand2",
            )
            self.settings_button.pack(side=tk.RIGHT, padx=(8, 0))
            self.subtitle_label = tk.Label(
                self.root, text="等待语音...",
                font=("Microsoft YaHei", int(self._ui_preferences.get("source_font_size", 26))),
                fg="#00FF00", bg="black", wraplength=initial_wrap,
                justify="left", anchor="w",
            )
            self.subtitle_label.pack(expand=True, fill="both", padx=20, pady=(10, 12))
            self.root.bind("<Configure>", self._on_window_configure)
            self.root.protocol("WM_DELETE_WINDOW", self.on_close)


    def _runtime_settings_snapshot(self) -> Dict[str, Any]:
            with self._runtime_settings_lock:
                return dict(self._runtime_settings)

    def _request_runtime_settings(self, settings: Dict[str, Any]) -> None:
            with self._runtime_settings_lock:
                self._runtime_settings.update(settings)
            self._runtime_settings_changed.set()
            logger.info(
                "[RUNTIME_SETTINGS] requested threshold=%.2f silence=%.2fs speech=%.2fs "
                "preroll=%dms max_utterance=%.1fs overlap=%dms hybrid=%s hold=%dms min_endpoint=%dms drain=%d",
                settings["vad_threshold"],
                settings["min_silence_duration"],
                settings["min_speech_duration"],
                settings["speech_preroll_ms"],
                settings["max_utterance_seconds"],
                settings["forced_segment_overlap_ms"],
                settings["enable_hybrid_endpoint"],
                settings["endpoint_punctuation_hold_ms"],
                settings["endpoint_min_utterance_ms"],
                settings["max_drain_chunks"],
            )

    def _apply_display_preferences(self) -> None:
            root = self.root
            if root is None:
                return
            try:
                root.attributes("-alpha", float(self._ui_preferences["opacity"]))
                root.attributes("-topmost", bool(self._ui_preferences["topmost"]))
                if self.subtitle_label is not None:
                    self.subtitle_label.config(
                        font=("Microsoft YaHei", int(self._ui_preferences["source_font_size"]))
                    )
            except tk.TclError:
                logger.debug("显示设置应用失败", exc_info=True)


    def _persist_settings_snapshot(self) -> None:
            payload = {
                "version": 2,
                "audio": self._runtime_settings_snapshot(),
                "display": dict(self._ui_preferences),
            }
            _save_application_settings(payload)


    def open_settings_dialog(self) -> None:
            if self.root is None:
                return
            if self.settings_window is not None:
                try:
                    self.settings_window.lift()
                    self.settings_window.focus_force()
                    return
                except tk.TclError:
                    self.settings_window = None
            win = tk.Toplevel(self.root)
            self.settings_window = win
            win.title("实时字幕设置")
            win.geometry("520x560")
            win.transient(self.root)
            win.resizable(False, False)
            current = self._runtime_settings_snapshot()
            fields = [
                ("VAD 阈值", "vad_threshold", str(current["vad_threshold"])),
                ("静默结束秒数", "min_silence_duration", str(current["min_silence_duration"])),
                ("最短语音秒数", "min_speech_duration", str(current["min_speech_duration"])),
                ("句首预录 ms", "speech_preroll_ms", str(current["speech_preroll_ms"])),
                ("最长单句秒数", "max_utterance_seconds", str(current["max_utterance_seconds"])),
                ("强制分段重叠 ms", "forced_segment_overlap_ms", str(current["forced_segment_overlap_ms"])),
                ("标点保持 ms", "endpoint_punctuation_hold_ms", str(current["endpoint_punctuation_hold_ms"])),
                ("标点分段最短句 ms", "endpoint_min_utterance_ms", str(current["endpoint_min_utterance_ms"])),
                ("每轮最多读取块数", "max_drain_chunks", str(current["max_drain_chunks"])),
            ]
            vars_by_key: Dict[str, tk.StringVar] = {}
            body = tk.Frame(win)
            body.pack(fill="both", expand=True, padx=18, pady=14)
            for row, (label, key, value) in enumerate(fields):
                tk.Label(body, text=label, anchor="w").grid(row=row, column=0, sticky="w", pady=5)
                var = tk.StringVar(value=value)
                vars_by_key[key] = var
                tk.Entry(body, textvariable=var, width=18).grid(row=row, column=1, sticky="e", pady=5)
            hybrid_var = tk.BooleanVar(value=bool(current["enable_hybrid_endpoint"]))
            tk.Checkbutton(body, text="启用稳定标点分段", variable=hybrid_var).grid(
                row=len(fields), column=0, columnspan=2, sticky="w", pady=(8, 10)
            )
            font_var = tk.StringVar(value=str(self._ui_preferences["source_font_size"]))
            opacity_var = tk.StringVar(value=str(self._ui_preferences["opacity"]))
            topmost_var = tk.BooleanVar(value=bool(self._ui_preferences["topmost"]))
            row = len(fields) + 1
            tk.Label(body, text="字幕字号", anchor="w").grid(row=row, column=0, sticky="w", pady=5)
            tk.Entry(body, textvariable=font_var, width=18).grid(row=row, column=1, sticky="e", pady=5)
            row += 1
            tk.Label(body, text="窗口透明度 0.50~1.00", anchor="w").grid(row=row, column=0, sticky="w", pady=5)
            tk.Entry(body, textvariable=opacity_var, width=18).grid(row=row, column=1, sticky="e", pady=5)
            row += 1
            tk.Checkbutton(body, text="窗口置顶", variable=topmost_var).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=8
            )

            def apply() -> None:
                try:
                    requested = {
                        "vad_threshold": float(vars_by_key["vad_threshold"].get()),
                        "min_silence_duration": float(vars_by_key["min_silence_duration"].get()),
                        "min_speech_duration": float(vars_by_key["min_speech_duration"].get()),
                        "speech_preroll_ms": int(vars_by_key["speech_preroll_ms"].get()),
                        "max_utterance_seconds": float(vars_by_key["max_utterance_seconds"].get()),
                        "forced_segment_overlap_ms": int(vars_by_key["forced_segment_overlap_ms"].get()),
                        "enable_hybrid_endpoint": bool(hybrid_var.get()),
                        "endpoint_punctuation_hold_ms": int(vars_by_key["endpoint_punctuation_hold_ms"].get()),
                        "endpoint_min_utterance_ms": int(vars_by_key["endpoint_min_utterance_ms"].get()),
                        "max_drain_chunks": int(vars_by_key["max_drain_chunks"].get()),
                    }
                    candidate = argparse.Namespace(**vars(self.args))
                    for key, value in requested.items():
                        if key == "enable_hybrid_endpoint":
                            candidate.disable_hybrid_endpoint = not bool(value)
                        else:
                            setattr(candidate, key, value)
                    validate_args(candidate)
                    font_size = max(12, min(72, int(font_var.get())))
                    opacity = max(0.50, min(1.00, float(opacity_var.get())))
                except Exception as exc:
                    messagebox.showerror("设置无效", str(exc), parent=win)
                    return
                self._request_runtime_settings(requested)
                self._ui_preferences.update({
                    "source_font_size": font_size,
                    "opacity": opacity,
                    "topmost": bool(topmost_var.get()),
                })
                self._apply_display_preferences()
                try:
                    self._persist_settings_snapshot()
                except Exception:
                    messagebox.showerror("设置保存失败", "设置已应用，但保存失败，请查看日志。", parent=win)
                    return
                self.update_status("● 设置已保存", "#00AA00")

            buttons = tk.Frame(win)
            buttons.pack(fill="x", padx=18, pady=(0, 14))
            tk.Button(buttons, text="应用并保存", command=apply, width=14).pack(side=tk.RIGHT)
            tk.Button(buttons, text="关闭", command=win.destroy, width=10).pack(side=tk.RIGHT, padx=(0, 8))
            def closed() -> None:
                self.settings_window = None
                win.destroy()
            win.protocol("WM_DELETE_WINDOW", closed)


    def _on_window_configure(self, event) -> None:
            if self.root is None or event.widget is not self.root:
                return
            self._update_text_wrap(event.width)

    def _update_text_wrap(self, width: int) -> None:
            wrap = max(280, int(width) - 50)
            if abs(wrap - self._last_wrap_width) < 8:
                return
            self._last_wrap_width = wrap
            if self.subtitle_label is not None:
                self.subtitle_label.config(wraplength=wrap)


    def sync_ui(self) -> None:
            if self.root is None:
                return
            work_remaining = False
            try:
                event = self.ui_error_queue.get_nowait()
            except queue.Empty:
                event = None
            if event:
                _, title, message = event
                try:
                    messagebox.showerror(title, message, parent=self.root)
                except Exception:
                    logger.debug("显示错误对话框失败", exc_info=True)
                work_remaining = True
            newest_status = None
            for _ in range(20):
                try:
                    event = self.ui_event_queue.get_nowait()
                except queue.Empty:
                    break
                if event and event[0] == "status":
                    newest_status = event
            if newest_status is not None and self.status_label is not None:
                _, status, color = newest_status
                self._configure_label_if_changed(self.status_label, "status", status, color)
            with self.ui_buffer_lock:
                if self.ui_update_buffer["SRC"] is not None and self.subtitle_label is not None:
                    text = self.ui_update_buffer["SRC"]
                    color = "#00FF00" if self.ui_update_buffer["IS_FINAL"] else "#00CC00"
                    self._configure_label_if_changed(self.subtitle_label, "source", text, color)
                    self.ui_update_buffer["SRC"] = None
            try:
                self.root.after(16 if work_remaining else 50, self.sync_ui)
            except tk.TclError:
                pass


    def _configure_label_if_changed(
            self,
            label,
            channel: str,
            text: str,
            color: str,
            now: Optional[float] = None,
        ) -> bool:
            previous_text, previous_color = self._last_rendered[channel]
            if text == previous_text and color == previous_color:
                self._stat_add("ui_skipped_render_count", 1)
                return False

            rendered_at = time.monotonic() if now is None else now
            if channel == "source" and previous_text not in (None, "") and text != previous_text:
                self._stat_add("subtitle_revision_count", 1)
                if rendered_at - self._last_source_render_at < 0.3:
                    self._stat_add("subtitle_flicker_count", 1)
            if channel == "source":
                self._last_source_render_at = rendered_at

            label.config(text=text, fg=color)
            self._last_rendered[channel] = (text, color)
            self._stat_add("ui_render_count", 1)
            return True

    def _post_ui_event(self, event: tuple) -> None:
            if event and event[0] == "error_dialog":
                try:
                    self.ui_error_queue.put_nowait(event)
                except queue.Full:
                    # Keep the newest actionable error while bounding a repeated-error storm.
                    try:
                        self.ui_error_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.ui_error_queue.put_nowait(event)
                    except queue.Full:
                        logger.error("UI 错误队列持续满载，无法投递：%s", event[1])
                return
            try:
                self.ui_event_queue.put_nowait(event)
            except queue.Full:
                # This queue contains replaceable status events only; retain the newest state.
                try:
                    while True:
                        self.ui_event_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.ui_event_queue.put_nowait(event)
                except queue.Full:
                    pass

    def update_status(self, status: str, color: str = "#666666") -> None:
            """线程安全地更新状态栏。子线程只投递事件，主线程统一消费。"""
            self._post_ui_event(("status", status, color))

    def show_error_dialog(self, title: str, message: str) -> None:
            logger.error(f"{title}: {message}")
            try:
                if self.root is not None and current_thread() is main_thread():
                    messagebox.showerror(title, message, parent=self.root)
                else:
                    self._post_ui_event(("error_dialog", title, message))
            except Exception:
                logger.debug("投递或显示错误对话框失败", exc_info=True)

    def _reset_endpoint_hints(self) -> None:
            with self._endpoint_lock:
                self._endpoint_text = ""
                self._endpoint_last_change_at = 0.0
                self._endpoint_stable_punctuation = ""
                self._endpoint_stable_punctuation_at = 0.0

    def _update_endpoint_hints(
            self,
            source_state: Any,
            now: float,
            is_final: bool,
        ) -> None:
            with self._endpoint_lock:
                if is_final:
                    self._endpoint_text = ""
                    self._endpoint_last_change_at = 0.0
                    self._endpoint_stable_punctuation = ""
                    self._endpoint_stable_punctuation_at = 0.0
                    return
                if source_state.text != self._endpoint_text:
                    self._endpoint_text = source_state.text
                    self._endpoint_last_change_at = now
                stable = source_state.stable_text
                # Hybrid endpoint is driven by a *confirmed sentence boundary*, not by
                # the raw full hypothesis ending in punctuation. During continuous
                # speech the recognizer usually keeps appending the next sentence before
                # the hold timer expires, so requiring full_text[-1] to remain punctuation
                # made the hybrid endpoint effectively unreachable.
                stable_boundary = ""
                if stable:
                    last_boundary = 0
                    for index, char in enumerate(stable):
                        if char in "。！？!?":
                            last_boundary = index + 1
                    if last_boundary > 0:
                        stable_boundary = stable[:last_boundary]
                if stable_boundary:
                    if stable_boundary != self._endpoint_stable_punctuation:
                        self._endpoint_stable_punctuation = stable_boundary
                        self._endpoint_stable_punctuation_at = now
                else:
                    self._endpoint_stable_punctuation = ""
                    self._endpoint_stable_punctuation_at = 0.0

    def _should_hybrid_endpoint(self, utterance_sample_count: int) -> bool:
            if self.args.disable_hybrid_endpoint:
                return False
            utterance_ms = utterance_sample_count / max(1, self.sample_rate) * 1000.0
            if utterance_ms < self.args.endpoint_min_utterance_ms:
                return False
            now = time.monotonic()
            hold_seconds = self.args.endpoint_punctuation_hold_ms / 1000.0
            with self._endpoint_lock:
                if not self._endpoint_stable_punctuation:
                    return False
                # Continuous speech keeps changing the full ASR hypothesis every few
                # tens of milliseconds. Requiring the *entire* hypothesis to go idle
                # for the punctuation hold therefore defeated the purpose of a hybrid
                # endpoint. The sentence boundary itself has already passed source
                # LocalAgreement; only require that specific boundary to remain stable.
                return now - self._endpoint_stable_punctuation_at >= hold_seconds

    def _set_lifecycle_state(self, state: str) -> None:
            with self._lifecycle_lock:
                self._lifecycle_state = str(state)

    def _event_session_guard(self, event: Any) -> bool:
            if getattr(event, "session_id", "") != self.session_id:
                return False
            with self._lifecycle_lock:
                state = self._lifecycle_state
            if state == "running":
                return True
            if state in ("stopping_input", "draining_finals"):
                return bool(getattr(event, "is_final", False))
            return False

    def _accept_pipeline_callback(self, *, is_final: bool) -> bool:
            with self._lifecycle_lock:
                state = self._lifecycle_state
            if state == "running":
                return True
            return bool(is_final and state in ("stopping_input", "draining_finals"))

    def _queue_transcript_event(self, event: TranscriptEvent) -> None:
            if not self._event_session_guard(event):
                return
            try:
                if event.is_final:
                    self._transcript_event_queue.put(event, timeout=0.2)
                else:
                    self._transcript_event_queue.put_nowait(event)
            except queue.Full:
                if event.is_final:
                    # Raise so _EventSubscriberWorker records the final into RecoveryJournal.
                    raise RuntimeError(f"最终字幕事件队列已满，utterance={event.utterance_id}")
                self._stat_add("dropped_transcript_previews", 1)


    def _start_transcript_controller(self) -> None:
            if self._transcript_controller_thread is not None:
                return
            def run_controller() -> None:
                while True:
                    if self._transcript_controller_stop.is_set() and self._transcript_event_queue.empty():
                        return
                    try:
                        event = self._transcript_event_queue.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    try:
                        self._handle_transcript_event(event)
                    except Exception:
                        logger.error("TranscriptController 处理失败", exc_info=True)
                    finally:
                        self._transcript_event_queue.task_done()
            self._transcript_controller_thread = Thread(
                target=run_controller, name="TranscriptController", daemon=False
            )
            self._transcript_controller_thread.start()


    def _stop_transcript_controller(self, *, drain: bool, timeout: float) -> bool:
            if not drain:
                while True:
                    try:
                        self._transcript_event_queue.get_nowait()
                    except queue.Empty:
                        break
                    else:
                        self._transcript_event_queue.task_done()
            self._transcript_controller_stop.set()
            thread = self._transcript_controller_thread
            if thread is not None and current_thread() is not thread:
                thread.join(timeout=max(0.1, float(timeout)))
            return thread is None or not thread.is_alive()


    def _poll_cleanup_close(self) -> None:
            root = self.root
            if root is None:
                return
            if self._cleanup_complete.is_set():
                if not self._cleanup_succeeded:
                    logger.critical(
                        "[CLEANUP] session_id=%s completed_with_failures=%s",
                        self.session_id, self._cleanup_failures,
                    )
                try:
                    root.destroy()
                except tk.TclError:
                    pass
                return
            try:
                root.after(50, self._poll_cleanup_close)
            except tk.TclError:
                pass

    def _shutdown_requested(self) -> bool:
            return bool(self.killed or self._shutdown_event.is_set())

    def on_close(self) -> None:
            if self._close_requested:
                return
            self._close_requested = True
            self._set_lifecycle_state("stopping_input")
            self._shutdown_event.set()
            logger.info("用户关闭窗口，正在退出...")
            self.update_status("● 正在退出...", "#FFAA00")
            if self.stop_event is not None:
                try:
                    self.stop_event.set()
                except Exception:
                    logger.debug("窗口关闭时设置录音停止事件失败", exc_info=True)
            Thread(target=self.cleanup, name="SubtitleCleanup", daemon=True).start()
            self._poll_cleanup_close()

    def _stat_add(self, key: str, amount: int = 1) -> int:
            with self._stats_lock:
                self.stats[key] = int(self.stats.get(key, 0)) + int(amount)
                return int(self.stats[key])

    def _stat_max(self, key: str, value: int) -> int:
            with self._stats_lock:
                self.stats[key] = max(int(self.stats.get(key, 0)), int(value))
                return int(self.stats[key])

    def _stats_snapshot(self) -> Dict[str, int]:
            with self._stats_lock:
                return dict(self.stats)

    def on_asr_result(self, text: str, is_final: bool, utterance_id: int = 0) -> None:
            if not self._accept_pipeline_callback(is_final=bool(is_final)):
                return
            self._last_asr_result_at = time.monotonic()
            utterance_id = int(utterance_id)
            full_text = normalize_subtitle_text(text)
            if utterance_id > 0 and full_text:
                with self._segment_overlap_lock:
                    previous_id = self._forced_continuation_from.get(utterance_id, 0)
                    previous_text = self._final_source_by_utterance.get(previous_id, "")
                if previous_text:
                    full_text, removed = remove_repeated_segment_prefix(previous_text, full_text)
                    if removed:
                        with self._segment_overlap_lock:
                            prior = self._forced_overlap_dedup_max.get(utterance_id, 0)
                            if removed > prior:
                                self._stat_add("forced_overlap_deduplicated_chars", removed - prior)
                                self._forced_overlap_dedup_max[utterance_id] = removed
            with self._audio_cursor_lock:
                processed_cursor = self.audio_cursor.processed_sample
                self.audio_cursor.update(
                    decoded=processed_cursor,
                    confirmed=processed_cursor if is_final else -1,
                    retention_samples=int(self.args.sample_rate * 0.8),
                )
            try:
                self.subtitle_session.ingest_asr(
                    full_text, is_final=bool(is_final), utterance_id=utterance_id
                )
            except Exception:
                logger.error(
                    "[SESSION_ASR] session_id=%s utterance=%d publish failed",
                    self.session_id, utterance_id, exc_info=True,
                )


    def _handle_transcript_event(self, event: TranscriptEvent) -> None:
            if not self._event_session_guard(event):
                return
            full_text = event.text
            display_text = full_text[-260:]
            now = event.emitted_at
            self._update_endpoint_hints(event, now, event.is_final)
            if not full_text:
                if event.is_final:
                    with self.ui_buffer_lock:
                        self.ui_update_buffer["IS_FINAL"] = True
                        self.ui_update_buffer["SRC_DISPLAY_IS_FINAL"] = True
                        self.ui_update_buffer["SOURCE_REVISION"] = event.revision
                        self.ui_update_buffer["SOURCE_STATE"] = "final"
                    self._stat_add("subtitle_final_count", 1)
                    self._reset_endpoint_hints()
                return
            with self.ui_buffer_lock:
                same_utterance = self.ui_update_buffer["SRC_UTTERANCE_ID"] in (0, event.utterance_id)
                age = now - float(self.ui_update_buffer["SRC_DISPLAY_AT"])
                should_update = should_accept_source_update(
                    self.ui_update_buffer["SRC_FULL_TEXT"],
                    self.ui_update_buffer["SRC_DISPLAY_IS_FINAL"],
                    full_text,
                    event.is_final,
                    age,
                    same_utterance=same_utterance,
                )
                if should_update:
                    self.ui_update_buffer["SRC"] = display_text
                    self.ui_update_buffer["SRC_DISPLAY_TEXT"] = display_text
                    self.ui_update_buffer["SRC_FULL_TEXT"] = full_text
                    self.ui_update_buffer["SRC_DISPLAY_IS_FINAL"] = event.is_final
                    self.ui_update_buffer["SRC_DISPLAY_AT"] = now
                    self.ui_update_buffer["SRC_UTTERANCE_ID"] = event.utterance_id
                    self.ui_update_buffer["IS_FINAL"] = event.is_final
                self.ui_update_buffer["SOURCE_REVISION"] = event.revision
                self.ui_update_buffer["SOURCE_STATE"] = event.state
                self.ui_update_buffer["STABLE_SOURCE_TEXT"] = event.committed_text
                self.ui_update_buffer["UNSTABLE_SOURCE_TEXT"] = event.revisable_text
            self._stat_add(f"subtitle_{event.state}_count", 1)
            logger.info(
                "[SUBTITLE_EVENT] session_id=%s utterance=%d revision=%d state=%s "
                "committed_chars=%d revisable_chars=%d final=%s full_chars=%d display_chars=%d",
                self.session_id, event.utterance_id, event.revision, event.state,
                len(event.committed_text), len(event.revisable_text), event.is_final,
                len(full_text), len(display_text),
            )
            if event.is_final:
                count = self._stat_add("recognize_count", 1)
                if event.utterance_id > 0:
                    with self._segment_overlap_lock:
                        self._final_source_by_utterance[event.utterance_id] = full_text
                        while len(self._final_source_by_utterance) > 64:
                            old_id, _ = self._final_source_by_utterance.popitem(last=False)
                            self._forced_continuation_from.pop(old_id, None)
                            self._forced_overlap_dedup_max.pop(old_id, None)
                self.update_status(f"● 识别完成 #{count}", "#00FF00")


    def cleanup(self) -> None:
            with self._cleanup_lock:
                if self._cleanup_started:
                    already_running = True
                else:
                    self._cleanup_started = True
                    already_running = False
            if already_running:
                if current_thread() is not self.worker_thread:
                    self._cleanup_complete.wait(timeout=20.0)
                return
            self._set_lifecycle_state("stopping_input")
            self._shutdown_event.set()
            failures: List[str] = []
            try:
                if self.stop_event is not None:
                    try:
                        self.stop_event.set()
                    except Exception:
                        logger.debug("录音停止事件设置失败", exc_info=True)
                process = self.recording_process
                if process is not None:
                    try:
                        if process.is_alive():
                            process.join(timeout=2.0)
                        if process.is_alive():
                            process.terminate(); process.join(timeout=1.0)
                        if process.is_alive() and hasattr(process, "kill"):
                            process.kill(); process.join(timeout=1.0)
                        if process.is_alive():
                            failures.append("recording_process")
                        else:
                            try: process.close()
                            except Exception: pass
                            self.recording_process = None
                    except Exception:
                        failures.append("recording_process")
                        logger.error("录音进程停止失败", exc_info=True)
                if self.async_asr is not None:
                    try:
                        self.async_asr.shutdown()
                    except Exception:
                        failures.append("asr_service")
                        logger.error("ASR 服务清理失败", exc_info=True)
                worker = self.worker_thread
                if worker is not None and worker.is_alive() and current_thread() is not worker:
                    if not self._worker_stopped_event.wait(timeout=12.0):
                        failures.append("worker_thread")
                    if worker.is_alive():
                        worker.join(timeout=1.0)
                elif current_thread() is worker:
                    self._worker_stopped_event.set()
                self._set_lifecycle_state("draining_finals")
                try:
                    if not self.event_bus.close(drain=True, timeout=5.0):
                        failures.append("session_event_bus")
                except Exception:
                    failures.append("session_event_bus")
                    logger.error("SessionEventBus 清理失败", exc_info=True)
                if not self._stop_transcript_controller(drain=True, timeout=5.0):
                    failures.append("transcript_controller")
                self._set_lifecycle_state("stopping_delivery")
                try:
                    if not self.subtitle_session.close(timeout=5.0):
                        failures.append("subtitle_session")
                except Exception:
                    failures.append("subtitle_session")
                    logger.error("字幕 Session 清理失败", exc_info=True)
                stats = self._stats_snapshot()
                logger.info(
                    "[SESSION_SUMMARY] session_id=%s finals=%d asr_tasks=%d max_asr_queue_ms=%d "
                    "max_asr_inference_ms=%d hybrid_endpoints=%d forced_endpoints=%d "
                    "forced_dedup_chars=%d audio_drops=%d discontinuities=%d max_audio_age_ms=%d asr_restarts=%d",
                    self.session_id, stats["subtitle_final_count"], stats["asr_tasks"],
                    stats["max_asr_queue_latency_ms"], stats["max_asr_inference_latency_ms"],
                    stats["hybrid_endpoint_count"], stats["forced_endpoint_count"],
                    stats["forced_overlap_deduplicated_chars"], stats["dropped_audio_chunks"],
                    stats["audio_discontinuity_count"], stats["max_audio_age_ms"], stats["asr_restart_count"],
                )
                self._cleanup_failures = failures
                self._cleanup_succeeded = not failures
                logger.info(
                    "[SESSION] session_id=%s cleanup_complete success=%s failures=%s",
                    self.session_id, self._cleanup_succeeded, failures,
                )
            finally:
                self.killed = True
                self._set_lifecycle_state("closed")
                self._cleanup_failures = failures
                self._cleanup_succeeded = not failures
                self._cleanup_complete.set()


    def _preload_runtime_dependencies(self) -> None:
            started = time.monotonic()
            ok = ensure_runtime_dependencies()
            logger.info(
                "[RUNTIME_PRELOAD_STAGE] stage=runtime_core ms=%.0f success=%s",
                (time.monotonic() - started) * 1000, ok,
            )
            logger.info(
                "[RUNTIME_PRELOAD_STAGE] stage=qwen_vllm_sidecar_client ms=0 success=True url=%s",
                self.args.qwen_server_url,
            )


    def _worker_thread(self) -> None:
        """Load runtime components, run audio/VAD, and feed every speech frame into stateful streaming ASR."""
        debug_vad_wav_file = None
        samples_queue = None
        try:
            if self.dependency_loader_thread is not None:
                while self.dependency_loader_thread.is_alive():
                    if self._shutdown_event.wait(0.1):
                        return
                    self.dependency_loader_thread.join(timeout=0.1)
            if self._shutdown_requested():
                return
            if not ensure_runtime_dependencies():
                self.update_status('● 错误：缺少 numpy/运行时依赖，见 subtitle.log', '#FF0000')
                return
            if not ensure_audio_dependencies():
                logger.error('音频依赖不可用：%s', _audio_import_error)
                self.update_status('● 错误：缺少音频依赖，见 subtitle.log', '#FF0000')
                return
            self.sample_rate = int(self.args.sample_rate)
            selected_dev = self.selected_devices
            if not selected_dev:
                self.update_status('● 错误：未选择任何设备', '#FF0000')
                return
            device_str = f'Qwen official streaming demo {self.args.qwen_server_url}'
            model_label = 'Qwen3-ASR-1.7B'
            asr_generation = 0
            self.update_status(f'● 连接 {model_label} 原生流式服务...', '#FFAA00')
            logger.info('加载 ASR backend: model=%s backend=qwen3_asr_official_demo_streaming server=%s language=%s', self.args.asr_model, self.args.qwen_server_url, self.args.asr_language)
            printer = MyPrinter()

            def on_asr_with_print(text: str, is_final: bool, utterance_id: int) -> None:
                self.on_asr_result(text, is_final, utterance_id)
                if is_final:
                    printer.do_print(text)
                    printer.on_endpoint()

            def on_asr_fatal_error(message: str) -> None:
                logger.error('ASR 致命错误：%s', message)
                self._asr_failure_message = message
                self._asr_failure_event.set()
                self.update_status('● 流式 ASR 异常，正在准备恢复...', '#FFAA00')

            def on_asr_metrics(metrics: Dict[str, Any]) -> None:
                self._stat_add('asr_tasks', 1)
                self._stat_max('max_asr_queue_latency_ms', metrics['queue_latency_ms'])
                self._stat_max('max_asr_inference_latency_ms', metrics['inference_latency_ms'])
                logger.info('[ASR_STREAM_METRIC] session_id=%s sequence=%d state=%s audio_ms=%d queue_ms=%d inference_ms=%d has_text=%s feature_cursor=%d', self.session_id, metrics['sequence'], metrics['state'], metrics['audio_duration_ms'], metrics['queue_latency_ms'], metrics['inference_latency_ms'], metrics['has_text'], metrics.get('feature_cursor', 0))

            def on_asr_discontinuity(dropped_samples: int) -> None:
                dropped_samples = max(0, int(dropped_samples))
                self._stat_add('audio_discontinuity_count', 1)
                chunk_samples = max(1, round(self.sample_rate * AUDIO_FRAME_SECONDS))
                self._stat_add('dropped_audio_chunks', max(1, (dropped_samples + chunk_samples - 1) // chunk_samples))
                self.update_status('● ASR 已从音频拥塞中恢复', '#FFAA00')
            if self._shutdown_requested():
                return

            def create_asr_proxy() -> Qwen3ASRStreamingHTTPRecognizer:
                return Qwen3ASRStreamingHTTPRecognizer(self.args.asr_model, on_asr_with_print, server_url=self.args.qwen_server_url, language=self.args.asr_language, sample_rate=self.sample_rate, chunk_size_sec=self.args.qwen_chunk_size_sec, unfixed_chunk_num=self.args.qwen_unfixed_chunk_num, unfixed_token_num=self.args.qwen_unfixed_token_num, push_interval_ms=self.args.qwen_push_interval_ms, request_timeout=self.args.qwen_http_timeout, on_error=on_asr_fatal_error, on_metrics=on_asr_metrics, on_discontinuity=on_asr_discontinuity, preserve_audio=self.args.audio_backpressure_mode == 'buffered', utterance_id_offset=asr_generation * 1000000)
            try:
                if self._shutdown_requested():
                    return
                self._asr_failure_event.clear()
                self._asr_failure_message = ''
                self.async_asr = create_asr_proxy()
                self.update_status(f'● 连接 {model_label} vLLM 原生流式 backend...', '#FFAA00')
                warmup_seconds = self.async_asr.warmup()
                if self._shutdown_requested():
                    return
                self._asr_failure_event.clear()
                logger.info('%s 原生流式 backend 就绪：%.0fms', model_label, warmup_seconds * 1000)
            except Exception as e:
                if self._shutdown_requested():
                    return
                logger.error('流式 ASR 初始化失败：%s', e, exc_info=True)
                self.update_status('● 错误：Qwen vLLM 流式服务不可用，见 subtitle.log', '#FF0000')
                return
            vad_model_path = os.path.abspath(self.args.vad_model_path)
            if not os.path.exists(vad_model_path):
                self.update_status('● 错误：ten-vad.onnx 不存在', '#FF0000')
                return

            def create_vad(runtime_settings: Dict[str, Any]):
                config = sherpa_onnx.VadModelConfig()
                config.ten_vad.model = vad_model_path
                config.ten_vad.min_silence_duration = float(runtime_settings['min_silence_duration'])
                config.ten_vad.min_speech_duration = float(runtime_settings['min_speech_duration'])
                config.ten_vad.threshold = float(runtime_settings['vad_threshold'])
                config.ten_vad.max_speech_duration = float(runtime_settings['max_utterance_seconds']) + 1.0
                config.sample_rate = self.sample_rate
                config.num_threads = 1
                config.provider = 'cpu'
                config.debug = False
                vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=max(self.args.vad_buffer_size, int(float(runtime_settings['max_utterance_seconds']) + 2.0)))
                if not supports_vad_reset(vad):
                    raise RuntimeError('VAD 版本不支持安全重置')
                return (vad, int(config.ten_vad.window_size))

            def apply_runtime_args(runtime_settings: Dict[str, Any]) -> None:
                for key in ('vad_threshold', 'min_silence_duration', 'min_speech_duration', 'speech_preroll_ms', 'max_utterance_seconds', 'forced_segment_overlap_ms', 'endpoint_punctuation_hold_ms', 'endpoint_min_utterance_ms', 'max_drain_chunks'):
                    setattr(self.args, key, runtime_settings[key])
                self.args.disable_hybrid_endpoint = not bool(runtime_settings['enable_hybrid_endpoint'])
            self._runtime_settings_changed.clear()
            runtime_settings = self._runtime_settings_snapshot()
            apply_runtime_args(runtime_settings)
            try:
                self.vad, window_size = create_vad(runtime_settings)
            except Exception as exc:
                self.update_status(f'● 错误：VAD 初始化失败：{exc}', '#FF0000')
                return
            samples_queue = multiprocessing.Queue(maxsize=self.args.audio_queue_size)
            if self.args.debug_save_audio:
                debug_audio_path = os.path.abspath(self.args.debug_save_audio)
                os.makedirs(os.path.dirname(debug_audio_path), exist_ok=True)
                debug_vad_wav_file = wave.open(debug_audio_path, 'wb')
                debug_vad_wav_file.setnchannels(1)
                debug_vad_wav_file.setsampwidth(2)
                debug_vad_wav_file.setframerate(self.sample_rate)
                logger.info('[Audio] 保存实际进入 VAD/ASR 的音频：%s', debug_audio_path)
            if self._shutdown_requested():
                return
            self.stop_event = multiprocessing.Event()
            self.recording_process = multiprocessing.Process(target=recording_process_entrypoint, args=(selected_dev, samples_queue, self.stop_event, self.args.mix_mode, '', self.sample_rate, self.args.audio_backpressure_mode), name='SubtitleAudioCaptureProcess')
            self.recording_process.start()
            if self._shutdown_requested():
                self.stop_event.set()
                return
            self.update_status('● 正在流式识别...', '#00FF00')
            pending_chunks: List = []
            vad_remainder = np.array([], dtype=np.float32)
            accuracy_leveler = ConservativeSpeechLeveler()
            last_audio_sequence = 0
            last_reported_drop_count = 0
            last_backlog_warning_at = 0.0
            started = False
            utterance_sample_count = 0
            preroll_samples = int(self.sample_rate * self.args.speech_preroll_ms / 1000.0)
            pre_speech_buf = RollingAudioBuffer(preroll_samples) if preroll_samples > 0 else None
            forced_overlap_samples = int(self.sample_rate * self.args.forced_segment_overlap_ms / 1000.0)
            forced_overlap_buf = RollingAudioBuffer(forced_overlap_samples) if forced_overlap_samples > 0 else None
            active_utterance_id = 0
            asr_restart_attempts = 0
            last_asr_restart_at = 0.0
            pending_runtime_settings: Optional[Dict[str, Any]] = None
            logger.info('Ready: Qwen3-ASR native streaming backend=vLLM chunk=%.2fs language=%s server=%s', self.args.qwen_chunk_size_sec, self.args.asr_language, self.args.qwen_server_url)
            while not self._shutdown_requested():
                if self._runtime_settings_changed.is_set():
                    self._runtime_settings_changed.clear()
                    pending_runtime_settings = self._runtime_settings_snapshot()
                if pending_runtime_settings is not None and (not started):
                    try:
                        apply_runtime_args(pending_runtime_settings)
                        new_vad, new_window_size = create_vad(pending_runtime_settings)
                    except Exception as exc:
                        logger.error('实时 VAD 参数应用失败：%s', exc, exc_info=True)
                        self.update_status('● VAD 设置应用失败，继续使用原设置', '#FF0000')
                        pending_runtime_settings = None
                    else:
                        self.vad = new_vad
                        window_size = new_window_size
                        runtime_settings = pending_runtime_settings
                        pending_runtime_settings = None
                        prior_preroll = pre_speech_buf.to_array() if pre_speech_buf is not None and pre_speech_buf else np.array([], dtype=np.float32)
                        preroll_samples = int(self.sample_rate * self.args.speech_preroll_ms / 1000.0)
                        pre_speech_buf = RollingAudioBuffer(preroll_samples) if preroll_samples > 0 else None
                        if pre_speech_buf is not None and prior_preroll.size:
                            pre_speech_buf.append(prior_preroll[-preroll_samples:])
                        forced_overlap_samples = int(self.sample_rate * self.args.forced_segment_overlap_ms / 1000.0)
                        forced_overlap_buf = RollingAudioBuffer(forced_overlap_samples) if forced_overlap_samples > 0 else None
                        self._reset_endpoint_hints()
                        logger.info('[RUNTIME_SETTINGS] applied threshold=%.2f silence=%.2fs speech=%.2fs preroll=%dms max_utterance=%.1fs overlap=%dms hybrid=%s hold=%dms min_endpoint=%dms drain=%d window=%d', self.args.vad_threshold, self.args.min_silence_duration, self.args.min_speech_duration, self.args.speech_preroll_ms, self.args.max_utterance_seconds, self.args.forced_segment_overlap_ms, not self.args.disable_hybrid_endpoint, self.args.endpoint_punctuation_hold_ms, self.args.endpoint_min_utterance_ms, self.args.max_drain_chunks, window_size)
                        self.update_status('● 设置已生效，等待语音', '#00FF00')
                if self.recording_process and (not self.recording_process.is_alive()):
                    logger.warning('录音进程意外退出，exitcode=%s', self.recording_process.exitcode)
                    self.update_status('● 错误：录音进程退出', '#FF0000')
                    break
                asr_failed = self._asr_failure_event.is_set() or (self.async_asr is not None and (not self.async_asr.is_alive()))
                if asr_failed:
                    failure = self._asr_failure_message or 'Qwen ASR backend 意外退出'
                    if time.monotonic() - last_asr_restart_at >= 60.0:
                        asr_restart_attempts = 0
                    if asr_restart_attempts >= 3:
                        logger.error('[ASR_PROCESS] session_id=%s restart_exhausted reason=%s', self.session_id, failure)
                        self.update_status('● 错误：ASR 自动恢复失败', '#FF0000')
                        break
                    asr_restart_attempts += 1
                    last_asr_restart_at = time.monotonic()
                    self._stat_add('asr_restart_count', 1)
                    logger.warning('[ASR_PROCESS] session_id=%s restart_attempt=%d reason=%s', self.session_id, asr_restart_attempts, failure)
                    self.update_status('● 正在重连 Qwen ASR backend...', '#FFAA00')
                    if self.async_asr is not None:
                        self.async_asr.shutdown()
                    self._asr_failure_event.clear()
                    self._asr_failure_message = ''
                    self.vad.reset()
                    with self._live_asr_lock:
                        self._live_asr_utterance_id = 0
                    started = False
                    utterance_sample_count = 0
                    active_utterance_id = 0
                    pending_chunks.clear()
                    vad_remainder = np.array([], dtype=np.float32)
                    if pre_speech_buf is not None:
                        pre_speech_buf.clear()
                    if forced_overlap_buf is not None:
                        forced_overlap_buf.clear()
                    try:
                        while True:
                            samples_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        if self._shutdown_requested():
                            break
                        asr_generation += 1
                        self.async_asr = create_asr_proxy()
                        warmup_seconds = self.async_asr.warmup()
                        if self._shutdown_requested():
                            break
                        self._asr_failure_event.clear()
                        logger.info('[ASR_PROCESS] session_id=%s restart_success warmup_ms=%.0f', self.session_id, warmup_seconds * 1000)
                        self.update_status('● ASR 已恢复，等待下一句', '#00FF00')
                        continue
                    except Exception as exc:
                        logger.error('ASR 自动恢复失败：%s', exc, exc_info=True)
                        self.update_status('● 错误：ASR 自动恢复失败', '#FF0000')
                        break
                try:
                    packet = samples_queue.get(timeout=0.02)
                    pending_chunks.append(packet)
                    drained = 1
                    while drained < self.args.max_drain_chunks:
                        try:
                            pending_chunks.append(samples_queue.get_nowait())
                            drained += 1
                        except queue.Empty:
                            break
                except queue.Empty:
                    pass
                except Exception as e:
                    logger.debug('读取录音队列失败：%s', e)
                    continue
                if not pending_chunks:
                    continue
                now = time.monotonic()
                metadata_audio = [item for item in pending_chunks if isinstance(item, dict) and item.get('type', 'audio') == 'audio']
                if metadata_audio:
                    oldest_age = max(0.0, now - min((float(item.get('captured_at', now)) for item in metadata_audio)))
                    if self.args.audio_backpressure_mode == 'live' and oldest_age * 1000 >= self.args.audio_latency_budget_ms:
                        try:
                            while True:
                                pending_chunks.append(samples_queue.get_nowait())
                        except queue.Empty:
                            pass
                        pending_chunks, stale_packets, max_age = trim_audio_packets_to_latency_budget(pending_chunks, now, self.args.audio_latency_budget_ms / 1000.0, self.args.audio_recovery_preroll_ms / 1000.0)
                        self._stat_add('audio_discontinuity_count', 1)
                        self._stat_add('dropped_audio_chunks', stale_packets)
                        self._stat_max('max_audio_age_ms', round(max_age * 1000))
                        logger.warning('[Audio] session_id=%s discontinuity stale_packets=%d max_age_ms=%.0f action=jump_to_live_edge', self.session_id, stale_packets, max_age * 1000)
                        if started and self.async_asr is not None:
                            try:
                                self.async_asr.finalize_utterance(reason='audio_discontinuity')
                            except Exception:
                                logger.debug('音频断流时结束 ASR utterance 失败', exc_info=True)
                        self.vad.reset()
                        with self._live_asr_lock:
                            self._live_asr_utterance_id = 0
                        started = False
                        utterance_sample_count = 0
                        active_utterance_id = 0
                        vad_remainder = np.array([], dtype=np.float32)
                        if pre_speech_buf is not None:
                            pre_speech_buf.clear()
                        if forced_overlap_buf is not None:
                            forced_overlap_buf.clear()
                        self._reset_endpoint_hints()
                        self.update_status('● 音频积压，已跳到实时位置', '#FFAA00')
                    elif self.args.audio_backpressure_mode == 'buffered' and oldest_age * 1000 >= self.args.audio_latency_budget_ms:
                        self._stat_max('max_audio_age_ms', round(oldest_age * 1000))
                        if now - last_backlog_warning_at >= 2.0:
                            last_backlog_warning_at = now
                            logger.warning('[Audio] session_id=%s buffered_backlog age_ms=%.0f action=preserve_audio_allow_latency', self.session_id, oldest_age * 1000)
                            self.update_status(f'● 音频缓冲积压 {oldest_age:.1f}s，正在完整处理', '#FFAA00')
                audio_arrays = []
                for packet in pending_chunks:
                    if not isinstance(packet, dict):
                        audio_arrays.append(packet)
                        continue
                    if packet.get('type') == 'audio_error':
                        logger.error('[Audio] session_id=%s device=%s capture_error=%s', self.session_id, packet.get('device', -1), packet.get('message', '未知录音错误'))
                        self.update_status('● 录音设备异常，识别已停止', '#FF0000')
                        self._set_lifecycle_state('stopping_input')
                        self._shutdown_event.set()
                        break
                    if packet.get('type', 'audio') != 'audio':
                        continue
                    sequence = int(packet['sequence'])
                    captured_at = float(packet['captured_at'])
                    dropped_before = int(packet.get('dropped_before', 0))
                    packet_samples = packet['samples']
                    audio_arrays.append(packet_samples)
                    with self._audio_cursor_lock:
                        self.audio_cursor.update(received=self.audio_cursor.received_sample + len(packet_samples))
                        self._last_audio_packet_at = time.monotonic()
                    self._stat_add('audio_chunks', 1)
                    if last_audio_sequence and sequence != last_audio_sequence + 1:
                        missing = max(0, sequence - last_audio_sequence - 1)
                        logger.warning('[Audio] session_id=%s sequence_gap last=%d current=%d missing=%d', self.session_id, last_audio_sequence, sequence, missing)
                    last_audio_sequence = max(last_audio_sequence, sequence)
                    if dropped_before > last_reported_drop_count:
                        dropped = dropped_before - last_reported_drop_count
                        self._stat_add('dropped_audio_chunks', dropped)
                        last_reported_drop_count = dropped_before
                        logger.warning('[Audio] session_id=%s output_drops=%d total=%d', self.session_id, dropped, dropped_before)
                    audio_age = max(0.0, now - captured_at)
                    self._stat_max('max_audio_age_ms', round(audio_age * 1000))
                    if audio_age > 0.5 and now - last_backlog_warning_at >= 5.0:
                        logger.warning('[Audio] session_id=%s backlog sequence=%d age_ms=%.0f', self.session_id, sequence, audio_age * 1000)
                        last_backlog_warning_at = now
                pending_chunks.clear()
                if self._shutdown_requested():
                    break
                if not audio_arrays:
                    continue
                try:
                    queue_depth = samples_queue.qsize()
                except (NotImplementedError, OSError):
                    queue_depth = 0
                self._stat_max('max_audio_queue_depth', queue_depth)
                new_audio = np.concatenate(audio_arrays)
                # Compatibility guard: v0.9.18 accidentally omitted the argparse
                # declaration for disable_audio_normalize. Default to normalization ON
                # when a Namespace from an older launcher/config lacks this attribute.
                if not bool(getattr(self.args, "disable_audio_normalize", False)):
                    new_audio = accuracy_leveler.process(new_audio)
                with self._audio_cursor_lock:
                    self.audio_cursor.update(processed=self.audio_cursor.processed_sample + len(new_audio))
                if debug_vad_wav_file is not None:
                    pcm = np.int16(np.clip(new_audio, -1.0, 1.0) * 32767)
                    debug_vad_wav_file.writeframes(pcm.tobytes())
                vad_scan = np.concatenate([vad_remainder, new_audio]) if vad_remainder.size else new_audio
                scan_offset = 0
                while scan_offset + window_size <= len(vad_scan):
                    window = vad_scan[scan_offset:scan_offset + window_size]
                    self.vad.accept_waveform(window)
                    scan_offset += window_size
                    if not started and pre_speech_buf is not None:
                        pre_speech_buf.append(window)
                    if not started and self.vad.is_speech_detected():
                        started = True
                        self._reset_endpoint_hints()
                        if pre_speech_buf is not None and pre_speech_buf:
                            initial_audio = pre_speech_buf.to_array()
                        else:
                            initial_audio = window.copy()
                        utterance_sample_count = len(initial_audio)
                        active_utterance_id = self.async_asr.begin_utterance(initial_audio, self.sample_rate)
                        with self._live_asr_lock:
                            self._live_asr_utterance_id = active_utterance_id
                        if forced_overlap_buf is not None:
                            forced_overlap_buf.clear()
                            forced_overlap_buf.append(initial_audio)
                        self.update_status('● 检测到语音／流式解码中', '#00AAFF')
                    elif started:
                        self.async_asr.accept_audio(window, self.sample_rate)
                        utterance_sample_count += len(window)
                        if forced_overlap_buf is not None:
                            forced_overlap_buf.append(window)
                    if started and (not self.vad.empty()):
                        self.vad.pop()
                        self.async_asr.finalize_utterance(reason='silence')
                        with self._live_asr_lock:
                            self._live_asr_utterance_id = 0
                        logger.info('[VAD] session_id=%s endpoint audio_ms=%d reason=silence', self.session_id, round(utterance_sample_count / self.sample_rate * 1000))
                        if pre_speech_buf is not None:
                            pre_speech_buf.clear()
                        started = False
                        utterance_sample_count = 0
                        active_utterance_id = 0
                        if forced_overlap_buf is not None:
                            forced_overlap_buf.clear()
                        self._reset_endpoint_hints()
                        continue
                    if started and self._should_hybrid_endpoint(utterance_sample_count):
                        self.async_asr.finalize_utterance(reason='stable_punctuation')
                        with self._live_asr_lock:
                            self._live_asr_utterance_id = 0
                        logger.info('[VAD] session_id=%s endpoint audio_ms=%d reason=stable_punctuation', self.session_id, round(utterance_sample_count / self.sample_rate * 1000))
                        self._stat_add('hybrid_endpoint_count', 1)
                        self.vad.reset()
                        if pre_speech_buf is not None:
                            pre_speech_buf.clear()
                        started = False
                        utterance_sample_count = 0
                        active_utterance_id = 0
                        if forced_overlap_buf is not None:
                            forced_overlap_buf.clear()
                        self._reset_endpoint_hints()
                        continue
                    if started and should_force_utterance_segment(utterance_sample_count, self.sample_rate, self.args.max_utterance_seconds, False):
                        previous_utterance_id = active_utterance_id
                        overlap_audio = forced_overlap_buf.to_array() if forced_overlap_buf is not None and forced_overlap_buf else np.array([], dtype=np.float32)
                        self.async_asr.finalize_utterance(reason='max_duration')
                        logger.info('[VAD] session_id=%s endpoint audio_ms=%d reason=max_duration', self.session_id, round(utterance_sample_count / self.sample_rate * 1000))
                        self._stat_add('forced_endpoint_count', 1)
                        if pre_speech_buf is not None:
                            pre_speech_buf.clear()
                        if overlap_audio.size:
                            active_utterance_id = self.async_asr.begin_utterance(overlap_audio, self.sample_rate)
                            with self._live_asr_lock:
                                self._live_asr_utterance_id = active_utterance_id
                            with self._segment_overlap_lock:
                                self._forced_continuation_from[active_utterance_id] = previous_utterance_id
                            started = True
                            utterance_sample_count = len(overlap_audio)
                            if forced_overlap_buf is not None:
                                forced_overlap_buf.clear()
                                forced_overlap_buf.append(overlap_audio)
                        else:
                            started = False
                            utterance_sample_count = 0
                            active_utterance_id = 0
                            with self._live_asr_lock:
                                self._live_asr_utterance_id = 0
                        self._reset_endpoint_hints()
                vad_remainder = vad_scan[scan_offset:].copy()
                if not started and vad_remainder.size > self.sample_rate * 2:
                    vad_remainder = vad_remainder[-window_size:]
        except Exception as e:
            logger.error('工作线程异常：%s', e, exc_info=True)
            self.update_status('● 发生错误，见 subtitle.log', '#FF0000')
        finally:
            if debug_vad_wav_file is not None:
                try:
                    debug_vad_wav_file.close()
                except Exception:
                    logger.debug('调试 WAV 关闭失败', exc_info=True)
            if samples_queue is not None:
                try:
                    samples_queue.cancel_join_thread()
                except Exception:
                    logger.debug('音频队列 cancel_join_thread 失败', exc_info=True)
                try:
                    samples_queue.close()
                except Exception:
                    logger.debug('音频队列 close 失败', exc_info=True)
            self._worker_stopped_event.set()
            self.cleanup()

    def _probe_selected_devices_with_dialog(self) -> tuple[List[int], Dict[int, str]]:
            """Probe PortAudio devices off the Tk thread while a modal progress UI stays responsive."""
            root = self.root
            if root is None:
                raise RuntimeError("Tk 主窗口未初始化")
            selected = list(self.selected_devices)
            result_queue: queue.Queue = queue.Queue(maxsize=1)

            dialog = tk.Toplevel(root)
            dialog.title("正在检查音频设备")
            dialog.geometry("460x140")
            dialog.resizable(False, False)
            dialog.transient(root)
            dialog.grab_set()
            tk.Label(
                dialog,
                text="正在隔离进程中打开所选设备，请稍候…",
                anchor="w",
            ).pack(fill="x", padx=20, pady=(24, 12))
            progress = ttk.Progressbar(dialog, mode="indeterminate")
            progress.pack(fill="x", padx=20, pady=8)
            progress.start(12)

            def probe() -> None:
                try:
                    result_queue.put((True, probe_audio_devices_isolated(selected)), timeout=0.5)
                except BaseException as exc:
                    result_queue.put((False, exc), timeout=0.5)

            Thread(target=probe, name="AudioDevicePreflight", daemon=True).start()
            outcome: Dict[str, Any] = {"done": False, "value": ([], {})}

            def poll() -> None:
                try:
                    ok, value = result_queue.get_nowait()
                except queue.Empty:
                    try:
                        dialog.after(50, poll)
                    except tk.TclError:
                        pass
                    return
                outcome["done"] = True
                if ok:
                    outcome["value"] = value
                else:
                    outcome["value"] = value
                try:
                    progress.stop()
                    dialog.destroy()
                except tk.TclError:
                    pass

            dialog.after(50, poll)
            root.wait_window(dialog)
            if not outcome["done"]:
                raise RuntimeError("音频设备预检窗口被意外关闭")
            value = outcome["value"]
            if isinstance(value, BaseException):
                raise value
            valid, failures = value
            return list(valid), dict(failures)

    def run(self) -> None:
            """Build the UI, validate resources/devices, run workers, and always release runtime resources."""
            self.build_ui()
            root = self.root
            if root is None:
                raise RuntimeError("Tk 主窗口初始化失败")
            try:
                if not ensure_first_run_assets(root, self.args):
                    return

                self.dependency_loader_thread = Thread(
                    target=self._preload_runtime_dependencies,
                    name="RuntimeDependencyPreload",
                    daemon=True,
                )
                self.dependency_loader_thread.start()

                if self.args.device >= 0:
                    self.selected_devices = [self.args.device]
                else:
                    if not ensure_audio_dependencies():
                        logger.error("音频依赖缺失：%s", _audio_import_error)
                        self.show_error_dialog(
                            "音频依赖缺失",
                            "缺少 PyAudioWPatch、sherpa-onnx、SciPy 或 NumPy。\n\n"
                            "请先按照 requirements.txt 安装依赖。",
                        )
                        return
                    if self.args.capture_source in ("system", "both") and not hasattr(
                        pyaudio_backend.PyAudio, "get_loopback_device_info_generator"
                    ) and not hasattr(pyaudio_backend, "paWASAPI"):
                        self.show_error_dialog(
                            "系统声音监听不可用",
                            "监听 Windows 系统声音需要 PyAudioWPatch。",
                        )
                        return
                    try:
                        p_temp = pyaudio_backend.PyAudio()
                        try:
                            self.selected_devices = self.select_devices_dialog(p_temp)
                        finally:
                            p_temp.terminate()
                    except Exception as exc:
                        logger.error("设备初始化失败：%s", exc, exc_info=True)
                        self.show_error_dialog("设备初始化失败", f"无法初始化或枚举音频设备：\n{exc}")
                        return

                if not self.selected_devices:
                    logger.info("未选择任何设备，退出")
                    return

                try:
                    valid_devices, failures = self._probe_selected_devices_with_dialog()
                    for device_idx, reason in failures.items():
                        logger.warning(
                            "[Audio] session_id=%s device=%d preflight_failed reason=%s",
                            self.session_id,
                            device_idx,
                            reason,
                        )
                    self.selected_devices = valid_devices
                except Exception as exc:
                    logger.error("音频设备预检失败：%s", exc, exc_info=True)
                    self.show_error_dialog("音频设备预检失败", f"无法验证所选音频设备：\n{exc}")
                    return

                if not self.selected_devices:
                    self.show_error_dialog(
                        "音频设备不可用",
                        "所选音频设备无法打开，请重新连接设备或选择其他输入。",
                    )
                    return

                self.worker_thread = Thread(
                    target=self._worker_thread, name="SubtitleWorker", daemon=False
                )
                self.worker_thread.start()
                root.after(16, self.sync_ui)
                root.mainloop()
            finally:
                # Covers model-download cancellation, dependency/device failures,
                # unexpected Tk termination, and normal user close. cleanup() is idempotent.
                self.cleanup()
                try:
                    if root.winfo_exists():
                        root.destroy()
                except (tk.TclError, RuntimeError):
                    pass


# ================== 4. 参数解析 ==================

def get_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="日语实时字幕（Qwen3-ASR-1.7B + vLLM 原生 Streaming）")
    parser.add_argument("--device", type=int, default=-1, help="音频设备 ID，-1 表示自动选择")
    parser.add_argument("--capture-source", choices=["system", "mic", "both"], default="system")
    parser.add_argument("--mix-mode", choices=["average", "add"], default="average")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--asr-model", type=str, default=QWEN3_ASR_MODEL_ID)
    parser.add_argument("--asr-language", type=str, default=QWEN3_ASR_DEFAULT_LANGUAGE)
    parser.add_argument("--asr-model-revision", type=str, default="", help=argparse.SUPPRESS)
    parser.add_argument("--qwen-server-url", type=str, default=QWEN3_ASR_DEFAULT_SERVER_URL)
    parser.add_argument("--qwen-chunk-size-sec", type=float, default=QWEN3_ASR_DEFAULT_CHUNK_SIZE_SEC)
    parser.add_argument("--qwen-unfixed-chunk-num", type=int, default=QWEN3_ASR_DEFAULT_UNFIXED_CHUNK_NUM)
    parser.add_argument("--qwen-unfixed-token-num", type=int, default=QWEN3_ASR_DEFAULT_UNFIXED_TOKEN_NUM)
    parser.add_argument("--qwen-push-interval-ms", type=int, default=QWEN3_ASR_DEFAULT_PUSH_INTERVAL_MS)
    parser.add_argument("--qwen-http-timeout", type=float, default=60.0)
    parser.add_argument("--qwen-gpu-memory-utilization", type=float, default=QWEN3_ASR_RTX3090_GPU_MEMORY_UTILIZATION)
    parser.add_argument("--qwen-cpu-offload-gb", type=float, default=0.0, help="兼容旧配置；WSL + 官方 vLLM 0.14.0 下会强制归一为 0")
    parser.add_argument("--qwen-cuda-visible-devices", type=str, default=QWEN3_ASR_RTX3090_GPU_INDEX)
    parser.add_argument("--qwen-startup-timeout", type=float, default=900.0)
    parser.add_argument("--qwen-wsl-hf-home", type=str, default="")
    parser.add_argument("--wsl-distro", type=str, default="")
    parser.add_argument("--wsl-python", type=str, default="")
    parser.add_argument("--no-qwen-auto-start", action="store_true")
    parser.add_argument("--keep-qwen-server", action="store_true")
    parser.add_argument("--keep-wsl-running", action="store_true")
    parser.add_argument("--vad-model-path", type=str, default=os.path.join(_models_dir, "ten-vad.onnx"))
    parser.add_argument("--min-silence-duration", type=float, default=0.90)
    parser.add_argument("--min-speech-duration", type=float, default=0.12)
    parser.add_argument("--vad-threshold", type=float, default=0.40)
    parser.add_argument("--vad-buffer-size", type=int, default=30)
    parser.add_argument("--stable-partial-threshold", type=int, default=3)
    parser.add_argument("--max-utterance-seconds", type=float, default=18.0)
    parser.add_argument("--forced-segment-overlap-ms", type=int, default=560)
    parser.add_argument("--disable-hybrid-endpoint", dest="disable_hybrid_endpoint", action="store_true")
    parser.add_argument("--enable-hybrid-endpoint", dest="disable_hybrid_endpoint", action="store_false")
    parser.set_defaults(disable_hybrid_endpoint=False)
    parser.add_argument("--endpoint-punctuation-hold-ms", type=int, default=320)
    parser.add_argument("--endpoint-min-utterance-ms", type=int, default=1200)
    parser.add_argument("--speech-preroll-ms", type=int, default=1000)
    parser.add_argument(
        "--disable-audio-normalize",
        action="store_true",
        help="禁用保守音量归一化；默认启用归一化",
    )
    parser.add_argument("--audio-queue-size", type=int, default=120)
    parser.add_argument("--audio-backpressure-mode", choices=["buffered", "live"], default="buffered")
    parser.add_argument("--max-drain-chunks", type=int, default=20)
    parser.add_argument("--audio-latency-budget-ms", type=int, default=900)
    parser.add_argument("--audio-recovery-preroll-ms", type=int, default=900)
    parser.add_argument("--debug-save-audio", type=str, default="")
    parsed = parser.parse_args()
    argv = list(sys.argv[1:])
    explicit_dests = set()
    for action in parser._actions:
        if not action.option_strings:
            continue
        if any(argument == option or argument.startswith(option + "=") for argument in argv for option in action.option_strings):
            explicit_dests.add(action.dest)
    parsed._explicit_dests = explicit_dests
    return parsed


# ================== 5. 入口 ==================

# ================== 5. 入口 ==================

if __name__ == "__main__":
    if sys.platform.startswith("win"):
        multiprocessing.freeze_support()
    try:
        args = validate_args(get_args())
    except ValueError as exc:
        logger.error("启动参数错误：%s", exc)
        try:
            messagebox.showerror("启动参数错误", str(exc))
        except Exception:
            pass
        sys.exit(2)
    logger.info("=" * 60)
    logger.info("实时字幕 - Qwen3-ASR-1.7B ASR-only 主程序")
    logger.info("=" * 60)
    logger.info("VAD 模型：TEN-VAD (%s)", args.vad_model_path)
    logger.info("VAD：silence=%.2fs speech=%.2fs threshold=%.2f", args.min_silence_duration, args.min_speech_duration, args.vad_threshold)
    logger.info("ASR 模型：%s", args.asr_model)
    logger.info("Qwen server：%s", args.qwen_server_url)
    logger.info(
        "Qwen streaming：language=%s chunk=%.2fs unfixed_chunks=%d unfixed_tokens=%d push=%dms",
        args.asr_language or "auto", args.qwen_chunk_size_sec, args.qwen_unfixed_chunk_num,
        args.qwen_unfixed_token_num, args.qwen_push_interval_ms,
    )
    logger.info("默认音频来源：%s", args.capture_source)
    logger.info("音频归一化：%s", "off" if args.disable_audio_normalize else "on")
    logger.info("稳定 partial 确认阈值：%d", args.stable_partial_threshold)
    logger.info("连续语音最大段长：%.1fs", args.max_utterance_seconds)
    logger.info("强制分段重叠：%dms", args.forced_segment_overlap_ms)
    logger.info(
        "混合 endpoint：%s hold=%dms min=%dms",
        "off" if args.disable_hybrid_endpoint else "on",
        args.endpoint_punctuation_hold_ms, args.endpoint_min_utterance_ms,
    )
    logger.info("音频队列上限：%d mode=%s", args.audio_queue_size, args.audio_backpressure_mode)
    logger.info("ASR backend：Qwen3-ASR official demo_streaming (vLLM; RTX3090 physical GPU1 + CUDA PCI_BUS_ID mapping; CPU offload disabled)")
    logger.info("=" * 60)
    runtime = PreparedRuntimeLauncher(args)
    try:
        if not runtime.start():
            sys.exit(3)
        app = SubtitleApp(args)
        app.run()
    finally:
        runtime.close()
