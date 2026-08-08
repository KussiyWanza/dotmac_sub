import logging
import time

from celery.beat import Scheduler

from app.models.domain_settings import SettingDomain
from app.services.scheduler_config import build_beat_schedule
from app.services.settings_spec import get_spec

logger = logging.getLogger(__name__)


def _beat_refresh_default() -> int:
    """The declared default for `scheduler.beat_refresh_seconds`.

    `control.settings_spec` owns the value; this reads it rather than restating
    it (docs/SOT_RELATIONSHIP_MAP.md).
    """

    spec = get_spec(SettingDomain.scheduler, "beat_refresh_seconds")
    if spec is None or not isinstance(spec.default, int):
        raise RuntimeError(
            "scheduler.beat_refresh_seconds has no integer spec default; "
            "beat cannot choose a refresh interval"
        )
    return spec.default


class DbScheduler(Scheduler):
    """Celery beat scheduler that rebuilds its schedule from the DB.

    ``tick()`` re-reads the DB-driven schedule every ``beat_refresh_seconds``
    and reconciles it into ``self.schedule`` without disturbing entries whose
    spec is unchanged. That invariant matters because celery's
    ``Scheduler.tick`` fires from a heap of per-entry events and only rebuilds
    that heap when it can *see* a change (``schedules_equal``):

    - Re-creating an entry resets its ``last_run_at`` to "now"
      (``ScheduleEntry.__init__``), silently restarting its countdown. When a
      transient hiccup makes a key vanish from one build and reappear in the
      next (``build_beat_schedule`` swallows mid-build DB errors and returns a
      partial dict), every re-created interval entry loses its elapsed time.
      Repeated often enough, sub-hourly timedelta tasks are starved
      indefinitely while crontab entries (wall-clock anchored, immune to
      ``last_run_at`` resets) and very short intervals still look healthy.
      ``_removed_entry_state`` preserves ``last_run_at``/``total_run_count``
      across such remove/re-add cycles so a returning key resumes its cadence
      instead of restarting it.
    - Conversely, updating entries in place (``ScheduleEntry.update``) is
      invisible to ``schedules_equal`` whenever the heap snapshot
      (``old_schedulers``) still aliases the same entry object, so a changed
      interval would not rebuild the heap: the entry kept firing on its stale
      pre-change event time (up to a full old interval late). Any actual
      change now sets ``self._heap = None``, which forces ``Scheduler.tick``
      to re-populate the heap from current entry state.
    """

    def __init__(self, *args, **kwargs):
        self._last_refresh_at = 0.0
        # last_run_at/total_run_count of entries whose key disappeared from a
        # build, keyed by schedule key, so a re-added key resumes its cadence.
        # Bounded by the distinct keys ever removed while this beat is up.
        self._removed_entry_state = {}
        super().__init__(*args, **kwargs)

    def setup_schedule(self):
        self._refresh_schedule()

    def tick(self, *args, **kwargs):
        self._refresh_schedule()
        return super().tick(*args, **kwargs)

    def _refresh_schedule(self):
        # The fallback comes from the spec, not a literal. It was 30 while the
        # spec declares 300 — a disagreement that only shows when the key is
        # absent from celery conf, i.e. exactly when nobody is looking.
        # `get_spec` is a pure in-memory lookup; no DB read is added to tick().
        refresh_seconds = int(
            self.app.conf.get("beat_refresh_seconds", _beat_refresh_default())
        )
        now = time.monotonic()
        if now - self._last_refresh_at < max(refresh_seconds, 1):
            return
        self._last_refresh_at = now
        try:
            schedule = build_beat_schedule()
        except Exception:
            # Keep ticking on the last known-good schedule; retry next cycle.
            logger.exception("beat_schedule_refresh_failed")
            return
        if not schedule:
            return
        if self._merge_schedule(schedule):
            # Force Scheduler.tick() to rebuild its event heap. In-place entry
            # updates are invisible to schedules_equal (old_schedulers aliases
            # the same entry objects), so clearing the heap is the only
            # reliable trigger to re-time events after a spec change.
            self._heap = None

    def _merge_schedule(self, schedule) -> bool:
        """Reconcile the built schedule dict into ``self.schedule``.

        Returns True when anything was added, removed or updated. Unchanged
        entries are left completely untouched (same objects, same heap
        events, same ``last_run_at``).
        """
        changed = False
        for key in set(self.schedule) - set(schedule):
            entry = self.schedule.pop(key, None)
            if entry is not None:
                self._removed_entry_state[key] = (
                    entry.last_run_at,
                    entry.total_run_count,
                )
            changed = True
        for key, spec in schedule.items():
            new_entry = self.Entry(**dict(spec, name=key, app=self.app))
            existing = self.schedule.get(key)
            if existing is None:
                restored = self._removed_entry_state.pop(key, None)
                if restored is not None:
                    new_entry.last_run_at, new_entry.total_run_count = restored
                self.schedule[key] = new_entry
                changed = True
            elif not existing.editable_fields_equal(new_entry):
                # ScheduleEntry.update only replaces the editable fields
                # (task/schedule/args/kwargs/options); last_run_at and
                # total_run_count are preserved.
                existing.update(new_entry)
                changed = True
        return changed
