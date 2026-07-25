"""Queue strategy adapter for backpressure and health management.

Provides intelligent queue routing with:
- Queue depth monitoring and backpressure
- Task prioritization and shedding
- Circuit breaker for overloaded queues
- Automatic routing to dedicated queues

Usage:
    from app.services.queue_strategy_adapter import strategic_enqueue

    # Enqueue with automatic strategy
    result = strategic_enqueue(
        "app.tasks.bandwidth.process_bandwidth_stream",
        args=[data],
        priority="low",  # low tasks can be shed under pressure
    )
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.services.adapters import adapter_registry
from app.services.queue_adapter import QueueDispatchResult, enqueue_task

logger = logging.getLogger(__name__)


class TaskPriority(str, Enum):
    """Task priority levels for shedding decisions."""

    critical = "critical"  # Never shed (auth, billing)
    high = "high"  # Shed only under extreme pressure
    normal = "normal"  # Default priority
    low = "low"  # Shed first (metrics, analytics)
    bulk = "bulk"  # Always deferrable


class QueueHealth(str, Enum):
    """Queue health states."""

    healthy = "healthy"  # < 100 tasks
    elevated = "elevated"  # 100-500 tasks
    degraded = "degraded"  # 500-2000 tasks
    critical = "critical"  # > 2000 tasks
    circuit_open = "circuit_open"  # Circuit breaker tripped


@dataclass
class QueueHealthState:
    """Current health state for a queue."""

    name: str
    depth: int = 0
    health: QueueHealth = QueueHealth.healthy
    last_check: float = 0.0
    circuit_open_until: float = 0.0
    tasks_shed: int = 0
    tasks_deferred: int = 0


@dataclass
class StrategyConfig:
    """Configuration for queue strategy."""

    # Queue depth thresholds
    healthy_threshold: int = 100
    elevated_threshold: int = 500
    degraded_threshold: int = 2000
    critical_threshold: int = 5000

    # Circuit breaker settings
    circuit_break_threshold: int = 10000
    circuit_recovery_seconds: int = 60

    # Backpressure settings
    shed_low_priority_at: QueueHealth = QueueHealth.degraded
    shed_normal_priority_at: QueueHealth = QueueHealth.critical
    defer_bulk_at: QueueHealth = QueueHealth.elevated

    # Health check interval
    health_check_interval_seconds: float = 5.0


# Task routing configuration - maps task prefixes to queues
TASK_QUEUE_ROUTING: dict[str, str] = {
    # ACS/TR-069 tasks -> acs queue
    "app.tasks.tr069.": "acs",
    # Provisioning -> tr069 queue
    "app.tasks.ont_provisioning.": "tr069",
    "app.tasks.olt_queue.": "tr069",
    # High-volume tasks -> dedicated queues (to be created)
    "app.tasks.bandwidth.": "bandwidth",
    "app.tasks.usage.": "ingestion",
}

# Task priority mapping
TASK_PRIORITY_MAPPING: dict[str, TaskPriority] = {
    # Critical - never shed
    "app.tasks.ont_provisioning.": TaskPriority.critical,
    "app.tasks.billing.": TaskPriority.critical,
    "app.tasks.tr069.apply_acs_config": TaskPriority.critical,
    "app.tasks.tr069.wait_for_ont_bootstrap": TaskPriority.critical,
    # High priority
    "app.tasks.tr069.reconcile_command_outcomes": TaskPriority.high,
    "app.tasks.tr069.sync_all_acs_devices": TaskPriority.high,
    # Normal priority (default)
    "app.tasks.tr069.check_device_health": TaskPriority.normal,
    # Low priority - can be shed
    "app.tasks.tr069.scrape_genieacs_metrics": TaskPriority.low,
    "app.tasks.tr069.cleanup_": TaskPriority.low,
    "app.tasks.monitoring_cleanup.": TaskPriority.low,
    # Bulk - always deferrable
    "app.tasks.bandwidth.": TaskPriority.bulk,
    "app.tasks.usage.": TaskPriority.bulk,
    "app.tasks.exports.": TaskPriority.bulk,
}


class QueueStrategyAdapter:
    """Strategic queue adapter with health monitoring and backpressure."""

    name = "queue.strategy"

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.config = config or StrategyConfig()
        self._queue_states: dict[str, QueueHealthState] = {}
        self._lock = threading.Lock()
        self._redis_client: Any = None

    def _get_redis(self) -> Any:
        """Lazy load Redis client."""
        if self._redis_client is None:
            from app.services.redis_client import get_redis

            self._redis_client = get_redis()
        return self._redis_client

    def _get_queue_depth(self, queue_name: str) -> int:
        """Get current queue depth from Redis."""
        try:
            redis = self._get_redis()
            if redis is None:
                return 0
            return redis.llen(queue_name) or 0
        except Exception as e:
            logger.debug("Failed to get queue depth for %s: %s", queue_name, e)
            return 0

    def _update_queue_health(self, queue_name: str) -> QueueHealthState:
        """Update and return health state for a queue."""
        now = time.time()

        with self._lock:
            state = self._queue_states.get(queue_name)
            if state is None:
                state = QueueHealthState(name=queue_name)
                self._queue_states[queue_name] = state

            # Check if we need to refresh
            if now - state.last_check < self.config.health_check_interval_seconds:
                return state

            # Get current depth
            depth = self._get_queue_depth(queue_name)
            state.depth = depth
            state.last_check = now

            # Check circuit breaker recovery
            if state.health == QueueHealth.circuit_open:
                if now >= state.circuit_open_until:
                    logger.info(
                        "Queue %s circuit breaker recovering, depth=%d",
                        queue_name,
                        depth,
                    )
                    # Fall through to re-evaluate health
                else:
                    return state

            # Evaluate health
            if depth >= self.config.circuit_break_threshold:
                state.health = QueueHealth.circuit_open
                state.circuit_open_until = now + self.config.circuit_recovery_seconds
                logger.warning(
                    "Queue %s circuit breaker OPEN, depth=%d, recovery in %ds",
                    queue_name,
                    depth,
                    self.config.circuit_recovery_seconds,
                )
            elif depth >= self.config.critical_threshold:
                state.health = QueueHealth.critical
            elif depth >= self.config.degraded_threshold:
                state.health = QueueHealth.degraded
            elif depth >= self.config.elevated_threshold:
                state.health = QueueHealth.elevated
            else:
                state.health = QueueHealth.healthy

            return state

    def _get_task_priority(self, task_name: str) -> TaskPriority:
        """Determine priority for a task."""
        for prefix, priority in TASK_PRIORITY_MAPPING.items():
            if task_name.startswith(prefix):
                return priority
        return TaskPriority.normal

    def _get_task_queue(self, task_name: str) -> str | None:
        """Determine target queue for a task."""
        for prefix, queue in TASK_QUEUE_ROUTING.items():
            if task_name.startswith(prefix):
                return queue
        return None

    def _should_shed(self, priority: TaskPriority, health: QueueHealth) -> bool:
        """Determine if a task should be shed based on priority and health."""
        if priority == TaskPriority.critical:
            return False

        if health == QueueHealth.circuit_open:
            # Only critical tasks allowed when circuit is open
            return priority != TaskPriority.critical

        if priority == TaskPriority.low:
            return health.value >= self.config.shed_low_priority_at.value

        if priority == TaskPriority.normal:
            return health.value >= self.config.shed_normal_priority_at.value

        if priority == TaskPriority.bulk:
            return health.value >= self.config.defer_bulk_at.value

        return False

    def _should_defer(self, priority: TaskPriority, health: QueueHealth) -> int | None:
        """Determine if task should be deferred and by how much."""
        if priority == TaskPriority.critical:
            return None

        if priority == TaskPriority.bulk and health.value >= QueueHealth.elevated.value:
            # Defer bulk tasks by 30-120s based on health
            base_delay = 30
            if health == QueueHealth.degraded:
                base_delay = 60
            elif health == QueueHealth.critical:
                base_delay = 120
            return base_delay

        return None

    def enqueue(
        self,
        task_name: str,
        *,
        args: tuple[object, ...] | list[object] | None = None,
        kwargs: dict[str, object] | None = None,
        queue: str | None = None,
        countdown: int | None = None,
        priority: TaskPriority | str | None = None,
        correlation_id: str | None = None,
        source: str | None = None,
        force: bool = False,
        **extra_kwargs: Any,
    ) -> QueueDispatchResult:
        """Enqueue a task with strategic routing and backpressure.

        Args:
            task_name: Celery task name
            args: Task arguments
            kwargs: Task keyword arguments
            queue: Override target queue (auto-routed if None)
            countdown: Delay in seconds
            priority: Task priority (auto-detected if None)
            correlation_id: Correlation ID for tracing
            source: Source identifier
            force: Bypass backpressure checks
            **extra_kwargs: Additional kwargs for enqueue_task

        Returns:
            QueueDispatchResult with queued status
        """
        # Determine queue
        target_queue = queue or self._get_task_queue(task_name)

        # Determine priority
        if priority is None:
            task_priority = self._get_task_priority(task_name)
        elif isinstance(priority, str):
            task_priority = TaskPriority(priority)
        else:
            task_priority = priority

        # Get queue health (use 'celery' for None/default queue)
        check_queue = target_queue or "celery"
        health_state = self._update_queue_health(check_queue)

        # Apply backpressure unless forced
        if not force:
            # Check if we should shed this task
            if self._should_shed(task_priority, health_state.health):
                with self._lock:
                    health_state.tasks_shed += 1

                logger.info(
                    "Task shed due to backpressure: task=%s queue=%s health=%s priority=%s",
                    task_name,
                    check_queue,
                    health_state.health.value,
                    task_priority.value,
                )
                return QueueDispatchResult(
                    queued=False,
                    task_name=task_name,
                    queue=target_queue,
                    error=f"Task shed: queue {check_queue} is {health_state.health.value}",
                )

            # Check if we should defer
            defer_seconds = self._should_defer(task_priority, health_state.health)
            if defer_seconds and countdown is None:
                countdown = defer_seconds
                with self._lock:
                    health_state.tasks_deferred += 1
                logger.debug(
                    "Task deferred %ds: task=%s queue=%s health=%s",
                    defer_seconds,
                    task_name,
                    check_queue,
                    health_state.health.value,
                )

        # Dispatch task
        return enqueue_task(
            task_name,
            args=args,
            kwargs=kwargs,
            queue=target_queue,
            countdown=countdown,
            correlation_id=correlation_id,
            source=source,
            **extra_kwargs,
        )

    def get_all_queue_health(self) -> dict[str, dict[str, Any]]:
        """Get health status for all tracked queues."""
        result = {}
        for queue_name in [
            "celery",
            "tr069",
            "acs",
            "bandwidth",
            "monitoring",
            "ingestion",
        ]:
            state = self._update_queue_health(queue_name)
            result[queue_name] = {
                "depth": state.depth,
                "health": state.health.value,
                "tasks_shed": state.tasks_shed,
                "tasks_deferred": state.tasks_deferred,
            }
        return result

    def reset_circuit_breaker(self, queue_name: str) -> bool:
        """Manually reset circuit breaker for a queue."""
        with self._lock:
            state = self._queue_states.get(queue_name)
            if state and state.health == QueueHealth.circuit_open:
                state.circuit_open_until = 0
                state.health = QueueHealth.healthy
                logger.info("Circuit breaker manually reset for queue %s", queue_name)
                return True
        return False


# Global instance
queue_strategy = QueueStrategyAdapter()
adapter_registry.register(queue_strategy)


def strategic_enqueue(
    task_name: str,
    *,
    args: tuple[object, ...] | list[object] | None = None,
    kwargs: dict[str, object] | None = None,
    queue: str | None = None,
    countdown: int | None = None,
    priority: TaskPriority | str | None = None,
    correlation_id: str | None = None,
    source: str | None = None,
    force: bool = False,
    **extra_kwargs: Any,
) -> QueueDispatchResult:
    """Enqueue a task with strategic routing and backpressure.

    This is the recommended entry point for task dispatch when backpressure
    management is needed.

    Example:
        from app.services.queue_strategy_adapter import strategic_enqueue, TaskPriority

        # Auto-routing based on task name
        result = strategic_enqueue(
            "app.tasks.bandwidth.process_bandwidth_stream",
            args=[stream_data],
        )

        # Explicit priority
        result = strategic_enqueue(
            "app.tasks.custom.my_task",
            args=[data],
            priority=TaskPriority.low,
        )

        # Force bypass backpressure
        result = strategic_enqueue(
            "app.tasks.critical.must_run",
            args=[data],
            force=True,
        )
    """
    return queue_strategy.enqueue(
        task_name,
        args=args,
        kwargs=kwargs,
        queue=queue,
        countdown=countdown,
        priority=priority,
        correlation_id=correlation_id,
        source=source,
        force=force,
        **extra_kwargs,
    )


def get_queue_health() -> dict[str, dict[str, Any]]:
    """Get current health status for all queues."""
    return queue_strategy.get_all_queue_health()
