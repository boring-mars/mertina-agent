# Mertina Web UI

The browser UI for the Mertina Gateway: React 19, TypeScript, Vite, Tailwind CSS v4 and shadcn/ui.
The Gateway serves the built files from `web/dist` on its own address, so the UI and the API share
one origin. The development plan is [docs/plans/v0.3-web-ui.md](../docs/plans/v0.3-web-ui.md); the
coding and testing rules are in [docs/en/development/](../docs/en/development/coding-style.md#front-end-web).

This is the scaffold (checkpoint CP2): the API client, the event-stream reader and the run-event
reducer are in place, and the page only shows whether the Gateway is reachable. Sessions and chat
come in the next checkpoints.

## Setup

Node.js 22 (see `.nvmrc`) and npm. From the repository root:

```bash
npm --prefix web ci
```

## Develop

Start a Gateway, then the Vite dev server, which proxies `/api` and `/v1` to it:

```bash
uv run mertina gateway
```

```bash
npm --prefix web run dev
```

Open the address Vite prints. The proxy targets `http://127.0.0.1:8765`; set
`MERTINA_WEB_GATEWAY_URL` to use another Gateway. It rewrites both `Host` and `Origin`, because the
Gateway refuses requests whose origin is not its own.

To work without a model key, run the deterministic stub model from `dev/e2e/` instead; its markers
(`[tool]`, `[parallel]`, `[toolerror]`, `[retry]`, `[fail]`, `[slow]`, `[tokens]`) reproduce each
run state. See `dev/e2e/stub_model.py`.

## Build

```bash
npm --prefix web run build
```

The Gateway serves `web/dist` the next time it starts. The build inlines nothing (no inline scripts,
no `data:` fonts), because the Gateway's Content-Security-Policy allows same-origin files only.

## Check

| Command                             | What it does                 |
| ----------------------------------- | ---------------------------- |
| `npm --prefix web run typecheck`    | TypeScript, strict           |
| `npm --prefix web run lint`         | ESLint                       |
| `npm --prefix web run format:check` | Prettier (`format` rewrites) |
| `npm --prefix web test`             | Vitest                       |

## Layout

| Path                    | Contents                                                                                                       |
| ----------------------- | -------------------------------------------------------------------------------------------------------------- |
| `src/api/`              | REST client, runtime decoders for every Gateway response, error types, SSE reader                              |
| `src/runs/`             | Typed run events, the pure run-event reducer, and the controller that follows a run (reconnect, resynchronize) |
| `src/ui/`               | shadcn/ui components and helpers; design tokens are in `src/index.css`                                         |
| `src/app/`              | The app shell and `copy.ts`, which holds all UI text                                                           |
| `tests/`                | Vitest tests mirroring `src/`; `tests/fixtures/runs/` holds recorded event sequences                           |
| `scripts/recordRun.mjs` | Records a run from a live Gateway as a fixture                                                                 |

## Recording run fixtures

Fixtures are recorded from the stub model, never from a real provider, because they are committed.
Start the stub model and `dev/e2e/serve_gateway.py` with an env file that points the Gateway at it,
then, for example:

```bash
node web/scripts/recordRun.mjs --gateway http://127.0.0.1:18765 --name tool --message "look it up [tool]"
```

Run and session IDs are replaced with fixed placeholders. The reducer tests replay every fixture and
compare the result with the Gateway's own final snapshot.
