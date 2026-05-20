# Repository Runtime Contract

Any repository registered with mac must declare how an agent prepares and
verifies that repository on a fresh host. The contract is intentionally
project-owned: mac should not guess that every worker has the same shell state,
package manager, Linux distribution, macOS setup, or WSL2 image.

The contract file lives at `.mac/project.yaml` in the repository root.

```yaml
schema: mac.repository_contract.v1
project: repo-beads-mac
platforms:
  - darwin
  - linux
  - wsl2
toolchain:
  required_commands:
    - python3
    - git
bootstrap:
  command: python3 scripts/bootstrap-project.py
  creates:
    - .venv/bin/python
test:
  command: PATH=.venv/bin:$PATH .venv/bin/python -m pytest
evidence:
  required:
    - repo.head_sha
    - repo.pushed
    - repo.dirty
    - repo.files_changed
    - tests
```

## Required Fields

- `schema`: must be `mac.repository_contract.v1`.
- `project`: must match the mac project name used when the repository is
  registered.
- `platforms`: explicit supported host families. Use broad families such as
  `darwin`, `linux`, and `wsl2`; document narrower distro assumptions inside
  the bootstrap script instead of assuming Ubuntu.
- `toolchain.required_commands`: commands that must exist before bootstrap can
  run. Keep this list small and portable.
- `bootstrap.command`: an idempotent command run from the repository root to
  create the local build/test environment.
- `bootstrap.creates`: relative paths expected after bootstrap. These are used
  as a quick signal that a host has already been prepared.
- `test.command`: the canonical verification command for default task work.
- `evidence.required`: manifest fields a worker must include before mac can
  consider repo work publishable.

## Enforcement

The Beads bridge validates this file during repository registration and again
before every poll. Registration fails if the contract is missing, malformed, or
names a different `project` than the registered mac project.

Imported tasks carry the normalized contract in both the project item payload
and `task.metadata.origin.repository_contract`. The Hermes executor prompt
surfaces the contract and tells workers to bootstrap from the local checkout
before running the declared test command.

## mac As First Adopter

mac declares its own contract in `.mac/project.yaml`. Its bootstrap command is:

```bash
python3 scripts/bootstrap-project.py
```

That script creates `.venv` and installs the dev extra so a fresh macOS, Linux,
or WSL2 agent can run:

```bash
PATH=.venv/bin:$PATH .venv/bin/python -m pytest
```
