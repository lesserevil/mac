# Getting Started

This is the shortest path for a fresh host that should become the first MAC
hub. It creates a local fleet registry under `~/.mac/fleets.yaml`, writes deploy
secrets to `~/.mac/.env`, validates the SSH target, and deploys the named hub.

```bash
bash deploy/deploy-mac-fleet.sh \
  --new-hub horde \
  --target horde@20.115.163.162:2201
```

Use `--ssh-port 2201` instead of an inline `:2201` when the target is an SSH
alias or otherwise contains a colon.

```bash
bash deploy/deploy-mac-fleet.sh \
  --new-hub horde \
  --target horde@20.115.163.162 \
  --ssh-port 2201
```

The generated registry is home-scoped, not checked in. Re-run deployment with:

```bash
set -a
. ~/.mac/.env
set +a
bash deploy/deploy-mac-fleet.sh --hub horde
```

The deploy path treats obsolete ACC-derived build, deploy, log, cache, and old
Hermes replacement artifacts as disposable. MAC state lives under `~/.mac`;
Hermes state lives under `~/.hermes`.
