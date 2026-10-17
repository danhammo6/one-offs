"""Background trigger loop for the gallery: watch for changes, then reconcile.

Filesystem events (watchdog: FSEvents, inotify, ReadDirectoryChangesW) are only
hints. Each wake runs the caller's `reconcile`, which diffs the real state, so
a dropped or coalesced event costs at most one slow poll. Without watchdog, or
if it fails to start, the loop polls on a short interval instead.
"""
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# Event types that mean the tree changed; "opened"/"closed" fire on mere reads
# on Linux and would feed back from our own scans.
CHANGE_EVENTS = {"created", "deleted", "moved", "modified"}


class _Hint:
    def __init__(self, wake, ignored_roots):
        self.wake = wake
        self.ignored_roots = tuple(str(Path(p)) for p in ignored_roots)

    def dispatch(self, event):
        if event.event_type not in CHANGE_EVENTS:
            return
        if event.is_directory and event.event_type == "modified":
            return  # the entry's own created/deleted event names the file
        paths = [getattr(event, "src_path", ""), getattr(event, "dest_path", "")]
        for path in map(os.fsdecode, filter(None, paths)):
            if (os.path.basename(path).startswith(".")  # atomic-write temps
                    or path.startswith(self.ignored_roots)):
                continue
            self.wake.set()
            return


class ChangeWatcher:
    def __init__(self, reconcile, watch_roots, poll_interval=5.0,
                 safety_interval=60.0, debounce=1.0, min_gap=2.0,
                 ignored_roots=(), use_watchdog=True):
        """`watch_roots()` returns [(path, recursive)] and is re-read after
        every reconcile so new sources are picked up."""
        self.reconcile = reconcile
        self.watch_roots = watch_roots
        self.poll_interval = poll_interval
        self.safety_interval = max(safety_interval, poll_interval)
        self.debounce = debounce
        self.min_gap = min_gap
        self.ignored_roots = ignored_roots
        self.use_watchdog = use_watchdog
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._observer = None
        self._scheduled = frozenset()
        self._thread = threading.Thread(
            target=self._run, name="gallery-watcher", daemon=True)

    @property
    def watching(self):
        return self._observer is not None

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=5)
        self._unschedule()

    def notify(self):
        self._wake.set()

    def _unschedule(self):
        observer, self._observer = self._observer, None
        self._scheduled = frozenset()
        if observer is not None:
            try:
                observer.stop()
                observer.join(timeout=5)
            except Exception:  # shutting down; nothing useful to do
                pass

    def _sync_observer(self):
        if not self.use_watchdog:
            return
        wanted = frozenset(
            (str(Path(path)), bool(recursive))
            for path, recursive in self.watch_roots()
            if Path(path).is_dir())
        if wanted == self._scheduled and (self._observer or not wanted):
            return
        self._unschedule()
        if not wanted:
            return
        try:
            from watchdog.observers import Observer
            observer = Observer()
            hint = _Hint(self._wake, self.ignored_roots)
            for path, recursive in wanted:
                observer.schedule(hint, path, recursive=recursive)
            observer.start()
        except Exception as error:  # missing package, OS watch limits, ...
            logger.warning("filesystem watching unavailable (%s); polling "
                           "every %gs", error, self.poll_interval)
            self.use_watchdog = False
            return
        self._observer, self._scheduled = observer, wanted

    def _run(self):
        while not self._stop.is_set():
            try:
                self._sync_observer()
                self.reconcile()
            except Exception:
                logger.exception("gallery rescan failed")
            if self._stop.wait(self.min_gap):
                break
            interval = (self.safety_interval if self.watching
                        else self.poll_interval)
            if self._wake.wait(interval) and not self._stop.is_set():
                # Let a burst of writes (a render run) settle, then rescan.
                self._stop.wait(self.debounce)
                self._wake.clear()
