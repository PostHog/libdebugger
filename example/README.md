# Flask + libdebugger example

A tiny Flask app you can poke at to see libdebugger fire probes against
real-looking endpoints. Two modes:

- **Local-only**: no PostHog needed. Probe captures print to stderr.
  Hand-written probes in `probes.py` are installed at startup.
- **PostHog-connected**: set `POSTHOG_PROJECT_API_KEY` and probe
  captures flow to PostHog as `$hogtrace_capture` events. Add
  `POSTHOG_PERSONAL_API_KEY` to also pull live programs from the
  control plane.

## Run it

```
cd example
uv sync
uv run python app.py
```

You should see something like:

```
... INFO example.app: No POSTHOG_PROJECT_API_KEY set; running in local-only mode (captures printed to stderr).
... INFO example.app: Installed 3 local program(s)
 * Serving Flask app 'app'
 * Running on http://127.0.0.1:5000
```

## Endpoints

| Method | Path                          | What it does                                |
|--------|-------------------------------|---------------------------------------------|
| GET    | `/health`                     | Sanity check.                               |
| GET    | `/users/<id>`                 | Look up a user.                             |
| POST   | `/users`                      | Create a user. Body: `{name, email}`.       |
| GET    | `/users/<id>/orders`          | List orders for a user.                     |
| POST   | `/orders`                     | Create an order. Body: `{user_id, item, qty}`. |
| GET    | `/slow/<n>`                   | Slow path; useful for exit-probe timing.    |
| GET    | `/_libdebugger/status`        | Dump the registry / wrapped functions.      |

## Try it

In local mode you'll see captures hit stderr as probes fire:

```
curl http://127.0.0.1:5000/users/1
# [probe] $hogtrace_capture program=local-0 probe=probe_0 spec={'specifier': 'example.services.get_user', 'target': 'entry'} captures={"user_id": 1}
# [probe] $hogtrace_capture program=local-0 probe=probe_1 spec={'specifier': 'example.services.get_user', 'target': 'exit'} captures={"user_id": 1}

curl http://127.0.0.1:5000/_libdebugger/status | jq .
# {
#   "installed_programs": ["local-0", "local-1", "local-2"],
#   "probe_index": { "example.services.get_user:entry": [["local-0", "probe_0"]], ... },
#   "wrapped_functions": ["create_order", "get_user", "slow_compute"],
#   "event_sink": "configured",
#   "manager_running": false
# }
```

`manager_running: false` is expected in local-only mode — the poller
only spins up when `POSTHOG_PERSONAL_API_KEY` is set.

## Iterating on probes

Edit `LOCAL_PROBE_SOURCES` in `probes.py` and bounce the server. The
hand-written probes target service functions by their fully-qualified
name (`example.services.get_user`, etc.) — make sure any new probe's
specifier resolves to a real function or you'll see a "not resolvable"
warning at install time.

For PostHog-connected iteration, set the env vars and the manager will
poll the control plane every 30s, picking up program changes
automatically.

## Notes

- The hogtrace request scope is opened in a Flask `before_request` and
  closed in `teardown_request`. Without that scope, probes silently
  skip because `get_store()` returns `None`.
- Probe captures are routed through the event sink registered at
  startup. In real-PostHog mode that's `client.capture`; in local mode
  it's a stdout pretty-printer.
- `services.py` is dependency-free on purpose — its qualnames are what
  the probes target and we don't want extra imports drifting them.
