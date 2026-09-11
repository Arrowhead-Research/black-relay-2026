COMPOSE := docker compose -f dev/compose.yml
SAFE_ANSIBLE_INVENTORY := ansible/inventory/production/hosts.yml

.PHONY: help
help:
	@printf '%s\n' \
		'Hetzner identity stack development environment' \
		'' \
		'  make dev-build    Build the Pi development image' \
		'  make dev-up       Start Pi Web on http://127.0.0.1:8504' \
		'  make dev-down     Stop Pi Web and retain its state volumes' \
		'  make dev-restart  Recreate the Pi Web container' \
		'  make dev-logs     Follow Pi Web logs' \
		'  make dev-status   Show container status and access URL' \
		'  make dev-shell    Open a shell in the running container' \
		'  make dev-doctor   Verify tools and credential isolation' \
		'  make lint         Run formatting, YAML, shell, JSON, and Ansible linting' \
		'  make validate     Validate Compose, Packer, OpenTofu, Ansible, and tests'

.PHONY: dev-build
dev-build:
	$(COMPOSE) build --pull

.PHONY: dev-up
dev-up:
	$(COMPOSE) up -d

.PHONY: dev-down
dev-down:
	$(COMPOSE) down

.PHONY: dev-restart
dev-restart:
	$(COMPOSE) up -d --force-recreate

.PHONY: dev-logs
dev-logs:
	$(COMPOSE) logs -f pi

.PHONY: dev-status
dev-status:
	$(COMPOSE) ps
	@printf '\nPi Web: http://127.0.0.1:8504\n'

.PHONY: dev-shell
dev-shell:
	$(COMPOSE) exec pi bash

.PHONY: dev-doctor
dev-doctor:
	$(COMPOSE) exec -T pi dev-doctor

.PHONY: lint
lint:
	packer fmt -check -recursive packer
	tofu fmt -check -recursive tofu
	yamllint -c .yamllint.yml .
	shellcheck dev/*.sh packer/scripts/*.sh scripts/operator/*.sh
	@for script in dev/*.sh packer/scripts/*.sh scripts/operator/*.sh; do bash -n "$$script"; done
	python3 -m json.tool dev/pi-web-config.json >/dev/null
	python3 -m json.tool renovate.json >/dev/null
	ANSIBLE_INVENTORY=$(SAFE_ANSIBLE_INVENTORY) ansible-lint ansible

.PHONY: validate
validate:
	docker compose -f dev/compose.yml config --quiet
	packer init packer
	packer inspect packer >/dev/null
	@tofu_data_dir="$$(mktemp -d)"; \
	trap 'rm -rf "$$tofu_data_dir"' EXIT; \
	TF_DATA_DIR="$$tofu_data_dir" tofu -chdir=tofu init -backend=false -input=false -lockfile=readonly; \
	TF_DATA_DIR="$$tofu_data_dir" tofu -chdir=tofu validate
	ANSIBLE_INVENTORY=$(SAFE_ANSIBLE_INVENTORY) ansible-playbook --syntax-check ansible/playbooks/site.yml
	python3 -m unittest discover -s tests
