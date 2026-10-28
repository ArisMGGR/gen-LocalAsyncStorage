"""Background operation lanes — the desktop side of the phone's task queues.

The Android app runs long work in two places: transfers in a foreground
service, and in-app operations (delete / move / restore) on their own
single-thread executor. Waiting work shows in a Queue strip; the running job
shows in a Tasks strip with a Stop button. This module is that machinery for the
desktop client.

A :class:`Lane` runs one job at a time and keeps the rest in a visible queue.
Every job is handed a :class:`threading.Event` it polls between items, so:

* the running job can be **stopped part-way** — it finishes the item in hand and
  skips the rest (a delete then puts back what it already removed), and
* a job that hasn't started yet can be **dropped from the queue** outright.

There is no Tk in here: the GUI passes an ``on_change`` callback and marshals it
onto the UI thread itself, which keeps this module importable (and testable)
without a display.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from typing import Callable

# Job bodies take the cancel flag to poll between items.
OpBody = Callable[[threading.Event], None]

# Ids are unique across every lane, so the Queue strip can address a waiting job
# by id alone without caring which lane it came from.
_next_id = itertools.count(1)


@dataclass
class Op:
    """One queued or running job: what to show for it, and how to stop it.

    ``meta`` carries whatever the caller needs to recognise the job later — the
    file paths a download claimed, say, so dropping it from the queue can hand
    them back.
    """

    id: int
    label: str
    body: OpBody
    cancelled: threading.Event = field(default_factory=threading.Event)
    meta: dict = field(default_factory=dict)

    def cancel(self) -> None:
        self.cancelled.set()


class Lane:
    """A single-file queue of jobs, run one at a time on one worker thread.

    ``on_change`` fires (from the worker thread) whenever the queue or the
    running job changes, so the UI can redraw its Tasks/Queue strips.
    """

    def __init__(self, name: str, on_change: Callable[[], None] | None = None):
        self.name = name
        self._on_change = on_change
        self._lock = threading.Condition()
        self._waiting: list[Op] = []
        self._running: Op | None = None
        self._closed = False
        self._thread = threading.Thread(
            target=self._worker, name=f"{name}-lane", daemon=True
        )
        self._thread.start()

    # -- submitting and inspecting --

    def submit(self, label: str, body: OpBody, meta: dict | None = None) -> Op:
        """Queue a job; it starts as soon as the lane is free."""
        op = Op(id=next(_next_id), label=label, body=body, meta=meta or {})
        with self._lock:
            self._waiting.append(op)
            self._lock.notify()
        self._changed()
        return op

    def running(self) -> Op | None:
        with self._lock:
            return self._running

    def waiting(self) -> list[Op]:
        with self._lock:
            return list(self._waiting)

    def busy(self) -> bool:
        with self._lock:
            return self._running is not None or bool(self._waiting)

    # -- stopping --

    def cancel_running(self) -> Op | None:
        """Flag the running job to stop after its current item. Returns it."""
        with self._lock:
            op = self._running
        if op is not None:
            op.cancel()
        return op

    def cancel_queued(self, op_id: int) -> Op | None:
        """Drop a still-waiting job, returning it. None once it's running."""
        with self._lock:
            for i, op in enumerate(self._waiting):
                if op.id == op_id:
                    del self._waiting[i]
                    break
            else:
                return None
        self._changed()
        return op

    def close(self) -> None:
        """Stop the worker once the current job returns (used at shutdown)."""
        with self._lock:
            self._closed = True
            self._waiting.clear()
            op = self._running
            self._lock.notify_all()
        if op is not None:
            op.cancel()

    # -- the worker --

    def _worker(self) -> None:
        while True:
            with self._lock:
                while not self._waiting and not self._closed:
                    self._lock.wait()
                if self._closed:
                    self._running = None
                    return
                self._running = self._waiting.pop(0)
                op = self._running
            self._changed()
            try:
                op.body(op.cancelled)
            except Exception:
                # A job reports its own failures to the user; the lane only has
                # to survive them so the queue keeps moving.
                pass
            finally:
                with self._lock:
                    self._running = None
                self._changed()

    def _changed(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                pass  # a UI hiccup must never wedge the lane
