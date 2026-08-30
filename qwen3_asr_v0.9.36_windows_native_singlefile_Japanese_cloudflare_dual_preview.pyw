# -*- coding: utf-8 -*-
# Main launcher revision: v0.9.36 Windows-native SINGLE-FILE (Qwen3-ASR + Cloudflare Workers AI dual-preview realtime + progressive + DPAPI)

import sys
import multiprocessing
import time
import os
import tkinter as tk
from tkinter import messagebox, ttk
from threading import Condition, Event, Thread, Lock, current_thread, main_thread, local
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
import requests
import socket
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

# ================== 实时翻译默认参数 ==================
TRANSLATION_DEFAULT_SOURCE: str = "ja"
TRANSLATION_DEFAULT_TARGET: str = "zh"
CLOUDFLARE_WORKERS_AI_MODEL: str = "@cf/meta/m2m100-1.2b"
CLOUDFLARE_API_BASE_URL: str = "https://api.cloudflare.com/client/v4/accounts"
CLOUDFLARE_ACCOUNT_ID_ENV: str = "CLOUDFLARE_ACCOUNT_ID"
CLOUDFLARE_API_TOKEN_ENV: str = "CLOUDFLARE_API_TOKEN"
CLOUDFLARE_TOKEN_DPAPI_FIELD: str = "cloudflare_api_token_dpapi"
CLOUDFLARE_TOKEN_DPAPI_PREFIX: str = "dpapi:v1:"
CLOUDFLARE_TOKEN_DPAPI_ENTROPY: bytes = b"NemoSubtitle.Cloudflare.WorkersAI.v1"
TRANSLATION_DEFAULT_PREVIEW_INTERVAL_MS: int = 250
TRANSLATION_DEFAULT_API_INTERVAL: int = 0  # deprecated compatibility field; ignored by v0.9.36 scheduler
TRANSLATION_REFERENCE_SHORT_THRESHOLD_BYTES: int = 10
TRANSLATION_DEFAULT_MIN_CHARS: int = 4
TRANSLATION_DEFAULT_MIN_DELTA_CHARS: int = 3
TRANSLATION_DEFAULT_PREVIEW_TIMEOUT: float = 2.0
TRANSLATION_DEFAULT_FINAL_TIMEOUT: float = 8.0
TRANSLATION_FINAL_RETRY_COUNT: int = 0
TRANSLATION_CACHE_MAX_ENTRIES: int = 256
TRANSLATION_CONTEXT_COUNT: int = 0
TRANSLATION_FINAL_DISPLAY_HOLD_SECONDS: float = 0.72
TRANSLATION_PREVIEW_CONCURRENCY: int = 2
TRANSLATION_PREVIEW_PENDING_SLOTS: int = 1
TRANSLATION_PREVIEW_TIMEOUT_BACKOFF_MS: int = 500
TRANSLATION_PREVIEW_RATE_LIMIT_BACKOFF_MS: int = 1000
TRANSLATION_PREVIEW_TIMEOUT_STREAK_TRIGGER: int = 2
TRANSLATION_PREVIEW_RECOVERY_SUCCESSES: int = 3
TRANSLATION_PREVIEW_EWMA_ALPHA: float = 0.35  # observability only; never controls normal pacing

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

    def append_with_record_id(
        self, reason: str, event_name: str, event: Any
    ) -> tuple[bool, str]:
        try:
            payload = asdict(event) if is_dataclass(event) else dict(getattr(event, "__dict__", {}))
            event_key = self._event_key(event_name, payload)
            now = time.time()
            with self._lock:
                self._ensure_loaded_locked()
                if event_key in self._pending_keys:
                    return True, self._pending_keys[event_key]
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
            return True, record["record_id"]
        except Exception as exc:
            self._write_failures += 1
            self._consecutive_write_failures += 1
            self._last_failure_at = time.time()
            self._last_error = str(exc)
            logger.error("[RECOVERY_JOURNAL] append failed path=%s", self.path, exc_info=True)
            return False, ""

    def append(self, reason: str, event_name: str, event: Any) -> bool:
        ok, _record_id = self.append_with_record_id(reason, event_name, event)
        return ok

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
        ok, _record_id = self.submit_durable_record(
            reason, event_name, event, timeout=timeout
        )
        return ok

    def submit_durable_record(
        self, reason: str, event_name: str, event: Any, *, timeout: float = 5.0
    ) -> tuple[bool, str]:
        """Durably append a final and return its stable recovery record id."""
        if self._closed.is_set():
            return self.journal.append_with_record_id(reason, event_name, event)
        done = Event()
        result: Dict[str, Any] = {}
        try:
            self._queue.put(
                ("append_durable_record", reason, event_name, event, done, result),
                timeout=0.05,
            )
        except queue.Full:
            logger.critical("[RECOVERY_SPOOL] durable queue full; using synchronous emergency write")
            return self.journal.append_with_record_id(reason, event_name, event)
        if done.wait(timeout=max(0.1, float(timeout))):
            return bool(result.get("ok", False)), str(result.get("record_id", ""))
        logger.error("[RECOVERY_SPOOL] durable append timeout; using idempotent direct fallback")
        return self.journal.append_with_record_id(reason, event_name, event)

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
                if item[0] in ("append", "append_durable", "append_durable_record"):
                    if item[0] in ("append_durable", "append_durable_record"):
                        _, reason, event_name, event, done, result = item
                    else:
                        _, reason, event_name, event = item
                        done = None
                        result = None
                    if item[0] == "append_durable_record":
                        ok, record_id = self.journal.append_with_record_id(
                            reason, event_name, event
                        )
                    else:
                        ok = self.journal.append(reason, event_name, event)
                        record_id = ""
                    if not ok:
                        self._write_failures += 1
                        self._consecutive_write_failures += 1
                    else:
                        self._consecutive_write_failures = 0
                    if result is not None:
                        result["ok"] = bool(ok)
                        result["record_id"] = record_id
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
        self._recovery_delivery_lock = Lock()
        self._recovery_record_by_event_object: Dict[int, tuple[Any, str]] = {}
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

    def _release_recovery_event_reference(self, event: Any) -> None:
        """Drop the delivery-only strong reference once a final is abandoned to WAL."""
        with self._recovery_delivery_lock:
            tracked = self._recovery_record_by_event_object.get(id(event))
            if tracked is not None and tracked[0] is event:
                self._recovery_record_by_event_object.pop(id(event), None)

    def spill_final(self, reason: str, event_name: str, event: Any) -> bool:
        if not _is_final_event(event) or self._journal is None:
            return False
        if self._recovery_spooler is not None:
            persisted = self._recovery_spooler.submit_durable(reason, event_name, event)
        else:
            # Late shutdown fallback; never invoked while holding EventBus/session locks.
            persisted = self._journal.append(reason, event_name, event)
        # spill_final() is used only after in-memory delivery has been abandoned
        # (no subscriber, backlog, callback failure, or shutdown). The write-ahead
        # journal owns recovery from this point; retaining the event cannot improve
        # durability and leaks one strong reference per undelivered final.
        self._release_recovery_event_reference(event)
        return persisted

    def _persist_final_before_publish(self, event_name: str, event: Any) -> bool:
        """Write-ahead every final so an accepted in-memory event survives a crash."""
        if not _is_final_event(event) or self._journal is None:
            return True
        if self._recovery_spooler is not None:
            ok, record_id = self._recovery_spooler.submit_durable_record(
                "eventbus_write_ahead", event_name, event
            )
        else:
            ok, record_id = self._journal.append_with_record_id(
                "eventbus_write_ahead", event_name, event
            )
        if ok and record_id:
            with self._recovery_delivery_lock:
                # Keep a strong event reference until delivery is acknowledged. This
                # prevents CPython object-id reuse from acknowledging the wrong final.
                self._recovery_record_by_event_object[id(event)] = (event, record_id)
        return bool(ok and record_id)

    def acknowledge_delivered_event(self, event: Any) -> bool:
        """ACK one final only after its application-level consumer has handled it."""
        if not _is_final_event(event) or self._journal is None:
            return True
        with self._recovery_delivery_lock:
            tracked = self._recovery_record_by_event_object.pop(id(event), None)
        if tracked is None:
            return True
        _strong_event_ref, record_id = tracked
        ok = self.acknowledge_recovery([record_id])
        if not ok:
            with self._recovery_delivery_lock:
                self._recovery_record_by_event_object[id(event)] = tracked
        return ok

    def replay_pending(
        self,
        event_name: str,
        decoder: Callable[[Dict[str, Any], Dict[str, Any]], Any],
        *,
        limit: int = 1000,
    ) -> int:
        """Requeue durable finals after subscribers are installed at startup."""
        restored = 0
        for record in self.pending_recovery_records(limit=limit):
            if str(record.get("event_name", "")) != str(event_name):
                continue
            record_id = str(record.get("record_id", "") or "")
            try:
                event = decoder(dict(record.get("event", {})), record)
            except Exception:
                logger.error(
                    "[RECOVERY_REPLAY] decode failed event=%s record_id=%s",
                    event_name,
                    record_id,
                    exc_info=True,
                )
                continue
            if not record_id or not _is_final_event(event):
                logger.error(
                    "[RECOVERY_REPLAY] invalid final event=%s record_id=%s",
                    event_name,
                    record_id,
                )
                continue
            with self._condition:
                if self._state != self.RUNNING or len(self._finals) >= self._max_pending_finals:
                    break
                with self._recovery_delivery_lock:
                    self._recovery_record_by_event_object[id(event)] = (event, record_id)
                self._finals.append((str(event_name), event))
                self._condition.notify()
            restored += 1
        if restored:
            logger.warning("[RECOVERY_REPLAY] event=%s restored=%d", event_name, restored)
        return restored

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
        if _is_final_event(event) and not self._persist_final_before_publish(name, event):
            logger.critical("[SESSION_FINAL_WAL] event=%s durable write failed", name)
            return False
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
APP_VERSION = "0.9.25-qwen3-asr-windows-native-singlefile-fix1-japanese"
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



def _dpapi_blob_crypt(data: bytes, *, protect: bool) -> bytes:
    """Protect/unprotect bytes with Windows DPAPI for the current user.

    No key is embedded in the application. Windows binds the encrypted blob to the
    current Windows user profile. CRYPTPROTECT_UI_FORBIDDEN prevents unexpected
    credential UI from a background/desktop subtitle process.
    """
    if sys.platform != "win32":
        raise RuntimeError("Windows DPAPI 仅可在 Windows 上使用")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]

    def make_blob(raw: bytes):
        if not raw:
            return DATA_BLOB(0, None), None
        buffer = ctypes.create_string_buffer(raw, len(raw))
        blob = DATA_BLOB(
            len(raw),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
        )
        return blob, buffer

    input_blob, input_buffer = make_blob(bytes(data))
    entropy_blob, entropy_buffer = make_blob(CLOUDFLARE_TOKEN_DPAPI_ENTROPY)
    output_blob = DATA_BLOB()
    flags = 0x01  # CRYPTPROTECT_UI_FORBIDDEN

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if protect:
        fn = crypt32.CryptProtectData
        fn.argtypes = [
            ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR,
            ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
        ]
        fn.restype = wintypes.BOOL
        ok = fn(
            ctypes.byref(input_blob),
            "NemoSubtitle Cloudflare API Token",
            ctypes.byref(entropy_blob),
            None,
            None,
            flags,
            ctypes.byref(output_blob),
        )
    else:
        fn = crypt32.CryptUnprotectData
        fn.argtypes = [
            ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
            ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
        ]
        fn.restype = wintypes.BOOL
        ok = fn(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            flags,
            ctypes.byref(output_blob),
        )

    # Keep source/entropy buffers alive through the Win32 call.
    _ = (input_buffer, entropy_buffer)
    if not ok:
        raise ctypes.WinError()
    try:
        if not output_blob.pbData or output_blob.cbData <= 0:
            return b""
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if output_blob.pbData:
            kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def _dpapi_protect_bytes(data: bytes) -> bytes:
    return _dpapi_blob_crypt(data, protect=True)


def _dpapi_unprotect_bytes(data: bytes) -> bytes:
    return _dpapi_blob_crypt(data, protect=False)


def _protect_secret_dpapi(secret: str) -> str:
    value = str(secret or "")
    if not value:
        return ""
    encrypted = _dpapi_protect_bytes(value.encode("utf-8"))
    return CLOUDFLARE_TOKEN_DPAPI_PREFIX + base64.b64encode(encrypted).decode("ascii")


def _unprotect_secret_dpapi(value: str) -> str:
    encoded = str(value or "").strip()
    if not encoded:
        return ""
    if not encoded.startswith(CLOUDFLARE_TOKEN_DPAPI_PREFIX):
        raise ValueError("未知的 DPAPI Token 格式")
    raw = base64.b64decode(
        encoded[len(CLOUDFLARE_TOKEN_DPAPI_PREFIX):].encode("ascii"),
        validate=True,
    )
    plain = _dpapi_unprotect_bytes(raw)
    return plain.decode("utf-8")


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


