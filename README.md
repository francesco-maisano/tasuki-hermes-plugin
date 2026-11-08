# tasuki — Hermes plugin

The agent-side half of tasuki's **Agent Gateway**. Installed into *your own* Hermes (from the tasuki iPhone app, through
Hermes' dashboard API, or by hand), it lets the tasuki app hand a request to your main Hermes and hear the answer
stream back, and it gives Hermes read-only tools for your mail, calendar and food log, and keeps a read-only markdown copy of your tasks inside the Hermes data directory (plus an inbox folder that turns dropped files into tasks).

Version `0.4.1`, protocol version `1`. Native Hermes plugin (`plugin.yaml` + `register(ctx)`), MIT.

## What it does

| Part | Where | What |
|---|---|---|
| Connect, enrol, status and config routes | `dashboard/plugin_api.py` | `POST /api/plugins/tasuki/connect` (owner claim secret), `POST /enroll-device` (one-time phone code), `POST /pair` (legacy code), `GET /status`, `POST /unpair`, `POST /config` (mirror directory), `POST /mirror/resync` on Hermes' dashboard. Need the dashboard login. |
| Delegation worker | `core/worker.py` | Long-polls tasuki for delegations and runs each in your main Hermes, streaming `speakable` / `progress` / `commentary` / `final` / `proposal` / `error` events back. Heartbeat every 10 s, cancel honoured, reconnects with backoff. Runs **inside the dashboard process**: no second `hermes serve`. |
| Data tools | `core/tools.py`, `__init__.py` | 12 tools, all prefixed `tasuki_` (below). |
| Task mirror | `core/mirror.py` | One-way markdown mirror of your tasks under `<HERMES_HOME>/tasuki/` (configurable): one file per open task, `done/YYYY-MM/`, four views, a README, a state file. Follows tasuki's change feed with the agent token; runs only while paired. See "The task mirror and inbox". |
| Inbox | `core/inbox.py` | `<mirror dir>/inbox/`: drop a `.md`, it becomes a task through the REST API (`source: agent:<id>`, idempotent by file hash); results go to `processed/` or `rejected/`. |

### Tools

`tasuki_mail_conversations`, `tasuki_mail_thread`, `tasuki_mail_read`, `tasuki_mail_draft_reply`,
`tasuki_calendar_events`, `tasuki_calendar_free`, `tasuki_calendar_summary`, `tasuki_meeting_prep`,
`tasuki_food_day`, `tasuki_food_week`, `tasuki_food_recent`, `tasuki_tasks_dir`, `tasuki_health_summary`, `tasuki_health_today`.

The two health tools (0.4.0, 08/11/2026) read the daily Apple Health summary the iPhone app sends to the bridge once the user turns on
"Let your agent see your health" (Settings, Health): steps, activity, sleep with stages, resting heart rate, HRV, weight, workouts, per day, with
`as_of` (when the phone read Health). It lives on the user's own bridge, next to their food log, and is read with the `food:read` scope.
Until sharing is on, both tools say so instead of returning numbers.

`tasuki_tasks_dir` returns the mirror folder and a paragraph telling Hermes it can read the task files directly with its file tools
(`read_file`, `search_files`), that they are read-only, and how to create tasks (the inbox or the tasuki task tools).
Mail reads always ask the bridge for `?format=agent` (compact plain text, 4000 characters per message, wrapped as untrusted).

Hermes defers plugin tools behind its own `tool_search` / `tool_call`, so the model finds them by searching; they are
hidden until the plugin is paired.

## What it can and cannot do

* **Can:** read mail (conversations, threads, one message as plain text), read the calendar (events, free slots, today's
  summary, meeting prep), read the food log and the shared health summary, create a **reply draft** (Outlook or Gmail) and return its link.
* **Cannot, by construction:** send, forward, delete, move, flag or mark mail; create, change or answer calendar events;
  write the food log. There is no such tool, and the HTTP client refuses any bridge call that is not on a short allowlist
  (`core/client.py` `ALLOWED_BRIDGE`; tests enumerate the tool list and the allowlist). A draft is the only write, and the
  user sends it from Outlook.
* Mail and calendar content is **untrusted data**: tool results are wrapped in an envelope
  (`"untrusted_content": true` + a notice), tool descriptions say so, bodies are plain text (HTML is converted, never passed
  through) and cut at 4000 characters with a `truncated` flag.
