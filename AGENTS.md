# Agent instructions: aist-bench-stand

- The machine is disposable. Every change to it goes through `ansible/` or `benchmarks/`; never edit the VM by hand.
- Before pushing: `make test`; for playbook changes also `make check` against the stand.
- Never commit secrets. API keys are GitHub Actions secrets; the SSH key for the stand lives only on the operator's machine.
- A benchmark `ref` is a full commit SHA and an `image` is pinned by digest, so a run can be reproduced.
- Results are data from the runs: keep the manifest with every reported number.