def _translation_settings_from_args(
    args: argparse.Namespace,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Load Cloudflare Workers AI translation preferences.

    Token precedence is explicit CLI > CLOUDFLARE_API_TOKEN environment variable
    > Windows DPAPI encrypted settings. Legacy/plaintext token fields on disk are
    never accepted.
    """
    settings: Dict[str, Any] = {
        "enabled": not bool(args.disable_translation),
        "source_language": str(args.translation_source_language),
        "target_language": str(args.translation_target_language),
        "cloudflare_account_id": str(args.cloudflare_account_id or os.environ.get(CLOUDFLARE_ACCOUNT_ID_ENV, "")),
        "cloudflare_api_token": str(args.cloudflare_api_token or os.environ.get(CLOUDFLARE_API_TOKEN_ENV, "")),
        "preview_interval_ms": int(args.translation_preview_interval_ms),
        "api_interval": int(getattr(args, "translation_api_interval", TRANSLATION_DEFAULT_API_INTERVAL)),
        "min_chars": int(args.translation_min_chars),
        "min_delta_chars": int(args.translation_min_delta_chars),
        "preview_timeout": float(args.translation_preview_timeout),
        "final_timeout": float(args.translation_final_timeout),
    }
    persisted = payload.get("translation", {}) if isinstance(payload, dict) else {}
    if not isinstance(persisted, dict):
        logger.warning("translation 设置结构无效，已忽略")
        return settings
    explicit = set(getattr(args, "_explicit_dests", set()) or set())
    dest_by_key = {
        "enabled": "disable_translation",
        "source_language": "translation_source_language",
        "target_language": "translation_target_language",
        "cloudflare_account_id": "cloudflare_account_id",
        "preview_interval_ms": "translation_preview_interval_ms",
        "api_interval": "translation_api_interval",
        "min_chars": "translation_min_chars",
        "min_delta_chars": "translation_min_delta_chars",
        "preview_timeout": "translation_preview_timeout",
        "final_timeout": "translation_final_timeout",
    }
    candidate = dict(settings)
    try:
        persisted_version = int(payload.get("version", 0) or 0) if isinstance(payload, dict) else 0
        realtime_scheduler_keys = {
            "preview_interval_ms", "api_interval", "min_chars",
            "min_delta_chars", "preview_timeout",
        }
        for key, dest in dest_by_key.items():
            # v0.9.36 retains the realtime scheduler migration and adds bounded dual-preview concurrency.
            # Preserve credentials/language/enabled state from old settings, but
            # intentionally migrate scheduler tuning to the new realtime defaults.
            if persisted_version < 6 and key in realtime_scheduler_keys:
                continue
            if key not in persisted or dest in explicit:
                continue
            value = persisted[key]
            if key == "enabled":
                if not isinstance(value, bool):
                    raise ValueError("translation.enabled 必须是布尔值")
                candidate[key] = value
            elif key in ("preview_interval_ms", "api_interval", "min_chars", "min_delta_chars"):
                candidate[key] = int(value)
            elif key in ("preview_timeout", "final_timeout"):
                candidate[key] = float(value)
            else:
                candidate[key] = str(value)
        # Never load a legacy/plaintext token from JSON. Only restore the DPAPI
        # ciphertext when neither CLI nor environment supplied a runtime token.
        if not str(candidate.get("cloudflare_api_token", "") or "").strip():
            encrypted_token = persisted.get(CLOUDFLARE_TOKEN_DPAPI_FIELD, "")
            if encrypted_token:
                try:
                    candidate["cloudflare_api_token"] = _unprotect_secret_dpapi(str(encrypted_token))
                except Exception as exc:
                    logger.warning(
                        "已保存的 Cloudflare API Token 无法用当前 Windows 用户解密，将要求重新输入：%s",
                        type(exc).__name__,
                    )
                    candidate["cloudflare_api_token"] = ""

        if not str(candidate["source_language"]).strip():
            raise ValueError("translation.source_language 不能为空")
        if not str(candidate["target_language"]).strip():
            raise ValueError("translation.target_language 不能为空")
        if not (100 <= int(candidate["preview_interval_ms"]) <= 5000):
            raise ValueError("translation.preview_interval_ms 超出范围")
        if not (0 <= int(candidate["api_interval"]) <= 10):
            raise ValueError("translation.api_interval 超出范围")
        if not (1 <= int(candidate["min_chars"]) <= 100):
            raise ValueError("translation.min_chars 超出范围")
        if not (1 <= int(candidate["min_delta_chars"]) <= 100):
            raise ValueError("translation.min_delta_chars 超出范围")
        if not (0.2 <= float(candidate["preview_timeout"]) <= 30.0):
            raise ValueError("translation.preview_timeout 超出范围")
        if not (0.2 <= float(candidate["final_timeout"]) <= 60.0):
            raise ValueError("translation.final_timeout 超出范围")
        return candidate
    except Exception as exc:
        logger.warning("已保存的 Cloudflare 翻译设置无效，整组忽略：%s", exc)
        return settings


def _save_application_settings(payload: Dict[str, Any]) -> None:
    """Atomically persist whitelisted preferences; never write model text or plaintext secrets."""
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
            enabled = audio["enable_hybrid_endpoint"]
            if not isinstance(enabled, bool):
                raise ValueError("audio.enable_hybrid_endpoint 必须是布尔值")
            candidate.disable_hybrid_endpoint = not enabled
        validate_args(candidate)
        topmost = display.get("topmost", True)
        if not isinstance(topmost, bool):
            raise ValueError("display.topmost 必须是布尔值")
        safe_display = {
            "source_font_size": max(12, min(72, int(display.get("source_font_size", 24)))),
            "translation_font_size": max(12, min(72, int(display.get("translation_font_size", 22)))),
            "opacity": max(0.50, min(1.00, float(display.get("opacity", 0.95)))),
            "topmost": topmost,
        }
    except Exception as exc:
        logger.warning("已保存的运行参数无效，整组忽略：%s", exc)
        return {}
    for key in direct_audio:
        setattr(args, key, getattr(candidate, key))
    args.disable_hybrid_endpoint = candidate.disable_hybrid_endpoint
    logger.info("已恢复保存的实时字幕设置")
    return safe_display


QWEN3_ASR_MODEL_ID = "Qwen/Qwen3-ASR-1.7B"
QWEN3_ASR_DEFAULT_LANGUAGE = "Japanese"  # force Japanese ASR
QWEN3_ASR_DEFAULT_SERVER_URL = "http://127.0.0.1:8000"
QWEN3_ASR_DEFAULT_CHUNK_SIZE_SEC = 1.0
QWEN3_ASR_DEFAULT_UNFIXED_CHUNK_NUM = 4
QWEN3_ASR_DEFAULT_UNFIXED_TOKEN_NUM = 5
QWEN3_ASR_DEFAULT_PUSH_INTERVAL_MS = 500  # official browser demo pushes 500 ms PCM blocks
QWEN3_ASR_RTX3090_GPU_INDEX = "0"  # nvidia-smi physical index: user-verified RTX 3090
QWEN3_ASR_RTX3090_DEVICE_ORDER = "PCI_BUS_ID"  # align CUDA numeric ordinals with NVIDIA physical ordering
QWEN3_ASR_RTX3090_GPU_MEMORY_UTILIZATION = 0.45  # 3090 single-stream balanced profile: ~10.8 GiB budget; avoids reserving 90% VRAM
QWEN3_ASR_RTX3090_MAX_MODEL_LEN = 24576  # single-stream ASR does not need Qwen's full 65536 context; keeps KV cache within the 0.45 VRAM budget
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

# RTX 3090 low-VRAM bootstrap: preserve Qwen's official streaming code while
# constraining only vLLM's single-stream scheduler/context sizing.  This avoids
# the 65,536-token KV-cache reservation that cannot fit inside a 0.45 VRAM budget.
QWEN_OFFICIAL_3090_LOW_VRAM_BOOTSTRAP = r'''
import vllm
from qwen_asr.cli import demo_streaming as _official_demo


def main():
    original_llm = vllm.LLM

    def _llm_3090_low_vram(*args, **kwargs):
        kwargs["max_num_seqs"] = 1
        kwargs["max_model_len"] = __MAX_MODEL_LEN__
        return original_llm(*args, **kwargs)

    vllm.LLM = _llm_3090_low_vram
    try:
        _official_demo.main()
    finally:
        vllm.LLM = original_llm


if __name__ == "__main__":
    main()
'''.replace("__MAX_MODEL_LEN__", str(QWEN3_ASR_RTX3090_MAX_MODEL_LEN)).strip()



# ================== 内嵌 Windows 原生 Qwen3-ASR Sidecar ==================
# 真单文件模式：主程序与 ASR sidecar 都来自本 .pyw。
# 子进程再次启动本文件并进入内部 sidecar 模式，不需要外部 .py，也不使用 WSL。
QWEN_WINDOWS_NATIVE_SIDECAR_B64 = 'IyBjb2Rpbmc6IHV0Zi04CiIiIldpbmRvd3MtbmF0aXZlIFF3ZW4zLUFTUiByb2xsaW5nLXN0cmVhbSBjb21wYXRpYmlsaXR5IHNpZGVjYXIuCgpJbXBsZW1lbnRzIHRoZSBzYW1lIGFjY3VtdWxhdGVkLWF1ZGlvICsgcm9sbGJhY2stcHJlZml4IGFsZ29yaXRobSBkb2N1bWVudGVkIGJ5ClF3ZW4zLUFTUidzIG9mZmljaWFsIHN0cmVhbWluZ190cmFuc2NyaWJlKCksIGJ1dCBydW5zIHRoZSBUcmFuc2Zvcm1lcnMgYmFja2VuZApvbiBXaW5kb3dzL0NVREEgYmVjYXVzZSB1cHN0cmVhbSBzdGF0ZWZ1bCBzdHJlYW1pbmcgaXMgY3VycmVudGx5IHZMTE0tb25seS4KIiIiCgppbXBvcnQgYXJncGFyc2UKaW1wb3J0IHRpbWUKaW1wb3J0IHV1aWQKaW1wb3J0IHRocmVhZGluZwpmcm9tIGRhdGFjbGFzc2VzIGltcG9ydCBkYXRhY2xhc3MKZnJvbSB0eXBpbmcgaW1wb3J0IERpY3QKCmltcG9ydCBudW1weSBhcyBucAppbXBvcnQgdG9yY2gKZnJvbSBmbGFzayBpbXBvcnQgRmxhc2ssIGpzb25pZnksIHJlcXVlc3QKZnJvbSB3ZXJremV1Zy5zZXJ2aW5nIGltcG9ydCBtYWtlX3NlcnZlcgoKZnJvbSBxd2VuX2FzciBpbXBvcnQgUXdlbjNBU1JNb2RlbApmcm9tIHF3ZW5fYXNyLmluZmVyZW5jZS51dGlscyBpbXBvcnQgcGFyc2VfYXNyX291dHB1dAoKCkBkYXRhY2xhc3MKY2xhc3MgU3RyZWFtaW5nU3RhdGU6CiAgICB1bmZpeGVkX2NodW5rX251bTogaW50CiAgICB1bmZpeGVkX3Rva2VuX251bTogaW50CiAgICBjaHVua19zaXplX3NlYzogZmxvYXQKICAgIGNodW5rX3NpemVfc2FtcGxlczogaW50CiAgICBjaHVua19pZDogaW50CiAgICBidWZmZXI6IG5wLm5kYXJyYXkKICAgIGF1ZGlvX2FjY3VtOiBucC5uZGFycmF5CiAgICBwcm9tcHRfcmF3OiBzdHIKICAgIGZvcmNlX2xhbmd1YWdlOiBzdHIKICAgIHJhd19kZWNvZGVkOiBzdHIKICAgIGxhbmd1YWdlOiBzdHIKICAgIHRleHQ6IHN0cgoKCkBkYXRhY2xhc3MKY2xhc3MgU2Vzc2lvbjoKICAgIHN0YXRlOiBTdHJlYW1pbmdTdGF0ZQogICAgY3JlYXRlZF9hdDogZmxvYXQKICAgIGxhc3Rfc2VlbjogZmxvYXQKCgphcHAgPSBGbGFzayhfX25hbWVfXykKQVNSID0gTm9uZQpTRVNTSU9OUzogRGljdFtzdHIsIFNlc3Npb25dID0ge30KTU9ERUxfTE9DSyA9IHRocmVhZGluZy5STG9jaygpClNFU1NJT05fTE9DSyA9IHRocmVhZGluZy5STG9jaygpClNFU1NJT05fVFRMX1NFQyA9IDEwICogNjAKVU5GSVhFRF9DSFVOS19OVU0gPSA0ClVORklYRURfVE9LRU5fTlVNID0gNQpDSFVOS19TSVpFX1NFQyA9IDEuMApNT0RFTF9OQU1FID0gIiIKRk9SQ0VfTEFOR1VBR0UgPSAiSmFwYW5lc2UiCgoKZGVmIF9nY19zZXNzaW9ucygpIC0+IE5vbmU6CiAgICBub3cgPSB0aW1lLnRpbWUoKQogICAgd2l0aCBTRVNTSU9OX0xPQ0s6CiAgICAgICAgZGVhZCA9IFtzaWQgZm9yIHNpZCwgcyBpbiBTRVNTSU9OUy5pdGVtcygpIGlmIG5vdyAtIHMubGFzdF9zZWVuID4gU0VTU0lPTl9UVExfU0VDXQogICAgICAgIGZvciBzaWQgaW4gZGVhZDoKICAgICAgICAgICAgU0VTU0lPTlMucG9wKHNpZCwgTm9uZSkKCgpkZWYgX2dldF9zZXNzaW9uKHNlc3Npb25faWQ6IHN0cik6CiAgICBfZ2Nfc2Vzc2lvbnMoKQogICAgd2l0aCBTRVNTSU9OX0xPQ0s6CiAgICAgICAgc2Vzc2lvbiA9IFNFU1NJT05TLmdldChzZXNzaW9uX2lkKQogICAgICAgIGlmIHNlc3Npb24gaXMgbm90IE5vbmU6CiAgICAgICAgICAgIHNlc3Npb24ubGFzdF9zZWVuID0gdGltZS50aW1lKCkKICAgICAgICByZXR1cm4gc2Vzc2lvbgoKCmRlZiBfcm9sbGJhY2tfcHJlZml4KHN0YXRlOiBTdHJlYW1pbmdTdGF0ZSkgLT4gc3RyOgogICAgaWYgc3RhdGUuY2h1bmtfaWQgPCBzdGF0ZS51bmZpeGVkX2NodW5rX251bSBvciBub3Qgc3RhdGUucmF3X2RlY29kZWQ6CiAgICAgICAgcmV0dXJuICIiCgogICAgdG9rZW5faWRzID0gQVNSLnByb2Nlc3Nvci50b2tlbml6ZXIuZW5jb2RlKHN0YXRlLnJhd19kZWNvZGVkKQogICAgcm9sbGJhY2sgPSBtYXgoMCwgaW50KHN0YXRlLnVuZml4ZWRfdG9rZW5fbnVtKSkKICAgIHdoaWxlIFRydWU6CiAgICAgICAgZW5kX2lkeCA9IG1heCgwLCBsZW4odG9rZW5faWRzKSAtIHJvbGxiYWNrKQogICAgICAgIHByZWZpeCA9IEFTUi5wcm9jZXNzb3IudG9rZW5pemVyLmRlY29kZSh0b2tlbl9pZHNbOmVuZF9pZHhdKSBpZiBlbmRfaWR4ID4gMCBlbHNlICIiCiAgICAgICAgaWYgIlx1ZmZmZCIgbm90IGluIHByZWZpeDoKICAgICAgICAgICAgcmV0dXJuIHByZWZpeAogICAgICAgIGlmIGVuZF9pZHggPT0gMDoKICAgICAgICAgICAgcmV0dXJuICIiCiAgICAgICAgcm9sbGJhY2sgKz0gMQoKCmRlZiBfZGVjb2RlX2FjY3VtdWxhdGVkKHN0YXRlOiBTdHJlYW1pbmdTdGF0ZSkgLT4gTm9uZToKICAgIGlmIHN0YXRlLmF1ZGlvX2FjY3VtLnNpemUgPT0gMDoKICAgICAgICByZXR1cm4KCiAgICBwcmVmaXggPSBfcm9sbGJhY2tfcHJlZml4KHN0YXRlKQogICAgcHJvbXB0ID0gc3RhdGUucHJvbXB0X3JhdyArIHByZWZpeAoKICAgIGlucHV0cyA9IEFTUi5wcm9jZXNzb3IoCiAgICAgICAgdGV4dD1bcHJvbXB0XSwKICAgICAgICBhdWRpbz1bc3RhdGUuYXVkaW9fYWNjdW1dLAogICAgICAgIHJldHVybl90ZW5zb3JzPSJwdCIsCiAgICAgICAgcGFkZGluZz1UcnVlLAogICAgKQogICAgaW5wdXRzID0gaW5wdXRzLnRvKEFTUi5tb2RlbC5kZXZpY2UpLnRvKEFTUi5tb2RlbC5kdHlwZSkKCiAgICB3aXRoIHRvcmNoLmluZmVyZW5jZV9tb2RlKCk6CiAgICAgICAgZ2VuZXJhdGlvbl9jb25maWcgPSBnZXRhdHRyKEFTUi5tb2RlbCwgImdlbmVyYXRpb25fY29uZmlnIiwgTm9uZSkKICAgICAgICBlb3NfdG9rZW5faWQgPSBnZXRhdHRyKGdlbmVyYXRpb25fY29uZmlnLCAiZW9zX3Rva2VuX2lkIiwgTm9uZSkgaWYgZ2VuZXJhdGlvbl9jb25maWcgaXMgbm90IE5vbmUgZWxzZSBOb25lCiAgICAgICAgcGFkX3Rva2VuX2lkID0gZ2V0YXR0cihnZW5lcmF0aW9uX2NvbmZpZywgInBhZF90b2tlbl9pZCIsIE5vbmUpIGlmIGdlbmVyYXRpb25fY29uZmlnIGlzIG5vdCBOb25lIGVsc2UgTm9uZQogICAgICAgIGlmIHBhZF90b2tlbl9pZCBpcyBOb25lOgogICAgICAgICAgICBwYWRfdG9rZW5faWQgPSBlb3NfdG9rZW5faWQKICAgICAgICBnZW5lcmF0ZWQgPSBBU1IubW9kZWwuZ2VuZXJhdGUoCiAgICAgICAgICAgICoqaW5wdXRzLAogICAgICAgICAgICBtYXhfbmV3X3Rva2Vucz1BU1IubWF4X25ld190b2tlbnMsCiAgICAgICAgICAgIGRvX3NhbXBsZT1GYWxzZSwKICAgICAgICAgICAgcGFkX3Rva2VuX2lkPXBhZF90b2tlbl9pZCwKICAgICAgICApCgogICAgc2VxdWVuY2VzID0gZ2VuZXJhdGVkLnNlcXVlbmNlcyBpZiBoYXNhdHRyKGdlbmVyYXRlZCwgInNlcXVlbmNlcyIpIGVsc2UgZ2VuZXJhdGVkCiAgICBwcm9tcHRfbGVuID0gaW50KGlucHV0c1siaW5wdXRfaWRzIl0uc2hhcGVbMV0pCiAgICBkZWNvZGVkID0gQVNSLnByb2Nlc3Nvci5iYXRjaF9kZWNvZGUoCiAgICAgICAgc2VxdWVuY2VzWzosIHByb21wdF9sZW46XSwKICAgICAgICBza2lwX3NwZWNpYWxfdG9rZW5zPVRydWUsCiAgICAgICAgY2xlYW5fdXBfdG9rZW5pemF0aW9uX3NwYWNlcz1GYWxzZSwKICAgIClbMF0KCiAgICBzdGF0ZS5yYXdfZGVjb2RlZCA9IHByZWZpeCArIGRlY29kZWQKICAgIGxhbmd1YWdlLCB0ZXh0ID0gcGFyc2VfYXNyX291dHB1dChzdGF0ZS5yYXdfZGVjb2RlZCwgdXNlcl9sYW5ndWFnZT1zdGF0ZS5mb3JjZV9sYW5ndWFnZSBvciBOb25lKQogICAgc3RhdGUubGFuZ3VhZ2UgPSBsYW5ndWFnZQogICAgc3RhdGUudGV4dCA9IHRleHQKICAgIHN0YXRlLmNodW5rX2lkICs9IDEKCgpkZWYgX2ZlZWQoc3RhdGU6IFN0cmVhbWluZ1N0YXRlLCB3YXY6IG5wLm5kYXJyYXkpIC0+IE5vbmU6CiAgICBhdWRpbyA9IG5wLmFzYXJyYXkod2F2KQogICAgaWYgYXVkaW8ubmRpbSAhPSAxOgogICAgICAgIGF1ZGlvID0gYXVkaW8ucmVzaGFwZSgtMSkKICAgIGlmIGF1ZGlvLmR0eXBlID09IG5wLmludDE2OgogICAgICAgIGF1ZGlvID0gYXVkaW8uYXN0eXBlKG5wLmZsb2F0MzIpIC8gMzI3NjguMAogICAgZWxzZToKICAgICAgICBhdWRpbyA9IGF1ZGlvLmFzdHlwZShucC5mbG9hdDMyLCBjb3B5PUZhbHNlKQoKICAgIGlmIGF1ZGlvLnNpemU6CiAgICAgICAgc3RhdGUuYnVmZmVyID0gbnAuY29uY2F0ZW5hdGUoW3N0YXRlLmJ1ZmZlciwgYXVkaW9dKQoKICAgIHdoaWxlIHN0YXRlLmJ1ZmZlci5zaXplID49IHN0YXRlLmNodW5rX3NpemVfc2FtcGxlczoKICAgICAgICBjaHVuayA9IHN0YXRlLmJ1ZmZlcls6IHN0YXRlLmNodW5rX3NpemVfc2FtcGxlc10KICAgICAgICBzdGF0ZS5idWZmZXIgPSBzdGF0ZS5idWZmZXJbc3RhdGUuY2h1bmtfc2l6ZV9zYW1wbGVzIDpdCiAgICAgICAgaWYgc3RhdGUuYXVkaW9fYWNjdW0uc2l6ZSA9PSAwOgogICAgICAgICAgICBzdGF0ZS5hdWRpb19hY2N1bSA9IGNodW5rLmNvcHkoKQogICAgICAgIGVsc2U6CiAgICAgICAgICAgIHN0YXRlLmF1ZGlvX2FjY3VtID0gbnAuY29uY2F0ZW5hdGUoW3N0YXRlLmF1ZGlvX2FjY3VtLCBjaHVua10pCiAgICAgICAgX2RlY29kZV9hY2N1bXVsYXRlZChzdGF0ZSkKCgpkZWYgX2ZpbmlzaChzdGF0ZTogU3RyZWFtaW5nU3RhdGUpIC0+IE5vbmU6CiAgICBpZiBzdGF0ZS5idWZmZXIuc2l6ZSA9PSAwOgogICAgICAgIHJldHVybgogICAgdGFpbCA9IHN0YXRlLmJ1ZmZlcgogICAgc3RhdGUuYnVmZmVyID0gbnAuemVyb3MoKDAsKSwgZHR5cGU9bnAuZmxvYXQzMikKICAgIGlmIHN0YXRlLmF1ZGlvX2FjY3VtLnNpemUgPT0gMDoKICAgICAgICBzdGF0ZS5hdWRpb19hY2N1bSA9IHRhaWwuY29weSgpCiAgICBlbHNlOgogICAgICAgIHN0YXRlLmF1ZGlvX2FjY3VtID0gbnAuY29uY2F0ZW5hdGUoW3N0YXRlLmF1ZGlvX2FjY3VtLCB0YWlsXSkKICAgIF9kZWNvZGVfYWNjdW11bGF0ZWQoc3RhdGUpCgoKQGFwcC5nZXQoIi9oZWFsdGgiKQpkZWYgaGVhbHRoKCk6CiAgICBncHVfbmFtZSA9IHRvcmNoLmN1ZGEuZ2V0X2RldmljZV9uYW1lKDApIGlmIHRvcmNoLmN1ZGEuaXNfYXZhaWxhYmxlKCkgZWxzZSAiIgogICAgcmV0dXJuIGpzb25pZnkoCiAgICAgICAgewogICAgICAgICAgICAib2siOiBUcnVlLAogICAgICAgICAgICAiYmFja2VuZCI6ICJ0cmFuc2Zvcm1lcnNfd2luZG93c19jdWRhIiwKICAgICAgICAgICAgInByb3RvY29sIjogInF3ZW4zLWFzci13aW5kb3dzLXRyYW5zZm9ybWVycy1yb2xsaW5nLXYyIiwKICAgICAgICAgICAgIm1vZGVsIjogTU9ERUxfTkFNRSwKICAgICAgICAgICAgImdwdSI6IGdwdV9uYW1lLAogICAgICAgICAgICAibGFuZ3VhZ2UiOiBGT1JDRV9MQU5HVUFHRSwKICAgICAgICB9CiAgICApCgoKQGFwcC5wb3N0KCIvYXBpL3N0YXJ0IikKZGVmIGFwaV9zdGFydCgpOgogICAgc2Vzc2lvbl9pZCA9IHV1aWQudXVpZDQoKS5oZXgKICAgIHJlcXVlc3RlZF9sYW5ndWFnZSA9IHN0cihyZXF1ZXN0LmFyZ3MuZ2V0KCJsYW5ndWFnZSIsICIiKSBvciAiIikuc3RyaXAoKQogICAgZm9yY2VfbGFuZ3VhZ2UgPSByZXF1ZXN0ZWRfbGFuZ3VhZ2Ugb3IgRk9SQ0VfTEFOR1VBR0UKICAgIGlmIGZvcmNlX2xhbmd1YWdlOgogICAgICAgIGZvcmNlX2xhbmd1YWdlID0gZm9yY2VfbGFuZ3VhZ2VbOjFdLnVwcGVyKCkgKyBmb3JjZV9sYW5ndWFnZVsxOl0ubG93ZXIoKQogICAgcHJvbXB0X3JhdyA9IEFTUi5fYnVpbGRfdGV4dF9wcm9tcHQoY29udGV4dD0iIiwgZm9yY2VfbGFuZ3VhZ2U9Zm9yY2VfbGFuZ3VhZ2Ugb3IgTm9uZSkKICAgIHN0YXRlID0gU3RyZWFtaW5nU3RhdGUoCiAgICAgICAgdW5maXhlZF9jaHVua19udW09VU5GSVhFRF9DSFVOS19OVU0sCiAgICAgICAgdW5maXhlZF90b2tlbl9udW09VU5GSVhFRF9UT0tFTl9OVU0sCiAgICAgICAgY2h1bmtfc2l6ZV9zZWM9Q0hVTktfU0laRV9TRUMsCiAgICAgICAgY2h1bmtfc2l6ZV9zYW1wbGVzPW1heCgxLCBpbnQocm91bmQoQ0hVTktfU0laRV9TRUMgKiAxNjAwMCkpKSwKICAgICAgICBjaHVua19pZD0wLAogICAgICAgIGJ1ZmZlcj1ucC56ZXJvcygoMCwpLCBkdHlwZT1ucC5mbG9hdDMyKSwKICAgICAgICBhdWRpb19hY2N1bT1ucC56ZXJvcygoMCwpLCBkdHlwZT1ucC5mbG9hdDMyKSwKICAgICAgICBwcm9tcHRfcmF3PXByb21wdF9yYXcsCiAgICAgICAgZm9yY2VfbGFuZ3VhZ2U9Zm9yY2VfbGFuZ3VhZ2UsCiAgICAgICAgcmF3X2RlY29kZWQ9IiIsCiAgICAgICAgbGFuZ3VhZ2U9IiIsCiAgICAgICAgdGV4dD0iIiwKICAgICkKICAgIG5vdyA9IHRpbWUudGltZSgpCiAgICB3aXRoIFNFU1NJT05fTE9DSzoKICAgICAgICBTRVNTSU9OU1tzZXNzaW9uX2lkXSA9IFNlc3Npb24oc3RhdGU9c3RhdGUsIGNyZWF0ZWRfYXQ9bm93LCBsYXN0X3NlZW49bm93KQogICAgcmV0dXJuIGpzb25pZnkoeyJzZXNzaW9uX2lkIjogc2Vzc2lvbl9pZCwgImJhY2tlbmQiOiAidHJhbnNmb3JtZXJzX3dpbmRvd3NfY3VkYSJ9KQoKCkBhcHAucG9zdCgiL2FwaS9jaHVuayIpCmRlZiBhcGlfY2h1bmsoKToKICAgIHNlc3Npb25faWQgPSByZXF1ZXN0LmFyZ3MuZ2V0KCJzZXNzaW9uX2lkIiwgIiIpCiAgICBzZXNzaW9uID0gX2dldF9zZXNzaW9uKHNlc3Npb25faWQpCiAgICBpZiBzZXNzaW9uIGlzIE5vbmU6CiAgICAgICAgcmV0dXJuIGpzb25pZnkoeyJlcnJvciI6ICJpbnZhbGlkIHNlc3Npb25faWQifSksIDQwMAogICAgaWYgcmVxdWVzdC5taW1ldHlwZSAhPSAiYXBwbGljYXRpb24vb2N0ZXQtc3RyZWFtIjoKICAgICAgICByZXR1cm4ganNvbmlmeSh7ImVycm9yIjogImV4cGVjdCBhcHBsaWNhdGlvbi9vY3RldC1zdHJlYW0ifSksIDQwMAoKICAgIHJhdyA9IHJlcXVlc3QuZ2V0X2RhdGEoY2FjaGU9RmFsc2UpCiAgICBpZiBsZW4ocmF3KSAlIDQgIT0gMDoKICAgICAgICByZXR1cm4ganNvbmlmeSh7ImVycm9yIjogImZsb2F0MzIgYnl0ZXMgbGVuZ3RoIG5vdCBtdWx0aXBsZSBvZiA0In0pLCA0MDAKICAgIHdhdiA9IG5wLmZyb21idWZmZXIocmF3LCBkdHlwZT1ucC5mbG9hdDMyKS5yZXNoYXBlKC0xKQoKICAgIHRyeToKICAgICAgICB3aXRoIE1PREVMX0xPQ0s6CiAgICAgICAgICAgIF9mZWVkKHNlc3Npb24uc3RhdGUsIHdhdikKICAgICAgICByZXR1cm4ganNvbmlmeSgKICAgICAgICAgICAgewogICAgICAgICAgICAgICAgImxhbmd1YWdlIjogc2Vzc2lvbi5zdGF0ZS5sYW5ndWFnZSBvciAiIiwKICAgICAgICAgICAgICAgICJ0ZXh0Ijogc2Vzc2lvbi5zdGF0ZS50ZXh0IG9yICIiLAogICAgICAgICAgICAgICAgImF1ZGlvX2N1cnNvcl9zYW1wbGVzIjogaW50KHNlc3Npb24uc3RhdGUuYXVkaW9fYWNjdW0uc2l6ZSksCiAgICAgICAgICAgICAgICAiY2h1bmtfaWQiOiBpbnQoc2Vzc2lvbi5zdGF0ZS5jaHVua19pZCksCiAgICAgICAgICAgIH0KICAgICAgICApCiAgICBleGNlcHQgRXhjZXB0aW9uIGFzIGV4YzoKICAgICAgICBhcHAubG9nZ2VyLmV4Y2VwdGlvbigiUXdlbjMtQVNSIGNodW5rIGluZmVyZW5jZSBmYWlsZWQiKQogICAgICAgIHJldHVybiBqc29uaWZ5KHsiZXJyb3IiOiBzdHIoZXhjKX0pLCA1MDAKCgpAYXBwLnBvc3QoIi9hcGkvZmluaXNoIikKZGVmIGFwaV9maW5pc2goKToKICAgIHNlc3Npb25faWQgPSByZXF1ZXN0LmFyZ3MuZ2V0KCJzZXNzaW9uX2lkIiwgIiIpCiAgICBzZXNzaW9uID0gX2dldF9zZXNzaW9uKHNlc3Npb25faWQpCiAgICBpZiBzZXNzaW9uIGlzIE5vbmU6CiAgICAgICAgcmV0dXJuIGpzb25pZnkoeyJlcnJvciI6ICJpbnZhbGlkIHNlc3Npb25faWQifSksIDQwMAoKICAgIHRyeToKICAgICAgICB3aXRoIE1PREVMX0xPQ0s6CiAgICAgICAgICAgIF9maW5pc2goc2Vzc2lvbi5zdGF0ZSkKICAgICAgICBvdXRwdXQgPSB7CiAgICAgICAgICAgICJsYW5ndWFnZSI6IHNlc3Npb24uc3RhdGUubGFuZ3VhZ2Ugb3IgIiIsCiAgICAgICAgICAgICJ0ZXh0Ijogc2Vzc2lvbi5zdGF0ZS50ZXh0IG9yICIiLAogICAgICAgICAgICAiYXVkaW9fY3Vyc29yX3NhbXBsZXMiOiBpbnQoc2Vzc2lvbi5zdGF0ZS5hdWRpb19hY2N1bS5zaXplKSwKICAgICAgICAgICAgImNodW5rX2lkIjogaW50KHNlc3Npb24uc3RhdGUuY2h1bmtfaWQpLAogICAgICAgIH0KICAgIGV4Y2VwdCBFeGNlcHRpb24gYXMgZXhjOgogICAgICAgIGFwcC5sb2dnZXIuZXhjZXB0aW9uKCJRd2VuMy1BU1IgZmluaXNoIGluZmVyZW5jZSBmYWlsZWQiKQogICAgICAgIHJldHVybiBqc29uaWZ5KHsiZXJyb3IiOiBzdHIoZXhjKX0pLCA1MDAKICAgIGZpbmFsbHk6CiAgICAgICAgd2l0aCBTRVNTSU9OX0xPQ0s6CiAgICAgICAgICAgIFNFU1NJT05TLnBvcChzZXNzaW9uX2lkLCBOb25lKQogICAgcmV0dXJuIGpzb25pZnkob3V0cHV0KQoKCmRlZiBwYXJzZV9hcmdzKCk6CiAgICBwYXJzZXIgPSBhcmdwYXJzZS5Bcmd1bWVudFBhcnNlcigKICAgICAgICBkZXNjcmlwdGlvbj0iUXdlbjMtQVNSIFdpbmRvd3MgbmF0aXZlIFRyYW5zZm9ybWVycyBzdHJlYW1pbmcgY29tcGF0aWJpbGl0eSBzZXJ2ZXIiCiAgICApCiAgICBwYXJzZXIuYWRkX2FyZ3VtZW50KCItLWFzci1tb2RlbC1wYXRoIiwgZGVmYXVsdD0iUXdlbi9Rd2VuMy1BU1ItMS43QiIpCiAgICBwYXJzZXIuYWRkX2FyZ3VtZW50KCItLWhvc3QiLCBkZWZhdWx0PSIxMjcuMC4wLjEiKQogICAgcGFyc2VyLmFkZF9hcmd1bWVudCgiLS1wb3J0IiwgdHlwZT1pbnQsIGRlZmF1bHQ9ODAwMCkKICAgIHBhcnNlci5hZGRfYXJndW1lbnQoIi0tdW5maXhlZC1jaHVuay1udW0iLCB0eXBlPWludCwgZGVmYXVsdD00KQogICAgcGFyc2VyLmFkZF9hcmd1bWVudCgiLS11bmZpeGVkLXRva2VuLW51bSIsIHR5cGU9aW50LCBkZWZhdWx0PTUpCiAgICBwYXJzZXIuYWRkX2FyZ3VtZW50KCItLWNodW5rLXNpemUtc2VjIiwgdHlwZT1mbG9hdCwgZGVmYXVsdD0xLjApCiAgICBwYXJzZXIuYWRkX2FyZ3VtZW50KCItLW1heC1uZXctdG9rZW5zIiwgdHlwZT1pbnQsIGRlZmF1bHQ9MzIpCiAgICBwYXJzZXIuYWRkX2FyZ3VtZW50KCItLWR0eXBlIiwgY2hvaWNlcz1bImJmbG9hdDE2IiwgImZsb2F0MTYiXSwgZGVmYXVsdD0iYmZsb2F0MTYiKQogICAgcGFyc2VyLmFkZF9hcmd1bWVudCgiLS1hc3ItbGFuZ3VhZ2UiLCBkZWZhdWx0PSJKYXBhbmVzZSIpCiAgICByZXR1cm4gcGFyc2VyLnBhcnNlX2FyZ3MoKQoKCmRlZiBtYWluKCkgLT4gTm9uZToKICAgIGdsb2JhbCBBU1IsIFVORklYRURfQ0hVTktfTlVNLCBVTkZJWEVEX1RPS0VOX05VTSwgQ0hVTktfU0laRV9TRUMsIE1PREVMX05BTUUsIEZPUkNFX0xBTkdVQUdFCgogICAgYXJncyA9IHBhcnNlX2FyZ3MoKQogICAgaWYgbm90IHRvcmNoLmN1ZGEuaXNfYXZhaWxhYmxlKCk6CiAgICAgICAgcmFpc2UgUnVudGltZUVycm9yKAogICAgICAgICAgICAiUHlUb3JjaCBDVURBIGlzIHVuYXZhaWxhYmxlLiBJbnN0YWxsIGEgQ1VEQS1lbmFibGVkIFdpbmRvd3MgUHlUb3JjaCBidWlsZC4iCiAgICAgICAgKQoKICAgIE1PREVMX05BTUUgPSBhcmdzLmFzcl9tb2RlbF9wYXRoCiAgICBGT1JDRV9MQU5HVUFHRSA9IHN0cihhcmdzLmFzcl9sYW5ndWFnZSBvciAiSmFwYW5lc2UiKS5zdHJpcCgpIG9yICJKYXBhbmVzZSIKICAgIEZPUkNFX0xBTkdVQUdFID0gRk9SQ0VfTEFOR1VBR0VbOjFdLnVwcGVyKCkgKyBGT1JDRV9MQU5HVUFHRVsxOl0ubG93ZXIoKQogICAgVU5GSVhFRF9DSFVOS19OVU0gPSBtYXgoMCwgaW50KGFyZ3MudW5maXhlZF9jaHVua19udW0pKQogICAgVU5GSVhFRF9UT0tFTl9OVU0gPSBtYXgoMCwgaW50KGFyZ3MudW5maXhlZF90b2tlbl9udW0pKQogICAgQ0hVTktfU0laRV9TRUMgPSBtYXgoMC4xLCBmbG9hdChhcmdzLmNodW5rX3NpemVfc2VjKSkKCiAgICB0b3JjaC5iYWNrZW5kcy5jdWRhLm1hdG11bC5hbGxvd190ZjMyID0gVHJ1ZQogICAgZHR5cGUgPSB0b3JjaC5iZmxvYXQxNiBpZiBhcmdzLmR0eXBlID09ICJiZmxvYXQxNiIgZWxzZSB0b3JjaC5mbG9hdDE2CgogICAgdHJ5OgogICAgICAgIGZyb20gdHJhbnNmb3JtZXJzLnV0aWxzIGltcG9ydCBsb2dnaW5nIGFzIHRyYW5zZm9ybWVyc19sb2dnaW5nCiAgICAgICAgcHJldmlvdXNfdmVyYm9zaXR5ID0gdHJhbnNmb3JtZXJzX2xvZ2dpbmcuZ2V0X3ZlcmJvc2l0eSgpCiAgICAgICAgdHJhbnNmb3JtZXJzX2xvZ2dpbmcuc2V0X3ZlcmJvc2l0eV9lcnJvcigpCiAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgIHRyYW5zZm9ybWVyc19sb2dnaW5nID0gTm9uZQogICAgICAgIHByZXZpb3VzX3ZlcmJvc2l0eSA9IE5vbmUKICAgIHRyeToKICAgICAgICBBU1IgPSBRd2VuM0FTUk1vZGVsLmZyb21fcHJldHJhaW5lZCgKICAgICAgICAgICAgYXJncy5hc3JfbW9kZWxfcGF0aCwKICAgICAgICAgICAgZGV2aWNlX21hcD0iY3VkYTowIiwKICAgICAgICAgICAgZHR5cGU9ZHR5cGUsCiAgICAgICAgICAgIG1heF9pbmZlcmVuY2VfYmF0Y2hfc2l6ZT0xLAogICAgICAgICAgICBtYXhfbmV3X3Rva2Vucz1tYXgoMSwgaW50KGFyZ3MubWF4X25ld190b2tlbnMpKSwKICAgICAgICApCiAgICBmaW5hbGx5OgogICAgICAgIGlmIHRyYW5zZm9ybWVyc19sb2dnaW5nIGlzIG5vdCBOb25lIGFuZCBwcmV2aW91c192ZXJib3NpdHkgaXMgbm90IE5vbmU6CiAgICAgICAgICAgIHRyYW5zZm9ybWVyc19sb2dnaW5nLnNldF92ZXJib3NpdHkocHJldmlvdXNfdmVyYm9zaXR5KQogICAgQVNSLm1vZGVsLmV2YWwoKQogICAgZ2VuZXJhdGlvbl9jb25maWcgPSBnZXRhdHRyKEFTUi5tb2RlbCwgImdlbmVyYXRpb25fY29uZmlnIiwgTm9uZSkKICAgIGlmIGdlbmVyYXRpb25fY29uZmlnIGlzIG5vdCBOb25lOgogICAgICAgICMgUXdlbiBzaGlwcyB0ZW1wZXJhdHVyZSBpbiBnZW5lcmF0aW9uX2NvbmZpZyBldmVuIGZvciBncmVlZHkgZGVjb2RlOwogICAgICAgICMgVHJhbnNmb3JtZXJzIHdhcm5zIGFib3V0IGl0IG9uIGV2ZXJ5IHN0YXJ0dXAuIEdyZWVkeSBBU1IgZG9lcyBub3QgdXNlIGl0LgogICAgICAgIGdlbmVyYXRpb25fY29uZmlnLmRvX3NhbXBsZSA9IEZhbHNlCiAgICAgICAgaWYgZ2V0YXR0cihnZW5lcmF0aW9uX2NvbmZpZywgInBhZF90b2tlbl9pZCIsIE5vbmUpIGlzIE5vbmU6CiAgICAgICAgICAgIGdlbmVyYXRpb25fY29uZmlnLnBhZF90b2tlbl9pZCA9IGdldGF0dHIoZ2VuZXJhdGlvbl9jb25maWcsICJlb3NfdG9rZW5faWQiLCBOb25lKQogICAgbW9kZWxfY29uZmlnID0gZ2V0YXR0cihBU1IubW9kZWwsICJjb25maWciLCBOb25lKQogICAgaWYgbW9kZWxfY29uZmlnIGlzIG5vdCBOb25lIGFuZCBnZXRhdHRyKG1vZGVsX2NvbmZpZywgInBhZF90b2tlbl9pZCIsIE5vbmUpIGlzIE5vbmU6CiAgICAgICAgbW9kZWxfY29uZmlnLnBhZF90b2tlbl9pZCA9IGdldGF0dHIobW9kZWxfY29uZmlnLCAiZW9zX3Rva2VuX2lkIiwgTm9uZSkKCiAgICBwcmludCgKICAgICAgICBmIldpbmRvd3MgbmF0aXZlIFF3ZW4zLUFTUiByZWFkeSBvbiB7dG9yY2guY3VkYS5nZXRfZGV2aWNlX25hbWUoMCl9ICIKICAgICAgICBmImJhY2tlbmQ9dHJhbnNmb3JtZXJzX3dpbmRvd3NfY3VkYSByb2xsaW5nX2RlY29kZT1vZmZpY2lhbC1jb21wYXRpYmxlIGxhbmd1YWdlPXtGT1JDRV9MQU5HVUFHRX0iLAogICAgICAgIGZsdXNoPVRydWUsCiAgICApCiAgICAjIG1ha2Vfc2VydmVyIGF2b2lkcyBGbGFzaydzIGRldmVsb3BtZW50LXNlcnZlciB3YXJuaW5nIHdoaWxlIGtlZXBpbmcgdGhlCiAgICAjIHNpZGVjYXIgbG9jYWwtb25seSBhbmQgdGhyZWFkZWQuIE1PREVMX0xPQ0sgc3RpbGwgc2VyaWFsaXplcyBHUFUgaW5mZXJlbmNlLgogICAgc2VydmVyID0gbWFrZV9zZXJ2ZXIoYXJncy5ob3N0LCBhcmdzLnBvcnQsIGFwcCwgdGhyZWFkZWQ9VHJ1ZSkKICAgIHNlcnZlci5zZXJ2ZV9mb3JldmVyKCkKCgppZiBfX25hbWVfXyA9PSAiX19tYWluX18iOgogICAgbWFpbigpCg=='
QWEN_WINDOWS_NATIVE_SIDECAR_INTERNAL_ARG = "--qwen-native-sidecar-internal"


def _run_embedded_windows_qwen_sidecar() -> None:
    """Run the embedded Windows/CUDA Qwen3-ASR sidecar inside this child process."""
    source = base64.b64decode(QWEN_WINDOWS_NATIVE_SIDECAR_B64.encode("ascii")).decode("utf-8")
    namespace = {
        "__name__": "__main__",
        "__file__": os.path.abspath(__file__),
        "__package__": None,
        "__builtins__": __builtins__,
    }
    exec(compile(source, "<embedded-qwen3-asr-windows-sidecar>", "exec"), namespace, namespace)


class PreparedRuntimeLauncher:
    """Windows-native Qwen3-ASR launcher. Never starts WSL/Ubuntu."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.root: Optional[tk.Tk] = None
        self.status_var: Optional[tk.StringVar] = None
        self.detail_widget: Optional[tk.Text] = None
        self.sidecar_process: Optional[subprocess.Popen] = None
        self.sidecar_owned = False
        self.sidecar_reused = False
        self.sidecar_log = os.path.join(_logs_dir, "qwen-windows-sidecar.log")
        self.sidecar_script = os.path.abspath(__file__)  # single-file: child re-enters this same .pyw
        self.python_executable = ""

    @staticmethod
    def _decode_output(raw: bytes) -> str:
        if not raw:
            return ""
        for encoding in ("utf-8", "utf-8-sig", "cp932", "mbcs"):
            try:
                return raw.decode(encoding).replace("\\x00", "").strip()
            except (UnicodeDecodeError, LookupError):
                pass
        return raw.decode("utf-8", errors="replace").replace("\\x00", "").strip()

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

    def _resolve_python_executable(self) -> str:
        configured = str(getattr(self.args, "qwen_python", "") or "").strip()
        if configured:
            candidate = os.path.abspath(os.path.expanduser(configured))
            if not os.path.isfile(candidate):
                raise RuntimeError(f"指定的 Windows Python 不存在：{candidate}")
            return candidate

        local_venv = os.path.join(_source_dir, ".venv", "Scripts", "python.exe")
        if os.path.isfile(local_venv):
            return local_venv

        current = os.path.abspath(sys.executable)
        sibling = os.path.join(os.path.dirname(current), "python.exe")
        return sibling if os.path.isfile(sibling) else current

    def _open_ui(self) -> None:
        root = tk.Tk()
        root.title("Qwen3 实时字幕 - Windows 原生 ASR")
        root.geometry("700x300")
        root.resizable(True, True)
        self.status_var = tk.StringVar(value="正在启动 Windows 原生 Qwen3-ASR...")
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

    def _pump_ui(self) -> None:
        if self.root is None:
            return
        try:
            self.root.update_idletasks()
            self.root.update()
        except tk.TclError:
            self.root = None

    def _status(self, text: str) -> None:
        logger.info("[WINDOWS_RUNTIME] %s", text)
        if self.status_var is not None:
            self.status_var.set(text)
        if self.detail_widget is not None:
            try:
                self.detail_widget.config(state="normal")
                self.detail_widget.insert("end", str(text).rstrip() + "\\n")
                self.detail_widget.see("end")
                self.detail_widget.config(state="disabled")
            except tk.TclError:
                pass
        self._pump_ui()

    def _run_capture(self, command: List[str], *, timeout: float = 30.0, env: Optional[Dict[str, str]] = None) -> tuple[int, str]:
        try:
            completed = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=timeout, env=env, **self._hidden_kwargs()
            )
            return int(completed.returncode), self._decode_output(completed.stdout or b"")
        except subprocess.TimeoutExpired as exc:
            return 124, self._decode_output(exc.stdout or b"") + "\\n命令执行超时"
        except Exception as exc:
            return 127, str(exc)

    @staticmethod
    def _tail_file(path: str, max_lines: int = 40) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return "".join(deque(handle, maxlen=max_lines)).strip()
        except OSError:
            return ""

    def _api_url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        url = self.args.qwen_server_url.rstrip("/") + path
        if params:
            query = urllib.parse.urlencode(params)
            if query:
                url += "?" + query
        return url

    def _probe_protocol(self, timeout: float = 1.2) -> tuple[bool, Dict[str, Any], str]:
        health: Dict[str, Any] = {}
        try:
            try:
                req = urllib.request.Request(self._api_url("/health"), headers={"User-Agent": "Qwen3Subtitle/windows"})
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if isinstance(payload, dict):
                    health = payload
            except Exception:
                pass

            start_req = urllib.request.Request(
                self._api_url("/api/start", {"language": str(getattr(self.args, "asr_language", "") or QWEN3_ASR_DEFAULT_LANGUAGE)}),
                data=b"", method="POST"
            )
            with urllib.request.urlopen(start_req, timeout=timeout) as response:
                started = json.loads(response.read().decode("utf-8"))
            session_id = str(started.get("session_id", "") or "")
            if not session_id:
                return False, health, "/api/start 未返回 session_id"

            finish_req = urllib.request.Request(
                self._api_url("/api/finish", {"session_id": session_id}), data=b"", method="POST"
            )
            with urllib.request.urlopen(finish_req, timeout=timeout) as response:
                finished = json.loads(response.read().decode("utf-8"))
            if not isinstance(finished, dict) or finished.get("error"):
                return False, health, "/api/finish 失败"
            return True, health, ""
        except Exception as exc:
            return False, health, str(exc)

    def _native_gpu_inventory(self) -> List[Dict[str, Any]]:
        code, out = self._run_capture([
            "nvidia-smi", "--query-gpu=index,name,memory.total,memory.free", "--format=csv,noheader,nounits"
        ], timeout=15)
        if code != 0:
            return []
        items: List[Dict[str, Any]] = []
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",", 3)]
            if len(parts) != 4:
                continue
            try:
                items.append({
                    "index": int(parts[0]), "name": parts[1],
                    "total_mib": int(float(parts[2])), "free_mib": int(float(parts[3]))
                })
            except ValueError:
                continue
        return items

    def _resolve_gpu(self) -> tuple[str, Dict[str, str], Dict[str, Any]]:
        requested = str(getattr(self.args, "qwen_cuda_visible_devices", QWEN3_ASR_RTX3090_GPU_INDEX) or "0").strip()
        inventory = self._native_gpu_inventory()
        candidates: List[str] = []
        if requested:
            candidates.append(requested)
        for item in inventory:
            if "3090" in str(item.get("name", "")).lower():
                value = str(item["index"])
                if value not in candidates:
                    candidates.append(value)
        if not candidates:
            candidates = ["0"]

        failures: List[str] = []
        for selected in candidates:
            env = os.environ.copy()
            env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
            env["CUDA_VISIBLE_DEVICES"] = selected
            probe = (
                "import json,torch; "
                "assert torch.cuda.is_available(), 'torch.cuda.is_available() is False'; "
                "p=torch.cuda.get_device_properties(0); f,t=torch.cuda.mem_get_info(0); "
                "print(json.dumps({'name':p.name,'total':int(t//1048576),'free':int(f//1048576)}))"
            )
            code, out = self._run_capture([self.python_executable, "-c", probe], timeout=60, env=env)
            if code != 0:
                failures.append(f"GPU {selected}: {out[-800:]}")
                continue
            try:
                info = json.loads(out.splitlines()[-1])
            except Exception:
                failures.append(f"GPU {selected}: CUDA 探测结果无法解析")
                continue
            if "3090" not in str(info.get("name", "")).lower():
                failures.append(f"GPU {selected}: {info.get('name', 'unknown')} 不是 RTX 3090")
                continue
            return selected, env, info

        raise RuntimeError("Windows CUDA 无法验证 RTX 3090。\\n\\n" + "\\n".join(failures[-8:]))

    def _verify_python_environment(self, env: Dict[str, str]) -> None:
        # Keep startup validation deliberately lightweight.  Importing qwen_asr /
        # transformers on Windows can take a long time on the first run and should
        # not be misclassified as a missing dependency.  The sidecar startup phase
        # below already has a much larger timeout and reports the real import/model
        # loading error through qwen-windows-sidecar.log.
        verify = (
            "import importlib.util, json, torch; "
            "mods=['flask','numpy','qwen_asr']; "
            "missing=[m for m in mods if importlib.util.find_spec(m) is None]; "
            "assert not missing, 'missing modules: ' + ', '.join(missing); "
            "assert torch.cuda.is_available(), 'torch.cuda.is_available() is False'; "
            "print(json.dumps({'torch':torch.__version__,"
            "'gpu':torch.cuda.get_device_name(0),'modules':'ok'}))"
        )
        code, out = self._run_capture(
            [self.python_executable, "-c", verify],
            timeout=120,
            env=env,
        )
        if code == 124:
            raise RuntimeError(
                "Windows Python/CUDA 轻量预检仍然超时。\n\n"
                f"Python: {self.python_executable}\n\n"
                "请在 PowerShell 执行：\n"
                f'  "{self.python_executable}" -c "import torch; '
                "print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0))\"\n\n"
                + out[-2500:]
            )
        if code != 0:
            raise RuntimeError(
                "Windows Python 环境检查失败。\n\n"
                f"Python: {self.python_executable}\n\n"
                "需要 torch(CUDA)、numpy、flask、qwen-asr。\n\n"
                + out[-3500:]
            )

    def _start_sidecar(self) -> bool:
        healthy, payload, _ = self._probe_protocol(timeout=1.0)
        if healthy:
            self.sidecar_reused = True
            self.sidecar_owned = False
            self._status("复用已运行 Qwen 服务：" + str(payload.get("backend") or payload.get("protocol") or "compatible"))
            return True

        parsed = urllib.parse.urlparse(self.args.qwen_server_url)
        host = (parsed.hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            messagebox.showerror("远程 Qwen 服务不可用", "Windows 原生自动启动只支持本机地址。", parent=self.root)
            return False
        if bool(getattr(self.args, "no_qwen_auto_start", False)):
            return False

        try:
            selected, env, info = self._resolve_gpu()
            self.args.qwen_cuda_visible_devices = selected
            self._status(
                f"Windows CUDA GPU={selected} → {info.get('name')} total={info.get('total')}MiB free={info.get('free')}MiB"
            )
            self._verify_python_environment(env)
        except Exception as exc:
            messagebox.showerror("Windows 原生 Qwen 环境错误", str(exc), parent=self.root)
            return False

        hf_home = str(getattr(self.args, "qwen_hf_home", "") or "").strip()
        if hf_home:
            env["HF_HOME"] = os.path.abspath(os.path.expanduser(hf_home))
        env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

        model = str(self.args.asr_model or QWEN3_ASR_MODEL_ID).strip()
        expanded = os.path.expanduser(model)
        if os.path.isabs(expanded):
            model = os.path.abspath(expanded)
            if not os.path.exists(model):
                messagebox.showerror("ASR 模型路径错误", f"本地模型不存在：{model}", parent=self.root)
                return False

        port = int(parsed.port or 8000)
        command = [
            self.python_executable, self.sidecar_script,
            QWEN_WINDOWS_NATIVE_SIDECAR_INTERNAL_ARG,
            "--asr-model-path", model,
            "--host", "127.0.0.1",
            "--port", str(port),
            "--unfixed-chunk-num", str(int(self.args.qwen_unfixed_chunk_num)),
            "--unfixed-token-num", str(int(self.args.qwen_unfixed_token_num)),
            "--chunk-size-sec", str(float(self.args.qwen_chunk_size_sec)),
            "--max-new-tokens", str(max(1, int(getattr(self.args, "qwen_max_new_tokens", 32)))),
            "--dtype", str(getattr(self.args, "qwen_transformers_dtype", "bfloat16")),
            "--asr-language", str(getattr(self.args, "asr_language", "") or QWEN3_ASR_DEFAULT_LANGUAGE),
        ]

        os.makedirs(os.path.dirname(self.sidecar_log), exist_ok=True)
        try:
            with open(self.sidecar_log, "w", encoding="utf-8", errors="replace") as log_handle:
                log_handle.write("===== Windows native Qwen3-ASR Transformers sidecar =====\\n")
                log_handle.flush()
                self.sidecar_process = subprocess.Popen(
                    command, stdout=log_handle, stderr=subprocess.STDOUT, env=env, **self._hidden_kwargs()
                )
        except Exception as exc:
            messagebox.showerror("Windows 原生 Qwen 服务启动失败", str(exc), parent=self.root)
            return False

        self.sidecar_owned = True
        timeout = max(30.0, float(getattr(self.args, "qwen_startup_timeout", 900.0)))
        deadline = time.monotonic() + timeout
        last_tail = ""
        while time.monotonic() < deadline:
            exit_code = self.sidecar_process.poll() if self.sidecar_process is not None else None
            if exit_code is not None:
                tail = self._tail_file(self.sidecar_log, 80)
                messagebox.showerror(
                    "Windows 原生 Qwen 服务异常退出",
                    f"exit code={exit_code}\\n\\n{tail[-7000:]}\\n\\n日志：{self.sidecar_log}",
                    parent=self.root,
                )
                return False
            healthy, payload, _ = self._probe_protocol(timeout=1.0)
            if healthy:
                self._status("Windows 原生 Qwen3-ASR 已就绪：" + str(payload.get("backend") or "transformers"))
                return True
            tail = self._tail_file(self.sidecar_log, 8)
            if tail and tail != last_tail:
                last_tail = tail
                self._status("模型加载中：" + tail.splitlines()[-1][:180])
            self._pump_ui()
            time.sleep(0.5)

        messagebox.showerror(
            "Windows 原生 Qwen 服务启动超时",
            self._tail_file(self.sidecar_log, 60)[-6000:],
            parent=self.root,
        )
        return False

    def start(self) -> bool:
        if not sys.platform.startswith("win"):
            messagebox.showerror("仅支持 Windows", "这个版本已改为 Windows 原生运行。")
            return False

        healthy, payload, _ = self._probe_protocol(timeout=0.8)
        if healthy:
            self.sidecar_reused = True
            self.sidecar_owned = False
            return True

        vad_path = os.path.abspath(os.path.expanduser(str(self.args.vad_model_path)))
        if not os.path.isfile(vad_path):
            messagebox.showerror("TEN-VAD 未准备", f"未找到：{vad_path}")
            return False

        try:
            self.python_executable = self._resolve_python_executable()
        except Exception as exc:
            messagebox.showerror("Windows Python 配置错误", str(exc))
            return False

        self._open_ui()
        try:
            self._status(
                f"Windows 原生模式：Python={self.python_executable}；Qwen3-ASR Transformers + CUDA；不启动 WSL"
            )
            return self._start_sidecar()
        finally:
            if self.root is not None:
                try:
                    self.root.destroy()
                except tk.TclError:
                    pass
                self.root = None

    def close(self) -> None:
        if bool(getattr(self.args, "keep_qwen_server", False)):
            return
        if self.sidecar_reused and not self.sidecar_owned:
            return
        process = self.sidecar_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        self.sidecar_process = None
        self.sidecar_owned = False



def ensure_first_run_assets(root: tk.Tk, args: argparse.Namespace) -> bool:
    """Validate local desktop assets; Qwen weights are loaded by the Windows sidecar."""
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


@dataclass(frozen=True)
class TranslationRequest:
    session_id: str
    utterance_id: int
    source_revision: int
    source_text: str
    is_final: bool
    request_id: int
    submitted_at: float
    context_texts: tuple[str, ...] = ()


@dataclass(frozen=True)
class TranslationResult:
    session_id: str
    utterance_id: int
    source_revision: int
    source_text: str
    translated_text: str
    is_final: bool
    request_id: int
    latency_ms: int
    backend: str
    error: str = ""
    context_texts: tuple[str, ...] = ()


class TranslationBackend:
    name = "base"

    def translate(self, request: TranslationRequest, timeout: float) -> str:
        raise NotImplementedError

    def close(self) -> None:
        return None


class CloudflareWorkersAITranslationBackend(TranslationBackend):
    """Cloudflare Workers AI translation backend using the official REST example.

    Official request shape:
      POST https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run/@cf/meta/m2m100-1.2b
      Authorization: Bearer {API_TOKEN}
      JSON: {"text": ..., "source_lang": ..., "target_lang": ...}

    The Cloudflare API envelope is expected to contain success/result/errors/messages,
    with the translation in result.translated_text. No alternate translation provider, fallback,
    alternate endpoint, retry, prompt wrapper, or context is used here.
    """

    name = "cloudflare"
    _MODEL = CLOUDFLARE_WORKERS_AI_MODEL
    _API_BASE_URL = CLOUDFLARE_API_BASE_URL

    def __init__(
        self,
        *,
        account_id: str,
        api_token: str,
        source_language: str = TRANSLATION_DEFAULT_SOURCE,
        target_language: str = TRANSLATION_DEFAULT_TARGET,
    ):
        self.account_id = str(account_id or "").strip()
        self.api_token = str(api_token or "").strip()
        self.source_language = str(source_language or TRANSLATION_DEFAULT_SOURCE).strip()
        self.target_language = str(target_language or TRANSLATION_DEFAULT_TARGET).strip()
        self.last_endpoint = "cloudflare/m2m100-1.2b"
        self.last_metrics: Dict[str, int] = {}
        self._reset_last_metrics()

    def _reset_last_metrics(self) -> None:
        self.last_metrics = {
            "cloudflare_success": 0,
            "cloudflare_timeout": 0,
            "cloudflare_http_error": 0,
            "cloudflare_api_error": 0,
            "cloudflare_parse_error": 0,
            "cloudflare_network_error": 0,
            "cloudflare_rate_limited": 0,
        }

    def close(self) -> None:
        return None

    def _endpoint(self) -> str:
        if not self.account_id:
            raise RuntimeError(
                "[ERROR] Cloudflare Account ID 未配置；请设置 CLOUDFLARE_ACCOUNT_ID "
                "或 --cloudflare-account-id"
            )
        return f"{self._API_BASE_URL}/{self.account_id}/ai/run/{self._MODEL}"

    def translate(self, request: TranslationRequest, timeout: float) -> str:
        self._reset_last_metrics()
        self.last_endpoint = "cloudflare/m2m100-1.2b"
        if not self.api_token:
            raise RuntimeError(
                "[ERROR] Cloudflare API Token 未配置；请设置 CLOUDFLARE_API_TOKEN "
                "或 --cloudflare-api-token"
            )

        endpoint = self._endpoint()
        headers = {"Authorization": f"Bearer {self.api_token}"}
        payload = {
            "text": request.source_text,
            "source_lang": self.source_language,
            "target_lang": self.target_language,
        }

        try:
            # Cloudflare's official Python example uses requests.post(..., headers=..., json=...).
            response = requests.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=max(0.2, float(timeout)),
            )
        except requests.Timeout as exc:
            self.last_metrics["cloudflare_timeout"] += 1
            raise RuntimeError("[ERROR] Cloudflare Workers AI request timed out") from exc
        except requests.RequestException as exc:
            self.last_metrics["cloudflare_network_error"] += 1
            raise RuntimeError(f"[ERROR] Cloudflare Workers AI network error: {exc}") from exc
        except Exception as exc:
            self.last_metrics["cloudflare_network_error"] += 1
            raise RuntimeError(f"[ERROR] Cloudflare Workers AI network error: {exc}") from exc

        status = int(response.status_code)
        try:
            body = response.json()
        except Exception as exc:
            self.last_metrics["cloudflare_parse_error"] += 1
            if status == 429:
                self.last_metrics["cloudflare_rate_limited"] += 1
            if not (200 <= status < 300):
                self.last_metrics["cloudflare_http_error"] += 1
            raise RuntimeError(
                f"[ERROR] Cloudflare Workers AI returned HTTP {status} with invalid JSON"
            ) from exc

        errors = body.get("errors", []) if isinstance(body, dict) else []
        error_text = ""
        if isinstance(errors, list) and errors:
            parts = []
            for item in errors:
                if isinstance(item, dict):
                    code = item.get("code")
                    message = item.get("message")
                    if code is not None and message:
                        parts.append(f"{code}: {message}")
                    elif message:
                        parts.append(str(message))
                    elif code is not None:
                        parts.append(str(code))
                else:
                    parts.append(str(item))
            error_text = "; ".join(part for part in parts if part)

        if not (200 <= status < 300):
            self.last_metrics["cloudflare_http_error"] += 1
            if status == 429:
                self.last_metrics["cloudflare_rate_limited"] += 1
            suffix = f" - {error_text}" if error_text else ""
            raise RuntimeError(f"[ERROR] Cloudflare Workers AI HTTP {status}{suffix}")

        if not isinstance(body, dict):
            self.last_metrics["cloudflare_parse_error"] += 1
            raise RuntimeError("[ERROR] Cloudflare Workers AI response is not a JSON object")

        if body.get("success") is not True:
            self.last_metrics["cloudflare_api_error"] += 1
            suffix = f": {error_text}" if error_text else ""
            raise RuntimeError(f"[ERROR] Cloudflare Workers AI API reported failure{suffix}")

        result = body.get("result")
        if not isinstance(result, dict):
            self.last_metrics["cloudflare_parse_error"] += 1
            raise RuntimeError("[ERROR] Cloudflare Workers AI result is missing")
        translated_text = result.get("translated_text")
        if not isinstance(translated_text, str) or not translated_text.strip():
            self.last_metrics["cloudflare_parse_error"] += 1
            raise RuntimeError("[ERROR] Cloudflare Workers AI result.translated_text is missing")

        self.last_metrics["cloudflare_success"] += 1
        return translated_text


