"""Regression: campaign task completion logs must not shadow LogRecord fields.

``process_due_campaign_steps`` returns a ``created`` count; spreading the
result into ``extra`` collided with the reserved ``LogRecord.created``
attribute and raised ``KeyError`` on every beat after the work had committed.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

from app.tasks import campaigns as campaign_tasks


class _StubAdapter:
    @contextmanager
    def session(self):
        class _Session:
            def commit(self):
                pass

        yield _Session()


def _run_with_stubbed_service(monkeypatch, task, service_name, result):
    from app.services import comms_campaigns

    monkeypatch.setattr(campaign_tasks, "db_session_adapter", _StubAdapter())
    monkeypatch.setattr(comms_campaigns, service_name, lambda session, **kwargs: result)
    return task.run()


def test_step_processing_with_created_count_logs_without_keyerror(monkeypatch, caplog):
    counts = {
        "campaigns": 1,
        "advanced": 1,
        "created": 5,
        "queued": 5,
        "sent": 0,
        "deferred": 0,
    }

    with caplog.at_level(logging.INFO, logger=campaign_tasks.__name__):
        returned = _run_with_stubbed_service(
            monkeypatch,
            campaign_tasks.process_due_campaign_steps,
            "process_due_campaign_steps",
            counts,
        )

    assert returned == counts
    record = next(
        r for r in caplog.records if r.message == "campaign step processing complete"
    )
    assert record.counts == counts


def test_campaign_processing_logs_without_reserved_key_collisions(monkeypatch, caplog):
    counts = {
        "campaigns": 2,
        "built": 1,
        "queued": 10,
        "sent": 10,
        "failed": 0,
        "deferred": 1,
    }

    with caplog.at_level(logging.INFO, logger=campaign_tasks.__name__):
        returned = _run_with_stubbed_service(
            monkeypatch,
            campaign_tasks.process_due_campaigns,
            "process_due_campaigns",
            counts,
        )

    assert returned == counts
    record = next(
        r for r in caplog.records if r.message == "campaign processing complete"
    )
    assert record.counts == counts