* Delegated requests run with whatever tools *your* Hermes has, exactly as if you had typed them. Pairing the tasuki app
  therefore grants the app (and tasuki's server, which holds the pairing) the ability to ask your Hermes to do things.
  The server also gets the `delegate` scope only if you pair. Unpair removes the token.

## The task mirror and inbox

tasuki core stays the single writer of the task database. While the plugin is paired it keeps a **read-only markdown copy** next to
the rest of the Hermes data, so Hermes and you can read your tasks as plain files:

```
<HERMES_HOME>/tasuki/                 (default; 0700; files 0600; change with POST /api/plugins/tasuki/config {"mirror_dir": "/abs/path"})
  README.md                           the format and the rules
  .tasuki-mirror.json                 state: schema version, cursor, file hashes, counters, view rows (task titles, no notes)
  tasks/<id>-<slug>.md                one file per OPEN task (front matter + notes + "## Comments")
  tasks/done/YYYY-MM/<id>-<slug>.md   completed tasks, with their evidence
  views/today.md  overdue.md  waiting-on-me.md  waiting-on-others.md
  inbox/                              drop a .md here to create a task; inbox/processed/ and inbox/rejected/ (+ .reason.txt)
```

* **Sync:** an initial full sync (cursor first, then the lists), then `GET /api/v1/changes?format=events` (long-poll, pages of 200).
  Events are applied idempotently; the state is saved after every page, so a crash resumes from the saved cursor. A reset from the
  server, or a project/label rename or import (`resync: tasks`), triggers a fresh baseline. Needs the `read` scope (the assistant
  preset has it); the inbox needs `write`.
* **No churn:** a file is rewritten only when its content hash changed; nothing in a file depends on the clock. Writes are temp file,
  fsync, rename, so a reader never sees half a file.
* **Read-only, enforced by restoring:** a mirrored file changed by something else (hash differs) is **never** pushed to the server.
  It is noted in `.tasuki-mirror.json` (counter plus the path and time, never the edit's text), a hint is written to `inbox/README.md`,
  and the file is restored from the server. tasuki has no route for an agent that holds `write` to *suggest a change* to an existing task
  (suggestions are new `proposed` tasks and only for credentials without `add`), so detection is counted, not turned into a suggestion.
  The mirror only ever deletes files it wrote. An edit made while the plugin was not running cannot be told from a crash, so after a restart
  drift is repaired quietly (counted as `recovered`, not as an external edit).
* **Inbox rules:** `.md`, at most 64 KB, UTF-8, 30 files a minute; front matter keys `title due project client labels priority effort next`
  (anything else is rejected); title from front matter, first `# heading`, or the file name. Created through `POST /api/v1/tasks` with
  `source: {"label": "agent:<agent id>"}` and `Idempotency-Key: inbox-<sha256 of the file>`; a re-drop is the same task (a local hash table
  covers longer than the server's 24 h replay). Symlinks are never followed (the folder is opened with `O_NOFOLLOW` and everything is
  relative to that descriptor); a symlink is moved aside, as a link, and rejected. Server trouble (429, 5xx, offline) leaves the file for
  the next scan; a verdict (created, 4xx, missing scope) moves it.
* **Failure isolation:** an unwritable directory, an offline server, a revoked token or a missing scope only show up in `GET /status`
  (`mirror.errors.last`: `dir_unwritable`, `unreachable`, `unauthorized`, `missing_scope:read`). The delegation worker is a separate thread
  and keeps running. Revoked token: the mirror stops polling until you pair again. Offline: backoff 1 to 30 s.
* **Privacy:** the mirror holds your task text on disk in the Hermes data directory (directory 0700, files 0600), and `.tasuki-mirror.json`
  holds task titles for the views. Nothing in the logs: only counts, ids and short codes. Unpair leaves the folder (it is your data); delete it
  to remove the copy.
* **Status:** `GET /status` gains `mirror {dir, running, last_sync_at, tasks, errors{count,last}, cursor, external_edits, recovered, resyncs, inbox{processed,rejected,pending}}`
  and `config {mirror_dir, mirror_dir_default}`. `POST /mirror/resync` queues a full re-sync (runs within one poll, at most about 10 s).

## Install

From the app (what the iPhone app does): see `docs/AGENTS_HERMES.md`. By hand:

```sh
hermes plugins install francesco-maisano/tasuki-hermes-plugin --enable
hermes dashboard                                                            # the routes and the worker live in the dashboard process
```

Hermes' install scan reports this plugin `safe`; it is installed from an unreviewed custom source, so Hermes says so.

## Upgrading from 0.2.0

A plugin installed with a pinned `ref` is not moved by `POST /api/dashboard/agent-plugins/tasuki/update` (Hermes answers `400 ... pinned`). Re-install over it:
`hermes plugins install francesco-maisano/tasuki-hermes-plugin --force --ref <40-char SHA of the release>` (or the same through `POST /api/dashboard/agent-plugins/install` with `force:true`).
A running dashboard keeps serving the old routes after that (no `/connect`, `/status` still says 0.2.0), and disable/enable does not change that: **restart the dashboard** (`systemctl --user restart hermes-dashboard.service` where it runs as a service). A paired connection resumes by itself.

## Connect and enrol a phone (no tasuki sign-in)

There is no tasuki account to sign in to. Your phone's identity comes from controlling your Hermes: the app logs into the Hermes dashboard,
installs this plugin, tells it to **connect** to your tasuki server, then asks it to **enrol** the phone.

The connection uses the tasuki server's **claim secret**, a long random string the server generates once and keeps in `claim.secret` in its data
directory. The plugin finds it, in this order: the request body (`claim_secret`), Hermes' secret store (`TASUKI_CLAIM_SECRET`), a local
file (config key `claim_file`, else `~/tasuki-data/claim.secret`, `/var/lib/tasuki/claim.secret`, `/etc/tasuki/claim.secret`). If Hermes runs on
the same machine as tasuki it just works; otherwise the call answers `409 claim_secret_needed` and you paste the secret once. It is kept only if you send
`remember:true`. A claim file that others can write, or that everyone can read and someone else owns, is refused; world-readable but yours is used with a log
warning (`chmod 600` it). The secret is never logged, returned, or written by the plugin.

```sh
T="Authorization: Bearer <dashboard access token>"
curl -H "$T" -H 'Content-Type: application/json' -X POST http://127.0.0.1:9119/api/plugins/tasuki/connect \
  -d '{"server_url":"https://app.usetasuki.com"}'                       # add "claim_secret":"..." (and "remember":true) if it answers 409 claim_secret_needed
curl -H "$T" -H 'Content-Type: application/json' -X POST http://127.0.0.1:9119/api/plugins/tasuki/enroll-device \
  -d '{"device_name":"My iPhone"}'                                      # -> {"code":"ABCD-EFGH","expires_at":"...","server_url":"..."}
curl -H "$T" http://127.0.0.1:9119/api/plugins/tasuki/status
```

The claim returns an agent token with the assistant scopes plus `enroll_device`, which the plugin stores exactly like a pairing token. Connecting
again replaces the token (and revokes the old agent on the server). Enrolment codes are one-time and last about 5 minutes; the plugin allows 5 per 10 minutes
(the server allows 3). `GET /status` gives `claimed`, `enroll_available`, `server` and `features` (`claim`, `enroll_device`, `pair_legacy`).

**Legacy pairing** (`POST /pair {"server_url","code"}` with a code from the tasuki app) still works. It has no `enroll_device` scope, so it cannot enrol a phone.

`server_url` must be `https://` (plain `http://` only for localhost, for development). Both flows refuse a server that speaks a different `protocol_version`.
Contract and error table: `docs/AGENTS_HERMES.md`.

## Security

* **Token storage:** the agent token (`ts_...`) is written with Hermes' own secret writer to `$HERMES_HOME/.env`
  (`TASUKI_AGENT_TOKEN`, mode 0600, per profile; the optional remembered claim secret is `TASUKI_CLAIM_SECRET` in the same file), never into the plugin directory, the repo, `config.yaml` or the state file.
  Non-secret facts (server, agent id, scopes) are in `$HERMES_HOME/state/tasuki/state.json` (0600). Nothing is logged except
  delegation ids, counts and short codes: no delegation text, reply text, token, pairing code, claim secret or enrolment code.
* **Routes** need the dashboard credential (and the plugin checks it again, failing closed). They only exist while the
  plugin is installed **and** in `plugins.enabled` (Hermes' own trust gate).
* **Bearer discipline:** the HTTP client follows no redirects and verifies TLS.
* **Supply chain / updates:** the plugin runs inside your dashboard process. Install it from a source you trust and pin a
  commit with `ref` when you can. Do **not** switch on Hermes' plugin auto-update for it unless you trust the upstream: an
  update is arbitrary code in your Hermes. (Hermes re-scans and asks again on a risky update.)
* **Unpair** stops the worker and the mirror, calls `DELETE /api/v1/agents/me` so tasuki revokes this agent, and deletes the token
  locally (`server_revoked: true`). If tasuki cannot be reached the token is still forgotten locally and `server_revoked: false` says to
  revoke the agent in the tasuki app.

## Develop and test

```sh
cd hermes-plugin
python3 -m unittest discover -s tests          # 130+ tests, no Hermes needed (fake tasuki server, fake change feed, fake Hermes gateway)
hermes plugins validate .                      # Hermes' own admission checks
tests/integration_isolated.sh fake             # plugin + throwaway Hermes against fake_tasuki (3-5 min; never touches your live Hermes)
tests/integration_isolated.sh chain            # the REAL chain: throwaway tasuki core + throwaway bridge + throwaway Hermes + the task mirror
python3 tests/fake_tasuki.py --port 8765       # a fake tasuki server for app development
```

Layout: `plugin.yaml`, `__init__.py` (register), `dashboard/` (manifest + routes), `core/` (client, store, worker, tools, mirror, inbox,
speech helpers, `persona/*.v2.txt` copied from tasuki voice), `tests/`.

## Known limits (v0)

* The worker needs the dashboard process (`hermes dashboard`; not a bare gateway). With `plugins.isolation: host`
  Hermes serves the routes from a separate plugin host process; the worker needs the dashboard's in-process credentials, so
  expect `hermes_unreachable` (not tested). Keep the default `in_process`.
* After a plugin *update* the dashboard process keeps the old code until it restarts.
* Hermes sessions for delegations are per `session_key` and titled `tasuki voice HH:MM`; they are not resumed after a
  dashboard restart (a new session starts).
* Reply text is held until the turn completes (text before a tool call is narration and is dropped), then sent as
  `commentary` chunks and a `final`.
