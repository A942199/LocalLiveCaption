# -*- coding: utf-8 -*-

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
from contextlib import nullcontext
from dataclasses import dataclass, field, asdict, is_dataclass
from difflib import SequenceMatcher
from typing import Optional, List, Dict, Any, Callable
import argparse
import json
import uuid
import wave
import hashlib
import shutil
import tempfile
import sqlite3
from math import gcd
JOURNAL_MAX_BYTES: int = 50 * 1024 * 1024
JOURNAL_RETAIN_ACKNOWLEDGED_SECONDS: float = 86400.0
DEDUP_FUZZY_THRESHOLD: float = 0.88
DEDUP_MIN_MATCHING_COVERAGE: float = 0.78
DEDUP_VARIABLE_MIN_COVERAGE: float = 0.74
DEDUP_VARIABLE_RELAXED_OFFSET: float = 0.05
DEDUP_VARIABLE_RELAXED_FLOOR: float = 0.82
SESSION_ACTOR_COMMAND_QUEUE_SIZE: int = 2048
PREVIEW_RESULT_CACHE_MAX: int = 128
EVENT_SUBSCRIBER_CONDITION_TIMEOUT: float = 0.5

class RecoveryJournal:
    """Crash-safe append-only journal with idempotent final records and O(1) metrics."""

    def __init__(self, path: str, *, max_bytes: int=JOURNAL_MAX_BYTES):
        self.path = os.path.abspath(path)
        self.max_bytes = max(1024 * 1024, int(max_bytes))
        self._lock = Lock()
        self._loaded = False
        self._records: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._pending_keys: Dict[str, str] = {}
        self._pending_count = 0
        self._write_failures = 0
        self._consecutive_write_failures = 0
        self._last_error = ''
        self._last_success_at = 0.0
        self._last_failure_at = 0.0
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def _serialize(self, record: Dict[str, Any]) -> str:
        return json.dumps(record, ensure_ascii=False, default=str, sort_keys=True) + '\n'

    @staticmethod
    def _event_key(event_name: str, payload: Dict[str, Any]) -> str:
        identity = {'event_name': str(event_name), 'session_id': payload.get('session_id', ''), 'utterance_id': payload.get('utterance_id', 0), 'source_revision': payload.get('source_revision', payload.get('revision', 0)), 'is_final': bool(payload.get('is_final', False)), 'text': payload.get('text', '')}
        raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def _ensure_loaded_locked(self) -> None:
        if self._loaded:
            return
        self._records.clear()
        self._pending_keys.clear()
        self._pending_count = 0
        if os.path.isfile(self.path):
            acknowledged: set[str] = set()
            with open(self.path, 'r', encoding='utf-8') as handle:
                for line_no, line in enumerate(handle, 1):
                    try:
                        item = json.loads(line)
                        if not isinstance(item, dict):
                            continue
                        if item.get('record_type') == 'ack':
                            acknowledged.update((str(v) for v in item.get('record_ids', []) if v))
                            continue
                        rid = str(item.get('record_id') or hashlib.sha256(line.encode('utf-8')).hexdigest())
                        item['record_id'] = rid
                        item.setdefault('status', 'pending')
                        item.setdefault('event_key', self._event_key(str(item.get('event_name', '')), dict(item.get('event', {}))))
                        self._records[rid] = item
                    except Exception:
                        logger.warning('[RECOVERY_JOURNAL] corrupt line skipped path=%s line=%d', self.path, line_no)
            for rid, record in self._records.items():
                if rid in acknowledged:
                    record['status'] = 'acknowledged'
                if record.get('status', 'pending') == 'pending':
                    key = str(record.get('event_key', ''))
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
            payload = asdict(event) if is_dataclass(event) else dict(getattr(event, '__dict__', {}))
            event_key = self._event_key(event_name, payload)
            now = time.time()
            with self._lock:
                self._ensure_loaded_locked()
                if event_key in self._pending_keys:
                    return True
                if os.path.isfile(self.path) and os.path.getsize(self.path) >= self.max_bytes:
                    self._compact_locked(retain_acknowledged_seconds=JOURNAL_RETAIN_ACKNOWLEDGED_SECONDS)
                record = {'record_id': uuid.uuid4().hex, 'event_key': event_key, 'recorded_at': now, 'updated_at': now, 'status': 'pending', 'reason': str(reason), 'event_name': str(event_name), 'event_type': type(event).__name__, 'event': payload}
                with open(self.path, 'a', encoding='utf-8') as handle:
                    handle.write(self._serialize(record))
                    handle.flush()
                    os.fsync(handle.fileno())
                self._records[record['record_id']] = record
                self._pending_keys[event_key] = record['record_id']
                self._pending_count += 1
                self._last_success_at = now
                self._consecutive_write_failures = 0
                self._last_error = ''
            return True
        except Exception as exc:
            self._write_failures += 1
            self._consecutive_write_failures += 1
            self._last_failure_at = time.time()
            self._last_error = str(exc)
            logger.error('[RECOVERY_JOURNAL] append failed path=%s', self.path, exc_info=True)
            return False

    def load_pending(self, *, limit: int=1000) -> List[Dict[str, Any]]:
        with self._lock:
            self._ensure_loaded_locked()
            pending = [dict(r) for r in self._records.values() if r.get('status', 'pending') == 'pending']
        return pending[-max(1, int(limit)):]

    def acknowledge_with_status(self, record_ids: List[str]) -> tuple[int, bool]:
        """Acknowledge records and report whether the operation itself succeeded.

        ``count == 0`` is a valid idempotent no-op when all records were already
        acknowledged.  The boolean avoids a costly full-journal scan just to
        distinguish that case from an I/O failure.
        """
        wanted = sorted({str(item) for item in record_ids if item})
        if not wanted:
            return (0, True)
        try:
            with self._lock:
                self._ensure_loaded_locked()
                actual = [rid for rid in wanted if rid in self._records and self._records[rid].get('status') == 'pending']
                if not actual:
                    return (0, True)
                marker = {'record_type': 'ack', 'recorded_at': time.time(), 'record_ids': actual}
                with open(self.path, 'a', encoding='utf-8') as handle:
                    handle.write(self._serialize(marker))
                    handle.flush()
                    os.fsync(handle.fileno())
                now = time.time()
                for rid in actual:
                    record = self._records[rid]
                    record['status'] = 'acknowledged'
                    record['updated_at'] = now
                    self._pending_keys.pop(str(record.get('event_key', '')), None)
                    self._pending_count = max(0, self._pending_count - 1)
                self._last_success_at = now
                self._consecutive_write_failures = 0
                self._last_error = ''
                return (len(actual), True)
        except Exception as exc:
            self._write_failures += 1
            self._consecutive_write_failures += 1
            self._last_failure_at = time.time()
            self._last_error = str(exc)
            logger.error('[RECOVERY_JOURNAL] acknowledge failed path=%s', self.path, exc_info=True)
            return (0, False)

    def acknowledge(self, record_ids: List[str]) -> int:
        count, _ok = self.acknowledge_with_status(record_ids)
        return count

    def compact(self, *, retain_acknowledged_seconds: float=JOURNAL_RETAIN_ACKNOWLEDGED_SECONDS) -> int:
        with self._lock:
            self._ensure_loaded_locked()
            return self._compact_locked(retain_acknowledged_seconds=retain_acknowledged_seconds)

    def _write_locked(self, records: List[Dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temporary = f'{self.path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp'
        with open(temporary, 'w', encoding='utf-8') as handle:
            for record in records:
                handle.write(self._serialize(record))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def _compact_locked(self, *, retain_acknowledged_seconds: float) -> int:
        cutoff = time.time() - max(0.0, float(retain_acknowledged_seconds))
        records = list(self._records.values())
        kept = [r for r in records if r.get('status') != 'acknowledged' or float(r.get('updated_at', 0)) >= cutoff]
        removed = len(records) - len(kept)
        self._write_locked(kept)
        self._loaded = False
        self._ensure_loaded_locked()
        return removed

    def health_snapshot(self) -> Dict[str, Any]:
        return {'component': 'recovery_journal', 'state': 'failed' if self._consecutive_write_failures else 'running', 'pending': self.pending_count, 'write_failures': int(self._write_failures), 'consecutive_write_failures': int(self._consecutive_write_failures), 'last_error': self._last_error, 'last_success_at': self._last_success_at, 'last_failure_at': self._last_failure_at}

class RecoverySpooler:
    """Dedicated durable-writer lane so EventBus, SessionActor and Tk never fsync."""

    def __init__(self, journal: RecoveryJournal, *, max_queue_size: int=4096):
        self.journal = journal
        self._queue: queue.Queue = queue.Queue(maxsize=max(128, int(max_queue_size)))
        self._stop = Event()
        self._closed = Event()
        self._write_failures = 0
        self._consecutive_write_failures = 0
        self._thread = Thread(target=self._run, name='RecoveryJournalWriter', daemon=False)
        self._thread.start()

    def submit(self, reason: str, event_name: str, event: Any) -> bool:
        if self._closed.is_set():
            return False
        try:
            self._queue.put(('append', reason, event_name, event), timeout=0.05)
            return True
        except queue.Full:
            logger.critical('[RECOVERY_SPOOL] queue full; using synchronous emergency write')
            return self.journal.append(reason, event_name, event)

    def submit_durable(self, reason: str, event_name: str, event: Any, *, timeout: float=5.0) -> bool:
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
            self._queue.put(('append_durable', reason, event_name, event, done, result), timeout=0.05)
        except queue.Full:
            logger.critical('[RECOVERY_SPOOL] durable queue full; using synchronous emergency write')
            return self.journal.append(reason, event_name, event)
        if done.wait(timeout=max(0.1, float(timeout))):
            return bool(result.get('ok', False))
        logger.error('[RECOVERY_SPOOL] durable append timeout; using idempotent direct fallback')
        return self.journal.append(reason, event_name, event)

    def acknowledge(self, record_ids: List[str]) -> bool:
        ids = [str(item) for item in record_ids if item]
        if not ids or self._closed.is_set():
            return False
        try:
            self._queue.put(('ack', ids), timeout=0.05)
            return True
        except queue.Full:
            logger.critical('[RECOVERY_SPOOL] ACK queue full; using synchronous emergency write')
            _count, ok = self.journal.acknowledge_with_status(ids)
            return ok

    def acknowledge_durable(self, record_ids: List[str], *, timeout: float=5.0) -> bool:
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
            self._queue.put(('ack_durable', ids, done, result), timeout=0.05)
        except queue.Full:
            logger.critical('[RECOVERY_SPOOL] durable ACK queue full; using synchronous emergency write')
            _count, ok = self.journal.acknowledge_with_status(ids)
            return ok
        if done.wait(timeout=max(0.1, float(timeout))):
            return bool(result.get('ok', False))
        logger.error('[RECOVERY_SPOOL] durable ACK timeout; using idempotent direct fallback')
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
                if item[0] in ('append', 'append_durable'):
                    if item[0] == 'append_durable':
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
                        result['ok'] = bool(ok)
                    if done is not None:
                        done.set()
                elif item[0] in ('ack', 'ack_durable'):
                    if item[0] == 'ack_durable':
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
                        self._consecutive_write_failures = 0
                    if result is not None:
                        result['ok'] = bool(ok)
                    if done is not None:
                        done.set()
            except Exception:
                self._write_failures += 1
                self._consecutive_write_failures += 1
                logger.error('[RECOVERY_SPOOL] writer task failed', exc_info=True)
            finally:
                self._queue.task_done()

    def flush(self, timeout: float=5.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return self._queue.unfinished_tasks == 0

    def close(self, *, drain: bool=True, timeout: float=5.0) -> bool:
        if not drain:
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
                else:
                    self._queue.task_done()
        elif not self.flush(timeout=max(0.1, timeout * 0.8)):
            logger.error('[RECOVERY_SPOOL] drain timeout pending=%d', self._queue.qsize())
        self._stop.set()
        if current_thread() is not self._thread:
            self._thread.join(timeout=max(0.1, timeout))
        self._closed.set()
        return not self._thread.is_alive()

    def metrics(self) -> Dict[str, Any]:
        return {'component': 'recovery_spooler', 'state': 'failed' if self._consecutive_write_failures else 'running', 'pending_tasks': self._queue.qsize(), 'write_failures': int(self._write_failures), 'consecutive_write_failures': int(self._consecutive_write_failures), 'worker_alive': self._thread.is_alive()}

class SubtitleHistoryStore:
    """Durable source-subtitle history store."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._lock = Lock()
        self._connection_handle: Optional[sqlite3.Connection] = None
        self._write_failures = 0
        self._consecutive_write_failures = 0
        self._last_error = ''
        self._last_success_at = 0.0
        self._last_failure_at = 0.0
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._initialize()

    def _connection(self) -> sqlite3.Connection:
        connection = self._connection_handle
        if connection is None:
            connection = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA busy_timeout=5000')
            self._connection_handle = connection
        return connection

    def _discard_connection(self) -> None:
        with self._lock:
            connection = self._connection_handle
            self._connection_handle = None
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    logger.debug('[SUBTITLE_HISTORY] connection close failed', exc_info=True)

    def _initialize(self) -> None:
        with self._lock:
            connection = self._connection()
            connection.execute('\n                CREATE TABLE IF NOT EXISTS subtitle_source_history (\n                    record_id TEXT PRIMARY KEY,\n                    session_id TEXT NOT NULL,\n                    utterance_id INTEGER NOT NULL,\n                    source_text TEXT NOT NULL,\n                    source_revision INTEGER NOT NULL DEFAULT 0,\n                    is_final INTEGER NOT NULL DEFAULT 1,\n                    recovered INTEGER NOT NULL DEFAULT 0,\n                    created_at REAL NOT NULL,\n                    updated_at REAL NOT NULL\n                )\n                ')
            connection.execute('CREATE INDEX IF NOT EXISTS idx_subtitle_source_history_updated ON subtitle_source_history(updated_at)')
            connection.commit()

    def persist(self, *, record_id: str, session_id: str, utterance_id: int, source_text: str, source_revision: int=0, is_final: bool=True, recovered: bool=False) -> bool:
        if not record_id:
            return False
        now = time.time()
        try:
            with self._lock:
                connection = self._connection()
                connection.execute('\n                    INSERT INTO subtitle_source_history (\n                        record_id, session_id, utterance_id, source_text,\n                        source_revision, is_final, recovered, created_at, updated_at\n                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n                    ON CONFLICT(record_id) DO UPDATE SET\n                        source_text=excluded.source_text,\n                        source_revision=MAX(source_revision, excluded.source_revision),\n                        is_final=excluded.is_final,\n                        recovered=excluded.recovered,\n                        updated_at=excluded.updated_at\n                    ', (str(record_id), str(session_id), int(utterance_id), str(source_text or ''), int(source_revision), 1 if is_final else 0, 1 if recovered else 0, now, now))
                connection.commit()
            self._consecutive_write_failures = 0
            self._last_success_at = now
            self._last_error = ''
            return True
        except Exception as exc:
            self._write_failures += 1
            self._consecutive_write_failures += 1
            self._last_failure_at = time.time()
            self._last_error = str(exc)
            self._discard_connection()
            logger.error('[SUBTITLE_HISTORY] persist failed record=%s', record_id, exc_info=True)
            return False

    def get(self, record_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection().execute('SELECT record_id, session_id, utterance_id, source_text, source_revision, is_final, recovered, created_at, updated_at FROM subtitle_source_history WHERE record_id=?', (str(record_id),)).fetchone()
        if row is None:
            return None
        keys = ('record_id', 'session_id', 'utterance_id', 'source_text', 'source_revision', 'is_final', 'recovered', 'created_at', 'updated_at')
        return dict(zip(keys, row))

    def close(self) -> None:
        self._discard_connection()

    def health_snapshot(self) -> Dict[str, Any]:
        return {'component': 'subtitle_history', 'state': 'failed' if self._consecutive_write_failures else 'running', 'write_failures': int(self._write_failures), 'consecutive_write_failures': int(self._consecutive_write_failures), 'last_error': self._last_error, 'last_success_at': self._last_success_at, 'last_failure_at': self._last_failure_at}

class SubtitleHistoryWriter:
    """Non-blocking SQLite writer for terminal source subtitle events."""

    def __init__(self, store: SubtitleHistoryStore, *, recovery_spill: Optional[Callable[[str, str, Any], bool]]=None, max_queue_size: int=2048):
        self.store = store
        self._recovery_spill = recovery_spill
        self._queue: queue.Queue = queue.Queue(maxsize=max(64, int(max_queue_size)))
        self._stop = Event()
        self._closed = Event()
        self._write_failures = 0
        self._consecutive_write_failures = 0
        self._thread = Thread(target=self._run, name='SubtitleHistoryWriter', daemon=False)
        self._thread.start()

    def _spill(self, reason: str, event_name: str, event: Any) -> bool:
        if event is None or self._recovery_spill is None:
            return False
        try:
            return bool(self._recovery_spill(reason, event_name, event))
        except Exception:
            logger.error('[SUBTITLE_HISTORY_WRITER] recovery spill failed', exc_info=True)
            return False

    def submit(self, payload: Dict[str, Any], *, fallback_reason: str='', fallback_event_name: str='', fallback_event: Any=None) -> bool:
        if self._closed.is_set():
            return self._spill(fallback_reason, fallback_event_name, fallback_event)
        item = (dict(payload), str(fallback_reason), str(fallback_event_name), fallback_event)
        try:
            self._queue.put(item, timeout=0.01)
            return True
        except queue.Full:
            logger.error('[SUBTITLE_HISTORY_WRITER] queue full; spilling terminal event')
            return self._spill(fallback_reason, fallback_event_name, fallback_event)

    def _run(self) -> None:
        while True:
            if self._stop.is_set() and self._queue.empty():
                self._closed.set()
                return
            try:
                payload, reason, event_name, event = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                ok = bool(self.store.persist(**payload))
                if ok:
                    self._consecutive_write_failures = 0
                else:
                    self._write_failures += 1
                    self._consecutive_write_failures += 1
                    if not self._spill(reason, event_name, event):
                        logger.critical('[SUBTITLE_HISTORY_WRITER] history and recovery persistence both failed record=%s', payload.get('record_id', ''))
            except Exception:
                self._write_failures += 1
                self._consecutive_write_failures += 1
                logger.error('[SUBTITLE_HISTORY_WRITER] task failed', exc_info=True)
                self._spill(reason, event_name, event)
            finally:
                self._queue.task_done()

    def flush(self, timeout: float=5.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return self._queue.unfinished_tasks == 0

    def close(self, *, drain: bool=True, timeout: float=5.0) -> bool:
        if drain:
            self.flush(timeout=max(0.1, timeout * 0.8))
        else:
            while True:
                try:
                    _payload, reason, event_name, event = self._queue.get_nowait()
                except queue.Empty:
                    break
                else:
                    self._spill(reason, event_name, event)
                    self._queue.task_done()
        self._stop.set()
        if current_thread() is not self._thread:
            self._thread.join(timeout=max(0.1, float(timeout)))
        self._closed.set()
        return not self._thread.is_alive()

    def metrics(self) -> Dict[str, Any]:
        return {'component': 'subtitle_history_writer', 'state': 'failed' if self._consecutive_write_failures else 'running', 'pending_tasks': self._queue.qsize(), 'write_failures': int(self._write_failures), 'consecutive_write_failures': int(self._consecutive_write_failures), 'worker_alive': self._thread.is_alive()}

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

    def update(self, *, received: int=-1, processed: int=-1, decoded: int=-1, confirmed: int=-1, retention_samples: int=0) -> None:
        if received >= 0:
            self.received_sample = max(self.received_sample, int(received))
        if processed >= 0:
            self.processed_sample = max(self.processed_sample, int(processed))
        if decoded >= 0:
            self.decoded_sample = max(self.decoded_sample, int(decoded))
        if confirmed >= 0:
            self.confirmed_sample = max(self.confirmed_sample, int(confirmed))
        self.retained_from_sample = max(0, self.confirmed_sample - max(0, int(retention_samples)))

class PipelineHealth:
    NORMAL = 'normal'
    WARNING = 'warning'
    DEGRADED = 'degraded'
    CRITICAL = 'critical'
    STOPPED = 'stopped'
    _SEVERITY = {NORMAL: 0, WARNING: 1, DEGRADED: 2, CRITICAL: 3, STOPPED: 4}

    def __init__(self, warning: int=20, degraded: int=50, critical: int=100):
        self.warning = int(warning)
        self.degraded = int(degraded)
        self.critical = int(critical)
        self.state = self.NORMAL
        self.last_transition_at = time.time()
        self.reason = ''
        self._lock = Lock()

    def evaluate(self, pending_finals: int, oldest_final_age_ms: int=0, recovery_pending: int=0, component_snapshots: Optional[List[Dict[str, Any]]]=None) -> str:
        value = max(int(pending_finals), int(recovery_pending))
        queue_state = self.CRITICAL if value >= self.critical else self.DEGRADED if value >= self.degraded else self.WARNING if value >= self.warning else self.NORMAL
        if oldest_final_age_ms >= 30000 and queue_state == self.NORMAL:
            queue_state = self.WARNING
        chosen = queue_state
        component_reason = ''
        for snapshot in component_snapshots or []:
            raw_state = str(snapshot.get('state', self.NORMAL)).lower()
            mapped = {'failed': self.CRITICAL, 'critical': self.CRITICAL, 'degraded': self.DEGRADED, 'warning': self.WARNING, 'stopped': self.STOPPED, 'running': self.NORMAL, 'normal': self.NORMAL, 'starting': self.NORMAL}.get(raw_state, self.WARNING)
            if self._SEVERITY.get(mapped, 1) > self._SEVERITY.get(chosen, 0):
                chosen = mapped
                component_reason = f"component={snapshot.get('component', 'unknown')} state={raw_state}"
        reason = component_reason or f'pending_finals={pending_finals} recovery_pending={recovery_pending} oldest_ms={oldest_final_age_ms}'
        with self._lock:
            if chosen != self.state:
                self.state = chosen
                self.last_transition_at = time.time()
            self.reason = reason
            return self.state

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {'state': self.state, 'reason': self.reason, 'last_transition_at': self.last_transition_at}

def _is_final_event(event: Any) -> bool:
    return bool(getattr(event, 'is_final', False) and getattr(event, 'complete', True))

def _preview_event_key(event_name: str, event: Any) -> tuple[Any, ...]:
    return (str(event_name), getattr(event, 'session_id', ''), getattr(event, 'utterance_id', 0), type(event).__name__)

class _EventSubscriberWorker:

    def __init__(self, event_name: str, callback: Callable[[Any], None], *, max_pending_finals: int, recovery_spill: Optional[Callable[[str, str, Any], bool]], session_guard: Optional[Callable[[Any], bool]], final_burst: int):
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
        self._thread = Thread(target=self._run, name=f'SubtitleEventSubscriber-{self.event_name}', daemon=True)
        self._thread.start()

    def _spill_final(self, reason: str, event: Any) -> bool:
        if not _is_final_event(event) or self._spill is None:
            return False
        return bool(self._spill(reason, self.event_name, event))

    def enqueue(self, event: Any, *, timeout: float=0.0) -> bool:
        del timeout
        spill_reason = ''
        with self._condition:
            if not self._accepting:
                spill_reason = 'subscriber_not_accepting'
            elif _is_final_event(event):
                if len(self._finals) >= self._max_pending_finals:
                    spill_reason = 'subscriber_final_backlog'
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
                while self._accepting and (not self._finals) and (not self._previews):
                    self._condition.wait(timeout=EVENT_SUBSCRIBER_CONDITION_TIMEOUT)
                abandoned: List[Any] = []
                if not self._accepting:
                    if not self._drain:
                        abandoned = list(self._finals)
                        self._finals.clear()
                        self._previews.clear()
                        item = None
                    elif not self._finals and (not self._previews):
                        return
                    else:
                        item = self._next_locked()
                else:
                    item = self._next_locked()
            if abandoned:
                for abandoned_item in abandoned:
                    self._spill_final('subscriber_close_without_drain', abandoned_item)
                return
            if item is None:
                continue
            if self._session_guard is not None and (not self._session_guard(item)):
                self._spill_final('subscriber_guard_rejected', item)
                continue
            try:
                self.callback(item)
            except Exception:
                logger.error('[SESSION_EVENT] event=%s callback=%r failed', self.event_name, self.callback, exc_info=True)
                self._spill_final('subscriber_callback_failed', item)

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
            self._spill_final('subscriber_close_without_drain', event)
        if current_thread() is not self._thread:
            self._thread.join(timeout=max(0.0, float(timeout)))
        if self._thread.is_alive():
            with self._condition:
                remaining = list(self._finals)
                self._finals.clear()
            for event in remaining:
                self._spill_final('subscriber_close_timeout', event)
        return not self._thread.is_alive()

    def metrics(self) -> Dict[str, Any]:
        with self._condition:
            oldest_final_age_ms = 0
            if self._finals:
                emitted = float(getattr(self._finals[0], 'emitted_at', time.monotonic()))
                oldest_final_age_ms = max(0, int((time.monotonic() - emitted) * 1000))
            return {'pending_finals': len(self._finals), 'pending_previews': len(self._previews), 'oldest_final_age_ms': oldest_final_age_ms, 'worker_alive': self._thread.is_alive()}

class SessionEventBus:
    RUNNING = 'running'
    DRAINING = 'draining'
    CLOSED = 'closed'

    def __init__(self, max_queue_size: int=512, *, max_pending_finals: int=200, recovery_path: str='', final_burst: int=8):
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
        self._dispatcher = Thread(target=self._dispatch_loop, name='SubtitleSessionEvents', daemon=False)
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
        return self._journal.append(reason, event_name, event)

    def acknowledge_recovery(self, record_ids: List[str]) -> bool:
        if self._journal is None:
            return False
        if self._recovery_spooler is not None:
            return self._recovery_spooler.acknowledge_durable(record_ids)
        _count, ok = self._journal.acknowledge_with_status(record_ids)
        return ok

    def pending_recovery_records(self, *, limit: int=1000) -> List[Dict[str, Any]]:
        if self._journal is None:
            return []
        if self._recovery_spooler is not None:
            self._recovery_spooler.flush(timeout=5.0)
        return self._journal.load_pending(limit=limit)

    def subscribe(self, event_name: str, callback: Callable[[Any], None], *, session_guard: Optional[Callable[[Any], bool]]=None) -> None:
        event_name = str(event_name)
        with self._condition:
            if self._state != self.RUNNING:
                raise RuntimeError('SessionEventBus 已停止接收订阅')
            workers = self._subscribers.setdefault(event_name, {})
            if callback not in workers:
                workers[callback] = _EventSubscriberWorker(event_name, callback, max_pending_finals=self._max_pending_finals, recovery_spill=self.spill_final, session_guard=session_guard, final_burst=self._final_burst)

    def unsubscribe(self, event_name: str, callback: Callable[[Any], None]) -> None:
        with self._condition:
            worker = self._subscribers.get(str(event_name), {}).pop(callback, None)
        if worker is not None:
            worker.close(drain=False, timeout=0.5)

    def publish(self, event_name: str, event: Any) -> bool:
        name = str(event_name)
        spill_reason = ''
        accepted = False
        with self._condition:
            if self._state != self.RUNNING:
                spill_reason = 'eventbus_not_running'
            elif _is_final_event(event):
                if len(self._finals) >= self._max_pending_finals:
                    spill_reason = 'central_final_backlog'
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
            if spill_reason == 'central_final_backlog':
                logger.error('[SESSION_FINAL_BACKLOG] event=%s pending=%d', name, self._max_pending_finals)
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
                while self._state == self.RUNNING and (not self._finals) and (not self._previews):
                    self._condition.wait(timeout=0.5)
                if self._state in (self.DRAINING, self.CLOSED) and (not self._finals) and (not self._previews):
                    self._state = self.CLOSED
                    self._condition.notify_all()
                    return
                item = self._next_locked()
                workers = list(self._subscribers.get(item[0], {}).values()) if item else []
            if item is None:
                continue
            event_name, event = item
            if not workers:
                self.spill_final('no_subscriber', event_name, event)
                continue
            for worker in workers:
                if not worker.enqueue(event):
                    logger.error('[SESSION_SUBSCRIBER_REJECT] event=%s callback=%r', event_name, worker.callback)

    def metrics(self) -> Dict[str, Any]:
        with self._condition:
            subscribers = {name: [worker.metrics() for worker in workers.values()] for name, workers in self._subscribers.items()}
            central_oldest = 0
            if self._finals:
                emitted = float(getattr(self._finals[0][1], 'emitted_at', time.monotonic()))
                central_oldest = max(0, int((time.monotonic() - emitted) * 1000))
            spooler_metrics = self._recovery_spooler.metrics() if self._recovery_spooler is not None else {}
            recovery_pending = (self._journal.pending_count if self._journal is not None else 0) + int(spooler_metrics.get('pending_tasks', 0))
            subscriber_finals = sum((int(item.get('pending_finals', 0)) for group in subscribers.values() for item in group))
            subscriber_previews = sum((int(item.get('pending_previews', 0)) for group in subscribers.values() for item in group))
            subscriber_oldest = max([int(item.get('oldest_final_age_ms', 0)) for group in subscribers.values() for item in group] or [0])
            return {'state': self._state, 'central_pending_finals': len(self._finals), 'subscriber_pending_finals': subscriber_finals, 'pending_finals': len(self._finals) + subscriber_finals, 'pending_previews': len(self._previews) + subscriber_previews, 'oldest_final_age_ms': max(central_oldest, subscriber_oldest), 'recovery_pending': recovery_pending, 'dispatcher_alive': self._dispatcher.is_alive(), 'recovery_spooler': spooler_metrics, 'subscribers': subscribers}

    def close(self, *, drain: bool=True, timeout: float=5.0) -> bool:
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
            self.spill_final('eventbus_close_without_drain', event_name, event)
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
            stopped = worker.close(drain=drain, timeout=max(0.0, deadline - time.monotonic()))
            workers_stopped = stopped and workers_stopped
        spooler_stopped = True
        if self._recovery_spooler is not None:
            spooler_stopped = self._recovery_spooler.close(drain=True, timeout=max(0.1, deadline - time.monotonic()))
        return dispatcher_stopped and workers_stopped and spooler_stopped
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')

def configure_hidden_windows_child_processes() -> None:
    """Use the windowless Python executable for multiprocessing children."""
    if not sys.platform.startswith('win') or getattr(sys, 'frozen', False) or '__compiled__' in globals():
        return
    candidates = []
    executable_dir = os.path.dirname(os.path.abspath(sys.executable))
    candidates.append(os.path.join(executable_dir, 'pythonw.exe'))
    candidates.append(os.path.join(sys.exec_prefix, 'pythonw.exe'))
    for candidate in candidates:
        if os.path.isfile(candidate):
            multiprocessing.set_executable(candidate)
            return
configure_hidden_windows_child_processes()
APP_NAME = 'NemoSubtitle'
APP_VERSION = '0.8.0-asr-only'
_source_dir = os.path.dirname(os.path.realpath(__file__))
_is_compiled = bool(getattr(sys, 'frozen', False) or '__compiled__' in globals())
_resource_dir = os.path.dirname(os.path.abspath(sys.executable)) if _is_compiled else _source_dir
_portable_mode = os.path.isfile(os.path.join(_resource_dir, 'portable.flag'))

def _default_user_data_dir() -> str:
    if sys.platform.startswith('win'):
        base = os.environ.get('LOCALAPPDATA', os.path.join(os.path.expanduser('~'), 'AppData', 'Local'))
        return os.path.join(base, APP_NAME)
    base = os.environ.get('XDG_DATA_HOME', os.path.join(os.path.expanduser('~'), '.local', 'share'))
    return os.path.join(base, APP_NAME)

def _directory_is_writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(prefix='.write-test-', dir=path, delete=True)
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
_logs_dir = os.path.join(_data_dir, 'logs')
_models_dir = os.path.join(_data_dir, 'models')
_legacy_models_dir = os.path.join(_resource_dir, 'models')
os.makedirs(_logs_dir, exist_ok=True)
os.makedirs(_models_dir, exist_ok=True)
if _is_compiled and (not _portable_mode):
    os.environ.setdefault('HF_HOME', os.path.join(_data_dir, 'huggingface'))
os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
_script_dir = _resource_dir
_log_file = os.path.join(_logs_dir, 'subtitle.log')
_model_revision_manifest_path = os.path.join(_data_dir, 'model-revisions.json')
_settings_path = os.path.join(_data_dir, 'settings.json')
_model_revision_lock = Lock()
_settings_lock = Lock()

def _load_model_revision_manifest() -> Dict[str, str]:
    try:
        with open(_model_revision_manifest_path, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            return {}
        return {str(key): str(value) for key, value in payload.items() if isinstance(key, str) and isinstance(value, str) and value}
    except Exception:
        return {}

def _model_revision(model_id: str, explicit_revision: str='') -> str:
    explicit = str(explicit_revision or '').strip()
    if explicit:
        return explicit
    with _model_revision_lock:
        return _load_model_revision_manifest().get(str(model_id), '')

def _snapshot_revision_from_path(snapshot_path: str) -> str:
    candidate = os.path.basename(os.path.normpath(str(snapshot_path)))
    if len(candidate) >= 7 and all((char in '0123456789abcdefABCDEF' for char in candidate)):
        return candidate.lower()
    return ''

def _record_model_revision(model_id: str, snapshot_path: str) -> str:
    revision = _snapshot_revision_from_path(snapshot_path)
    if not revision:
        return ''
    with _model_revision_lock:
        payload = _load_model_revision_manifest()
        if payload.get(str(model_id)) == revision:
            return revision
        payload[str(model_id)] = revision
        temporary = f'{_model_revision_manifest_path}.tmp'
        try:
            with open(temporary, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(temporary, _model_revision_manifest_path)
        except Exception:
            logger.debug('模型 revision manifest 写入失败', exc_info=True)
            try:
                os.remove(temporary)
            except OSError:
                pass
    return revision

def _candidate_hf_hub_dirs() -> List[str]:
    """Return every plausible Hugging Face hub cache, newest configuration first.

    Older source, packaged and portable builds used different cache roots.  Cache
    discovery must therefore not assume that the current process environment is
    the same one that originally downloaded the model.
    """
    candidates: List[str] = []

    def add(path: str) -> None:
        path = os.path.abspath(os.path.expanduser(str(path or '')))
        if path and path not in candidates:
            candidates.append(path)
    configured_hub = os.environ.get('HUGGINGFACE_HUB_CACHE', '')
    if configured_hub:
        add(configured_hub)
    configured_home = os.environ.get('HF_HOME', '')
    if configured_home:
        add(os.path.join(configured_home, 'hub'))
    add(os.path.join(os.path.expanduser('~'), '.cache', 'huggingface', 'hub'))
    add(os.path.join(_data_dir, 'huggingface', 'hub'))
    add(os.path.join(_resource_dir, 'huggingface', 'hub'))
    add(os.path.join(_resource_dir, '.cache', 'huggingface', 'hub'))
    if sys.platform.startswith('win'):
        local_app_data = os.environ.get('LOCALAPPDATA', '')
        if local_app_data:
            add(os.path.join(local_app_data, APP_NAME, 'huggingface', 'hub'))
    return candidates

def _hf_repo_cache_dir(hub_dir: str, model_id: str) -> str:
    return os.path.join(hub_dir, 'models--' + str(model_id).replace('/', '--'))

def _iter_local_hf_snapshots(model_id: str, revision: str=''):
    """Yield local snapshots without contacting Hugging Face or relying on refs."""
    seen = set()
    wanted = str(revision or '').strip()
    for hub_dir in _candidate_hf_hub_dirs():
        repo_dir = _hf_repo_cache_dir(hub_dir, model_id)
        snapshots_dir = os.path.join(repo_dir, 'snapshots')
        ordered: List[str] = []
        if wanted:
            direct = os.path.join(snapshots_dir, wanted)
            ordered.append(direct)
            ref_path = os.path.join(repo_dir, 'refs', wanted.replace('/', os.sep))
            try:
                with open(ref_path, 'r', encoding='utf-8') as handle:
                    commit = handle.read().strip()
                if commit:
                    ordered.append(os.path.join(snapshots_dir, commit))
            except OSError:
                pass
        try:
            snapshots = [os.path.join(snapshots_dir, name) for name in os.listdir(snapshots_dir) if os.path.isdir(os.path.join(snapshots_dir, name))]
            snapshots.sort(key=lambda item: os.path.getmtime(item), reverse=True)
            ordered.extend(snapshots)
        except OSError:
            pass
        for snapshot in ordered:
            normalized = os.path.normcase(os.path.abspath(snapshot))
            if normalized in seen or not os.path.isdir(snapshot):
                continue
            seen.add(normalized)
            yield snapshot

def _snapshot_available_files(snapshot_path: str) -> tuple[set[str], set[str]]:
    available = {os.path.relpath(os.path.join(root, name), snapshot_path).replace('\\', '/') for root, _, names in os.walk(snapshot_path) for name in names}
    return (available, {os.path.basename(name) for name in available})

def _local_snapshot_matches(snapshot_path: str, *, required_all: tuple[str, ...]=(), required_any: tuple[str, ...]=(), require_weights: bool=False) -> bool:
    try:
        available, basenames = _snapshot_available_files(snapshot_path)
        if any((name not in available and name not in basenames for name in required_all)):
            return False
        if required_any and (not any((name in available or name in basenames for name in required_any))):
            return False
        if require_weights and (not any((name.endswith(('.safetensors', '.bin')) for name in available))):
            return False
        return True
    except OSError:
        return False

def _find_local_hf_snapshot(model_id: str, *, revision: str='', required_all: tuple[str, ...]=(), required_any: tuple[str, ...]=(), require_weights: bool=False) -> str:
    passes = [str(revision or '').strip(), ''] if revision else ['']
    visited = set()
    for preferred_revision in passes:
        for snapshot in _iter_local_hf_snapshots(model_id, preferred_revision):
            key = os.path.normcase(os.path.abspath(snapshot))
            if key in visited:
                continue
            visited.add(key)
            if _local_snapshot_matches(snapshot, required_all=required_all, required_any=required_any, require_weights=require_weights):
                return snapshot
    return ''

def _activate_best_existing_hf_cache(model_ids: List[str]) -> str:
    """Select the cache root containing the most requested repositories."""
    best_dir = ''
    best_score = 0
    for hub_dir in _candidate_hf_hub_dirs():
        score = sum((os.path.isdir(os.path.join(_hf_repo_cache_dir(hub_dir, model_id), 'snapshots')) for model_id in model_ids if model_id))
        if score > best_score:
            best_score = score
            best_dir = hub_dir
    if best_dir and best_score:
        current = os.environ.get('HUGGINGFACE_HUB_CACHE', '')
        if os.path.normcase(os.path.abspath(current or '.')) != os.path.normcase(os.path.abspath(best_dir)):
            os.environ['HUGGINGFACE_HUB_CACHE'] = best_dir
            logger.info('[HF_CACHE] 复用已有缓存目录：%s（匹配 %d 个仓库）', best_dir, best_score)
    return best_dir
_is_main_process = multiprocessing.current_process().name == 'MainProcess'
_logging_handlers: List[logging.Handler] = [logging.NullHandler()]
if _is_main_process:
    try:
        _logging_handlers = [RotatingFileHandler(_log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding='utf-8')]
    except Exception:
        _logging_handlers = [logging.NullHandler()]
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] [%(processName)s/%(threadName)s] %(name)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S', handlers=_logging_handlers)
logger = logging.getLogger(__name__)

def _load_application_settings() -> Dict[str, Any]:
    """Load non-sensitive UI/runtime preferences and quarantine malformed files."""
    with _settings_lock:
        try:
            with open(_settings_path, 'r', encoding='utf-8') as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                raise ValueError('设置文件顶层必须是 JSON 对象')
            return payload
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning('设置文件损坏或不可读，将使用默认值：%s', exc)
            try:
                suffix = time.strftime('%Y%m%d-%H%M%S')
                os.replace(_settings_path, f'{_settings_path}.corrupt-{suffix}')
            except OSError:
                logger.debug('损坏设置文件隔离失败', exc_info=True)
            return {}

def _save_application_settings(payload: Dict[str, Any]) -> None:
    """Atomically persist whitelisted preferences; never write model text or secrets."""
    with _settings_lock:
        os.makedirs(os.path.dirname(_settings_path), exist_ok=True)
        temporary = f'{_settings_path}.{os.getpid()}.tmp'
        try:
            with open(temporary, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, _settings_path)
        except Exception:
            logger.error('设置保存失败', exc_info=True)
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise

def _apply_persisted_settings_to_args(args: argparse.Namespace, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Apply validated ASR/audio/display preferences while preserving explicit CLI overrides."""
    if not payload:
        return {}
    candidate = argparse.Namespace(**vars(args))
    explicit = set(getattr(args, '_explicit_dests', set()) or set())
    audio = payload.get('audio', {})
    display = payload.get('display', {})
    if not isinstance(audio, dict) or not isinstance(display, dict):
        logger.warning('设置文件结构无效，已忽略')
        return {}
    direct_audio = {'vad_threshold': float, 'min_silence_duration': float, 'min_speech_duration': float, 'speech_preroll_ms': int, 'max_utterance_seconds': float, 'forced_segment_overlap_ms': int, 'endpoint_punctuation_hold_ms': int, 'endpoint_min_utterance_ms': int, 'max_drain_chunks': int}
    try:
        for key, converter in direct_audio.items():
            if key in audio and key not in explicit:
                setattr(candidate, key, converter(audio[key]))
        if 'enable_hybrid_endpoint' in audio and 'disable_hybrid_endpoint' not in explicit:
            candidate.disable_hybrid_endpoint = not bool(audio['enable_hybrid_endpoint'])
        validate_args(candidate)
    except Exception as exc:
        logger.warning('已保存的运行参数无效，整组忽略：%s', exc)
        return {}
    for key in direct_audio:
        setattr(args, key, getattr(candidate, key))
    args.disable_hybrid_endpoint = candidate.disable_hybrid_endpoint
    safe_display = {'source_font_size': max(12, min(72, int(display.get('source_font_size', 30)))), 'opacity': max(0.5, min(1.0, float(display.get('opacity', 0.95)))), 'topmost': bool(display.get('topmost', True))}
    logger.info('已恢复保存的实时字幕设置')
    return safe_display
NEMOTRON_MODEL_ID = 'nvidia/nemotron-3.5-asr-streaming-0.6b'

def is_huggingface_model_cached(model_id: str, revision: str='') -> bool:
    """Validate Nemotron by scanning all current and legacy local caches first."""
    resolved_revision = _model_revision(model_id, revision)
    required = ('config.json', 'processor_config.json')
    snapshot_path = _find_local_hf_snapshot(model_id, revision=resolved_revision, required_all=required, require_weights=True)
    if snapshot_path:
        _record_model_revision(model_id, snapshot_path)
        logger.info('[HF_CACHE] 已复用 ASR 缓存：%s', snapshot_path)
        return True
    try:
        from huggingface_hub import snapshot_download
        for candidate_revision in (resolved_revision, '') if resolved_revision else ('',):
            kwargs: Dict[str, Any] = {'repo_id': model_id, 'local_files_only': True}
            if candidate_revision:
                kwargs['revision'] = candidate_revision
            try:
                snapshot_path = snapshot_download(**kwargs)
            except Exception:
                continue
            if _local_snapshot_matches(snapshot_path, required_all=required, require_weights=True):
                _record_model_revision(model_id, snapshot_path)
                return True
    except Exception:
        pass
    return False

def is_huggingface_snapshot_cached(model_id: str, *, required_all: tuple[str, ...]=(), required_any: tuple[str, ...]=(), allow_patterns: Optional[tuple[str, ...]]=None, revision: str='') -> bool:
    """Validate a snapshot across active and legacy caches without network access."""
    resolved_revision = _model_revision(model_id, revision)
    snapshot_path = _find_local_hf_snapshot(model_id, revision=resolved_revision, required_all=required_all, required_any=required_any)
    if snapshot_path:
        _record_model_revision(model_id, snapshot_path)
        logger.info('[HF_CACHE] 已复用模型缓存 %s：%s', model_id, snapshot_path)
        return True
    try:
        from huggingface_hub import snapshot_download
        for candidate_revision in (resolved_revision, '') if resolved_revision else ('',):
            kwargs: Dict[str, Any] = {'repo_id': model_id, 'local_files_only': True}
            if candidate_revision:
                kwargs['revision'] = candidate_revision
            if allow_patterns:
                kwargs['allow_patterns'] = list(allow_patterns)
            try:
                snapshot_path = snapshot_download(**kwargs)
            except Exception:
                continue
            if _local_snapshot_matches(snapshot_path, required_all=required_all, required_any=required_any):
                _record_model_revision(model_id, snapshot_path)
                return True
    except Exception:
        pass
    return False

def _resolve_local_hf_snapshot(model_id: str, *, revision: str='', required_all: tuple[str, ...]=(), required_any: tuple[str, ...]=(), allow_patterns: Optional[tuple[str, ...]]=None, require_weights: bool=False) -> str:
    """Return an exact complete local snapshot path without contacting the network."""
    resolved_revision = _model_revision(model_id, revision)
    snapshot_path = _find_local_hf_snapshot(model_id, revision=resolved_revision, required_all=required_all, required_any=required_any, require_weights=require_weights)
    if snapshot_path:
        _record_model_revision(model_id, snapshot_path)
        return os.path.abspath(snapshot_path)
    try:
        from huggingface_hub import snapshot_download
        for candidate_revision in (resolved_revision, '') if resolved_revision else ('',):
            kwargs: Dict[str, Any] = {'repo_id': model_id, 'local_files_only': True}
            if candidate_revision:
                kwargs['revision'] = candidate_revision
            if allow_patterns:
                kwargs['allow_patterns'] = list(allow_patterns)
            try:
                candidate = snapshot_download(**kwargs)
            except Exception:
                continue
            if _local_snapshot_matches(candidate, required_all=required_all, required_any=required_any, require_weights=require_weights):
                _record_model_revision(model_id, candidate)
                return os.path.abspath(candidate)
    except Exception:
        logger.debug('本地 Hugging Face snapshot 解析失败：%s', model_id, exc_info=True)
    return ''

def _publish_runtime_model_paths(args: argparse.Namespace) -> None:
    """Pin the ASR worker to the exact local snapshot validated by cache discovery."""
    args.asr_model_load_path = _resolve_local_hf_snapshot(args.asr_model, revision=getattr(args, 'asr_model_revision', ''), required_all=('config.json', 'processor_config.json'), require_weights=True) or args.asr_model
    logger.info('[HF_CACHE] ASR 实际加载路径：%s', args.asr_model_load_path)

def _snapshot_download_worker(model_id: str, result_queue, allow_patterns: Optional[tuple[str, ...]]=None, revision: str='') -> None:
    """Run snapshot_download in a killable child process for responsive cancellation."""
    try:
        from huggingface_hub import snapshot_download
        kwargs: Dict[str, Any] = {'repo_id': model_id}
        if allow_patterns:
            kwargs['allow_patterns'] = list(allow_patterns)
        if revision:
            kwargs['revision'] = revision
        snapshot_path = snapshot_download(**kwargs)
        result_queue.put((True, str(snapshot_path)))
    except BaseException as exc:
        try:
            result_queue.put((False, str(exc)))
        except Exception:
            pass

def download_huggingface_snapshot(model_id: str, cancel_event: Event, *, allow_patterns: Optional[tuple[str, ...]]=None, revision: str='') -> str:
    ctx = multiprocessing.get_context('spawn')
    result_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(target=_snapshot_download_worker, args=(model_id, result_queue, allow_patterns, revision), name='SubtitleModelDownloader')
    process.start()
    try:
        while process.is_alive():
            if cancel_event.wait(0.2):
                process.terminate()
                process.join(timeout=2.0)
                if process.is_alive() and hasattr(process, 'kill'):
                    process.kill()
                    process.join(timeout=0.5)
                raise RuntimeError('资源下载已取消')
        process.join(timeout=0.5)
        if process.exitcode != 0:
            raise RuntimeError(f'模型下载进程异常退出：{process.exitcode}')
        try:
            ok, message = result_queue.get(timeout=1.0)
        except queue.Empty as exc:
            raise RuntimeError('模型下载进程未返回结果') from exc
        if not ok:
            raise RuntimeError(message or f'模型下载失败：{model_id}')
        recorded = _record_model_revision(model_id, str(message))
        return recorded or str(revision or '')
    finally:
        try:
            result_queue.close()
            result_queue.join_thread()
        except Exception:
            pass

def ensure_first_run_assets(root: tk.Tk, args: argparse.Namespace) -> bool:
    """Validate or download the Nemotron ASR asset before realtime processing starts."""
    args.asr_model_revision = _model_revision(args.asr_model, getattr(args, 'asr_model_revision', ''))
    _activate_best_existing_hf_cache([args.asr_model])
    asr_ready = is_huggingface_model_cached(args.asr_model, revision=args.asr_model_revision)
    if asr_ready:
        _publish_runtime_model_paths(args)
        return True
    required_gb = 3.1
    free_gb = shutil.disk_usage(_data_dir).free / 1024 ** 3
    if free_gb < required_gb + 1.5:
        messagebox.showerror('磁盘空间不足', f'Nemotron 模型安装最多需要 {required_gb + 1.5:.1f} GB，当前只有 {free_gb:.1f} GB。', parent=root)
        return False
    if not messagebox.askyesno('首次运行配置', '需要下载 Nemotron 3.5 ASR 模型（约 3.1 GB）。\n\n下载并验证后可离线使用。\n\n是否继续？', parent=root):
        return False
    dialog = tk.Toplevel(root)
    dialog.title('正在准备 ASR 模型')
    dialog.geometry('560x180')
    dialog.resizable(False, False)
    dialog.transient(root)
    dialog.grab_set()
    status_var = tk.StringVar(value='准备下载 Nemotron ASR...')
    tk.Label(dialog, textvariable=status_var, anchor='w', wraplength=520).pack(fill='x', padx=20, pady=(22, 12))
    progress = ttk.Progressbar(dialog, mode='indeterminate')
    progress.pack(fill='x', padx=20, pady=8)
    progress.start(12)
    cancel_event = Event()
    events: queue.Queue = queue.Queue()
    result = {'ok': False, 'done': False}

    def install() -> None:
        try:
            events.put(('status', '正在下载 Nemotron 3.5 ASR（约 3.1 GB）...'))
            args.asr_model_revision = download_huggingface_snapshot(args.asr_model, cancel_event, revision=args.asr_model_revision)
            if cancel_event.is_set():
                raise RuntimeError('资源准备已取消')
            if not is_huggingface_model_cached(args.asr_model, revision=args.asr_model_revision):
                raise RuntimeError('Nemotron 模型校验失败')
            _publish_runtime_model_paths(args)
            events.put(('done', True, 'ASR 模型准备完成'))
        except Exception as exc:
            logger.error('首次资源准备失败：%s', exc, exc_info=True)
            events.put(('done', False, str(exc)))

    def poll() -> None:
        try:
            while True:
                event = events.get_nowait()
                if event[0] == 'status':
                    status_var.set(event[1])
                elif event[0] == 'done':
                    result['ok'] = bool(event[1])
                    result['done'] = True
                    progress.stop()
                    if result['ok']:
                        dialog.destroy()
                    else:
                        status_var.set(f'准备失败：{event[2]}')
                        cancel_button.config(text='关闭')
        except queue.Empty:
            pass
        if dialog.winfo_exists() and (not result['done']):
            dialog.after(100, poll)

    def cancel() -> None:
        if result['done']:
            dialog.destroy()
            return
        if messagebox.askyesno('取消准备', '确定取消模型下载吗？', parent=dialog):
            cancel_event.set()
            status_var.set('正在取消...')
    cancel_button = tk.Button(dialog, text='取消', command=cancel, width=10)
    cancel_button.pack(pady=(12, 8))
    dialog.protocol('WM_DELETE_WINDOW', cancel)
    Thread(target=install, name='FirstRunModelInstaller', daemon=True).start()
    dialog.after(100, poll)
    root.wait_window(dialog)
    return bool(result['ok'])
torch: Any = None
np: Any = None
_runtime_import_error = None
_audio_import_error = None
pyaudio_backend: Any = None
sherpa_onnx: Any = None
scipy_signal: Any = None
TORCH_CONFIG_LOCK = Lock()
_torch_runtime_config: Optional[tuple[int, int, int]] = None
TAG_PATTERN = re.compile('<\\|.*?\\|>')
JP_SPACE_PATTERN = re.compile('(?<=[\\u3040-\\u30ff\\u3400-\\u9fff])\\s+(?=[\\u3040-\\u30ff\\u3400-\\u9fff])')
JP_PUNCT_BEFORE_SPACE_PATTERN = re.compile('\\s+([、。！？!?」』）】〉》])')
JP_PUNCT_AFTER_SPACE_PATTERN = re.compile('([「『（【〈《])\\s+')
JP_PUNCT_TO_JP_SPACE_PATTERN = re.compile('([、。！？!?])\\s+(?=[\\u3040-\\u30ff\\u3400-\\u9fff])')
SENTENCE_END_PATTERN = re.compile('(?<=[。！？!?])')

def normalize_subtitle_text(text: str, max_chars: Optional[int]=None) -> str:
    text = TAG_PATTERN.sub('', text or '')
    text = text.replace('\u3000', ' ')
    text = JP_SPACE_PATTERN.sub('', text)
    text = JP_PUNCT_BEFORE_SPACE_PATTERN.sub('\\1', text)
    text = JP_PUNCT_AFTER_SPACE_PATTERN.sub('\\1', text)
    text = JP_PUNCT_TO_JP_SPACE_PATTERN.sub('\\1', text)
    text = re.sub('\\s{2,}', ' ', text).strip()
    if max_chars is not None and len(text) > max_chars:
        text = text[-max_chars:]
    return text

def remove_repeated_segment_prefix(previous: str, current: str, max_chars: int=40, fuzzy_threshold: float=DEDUP_FUZZY_THRESHOLD, min_fuzzy_chars: int=6) -> tuple[str, int]:
    """Remove ASR text duplicated by forced-segment audio overlap.

    The boundary matcher is deliberately conservative: exact suffix/prefix matches
    win first, then equal-length fuzzy matching, then a variable-length alignment
    that tolerates a few kana/kanji insertions or deletions. Short repetitions are
    never fuzzily removed so legitimate phrases such as 「はい、はい」 survive.
    """
    previous = normalize_subtitle_text(previous)
    current = normalize_subtitle_text(current)
    max_chars = max(0, int(max_chars))
    if previous and previous == current and (len(current) <= max_chars):
        return ('', len(current))
    upper = min(max_chars, len(previous), len(current))
    for size in range(upper, 0, -1):
        if previous[-size:] == current[:size]:
            return (current[size:].lstrip('、，,。！？!? '), size)
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
        matching = sum((block.size for block in blocks))
        if blocks:
            first = blocks[0]
            last = blocks[-1]
            anchored_start = first.a <= 2 and first.b <= 2
            anchored_end = size - (last.a + last.size) <= 2 and size - (last.b + last.size) <= 2
        else:
            anchored_start = anchored_end = False
        if anchored_start and anchored_end and (ratio >= threshold) and (matching >= max(minimum, int(size * DEDUP_MIN_MATCHING_COVERAGE))):
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
                matching = sum((block.size for block in blocks))
                first = blocks[0]
                last = blocks[-1]
                anchored_start = first.a <= 2 and first.b <= 2
                anchored_end = left_size - (last.a + last.size) <= 2 and right_size - (last.b + last.size) <= 2
                coverage = matching / max(left_size, right_size)
                relaxed_threshold = max(DEDUP_VARIABLE_RELAXED_FLOOR, threshold - DEDUP_VARIABLE_RELAXED_OFFSET)
                if anchored_start and anchored_end and (ratio >= relaxed_threshold) and (coverage >= DEDUP_VARIABLE_MIN_COVERAGE) and (matching >= minimum):
                    trim_size = last.b + last.size
                    score = ratio + coverage * 0.2 + min(trim_size, 40) / 1000.0
                    candidate = (score, trim_size, matching)
                    if best_candidate is None or candidate > best_candidate:
                        best_candidate = candidate
        if best_candidate is not None:
            best_ratio = best_candidate[0]
            best_size = best_candidate[1]
    if best_size:
        logger.debug('[ASR_OVERLAP] fuzzy_dedup chars=%d score=%.3f', best_size, best_ratio)
        return (current[best_size:].lstrip('、，,。！？!? '), best_size)
    return (current, 0)
JP_COMMIT_BOUNDARY_CHARS = '。！？!?、，,；;：:'
JP_STRONG_UNIT_BOUNDARY_CHARS = '。！？!?'
JP_WEAK_UNIT_BOUNDARY_CHARS = '、，,；;：:'
TARGET_UNIT_PUNCTUATION_CHARS = '。！？!?.,，、；;：:…—-~～・·()（）[]【】{}<>《》〈〉「」『』"\'“”‘’'

def longest_common_prefix(texts: List[str]) -> str:
    if not texts:
        return ''
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

def stable_semantic_prefix(stable_prefix: str, current_text: str, tail_guard_chars: int=0) -> str:
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
        return ''
    if stable == current:
        return stable
    last_boundary = 0
    for index, char in enumerate(stable):
        if char in '。！？!?':
            last_boundary = index + 1
    guard = max(0, int(tail_guard_chars))
    guarded_end = len(stable) - guard if guard else len(stable)
    guarded_end = max(0, guarded_end)
    if last_boundary > 0:
        if guarded_end >= last_boundary + 4:
            return stable[:guarded_end]
        return stable[:last_boundary]
    if guard and guarded_end >= 4:
        return stable[:guarded_end]
    return ''

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
    """Immutable source-subtitle event shared by ASR and UI."""
    session_id: str
    utterance_id: int
    revision: int
    state: str
    text: str
    committed_text: str
    revisable_text: str
    is_final: bool
    emitted_at: float
    recovery_record_id: str = ''
    recovered: bool = False

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

    def __init__(self, stability_window: int=3, *, tail_guard_chars: int=4, min_commit_chars: int=3, normalizer: Callable[[str], str]=normalize_subtitle_text):
        self.stability_window = max(2, int(stability_window))
        self.tail_guard_chars = max(0, int(tail_guard_chars))
        self.min_commit_chars = max(1, int(min_commit_chars))
        self._normalizer = normalizer
        self._history: deque[str] = deque(maxlen=self.stability_window)
        self._revision = 0
        self._committed_text = ''
        self._last_text = ''
        self._lock = Lock()

    def reset(self) -> None:
        with self._lock:
            self._history.clear()
            self._committed_text = ''
            self._last_text = ''

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

    def observe(self, text: str, is_final: bool=False) -> SubtitleRevisionState:
        normalized = self._normalizer(text or '')
        with self._lock:
            if is_final:
                self._revision += 1
                revision = self._revision
                self._history.clear()
                self._committed_text = normalized
                self._last_text = normalized
                return SubtitleRevisionState(revision=revision, state='final', text=normalized, stable_text=normalized, unstable_text='')
            if self._committed_text and (not normalized.startswith(self._committed_text)):
                logger.info('[LOCAL_AGREEMENT_CONFLICT] committed=%r hypothesis=%r action=hold', self._committed_text[-80:], normalized[-80:])
                held = self._last_text or self._committed_text + normalized
                revisable = held[len(self._committed_text):] if held.startswith(self._committed_text) else ''
                return SubtitleRevisionState(revision=self._revision, state='stable', text=held, stable_text=self._committed_text, unstable_text=revisable)
            self._revision += 1
            revision = self._revision
            self._history.append(normalized)
            candidate = ''
            if len(self._history) >= self.stability_window:
                common = longest_common_prefix(list(self._history))
                candidate = stable_semantic_prefix(common, normalized, tail_guard_chars=self.tail_guard_chars)
                if len(candidate) < self.min_commit_chars:
                    candidate = ''
            if candidate:
                if not self._committed_text:
                    self._committed_text = candidate
                elif candidate.startswith(self._committed_text):
                    self._committed_text = candidate
                elif not normalized.startswith(self._committed_text):
                    self._committed_text = candidate
            committed = self._committed_text if self._committed_text and normalized.startswith(self._committed_text) else ''
            revisable = normalized[len(committed):] if committed else normalized
            self._last_text = normalized
            return SubtitleRevisionState(revision=revision, state='stable' if committed else 'interim', text=normalized, stable_text=committed, unstable_text=revisable)

class RealtimeSubtitleSession:
    """Per-session authoritative ASR state with transactional event publication."""
    TRANSCRIPT_EVENT = 'transcript'

    def __init__(self, session_id: str, event_bus: SessionEventBus, *, source_stability_window: int=3, retention_seconds: float=300.0, max_completed_utterances: int=512, max_tracked_utterances: int=1024, clock: Callable[[], float]=time.monotonic):
        self.session_id = str(session_id)
        self.event_bus = event_bus
        self.source_stability_window = max(2, int(source_stability_window))
        self.retention_seconds = max(30.0, float(retention_seconds))
        self.max_completed_utterances = max(64, int(max_completed_utterances))
        self.max_tracked_utterances = max(self.max_completed_utterances, int(max_tracked_utterances))
        self.clock = clock
        self._lock = Lock()
        self._utterance_locks = KeyedLockPool()
        self._source_trackers: OrderedDict[int, LocalAgreementCommitPolicy] = OrderedDict()
        self._latest_source: OrderedDict[int, TranscriptEvent] = OrderedDict()
        self._utterance_lifecycle: OrderedDict[int, Dict[str, Any]] = OrderedDict()
        self._closed = False

    def _touch_lifecycle_locked(self, key: int) -> Dict[str, Any]:
        item = self._utterance_lifecycle.get(key)
        if item is None:
            item = {'source_final': False, 'last_activity': self.clock()}
            self._utterance_lifecycle[key] = item
        item['last_activity'] = self.clock()
        self._utterance_lifecycle.move_to_end(key)
        return item

    def _cleanup_completed_locked(self) -> None:
        now = self.clock()
        terminal = [key for key, meta in self._utterance_lifecycle.items() if bool(meta.get('source_final'))]
        expired = [key for key, meta in self._utterance_lifecycle.items() if now - float(meta.get('last_activity', now)) >= self.retention_seconds]
        terminal_excess = max(0, len(terminal) - self.max_completed_utterances)
        total_excess = max(0, len(self._utterance_lifecycle) - self.max_tracked_utterances)
        oldest_total = list(self._utterance_lifecycle.keys())[:total_excess]
        to_remove = list(dict.fromkeys(expired + terminal[:terminal_excess] + oldest_total))
        for key in to_remove:
            self._utterance_lifecycle.pop(key, None)
            self._source_trackers.pop(key, None)
            self._latest_source.pop(key, None)
            self._utterance_locks.discard(key)

    def _source_tracker(self, utterance_id: int) -> LocalAgreementCommitPolicy:
        key = int(utterance_id)
        with self._lock:
            tracker = self._source_trackers.get(key)
            if tracker is None:
                tracker = LocalAgreementCommitPolicy(stability_window=self.source_stability_window, tail_guard_chars=4, min_commit_chars=3)
                self._source_trackers[key] = tracker
            self._source_trackers.move_to_end(key)
            self._touch_lifecycle_locked(key)
            self._cleanup_completed_locked()
            return tracker

    def latest_source(self, utterance_id: int) -> Optional[TranscriptEvent]:
        with self._lock:
            return self._latest_source.get(int(utterance_id))

    def ingest_asr(self, text: str, *, is_final: bool, utterance_id: int) -> TranscriptEvent:
        key = int(utterance_id)
        with self._utterance_locks.get(key):
            return self._ingest_asr_serialized(text, is_final=is_final, utterance_id=key)

    def _ingest_asr_serialized(self, text: str, *, is_final: bool, utterance_id: int) -> TranscriptEvent:
        normalized_input = normalize_subtitle_text(text)
        key = int(utterance_id)
        with self._lock:
            if self._closed:
                raise RuntimeError('字幕 Session 已关闭')
            previous = self._latest_source.get(key)
            if previous is not None and previous.text == normalized_input and (previous.is_final == bool(is_final)):
                self._touch_lifecycle_locked(key)
                return previous
        tracker = self._source_tracker(key)
        snapshot = tracker.snapshot()
        state = tracker.observe(normalized_input, is_final=is_final)
        event = TranscriptEvent(session_id=self.session_id, utterance_id=key, revision=state.revision, state=state.state, text=state.text, committed_text=state.stable_text, revisable_text=state.unstable_text, is_final=bool(is_final), emitted_at=self.clock())
        with self._lock:
            previous = self._latest_source.get(key)
        unchanged = bool(previous is not None and event.revision == previous.revision and (event.text == previous.text) and (event.committed_text == previous.committed_text) and (event.revisable_text == previous.revisable_text) and (event.is_final == previous.is_final))
        if unchanged:
            return previous
        if not self.event_bus.publish(self.TRANSCRIPT_EVENT, event):
            tracker.restore(snapshot)
            logger.error('[TRANSCRIPT_EVENT_ROLLBACK] session_id=%s utterance=%d revision=%d final=%s', self.session_id, key, event.revision, event.is_final)
            return previous if previous is not None else TranscriptEvent(session_id=self.session_id, utterance_id=key, revision=snapshot[1], state='unpublished', text=snapshot[3], committed_text=snapshot[2], revisable_text=snapshot[3][len(snapshot[2]):] if snapshot[3].startswith(snapshot[2]) else snapshot[3], is_final=False, emitted_at=self.clock())
        with self._lock:
            self._latest_source[key] = event
            self._latest_source.move_to_end(key)
            lifecycle = self._touch_lifecycle_locked(key)
            lifecycle['source_final'] = bool(is_final)
            self._cleanup_completed_locked()
        return event

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._source_trackers.clear()
            self._latest_source.clear()
            self._utterance_lifecycle.clear()
            self._utterance_locks.clear()

def should_accept_source_update(current_text: str, current_is_final: bool, new_text: str, is_final: bool, age_seconds: float, same_utterance: bool=True) -> bool:
    current_text = current_text or ''
    new_text = new_text or ''
    if not new_text:
        return False
    if not same_utterance:
        return True
    if not current_text:
        return True
    if new_text == current_text:
        return is_final and (not current_is_final)
    if is_final:
        return True
    age_seconds = max(0.0, float(age_seconds))
    if current_is_final and age_seconds < 0.8 and (new_text in current_text):
        return False
    if age_seconds < 2.5 and len(new_text) + 4 < len(current_text):
        if new_text in current_text:
            return False
        if SequenceMatcher(None, new_text, current_text).ratio() >= 0.64:
            return False
    return True

def validate_args(args: argparse.Namespace) -> argparse.Namespace:
    checks = {'sample_rate': args.sample_rate == 16000, 'min_silence_duration': args.min_silence_duration > 0, 'min_speech_duration': args.min_speech_duration > 0, 'vad_threshold': 0.0 < args.vad_threshold < 1.0, 'vad_buffer_size': args.vad_buffer_size > 0, 'max_utterance_seconds': args.max_utterance_seconds > 0, 'speech_preroll_ms': args.speech_preroll_ms >= 0, 'forced_segment_overlap_ms': 0 <= args.forced_segment_overlap_ms <= 1000, 'audio_queue_size': args.audio_queue_size > 0, 'max_drain_chunks': args.max_drain_chunks > 0, 'audio_latency_budget_ms': args.audio_latency_budget_ms >= 200, 'audio_backpressure_mode': args.audio_backpressure_mode in ('live', 'buffered'), 'audio_recovery_preroll_ms': 100 <= args.audio_recovery_preroll_ms <= args.audio_latency_budget_ms, 'min_audio_rms': args.min_audio_rms >= 0, 'cuda_device': args.cuda_device >= 0, 'stable_partial_threshold': args.stable_partial_threshold >= 2, 'torch_intra_op_threads': args.torch_intra_op_threads > 0, 'torch_inter_op_threads': args.torch_inter_op_threads > 0, 'stream_process_interval_ms': args.stream_process_interval_ms > 0, 'stream_final_padding_ms': args.stream_final_padding_ms >= 0, 'nemotron_lookahead_tokens': args.nemotron_lookahead_tokens in (0, 3, 6, 13), 'endpoint_punctuation_hold_ms': args.endpoint_punctuation_hold_ms >= 0, 'endpoint_min_utterance_ms': args.endpoint_min_utterance_ms >= 0}
    invalid = [name for name, ok in checks.items() if not ok]
    if invalid:
        raise ValueError(f"参数必须在有效范围内：{', '.join(invalid)}")
    return args

def should_force_utterance_segment(sample_count: int, sample_rate: int, max_utterance_seconds: float, vad_has_endpoint: bool) -> bool:
    if vad_has_endpoint or sample_rate <= 0 or max_utterance_seconds <= 0:
        return False
    return sample_count >= round(sample_rate * max_utterance_seconds)

def supports_vad_reset(vad) -> bool:
    return callable(getattr(vad, 'reset', None))

def ensure_runtime_dependencies() -> bool:
    """
    延迟加载重依赖，避免 .pyw 在日志初始化前静默退出。
    """
    global torch, np, _runtime_import_error
    try:
        if torch is None:
            _import_started = time.monotonic()
            import torch as _torch
            torch = _torch
            logger.info('[RUNTIME_IMPORT] module=torch ms=%.0f', (time.monotonic() - _import_started) * 1000)
        if np is None:
            _import_started = time.monotonic()
            import numpy as _np
            np = _np
            logger.info('[RUNTIME_IMPORT] module=numpy ms=%.0f', (time.monotonic() - _import_started) * 1000)
    except Exception as e:
        _runtime_import_error = e
        logger.error(f'运行时依赖加载失败：{e}', exc_info=True)
        return False
    return True

def configure_torch_for_accuracy(allow_tf32: bool=True, cudnn_benchmark: bool=True) -> None:
    """Configure CUDA for low-latency inference on fixed-shape audio chunks.

    Nemotron runs in FP16 on CUDA. TF32 still accelerates any remaining FP32
    matmul/convolution paths on Ampere-or-newer GPUs, while cuDNN autotuning is
    useful because the streaming feature shapes are stable after warmup.
    """
    if torch is None:
        return
    try:
        torch.set_float32_matmul_precision('high' if allow_tf32 else 'highest')
    except Exception:
        pass
    try:
        if hasattr(torch.backends, 'cuda'):
            torch.backends.cuda.matmul.allow_tf32 = bool(allow_tf32)
            matmul = getattr(torch.backends.cuda, 'matmul', None)
            if matmul is not None and hasattr(matmul, 'allow_fp16_reduced_precision_reduction'):
                matmul.allow_fp16_reduced_precision_reduction = True
            for name in ('enable_flash_sdp', 'enable_mem_efficient_sdp', 'enable_math_sdp'):
                setter = getattr(torch.backends.cuda, name, None)
                if callable(setter):
                    setter(True)
        if hasattr(torch.backends, 'cudnn'):
            torch.backends.cudnn.allow_tf32 = bool(allow_tf32)
            torch.backends.cudnn.benchmark = bool(cudnn_benchmark)
    except Exception as e:
        logger.debug(f'设置 torch CUDA 性能选项失败：{e}')

def configure_torch_runtime(intra_op_threads: int=4, inter_op_threads: int=1, allow_tf32: bool=True, cudnn_benchmark: bool=True) -> Dict[str, int]:
    """Apply the measured balanced CPU policy before the first inference."""
    global _torch_runtime_config
    if torch is None:
        return {}
    requested_intra = max(1, int(intra_op_threads))
    requested_inter = max(1, int(inter_op_threads))
    with TORCH_CONFIG_LOCK:
        config_key = (id(torch), requested_intra, requested_inter)
        if _torch_runtime_config != config_key:
            torch.set_num_threads(requested_intra)
            try:
                torch.set_num_interop_threads(requested_inter)
            except RuntimeError as e:
                actual_inter = int(torch.get_num_interop_threads())
                if actual_inter != requested_inter:
                    logger.warning('[CPU] inter-op 线程池已启动，无法从 %d 调整为 %d：%s', actual_inter, requested_inter, e)
                else:
                    logger.debug('[CPU] inter-op 线程数已是 %d', actual_inter)
            _torch_runtime_config = config_key
        configure_torch_for_accuracy(allow_tf32=allow_tf32, cudnn_benchmark=cudnn_benchmark)
        actual = {'intra_op_threads': int(torch.get_num_threads()), 'inter_op_threads': int(torch.get_num_interop_threads())}
        logger.info('[TORCH] intra_op=%d inter_op=%d tf32=%s cudnn_benchmark=%s', actual['intra_op_threads'], actual['inter_op_threads'], bool(allow_tf32), bool(cudnn_benchmark))
        return actual

def configure_current_process_priority(level: str) -> None:
    """Best-effort process priority control.

    ASR/UI and audio capture may request an elevated process priority. Failures are non-fatal.
    """
    try:
        if sys.platform.startswith('win'):
            import ctypes
            classes = {'idle': 64, 'below_normal': 16384, 'normal': 32, 'above_normal': 32768, 'high': 128}
            value = classes.get(level, classes['normal'])
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if not ctypes.windll.kernel32.SetPriorityClass(handle, value):
                raise OSError('SetPriorityClass failed')
        elif level == 'below_normal':
            try:
                getattr(os, 'nice')(5)
            except OSError:
                pass
        logger.info('[PROCESS] pid=%d priority=%s', os.getpid(), level)
    except Exception as exc:
        logger.debug('[PROCESS] 无法设置进程优先级 %s: %s', level, exc)
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
        logger.error('音频依赖加载失败：%s', exc, exc_info=True)
        return False

def get_audio_devices(p_audio) -> List[tuple[int, Dict[str, Any]]]:
    """Return MME devices for the legacy microphone list used by the UI."""
    device_count = p_audio.get_device_count()
    if device_count <= 0:
        return []
    host_api = 0
    for index in range(p_audio.get_host_api_count()):
        info = p_audio.get_host_api_info_by_index(index)
        if 'MME' in info.get('name', ''):
            host_api = index
            break
    devices = []
    for index in range(device_count):
        info = p_audio.get_device_info_by_index(index)
        if info.get('hostApi') == host_api:
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
        raise TimeoutError(f'设备试读超过 {timeout:.1f}s')
    if errors:
        raise errors[0]

def probe_audio_devices(p_audio, device_indices, chunk_seconds: float=AUDIO_FRAME_SECONDS, read_timeout: float=1.0):
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
                native_rate = int(info['defaultSampleRate'])
                channels = int(info.get('maxInputChannels', 0) or info.get('maxOutputChannels', 0))
                if native_rate <= 0 or channels <= 0:
                    raise ValueError('设备没有可用声道或采样率')
                frames = max(1, int(native_rate * chunk_seconds))
                streams[device_idx] = p_audio.open(format=pyaudio_backend.paFloat32, channels=channels, rate=native_rate, input=True, input_device_index=device_idx, frames_per_buffer=frames, start=False)
                frames_by_device[device_idx] = frames
                if info.get('isLoopbackDevice', False):
                    loopback_devices.add(device_idx)
            except Exception as exc:
                failures[device_idx] = str(exc)
        for device_idx, stream in streams.items():
            try:
                stream.start_stream()
                _read_probe_chunk(stream, frames_by_device[device_idx], read_timeout)
                valid_devices.append(device_idx)
            except TimeoutError as exc:
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
    return (valid_devices, failures)

def _probe_audio_devices_worker(device_indices, chunk_seconds, result_queue) -> None:
    if not ensure_audio_dependencies():
        result_queue.put(([], {idx: f'音频依赖不可用：{_audio_import_error}' for idx in device_indices}))
        return
    p_audio = pyaudio_backend.PyAudio()
    try:
        result_queue.put(probe_audio_devices(p_audio, device_indices, chunk_seconds))
    finally:
        p_audio.terminate()

def probe_audio_devices_isolated(device_indices, timeout: float=8.0):
    """Protect the UI process from PortAudio hangs and native probe crashes."""
    result_queue = multiprocessing.Queue(maxsize=1)
    process = multiprocessing.Process(target=_probe_audio_devices_worker, args=(list(device_indices), AUDIO_FRAME_SECONDS, result_queue))
    try:
        process.start()
        process.join(timeout=max(0.1, timeout))
        if process.is_alive():
            process.terminate()
            process.join(timeout=1.0)
            return ([], {idx: '设备组合预检超时' for idx in device_indices})
        if process.exitcode != 0:
            return ([], {idx: f'设备组合预检进程异常退出：{process.exitcode}' for idx in device_indices})
        try:
            return result_queue.get(timeout=0.5)
        except queue.Empty:
            return ([], {idx: '设备组合预检未返回结果' for idx in device_indices})
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
            raise ValueError('重采样采样率必须大于 0')
        divisor = gcd(self.native_rate, self.target_rate)
        self.up = self.target_rate // divisor
        self.down = self.native_rate // divisor
        history = max(self.down * 32, round(self.native_rate * 0.02))
        self.history_samples = max(self.down, (history + self.down - 1) // self.down * self.down)
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

def _limit_audio(samples, ceiling: float=0.98):
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

    def __init__(self, target_rms: float=0.075, gate_rms: float=0.004, min_gain: float=0.75, max_gain: float=3.0, attack: float=0.3, release: float=0.08, ceiling: float=0.95):
        self.target_rms = max(0.0001, float(target_rms))
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
            desired = min(self.max_gain, max(self.min_gain, self.target_rms / max(rms, 1e-06)))
        else:
            desired = 1.0
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

    def __init__(self, tolerance_seconds: float, adaptation: float=0.05):
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

def _mix_audio_tracks(tracks, mode: str='average', *, expected_track_count: Optional[int]=None):
    """Mix tracks with stable gain even when one device temporarily misses a frame."""
    normalized = [np.asarray(track, dtype=np.float32) for track in tracks if len(track)]
    if not normalized:
        return np.array([], dtype=np.float32)
    if mode == 'add':
        return _limit_audio(np.sum(normalized, axis=0))
    active = []
    for track in normalized:
        rms = float(np.sqrt(np.mean(np.square(track, dtype=np.float64))))
        if rms >= 1e-05:
            active.append(track)
    selected = active or normalized
    normalization_count = max(1, int(expected_track_count) if expected_track_count is not None else len(selected))
    mixed = np.sum(selected, axis=0) / max(1.0, normalization_count ** 0.5)
    return _limit_audio(mixed)

def trim_audio_packets_to_latency_budget(packets: List[Any], now: float, latency_budget_seconds: float, recovery_preroll_seconds: float) -> tuple[List[Any], int, float]:
    """Keep the live edge when queued capture audio exceeds the real-time budget."""
    audio_packets = [packet for packet in packets if isinstance(packet, dict) and packet.get('type', 'audio') == 'audio']
    if not audio_packets:
        return (packets, 0, 0.0)
    oldest_at = min((float(packet.get('captured_at', now)) for packet in audio_packets))
    newest_at = max((float(packet.get('captured_at', now)) for packet in audio_packets))
    max_age = max(0.0, now - oldest_at)
    if max_age <= max(0.05, float(latency_budget_seconds)):
        return (packets, 0, max_age)
    cutoff = newest_at - max(0.05, float(recovery_preroll_seconds))
    kept = [packet for packet in packets if not isinstance(packet, dict) or packet.get('type', 'audio') != 'audio' or float(packet.get('captured_at', newest_at)) >= cutoff]
    return (kept, max(0, len(packets) - len(kept)), max_age)

class GrowableAudioBuffer:
    """Exponentially growing contiguous buffer without O(n²) append copies."""

    def __init__(self, initial_capacity: int=32000):
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
        if count <= 0:
            return 0
        remaining = self._size - count
        if remaining > 0:
            self._data[:remaining] = self._data[count:self._size]
        self._size = remaining
        return count

    def __len__(self) -> int:
        return self._size

    def __getitem__(self, item):
        return self._data[:self._size].__getitem__(item)

def _put_audio_packet(output_queue, packet, on_drop: Callable[[], None], mode: str='live', stop_event: Optional[Any]=None) -> bool:
    """Publish audio using either live-edge shedding or buffered backpressure."""
    if mode == 'buffered':
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

def start_recording(device_indices, output_queue, stop_event, mix_mode: str='average', debug_save_audio: str='', target_sample_rate: int=16000, emit_metadata: bool=False, backpressure_mode: str='buffered') -> None:
    """Capture, resample and mix audio with a zero-wait single-device fast path."""
    if not device_indices:
        raise ValueError('没有选择任何音频设备')
    if not ensure_audio_dependencies():
        raise RuntimeError(f'缺少音频依赖：{_audio_import_error}')
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
        debug_wav_file = wave.open(debug_save_audio, 'wb')
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
                if backpressure_mode == 'buffered':
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
                            raise RuntimeError(f'设备 {device_idx} 音频队列无法恢复实时位置') from exc
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
            packet = {'type': 'audio', 'sequence': output_sequence, 'captured_at': captured_at, 'samples': samples, 'dropped_before': dropped_output_chunks, 'device_drops': dict(device_drop_counts), 'sync_missing_chunks': sync_missing_chunks}

        def on_drop() -> None:
            nonlocal dropped_output_chunks
            dropped_output_chunks += 1
            if isinstance(packet, dict):
                packet['dropped_before'] = dropped_output_chunks
        if not _put_audio_packet(output_queue, packet, on_drop, mode=backpressure_mode, stop_event=stop_event):
            raise RuntimeError('录音输出队列不可用，停止识别以避免静默丢帧')
    try:
        for raw_device_idx in device_indices:
            device_idx = int(raw_device_idx)
            info = p_audio.get_device_info_by_index(device_idx)
            device_info_map[device_idx] = info
            native_rate = int(info['defaultSampleRate'])
            channels = int(info.get('maxInputChannels', 0) or info.get('maxOutputChannels', 0))
            if native_rate <= 0 or channels <= 0:
                raise RuntimeError(f'设备 {device_idx} 没有可用声道或采样率')
            frames = max(1, int(AUDIO_FRAME_SECONDS * native_rate))
            device_streams[device_idx] = p_audio.open(format=pyaudio_backend.paFloat32, channels=channels, rate=native_rate, input=True, input_device_index=device_idx, frames_per_buffer=frames, start=False)
            device_queues[device_idx] = queue.Queue(maxsize=120)
            device_resamplers[device_idx] = StreamingAudioResampler(native_rate, target_sample_rate)
            thread = Thread(target=capture_device, args=(device_idx, native_rate, channels), daemon=True)
            thread.start()
            device_threads[device_idx] = thread
            logger.info('[Audio] device=%d name=%s rate=%d channels=%d', device_idx, info.get('name', 'unknown'), native_rate, channels)
        for stream in device_streams.values():
            stream.start_stream()
        capture_start_event.set()
        selected = [int(idx) for idx in device_indices]
        if len(selected) == 1:
            device_idx = selected[0]
            while not stop_event.is_set():
                try:
                    failed_device, message = capture_errors.get_nowait()
                    error_packet = {'type': 'audio_error', 'device': failed_device, 'message': message}
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
            anchor_device = selected[0]
            sync_wait = AUDIO_FRAME_SECONDS * 2.0
            sync_tolerance = AUDIO_FRAME_SECONDS * 0.75
            clock_aligner = AdaptiveAudioClockAligner(sync_tolerance)
            pending_by_device: Dict[int, Any] = {}
            while not stop_event.is_set():
                try:
                    failed_device, message = capture_errors.get_nowait()
                    error_packet = {'type': 'audio_error', 'device': failed_device, 'message': message}
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
                        captured_at, samples, native_rate = (anchor_time, anchor_samples, anchor_rate)
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
                            aligned_delta = clock_aligner.delta(device_idx, anchor_time, candidate_at)
                            if aligned_delta < -sync_tolerance:
                                sync_missing_chunks += 1
                                packet = None
                                continue
                            if aligned_delta > sync_tolerance:
                                pending_by_device[device_idx] = packet
                                packet = None
                                break
                            clock_aligner.accept(device_idx, anchor_time, candidate_at)
                            captured_at, samples, native_rate = (candidate_at, candidate_samples, candidate_rate)
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
                mixed = _mix_audio_tracks(tracks, mix_mode, expected_track_count=len(selected))
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
        self.prev_result = ''

    def do_print(self, result) -> None:
        if result and self.prev_result != result:
            self.prev_result = result
            print(result, end='\n', flush=True)

    def on_endpoint(self) -> None:
        print('\n', end='', flush=True)

def _single_device_recording_worker(device_idx: int, output_queue, stop_event, target_sample_rate: int, backpressure_mode: str) -> None:
    """Capture one device in its own PortAudio process.

    PyAudioWPatch can stall a WASAPI loopback stream when a microphone stream is
    open in the same process, and its multi-stream teardown can access invalid
    native state. Process isolation avoids both PortAudio limitations.
    """
    configure_current_process_priority('above_normal')
    try:
        start_recording([device_idx], output_queue, stop_event, target_sample_rate=target_sample_rate, emit_metadata=True, backpressure_mode=backpressure_mode)
    except BaseException as exc:
        try:
            output_queue.put_nowait({'type': 'audio_error', 'device': device_idx, 'message': str(exc)})
        except Exception:
            pass

def _audio_packet_device_drop_total(packet: Dict[str, Any], device_idx: int) -> int:
    """Combine capture-thread drops and child-output-queue drops for one device."""
    per_device = packet.get('device_drops') or {}
    capture_drops = per_device.get(device_idx, per_device.get(str(device_idx), 0))
    return max(0, int(packet.get('dropped_before', 0))) + max(0, int(capture_drops or 0))

def _record_multiple_devices_isolated(device_indices, output_queue, stop_event, mix_mode: str, target_sample_rate: int, backpressure_mode: str) -> None:
    """Mix device streams captured by independent child processes."""
    ctx = multiprocessing.get_context('spawn')
    selected = [int(item) for item in device_indices]
    device_queues = {idx: ctx.Queue(maxsize=120) for idx in selected}
    processes = {idx: ctx.Process(target=_single_device_recording_worker, args=(idx, device_queues[idx], stop_event, target_sample_rate, backpressure_mode), name=f'SubtitleAudioDevice-{idx}') for idx in selected}
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
                    raise RuntimeError(f'音频设备子进程异常退出：{dead}')
                continue
            if anchor_packet.get('type') == 'audio_error':
                _put_audio_packet(output_queue, anchor_packet, lambda: None)
                stop_event.set()
                break
            packets_by_device = {anchor_device: anchor_packet}
            anchor_time = float(anchor_packet['captured_at'])
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
                    if packet.get('type') == 'audio_error':
                        _put_audio_packet(output_queue, packet, lambda: None)
                        stop_event.set()
                        break
                    candidate_at = float(packet['captured_at'])
                    aligned_delta = clock_aligner.delta(device_idx, anchor_time, candidate_at)
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
                track = np.asarray(device_packet['samples'], dtype=np.float32)
                if len(track) < frame_samples:
                    track = np.pad(track, (0, frame_samples - len(track)))
                elif len(track) > frame_samples:
                    track = track[:frame_samples]
                tracks.append(track)
            mixed = _mix_audio_tracks(tracks, mix_mode, expected_track_count=len(selected))
            output_sequence += 1
            packet = {'type': 'audio', 'sequence': output_sequence, 'captured_at': min((float(item['captured_at']) for item in packets)), 'samples': mixed, 'dropped_before': dropped_output_chunks, 'device_drops': {idx: _audio_packet_device_drop_total(packets_by_device[idx], idx) if idx in packets_by_device else 0 for idx in selected}, 'sync_missing_chunks': sync_missing_chunks}

            def on_drop() -> None:
                nonlocal dropped_output_chunks
                dropped_output_chunks += 1
                packet['dropped_before'] = dropped_output_chunks
            if not _put_audio_packet(output_queue, packet, on_drop, mode=backpressure_mode, stop_event=stop_event):
                raise RuntimeError('多设备混音输出队列不可用，停止识别以避免静默丢帧')
    finally:
        stop_event.set()
        for process in processes.values():
            process.join(timeout=1.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=0.5)
            if process.is_alive() and hasattr(process, 'kill'):
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

def recording_process_entrypoint(device_indices, output_queue, stop_event, mix_mode='average', debug_save_audio='', target_sample_rate=16000, backpressure_mode='buffered') -> None:
    """Child-process entrypoint with explicit error reporting."""
    configure_current_process_priority('above_normal')
    try:
        if not ensure_audio_dependencies():
            raise RuntimeError(f'缺少音频依赖：{_audio_import_error}')
        if len(device_indices) > 1 and (not debug_save_audio):
            _record_multiple_devices_isolated(device_indices, output_queue, stop_event, mix_mode, target_sample_rate, backpressure_mode)
        else:
            start_recording(device_indices, output_queue, stop_event, mix_mode, debug_save_audio, target_sample_rate, emit_metadata=True, backpressure_mode=backpressure_mode)
    except Exception as exc:
        logger.error('录音进程异常退出：%s', exc, exc_info=True)
        try:
            output_queue.put({'type': 'audio_error', 'device': -1, 'message': str(exc)}, timeout=1.0)
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

class _CallbackTokenStreamer:
    """Decode every generated token without waiting for whitespace boundaries."""

    def __init__(self, tokenizer, callback: Callable[[str], None]):
        self.tokenizer = tokenizer
        self.callback = callback
        self.token_ids: List[int] = []
        self.text = ''
        self._skip_prompt = True

    def put(self, value) -> None:
        if self._skip_prompt:
            self._skip_prompt = False
            return
        values = value.detach().cpu().reshape(-1).tolist()
        self.token_ids.extend((int(item) for item in values))
        text = normalize_subtitle_text(self.tokenizer.decode(self.token_ids, skip_special_tokens=True))
        if text and text != self.text:
            self.text = text
            self.callback(text)

    def end(self) -> None:
        return

class StreamingNemotronRecognizer:
    """Native cache-aware streaming wrapper for Nemotron 3.5 ASR.

    Audio is converted into the model's exact fixed-size mel chunks. A blocking
    feature generator keeps one ``generate`` call alive for the utterance, so
    encoder and RNNT decoder caches are retained instead of recomputing history.
    """
    _FEATURE_END = object()

    def __init__(self, recognizer, processor, on_result: Callable[[str, bool, int], None], sample_rate: int=16000, language: str='ja-JP', lookahead_tokens: int=3, normalize_audio: bool=True, final_padding_seconds: float=0.32, min_audio_rms: float=1e-05, on_error: Optional[Callable[[str], None]]=None, on_metrics: Optional[Callable[[Dict[str, Any]], None]]=None, clock: Callable[[], float]=time.monotonic, process_interval_seconds: float=0.08, pin_memory: bool=True):
        self.recognizer = recognizer
        self.processor = processor
        self.on_result = on_result
        self.on_error = on_error
        self.on_metrics = on_metrics
        self.clock = clock
        self.sample_rate = int(sample_rate)
        self.language = language
        self.lookahead_tokens = int(lookahead_tokens)
        self.normalize_audio = bool(normalize_audio)
        self.final_padding_seconds = max(0.0, float(final_padding_seconds))
        self.min_audio_rms = max(0.0, float(min_audio_rms))
        self.process_interval_samples = max(1, int(self.sample_rate * process_interval_seconds))
        self.device = next(recognizer.parameters()).device
        self.compute_dtype = next(recognizer.parameters()).dtype
        self._use_cuda = getattr(self.device, 'type', str(self.device).split(':', 1)[0]) == 'cuda'
        self.pin_memory = bool(pin_memory and self._use_cuda)
        self.processor.set_num_lookahead_tokens(self.lookahead_tokens)
        self._first_samples = int(self.processor.num_samples_first_audio_chunk)
        self._chunk_samples = int(self.processor.num_samples_per_audio_chunk)
        self._first_frames = int(self.processor.num_mel_frames_first_audio_chunk)
        self._chunk_frames = int(self.processor.num_mel_frames_per_audio_chunk)
        self._hop_length = int(self.processor.feature_extractor.hop_length)
        self._n_fft = int(self.processor.feature_extractor.n_fft)
        self._commands: queue.Queue = queue.Queue()
        self._running = True
        self._fatal_error_reported = False
        self._public_lock = Lock()
        self._public_audio_lock = Lock()
        self._next_utterance_id = 0
        self._continuation_sequence = 0
        self._public_utterance_id = 0
        self._public_audio_chunks: Dict[int, deque] = {}
        self._public_audio_samples: Dict[int, int] = {}
        self._public_audio_dropped_samples: Dict[int, int] = {}
        self._public_audio_notifications: set[int] = set()
        self._max_public_audio_samples = self.sample_rate * 30
        self._sequence = 0
        self._suppress_callbacks = False
        self._reset_stream_state(0)
        self._worker_thread = Thread(target=self._worker, name='NemotronStreamingASR', daemon=True)
        self._worker_thread.start()
        logger.info('[ASR_STREAM] backend=nemotron-transformers language=%s lookahead=%d first_samples=%d chunk_samples=%d first_frames=%d chunk_frames=%d dtype=%s', self.language, self.lookahead_tokens, self._first_samples, self._chunk_samples, self._first_frames, self._chunk_frames, self.compute_dtype)

    def _reset_stream_state(self, utterance_id: int, public_utterance_id: Optional[int]=None) -> None:
        self._active_utterance_id = utterance_id
        self._active_public_utterance_id = utterance_id if public_utterance_id is None else public_utterance_id
        self._audio = GrowableAudioBuffer(self.sample_rate * 2)
        self._raw_sample_count = 0
        self._audio_base_sample = 0
        self._last_processed_samples = 0
        self._next_chunk_start = 0
        self._first_chunk_sent = False
        self._feature_queue: queue.Queue = queue.Queue(maxsize=4)
        self._generation_thread: Optional[Thread] = None
        self._generation_done = Event()
        self._generation_error: Optional[BaseException] = None
        self._generation_output = None
        self._streamer: Optional[_CallbackTokenStreamer] = None
        self._last_text = ''
        self._last_feature_at = self.clock()
        self._last_queue_latency_ms = 0
        self._dc_estimate = 0.0

    def begin_utterance(self, samples, sample_rate: Optional[int]=None) -> int:
        with self._public_lock:
            self._next_utterance_id += 1
            utterance_id = self._next_utterance_id
            self._public_utterance_id = utterance_id
        with self._public_audio_lock:
            self._public_audio_chunks[utterance_id] = deque()
            self._public_audio_samples[utterance_id] = 0
            self._public_audio_dropped_samples[utterance_id] = 0
        self._commands.put(('begin', utterance_id, samples, sample_rate or self.sample_rate, self.clock()))
        return utterance_id

    def accept_audio(self, samples, sample_rate: Optional[int]=None, dropped_before_samples: int=0) -> None:
        with self._public_lock:
            utterance_id = self._public_utterance_id
        if utterance_id <= 0 or not self._running:
            return
        audio = np.asarray(samples, dtype=np.float32).reshape(-1).copy()
        if audio.size == 0:
            return
        notify = False
        with self._public_audio_lock:
            chunks = self._public_audio_chunks.setdefault(utterance_id, deque())
            chunks.append((audio, sample_rate or self.sample_rate, self.clock()))
            sample_count = self._public_audio_samples.get(utterance_id, 0) + len(audio)
            dropped = self._public_audio_dropped_samples.get(utterance_id, 0) + max(0, int(dropped_before_samples))
            while sample_count > self._max_public_audio_samples and len(chunks) > 1:
                stale_audio, _, _ = chunks.popleft()
                sample_count -= len(stale_audio)
                dropped += len(stale_audio)
            self._public_audio_samples[utterance_id] = sample_count
            self._public_audio_dropped_samples[utterance_id] = dropped
            if utterance_id not in self._public_audio_notifications:
                self._public_audio_notifications.add(utterance_id)
                notify = True
        if notify:
            self._commands.put(('audio_ready', utterance_id, None, self.sample_rate, self.clock()))

    def _take_public_audio(self, utterance_id: int):
        with self._public_audio_lock:
            chunks = self._public_audio_chunks.pop(utterance_id, deque())
            self._public_audio_samples.pop(utterance_id, None)
            dropped = self._public_audio_dropped_samples.pop(utterance_id, 0)
            self._public_audio_notifications.discard(utterance_id)
        if not chunks:
            return (np.empty(0, dtype=np.float32), self.sample_rate, 0.0, dropped)
        rates = {int(item[1]) for item in chunks}
        if len(rates) != 1:
            raise ValueError(f'同一 utterance 收到不同采样率：{sorted(rates)}')
        arrays = [item[0] for item in chunks]
        return (arrays[0] if len(arrays) == 1 else np.concatenate(arrays), rates.pop(), min((float(item[2]) for item in chunks)), dropped)

    def finalize_utterance(self, reason: str='endpoint') -> None:
        with self._public_lock:
            utterance_id = self._public_utterance_id
            self._public_utterance_id = 0
        if utterance_id > 0 and self._running:
            self._commands.put(('final', utterance_id, reason, self.sample_rate, self.clock()))

    def warmup(self) -> float:
        started = self.clock()
        done = Event()
        error_box: List[BaseException] = []
        self._commands.put(('warmup', 0, (done, error_box), self.sample_rate, self.clock()))
        if not done.wait(timeout=120.0):
            raise TimeoutError('Nemotron 流式 ASR 预热超过 120 秒')
        if error_box:
            raise RuntimeError(f'Nemotron 流式 ASR 预热失败：{error_box[0]}') from error_box[0]
        return self.clock() - started

    def _append_audio(self, samples, sample_rate: int) -> None:
        if int(sample_rate) != self.sample_rate:
            raise ValueError(f'ASR 输入采样率必须为 {self.sample_rate}，实际为 {sample_rate}')
        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        audio = np.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0)
        if audio.size == 0:
            return
        if self.normalize_audio:
            mean = float(np.mean(audio))
            self._dc_estimate = 0.98 * self._dc_estimate + 0.02 * mean
            audio = audio - self._dc_estimate
            peak = float(np.max(np.abs(audio)))
            if peak > 0.98:
                audio = audio * (0.98 / peak)
        audio = np.clip(audio, -0.98, 0.98).astype(np.float32, copy=False)
        if self.min_audio_rms > 0:
            rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
            if rms < self.min_audio_rms:
                audio = np.zeros_like(audio)
        self._audio.append(audio)
        self._raw_sample_count += len(audio)

    def _process_chunk(self, audio, is_first: bool):
        inputs = self.processor(audio, sampling_rate=self.sample_rate, is_streaming=True, is_first_audio_chunk=is_first, language=self.language, return_tensors='pt')
        features = inputs.input_features
        if is_first:
            features = features[:, :self._first_frames, :]
        expected = self._first_frames if is_first else self._chunk_frames
        if features.shape[1] != expected:
            raise RuntimeError(f'Nemotron 特征块帧数错误：expected={expected} actual={features.shape[1]}')
        prompt_ids = inputs.prompt_ids
        if self.pin_memory:
            if not features.is_pinned():
                features = features.pin_memory()
            if not prompt_ids.is_pinned():
                prompt_ids = prompt_ids.pin_memory()
        return (features, prompt_ids)

    def _start_generation(self, prompt_ids, utterance_id: int) -> None:
        self._streamer = _CallbackTokenStreamer(self.processor.tokenizer, lambda text: self._on_stream_text(text, utterance_id))

        def feature_generator():
            while True:
                item = self._feature_queue.get()
                if item is self._FEATURE_END:
                    return
                if self._use_cuda:
                    item = item.to(device=self.device, dtype=self.compute_dtype, non_blocking=self.pin_memory)
                yield item

        def run_generation() -> None:
            try:
                if self._use_cuda:
                    prompt_for_generation = prompt_ids.to(device=self.device, non_blocking=self.pin_memory)
                else:
                    prompt_for_generation = prompt_ids.to(self.device)
                autocast_context = torch.autocast(device_type='cuda', dtype=self.compute_dtype) if self._use_cuda else nullcontext()
                with torch.inference_mode(), autocast_context:
                    self._generation_output = self.recognizer.generate(input_features=feature_generator(), prompt_ids=prompt_for_generation, num_lookahead_tokens=self.lookahead_tokens, streamer=self._streamer)
                sequences = getattr(self._generation_output, 'sequences', self._generation_output)
                decoded = self.processor.batch_decode(sequences, skip_special_tokens=True)[0]
                text = normalize_subtitle_text(decoded)
                if text:
                    self._last_text = text
            except BaseException as exc:
                self._generation_error = exc
            finally:
                self._generation_done.set()
        self._generation_thread = Thread(target=run_generation, name=f'NemotronGenerate-{utterance_id}', daemon=True)
        self._generation_thread.start()

    def _put_feature(self, features) -> None:
        """Apply bounded backpressure without hanging if generation has failed."""
        while self._running:
            if self._generation_done.is_set():
                if self._generation_error is not None:
                    raise RuntimeError(f'Nemotron 流式生成提前失败：{self._generation_error}')
                raise RuntimeError('Nemotron 流式生成在音频结束前意外退出')
            try:
                self._feature_queue.put(features, timeout=0.25)
                return
            except queue.Full:
                continue
        raise RuntimeError('Nemotron 流式识别已停止')

    def _on_stream_text(self, text: str, utterance_id: int) -> None:
        text = normalize_subtitle_text(text)
        if not text or text == self._last_text:
            return
        self._last_text = text
        if not self._suppress_callbacks:
            self.on_result(text, False, utterance_id)
            self._emit_metrics('interim', self._last_feature_at)

    def _emit_metrics(self, state: str, created_at: float) -> None:
        if self.on_metrics is None or self._suppress_callbacks:
            return
        now = self.clock()
        self._sequence += 1
        self.on_metrics({'sequence': self._sequence, 'utterance_id': self._active_utterance_id, 'state': state, 'audio_duration_ms': round(self._raw_sample_count / self.sample_rate * 1000), 'queue_latency_ms': max(0, int(self._last_queue_latency_ms)), 'inference_latency_ms': round(max(0.0, now - self._last_feature_at) * 1000), 'has_text': bool(self._last_text), 'feature_cursor': self._audio_base_sample + self._next_chunk_start})

    def _flush_features(self, final: bool, created_at: float) -> None:
        self._last_queue_latency_ms = round(max(0.0, self.clock() - created_at) * 1000)
        if not self._first_chunk_sent:
            if len(self._audio) < self._first_samples and (not final):
                return
            chunk = self._audio[:self._first_samples]
            if len(chunk) < self._first_samples:
                chunk = np.pad(chunk, (0, self._first_samples - len(chunk)))
            features, prompt_ids = self._process_chunk(chunk, is_first=True)
            self._feature_queue.put_nowait(features)
            self._first_chunk_sent = True
            self._last_processed_samples = min(len(self._audio), self._first_samples)
            mel_frame_idx = self._first_frames
            self._next_chunk_start = mel_frame_idx * self._hop_length - self._n_fft // 2
            self._last_feature_at = self.clock()
            self._start_generation(prompt_ids, self._active_utterance_id)
        while len(self._audio) - self._next_chunk_start >= self._chunk_samples or (final and len(self._audio) > self._next_chunk_start):
            chunk = self._audio[self._next_chunk_start:self._next_chunk_start + self._chunk_samples]
            if len(chunk) < self._chunk_samples:
                chunk = np.pad(chunk, (0, self._chunk_samples - len(chunk)))
            features, _ = self._process_chunk(chunk, is_first=False)
            self._put_feature(features)
            self._next_chunk_start += self._chunk_frames * self._hop_length
            self._last_processed_samples = min(len(self._audio), self._next_chunk_start)
            self._last_feature_at = self.clock()
        self._trim_processed_audio()

    def _trim_processed_audio(self) -> None:
        """Trim feature-consumed application PCM while retaining 800 ms context.

        This bounds the local staging buffer only; it does not claim word-aligned
        confirmed-audio trimming and does not mutate Nemotron model cache.
        """
        retention = int(self.sample_rate * 0.8)
        safe_local = max(0, min(self._last_processed_samples, self._next_chunk_start) - retention)
        if safe_local <= 0:
            return
        removed = self._audio.discard_prefix(safe_local)
        if removed:
            self._audio_base_sample += removed
            self._next_chunk_start = max(0, self._next_chunk_start - removed)
            self._last_processed_samples = max(0, self._last_processed_samples - removed)

    def _finish_generation(self, timeout: float=30.0) -> None:
        if self._generation_thread is None:
            return
        deadline = self.clock() + timeout
        while not self._generation_done.is_set():
            try:
                self._feature_queue.put(self._FEATURE_END, timeout=0.25)
                break
            except queue.Full:
                if self.clock() >= deadline:
                    raise TimeoutError(f'Nemotron 流式生成队列结束超过 {timeout:.0f} 秒')
        if not self._generation_done.wait(timeout=timeout):
            raise TimeoutError(f'Nemotron 流式生成结束超过 {timeout:.0f} 秒')
        self._generation_thread.join(timeout=1.0)
        if self._generation_error is not None:
            raise RuntimeError(f'Nemotron 流式生成失败：{self._generation_error}') from self._generation_error

    def _allocate_continuation_utterance_id(self) -> int:
        with self._public_lock:
            self._continuation_sequence += 1
            return 1000000000 + self._continuation_sequence

    def _finalize_active_segment(self, reason: str, created_at: float) -> None:
        callback_utterance_id = self._active_utterance_id
        if callback_utterance_id == 0:
            return
        pad = int(self.final_padding_seconds * self.sample_rate)
        if pad:
            self._append_audio(np.zeros(pad, dtype=np.float32), self.sample_rate)
        self._flush_features(final=True, created_at=created_at)
        self._finish_generation()
        final_text = normalize_subtitle_text(self._last_text)
        if not self._suppress_callbacks:
            self.on_result(final_text, True, callback_utterance_id)
        self._emit_metrics('final', created_at)
        logger.info('[ASR_STREAM] backend=nemotron utterance=%d finalized reason=%s text_chars=%d samples=%d', callback_utterance_id, reason, len(final_text), self._raw_sample_count)
        self._reset_stream_state(0)

    def _worker(self) -> None:
        while self._running:
            try:
                kind, utterance_id, payload, sample_rate, created_at = self._commands.get(timeout=0.5)
            except queue.Empty:
                continue
            if kind == 'shutdown':
                return
            try:
                if kind == 'warmup':
                    done, error_box = payload
                    try:
                        self._suppress_callbacks = True
                        self._reset_stream_state(-1)
                        self._append_audio(np.zeros(self._first_samples, dtype=np.float32), self.sample_rate)
                        self._flush_features(final=True, created_at=created_at)
                        self._finish_generation(timeout=90.0)
                    except BaseException as exc:
                        error_box.append(exc)
                    finally:
                        self._suppress_callbacks = False
                        self._reset_stream_state(0)
                        done.set()
                    continue
                if kind == 'begin':
                    self._reset_stream_state(utterance_id, utterance_id)
                    self._append_audio(payload, sample_rate)
                    self._flush_features(final=False, created_at=created_at)
                elif kind == 'audio_ready':
                    audio, rate, buffered_at, dropped = self._take_public_audio(utterance_id)
                    if utterance_id != self._active_public_utterance_id:
                        logger.debug('[ASR_STREAM] 丢弃非当前 public utterance 音频 id=%d active_public=%d', utterance_id, self._active_public_utterance_id)
                        continue
                    if dropped:
                        logger.warning('[ASR_STREAM] public_audio_overflow utterance=%d dropped_ms=%.0f action=split_discontinuity', utterance_id, dropped / self.sample_rate * 1000)
                        self._finalize_active_segment('buffer_overflow', buffered_at)
                        continuation_id = self._allocate_continuation_utterance_id()
                        self._reset_stream_state(continuation_id, utterance_id)
                        recovery_samples = max(self._first_samples, self.sample_rate * 2)
                        audio = audio[-recovery_samples:]
                    if audio.size:
                        self._append_audio(audio, rate)
                        if self._raw_sample_count - self._last_processed_samples >= self.process_interval_samples:
                            self._flush_features(final=False, created_at=buffered_at)
                elif utterance_id != self._active_public_utterance_id:
                    logger.debug('[ASR_STREAM] 丢弃非当前 utterance 命令 kind=%s id=%d active_public=%d', kind, utterance_id, self._active_public_utterance_id)
                elif kind == 'final':
                    audio, rate, _, dropped = self._take_public_audio(utterance_id)
                    if dropped:
                        logger.warning('[ASR_STREAM] public_audio_overflow_final utterance=%d dropped_ms=%.0f action=split_discontinuity', utterance_id, dropped / self.sample_rate * 1000)
                        self._finalize_active_segment('buffer_overflow_before_final', created_at)
                        continuation_id = self._allocate_continuation_utterance_id()
                        self._reset_stream_state(continuation_id, utterance_id)
                        recovery_samples = max(self._first_samples, self.sample_rate * 2)
                        audio = audio[-recovery_samples:]
                    if audio.size:
                        self._append_audio(audio, rate)
                    self._finalize_active_segment(str(payload), created_at)
            except RuntimeError as exc:
                self._handle_fatal_error(f'Nemotron 流式推理状态异常：{exc}')
                return
            except Exception as exc:
                self._handle_fatal_error(f'Nemotron 流式推理异常：{exc}')
                return

    def _handle_fatal_error(self, message: str) -> None:
        if self._fatal_error_reported:
            return
        self._fatal_error_reported = True
        self._running = False
        logger.error('[ASR_STREAM_FATAL] %s', message, exc_info=True)
        if self.on_error is not None:
            self.on_error(message)

    def shutdown(self) -> None:
        self._running = False
        with self._public_lock:
            self._public_utterance_id = 0
        with self._public_audio_lock:
            self._public_audio_chunks.clear()
            self._public_audio_samples.clear()
            self._public_audio_dropped_samples.clear()
            self._public_audio_notifications.clear()
        try:
            self._feature_queue.put_nowait(self._FEATURE_END)
        except queue.Full:
            pass
        self._commands.put(('shutdown', 0, None, self.sample_rate, self.clock()))
        if current_thread() is not self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)

def isolated_nemotron_process_entrypoint(command_queue, critical_event_queue, stream_event_queue, model_name: str, model_revision: str, language: str, sample_rate: int, lookahead_tokens: int, normalize_audio: bool, final_padding_seconds: float, min_audio_rms: float, process_interval_seconds: float, intra_op_threads: int, inter_op_threads: int, allow_tf32: bool, cuda_device: int, cudnn_benchmark: bool, pin_memory: bool) -> None:
    """Own the ASR model and CUDA context in a restartable child process."""
    _disable_child_file_logging()
    configure_current_process_priority('above_normal')
    recognizer = None
    fatal = Event()

    def publish(kind: str, payload: Any, critical: bool=False) -> None:
        packet = (kind, payload)
        if critical:
            deadline = time.monotonic() + 5.0
            while not fatal.is_set() and time.monotonic() < deadline:
                try:
                    critical_event_queue.put(packet, timeout=0.25)
                    return
                except queue.Full:
                    continue
                except (EOFError, OSError, ValueError):
                    return
            logger.error('ASR 关键事件投递超时 kind=%s', kind)
            return
        try:
            stream_event_queue.put_nowait(packet)
            return
        except queue.Full:
            pass
        try:
            stream_event_queue.get_nowait()
        except (queue.Empty, EOFError, OSError, ValueError):
            pass
        try:
            stream_event_queue.put_nowait(packet)
        except (queue.Full, EOFError, OSError, ValueError):
            pass
    try:
        if not ensure_runtime_dependencies():
            raise RuntimeError(f'ASR 运行时依赖不可用：{_runtime_import_error}')
        configure_torch_runtime(intra_op_threads, inter_op_threads, allow_tf32, cudnn_benchmark)
        from transformers import AutoModelForRNNT, AutoProcessor
        if torch.cuda.is_available():
            device_count = int(torch.cuda.device_count())
            if cuda_device >= device_count:
                raise RuntimeError(f'CUDA 设备 {cuda_device} 不存在，当前仅检测到 {device_count} 张显卡')
            torch.cuda.set_device(cuda_device)
            device_str = f'cuda:{cuda_device}'
        elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            device_str = 'mps'
        else:
            device_str = 'cpu'
        compute_dtype = torch.float16 if device_str.startswith('cuda') or device_str == 'mps' else torch.float32
        revision_kwargs: Dict[str, Any] = {'local_files_only': True}
        if model_revision and (not os.path.isdir(model_name)):
            revision_kwargs['revision'] = model_revision
        processor = AutoProcessor.from_pretrained(model_name, **revision_kwargs)
        model = AutoModelForRNNT.from_pretrained(model_name, **revision_kwargs, dtype=compute_dtype, low_cpu_mem_usage=True).to(device_str)
        model.eval()
        model.requires_grad_(False)

        def on_result(text: str, is_final: bool, utterance_id: int) -> None:
            publish('result', (text, is_final, utterance_id), critical=is_final)

        def on_error(message: str) -> None:
            publish('fatal', message, critical=True)
            fatal.set()
        recognizer = StreamingNemotronRecognizer(model, processor, on_result, sample_rate=sample_rate, language=language, lookahead_tokens=lookahead_tokens, normalize_audio=normalize_audio, final_padding_seconds=final_padding_seconds, min_audio_rms=min_audio_rms, on_error=on_error, on_metrics=lambda metrics: publish('metrics', metrics), process_interval_seconds=process_interval_seconds, pin_memory=pin_memory)
        warmup_seconds = recognizer.warmup()
        ready_payload: Dict[str, Any] = {'warmup_seconds': warmup_seconds, 'device': device_str, 'dtype': str(compute_dtype), 'tf32': bool(allow_tf32), 'cudnn_benchmark': bool(cudnn_benchmark)}
        if device_str.startswith('cuda'):
            props = torch.cuda.get_device_properties(cuda_device)
            ready_payload.update({'gpu_name': props.name, 'vram_mb': round(props.total_memory / (1024 * 1024))})
        publish('ready', ready_payload, critical=True)
        while not fatal.is_set():
            try:
                command = command_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            kind = command[0]
            if kind == 'shutdown':
                break
            if kind == 'begin':
                _, audio, rate = command
                recognizer.begin_utterance(audio, rate)
            elif kind == 'audio':
                _, audio, rate, *gap = command
                dropped_before_samples = int(gap[0]) if gap else 0
                recognizer.accept_audio(audio, rate, dropped_before_samples=dropped_before_samples)
            elif kind == 'final':
                recognizer.finalize_utterance(reason=command[1])
            else:
                raise ValueError(f'未知 ASR 进程命令：{kind}')
    except BaseException as exc:
        publish('fatal', f'ASR 子进程异常：{exc}', critical=True)
    finally:
        if recognizer is not None:
            try:
                recognizer.shutdown()
            except Exception:
                pass

class IsolatedStreamingNemotronRecognizer:
    """Parent-side proxy for a cache-aware Nemotron worker process."""

    def __init__(self, model_name: str, on_result: Callable[[str, bool, int], None], model_revision: str='', sample_rate: int=16000, language: str='ja-JP', lookahead_tokens: int=13, normalize_audio: bool=True, final_padding_seconds: float=0.48, min_audio_rms: float=1e-05, on_error: Optional[Callable[[str], None]]=None, on_metrics: Optional[Callable[[Dict[str, Any]], None]]=None, on_discontinuity: Optional[Callable[[int], None]]=None, process_interval_seconds: float=0.08, intra_op_threads: int=4, inter_op_threads: int=1, allow_tf32: bool=True, cuda_device: int=0, cudnn_benchmark: bool=True, pin_memory: bool=True, preserve_audio: bool=True, audio_enqueue_timeout_seconds: float=5.0, utterance_id_offset: int=0):
        self.on_result = on_result
        self.on_error = on_error
        self.on_metrics = on_metrics
        self.on_discontinuity = on_discontinuity
        self.sample_rate = int(sample_rate)
        self._running = True
        self._shutdown_requested = Event()
        self._shutdown_lock = Lock()
        self._shutdown_complete = False
        self._ready = Event()
        self._ready_payload: Dict[str, Any] = {}
        self._startup_error: Optional[str] = None
        self._fatal_reported = False
        self._utterance_id_offset = max(0, int(utterance_id_offset))
        self._next_utterance_id = 0
        self._audio_batch_lock = Lock()
        self._audio_batch: List[Any] = []
        self._audio_batch_sample_count = 0
        self._audio_batch_target_samples = max(1, round(self.sample_rate * 0.08))
        self._ipc_dropped_audio_samples = 0
        self._ipc_drop_lock = Lock()
        self._last_ipc_drop_warning_at = 0.0
        self._preserve_audio = bool(preserve_audio)
        self._audio_enqueue_timeout_seconds = max(0.5, float(audio_enqueue_timeout_seconds))
        self._ctx = multiprocessing.get_context('spawn')
        self._command_queue = self._ctx.Queue(maxsize=32)
        self._critical_event_queue = self._ctx.Queue(maxsize=64)
        self._stream_event_queue = self._ctx.Queue(maxsize=64)
        self._result_condition = Condition(Lock())
        self._result_finals: deque[tuple[str, bool, int]] = deque()
        self._result_previews: OrderedDict[int, tuple[str, bool, int]] = OrderedDict()
        self._result_accepting = True
        self._result_dispatcher = Thread(target=self._dispatch_results, name='NemotronASRResultDispatcher', daemon=True)
        self._result_dispatcher.start()
        self._process = self._ctx.Process(target=isolated_nemotron_process_entrypoint, args=(self._command_queue, self._critical_event_queue, self._stream_event_queue, model_name, str(model_revision or ''), language, self.sample_rate, int(lookahead_tokens), bool(normalize_audio), float(final_padding_seconds), float(min_audio_rms), float(process_interval_seconds), int(intra_op_threads), int(inter_op_threads), bool(allow_tf32), int(cuda_device), bool(cudnn_benchmark), bool(pin_memory)), name='SubtitleNemotronASRProcess')
        try:
            self._process.start()
        except BaseException:
            self._stop_result_dispatcher(drain_finals=False, timeout=1.0)
            for channel in (self._command_queue, self._critical_event_queue, self._stream_event_queue):
                try:
                    channel.close()
                except Exception:
                    pass
            raise
        self._event_thread = Thread(target=self._pump_events, name='NemotronASREventPump', daemon=True)
        self._event_thread.start()

    def _report_fatal(self, message: str) -> None:
        if self._fatal_reported:
            return
        self._fatal_reported = True
        self._startup_error = message
        self._ready.set()
        self._running = False
        if self.on_error is not None:
            try:
                self.on_error(message)
            except Exception:
                logger.error('ASR 错误回调异常', exc_info=True)

    def _enqueue_result(self, text: str, is_final: bool, utterance_id: int) -> None:
        adjusted_id = self._utterance_id_offset + int(utterance_id)
        item = (str(text), bool(is_final), adjusted_id)
        with self._result_condition:
            if not self._result_accepting:
                return
            if is_final:
                self._result_finals.append(item)
                self._result_previews.pop(adjusted_id, None)
            else:
                self._result_previews[adjusted_id] = item
                self._result_previews.move_to_end(adjusted_id)
                while len(self._result_previews) > PREVIEW_RESULT_CACHE_MAX:
                    self._result_previews.popitem(last=False)
            self._result_condition.notify()

    def _dispatch_results(self) -> None:
        while True:
            with self._result_condition:
                while self._result_accepting and (not self._result_finals) and (not self._result_previews):
                    self._result_condition.wait(timeout=0.2)
                if not self._result_accepting and (not self._result_finals):
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
                logger.error('ASR 结果回调异常', exc_info=True)

    def _stop_result_dispatcher(self, *, drain_finals: bool, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.1, float(timeout))
        with self._result_condition:
            self._result_accepting = False
            if not drain_finals:
                self._result_finals.clear()
            self._result_previews.clear()
            self._result_condition.notify_all()
        thread = self._result_dispatcher
        if current_thread() is not thread:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return not thread.is_alive()

    def _pump_events(self) -> None:
        while self._running:
            event = None
            try:
                event = self._critical_event_queue.get_nowait()
            except queue.Empty:
                try:
                    event = self._stream_event_queue.get(timeout=0.1)
                except queue.Empty:
                    if self._process.exitcode is not None:
                        if self._shutdown_requested.is_set():
                            self._running = False
                            return
                        self._report_fatal(f'ASR 子进程意外退出，exitcode={self._process.exitcode}')
                        return
                    continue
                except (EOFError, OSError, ValueError) as exc:
                    if self._running:
                        self._report_fatal(f'ASR 流式事件通道中断：{exc}')
                    return
            except (EOFError, OSError, ValueError) as exc:
                if self._running:
                    self._report_fatal(f'ASR 关键事件通道中断：{exc}')
                return
            if event is None:
                continue
            kind, payload = event
            if kind == 'ready':
                self._ready_payload = dict(payload)
                self._ready.set()
            elif kind == 'result':
                text, is_final, utterance_id = payload
                self._enqueue_result(text, is_final, utterance_id)
            elif kind == 'metrics' and self.on_metrics is not None:
                adjusted = dict(payload)
                adjusted['utterance_id'] = self._utterance_id_offset + int(adjusted.get('utterance_id', 0))
                try:
                    self.on_metrics(adjusted)
                except Exception:
                    logger.error('ASR 指标回调异常', exc_info=True)
            elif kind == 'fatal':
                self._report_fatal(str(payload))
                return
            else:
                logger.warning('未知 ASR 事件：%s', kind)

    def warmup(self) -> float:
        if not self._ready.wait(timeout=180.0):
            raise TimeoutError('Nemotron ASR 子进程在 180 秒内未完成启动')
        if self._startup_error:
            raise RuntimeError(self._startup_error)
        return float(self._ready_payload.get('warmup_seconds', 0.0))

    def _send(self, command, critical: bool) -> bool:
        if not self._running or self._process.exitcode is not None:
            raise RuntimeError('Nemotron ASR 子进程未运行')
        try:
            if critical:
                self._command_queue.put(command, timeout=2.0)
            else:
                self._command_queue.put_nowait(command)
            return True
        except queue.Full as exc:
            if not critical:
                return False
            message = 'ASR 控制命令队列持续满载，无法保持 utterance 边界一致性'
            self._report_fatal(message)
            raise RuntimeError(message) from exc

    def _send_audio_batch(self, combined, sample_rate: int, critical: bool) -> bool:
        with self._ipc_drop_lock:
            dropped_before = max(0, int(self._ipc_dropped_audio_samples))
        command = ('audio', combined, int(sample_rate), dropped_before)
        if self._preserve_audio and (not critical):
            deadline = time.monotonic() + self._audio_enqueue_timeout_seconds
            sent = False
            while self._running and self._process.exitcode is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    self._command_queue.put(command, timeout=min(0.2, remaining))
                    sent = True
                    break
                except queue.Full:
                    continue
                except (EOFError, OSError, ValueError) as exc:
                    message = f'ASR 音频 IPC 通道中断：{exc}'
                    self._report_fatal(message)
                    raise RuntimeError(message) from exc
            if not sent:
                message = 'ASR 音频队列持续拥塞，已停止识别以避免静默丢音频；请降低系统/ASR负载或改用 --audio-backpressure-mode live'
                self._report_fatal(message)
                raise RuntimeError(message)
        else:
            sent = self._send(command, critical=critical)
        if sent:
            with self._ipc_drop_lock:
                self._ipc_dropped_audio_samples = max(0, self._ipc_dropped_audio_samples - dropped_before)
            if dropped_before:
                logger.warning('[ASR_IPC] recovered dropped_ms=%.0f action=split_discontinuity', dropped_before / self.sample_rate * 1000)
                if self.on_discontinuity is not None:
                    try:
                        self.on_discontinuity(dropped_before)
                    except Exception:
                        logger.error('ASR IPC 断流回调异常', exc_info=True)
            return True
        with self._ipc_drop_lock:
            self._ipc_dropped_audio_samples += len(combined)
            accumulated = self._ipc_dropped_audio_samples
        now = time.monotonic()
        if now - self._last_ipc_drop_warning_at >= 1.0:
            self._last_ipc_drop_warning_at = now
            logger.warning('[ASR_IPC] audio_queue_full accumulated_drop_ms=%.0f action=drop_audio_keep_process', accumulated / self.sample_rate * 1000)
        return False

    def begin_utterance(self, samples, sample_rate: Optional[int]=None) -> int:
        import numpy as numpy_for_ipc
        audio = numpy_for_ipc.asarray(samples, dtype=numpy_for_ipc.float32)
        self._send(('begin', audio, sample_rate or self.sample_rate), True)
        self._next_utterance_id += 1
        return self._utterance_id_offset + self._next_utterance_id

    def accept_audio(self, samples, sample_rate: Optional[int]=None) -> None:
        import numpy as numpy_for_ipc
        audio = numpy_for_ipc.asarray(samples, dtype=numpy_for_ipc.float32)
        if audio.size == 0:
            return
        batch = None
        with self._audio_batch_lock:
            self._audio_batch.append(audio)
            self._audio_batch_sample_count += len(audio)
            if self._audio_batch_sample_count >= self._audio_batch_target_samples:
                batch = self._audio_batch
                self._audio_batch = []
                self._audio_batch_sample_count = 0
        if batch:
            combined = batch[0] if len(batch) == 1 else numpy_for_ipc.concatenate(batch)
            self._send_audio_batch(combined, sample_rate or self.sample_rate, critical=False)

    def _flush_audio_batch(self) -> None:
        import numpy as numpy_for_ipc
        with self._audio_batch_lock:
            batch = self._audio_batch
            self._audio_batch = []
            self._audio_batch_sample_count = 0
        if batch:
            combined = batch[0] if len(batch) == 1 else numpy_for_ipc.concatenate(batch)
            self._send_audio_batch(combined, self.sample_rate, critical=True)

    def finalize_utterance(self, reason: str='endpoint') -> None:
        self._flush_audio_batch()
        self._send(('final', reason), True)

    def is_alive(self) -> bool:
        try:
            return bool(self._running and self._process.is_alive())
        except (AssertionError, ValueError, OSError):
            return False

    def shutdown(self) -> bool:
        with self._shutdown_lock:
            if self._shutdown_complete:
                return not self.is_alive()
            self._shutdown_complete = True
        shutdown_requested = getattr(self, '_shutdown_requested', None)
        if shutdown_requested is None:
            shutdown_requested = Event()
            self._shutdown_requested = shutdown_requested
        shutdown_requested.set()
        if not self._ready.is_set():
            self._startup_error = self._startup_error or 'Nemotron ASR 启动已取消'
            self._ready.set()
        with self._audio_batch_lock:
            self._audio_batch = []
            self._audio_batch_sample_count = 0
        with self._ipc_drop_lock:
            self._ipc_dropped_audio_samples = 0
        try:
            self._command_queue.put_nowait(('shutdown',))
        except Exception:
            pass

        def process_alive() -> bool:
            try:
                return bool(self._process.is_alive())
            except (AssertionError, ValueError, OSError):
                return False
        if process_alive():
            self._process.join(timeout=3.0)
        if process_alive():
            self._process.terminate()
            self._process.join(timeout=1.0)
        if process_alive() and hasattr(self._process, 'kill'):
            self._process.kill()
            self._process.join(timeout=0.5)
        if current_thread() is not self._event_thread and self._event_thread.is_alive():
            self._event_thread.join(timeout=1.5)
        if self._event_thread.is_alive():
            self._running = False
            if current_thread() is not self._event_thread:
                self._event_thread.join(timeout=0.5)
        else:
            self._running = False
        result_dispatcher_stopped = self._stop_result_dispatcher(drain_finals=True, timeout=5.0)
        for channel in (self._command_queue, self._critical_event_queue, self._stream_event_queue):
            try:
                channel.cancel_join_thread()
            except Exception:
                logger.debug('ASR IPC cancel_join_thread 失败', exc_info=True)
            try:
                channel.close()
            except Exception:
                logger.debug('ASR IPC close 失败', exc_info=True)
        stopped = not process_alive() and (not self._event_thread.is_alive()) and result_dispatcher_stopped
        try:
            self._process.close()
        except Exception:
            pass
        return stopped

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
        return (self.ok, self.value)

class SessionActor:
    """Single-threaded, deadline-aware session actor for ASR state."""

    def __init__(self, session: 'RealtimeSubtitleSession'):
        self._session = session
        self._commands: queue.Queue = queue.Queue(maxsize=SESSION_ACTOR_COMMAND_QUEUE_SIZE)
        self._closed = Event()
        self._stop_requested = Event()
        self._thread = Thread(target=self._run, name='SubtitleSessionActor', daemon=False)
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
                self._commands.task_done()
                return
            fn, args, kwargs, result_slot, deadline, cancelled = item
            started = time.monotonic()
            try:
                if cancelled.is_set() or time.monotonic() > deadline:
                    result_slot.set(False, TimeoutError('Session command expired before execution'))
                    continue
                value = fn(*args, **kwargs)
                if not cancelled.is_set():
                    result_slot.set(True, value)
            except BaseException as exc:
                result_slot.set(False, exc)
            finally:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                if elapsed_ms > 100:
                    logger.warning('[SESSION_ACTOR_SLOW_COMMAND] method=%s elapsed_ms=%d', getattr(fn, '__name__', repr(fn)), elapsed_ms)
                self._commands.task_done()

    def _call(self, method: str, *args, timeout: float=15.0, **kwargs):
        if self._closed.is_set() or self._stop_requested.is_set():
            raise RuntimeError('Subtitle Session Actor 已关闭')
        timeout = max(0.1, float(timeout))
        deadline = time.monotonic() + timeout
        cancelled = Event()
        result_slot = _ResultSlot()
        fn = getattr(self._session, method)
        self._commands.put((fn, args, kwargs, result_slot, deadline, cancelled), timeout=min(2.0, timeout))
        try:
            ok, value = result_slot.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty as exc:
            cancelled.set()
            raise TimeoutError(f'Subtitle Session Actor command timeout: {method}') from exc
        if ok:
            return value
        raise value

    def ingest_asr(self, *args, **kwargs):
        return self._call('ingest_asr', *args, **kwargs)

    def latest_source(self, *args, **kwargs):
        return self._call('latest_source', *args, **kwargs)

    def close(self, timeout: float=5.0) -> bool:
        if self._closed.is_set():
            return not self._thread.is_alive()
        underlying_closed = False
        try:
            self._call('close', timeout=max(1.0, timeout / 2))
            underlying_closed = True
        except Exception:
            logger.error('Subtitle Session Actor underlying close failed', exc_info=True)
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
                        result_slot.set(False, RuntimeError('Session Actor closed before execution'))
                finally:
                    self._commands.task_done()
            if current_thread() is not self._thread:
                self._thread.join(timeout=max(0.1, timeout))
            if not underlying_closed and (not self._thread.is_alive()):
                try:
                    self._session.close()
                    underlying_closed = True
                except Exception:
                    logger.error('Subtitle Session direct close fallback failed', exc_info=True)
        return not self._thread.is_alive() and underlying_closed

class SubtitleApp:

    def __init__(self, args):
        self.args = args
        required_runtime_args = ('vad_threshold', 'min_silence_duration', 'min_speech_duration', 'speech_preroll_ms', 'max_utterance_seconds', 'forced_segment_overlap_ms', 'disable_hybrid_endpoint', 'endpoint_punctuation_hold_ms', 'endpoint_min_utterance_ms', 'max_drain_chunks')
        missing_runtime_args = [name for name in required_runtime_args if not hasattr(args, name)]
        if missing_runtime_args:
            raise ValueError('SubtitleApp 缺少已验证的运行参数：' + ', '.join(missing_runtime_args))
        self._persisted_settings = _load_application_settings()
        persisted_display = _apply_persisted_settings_to_args(args, self._persisted_settings)
        configure_current_process_priority('above_normal')
        self.session_id = uuid.uuid4().hex[:12]
        self.killed = False
        self._lifecycle_lock = Lock()
        self._lifecycle_state = 'running'
        self._transcript_event_queue: queue.Queue = queue.Queue(maxsize=512)
        self._transcript_controller_stop = Event()
        self._transcript_controller_thread: Optional[Thread] = None
        self._recovery_replay_lock = Lock()
        self._recovery_replay_running = False
        self._recovery_replay_next_at = 0.0
        self._recovery_replay_backoff = 1.0
        self._recovery_inflight_record_ids: set[str] = set()
        self.subtitle_history_store = SubtitleHistoryStore(os.path.join(_data_dir, 'subtitle-history.sqlite3'))
        try:
            self.event_bus = SessionEventBus(max_queue_size=512, max_pending_finals=200, recovery_path=os.path.join(_data_dir, 'pending-final-events.jsonl'), final_burst=8)
            self.subtitle_history_writer = SubtitleHistoryWriter(self.subtitle_history_store, recovery_spill=self.event_bus.spill_final)
        except BaseException:
            event_bus = getattr(self, 'event_bus', None)
            if event_bus is not None:
                try:
                    event_bus.close(drain=False, timeout=2.0)
                except Exception:
                    logger.error('SubtitleApp 构造回滚关闭 EventBus 失败', exc_info=True)
            try:
                self.subtitle_history_store.close()
            except Exception:
                logger.error('SubtitleApp 构造回滚关闭 HistoryStore 失败', exc_info=True)
            raise
        self.pipeline_health = PipelineHealth(warning=20, degraded=50, critical=100)
        self._health_stop_event = Event()
        self._health_thread: Optional[Thread] = None
        self._recovery_replayed = 0
        raw_subtitle_session = RealtimeSubtitleSession(self.session_id, self.event_bus, source_stability_window=getattr(args, 'stable_partial_threshold', 3))
        try:
            self.subtitle_session = SessionActor(raw_subtitle_session)
        except BaseException:
            self.subtitle_history_writer.close(drain=False, timeout=2.0)
            self.event_bus.close(drain=False, timeout=2.0)
            self.subtitle_history_store.close()
            raise
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
        self._endpoint_text = ''
        self._endpoint_last_change_at = 0.0
        self._endpoint_stable_punctuation = ''
        self._endpoint_stable_punctuation_at = 0.0
        self._asr_failure_event = Event()
        self._asr_failure_message = ''
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
        self._runtime_settings: Dict[str, Any] = {'vad_threshold': float(args.vad_threshold), 'min_silence_duration': float(args.min_silence_duration), 'min_speech_duration': float(args.min_speech_duration), 'speech_preroll_ms': int(args.speech_preroll_ms), 'max_utterance_seconds': float(args.max_utterance_seconds), 'forced_segment_overlap_ms': int(args.forced_segment_overlap_ms), 'enable_hybrid_endpoint': not bool(args.disable_hybrid_endpoint), 'endpoint_punctuation_hold_ms': int(args.endpoint_punctuation_hold_ms), 'endpoint_min_utterance_ms': int(args.endpoint_min_utterance_ms), 'max_drain_chunks': int(args.max_drain_chunks)}
        self.settings_window: Optional[tk.Toplevel] = None
        self._ui_preferences: Dict[str, Any] = {'source_font_size': 30, 'opacity': 0.95, 'topmost': True}
        self._ui_preferences.update(persisted_display)
        self.ui_buffer_lock = Lock()
        self.ui_update_buffer: Dict[str, Any] = {'SRC': None, 'SRC_DISPLAY_TEXT': '', 'SRC_FULL_TEXT': '', 'SRC_DISPLAY_IS_FINAL': False, 'SRC_DISPLAY_AT': 0.0, 'SRC_UTTERANCE_ID': 0, 'IS_FINAL': False, 'SOURCE_REVISION': 0, 'SOURCE_STATE': 'interim', 'STABLE_SOURCE_TEXT': '', 'UNSTABLE_SOURCE_TEXT': ''}
        self.ui_event_queue: queue.Queue = queue.Queue(maxsize=100)
        self.ui_error_queue: queue.Queue = queue.Queue(maxsize=32)
        self._last_wrap_width = 0
        self._last_rendered: Dict[str, tuple] = {'status': (None, None), 'source': (None, None)}
        self._last_source_render_at = 0.0
        self.root: Optional[tk.Tk] = None
        self.subtitle_label: Optional[tk.Label] = None
        self.status_label: Optional[tk.Label] = None
        self.settings_button: Optional[tk.Button] = None
        self.async_asr: Optional[Any] = None
        self.recognizer = None
        self.vad = None
        self.sample_rate = 16000
        self.stats = {'recognize_count': 0, 'audio_chunks': 0, 'dropped_audio_chunks': 0, 'max_audio_queue_depth': 0, 'max_audio_age_ms': 0, 'audio_discontinuity_count': 0, 'asr_tasks': 0, 'max_asr_queue_latency_ms': 0, 'max_asr_inference_latency_ms': 0, 'asr_restart_count': 0, 'ui_render_count': 0, 'ui_skipped_render_count': 0, 'subtitle_revision_count': 0, 'subtitle_flicker_count': 0, 'forced_endpoint_count': 0, 'forced_overlap_deduplicated_chars': 0, 'hybrid_endpoint_count': 0, 'subtitle_interim_count': 0, 'subtitle_stable_count': 0, 'subtitle_final_count': 0, 'dropped_transcript_previews': 0}
        self._stats_lock = Lock()
        self.selected_devices: List[int] = []
        self._last_audio_packet_at = 0.0
        self._last_asr_result_at = 0.0
        try:
            self.event_bus.subscribe(RealtimeSubtitleSession.TRANSCRIPT_EVENT, self._queue_transcript_event, session_guard=self._event_session_guard)
            self._start_transcript_controller()
            self._start_health_supervisor()
        except BaseException:
            self._health_stop_event.set()
            self._transcript_controller_stop.set()
            thread = self._transcript_controller_thread
            if thread is not None and current_thread() is not thread:
                thread.join(timeout=1.0)
            try:
                self.subtitle_session.close(timeout=2.0)
            except Exception:
                logger.error('SubtitleApp 构造回滚关闭 SessionActor 失败', exc_info=True)
            try:
                self.event_bus.close(drain=False, timeout=2.0)
            except Exception:
                logger.error('SubtitleApp 构造回滚关闭 EventBus 失败', exc_info=True)
            try:
                self.subtitle_history_writer.close(drain=False, timeout=2.0)
            except Exception:
                logger.error('SubtitleApp 构造回滚关闭 HistoryWriter 失败', exc_info=True)
            try:
                self.subtitle_history_store.close()
            except Exception:
                logger.error('SubtitleApp 构造回滚关闭 HistoryStore 失败', exc_info=True)
            raise
        logger.info('[SESSION] session_id=%s created', self.session_id)

    def _get_loopback_devices(self, p_audio) -> List[Dict[str, Any]]:
        try:
            return list(p_audio.get_loopback_device_info_generator())
        except Exception as e:
            logger.warning(f'无法枚举 WASAPI loopback 设备：{e}')
            return []

    def _find_default_loopback(self, p_audio, loopbacks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not loopbacks:
            return None
        try:
            import pyaudiowpatch as pyaudio_wp
            wasapi_info = p_audio.get_host_api_info_by_type(pyaudio_wp.paWASAPI)
            default_speakers = p_audio.get_device_info_by_index(wasapi_info['defaultOutputDevice'])
            default_name = default_speakers.get('name', '')
            if default_speakers.get('isLoopbackDevice', False):
                return default_speakers
            for loopback in loopbacks:
                loopback_name = loopback.get('name', '')
                if default_name and (default_name in loopback_name or loopback_name in default_name):
                    return loopback
        except Exception as e:
            logger.warning(f'查找默认系统播放设备失败：{e}')
        for loopback in loopbacks:
            if loopback.get('isDefaultLoopbackDevice', False):
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
        dialog = tk.Toplevel(self.root)
        dialog.title('选择音频来源（默认监听系统声音）')
        sw, sh = (dialog.winfo_screenwidth(), dialog.winfo_screenheight())
        width = min(920, max(640, int(sw * 0.88)))
        height = min(560, max(420, int(sh * 0.72)))
        x = max(0, int((sw - width) / 2))
        y = max(0, int((sh - height) / 2))
        dialog.geometry(f'{width}x{height}+{x}+{y}')
        dialog.minsize(640, 420)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.focus_set()
        selection_summary = tk.StringVar(value='')
        ok_button_ref: Dict[str, Optional[tk.Button]] = {'button': None}

        def selected_now() -> List[int]:
            return [idx for idx, var in device_vars.items() if var.get()]

        def refresh_selection_summary() -> None:
            chosen = selected_now()
            mic_count = sum((1 for idx in chosen if idx in mic_device_ids))
            system_count = sum((1 for idx in chosen if idx in loopback_device_ids))
            if chosen:
                selection_summary.set(f'已选择 {len(chosen)} 个设备：系统声音 {system_count} 个，麦克风 {mic_count} 个')
            else:
                selection_summary.set('未选择音频来源')
            ok_button = ok_button_ref['button']
            if ok_button is not None:
                ok_button.config(state=tk.NORMAL if chosen else tk.DISABLED)

        def set_selected(groups: List[set]) -> None:
            allowed = set()
            for group in groups:
                allowed.update(group)
            for idx, var in device_vars.items():
                var.set(idx in allowed)
            refresh_selection_summary()

        def add_device_option(parent, group: set, idx: int, name: str, selected: bool, prefix: str='', bold: bool=False) -> None:
            group.add(idx)
            if idx not in device_vars:
                device_vars[idx] = tk.BooleanVar(value=selected)
            elif selected:
                device_vars[idx].set(True)
            label = f'{prefix}{idx}: {name}'
            checkbutton_options = {'text': label, 'variable': device_vars[idx], 'command': refresh_selection_summary, 'wraplength': max(260, int(width * 0.42)), 'justify': tk.LEFT, 'anchor': 'w'}
            if bold:
                checkbutton_options['font'] = ('Arial', 9, 'bold')
            tk.Checkbutton(parent, **checkbutton_options).pack(anchor='w', fill='x', padx=10, pady=2)

        def on_cancel():
            selected_devices.clear()
            try:
                dialog.grab_release()
            except Exception:
                pass
            dialog.destroy()
        tk.Label(dialog, text='默认已选择系统播放声音（WASAPI Loopback）。关闭麦克风/录音设备不会影响系统声音识别。', font=('Microsoft YaHei', 10), fg='#0044AA', wraplength=width - 40, justify=tk.LEFT).pack(fill='x', padx=12, pady=(10, 0))

        def on_ok():
            chosen = selected_now()
            if not chosen:
                messagebox.showwarning('请选择音频来源', '请至少选择一个系统声音或麦克风设备。', parent=dialog)
                return
            selected_devices.extend(chosen)
            try:
                dialog.grab_release()
            except Exception:
                pass
            dialog.destroy()
        main_frame = tk.Frame(dialog)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        left_frame = tk.Frame(main_frame, relief=tk.RIDGE, borderwidth=2)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        tk.Label(left_frame, text='麦克风/输入设备（默认不选）', font=('Arial', 12, 'bold')).pack(pady=5)
        left_canvas = tk.Canvas(left_frame)
        left_sb = tk.Scrollbar(left_frame, orient='vertical', command=left_canvas.yview)
        left_inner = tk.Frame(left_canvas)
        left_inner.bind('<Configure>', lambda e: left_canvas.configure(scrollregion=left_canvas.bbox('all')))
        left_window = left_canvas.create_window((0, 0), window=left_inner, anchor='nw')
        left_canvas.bind('<Configure>', lambda e: left_canvas.itemconfigure(left_window, width=e.width))
        left_canvas.configure(yscrollcommand=left_sb.set)
        default_mic_added = False
        try:
            default_input = p_audio.get_default_input_device_info()
            didx = default_input['index']
            mic_selected = capture_source in ('mic', 'both')
            add_device_option(left_inner, mic_device_ids, didx, default_input['name'], mic_selected, prefix='[默认麦克风] ', bold=True)
            tk.Frame(left_inner, height=2, bg='gray').pack(fill=tk.X, padx=10, pady=5)
            default_mic_added = True
        except Exception:
            didx = -1
        input_devices = [(i, d) for i, d in devices if d['maxInputChannels'] > 0]
        mic_fallback_selected = capture_source == 'mic' and didx == -1
        for idx, dev in input_devices:
            if idx == didx:
                continue
            add_device_option(left_inner, mic_device_ids, idx, dev['name'], mic_fallback_selected)
            mic_fallback_selected = False
        if not default_mic_added and (not input_devices):
            tk.Label(left_inner, text='未找到麦克风输入设备。', fg='#888888', wraplength=max(260, int(width * 0.42)), justify=tk.LEFT).pack(anchor='w', padx=10, pady=8)
        left_canvas.pack(side='left', fill='both', expand=True)
        left_sb.pack(side='right', fill='y')
        right_frame = tk.Frame(main_frame, relief=tk.RIDGE, borderwidth=2)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=5)
        tk.Label(right_frame, text='系统声音/WASAPI Loopback（默认选择）', font=('Arial', 12, 'bold')).pack(pady=5)
        right_canvas = tk.Canvas(right_frame)
        right_sb = tk.Scrollbar(right_frame, orient='vertical', command=right_canvas.yview)
        right_inner = tk.Frame(right_canvas)
        right_inner.bind('<Configure>', lambda e: right_canvas.configure(scrollregion=right_canvas.bbox('all')))
        right_window = right_canvas.create_window((0, 0), window=right_inner, anchor='nw')
        right_canvas.bind('<Configure>', lambda e: right_canvas.itemconfigure(right_window, width=e.width))
        right_canvas.configure(yscrollcommand=right_sb.set)
        loopbacks = self._get_loopback_devices(p_audio)
        default_loopback = self._find_default_loopback(p_audio, loopbacks)
        default_loopback_idx = default_loopback['index'] if default_loopback else -1
        system_selected = capture_source in ('system', 'both')
        if default_loopback is not None:
            add_device_option(right_inner, loopback_device_ids, default_loopback_idx, default_loopback['name'], system_selected, prefix='[默认系统声音] ', bold=True)
            tk.Frame(right_inner, height=2, bg='gray').pack(fill=tk.X, padx=10, pady=5)
        for loopback in loopbacks:
            idx = loopback['index']
            if idx == default_loopback_idx:
                continue
            add_device_option(right_inner, loopback_device_ids, idx, loopback['name'], False)
        if not loopbacks:
            tk.Label(right_inner, text='未找到系统声音 loopback 设备。请安装 PyAudioWPatch，并确认 Windows 默认播放设备可用。', fg='#CC0000', wraplength=max(260, int(width * 0.42)), justify=tk.LEFT).pack(anchor='w', padx=10, pady=8)
        right_canvas.pack(side='left', fill='both', expand=True)
        right_sb.pack(side='right', fill='y')
        footer = tk.Frame(dialog)
        footer.pack(fill='x', padx=12, pady=(0, 10))
        tk.Label(footer, textvariable=selection_summary, anchor='w', fg='#555555').pack(fill='x', pady=(0, 6))
        quick_frame = tk.Frame(footer)
        quick_frame.pack(fill='x')
        tk.Button(quick_frame, text='仅系统声音', command=lambda: set_selected([loopback_device_ids]), state=tk.NORMAL if loopback_device_ids else tk.DISABLED).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(quick_frame, text='仅麦克风', command=lambda: set_selected([mic_device_ids]), state=tk.NORMAL if mic_device_ids else tk.DISABLED).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(quick_frame, text='系统+麦克风', command=lambda: set_selected([loopback_device_ids, mic_device_ids]), state=tk.NORMAL if loopback_device_ids and mic_device_ids else tk.DISABLED).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(quick_frame, text='取消', command=on_cancel, width=10).pack(side=tk.RIGHT, padx=(6, 0))
        ok_button = tk.Button(quick_frame, text='确定开始', command=on_ok, font=('Arial', 11, 'bold'), width=12)
        ok_button.pack(side=tk.RIGHT)
        ok_button_ref['button'] = ok_button
        refresh_selection_summary()
        dialog.protocol('WM_DELETE_WINDOW', on_cancel)
        dialog.bind('<Return>', lambda _event: on_ok())
        dialog.bind('<Escape>', lambda _event: on_cancel())
        root = self.root
        if root is None:
            return []
        root.wait_window(dialog)
        logger.info(f'用户选择的设备：{selected_devices}')
        return selected_devices

    def build_ui(self) -> None:
        self.root = tk.Tk()
        self.root.title('实时日语字幕（Nemotron 3.5）')
        self.root.attributes('-topmost', bool(self._ui_preferences['topmost']))
        self.root.attributes('-alpha', float(self._ui_preferences['opacity']))
        self.root.configure(bg='black')
        sw, sh = (self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        initial_width = int(sw * 0.85)
        initial_wrap = max(360, initial_width - 60)
        self.root.geometry(f'{initial_width}x150+{int(sw * 0.075)}+{sh - 230}')
        self.root.resizable(True, True)
        header_frame = tk.Frame(self.root, bg='black')
        header_frame.pack(fill='x', padx=10, pady=(5, 0))
        self.status_label = tk.Label(header_frame, text='● 初始化中...', font=('SimHei', 11), fg='#FFAA00', bg='black', anchor='w')
        self.status_label.pack(side=tk.LEFT, fill='x', expand=True)
        self.settings_button = tk.Button(header_frame, text='⚙ 设置', command=self.open_settings_dialog, font=('Microsoft YaHei', 9), fg='#FFFFFF', bg='#303030', activeforeground='#FFFFFF', activebackground='#505050', relief=tk.FLAT, padx=10, pady=2, cursor='hand2')
        self.settings_button.pack(side=tk.RIGHT, padx=(8, 0))
        self.subtitle_label = tk.Label(self.root, text='等待语音...', font=('Microsoft YaHei', int(self._ui_preferences['source_font_size'])), fg='#00FF00', bg='black', wraplength=initial_wrap, justify='left', anchor='w')
        self.subtitle_label.pack(expand=True, fill='both', padx=20, pady=(10, 12))
        self.root.bind('<Configure>', self._on_window_configure)
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)

    def _runtime_settings_snapshot(self) -> Dict[str, Any]:
        with self._runtime_settings_lock:
            return dict(self._runtime_settings)

    def _request_runtime_settings(self, settings: Dict[str, Any]) -> None:
        with self._runtime_settings_lock:
            self._runtime_settings.update(settings)
        self._runtime_settings_changed.set()
        logger.info('[RUNTIME_SETTINGS] requested threshold=%.2f silence=%.2fs speech=%.2fs preroll=%dms max_utterance=%.1fs overlap=%dms hybrid=%s hold=%dms min_endpoint=%dms drain=%d', settings['vad_threshold'], settings['min_silence_duration'], settings['min_speech_duration'], settings['speech_preroll_ms'], settings['max_utterance_seconds'], settings['forced_segment_overlap_ms'], settings['enable_hybrid_endpoint'], settings['endpoint_punctuation_hold_ms'], settings['endpoint_min_utterance_ms'], settings['max_drain_chunks'])

    def _apply_display_preferences(self, preferences: Dict[str, Any]) -> None:
        self._ui_preferences.update(preferences)
        if self.subtitle_label is not None:
            self.subtitle_label.config(font=('Microsoft YaHei', int(self._ui_preferences['source_font_size'])))
        if self.root is not None:
            self.root.attributes('-alpha', float(self._ui_preferences['opacity']))
            self.root.attributes('-topmost', bool(self._ui_preferences['topmost']))

    def _persist_settings_snapshot(self, audio_settings: Dict[str, Any], display_preferences: Dict[str, Any]) -> None:
        payload = {'schema_version': 2, 'audio': dict(audio_settings), 'display': {'source_font_size': int(display_preferences['source_font_size']), 'opacity': float(display_preferences['opacity']), 'topmost': bool(display_preferences['topmost'])}}
        _save_application_settings(payload)

    def open_settings_dialog(self) -> None:
        if self.root is None:
            return
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_force()
            return
        dialog = tk.Toplevel(self.root)
        self.settings_window = dialog
        dialog.title('实时字幕设置')
        dialog.geometry('540x620')
        dialog.minsize(500, 560)
        dialog.transient(self.root)
        container = ttk.Frame(dialog, padding=12)
        container.pack(fill='both', expand=True)
        current = self._runtime_settings_snapshot()
        vars_map = {'vad_threshold': tk.DoubleVar(value=current['vad_threshold']), 'min_silence_duration': tk.DoubleVar(value=current['min_silence_duration']), 'min_speech_duration': tk.DoubleVar(value=current['min_speech_duration']), 'speech_preroll_ms': tk.IntVar(value=current['speech_preroll_ms']), 'max_utterance_seconds': tk.DoubleVar(value=current['max_utterance_seconds']), 'forced_segment_overlap_ms': tk.IntVar(value=current['forced_segment_overlap_ms']), 'enable_hybrid_endpoint': tk.BooleanVar(value=current['enable_hybrid_endpoint']), 'endpoint_punctuation_hold_ms': tk.IntVar(value=current['endpoint_punctuation_hold_ms']), 'endpoint_min_utterance_ms': tk.IntVar(value=current['endpoint_min_utterance_ms']), 'max_drain_chunks': tk.IntVar(value=current['max_drain_chunks']), 'source_font_size': tk.IntVar(value=int(self._ui_preferences['source_font_size'])), 'opacity': tk.DoubleVar(value=float(self._ui_preferences['opacity'])), 'topmost': tk.BooleanVar(value=bool(self._ui_preferences['topmost']))}
        rows = [('VAD 阈值', 'vad_threshold'), ('静默分段秒数', 'min_silence_duration'), ('最短语音秒数', 'min_speech_duration'), ('语音起点预录 ms', 'speech_preroll_ms'), ('连续语音最大秒数', 'max_utterance_seconds'), ('强制分段重叠 ms', 'forced_segment_overlap_ms'), ('句末保持 ms', 'endpoint_punctuation_hold_ms'), ('句末分段最短时长 ms', 'endpoint_min_utterance_ms'), ('每轮最多读取音频块', 'max_drain_chunks'), ('字幕字号', 'source_font_size'), ('窗口透明度', 'opacity')]
        ttk.Label(container, text='识别与显示', font=('Microsoft YaHei', 11, 'bold')).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 10))
        row_index = 1
        for label, key in rows:
            ttk.Label(container, text=label).grid(row=row_index, column=0, sticky='w', padx=(0, 12), pady=5)
            ttk.Entry(container, textvariable=vars_map[key], width=18).grid(row=row_index, column=1, sticky='ew', pady=5)
            row_index += 1
        ttk.Checkbutton(container, text='启用稳定句末混合分段', variable=vars_map['enable_hybrid_endpoint']).grid(row=row_index, column=0, columnspan=2, sticky='w', pady=6)
        row_index += 1
        ttk.Checkbutton(container, text='窗口始终置顶', variable=vars_map['topmost']).grid(row=row_index, column=0, columnspan=2, sticky='w', pady=6)
        row_index += 1
        container.columnconfigure(1, weight=1)

        def collect() -> tuple[Dict[str, Any], Dict[str, Any]]:
            audio_settings = {'vad_threshold': float(vars_map['vad_threshold'].get()), 'min_silence_duration': float(vars_map['min_silence_duration'].get()), 'min_speech_duration': float(vars_map['min_speech_duration'].get()), 'speech_preroll_ms': int(vars_map['speech_preroll_ms'].get()), 'max_utterance_seconds': float(vars_map['max_utterance_seconds'].get()), 'forced_segment_overlap_ms': int(vars_map['forced_segment_overlap_ms'].get()), 'enable_hybrid_endpoint': bool(vars_map['enable_hybrid_endpoint'].get()), 'endpoint_punctuation_hold_ms': int(vars_map['endpoint_punctuation_hold_ms'].get()), 'endpoint_min_utterance_ms': int(vars_map['endpoint_min_utterance_ms'].get()), 'max_drain_chunks': int(vars_map['max_drain_chunks'].get())}
            display_preferences = {'source_font_size': max(12, min(72, int(vars_map['source_font_size'].get()))), 'opacity': max(0.5, min(1.0, float(vars_map['opacity'].get()))), 'topmost': bool(vars_map['topmost'].get())}
            candidate = argparse.Namespace(**vars(self.args))
            for key, value in audio_settings.items():
                if key == 'enable_hybrid_endpoint':
                    candidate.disable_hybrid_endpoint = not value
                else:
                    setattr(candidate, key, value)
            validate_args(candidate)
            return (audio_settings, display_preferences)

        def apply_settings() -> None:
            try:
                audio_settings, display_preferences = collect()
                self._request_runtime_settings(audio_settings)
                self._apply_display_preferences(display_preferences)
                self._persist_settings_snapshot(audio_settings, display_preferences)
                self._post_ui_event(('settings_result', True, '设置已应用并保存'))
            except Exception as exc:
                logger.error('设置应用失败：%s', exc, exc_info=True)
                self._post_ui_event(('settings_result', False, str(exc)))

        def restore_recommended() -> None:
            defaults = {'vad_threshold': 0.4, 'min_silence_duration': 0.9, 'min_speech_duration': 0.12, 'speech_preroll_ms': 1000, 'max_utterance_seconds': 18.0, 'forced_segment_overlap_ms': 560, 'enable_hybrid_endpoint': True, 'endpoint_punctuation_hold_ms': 320, 'endpoint_min_utterance_ms': 1200, 'max_drain_chunks': 20, 'source_font_size': 30, 'opacity': 0.95, 'topmost': True}
            for key, value in defaults.items():
                vars_map[key].set(value)
        button_bar = ttk.Frame(dialog, padding=(12, 4, 12, 12))
        button_bar.pack(fill='x')
        ttk.Button(button_bar, text='恢复推荐值', command=restore_recommended).pack(side=tk.LEFT)
        ttk.Button(button_bar, text='关闭', command=dialog.destroy).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(button_bar, text='应用', command=apply_settings).pack(side=tk.RIGHT)

        def on_destroy(event) -> None:
            if event.widget is dialog:
                self.settings_window = None
        dialog.bind('<Destroy>', on_destroy)
        dialog.protocol('WM_DELETE_WINDOW', dialog.destroy)
        dialog.lift()
        dialog.focus_force()

    def _on_window_configure(self, event) -> None:
        if self.root is None or event.widget is not self.root:
            return
        self._update_text_wrap(event.width)

    def _update_text_wrap(self, width: int) -> None:
        wrap_width = max(260, width - 60)
        if abs(wrap_width - self._last_wrap_width) < 16:
            return
        self._last_wrap_width = wrap_width
        if self.subtitle_label:
            self.subtitle_label.config(wraplength=wrap_width)

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
                logger.debug('显示错误对话框失败', exc_info=True)
            work_remaining = True
        newest_status = None
        processed = 0
        for _ in range(20):
            try:
                event = self.ui_event_queue.get_nowait()
            except queue.Empty:
                break
            if not event:
                continue
            processed += 1
            kind = event[0]
            if kind == 'status':
                newest_status = event
            elif kind == 'settings_result':
                _, ok, message = event
                newest_status = ('status', f'● {message}', '#00AA00' if ok else '#FF0000')
                if not ok:
                    self._post_ui_event(('error_dialog', '设置应用失败', message))
        if newest_status is not None and self.status_label:
            _, status, color = newest_status
            self._configure_label_if_changed(self.status_label, 'status', status, color)
        if processed >= 20:
            work_remaining = True
        with self.ui_buffer_lock:
            if self.ui_update_buffer['SRC'] is not None and self.subtitle_label:
                src_text = self.ui_update_buffer['SRC']
                is_final = self.ui_update_buffer['IS_FINAL']
                fg_color = '#00FF00' if is_final else '#00CC00'
                self._configure_label_if_changed(self.subtitle_label, 'source', src_text, fg_color)
                self.ui_update_buffer['SRC'] = None
        try:
            self.root.after(16 if work_remaining else 50, self.sync_ui)
        except tk.TclError:
            pass

    def _configure_label_if_changed(self, label, channel: str, text: str, color: str, now: Optional[float]=None) -> bool:
        previous_text, previous_color = self._last_rendered[channel]
        if text == previous_text and color == previous_color:
            self._stat_add('ui_skipped_render_count', 1)
            return False
        rendered_at = time.monotonic() if now is None else now
        if channel == 'source' and previous_text not in (None, '') and (text != previous_text):
            self._stat_add('subtitle_revision_count', 1)
            if rendered_at - self._last_source_render_at < 0.3:
                self._stat_add('subtitle_flicker_count', 1)
        if channel == 'source':
            self._last_source_render_at = rendered_at
        label.config(text=text, fg=color)
        self._last_rendered[channel] = (text, color)
        self._stat_add('ui_render_count', 1)
        return True

    def _post_ui_event(self, event: tuple) -> None:
        if event and event[0] == 'error_dialog':
            try:
                self.ui_error_queue.put_nowait(event)
            except queue.Full:
                try:
                    self.ui_error_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.ui_error_queue.put_nowait(event)
                except queue.Full:
                    logger.error('UI 错误队列持续满载，无法投递：%s', event[1])
            return
        try:
            self.ui_event_queue.put_nowait(event)
        except queue.Full:
            try:
                while True:
                    self.ui_event_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.ui_event_queue.put_nowait(event)
            except queue.Full:
                pass

    def update_status(self, status: str, color: str='#666666') -> None:
        """线程安全地更新状态栏。子线程只投递事件，主线程统一消费。"""
        self._post_ui_event(('status', status, color))

    def show_error_dialog(self, title: str, message: str) -> None:
        logger.error(f'{title}: {message}')
        try:
            if self.root is not None and current_thread() is main_thread():
                messagebox.showerror(title, message, parent=self.root)
            else:
                self._post_ui_event(('error_dialog', title, message))
        except Exception:
            logger.debug('投递或显示错误对话框失败', exc_info=True)

    def _reset_endpoint_hints(self) -> None:
        with self._endpoint_lock:
            self._endpoint_text = ''
            self._endpoint_last_change_at = 0.0
            self._endpoint_stable_punctuation = ''
            self._endpoint_stable_punctuation_at = 0.0

    def _update_endpoint_hints(self, source_state: SubtitleRevisionState, now: float, is_final: bool) -> None:
        with self._endpoint_lock:
            if is_final:
                self._endpoint_text = ''
                self._endpoint_last_change_at = 0.0
                self._endpoint_stable_punctuation = ''
                self._endpoint_stable_punctuation_at = 0.0
                return
            if source_state.text != self._endpoint_text:
                self._endpoint_text = source_state.text
                self._endpoint_last_change_at = now
            stable = source_state.stable_text
            stable_boundary = ''
            if stable:
                last_boundary = 0
                for index, char in enumerate(stable):
                    if char in '。！？!?':
                        last_boundary = index + 1
                if last_boundary > 0:
                    stable_boundary = stable[:last_boundary]
            if stable_boundary:
                if stable_boundary != self._endpoint_stable_punctuation:
                    self._endpoint_stable_punctuation = stable_boundary
                    self._endpoint_stable_punctuation_at = now
            else:
                self._endpoint_stable_punctuation = ''
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
            return now - self._endpoint_stable_punctuation_at >= hold_seconds

    def _set_lifecycle_state(self, state: str) -> None:
        with self._lifecycle_lock:
            self._lifecycle_state = str(state)

    def _event_session_guard(self, event: Any) -> bool:
        if getattr(event, 'session_id', '') != self.session_id:
            return False
        with self._lifecycle_lock:
            state = self._lifecycle_state
        if state == 'running':
            return True
        if state in ('stopping_input', 'draining_finals'):
            return bool(getattr(event, 'is_final', False))
        return False

    def _accept_pipeline_callback(self, *, is_final: bool) -> bool:
        with self._lifecycle_lock:
            state = self._lifecycle_state
        if state == 'running':
            return True
        return bool(is_final and state in ('stopping_input', 'draining_finals'))

    def _queue_transcript_event(self, event: TranscriptEvent) -> None:
        if not self._event_session_guard(event):
            self.event_bus.spill_final('transcript_controller_guard_rejected', RealtimeSubtitleSession.TRANSCRIPT_EVENT, event)
            return
        try:
            if event.is_final:
                self._transcript_event_queue.put(event, timeout=0.2)
            else:
                self._transcript_event_queue.put_nowait(event)
        except queue.Full:
            if event.is_final:
                self.event_bus.spill_final('transcript_controller_backlog', RealtimeSubtitleSession.TRANSCRIPT_EVENT, event)
            else:
                self._stat_add('dropped_transcript_previews', 1)

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
                    logger.error('TranscriptController 处理失败', exc_info=True)
                    self.event_bus.spill_final('transcript_controller_failed', RealtimeSubtitleSession.TRANSCRIPT_EVENT, event)
                finally:
                    self._transcript_event_queue.task_done()
        self._transcript_controller_thread = Thread(target=run_controller, name='TranscriptController', daemon=False)
        self._transcript_controller_thread.start()

    def _stop_transcript_controller(self, *, drain: bool, timeout: float) -> bool:
        if not drain:
            while True:
                try:
                    event = self._transcript_event_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self.event_bus.spill_final('transcript_controller_close_without_drain', RealtimeSubtitleSession.TRANSCRIPT_EVENT, event)
                finally:
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
                logger.critical('[CLEANUP] session_id=%s completed_with_failures=%s', self.session_id, self._cleanup_failures)
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
        self._set_lifecycle_state('stopping_input')
        self._shutdown_event.set()
        logger.info('用户关闭窗口，正在退出...')
        self.update_status('● 正在退出...', '#FFAA00')
        if self.stop_event is not None:
            try:
                self.stop_event.set()
            except Exception:
                logger.debug('窗口关闭时设置录音停止事件失败', exc_info=True)
        Thread(target=self.cleanup, name='SubtitleCleanup', daemon=True).start()
        self._poll_cleanup_close()

    def _stat_add(self, key: str, amount: int=1) -> int:
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

    def on_asr_result(self, text: str, is_final: bool, utterance_id: int=0) -> None:
        """Normalize overlap and publish one ASR event."""
        if not self._accept_pipeline_callback(is_final=bool(is_final)):
            return
        self._last_asr_result_at = time.monotonic()
        utterance_id = int(utterance_id)
        full_text = normalize_subtitle_text(text)
        if utterance_id > 0 and full_text:
            with self._segment_overlap_lock:
                previous_id = self._forced_continuation_from.get(utterance_id, 0)
                previous_text = self._final_source_by_utterance.get(previous_id, '')
            if previous_text:
                full_text, deduplicated_chars = remove_repeated_segment_prefix(previous_text, full_text)
                if deduplicated_chars:
                    with self._segment_overlap_lock:
                        prior_max = self._forced_overlap_dedup_max.get(utterance_id, 0)
                        if deduplicated_chars > prior_max:
                            self._stat_add('forced_overlap_deduplicated_chars', deduplicated_chars - prior_max)
                            self._forced_overlap_dedup_max[utterance_id] = deduplicated_chars
        with self._audio_cursor_lock:
            processed_cursor = self.audio_cursor.processed_sample
            self.audio_cursor.update(decoded=processed_cursor, confirmed=processed_cursor if is_final else -1, retention_samples=int(self.args.sample_rate * 0.8))
        try:
            self.subtitle_session.ingest_asr(full_text, is_final=bool(is_final), utterance_id=utterance_id)
        except Exception:
            logger.error('[SESSION_ASR] session_id=%s utterance=%d publish failed', self.session_id, utterance_id, exc_info=True)

    def _handle_recovered_transcript_event(self, source_event: TranscriptEvent) -> None:
        record_id = str(source_event.recovery_record_id or '')
        text = normalize_subtitle_text(source_event.text)
        if not record_id:
            return
        logger.info('[RECOVERY_SOURCE] record=%s utterance=%d', record_id, source_event.utterance_id)
        ok = self.subtitle_history_store.persist(record_id=record_id, session_id=self.session_id, utterance_id=source_event.utterance_id, source_text=text, source_revision=source_event.revision, is_final=source_event.is_final, recovered=True)
        if ok:
            self._ack_recovery_records([record_id])
        else:
            self._mark_recovery_retryable(record_id)

    def _handle_transcript_event(self, source_event: TranscriptEvent) -> None:
        """Consume an immutable ASR event, update the UI and persist terminal subtitles."""
        if not self._event_session_guard(source_event):
            return
        if source_event.recovered:
            self._handle_recovered_transcript_event(source_event)
            return
        full_text = source_event.text
        is_final = source_event.is_final
        utterance_id = source_event.utterance_id
        now = source_event.emitted_at
        display_text = full_text[-320:]
        self._update_endpoint_hints(source_event, now, is_final)
        if full_text:
            with self.ui_buffer_lock:
                current_id = int(self.ui_update_buffer['SRC_UTTERANCE_ID'] or 0)
                same_utterance = utterance_id <= 0 or current_id in (0, utterance_id)
                source_age = now - float(self.ui_update_buffer['SRC_DISPLAY_AT'] or 0.0)
                should_update = should_accept_source_update(self.ui_update_buffer['SRC_FULL_TEXT'], self.ui_update_buffer['SRC_DISPLAY_IS_FINAL'], full_text, is_final, source_age, same_utterance=same_utterance)
                if should_update:
                    self.ui_update_buffer['SRC'] = display_text
                    self.ui_update_buffer['IS_FINAL'] = is_final
                    self.ui_update_buffer['SRC_DISPLAY_TEXT'] = display_text
                    self.ui_update_buffer['SRC_FULL_TEXT'] = full_text
                    self.ui_update_buffer['SRC_DISPLAY_IS_FINAL'] = is_final
                    self.ui_update_buffer['SRC_DISPLAY_AT'] = now
                    self.ui_update_buffer['SRC_UTTERANCE_ID'] = utterance_id
                self.ui_update_buffer['SOURCE_REVISION'] = source_event.revision
                self.ui_update_buffer['SOURCE_STATE'] = source_event.state
                self.ui_update_buffer['STABLE_SOURCE_TEXT'] = source_event.committed_text
                self.ui_update_buffer['UNSTABLE_SOURCE_TEXT'] = source_event.revisable_text
        elif is_final:
            with self.ui_buffer_lock:
                self.ui_update_buffer['IS_FINAL'] = True
                self.ui_update_buffer['SRC_DISPLAY_IS_FINAL'] = True
                self.ui_update_buffer['SOURCE_REVISION'] = source_event.revision
                self.ui_update_buffer['SOURCE_STATE'] = 'final'
                self.ui_update_buffer['STABLE_SOURCE_TEXT'] = ''
                self.ui_update_buffer['UNSTABLE_SOURCE_TEXT'] = ''
        self._stat_add(f'subtitle_{source_event.state}_count', 1)
        logger.info('[SUBTITLE_EVENT] session_id=%s utterance=%d revision=%d state=%s committed_chars=%d revisable_chars=%d final=%s full_chars=%d display_chars=%d', self.session_id, utterance_id, source_event.revision, source_event.state, len(source_event.committed_text), len(source_event.revisable_text), is_final, len(full_text), len(display_text))
        if is_final:
            self._reset_endpoint_hints()
            recognize_count = self._stat_add('recognize_count', 1)
            if utterance_id > 0 and full_text:
                with self._segment_overlap_lock:
                    self._final_source_by_utterance[utterance_id] = full_text
                    while len(self._final_source_by_utterance) > 64:
                        old_id, _ = self._final_source_by_utterance.popitem(last=False)
                        self._forced_continuation_from.pop(old_id, None)
                        self._forced_overlap_dedup_max.pop(old_id, None)
            record_id = source_event.recovery_record_id or f'{self.session_id}:{utterance_id}:{source_event.revision}'
            self.subtitle_history_writer.submit({'record_id': record_id, 'session_id': self.session_id, 'utterance_id': utterance_id, 'source_text': full_text, 'source_revision': source_event.revision, 'is_final': True, 'recovered': False}, fallback_reason='subtitle_history_write_failed', fallback_event_name=RealtimeSubtitleSession.TRANSCRIPT_EVENT, fallback_event=source_event)
            self.update_status(f'● 识别完成 #{recognize_count}', '#00FF00')

    def _mark_recovery_retryable(self, record_id: str) -> None:
        record_id = str(record_id or '')
        if not record_id:
            return
        with self._recovery_replay_lock:
            self._recovery_inflight_record_ids.discard(record_id)
            self._recovery_replay_next_at = min(self._recovery_replay_next_at or time.monotonic(), time.monotonic() + 0.25)

    def _ack_recovery_records(self, record_ids: List[str]) -> bool:
        """Durably and idempotently acknowledge recovered records."""
        ids = [str(record_id) for record_id in record_ids if record_id]
        if not ids:
            return False
        acknowledge = getattr(self.event_bus, 'acknowledge_recovery', None)
        if callable(acknowledge):
            ok = bool(acknowledge(ids))
        else:
            journal = getattr(self.event_bus, 'recovery_journal', None)
            if journal is None:
                return False
            try:
                _count, ok = journal.acknowledge_with_status(ids)
            except Exception:
                logger.error('[RECOVERY_ACK] fallback acknowledgement failed', exc_info=True)
                return False
        if ok:
            with self._recovery_replay_lock:
                for record_id in ids:
                    self._recovery_inflight_record_ids.discard(record_id)
        return ok

    def _replay_recovery_journal(self) -> None:
        now = time.monotonic()
        with self._recovery_replay_lock:
            if self._recovery_replay_running or now < self._recovery_replay_next_at:
                return
            self._recovery_replay_running = True
        records: List[Dict[str, Any]] = []
        replayed_now = 0
        failures = 0
        obsolete_ids: List[str] = []
        try:
            try:
                loader = getattr(self.event_bus, 'pending_recovery_records', None)
                if callable(loader):
                    records = list(loader(limit=10000))
                else:
                    journal = self.event_bus.recovery_journal
                    records = [] if journal is None else list(journal.load_pending(limit=10000))
            except Exception:
                failures += 1
                logger.error('[RECOVERY_REPLAY] journal load failed', exc_info=True)
                return
            pending_ids = {str(record.get('record_id', '')) for record in records if record.get('record_id')}
            with self._recovery_replay_lock:
                self._recovery_inflight_record_ids.intersection_update(pending_ids)
            for index, record in enumerate(records, 1):
                record_id = str(record.get('record_id', ''))
                if not record_id:
                    failures += 1
                    continue
                payload = record.get('event', {})
                event_name = str(record.get('event_name', ''))
                event_type = str(record.get('event_type', ''))
                if event_name != RealtimeSubtitleSession.TRANSCRIPT_EVENT or event_type != 'TranscriptEvent':
                    obsolete_ids.append(record_id)
                    continue
                with self._recovery_replay_lock:
                    if record_id in self._recovery_inflight_record_ids:
                        continue
                    self._recovery_inflight_record_ids.add(record_id)
                try:
                    original_utterance = int(payload.get('utterance_id', index) or index)
                    digest = hashlib.sha256(record_id.encode('utf-8')).digest()
                    # SQLite INTEGER is signed 64-bit.  Mask the digest to 63 bits
                    # so every deterministic negative recovery ID is persistable.
                    stable_value = int.from_bytes(digest[:8], 'big') & ((1 << 63) - 1)
                    fallback_value = min((1 << 63) - 1, abs(original_utterance) or index or 1)
                    recovery_utterance = -(stable_value or fallback_value)
                    allowed = set(TranscriptEvent.__dataclass_fields__)
                    data = {k: v for k, v in payload.items() if k in allowed}
                    data.update({'session_id': self.session_id, 'utterance_id': recovery_utterance, 'emitted_at': time.monotonic(), 'recovery_record_id': record_id, 'recovered': True})
                    if self.event_bus.publish(event_name, TranscriptEvent(**data)):
                        replayed_now += 1
                    else:
                        failures += 1
                        self._mark_recovery_retryable(record_id)
                except Exception:
                    failures += 1
                    self._mark_recovery_retryable(record_id)
                    logger.error('[RECOVERY_REPLAY] record=%s failed', record_id, exc_info=True)
            if obsolete_ids:
                self._ack_recovery_records(obsolete_ids)
                logger.info('[RECOVERY_REPLAY] discarded_obsolete_records=%d', len(obsolete_ids))
            journal = self.event_bus.recovery_journal
            if journal is not None:
                try:
                    journal.compact(retain_acknowledged_seconds=7 * 86400)
                except Exception:
                    logger.error('[RECOVERY_REPLAY] journal compact failed', exc_info=True)
            self._recovery_replayed += replayed_now
            if records:
                logger.info('[RECOVERY_REPLAY] pending=%d replayed_now=%d inflight=%d failures=%d total=%d', len(records), replayed_now, len(self._recovery_inflight_record_ids), failures, self._recovery_replayed)
        finally:
            with self._recovery_replay_lock:
                self._recovery_replay_running = False
                self._recovery_replay_backoff = min(30.0, max(1.0, self._recovery_replay_backoff * 2.0)) if failures else 1.0
                self._recovery_replay_next_at = time.monotonic() + self._recovery_replay_backoff

    def _component_health_snapshots(self) -> List[Dict[str, Any]]:
        snapshots: List[Dict[str, Any]] = []
        if self.async_asr is not None:
            alive_check = getattr(self.async_asr, 'is_alive', None)
            alive = True if not callable(alive_check) else bool(alive_check())
            snapshots.append({'component': 'asr', 'state': 'running' if alive else 'failed', 'last_success_at': self._last_asr_result_at})
        if self.recording_process is not None:
            try:
                alive = bool(self.recording_process.is_alive())
            except Exception:
                alive = False
            snapshots.append({'component': 'audio_capture', 'state': 'running' if alive else 'failed', 'last_success_at': self._last_audio_packet_at})
        journal = self.event_bus.recovery_journal
        if journal is not None:
            snapshots.append(journal.health_snapshot())
        spooler = getattr(self.event_bus, '_recovery_spooler', None)
        if spooler is not None:
            snapshots.append(spooler.metrics())
        snapshots.append(self.subtitle_history_writer.metrics())
        snapshots.append(self.subtitle_history_store.health_snapshot())
        return snapshots

    def _start_health_supervisor(self) -> None:
        if self._health_thread is not None:
            return

        def supervise() -> None:
            previous = ''
            while not self._health_stop_event.wait(1.0):
                try:
                    self._replay_recovery_journal()
                    metrics = self.event_bus.metrics()
                    state = self.pipeline_health.evaluate(int(metrics.get('pending_finals', 0)), int(metrics.get('oldest_final_age_ms', 0)), int(metrics.get('recovery_pending', 0)), component_snapshots=self._component_health_snapshots())
                    if state != previous:
                        logger.warning('[PIPELINE_HEALTH] state=%s event_metrics=%s', state, metrics)
                        previous = state
                except Exception:
                    logger.error('健康监督线程异常', exc_info=True)
        self._health_thread = Thread(target=supervise, name='PipelineHealthSupervisor', daemon=True)
        self._health_thread.start()

    def cleanup(self) -> None:
        with self._cleanup_lock:
            if self._cleanup_started:
                already_running = True
            else:
                self._cleanup_started = True
                already_running = False
        if already_running:
            return
        failures: List[str] = []
        self._set_lifecycle_state('stopping_input')
        self._shutdown_event.set()
        self._health_stop_event.set()

        def signal_capture_stop() -> None:
            if self.stop_event is not None:
                try:
                    self.stop_event.set()
                except Exception:
                    logger.debug('设置录音停止事件失败', exc_info=True)

        def stop_recording_process() -> bool:
            process = self.recording_process
            if process is None:
                return True
            try:
                process.join(timeout=2.0)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=1.0)
                if process.is_alive() and hasattr(process, 'kill'):
                    process.kill()
                    process.join(timeout=1.0)
                alive = bool(process.is_alive())
            except Exception:
                alive = True
                logger.error('录音进程停止失败', exc_info=True)
            if not alive:
                try:
                    process.close()
                except Exception:
                    pass
                self.recording_process = None
            return not alive

        def shutdown_component(name: str, component: Any) -> bool:
            if component is None:
                return True
            try:
                shutdown = getattr(component, 'shutdown', None)
                if callable(shutdown):
                    result = shutdown()
                    if isinstance(result, bool) and (not result):
                        return False
                alive_check = getattr(component, 'is_alive', None)
                if callable(alive_check):
                    return not bool(alive_check())
                return True
            except Exception:
                logger.error('%s 清理失败', name, exc_info=True)
                return False
        try:
            signal_capture_stop()
            if not stop_recording_process():
                failures.append('recording_process')
            if not shutdown_component('ASR 服务', self.async_asr):
                failures.append('asr_service')
            worker = self.worker_thread
            if worker is not None and worker.is_alive() and (current_thread() is not worker):
                if not self._worker_stopped_event.wait(timeout=12.0):
                    failures.append('worker_thread')
                    logger.error('[CLEANUP] session_id=%s worker_stop_timeout', self.session_id)
                if worker.is_alive():
                    worker.join(timeout=1.0)
            elif current_thread() is worker:
                self._worker_stopped_event.set()
            else:
                self._worker_stopped_event.set()
            health_thread = self._health_thread
            if health_thread is not None and current_thread() is not health_thread:
                health_thread.join(timeout=1.5)
                if health_thread.is_alive():
                    failures.append('health_supervisor')
            signal_capture_stop()
            if not stop_recording_process() and 'recording_process' not in failures:
                failures.append('recording_process')
            if not shutdown_component('ASR 服务', self.async_asr) and 'asr_service' not in failures:
                failures.append('asr_service')
            self._set_lifecycle_state('draining_finals')
            try:
                if not self.event_bus.close(drain=True, timeout=5.0):
                    failures.append('session_event_bus')
            except Exception:
                failures.append('session_event_bus')
                logger.error('SessionEventBus 清理失败', exc_info=True)
            if not self._stop_transcript_controller(drain=True, timeout=5.0):
                failures.append('transcript_controller')
            self._set_lifecycle_state('stopping_delivery')
            try:
                if not self.subtitle_session.close(timeout=5.0):
                    failures.append('subtitle_session')
            except Exception:
                failures.append('subtitle_session')
                logger.error('字幕 Session 清理失败', exc_info=True)
            try:
                if not self.subtitle_history_writer.close(drain=True, timeout=5.0):
                    failures.append('subtitle_history_writer')
            except Exception:
                failures.append('subtitle_history_writer')
                logger.error('字幕历史写入线程清理失败', exc_info=True)
            try:
                self.subtitle_history_store.close()
            except Exception:
                failures.append('subtitle_history_store')
                logger.error('字幕历史数据库清理失败', exc_info=True)
            stats = self._stats_snapshot()
            logger.info('[SESSION_SUMMARY] session_id=%s finals=%d asr_tasks=%d max_asr_queue_ms=%d max_asr_inference_ms=%d hybrid_endpoints=%d forced_endpoints=%d forced_dedup_chars=%d audio_drops=%d discontinuities=%d max_audio_age_ms=%d asr_restarts=%d', self.session_id, stats['subtitle_final_count'], stats['asr_tasks'], stats['max_asr_queue_latency_ms'], stats['max_asr_inference_latency_ms'], stats['hybrid_endpoint_count'], stats['forced_endpoint_count'], stats['forced_overlap_deduplicated_chars'], stats['dropped_audio_chunks'], stats['audio_discontinuity_count'], stats['max_audio_age_ms'], stats['asr_restart_count'])
            self._cleanup_failures = failures
            self._cleanup_succeeded = not failures
            logger.info('[SESSION] session_id=%s cleanup_complete success=%s failures=%s', self.session_id, self._cleanup_succeeded, failures)
        finally:
            self.killed = True
            self._set_lifecycle_state('closed')
            self._cleanup_failures = failures
            self._cleanup_succeeded = not failures
            self._cleanup_complete.set()

    def _preload_runtime_dependencies(self) -> None:
        started_at = time.monotonic()
        stage_at = started_at
        runtime_ready = ensure_runtime_dependencies()
        logger.info('[RUNTIME_PRELOAD_STAGE] stage=runtime_core ms=%.0f success=%s', (time.monotonic() - stage_at) * 1000, runtime_ready)
        if runtime_ready:
            stage_at = time.monotonic()
            configure_torch_runtime(intra_op_threads=self.args.torch_intra_op_threads, inter_op_threads=self.args.torch_inter_op_threads, allow_tf32=self.args.allow_tf32, cudnn_benchmark=not self.args.disable_cudnn_benchmark)
            logger.info('[RUNTIME_PRELOAD_STAGE] stage=torch_config ms=%.0f success=True', (time.monotonic() - stage_at) * 1000)
        stage_at = time.monotonic()
        try:
            from transformers import AutoModelForRNNT, AutoProcessor
            transformers_ready = True
        except Exception as exc:
            global _runtime_import_error
            _runtime_import_error = exc
            transformers_ready = False
            logger.error('Transformers Nemotron ASR 加载失败：%s', exc, exc_info=True)
        logger.info('[RUNTIME_PRELOAD_STAGE] stage=transformers_import ms=%.0f success=%s', (time.monotonic() - stage_at) * 1000, transformers_ready)
        logger.info('运行时依赖后台预加载结束：%.0fms success=%s', (time.monotonic() - started_at) * 1000, _runtime_import_error is None)

    def _worker_thread(self) -> None:
        """Load models, run audio/VAD, and feed every speech frame into stateful streaming ASR."""
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
                self.update_status('● 错误：缺少 torch/numpy，见 subtitle.log', '#FF0000')
                return
            configure_torch_runtime(intra_op_threads=self.args.torch_intra_op_threads, inter_op_threads=self.args.torch_inter_op_threads, allow_tf32=self.args.allow_tf32, cudnn_benchmark=not self.args.disable_cudnn_benchmark)
            if not ensure_audio_dependencies():
                logger.error('音频依赖不可用：%s', _audio_import_error)
                self.update_status('● 错误：缺少音频依赖，见 subtitle.log', '#FF0000')
                return
            self.sample_rate = int(self.args.sample_rate)
            selected_dev = self.selected_devices
            if not selected_dev:
                self.update_status('● 错误：未选择任何设备', '#FF0000')
                return
            if torch.cuda.is_available():
                device_str = f'cuda:{self.args.cuda_device}'
            elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                device_str = 'mps'
            else:
                device_str = 'cpu'
                logger.warning('未检测到 GPU，使用 CPU 流式推理，速度可能较慢')
            model_label = 'Nemotron 3.5'
            asr_generation = 0
            self.update_status(f'● 加载 {model_label} 流式模型 ({device_str})...', '#FFAA00')
            logger.info('加载 ASR 模型: %s (设备: %s)', self.args.asr_model, device_str)
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
            self._replay_recovery_journal()

            def create_asr_proxy() -> IsolatedStreamingNemotronRecognizer:
                return IsolatedStreamingNemotronRecognizer(getattr(self.args, 'asr_model_load_path', self.args.asr_model), on_asr_with_print, model_revision=self.args.asr_model_revision, language=self.args.asr_language, lookahead_tokens=self.args.nemotron_lookahead_tokens, sample_rate=self.sample_rate, normalize_audio=not self.args.disable_audio_normalize, final_padding_seconds=self.args.stream_final_padding_ms / 1000.0, min_audio_rms=self.args.min_audio_rms, on_error=on_asr_fatal_error, on_metrics=on_asr_metrics, on_discontinuity=on_asr_discontinuity, process_interval_seconds=self.args.stream_process_interval_ms / 1000.0, intra_op_threads=self.args.torch_intra_op_threads, inter_op_threads=self.args.torch_inter_op_threads, allow_tf32=self.args.allow_tf32, cuda_device=self.args.cuda_device, cudnn_benchmark=not self.args.disable_cudnn_benchmark, pin_memory=not self.args.disable_pinned_memory, preserve_audio=self.args.audio_backpressure_mode == 'buffered', utterance_id_offset=asr_generation * 1000000)
            try:
                if self._shutdown_requested():
                    return
                self._asr_failure_event.clear()
                self._asr_failure_message = ''
                self.async_asr = create_asr_proxy()
                self.update_status(f'● 启动独立 {model_label} 流式 ASR 进程...', '#FFAA00')
                warmup_seconds = self.async_asr.warmup()
                if self._shutdown_requested():
                    return
                self._asr_failure_event.clear()
                logger.info('%s 独立流式 ASR 进程预热完成：%.0fms', model_label, warmup_seconds * 1000)
            except Exception as e:
                if self._shutdown_requested():
                    return
                logger.error('流式 ASR 初始化失败：%s', e, exc_info=True)
                self.update_status('● 错误：模型不支持流式接口，见 subtitle.log', '#FF0000')
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
            logger.info('Ready: Nemotron cache-aware streaming lookahead_tokens=%d', self.args.nemotron_lookahead_tokens)
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
                    failure = self._asr_failure_message or 'ASR 独立进程意外退出'
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
                    self.update_status('● 正在重启 ASR 模型...', '#FFAA00')
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
                if not self.args.disable_audio_normalize:
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
            raise RuntimeError('Tk 主窗口未初始化')
        selected = list(self.selected_devices)
        result_queue: queue.Queue = queue.Queue(maxsize=1)
        dialog = tk.Toplevel(root)
        dialog.title('正在检查音频设备')
        dialog.geometry('460x140')
        dialog.resizable(False, False)
        dialog.transient(root)
        dialog.grab_set()
        tk.Label(dialog, text='正在隔离进程中打开所选设备，请稍候…', anchor='w').pack(fill='x', padx=20, pady=(24, 12))
        progress = ttk.Progressbar(dialog, mode='indeterminate')
        progress.pack(fill='x', padx=20, pady=8)
        progress.start(12)

        def probe() -> None:
            try:
                result_queue.put((True, probe_audio_devices_isolated(selected)), timeout=0.5)
            except BaseException as exc:
                result_queue.put((False, exc), timeout=0.5)
        Thread(target=probe, name='AudioDevicePreflight', daemon=True).start()
        outcome: Dict[str, Any] = {'done': False, 'value': ([], {})}

        def poll() -> None:
            try:
                ok, value = result_queue.get_nowait()
            except queue.Empty:
                try:
                    dialog.after(50, poll)
                except tk.TclError:
                    pass
                return
            outcome['done'] = True
            if ok:
                outcome['value'] = value
            else:
                outcome['value'] = value
            try:
                progress.stop()
                dialog.destroy()
            except tk.TclError:
                pass
        dialog.after(50, poll)
        root.wait_window(dialog)
        if not outcome['done']:
            raise RuntimeError('音频设备预检窗口被意外关闭')
        value = outcome['value']
        if isinstance(value, BaseException):
            raise value
        valid, failures = value
        return (list(valid), dict(failures))

    def run(self) -> None:
        """Build the UI, validate resources/devices, run workers, and always release runtime resources."""
        self.build_ui()
        root = self.root
        if root is None:
            raise RuntimeError('Tk 主窗口初始化失败')
        try:
            if not ensure_first_run_assets(root, self.args):
                return
            self.dependency_loader_thread = Thread(target=self._preload_runtime_dependencies, name='RuntimeDependencyPreload', daemon=True)
            self.dependency_loader_thread.start()
            if self.args.device >= 0:
                self.selected_devices = [self.args.device]
            else:
                if not ensure_audio_dependencies():
                    logger.error('音频依赖缺失：%s', _audio_import_error)
                    self.show_error_dialog('音频依赖缺失', '缺少 PyAudioWPatch、sherpa-onnx、SciPy 或 NumPy。\n\n请先按照 requirements.txt 安装依赖。')
                    return
                if self.args.capture_source in ('system', 'both') and (not hasattr(pyaudio_backend.PyAudio, 'get_loopback_device_info_generator')) and (not hasattr(pyaudio_backend, 'paWASAPI')):
                    self.show_error_dialog('系统声音监听不可用', '监听 Windows 系统声音需要 PyAudioWPatch。')
                    return
                try:
                    p_temp = pyaudio_backend.PyAudio()
                    try:
                        self.selected_devices = self.select_devices_dialog(p_temp)
                    finally:
                        p_temp.terminate()
                except Exception as exc:
                    logger.error('设备初始化失败：%s', exc, exc_info=True)
                    self.show_error_dialog('设备初始化失败', f'无法初始化或枚举音频设备：\n{exc}')
                    return
            if not self.selected_devices:
                logger.info('未选择任何设备，退出')
                return
            try:
                valid_devices, failures = self._probe_selected_devices_with_dialog()
                for device_idx, reason in failures.items():
                    logger.warning('[Audio] session_id=%s device=%d preflight_failed reason=%s', self.session_id, device_idx, reason)
                self.selected_devices = valid_devices
            except Exception as exc:
                logger.error('音频设备预检失败：%s', exc, exc_info=True)
                self.show_error_dialog('音频设备预检失败', f'无法验证所选音频设备：\n{exc}')
                return
            if not self.selected_devices:
                self.show_error_dialog('音频设备不可用', '所选音频设备无法打开，请重新连接设备或选择其他输入。')
                return
            self.worker_thread = Thread(target=self._worker_thread, name='SubtitleWorker', daemon=False)
            self.worker_thread.start()
            root.after(16, self.sync_ui)
            root.mainloop()
        finally:
            self.cleanup()
            try:
                if root.winfo_exists():
                    root.destroy()
            except (tk.TclError, RuntimeError):
                pass

