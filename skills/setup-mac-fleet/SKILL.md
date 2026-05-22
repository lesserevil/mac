---
name: setup-mac-fleet
description: Use when a user asks to set up, bootstrap, deploy, or configure a new mac agent fleet. Runs the first-time setup wizard, writes a home-scoped multi-fleet registry, and keeps fleet-specific data out of Git.
---

# Setup Mac Fleet

Use this skill when the user asks to set up or deploy a new mac fleet and
`~/.mac/fleets.yaml` or `~/.mac/.env` is missing.

## Rules

- Do not invent agent names, hostnames, IP addresses, Slack channel names, or
  model selectors.
- Do not commit fleet topology or secrets. Fleet topology belongs in
  `~/.mac/fleets.yaml`; local deploy secrets belong in `~/.mac/.env`.
- Do not put upstream provider API keys in mac config. Those belong in
  TokenHub or the site's secret store.
- Keep committed fleet examples generic. Personal fleets must live only in the
  home-scoped fleet registry.

## Workflow

1. Run the wizard:

   ```bash
   bash setup.sh
   ```

2. If the user wants a non-default path, pass explicit paths:

   ```bash
   bash setup.sh --fleets-config ~/.mac/fleets.yaml --env-file ~/.mac/.env
   ```

3. The wizard asks for fleet topology, hub, supervisor, Slack home channel,
   per-agent Hermes models, worker mode, canary policy, shared Qdrant readiness,
   fleet network provider, and optional deploy hub token. Keep Tailscale as the
   default; use Headscale only when the user supplies an explicit login server,
   enrollment-key source, DNS assumption, and health check.

4. Source the generated caller-machine env, then deploy:

   ```bash
   set -a; . ~/.mac/.env; set +a
   bash deploy/deploy-mac-fleet.sh --hub <hub-node>
   ```

5. If asked to inspect or edit the fleet later, edit
   `~/.mac/fleets.yaml`, not `deploy/fleet/config.yaml`.

## Validation

Before deploy, run:

```bash
bash -n deploy/deploy-mac-fleet.sh
bash -n deploy/install-qdrant-service.sh
bash -n deploy/install-tailscale.sh
bash -n deploy/install-headscale.sh
uv run pytest tests/test_deploy_agent_configs.py tests/test_hermes_startup.py
```
