**Live:** (not deployed yet)

# AIST benchmark stand

One shared machine where everyone in the MAS Lifecycle team runs the same benchmarks the same way, instead of each person running their own scripts on their own hardware. Prototype; the machine is temporary, so everything that defines it lives in this repository.

## What is declared here

| Path | What it fixes |
| --- | --- |
| `inventory/stand.yml` | Which machine is the stand. Moving to another VM changes one line. |
| `ansible/` | The machine itself: Docker, a `bench` user, firewall (SSH only), key-only SSH, results directory with retention, a GitHub Actions self-hosted runner as a systemd service. Idempotent; rerun after any change. |
| `benchmarks/*.yaml` | Each benchmark: target repository at an exact commit, container image pinned by digest, setup and run commands, parameters with defaults, which API keys it may read, which directory it produces. |
| `overlays/` | Files copied into the target checkout before the run (for example the T11 provider config). |
| `harness/bench.py` | Runs one benchmark in a container and writes `manifest.json`: definition sha256, target commit, image digest, parameters, exit code, wall time, sha256 per output file, secret scan. |
| `.github/workflows/bench.yml` | The entry point for people: `workflow_dispatch` with bench name, parameter overrides and an optional target ref. Jobs queue on the single runner (FIFO). Results are uploaded as an artifact and kept on the stand. |

API keys are GitHub Actions secrets of this repository. They reach the container through an env file that exists only while the container runs, and every run is scanned for them before upload.

## Use

```bash
make bench B=t11-drift P="n_tasks=5 conditions=v1:A0,v2_units:A0"
make bench B=t11-drift P="provider=glm45 experiment=glm45-check"
```

Or from the Actions tab: `bench` -> Run workflow. To run against your own branch of the target repository, pass `ref` (branch or commit).

Locally, with Docker: `python3 harness/bench.py run t11-drift --set n_tasks=2 --results /tmp/results`.

## Operate

```bash
make test
make check
make deploy
```

`make deploy` fetches a runner registration token with `gh` (needs admin on the runner repository) and applies the playbook. To move the stand: add the operator's SSH key to the new VM, change `ansible_host` in `inventory/stand.yml`, run `make deploy`, then remove the old runner in Settings -> Actions -> Runners.

## Add a benchmark

Copy `benchmarks/t11-drift.yaml`, set `repo`, a full 40-character `ref`, a digest-pinned `image`, `run` with `{param}` placeholders and their defaults, `outputs` and the `secrets` it needs. `make test` validates it. Parameters passed at run time must be declared; values are shell-quoted.

## Not done yet

- The runner is attached to this repository; for the lab it should move to an organization runner group so other repositories can call it.
- No GPU and no shared response cache between runs yet.
- One runner means one job at a time; a second runner on the same machine is a small playbook change.