def get_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='日语实时语音识别字幕（Nemotron 3.5 原生缓存式流式推理）')
    parser.add_argument('--device', type=int, default=-1, help='音频设备 ID，-1 表示自动选择')
    parser.add_argument('--capture-source', choices=['system', 'mic', 'both'], default='system', help='默认音频来源')
    parser.add_argument('--mix-mode', choices=['average', 'add'], default='average', help='音频混合模式')
    parser.add_argument('--sample-rate', type=int, default=16000, help='采样率；模型要求 16000 Hz')
    parser.add_argument('--asr-model', type=str, default=NEMOTRON_MODEL_ID, help='Hugging Face ASR 模型名')
    parser.add_argument('--asr-language', type=str, default='ja-JP', help='Nemotron 目标语言提示')
    parser.add_argument('--asr-model-revision', type=str, default='', help='可选 Nemotron Hugging Face commit/revision')
    parser.add_argument('--nemotron-lookahead-tokens', type=int, choices=[0, 3, 6, 13], default=13, help='Nemotron 右侧前瞻')
    parser.add_argument('--stream-process-interval-ms', type=int, default=50, help='音频前端检查间隔毫秒')
    parser.add_argument('--stream-final-padding-ms', type=int, default=400, help='句末追加静音毫秒数')
    parser.add_argument('--vad-model-path', type=str, default=os.path.join(_script_dir, 'ten-vad.onnx'), help='TEN-VAD ONNX 模型路径')
    parser.add_argument('--min-silence-duration', type=float, default=0.9, help='TEN-VAD 最小静默时长')
    parser.add_argument('--min-speech-duration', type=float, default=0.12, help='TEN-VAD 最短有效语音时长')
    parser.add_argument('--vad-threshold', type=float, default=0.4, help='TEN-VAD 语音阈值')
    parser.add_argument('--vad-buffer-size', type=int, default=30, help='VAD 缓冲秒数')
    parser.add_argument('--stable-partial-threshold', type=int, default=3, help='ASR 文本连续确认次数')
    parser.add_argument('--max-utterance-seconds', type=float, default=18.0, help='连续语音强制分段上限')
    parser.add_argument('--forced-segment-overlap-ms', type=int, default=560, help='强制分段边界重叠音频')
    parser.add_argument('--disable-hybrid-endpoint', dest='disable_hybrid_endpoint', action='store_true', help='关闭稳定句末标点混合 endpoint')
    parser.add_argument('--enable-hybrid-endpoint', dest='disable_hybrid_endpoint', action='store_false', help='启用稳定句末标点混合 endpoint')
    parser.set_defaults(disable_hybrid_endpoint=False)
    parser.add_argument('--endpoint-punctuation-hold-ms', type=int, default=320, help='稳定句末标点保持时长')
    parser.add_argument('--endpoint-min-utterance-ms', type=int, default=1200, help='混合 endpoint 最短 utterance 时长')
    parser.add_argument('--speech-preroll-ms', type=int, default=1000, help='语音起点预录毫秒数')
    parser.add_argument('--disable-audio-normalize', action='store_true', help='关闭输入音频轻量处理')
    parser.add_argument('--cuda-device', type=int, default=0, help='ASR 使用的 CUDA GPU 编号')
    parser.add_argument('--allow-tf32', dest='allow_tf32', action='store_true', help='允许 TF32')
    parser.add_argument('--disable-tf32', dest='allow_tf32', action='store_false', help='关闭 TF32')
    parser.set_defaults(allow_tf32=True)
    parser.add_argument('--disable-cudnn-benchmark', action='store_true', help='关闭 cuDNN benchmark')
    parser.add_argument('--disable-pinned-memory', action='store_true', help='关闭页锁定内存')
    parser.add_argument('--torch-intra-op-threads', type=int, default=4, help='PyTorch 单算子 CPU 线程数')
    parser.add_argument('--torch-inter-op-threads', type=int, default=1, help='PyTorch 算子间 CPU 线程数')
    parser.add_argument('--min-audio-rms', type=float, default=1e-05, help='极低能量门限')
    parser.add_argument('--audio-queue-size', type=int, default=120, help='音频队列上限')
    parser.add_argument('--audio-backpressure-mode', choices=['buffered', 'live'], default='buffered', help='音频背压模式')
    parser.add_argument('--max-drain-chunks', type=int, default=20, help='主循环每轮最多读取块数')
    parser.add_argument('--audio-latency-budget-ms', type=int, default=900, help='音频积压告警阈值')
    parser.add_argument('--audio-recovery-preroll-ms', type=int, default=900, help='跳到实时位置时保留音频')
    parser.add_argument('--debug-save-audio', type=str, default='', help='可选：保存送入 VAD/ASR 的 WAV')
    parsed = parser.parse_args()
    argv = list(sys.argv[1:])
    explicit_dests = set()
    for action in parser._actions:
        if not action.option_strings:
            continue
        if any((argument == option or argument.startswith(option + '=') for argument in argv for option in action.option_strings)):
            explicit_dests.add(action.dest)
    parsed._explicit_dests = explicit_dests
    return parsed
