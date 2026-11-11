# Shared benchmark stand: design

Status: proposal for the MAS Lifecycle team, 12 November 2026. Sections 1-2 describe what runs today; sections 3-8 are the plan. Decisions that belong to the team are listed in section 9.

## 1. Goal

Everyone runs the same benchmarks on the same machine under the same rules, from their own branch, and the results land in one place with traces. Concretely:

- a run can be started from a branch or from a comment on a pull request in `barakhsin/mas-lifecycle`;
- the system under test is one MAS chosen by the team, and any of its components can be replaced for a run without touching the others;
- every run keeps its inputs (commits, image digest, parameters), raw traces, task outcomes and the analyses the lifecycle components produce on it;
- access follows access to the project repository.

## 2. What exists (observed, `keksmd/aist-bench-stand` at `cfaaa50`)

- A VM declared with Ansible (Docker, runner, API, worker, Caddy with a Let's Encrypt IP certificate, basic auth). `make deploy` rebuilds it; a second apply changes nothing.
- Declarations: `mas/t11.yaml` (T11 at `66bea70`, digest-pinned image, provider config overlay) and `benchmarks/drift.yaml` (the ST03 drift grid), linked by a `family`.
- A SQLite queue and index, a worker that runs one container at a time, results under `/srv/bench/results/<run>/` with a manifest, a T11 trace adapter, a UI with progress, pass per condition, a task x condition matrix and a trace viewer.
- Entry points: UI, REST API, `bench` workflow in the tool repository.
- Checked: fake runs through CLI and Actions; a free-tier `glm-4.5-flash` run queued from Actions; the earlier `glm45` grid imported and shown with the known 25 failures under `v2_units:A0`.

## 3. Split: an open tool and the lab's configuration

The engine knows nothing about this lab. It goes into its own public repository under Apache-2.0 as an on-prem tool (working name `mas-stand`): queue, executor, composition, adapters, analyzers runner, API, UI, Ansible role. The lab's stand is an instance of it.

What lives where:

| Content | Repository |
| --- | --- |
| Engine, UI, Ansible role, generic adapters | `mas-stand` (public tool) |
| The chosen MAS and its component map, benchmarks, analyzers, provider config | `barakhsin/mas-lifecycle`, directory `stand/` |
| The lab instance: inventory, URL, auth settings | `mas-lifecycle/stand/deploy/` or a small private ops repo |

The stand reads its configuration from `mas-lifecycle` at the commit under test, so a pull request that changes a component, a benchmark parameter or an analyzer is tested with exactly that change.

## 4. One MAS, replaceable by component

The team picks one MAS (section 9). Its declaration names components and the files that implement each:

```yaml
mas:
  id: drift-mas
  base: {repo: 7SUNNY77/T11, ref: 66bea708a39fbe7caec789bdcf19b984c19ade2b}
  image: python:3.11-slim@sha256:...
  components:
    planner:  {paths: [mas_drift/agents/planner.py]}
    executor: {paths: [mas_drift/agents/executor.py]}
    verifier: {paths: [mas_drift/agents/verifier.py]}
    prompts:  {paths: [mas_drift/agents/prompts.py]}
    tools:    {paths: [mas_drift/tools/]}
    model:    {config: providers}
```

A run may override any component with a source: another commit of the base repository, a fork, or a path in `mas-lifecycle` (for example `stand/components/executor_retry.py`). The executor builds the checkout as base plus overlays, records the source and sha256 of every overlaid path in the manifest, and the UI shows which components differ from the base. Two runs that differ in one component are a paired comparison; the ST02 analysis already works on such pairs.

Limits to accept or fix:

- Overlays work while the component interfaces stay put (`run_planner(ctx, request, changelog)` and so on). A run whose overlay breaks an interface fails at import, which the log shows.
- T11 uses one model for all agents. A model per agent needs a small change in T11 (a `[agents.<name>] provider = ...` table); proposed to the ST03 owner, not done here.

## 5. Benchmarks and analyzers

A benchmark is the task set and how its outcomes are read (`drift` today). An analyzer is a lifecycle component that runs after the benchmark on the stored outputs and writes a JSON report next to them, shown in the UI. Analyzers come from `mas-lifecycle` at the commit under test:

```yaml
analyzers:
  change-impact:            # ST02
    run: python examples/change/drift_stand_benchmark.py --stand {mas_checkout} --traces {outputs}/traces --label {run_id} --out {report}
  trace-evidence:           # ST05
    run: ...
```

So "our agent on our benchmark" is: the chosen MAS, the drift benchmark, and the ST02/ST05 analyzers on each run. Other students add their own analyzers and benchmarks the same way.

## 6. Runs from branches and pull requests

In `mas-lifecycle`, one workflow `.github/workflows/stand.yml`:

- `workflow_dispatch` on any branch: inputs `benchmark`, `params`, `components` (for example `executor=stand/components/executor_retry.py`). It submits the run for the branch head commit.
- `issue_comment` on a pull request: a comment `/stand drift provider=glm45 n_tasks=10` from a user with write access (author association `OWNER`, `MEMBER` or `COLLABORATOR`) submits a run for the pull request head. The workflow replies with the run link at once and, when the run ends, edits the reply with the pass table and analyzer verdicts. A commit status `stand/drift` on the head SHA links the run.
- Optional baseline: `/stand drift --compare main` queues the same configuration for the base commit, and the reply shows the per-task differences (the ST02 flip definition).

The workflow runs on GitHub-hosted runners and only calls the stand API with a token stored as a repository secret (`STAND_TOKEN`), so the self-hosted runner never executes pull-request code from GitHub's side. The stand executes it, in a container, for collaborators only.

Needs from the maintainer: merge the workflow and add `STAND_URL` and `STAND_TOKEN` secrets.

## 7. Access

- People: GitHub login in front of the UI (oauth2-proxy), allowed when the user is a collaborator of `barakhsin/mas-lifecycle`. Losing repository access removes stand access. Needs a GitHub OAuth app (client ID and secret) registered by the repository owner or by the operator.
- Automation: per-repository API tokens (the `STAND_TOKEN` above), revocable on the stand.
- Basic auth remains only as the operator's fallback.
- Pull-request code sees the API keys of the providers it calls. That is acceptable for collaborators and is the reason the trigger is limited to them.

## 8. Order of work

1. Composition by component and source recording in the manifest; analyzers step; configuration read from a repository at a commit.
2. `stand/` directory and `stand.yml` workflow in `mas-lifecycle` (pull request to Barakhsin), with T11 as the interim MAS and the ST02 analyzer.
3. API tokens; GitHub login once the OAuth app exists.
4. Split out the public `mas-stand` repository and move the lab instance configuration.
5. Swap the interim MAS for the team's choice if it differs.

## 9. Decisions for the team

1. Which MAS. Candidates: T11 (runs today, used by ST02-ST05, small, one model for all agents) and FEDOT.MAS (`docs/interfaces.md` proposes its `MAWConfig` as the adaptation target; not yet runnable on the stand). Until the team decides, the stand uses T11.
2. Who registers the GitHub OAuth app and holds its secret.
3. Which provider keys the stand may use, and a monthly budget if a paid model is ever added. Today only free-tier keys are present.
4. Name and licence of the public tool (proposal: `mas-stand`, Apache-2.0).
