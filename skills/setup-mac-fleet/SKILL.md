---
name: setup-mac-fleet
description: Use when a user asks to set up, bootstrap, deploy, or configure a new mac agent fleet. Runs the first-time setup wizard, writes site-local fleet config, and keeps fleet-specific data out of Git.
---

# Setup Mac Fleet

Use this skill when the user asks to set up or deploy a new mac fleet and
`deploy/fleet/config-site.yaml` or `~/.mac/.env` is missing.

## Rules

- Do not invent agent names, hostnames, IP addresses, Slack channel names, or
  model selectors.
- Do not commit `deploy/fleet/config-site.yaml` or `~/.mac/.env`.
- Do not put upstream provider API keys in mac config. Those belong in
  TokenHub or the site's secret store.
- Keep committed fleet examples generic. Personal fleets must live only in the
  ignored site config.

## Workflow

1. Run the wizard:

   ```bash
   bash setup.sh
   ```

2. If the user wants a non-default path, pass explicit paths:

   ```bash
   bash setup.sh --site-config /path/to/config-site.yaml --env-file ~/.mac/.env
   ```

3. The wizard asks for fleet topology, hub, supervisor, Slack home channel,
   per-agent Hermes models, worker mode, canary policy, shared Qdrant readiness,
   and optional deploy hub token.

4. Source the generated caller-machine env, then deploy:

   ```bash
   set -a; . ~/.mac/.env; set +a
   bash deploy/deploy-mac-fleet.sh
   ```

5. If asked to inspect or edit the fleet later, edit
   `deploy/fleet/config-site.yaml`, not `deploy/fleet/config.yaml`.

## Validation

Before deploy, run:

```bash
bash -n deploy/deploy-mac-fleet.sh
bash -n deploy/install-qdrant-service.sh
uv run pytest tests/test_deploy_agent_configs.py tests/test_hermes_startup.py
```