class TranslationCoordinator:
    """Cloudflare Workers AI realtime translation pipeline.

    Preview traffic uses a bounded speculative pool: at most two Cloudflare
    preview requests may be in flight and exactly one newest pending revision is
    retained under backpressure. Normal dispatch cadence is 250ms and is not slowed
    merely because Cloudflare latency is ~0.6-1.0s. Returned previews remain
    prefix-safe progressive with monotonic source coverage. Final traffic is
    an authoritative independent FIFO: every submitted final result is delivered
    exactly once (success or failure) and can never be invalidated by a preview.
    """

    def __init__(
        self,
        *,
        session_id: str,
        result_callback: Callable[[TranslationResult], None],
        enabled: bool = True,
        source_language: str = TRANSLATION_DEFAULT_SOURCE,
        target_language: str = TRANSLATION_DEFAULT_TARGET,
        cloudflare_account_id: str = "",
        cloudflare_api_token: str = "",
        preview_interval_ms: int = TRANSLATION_DEFAULT_PREVIEW_INTERVAL_MS,
        api_interval: int = TRANSLATION_DEFAULT_API_INTERVAL,
        min_chars: int = TRANSLATION_DEFAULT_MIN_CHARS,
        min_delta_chars: int = TRANSLATION_DEFAULT_MIN_DELTA_CHARS,
        preview_timeout: float = TRANSLATION_DEFAULT_PREVIEW_TIMEOUT,
        final_timeout: float = TRANSLATION_DEFAULT_FINAL_TIMEOUT,
        backend_factory: Optional[Callable[[Dict[str, Any]], TranslationBackend]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.session_id = str(session_id)
        self.result_callback = result_callback
        self.clock = clock
        self._backend_factory = backend_factory
        self._backend_local = local()
        self._backend_generation = 0
        self._condition = Condition(Lock())
        self._accepting = True
        self._stop_preview = False
        self._stop_final_when_empty = False
        self._drop_finals = False
        self._preview_slot: Optional[TranslationRequest] = None
        self._final_queue: deque[TranslationRequest] = deque()
        self._request_seq = 0
        self._latest_preview_requested_id = 0
        self._last_preview_source: Dict[int, str] = {}
        self._last_preview_at: Dict[int, float] = {}
        # Latest ASR text is tracked even when a revision is too small to dispatch.
        # Returned previews may be shown when their source is still an exact prefix
        # of this latest text.
        self._latest_source_by_utterance: Dict[int, str] = {}
        self._latest_revision_by_utterance: Dict[int, int] = {}
        self._published_preview_source: Dict[int, str] = {}
        self._published_preview_coverage: Dict[int, int] = {}
        self._published_preview_revision: Dict[int, int] = {}
        # v0.9.36 bounded speculative preview scheduler.
        # Two workers may translate concurrently; all excess revisions collapse
        # into the single _preview_slot (latest-only backpressure).
        self._last_preview_dispatched_at: float = 0.0
        self._preview_latency_ewma_ms: float = 0.0
        self._effective_preview_debounce_ms: int = int(TRANSLATION_DEFAULT_PREVIEW_INTERVAL_MS)
        self._preview_inflight: int = 0
        self._preview_timeout_streak: int = 0
        self._preview_recovery_success_streak: int = 0
        self._last_final_key: set[tuple[int, int, str]] = set()
        self._finalized_utterances: set[int] = set()
        self._translation_cache: OrderedDict[tuple[tuple[str, ...], str], str] = OrderedDict()
        self._stats: Dict[str, int] = {
            "submitted_previews": 0,
            "submitted_finals": 0,
            "coalesced_previews": 0,
            "stale_results": 0,
            "prefix_safe_previews": 0,
            "preview_branch_resets": 0,
            "preview_coverage_rejections": 0,
            "translation_errors": 0,
            "completed_previews": 0,
            "completed_finals": 0,
            "published_previews": 0,
            "published_finals": 0,
            "dispatched_previews": 0,
            "max_preview_inflight": 0,
            "preview_backoff_events": 0,
            "preview_recovery_events": 0,
            "cache_hits": 0,
            "max_latency_ms": 0,
            "cloudflare_success": 0,
            "cloudflare_timeout": 0,
            "cloudflare_http_error": 0,
            "cloudflare_api_error": 0,
            "cloudflare_parse_error": 0,
            "cloudflare_network_error": 0,
            "cloudflare_rate_limited": 0,
        }
        self._config: Dict[str, Any] = {}
        self.configure(
            enabled=enabled,
            source_language=source_language,
            target_language=target_language,
            cloudflare_account_id=cloudflare_account_id,
            cloudflare_api_token=cloudflare_api_token,
            preview_interval_ms=preview_interval_ms,
            api_interval=api_interval,
            min_chars=min_chars,
            min_delta_chars=min_delta_chars,
            preview_timeout=preview_timeout,
            final_timeout=final_timeout,
        )
        self._preview_threads = [
            Thread(
                target=self._preview_loop,
                name=f"TranslationPreview-{index + 1}",
                daemon=False,
            )
            for index in range(TRANSLATION_PREVIEW_CONCURRENCY)
        ]
        # Compatibility alias for older diagnostics/tests; close() uses the full pool.
        self._preview_thread = self._preview_threads[0]
        self._final_thread = Thread(target=self._final_loop, name="TranslationFinal", daemon=False)
        for thread in self._preview_threads:
            thread.start()
        self._final_thread.start()

    def configure(self, **values: Any) -> None:
        allowed = {
            "enabled", "source_language", "target_language",
            "cloudflare_account_id", "cloudflare_api_token",
            "preview_interval_ms", "api_interval", "min_chars",
            "min_delta_chars", "preview_timeout", "final_timeout",
        }
        values = {key: value for key, value in values.items() if key in allowed}
        with self._condition:
            previous_backend_identity = (
                self._config.get("source_language"),
                self._config.get("target_language"),
                self._config.get("cloudflare_account_id"),
                self._config.get("cloudflare_api_token"),
            )
            current = dict(self._config)
            current.update(values)
            current.setdefault("enabled", True)
            current.setdefault("source_language", TRANSLATION_DEFAULT_SOURCE)
            current.setdefault("target_language", TRANSLATION_DEFAULT_TARGET)
            current.setdefault("cloudflare_account_id", "")
            current.setdefault("cloudflare_api_token", "")
            current.setdefault("preview_interval_ms", TRANSLATION_DEFAULT_PREVIEW_INTERVAL_MS)
            current.setdefault("api_interval", TRANSLATION_DEFAULT_API_INTERVAL)
            current.setdefault("min_chars", TRANSLATION_DEFAULT_MIN_CHARS)
            current.setdefault("min_delta_chars", TRANSLATION_DEFAULT_MIN_DELTA_CHARS)
            current.setdefault("preview_timeout", TRANSLATION_DEFAULT_PREVIEW_TIMEOUT)
            current.setdefault("final_timeout", TRANSLATION_DEFAULT_FINAL_TIMEOUT)
            current["enabled"] = bool(current["enabled"])
            current["source_language"] = str(current["source_language"] or TRANSLATION_DEFAULT_SOURCE).strip()
            current["target_language"] = str(current["target_language"] or TRANSLATION_DEFAULT_TARGET).strip()
            current["cloudflare_account_id"] = str(current["cloudflare_account_id"] or "").strip()
            current["cloudflare_api_token"] = str(current["cloudflare_api_token"] or "").strip()
            current["preview_interval_ms"] = max(100, min(5000, int(current["preview_interval_ms"])))
            current["api_interval"] = max(0, min(10, int(current["api_interval"])))  # deprecated; scheduler ignores this
            current["min_chars"] = max(1, min(100, int(current["min_chars"])))
            current["min_delta_chars"] = max(1, min(100, int(current["min_delta_chars"])))
            current["preview_timeout"] = max(0.2, min(30.0, float(current["preview_timeout"])))
            current["final_timeout"] = max(0.2, min(60.0, float(current["final_timeout"])))
            self._config = current
            base_preview_ms = int(current["preview_interval_ms"])
            if self._effective_preview_debounce_ms < base_preview_ms:
                self._effective_preview_debounce_ms = base_preview_ms
            current_backend_identity = (
                current.get("source_language"),
                current.get("target_language"),
                current.get("cloudflare_account_id"),
                current.get("cloudflare_api_token"),
            )
            if current_backend_identity != previous_backend_identity:
                self._backend_generation += 1
                self._translation_cache.clear()
            if not current["enabled"]:
                self._preview_slot = None
            self._condition.notify_all()

    def config_snapshot(self, *, include_secret: bool = False) -> Dict[str, Any]:
        with self._condition:
            snapshot = dict(self._config)
        if not include_secret:
            snapshot.pop("cloudflare_api_token", None)
        return snapshot

    def _make_backend(self) -> TranslationBackend:
        with self._condition:
            config = dict(self._config)
        if self._backend_factory is not None:
            return self._backend_factory(config)
        return CloudflareWorkersAITranslationBackend(
            account_id=config.get("cloudflare_account_id", ""),
            api_token=config.get("cloudflare_api_token", ""),
            source_language=config.get("source_language", TRANSLATION_DEFAULT_SOURCE),
            target_language=config.get("target_language", TRANSLATION_DEFAULT_TARGET),
        )

    def _thread_backend(self) -> TranslationBackend:
        generation = int(self._backend_generation)
        backend = getattr(self._backend_local, "backend", None)
        backend_generation = getattr(self._backend_local, "generation", None)
        if backend is None or backend_generation != generation:
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    logger.debug("Cloudflare 翻译 backend 关闭失败", exc_info=True)
            backend = self._make_backend()
            self._backend_local.backend = backend
            self._backend_local.generation = generation
        return backend

    def _close_thread_backend(self) -> None:
        backend = getattr(self._backend_local, "backend", None)
        self._backend_local.backend = None
        if backend is not None:
            try:
                backend.close()
            except Exception:
                logger.debug("Cloudflare 翻译 backend 关闭失败", exc_info=True)

    def _new_request(self, event: TranscriptEvent, source_text: str) -> TranslationRequest:
        self._request_seq += 1
        request = TranslationRequest(
            session_id=event.session_id,
            utterance_id=int(event.utterance_id),
            source_revision=int(event.revision),
            source_text=source_text,
            is_final=bool(event.is_final),
            request_id=self._request_seq,
            submitted_at=self.clock(),
            context_texts=(),
        )
        return request

    @staticmethod
    def _cache_key(request: TranslationRequest) -> tuple[tuple[str, ...], str]:
        return (tuple(request.context_texts), request.source_text)

    @staticmethod
    def _meaningful_delta(previous: str, current: str) -> int:
        if not previous:
            return len(current)
        common = 0
        for old_char, new_char in zip(previous, current):
            if old_char != new_char:
                break
            common += 1
        return max(len(current) - common, len(previous) - common)

    def submit(self, event: TranscriptEvent) -> Optional[int]:
        if event.session_id != self.session_id:
            return None
        source = normalize_subtitle_text(event.text)
        if not source:
            return None
        with self._condition:
            if not self._accepting or not self._config.get("enabled", True):
                return None
            utterance_id = int(event.utterance_id)
            event_revision = int(event.revision)
            previous_latest_revision = self._latest_revision_by_utterance.get(utterance_id, 0)
            if event_revision >= previous_latest_revision:
                self._latest_source_by_utterance[utterance_id] = source
                self._latest_revision_by_utterance[utterance_id] = event_revision

                # Monotonic coverage applies only while ASR extends the same text
                # branch. If ASR rewrites/retracts the already displayed prefix,
                # reset the baseline so a corrected (even shorter) branch can replace it.
                published_source = self._published_preview_source.get(utterance_id, "")
                if published_source and not source.startswith(published_source):
                    self._published_preview_source.pop(utterance_id, None)
                    self._published_preview_coverage.pop(utterance_id, None)
                    self._published_preview_revision.pop(utterance_id, None)
                    self._stats["preview_branch_resets"] += 1

            if event.is_final:
                final_key = (utterance_id, int(event.revision), source)
                if final_key in self._last_final_key:
                    return None
                self._last_final_key.add(final_key)
                if len(self._last_final_key) > 2048:
                    self._last_final_key = set(list(self._last_final_key)[-1024:])
                request = self._new_request(event, source)
                self._finalized_utterances.add(utterance_id)
                if self._preview_slot is not None and self._preview_slot.utterance_id == utterance_id:
                    self._preview_slot = None
                    self._stats["coalesced_previews"] += 1
                self._final_queue.append(request)
                self._stats["submitted_finals"] += 1
                self._last_preview_source[utterance_id] = source
                self._last_preview_at[utterance_id] = request.submitted_at
                self._condition.notify_all()
                return request.request_id

            if utterance_id in self._finalized_utterances:
                return None

            # Fast realtime preview policy:
            # - first useful partial can translate immediately;
            # - subsequent partials need a meaningful 3-char delta by default;
            # - EOS punctuation bypasses delta gating;
            # - timing/debounce happens in _preview_loop so revisions arriving
            #   during the debounce window simply replace the one pending slot.
            previous = self._last_preview_source.get(utterance_id, "")
            if previous == source:
                return None
            punctuated = source.endswith((".", "?", "!", "。", "？", "！"))
            if not punctuated:
                if len(source) < int(self._config["min_chars"]):
                    return None
                if self._meaningful_delta(previous, source) < int(self._config["min_delta_chars"]):
                    return None

            now = self.clock()
            request = self._new_request(event, source)
            self._latest_preview_requested_id = request.request_id
            if self._preview_slot is not None:
                self._stats["coalesced_previews"] += 1
            self._preview_slot = request
            self._last_preview_source[utterance_id] = source
            self._last_preview_at[utterance_id] = now
            self._stats["submitted_previews"] += 1
            self._condition.notify_all()
            return request.request_id

    def _merge_backend_metrics(self, backend: Optional[TranslationBackend]) -> None:
        metrics = getattr(backend, "last_metrics", None) if backend is not None else None
        if not isinstance(metrics, dict):
            return
        with self._condition:
            for key in (
                "cloudflare_success", "cloudflare_timeout", "cloudflare_http_error",
                "cloudflare_api_error", "cloudflare_parse_error", "cloudflare_network_error",
                "cloudflare_rate_limited",
            ):
                self._stats[key] += int(metrics.get(key, 0) or 0)

    def _translate_one(self, request: TranslationRequest) -> TranslationResult:
        started = self.clock()
        cache_key = self._cache_key(request)
        backend_label = "cloudflare/cache"
        with self._condition:
            timeout = float(self._config["final_timeout"] if request.is_final else self._config["preview_timeout"])
            cached = self._translation_cache.get(cache_key)
            if cached is not None:
                self._translation_cache.move_to_end(cache_key)
                self._stats["cache_hits"] += 1
        if cached is not None:
            translated = cached
            error = ""
        else:
            backend: Optional[TranslationBackend] = None
            try:
                backend = self._thread_backend()
                translated = backend.translate(request, timeout)
                backend_label = str(getattr(backend, "last_endpoint", "cloudflare"))
                error = ""
            except Exception as exc:
                translated = ""
                backend_label = "cloudflare/error"
                error = str(exc)
                logger.warning(
                    "[TRANSLATION_ERROR] request=%d utterance=%d final=%s backend=cloudflare error=%s",
                    request.request_id, request.utterance_id, request.is_final, error,
                )
            finally:
                self._merge_backend_metrics(backend)
        latency_ms = max(0, int(round((self.clock() - started) * 1000.0)))
        return TranslationResult(
            session_id=request.session_id,
            utterance_id=request.utterance_id,
            source_revision=request.source_revision,
            source_text=request.source_text,
            translated_text=translated,
            is_final=request.is_final,
            request_id=request.request_id,
            latency_ms=latency_ms,
            backend=backend_label,
            error=error,
            context_texts=request.context_texts,
        )

    def _update_preview_backoff(self, result: TranslationResult) -> None:
        """Update preview pacing from failures only.

        Normal Cloudflare latency is observed with EWMA but never raises debounce.
        Two consecutive preview timeouts enter 500ms backoff. HTTP 429/rate-limit
        enters 1000ms backoff immediately. Three consecutive successful previews
        recover to the configured base cadence (250ms by default).
        """
        if result.is_final:
            return
        with self._condition:
            base_ms = int(self._config.get(
                "preview_interval_ms", TRANSLATION_DEFAULT_PREVIEW_INTERVAL_MS
            ))

            if not result.error:
                latency = max(0.0, float(result.latency_ms))
                if self._preview_latency_ewma_ms <= 0.0:
                    self._preview_latency_ewma_ms = latency
                else:
                    alpha = float(TRANSLATION_PREVIEW_EWMA_ALPHA)
                    self._preview_latency_ewma_ms = (
                        (1.0 - alpha) * self._preview_latency_ewma_ms + alpha * latency
                    )

                self._preview_timeout_streak = 0
                if self._effective_preview_debounce_ms > base_ms:
                    self._preview_recovery_success_streak += 1
                    if (
                        self._preview_recovery_success_streak
                        >= TRANSLATION_PREVIEW_RECOVERY_SUCCESSES
                    ):
                        self._effective_preview_debounce_ms = base_ms
                        self._preview_recovery_success_streak = 0
                        self._stats["preview_recovery_events"] += 1
                else:
                    self._effective_preview_debounce_ms = base_ms
                    self._preview_recovery_success_streak = 0
            else:
                error_text = str(result.error).lower()
                self._preview_recovery_success_streak = 0

                if "429" in error_text or "rate" in error_text:
                    target_ms = max(base_ms, TRANSLATION_PREVIEW_RATE_LIMIT_BACKOFF_MS)
                    if self._effective_preview_debounce_ms != target_ms:
                        self._stats["preview_backoff_events"] += 1
                    self._effective_preview_debounce_ms = target_ms
                    self._preview_timeout_streak = 0
                elif "timeout" in error_text or "timed out" in error_text:
                    self._preview_timeout_streak += 1
                    if (
                        self._preview_timeout_streak
                        >= TRANSLATION_PREVIEW_TIMEOUT_STREAK_TRIGGER
                    ):
                        target_ms = max(base_ms, TRANSLATION_PREVIEW_TIMEOUT_BACKOFF_MS)
                        if self._effective_preview_debounce_ms < target_ms:
                            self._effective_preview_debounce_ms = target_ms
                            self._stats["preview_backoff_events"] += 1
                # Other errors do not slow normal pacing.

            self._condition.notify_all()


    def _complete(self, result: TranslationResult) -> None:
        should_publish = False
        with self._condition:
            self._stats["max_latency_ms"] = max(self._stats["max_latency_ms"], int(result.latency_ms))
            key = "completed_finals" if result.is_final else "completed_previews"
            self._stats[key] += 1
            if result.error:
                self._stats["translation_errors"] += 1
            elif result.translated_text:
                cache_key = (tuple(result.context_texts), result.source_text)
                self._translation_cache[cache_key] = result.translated_text
                self._translation_cache.move_to_end(cache_key)
                while len(self._translation_cache) > TRANSLATION_CACHE_MAX_ENTRIES:
                    self._translation_cache.popitem(last=False)
            if result.is_final:
                # Final is authoritative FIFO traffic. Never gate it by a newer request_id.
                should_publish = True
                self._stats["published_finals"] += 1
            elif not result.error and result.utterance_id not in self._finalized_utterances:
                latest_source = self._latest_source_by_utterance.get(result.utterance_id, "")
                latest_revision = self._latest_revision_by_utterance.get(result.utterance_id, 0)
                prior_coverage = self._published_preview_coverage.get(result.utterance_id, 0)
                prior_revision = self._published_preview_revision.get(result.utterance_id, 0)

                # A returned preview is safe when the exact Japanese source that
                # produced it is still a prefix of the newest ASR text. This turns
                # Cloudflare's ~0.6-1.0s response into useful progressive output
                # instead of discarding it merely because ASR advanced a revision.
                prefix_safe = bool(
                    latest_source
                    and latest_source.startswith(result.source_text)
                    and int(result.source_revision) <= int(latest_revision)
                )
                progressive = bool(
                    len(result.source_text) > int(prior_coverage)
                    and int(result.source_revision) > int(prior_revision)
                )
                if prefix_safe and progressive and result.translated_text:
                    should_publish = True
                    self._stats["published_previews"] += 1
                    if (
                        result.request_id != self._latest_preview_requested_id
                        or int(result.source_revision) < int(latest_revision)
                    ):
                        self._stats["prefix_safe_previews"] += 1
                    self._published_preview_source[result.utterance_id] = result.source_text
                    self._published_preview_coverage[result.utterance_id] = len(result.source_text)
                    self._published_preview_revision[result.utterance_id] = int(result.source_revision)
                else:
                    if prefix_safe and not progressive:
                        self._stats["preview_coverage_rejections"] += 1
                    self._stats["stale_results"] += 1
            else:
                self._stats["stale_results"] += 1
        if should_publish:
            try:
                self.result_callback(result)
            except Exception:
                logger.error("翻译结果回调失败", exc_info=True)

    def _preview_loop(self) -> None:
        try:
            while True:
                request: Optional[TranslationRequest] = None
                with self._condition:
                    while True:
                        while self._preview_slot is None and not self._stop_preview:
                            self._condition.wait(timeout=0.25)
                        if self._stop_preview:
                            return

                        # Global dispatch spacing is shared by both preview workers.
                        # During the wait, submit() may replace _preview_slot, so only
                        # the newest pending revision survives.
                        now = self.clock()
                        due_at = self._last_preview_dispatched_at + (
                            float(self._effective_preview_debounce_ms) / 1000.0
                        )
                        wait_seconds = due_at - now
                        if self._last_preview_dispatched_at > 0.0 and wait_seconds > 0.0:
                            self._condition.wait(timeout=min(wait_seconds, 0.25))
                            continue

                        request = self._preview_slot
                        self._preview_slot = None
                        self._last_preview_dispatched_at = self.clock()
                        self._preview_inflight += 1
                        self._stats["dispatched_previews"] += 1
                        self._stats["max_preview_inflight"] = max(
                            self._stats["max_preview_inflight"],
                            self._preview_inflight,
                        )
                        break

                if request is not None:
                    try:
                        result = self._translate_one(request)
                        self._update_preview_backoff(result)
                        self._complete(result)
                    finally:
                        with self._condition:
                            self._preview_inflight = max(0, self._preview_inflight - 1)
                            self._condition.notify_all()
        finally:
            self._close_thread_backend()


    def _final_loop(self) -> None:
        try:
            while True:
                with self._condition:
                    while not self._final_queue:
                        if self._stop_final_when_empty:
                            return
                        self._condition.wait(timeout=0.25)
                    if self._drop_finals:
                        self._final_queue.clear()
                        return
                    request = self._final_queue.popleft()
                self._complete(self._translate_one(request))
        finally:
            self._close_thread_backend()

    def stats_snapshot(self) -> Dict[str, int]:
        with self._condition:
            snapshot = dict(self._stats)
            snapshot["preview_latency_ewma_ms"] = int(round(self._preview_latency_ewma_ms))
            snapshot["effective_preview_debounce_ms"] = int(self._effective_preview_debounce_ms)
            snapshot["preview_inflight"] = int(self._preview_inflight)
            snapshot["preview_timeout_streak"] = int(self._preview_timeout_streak)
            snapshot["preview_recovery_success_streak"] = int(
                self._preview_recovery_success_streak
            )
            snapshot["preview_concurrency"] = int(TRANSLATION_PREVIEW_CONCURRENCY)
            return snapshot

    def close(self, *, drain_finals: bool = True, timeout: float = 8.0) -> bool:
        deadline = time.monotonic() + max(0.2, float(timeout))
        with self._condition:
            self._accepting = False
            self._stop_preview = True
            self._preview_slot = None
            self._drop_finals = not bool(drain_finals)
            if self._drop_finals:
                self._final_queue.clear()
            self._stop_final_when_empty = True
            self._condition.notify_all()
        all_threads = [*self._preview_threads, self._final_thread]
        for thread in all_threads:
            if current_thread() is thread:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)
        return all(not thread.is_alive() for thread in all_threads)

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


def _is_valid_qwen_server_url(value: Any) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(value or "").strip())
        # Accessing .port performs urllib's range/numeric validation.
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme.lower() in ("http", "https")
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and parsed.path in ("", "/")
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
        and (port is None or 1 <= port <= 65535)
    )


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
        "qwen_server_url": _is_valid_qwen_server_url(args.qwen_server_url),
        "qwen_gpu_memory_utilization": 0.05 <= args.qwen_gpu_memory_utilization <= 1.0,
        "qwen_cpu_offload_gb": 0.0 <= args.qwen_cpu_offload_gb <= 64.0,
        "qwen_startup_timeout": args.qwen_startup_timeout > 0,
        "endpoint_punctuation_hold_ms": args.endpoint_punctuation_hold_ms >= 0,
        "endpoint_min_utterance_ms": args.endpoint_min_utterance_ms >= 0,
        "translation_source_language": bool(str(args.translation_source_language).strip()),
        "translation_target_language": bool(str(args.translation_target_language).strip()),
        "translation_preview_interval_ms": 100 <= args.translation_preview_interval_ms <= 5000,
        "translation_api_interval": 0 <= getattr(args, "translation_api_interval", TRANSLATION_DEFAULT_API_INTERVAL) <= 10,
        "translation_min_chars": 1 <= args.translation_min_chars <= 100,
        "translation_min_delta_chars": 1 <= args.translation_min_delta_chars <= 100,
        "translation_preview_timeout": 0.2 <= args.translation_preview_timeout <= 30.0,
        "translation_final_timeout": 0.2 <= args.translation_final_timeout <= 60.0,
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
    """Lazy-load NumPy; Qwen3-ASR inference runs in a Windows-native sidecar."""
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


