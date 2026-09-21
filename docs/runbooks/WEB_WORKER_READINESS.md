# Web worker route readiness

Every supported API router is loaded during the FastAPI lifespan before it
yields to Uvicorn. `/api/v1/health/ready` remains non-ready until the route
table contains `/api/v1/subscribers/sync` and essential startup checks pass.
Optional dashboard prewarming, settings seeding, webhook-policy hydration, and
scheduler-drift checks run only after route readiness.

Roll out with the normal rolling-release sequence: deploy one new worker,
wait for its readiness endpoint and the startup-complete log, send an
authenticated subscriber-sync canary, then progress the remaining workers.
Abort if readiness does not become true or any sync-route 404 is observed.
Rollback by returning to the prior immutable image digest through the normal
rolling process; do not perform an ad-hoc restart.

The platform owner must remove the unavailable upstream from Nginx:

```nginx
upstream dotmac_sub {
    server 127.0.0.1:8000;
    # Remove: server 127.0.0.1:18002 backup;
}
```

Port 18002 may return only as a genuinely healthy, independently monitored
standby with matching application configuration.


`runtime.web_worker_readiness` is implemented by
`app.services.web_worker_readiness`. It evaluates typed route and preflight
observations. The application startup adapter supplies those observations only
after preflight and router loading, then maps the result to readiness state.
