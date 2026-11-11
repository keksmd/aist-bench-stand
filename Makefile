INVENTORY ?= inventory/stand.yml
RUNNER_REPO ?= keksmd/aist-bench-stand

.PHONY: check test deploy bench

test:
	.venv/bin/python -m stand validate
	.venv/bin/python -m unittest discover -s tests -t .

check:
	ansible-playbook -i $(INVENTORY) ansible/site.yml --check --diff -e runner_token=dry-run

deploy:
	git diff --quiet HEAD
	git push -q
	ansible-playbook -i $(INVENTORY) ansible/site.yml -e stand_version=$$(git rev-parse HEAD) \
	  -e runner_token=$$(gh api -X POST repos/$(RUNNER_REPO)/actions/runners/registration-token -q .token)

bench:
	gh workflow run bench.yml -R $(RUNNER_REPO) -f bench=$(or $(B),drift) -f mas=$(or $(M),t11) -f params="$(P)" -f ref="$(REF)"