def _audio_packet_drop_metrics(packet: Dict[str, Any]) -> tuple[int, int]:
    """Return cumulative output/device drops and sync-missing frames safely."""
    def nonnegative_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    per_device = packet.get("device_drops") or {}
    if not isinstance(per_device, dict):
        per_device = {}
    device_drops = sum(nonnegative_int(value) for value in per_device.values())
    total_drops = nonnegative_int(packet.get("dropped_before", 0)) + device_drops
    sync_missing = nonnegative_int(packet.get("sync_missing_chunks", 0))
    return total_drops, sync_missing


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
    """Desktop proxy for the Qwen3-ASR streaming-demo HTTP contract.

    On Windows the embedded sidecar uses Transformers/CUDA and mirrors Qwen's
    official accumulated-audio + rollback-prefix rolling-decode algorithm.
    This class batches 16 kHz float32 PCM and serializes /api/start, /api/chunk
    and /api/finish calls; GPU inference remains single-concurrency.
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
            logger.info(
                "[QWEN_STREAM] forced ASR language=%s; Windows native sidecar will apply it in the model prompt",
                self.language,
            )
        self._result_dispatcher = Thread(
            target=self._dispatch_results, name="Qwen3ASRResultDispatcher", daemon=True
        )
        self._result_dispatcher.start()
        self._worker_thread = Thread(
            # urllib requests cannot be cancelled from another thread. A bounded
            # graceful join still runs in shutdown(), while daemon=True prevents a
            # stalled server from keeping the hidden .pyw process alive forever.
            target=self._run, name="Qwen3ASROfficialHTTPWorker", daemon=True
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
        started = self._request_json("/api/start", method="POST", params={"language": self.language or None}, raw_body=b"")
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
        audio_cursor_samples: int = 0,
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
            "audio_cursor_samples": max(0, int(audio_cursor_samples)),
            "backend": "transformers_windows_cuda_rolling",
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
            audio_cursor_samples=int(result.get("audio_cursor_samples", 0) or 0),
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
            audio_cursor_samples=int(result.get("audio_cursor_samples", 0) or 0),
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
                        # Pass the configured language as a query parameter; Windows native sidecar uses it to force the model prompt.
                        result = self._request_json("/api/start", method="POST", params={"language": self.language or None}, raw_body=b"")
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
            self._translation_settings = _translation_settings_from_args(args, self._persisted_settings)
            logger.info(
                "[TRANSLATION_CONFIG] backend=cloudflare-workers-ai model=%s source=%s target=%s "
                "account_id=%s token=%s",
                CLOUDFLARE_WORKERS_AI_MODEL,
                self._translation_settings.get("source_language", TRANSLATION_DEFAULT_SOURCE),
                self._translation_settings.get("target_language", TRANSLATION_DEFAULT_TARGET),
                "configured" if self._translation_settings.get("cloudflare_account_id") else "missing",
                "configured" if self._translation_settings.get("cloudflare_api_token") else "missing",
            )
            configure_current_process_priority("above_normal")
            self.session_id = uuid.uuid4().hex[:12]
            self.killed = False
            self._lifecycle_lock = Lock()
            self._lifecycle_state = "running"
            # Thread-owning components are started only after every ordinary field
            # has been initialized.  The guarded startup block at the end of this
            # constructor can then reliably close every component if any later
            # startup/recovery step raises.
            self.event_bus: Optional[SessionEventBus] = None
            self.subtitle_session: Optional[SessionActor] = None
            self._transcript_event_queue: queue.Queue = queue.Queue(maxsize=512)
            self._transcript_controller_stop = Event()
            self._transcript_controller_thread: Optional[Thread] = None
            self.translation_coordinator: Optional[TranslationCoordinator] = None

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
                "source_font_size": 24,
                "translation_font_size": 22,
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
                "TR": None,
                "TR_FULL_TEXT": "",
                "TR_SOURCE_TEXT": "",
                "TR_UTTERANCE_ID": 0,
                "TR_SOURCE_REVISION": 0,
                "TR_IS_FINAL": False,
                "TR_REQUEST_ID": 0,
                "TR_LATENCY_MS": 0,
                "TR_BACKEND": "",
                "TR_CURRENT_TEXT": "",
                "TR_CURRENT_UTTERANCE_ID": 0,
                "TR_CURRENT_SOURCE_REVISION": 0,
                "TR_CURRENT_IS_FINAL": False,
                "TR_PREVIOUS_FINAL_TEXT": "",
                "TR_PREVIOUS_FINAL_UTTERANCE_ID": 0,
            }
            # Slow final translations that belong to an older utterance are queued
            # for presentation instead of overwriting one another inside the single
            # UI buffer. 720ms mirrors LiveCaptions-Translator's complete-sentence
            # display choke and guarantees a human-visible final cadence.
            self._translation_final_ui_queue: deque[tuple[int, str]] = deque()
            self._translation_final_ui_pending: set[int] = set()
            self._translation_final_hold_until = 0.0
            self.ui_event_queue: queue.Queue = queue.Queue(maxsize=100)
            self.ui_error_queue: queue.Queue = queue.Queue(maxsize=32)
            self._last_wrap_width = 0
            self._last_rendered: Dict[str, tuple] = {
                "status": (None, None),
                "source": (None, None),
                "translation": (None, None),
            }
            self._last_source_render_at = 0.0

            self.root: Optional[tk.Tk] = None
            self.subtitle_label: Optional[tk.Label] = None
            self.translation_label: Optional[tk.Label] = None
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
                "sync_missing_audio_chunks": 0,
                "translation_display_count": 0,
                "translation_final_display_count": 0,
                "translation_final_result_count": 0,
                "translation_preview_result_count": 0,
                "translation_error_count": 0,
                "max_translation_latency_ms": 0,
            }
            self._recovered_transcript_count = 0
            try:
                self.translation_coordinator = TranslationCoordinator(
                    session_id=self.session_id,
                    result_callback=self._handle_translation_result,
                    enabled=bool(self._translation_settings["enabled"]),
                    source_language=str(self._translation_settings["source_language"]),
                    target_language=str(self._translation_settings["target_language"]),
                    cloudflare_account_id=str(self._translation_settings.get("cloudflare_account_id", "")),
                    cloudflare_api_token=str(self._translation_settings.get("cloudflare_api_token", "")),
                    preview_interval_ms=int(self._translation_settings["preview_interval_ms"]),
                    api_interval=int(self._translation_settings["api_interval"]),
                    min_chars=int(self._translation_settings["min_chars"]),
                    min_delta_chars=int(self._translation_settings["min_delta_chars"]),
                    preview_timeout=float(self._translation_settings["preview_timeout"]),
                    final_timeout=float(self._translation_settings["final_timeout"]),
                )
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
                self.event_bus.subscribe(
                    RealtimeSubtitleSession.TRANSCRIPT_EVENT,
                    self._queue_transcript_event,
                    session_guard=self._event_session_guard,
                )
                self._start_transcript_controller()
                self._recovered_transcript_count = self.event_bus.replay_pending(
                    RealtimeSubtitleSession.TRANSCRIPT_EVENT,
                    self._decode_recovered_transcript,
                )
            except BaseException:
                self._cleanup_failed_initialization()
                raise


    def _cleanup_failed_initialization(self) -> None:
            """Stop every thread-owning component created by a partial __init__."""
            translation_coordinator = getattr(self, "translation_coordinator", None)
            if translation_coordinator is not None:
                try:
                    translation_coordinator.close(drain_finals=False, timeout=2.0)
                except Exception:
                    logger.error("TranslationCoordinator partial initialization cleanup failed", exc_info=True)
            event_bus = getattr(self, "event_bus", None)
            if event_bus is not None:
                try:
                    event_bus.close(drain=False, timeout=2.0)
                except Exception:
                    logger.error("SessionEventBus partial initialization cleanup failed", exc_info=True)
            try:
                self._stop_transcript_controller(drain=False, timeout=2.0)
            except Exception:
                logger.error("TranscriptController partial initialization cleanup failed", exc_info=True)
            subtitle_session = getattr(self, "subtitle_session", None)
            if subtitle_session is not None:
                try:
                    subtitle_session.close(timeout=2.0)
                except Exception:
                    logger.error("SubtitleSession partial initialization cleanup failed", exc_info=True)


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
            self.root.title("日中实时字幕（Qwen3-ASR-1.7B + 实时翻译）")
            self.root.attributes("-topmost", bool(self._ui_preferences.get("topmost", True)))
            self.root.attributes("-alpha", float(self._ui_preferences.get("opacity", 0.95)))
            self.root.configure(bg="black")
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            initial_width = int(sw * 0.85)
            initial_wrap = max(360, initial_width - 60)
            self.root.geometry(f"{initial_width}x235+{int(sw*0.075)}+{max(0, sh-315)}")
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
            self.subtitle_label.pack(expand=True, fill="both", padx=20, pady=(8, 2))
            self.translation_label = tk.Label(
                self.root,
                text="中文翻译：等待语音..." if self._translation_settings.get("enabled", True) else "中文翻译：已关闭",
                font=("Microsoft YaHei", int(self._ui_preferences.get("translation_font_size", 22))),
                fg="#FFFFFF", bg="black", wraplength=initial_wrap,
                justify="left", anchor="w",
            )
            self.translation_label.pack(expand=True, fill="both", padx=20, pady=(2, 10))
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
                if self.translation_label is not None:
                    self.translation_label.config(
                        font=("Microsoft YaHei", int(self._ui_preferences["translation_font_size"]))
                    )
            except tk.TclError:
                logger.debug("显示设置应用失败", exc_info=True)


    def _persist_settings_snapshot(self) -> None:
            translation = dict(self._translation_settings)
            api_token = str(translation.pop("cloudflare_api_token", "") or "").strip()
            translation.pop(CLOUDFLARE_TOKEN_DPAPI_FIELD, None)
            if api_token:
                # Persist only the Windows DPAPI ciphertext. The cleartext token
                # never enters settings.json.
                translation[CLOUDFLARE_TOKEN_DPAPI_FIELD] = _protect_secret_dpapi(api_token)
            payload = {
                "version": 7,
                "audio": self._runtime_settings_snapshot(),
                "display": dict(self._ui_preferences),
                "translation": translation,
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
            win.geometry("650x900")
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
            row += 1
            translation_enabled_var = tk.BooleanVar(value=bool(self._translation_settings.get("enabled", True)))
            tk.Checkbutton(body, text="启用日语 → 中文实时翻译（Cloudflare Workers AI）", variable=translation_enabled_var).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(8, 4)
            )
            row += 1
            tk.Label(body, text="翻译后端", anchor="w").grid(row=row, column=0, sticky="w", pady=4)
            tk.Label(body, text="Cloudflare @cf/meta/m2m100-1.2b", anchor="e").grid(row=row, column=1, sticky="e", pady=4)
            row += 1
            tk.Label(body, text="Cloudflare Account ID", anchor="w").grid(row=row, column=0, sticky="w", pady=4)
            cloudflare_account_var = tk.StringVar(value=str(self._translation_settings.get("cloudflare_account_id", "")))
            tk.Entry(body, textvariable=cloudflare_account_var, width=34).grid(row=row, column=1, sticky="e", pady=4)
            row += 1
            tk.Label(body, text="Cloudflare API Token（Windows DPAPI 加密保存）", anchor="w").grid(row=row, column=0, sticky="w", pady=4)
            cloudflare_token_var = tk.StringVar(value=str(self._translation_settings.get("cloudflare_api_token", "")))
            tk.Entry(body, textvariable=cloudflare_token_var, width=34, show="*").grid(row=row, column=1, sticky="e", pady=4)
            row += 1
            tk.Label(body, text="语言", anchor="w").grid(row=row, column=0, sticky="w", pady=4)
            tk.Label(body, text="ja → zh", anchor="e").grid(row=row, column=1, sticky="e", pady=4)
            row += 1
            tk.Label(body, text="中文字号", anchor="w").grid(row=row, column=0, sticky="w", pady=4)
            translation_font_var = tk.StringVar(value=str(self._ui_preferences.get("translation_font_size", 22)))
            tk.Entry(body, textvariable=translation_font_var, width=18).grid(row=row, column=1, sticky="e", pady=4)

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
                    translation_font_size = max(12, min(72, int(translation_font_var.get())))
                    opacity = max(0.50, min(1.00, float(opacity_var.get())))
                    translation_settings = dict(self._translation_settings)
                    translation_settings.update({
                        "enabled": bool(translation_enabled_var.get()),
                        "source_language": TRANSLATION_DEFAULT_SOURCE,
                        "target_language": TRANSLATION_DEFAULT_TARGET,
                        "cloudflare_account_id": cloudflare_account_var.get().strip(),
                        "cloudflare_api_token": cloudflare_token_var.get().strip(),
                    })
                    if translation_settings["enabled"] and not translation_settings["cloudflare_account_id"]:
                        raise ValueError("启用 Cloudflare 翻译时必须填写 Account ID")
                    if translation_settings["enabled"] and not translation_settings["cloudflare_api_token"]:
                        raise ValueError("启用 Cloudflare 翻译时必须填写 API Token，或设置 CLOUDFLARE_API_TOKEN")
                except Exception as exc:
                    messagebox.showerror("设置无效", str(exc), parent=win)
                    return
                self._request_runtime_settings(requested)
                self._ui_preferences.update({
                    "source_font_size": font_size,
                    "translation_font_size": translation_font_size,
                    "opacity": opacity,
                    "topmost": bool(topmost_var.get()),
                })
                self._translation_settings = translation_settings
                if self.translation_coordinator is not None:
                    self.translation_coordinator.configure(**self._translation_settings)
                if self.translation_label is not None and not self._translation_settings["enabled"]:
                    self._configure_label_if_changed(
                        self.translation_label, "translation", "中文翻译：已关闭", "#808080"
                    )
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
            if self.translation_label is not None:
                self.translation_label.config(wraplength=wrap)


    def _promote_queued_final_locked(self, now: Optional[float] = None) -> bool:
            now = time.monotonic() if now is None else float(now)
            if not self._translation_final_ui_queue or now < float(self._translation_final_hold_until):
                return False
            final_utt, final_text = self._translation_final_ui_queue.popleft()
            self._translation_final_ui_pending.discard(int(final_utt))
            current_source_utt = int(self.ui_update_buffer.get("SRC_UTTERANCE_ID", 0) or 0)
            previous_utt = int(self.ui_update_buffer.get("TR_PREVIOUS_FINAL_UTTERANCE_ID", 0) or 0)
            if final_utt >= current_source_utt or final_utt < previous_utt:
                return False
            self.ui_update_buffer["TR_PREVIOUS_FINAL_TEXT"] = final_text
            self.ui_update_buffer["TR_PREVIOUS_FINAL_UTTERANCE_ID"] = final_utt
            self.ui_update_buffer["TR"] = self._compose_translation_display_locked()
            self.ui_update_buffer["TR_IS_FINAL"] = bool(
                self.ui_update_buffer.get("TR_CURRENT_IS_FINAL", False)
                and int(self.ui_update_buffer.get("TR_CURRENT_UTTERANCE_ID", 0) or 0) == current_source_utt
            )
            self._translation_final_hold_until = now + TRANSLATION_FINAL_DISPLAY_HOLD_SECONDS
            return True

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
            final_queue_displayed = False
            with self.ui_buffer_lock:
                final_queue_displayed = self._promote_queued_final_locked(time.monotonic())
                if self.ui_update_buffer["SRC"] is not None and self.subtitle_label is not None:
                    text = self.ui_update_buffer["SRC"]
                    color = "#00FF00" if self.ui_update_buffer["IS_FINAL"] else "#00CC00"
                    self._configure_label_if_changed(self.subtitle_label, "source", text, color)
                    self.ui_update_buffer["SRC"] = None
                if self.ui_update_buffer["TR"] is not None and self.translation_label is not None:
                    text = self.ui_update_buffer["TR"]
                    color = "#FFFFFF" if self.ui_update_buffer["TR_IS_FINAL"] else "#D8D8D8"
                    self._configure_label_if_changed(self.translation_label, "translation", text, color)
                    self.ui_update_buffer["TR"] = None
            if final_queue_displayed:
                self._stat_add("translation_display_count", 1)
                self._stat_add("translation_final_display_count", 1)
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


    def _decode_recovered_transcript(
            self, payload: Dict[str, Any], record: Dict[str, Any]
        ) -> TranscriptEvent:
            """Remap a prior-process final into this UI session without id collisions."""
            text = normalize_subtitle_text(str(payload.get("text", "") or ""))
            record_id = str(record.get("record_id", "") or "")
            try:
                recovered_id = -max(1, int(record_id[:12], 16))
            except ValueError:
                recovered_id = -max(1, int(time.time_ns() & 0x7FFFFFFF))
            return TranscriptEvent(
                session_id=self.session_id,
                utterance_id=recovered_id,
                revision=max(1, int(payload.get("revision", 1) or 1)),
                state="final",
                text=text,
                committed_text=text,
                revisable_text="",
                is_final=True,
                emitted_at=time.monotonic(),
            )


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
                    handled = False
                    try:
                        self._handle_transcript_event(event)
                        handled = True
                    except Exception:
                        logger.error("TranscriptController 处理失败", exc_info=True)
                    finally:
                        if handled and event.is_final:
                            if not self.event_bus.acknowledge_delivered_event(event):
                                logger.error(
                                    "[RECOVERY_ACK] final delivery ACK failed utterance=%s",
                                    event.utterance_id,
                                )
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

    def _shutdown_asr_for_restart(self) -> bool:
            """Require the old proxy to stop before a replacement is constructed."""
            proxy = self.async_asr
            if proxy is None:
                return True
            try:
                stopped = bool(proxy.shutdown())
            except Exception:
                logger.error("ASR restart could not shut down the previous proxy", exc_info=True)
                return False
            if not stopped:
                logger.error(
                    "[ASR_PROCESS] session_id=%s restart_aborted old_worker_still_alive",
                    self.session_id,
                )
                return False
            self.async_asr = None
            return True

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

    def _compose_translation_display_locked(self) -> str:
            current_utt = int(self.ui_update_buffer.get("SRC_UTTERANCE_ID", 0) or 0)
            previous_utt = int(self.ui_update_buffer.get("TR_PREVIOUS_FINAL_UTTERANCE_ID", 0) or 0)
            previous_text = str(self.ui_update_buffer.get("TR_PREVIOUS_FINAL_TEXT", "") or "").strip()
            translated_utt = int(self.ui_update_buffer.get("TR_CURRENT_UTTERANCE_ID", 0) or 0)
            current_text = str(self.ui_update_buffer.get("TR_CURRENT_TEXT", "") or "").strip()
            lines: List[str] = []
            if previous_text and previous_utt > 0 and previous_utt < current_utt:
                lines.append("上一句：" + previous_text)
            if current_utt > 0:
                if translated_utt == current_utt and current_text:
                    lines.append("当前：" + current_text)
                else:
                    lines.append("当前：翻译中…")
            return "\n".join(lines) if lines else "中文翻译：等待语音..."

    def _handle_translation_result(self, result: TranslationResult) -> None:
            if result.session_id != self.session_id:
                return
            if result.is_final:
                self._stat_add("translation_final_result_count", 1)
            else:
                self._stat_add("translation_preview_result_count", 1)
            if result.error:
                self._stat_add("translation_error_count", 1)
                if not result.is_final:
                    return
                display_text = f"[翻译失败] {result.error}"[-260:]
            else:
                display_text = result.translated_text[-260:]
            self._stat_max("max_translation_latency_ms", result.latency_ms)
            logger.info(
                "[TRANSLATION_RESULT] utterance=%d revision=%d request=%d final=%s backend=%s latency_ms=%d source_chars=%d translated_chars=%d",
                result.utterance_id, result.source_revision, result.request_id, result.is_final,
                result.backend, result.latency_ms, len(result.source_text), len(result.translated_text),
            )

            displayed = False
            final_visible = False
            with self.ui_buffer_lock:
                current_source_utterance = int(self.ui_update_buffer.get("SRC_UTTERANCE_ID", 0) or 0)
                current_translation_utterance = int(self.ui_update_buffer.get("TR_CURRENT_UTTERANCE_ID", 0) or 0)
                current_translation_revision = int(self.ui_update_buffer.get("TR_CURRENT_SOURCE_REVISION", 0) or 0)
                current_translation_final = bool(self.ui_update_buffer.get("TR_CURRENT_IS_FINAL", False))

                if result.is_final:
                    if result.utterance_id < current_source_utterance:
                        # Slow authoritative final: queue it for a guaranteed 720ms
                        # previous-final presentation instead of racing the UI buffer.
                        previous_utt = int(self.ui_update_buffer.get("TR_PREVIOUS_FINAL_UTTERANCE_ID", 0) or 0)
                        if (
                            result.utterance_id >= previous_utt
                            and result.utterance_id not in self._translation_final_ui_pending
                        ):
                            self._translation_final_ui_queue.append((result.utterance_id, display_text))
                            self._translation_final_ui_pending.add(result.utterance_id)
                    else:
                        self.ui_update_buffer["TR_CURRENT_TEXT"] = display_text
                        self.ui_update_buffer["TR_CURRENT_UTTERANCE_ID"] = result.utterance_id
                        self.ui_update_buffer["TR_CURRENT_SOURCE_REVISION"] = result.source_revision
                        self.ui_update_buffer["TR_CURRENT_IS_FINAL"] = True
                        displayed = True
                        final_visible = True
                else:
                    # Preview is only meaningful for the currently displayed source.
                    if result.utterance_id != current_source_utterance:
                        return
                    if result.utterance_id < current_translation_utterance:
                        return
                    if result.utterance_id == current_translation_utterance:
                        if current_translation_final:
                            return
                        if result.source_revision < current_translation_revision:
                            return
                    self.ui_update_buffer["TR_CURRENT_TEXT"] = display_text
                    self.ui_update_buffer["TR_CURRENT_UTTERANCE_ID"] = result.utterance_id
                    self.ui_update_buffer["TR_CURRENT_SOURCE_REVISION"] = result.source_revision
                    self.ui_update_buffer["TR_CURRENT_IS_FINAL"] = False
                    displayed = True

                if displayed:
                    composed = self._compose_translation_display_locked()
                    self.ui_update_buffer["TR"] = composed
                    self.ui_update_buffer["TR_FULL_TEXT"] = result.translated_text
                    self.ui_update_buffer["TR_SOURCE_TEXT"] = result.source_text
                    self.ui_update_buffer["TR_UTTERANCE_ID"] = result.utterance_id
                    self.ui_update_buffer["TR_SOURCE_REVISION"] = result.source_revision
                    # Combined two-level display is white only when current sentence
                    # has an authoritative final; otherwise keep preview gray.
                    self.ui_update_buffer["TR_IS_FINAL"] = bool(
                        self.ui_update_buffer.get("TR_CURRENT_IS_FINAL", False)
                        and int(self.ui_update_buffer.get("TR_CURRENT_UTTERANCE_ID", 0) or 0) == current_source_utterance
                    )
                    self.ui_update_buffer["TR_REQUEST_ID"] = result.request_id
                    self.ui_update_buffer["TR_LATENCY_MS"] = result.latency_ms
                    self.ui_update_buffer["TR_BACKEND"] = result.backend
            if displayed:
                self._stat_add("translation_display_count", 1)
            if final_visible:
                self._stat_add("translation_final_display_count", 1)


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
                    previous_source_utt = int(self.ui_update_buffer.get("SRC_UTTERANCE_ID", 0) or 0)
                    if event.utterance_id > previous_source_utt:
                        tr_current_utt = int(self.ui_update_buffer.get("TR_CURRENT_UTTERANCE_ID", 0) or 0)
                        tr_current_final = bool(self.ui_update_buffer.get("TR_CURRENT_IS_FINAL", False))
                        tr_current_text = str(self.ui_update_buffer.get("TR_CURRENT_TEXT", "") or "").strip()
                        if tr_current_final and tr_current_utt == previous_source_utt and tr_current_text:
                            self.ui_update_buffer["TR_PREVIOUS_FINAL_TEXT"] = tr_current_text
                            self.ui_update_buffer["TR_PREVIOUS_FINAL_UTTERANCE_ID"] = tr_current_utt
                            self._translation_final_hold_until = max(
                                float(self._translation_final_hold_until),
                                time.monotonic() + TRANSLATION_FINAL_DISPLAY_HOLD_SECONDS,
                            )
                        self.ui_update_buffer["TR_CURRENT_TEXT"] = ""
                        self.ui_update_buffer["TR_CURRENT_UTTERANCE_ID"] = event.utterance_id
                        self.ui_update_buffer["TR_CURRENT_SOURCE_REVISION"] = 0
                        self.ui_update_buffer["TR_CURRENT_IS_FINAL"] = False
                    self.ui_update_buffer["SRC"] = display_text
                    self.ui_update_buffer["SRC_DISPLAY_TEXT"] = display_text
                    self.ui_update_buffer["SRC_FULL_TEXT"] = full_text
                    self.ui_update_buffer["SRC_DISPLAY_IS_FINAL"] = event.is_final
                    self.ui_update_buffer["SRC_DISPLAY_AT"] = now
                    self.ui_update_buffer["SRC_UTTERANCE_ID"] = event.utterance_id
                    self.ui_update_buffer["IS_FINAL"] = event.is_final
                    self.ui_update_buffer["TR"] = self._compose_translation_display_locked()
                    self.ui_update_buffer["TR_IS_FINAL"] = bool(
                        self.ui_update_buffer.get("TR_CURRENT_IS_FINAL", False)
                        and int(self.ui_update_buffer.get("TR_CURRENT_UTTERANCE_ID", 0) or 0) == event.utterance_id
                    )
                self.ui_update_buffer["SOURCE_REVISION"] = event.revision
                self.ui_update_buffer["SOURCE_STATE"] = event.state
                self.ui_update_buffer["STABLE_SOURCE_TEXT"] = event.committed_text
                self.ui_update_buffer["UNSTABLE_SOURCE_TEXT"] = event.revisable_text
            if self.translation_coordinator is not None:
                try:
                    self.translation_coordinator.submit(event)
                except Exception:
                    logger.error("TranslationCoordinator 提交失败", exc_info=True)
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
                        if not self.async_asr.shutdown():
                            failures.append("asr_service")
                            logger.error("ASR 服务未在超时内停止")
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
                if self.translation_coordinator is not None:
                    try:
                        if not self.translation_coordinator.close(drain_finals=True, timeout=8.0):
                            failures.append("translation_coordinator")
                    except Exception:
                        failures.append("translation_coordinator")
                        logger.error("TranslationCoordinator 清理失败", exc_info=True)
                self._set_lifecycle_state("stopping_delivery")
                try:
                    if not self.subtitle_session.close(timeout=5.0):
                        failures.append("subtitle_session")
                except Exception:
                    failures.append("subtitle_session")
                    logger.error("字幕 Session 清理失败", exc_info=True)
                stats = self._stats_snapshot()
                tr_stats = self.translation_coordinator.stats_snapshot() if self.translation_coordinator is not None else {}
                logger.info(
                    "[SESSION_SUMMARY] session_id=%s finals=%d asr_tasks=%d max_asr_queue_ms=%d "
                    "max_asr_inference_ms=%d hybrid_endpoints=%d forced_endpoints=%d "
                    "forced_dedup_chars=%d audio_drops=%d sync_missing=%d discontinuities=%d "
                    "max_audio_age_ms=%d asr_restarts=%d translation_displays=%d translation_final_displays=%d translation_finals=%d "
                    "translation_previews=%d translation_errors=%d max_translation_ms=%d "
                    "cloudflare_success=%d cloudflare_timeout=%d cloudflare_http_error=%d cloudflare_api_error=%d "
                    "cloudflare_parse_error=%d cloudflare_network_error=%d cloudflare_rate_limited=%d "
                    "preview_dispatched=%d preview_ewma_ms=%d preview_debounce_ms=%d "
                    "preview_concurrency=%d preview_max_inflight=%d preview_backoff_events=%d "
                    "preview_recovery_events=%d preview_prefix_safe=%d preview_branch_resets=%d "
                    "preview_coverage_rejections=%d",
                    self.session_id, stats["subtitle_final_count"], stats["asr_tasks"],
                    stats["max_asr_queue_latency_ms"], stats["max_asr_inference_latency_ms"],
                    stats["hybrid_endpoint_count"], stats["forced_endpoint_count"],
                    stats["forced_overlap_deduplicated_chars"], stats["dropped_audio_chunks"],
                    stats["sync_missing_audio_chunks"], stats["audio_discontinuity_count"],
                    stats["max_audio_age_ms"], stats["asr_restart_count"],
                    stats["translation_display_count"], stats["translation_final_display_count"],
                    stats["translation_final_result_count"], stats["translation_preview_result_count"], stats["translation_error_count"],
                    stats["max_translation_latency_ms"],
                    tr_stats.get("cloudflare_success", 0), tr_stats.get("cloudflare_timeout", 0),
                    tr_stats.get("cloudflare_http_error", 0), tr_stats.get("cloudflare_api_error", 0),
                    tr_stats.get("cloudflare_parse_error", 0), tr_stats.get("cloudflare_network_error", 0),
                    tr_stats.get("cloudflare_rate_limited", 0),
                    tr_stats.get("dispatched_previews", 0),
                    tr_stats.get("preview_latency_ewma_ms", 0),
                    tr_stats.get("effective_preview_debounce_ms", 0),
                    tr_stats.get("preview_concurrency", TRANSLATION_PREVIEW_CONCURRENCY),
                    tr_stats.get("max_preview_inflight", 0),
                    tr_stats.get("preview_backoff_events", 0),
                    tr_stats.get("preview_recovery_events", 0),
                    tr_stats.get("prefix_safe_previews", 0),
                    tr_stats.get("preview_branch_resets", 0),
                    tr_stats.get("preview_coverage_rejections", 0),
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
                "[RUNTIME_PRELOAD_STAGE] stage=qwen_windows_transformers_sidecar_client ms=0 success=True url=%s",
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
            device_str = f'Qwen Windows Transformers rolling-stream {self.args.qwen_server_url}'
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
                logger.info('[ASR_STREAM_METRIC] session_id=%s sequence=%d state=%s audio_ms=%d queue_ms=%d inference_ms=%d has_text=%s audio_cursor_samples=%d', self.session_id, metrics['sequence'], metrics['state'], metrics['audio_duration_ms'], metrics['queue_latency_ms'], metrics['inference_latency_ms'], metrics['has_text'], metrics.get('audio_cursor_samples', 0))

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
                self.update_status(f'● 连接 {model_label} Windows Transformers rolling-stream backend...', '#FFAA00')
                warmup_seconds = self.async_asr.warmup()
                if self._shutdown_requested():
                    return
                self._asr_failure_event.clear()
                logger.info('%s 原生流式 backend 就绪：%.0fms', model_label, warmup_seconds * 1000)
            except Exception as e:
                if self._shutdown_requested():
                    return
                logger.error('流式 ASR 初始化失败：%s', e, exc_info=True)
                self.update_status('● 错误：Qwen Windows Transformers sidecar 不可用，见 subtitle.log', '#FF0000')
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
            last_reported_sync_missing_count = 0
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
            logger.info('Ready: Qwen3-ASR native rolling-stream backend=transformers_windows_cuda chunk=%.2fs language=%s server=%s', self.args.qwen_chunk_size_sec, self.args.asr_language, self.args.qwen_server_url)
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
                    if not self._shutdown_asr_for_restart():
                        self._asr_failure_message = (
                            "Previous ASR worker did not stop; automatic restart aborted"
                        )
                        self.update_status(
                            "● 错误：旧 ASR 未停止，已中止重连",
                            "#FF0000",
                        )
                        break
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
                    cumulative_drops, sync_missing = _audio_packet_drop_metrics(packet)
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
                    if cumulative_drops > last_reported_drop_count:
                        dropped = cumulative_drops - last_reported_drop_count
                        self._stat_add('dropped_audio_chunks', dropped)
                        last_reported_drop_count = cumulative_drops
                        logger.warning(
                            '[Audio] session_id=%s output_or_device_drops=%d total=%d',
                            self.session_id, dropped, cumulative_drops,
                        )
                    if sync_missing > last_reported_sync_missing_count:
                        missing = sync_missing - last_reported_sync_missing_count
                        self._stat_add('sync_missing_audio_chunks', missing)
                        last_reported_sync_missing_count = sync_missing
                        logger.warning(
                            '[Audio] session_id=%s sync_missing_chunks=%d total=%d',
                            self.session_id, missing, sync_missing,
                        )
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
    parser = argparse.ArgumentParser(description="日语实时字幕（Qwen3-ASR-1.7B + Windows 原生 Transformers/CUDA Streaming）")
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
    parser.add_argument("--qwen-gpu-memory-utilization", type=float, default=QWEN3_ASR_RTX3090_GPU_MEMORY_UTILIZATION, help=argparse.SUPPRESS)
    parser.add_argument("--qwen-cpu-offload-gb", type=float, default=0.0, help=argparse.SUPPRESS)
    parser.add_argument("--qwen-cuda-visible-devices", type=str, default=QWEN3_ASR_RTX3090_GPU_INDEX)
    parser.add_argument("--qwen-startup-timeout", type=float, default=900.0)
    parser.add_argument("--qwen-python", type=str, default="", help="Windows 原生 sidecar 使用的 python.exe；默认优先 .venv\\Scripts\\python.exe")
    parser.add_argument("--qwen-hf-home", type=str, default="", help="可选：Windows Hugging Face 缓存目录")
    parser.add_argument("--qwen-transformers-dtype", choices=["bfloat16", "float16"], default="bfloat16")
    parser.add_argument("--qwen-max-new-tokens", type=int, default=32)
    parser.add_argument("--no-qwen-auto-start", action="store_true")
    parser.add_argument("--keep-qwen-server", action="store_true")
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
    parser.add_argument("--disable-translation", action="store_true", help="关闭 Cloudflare Workers AI 日语→中文实时翻译")
    parser.add_argument("--translation-source-language", type=str, default=TRANSLATION_DEFAULT_SOURCE)
    parser.add_argument("--translation-target-language", type=str, default=TRANSLATION_DEFAULT_TARGET)
    parser.add_argument("--cloudflare-account-id", type=str, default=os.environ.get(CLOUDFLARE_ACCOUNT_ID_ENV, ""), help="Cloudflare Account ID；也可使用 CLOUDFLARE_ACCOUNT_ID 环境变量")
    parser.add_argument("--cloudflare-api-token", type=str, default=os.environ.get(CLOUDFLARE_API_TOKEN_ENV, ""), help="Cloudflare Workers AI API Token；也可使用 CLOUDFLARE_API_TOKEN 环境变量")
    parser.add_argument("--translation-preview-interval-ms", type=int, default=TRANSLATION_DEFAULT_PREVIEW_INTERVAL_MS)
    parser.add_argument("--translation-api-interval", type=int, default=TRANSLATION_DEFAULT_API_INTERVAL, help=argparse.SUPPRESS)  # deprecated compatibility option
    parser.add_argument("--translation-min-chars", type=int, default=TRANSLATION_DEFAULT_MIN_CHARS)
    parser.add_argument("--translation-min-delta-chars", type=int, default=TRANSLATION_DEFAULT_MIN_DELTA_CHARS)
    parser.add_argument("--translation-preview-timeout", type=float, default=TRANSLATION_DEFAULT_PREVIEW_TIMEOUT)
    parser.add_argument("--translation-final-timeout", type=float, default=TRANSLATION_DEFAULT_FINAL_TIMEOUT)
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
    if QWEN_WINDOWS_NATIVE_SIDECAR_INTERNAL_ARG in sys.argv:
        sys.argv = [
            arg for arg in sys.argv
            if arg != QWEN_WINDOWS_NATIVE_SIDECAR_INTERNAL_ARG
        ]
        _run_embedded_windows_qwen_sidecar()
        raise SystemExit(0)

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
    logger.info("ASR backend：Windows-native Qwen3-ASR Transformers CUDA streaming compatibility server")
    logger.info(
        "实时翻译：%s backend=cloudflare-workers-ai model=%s source=%s target=%s "
        "preview_debounce=%dms min_chars=%d delta=%d preview_timeout=%.1fs final_timeout=%.1fs "
        "preview_concurrency=%d pending=1 backoff=timeoutx2->500ms,429->1000ms,recoverx3->base",
        "off" if args.disable_translation else "on",
        CLOUDFLARE_WORKERS_AI_MODEL,
        args.translation_source_language,
        args.translation_target_language,
        args.translation_preview_interval_ms,
        args.translation_min_chars,
        args.translation_min_delta_chars,
        args.translation_preview_timeout,
        args.translation_final_timeout,
        TRANSLATION_PREVIEW_CONCURRENCY,
    )
    logger.info("=" * 60)
    runtime = PreparedRuntimeLauncher(args)
    try:
        if not runtime.start():
            sys.exit(3)
        app = SubtitleApp(args)
        app.run()
    finally:
        runtime.close()
