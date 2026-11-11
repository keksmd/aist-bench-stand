**Live:** https://93.189.228.196 (UI and API, basic auth; ask the operator for the password)

# AIST benchmark stand

One shared machine where the MAS Lifecycle team runs the same benchmarks the same way, stores every run with its traces in one place and looks at them in one UI. Prototype: the machine is temporary, so everything that defines it is declared in this repository and `make deploy` rebuilds it on any Ubuntu VM.

## Pieces

| Piece | Where | What it does |
| --- | --- | --- |
| MAS | `mas/*.yaml` | A multi-agent system at an exact commit: repository, digest-pinned image, setup, files overlaid on the checkout (provider config), agents, edges, tools, parameters with allowed values, the API keys it may read. |
| Benchmark | `benchmarks/*.yaml` | A task set: run command with `{param}` placeholders, output directory, trace adapter, parameters. Runs on any MAS of the same `family`. |
| Trace adapter | `stand/adapters/` | Reads a benchmark's output into common task records (task, condition, repeat, pass, reason, tokens, duration) and common trace events (agent, LLM call, tool calls with results, final check). |
| Queue and storage | `stand/db.py`, `/srv/bench/stand.db`, `/srv/bench/results/<run>/` | SQLite index of runs and task outcomes; the run directory keeps `outputs/` (raw traces), `log.txt` and `manifest.json` (MAS commit, image digest, parameters, command, exit code, wall time, sha256 per file, secret scan). |
| Worker | `stand/worker.py`, `stand/executor.py` | Takes queued runs one by one, checks out the MAS, runs the benchmark in a container, updates progress, collects results. A restart waits for the current run to finish. |
| API and UI | `stand/api.py`, `ui/index.html` | Runs list with progress, launch form, per-run pass table by condition, task x condition matrix, trace viewer, container log. |
| Machine | `inventory/stand.yml`, `ansible/` | Docker, `bench` user, firewall (22, 80, 443), key-only SSH, GitHub Actions runner, API and worker as systemd services, Caddy with a Let's Encrypt certificate for the IP and basic auth. |

## Run something

- UI: open the stand, pick MAS, benchmark and parameters, press "В очередь".
- GitHub: Actions -> `bench` -> Run workflow, or `make bench M=t11 B=drift P="provider=glm45 n_tasks=5"`. The job waits for the run and links it in the job summary.
- API: `curl -u aist:<password> -H 'content-type: application/json' -d '{"mas":"t11","bench":"drift","overrides":{"n_tasks":5}}' https://93.189.228.196/api/runs`.
- To test your own version of a MAS, pass `ref` (branch or commit of the MAS repository) or add a `mas/<name>.yaml` that points at your fork.

`provider=fake` costs nothing and checks the wiring. Other providers call free-tier models with the keys stored on the stand.

## Add a MAS or a benchmark

Copy an existing YAML. A MAS needs a full 40-character `ref` and an image pinned by `@sha256`. A benchmark needs a `family` that some MAS has and an adapter in `stand/adapters/` that reads its output directory. `make test` validates everything.

## Operate

```bash
make test
make deploy
gh workflow run sync-secrets.yml -R keksmd/aist-bench-stand
```

`make deploy` pushes, pins the stand code to the current commit and applies the playbook. API keys live in the repository's GitHub Actions secrets; `sync-secrets` writes them to the stand, and the worker reads them at every run. To move the stand: add the operator's SSH key to the new VM, change `ansible_host` in `inventory/stand.yml`, `make deploy`, then remove the old runner in Settings -> Actions -> Runners. Earlier local output directories can be stored with `python -m stand import <dir> <bench> --mas <id> --set k=v --note "..."`.

## Not done yet

- One MAS family (T11). The family contract is the T11 command line and output layout; a second family needs its own adapter.
- One worker, so runs execute one at a time.
- Single shared UI password; no per-person accounts.
- No response cache shared between runs.
