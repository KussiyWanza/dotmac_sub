from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_supported_routes_complete_before_lifespan_yields() -> None:
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    before_yield = source.split("    try:\n        yield", maxsplit=1)[0]
    assert "await _load_deferred_api_routers(app)" in before_yield
    assert "evaluate_worker_readiness(" in before_yield
    assert before_yield.index(
        "_startup_preflight()", before_yield.index("async def lifespan")
    ) < before_yield.index("readiness = evaluate_worker_readiness(")
    assert "app.state.routes_ready = True" in before_yield
    assert "asyncio.create_task(_load_deferred_api_routers(app))" not in source


def test_readiness_requires_route_registration() -> None:
    source = (ROOT / "app/api/health.py").read_text(encoding="utf-8")
    assert 'getattr(request.app.state, "routes_ready", False)' in source
    assert '"status": "not_ready"' in source


def test_worker_readiness_requires_both_preflight_and_supported_routes() -> None:
    from app.services.web_worker_readiness import (
        WorkerStartupObservation,
        evaluate_worker_readiness,
    )

    assert not evaluate_worker_readiness(WorkerStartupObservation((), True)).ready
    routes = ("/api/v1/subscribers/sync",)
    assert not evaluate_worker_readiness(WorkerStartupObservation(routes, False)).ready
    assert evaluate_worker_readiness(WorkerStartupObservation(routes, True)).ready
