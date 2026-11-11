INVENTORY ?= inventory/stand.yml
RUNNER_REPO ?= keksmd/aist-bench-stand

.PHONY: check test deploy bench

test:
	python3 harness/bench.py validate
	python3 -m unittest discover -s harness -p 'test_*.py'

check:
	ansible-playbook -i $(INVENTORY) ansible/site.yml --check --diff -e runner_token=dry-run

deploy:
	ansible-playbook -i $(INVENTORY) ansible/site.yml \
	  -e runner_token=$$(gh api -X POST repos/$(RUNNER_REPO)/actions/runners/registration-token -q .token)

bench:
	gh workflow run bench.yml -R $(RUNNER_REPO) -f bench=$(B) -f params="$(P)" -f ref="$(REF)"
