import pytest

from mac.models import NotFoundError, ValidationError
from mac.roles_service import machine_hardware_satisfies
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _register_machine(cp, hostname="host-1", hardware=None):
    return cp.register_machine(hostname, hardware=hardware or {})


def test_create_and_get_role_resolves_by_id_or_slug(cp):
    role = cp.roles.create_role(
        slug="code-reviewer",
        name="Code Reviewer",
        description="Reviews code for correctness, security, fit.",
        system_prompt="You are the second pair of eyes.",
        level="ic",
        default_capabilities=["review", "python"],
    )
    assert cp.roles.get_role(role.id).id == role.id
    assert cp.roles.get_role("code-reviewer").id == role.id


def test_create_role_validates_required_fields_and_level(cp):
    with pytest.raises(ValidationError):
        cp.roles.create_role(
            slug="bad slug", name="x", description="x", system_prompt="x", level="ic"
        )
    with pytest.raises(ValidationError):
        cp.roles.create_role(
            slug="ok", name="", description="x", system_prompt="x", level="ic"
        )
    with pytest.raises(ValidationError):
        cp.roles.create_role(
            slug="ok", name="x", description="x", system_prompt="x", level="emperor"
        )


def test_update_role_preserves_unmentioned_fields(cp):
    role = cp.roles.create_role(
        slug="qa",
        name="QA Engineer",
        description="Tests stuff.",
        system_prompt="Find bugs.",
        level="ic",
        specialties=["e2e", "regression"],
    )
    updated = cp.roles.update_role(role.id, description="Tests everything.")
    assert updated.description == "Tests everything."
    assert updated.specialties == ["e2e", "regression"]
    assert updated.system_prompt == "Find bugs."


def test_delete_role_refuses_when_agent_assigned(cp):
    role = cp.roles.create_role(
        slug="ops",
        name="Ops",
        description="d",
        system_prompt="p",
        level="ic",
        default_capabilities=["ops"],
    )
    machine = _register_machine(cp)
    agent = cp.register_agent(machine.id, "worker")
    cp.roles.assign_role(agent.id, role.id)
    with pytest.raises(ValidationError):
        cp.roles.delete_role(role.id)
    # Unassign clears the reference and delete now succeeds.
    cp.roles.unassign_role(agent.id)
    cp.roles.delete_role(role.id)
    with pytest.raises(NotFoundError):
        cp.roles.get_role(role.id)


def test_assign_role_merges_default_capabilities_into_agent(cp):
    role = cp.roles.create_role(
        slug="devops",
        name="DevOps",
        description="d",
        system_prompt="p",
        level="ic",
        default_capabilities=["ops", "ci"],
    )
    machine = _register_machine(cp)
    agent = cp.register_agent(machine.id, "rocky", capabilities=["python"])
    refreshed = cp.roles.assign_role(agent.id, "devops")
    assert refreshed.role_id == role.id
    assert set(refreshed.capabilities) == {"python", "ops", "ci"}


def test_assign_role_rejects_when_hardware_mismatch(cp):
    role = cp.roles.create_role(
        slug="gpu-runner",
        name="GPU Runner",
        description="d",
        system_prompt="p",
        level="ic",
        hardware_requirements={"cpu_arch": ["arm64"], "memory_gb_min": 32},
    )
    cpu_only = _register_machine(
        cp,
        hostname="cpu-only",
        hardware={"cpu_arch": "x86_64", "memory_gb": 16},
    )
    agent = cp.register_agent(cpu_only.id, "wrong-host")
    with pytest.raises(ValidationError) as exc:
        cp.roles.assign_role(agent.id, role.id)
    assert "cpu_arch" in str(exc.value) or "memory_gb" in str(exc.value)

    fit = _register_machine(
        cp,
        hostname="arm-big",
        hardware={"cpu_arch": "arm64", "memory_gb": 64},
    )
    agent2 = cp.register_agent(fit.id, "right-host")
    cp.roles.assign_role(agent2.id, role.id)  # no error


def test_machine_hardware_satisfies_handles_accelerators_and_tags():
    req = {
        "accelerators": [
            {"kind": "gpu", "vendor": "nvidia", "memory_gb_min": 40, "count_min": 1}
        ],
        "tags_all": ["dgx"],
    }
    ok, _ = machine_hardware_satisfies(
        req,
        {
            "tags": ["dgx", "internal"],
            "accelerators": [
                {"kind": "gpu", "vendor": "nvidia", "memory_gb": 80, "count": 8}
            ],
        },
    )
    assert ok

    bad_ok, reasons = machine_hardware_satisfies(
        req,
        {"tags": ["internal"], "accelerators": [{"kind": "gpu", "vendor": "amd"}]},
    )
    assert not bad_ok
    assert any("tags" in r for r in reasons) or any("accelerator" in r for r in reasons)


def test_seed_defaults_loads_thirteen_loom_roles_idempotent(cp):
    rows = cp.roles.seed_defaults()
    slugs = sorted(role.slug for role in rows)
    # CEO/CFO/CTO removed; 13 of loom's 16 personas remain.
    assert "ceo" not in slugs
    assert "code-reviewer" in slugs
    assert len(slugs) == 13

    # Idempotent: a second call doesn't duplicate, and replace=False
    # leaves existing rows alone.
    rows_again = cp.roles.seed_defaults()
    assert {r.slug for r in rows_again} == set(slugs)

    listed = cp.roles.list_roles(include_defaults=True)
    assert len(listed) == 13


def test_tenant_scoped_role_shadows_global_default(cp):
    tenant = cp.register_tenant("alpha")
    cp.roles.create_role(
        slug="reviewer",
        name="Global Reviewer",
        description="d",
        system_prompt="global",
        level="ic",
    )
    cp.roles.create_role(
        slug="reviewer",
        name="Alpha Reviewer",
        description="d",
        system_prompt="alpha-specific",
        level="ic",
        tenant_id=tenant.id,
    )
    # Tenant-scoped lookup prefers the tenant copy.
    by_tenant = cp.roles.get_role("reviewer", tenant_id=tenant.id)
    assert by_tenant.system_prompt == "alpha-specific"
    # Without a tenant, fall back to the global.
    assert cp.roles.get_role("reviewer").system_prompt == "global"
