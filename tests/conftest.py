"""Shared pytest fixtures and helpers for the MAC test suite."""

from __future__ import annotations

from typing import Iterable, Optional

from mac.services import ControlPlane


def bind_soul(
    cp: ControlPlane,
    *,
    persona_name: str = "Test Persona",
    allowed_role_slugs: Optional[Iterable[str]] = None,
    tenant_name: str = "test-tenant",
    instance_name: Optional[str] = None,
) -> str:
    """Create a tenant + persona + hermes instance and return the
    instance id.

    ``allowed_role_slugs`` controls the persona's metadata.role_slugs
    list — pass the slugs the soul should accept. If omitted, the
    persona's name (slugified) becomes the only allowed role (the loom
    default).

    Tests that need to assign a role to an agent should bind a soul
    first via this helper; agents without a soul refuse all role
    assignments by design.
    """
    tenant = cp.register_tenant(tenant_name)
    metadata = None
    if allowed_role_slugs is not None:
        metadata = {"role_slugs": [str(s) for s in allowed_role_slugs]}
    persona = cp.register_persona(
        tenant.id,
        persona_name,
        "hermes://%s/%s/SOUL.md" % (tenant_name, persona_name.lower()),
        "hermes://%s/%s/memory" % (tenant_name, persona_name.lower()),
        metadata=metadata,
    )
    instance = cp.register_hermes_instance(
        tenant.id,
        instance_name or "instance-%s" % persona_name.lower().replace(" ", "-"),
        persona_id=persona.id,
    )
    return instance.id