if __name__ == '__main__':
    if sys.platform.startswith('win'):
        multiprocessing.freeze_support()
    try:
        args = validate_args(get_args())
    except ValueError as exc:
        logger.error('启动参数错误：%s', exc)
        try:
            messagebox.showerror('启动参数错误', str(exc))
        except Exception:
            pass
        sys.exit(2)
    logger.info('=' * 60)
    logger.info('实时日语字幕 - Nemotron 3.5 ASR Only (.pyw)')
    logger.info('=' * 60)
    logger.info('VAD 模型：TEN-VAD (%s)', args.vad_model_path)
    logger.info('VAD 静默时长：%ss', args.min_silence_duration)
    logger.info('VAD 最短语音：%ss', args.min_speech_duration)
    logger.info('VAD 阈值：%s', args.vad_threshold)
    logger.info('ASR 模型：%s', args.asr_model)
    logger.info('Nemotron lookahead tokens：%s', args.nemotron_lookahead_tokens)
    logger.info('流式调度轮询：%sms', args.stream_process_interval_ms)
    logger.info('流式句末 padding：%sms', args.stream_final_padding_ms)
    logger.info('默认音频来源：%s', args.capture_source)
    logger.info('稳定 partial 确认阈值：%s', args.stable_partial_threshold)
    logger.info('连续语音最大段长：%ss', args.max_utterance_seconds)
    logger.info('强制分段重叠：%sms', args.forced_segment_overlap_ms)
    logger.info('混合 endpoint：%s hold=%dms min=%dms', 'off' if args.disable_hybrid_endpoint else 'on', args.endpoint_punctuation_hold_ms, args.endpoint_min_utterance_ms)
    logger.info('语音起点预录：%sms', args.speech_preroll_ms)
    logger.info('音频归一化：%s', not args.disable_audio_normalize)
    logger.info('最低音频 RMS：%s', args.min_audio_rms)
    logger.info('CUDA：device=%d TF32=%s cuDNN benchmark=%s pinned_memory=%s', args.cuda_device, args.allow_tf32, not args.disable_cudnn_benchmark, not args.disable_pinned_memory)
    logger.info('PyTorch intra-op 线程：%s', args.torch_intra_op_threads)
    logger.info('PyTorch inter-op 线程：%s', args.torch_inter_op_threads)
    logger.info('音频队列上限：%s', args.audio_queue_size)
    logger.info('音频背压模式：%s', args.audio_backpressure_mode)
    logger.info('每轮最大读取块数：%s', args.max_drain_chunks)
    logger.info('音频实时预算：%dms，恢复预录：%dms', args.audio_latency_budget_ms, args.audio_recovery_preroll_ms)
    logger.info('ASR 独立进程：on')
    logger.info('调试音频保存：%s', args.debug_save_audio or 'off')
    logger.info('=' * 60)
    app = SubtitleApp(args)
    app.run()
