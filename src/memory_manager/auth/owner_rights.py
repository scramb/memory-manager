# SPDX-License-Identifier: AGPL-3.0-only
"""What a personal token's owner may do *right now* (ADR-0012, #134).

ADR-0012 option A: "a personal token's scopes and namespaces must be a
subset of what its owner may do. That is checked at creation **and** on
every request, because the owner's rights can shrink after creation." The
creation-time half lives where the owner is known without a resolver at all
- `cli.py`'s `_run_token_create` (`"git"`) and
`auth.tokens.create_token(..., enterprise=True)` (`"postgres"`, ADR-0006
§7's existing owner-in-`users` check). This module is the *request-time*
half: `auth.verifier._verify_static_token` calls `OwnerRightsResolver.
resolve` for every single verification of a `kind="personal"` token, never
for a `"service"`/`"agent"` one.

Deliberately narrow, per ADR-0006 addendum 2026-10-08: a removed Entra app
role only takes effect at the next login (reading `appRoleAssignments` live
would need `Directory.Read.All`, rejected there) - `roles` stays exactly
what `create_token` wrote, for the lifetime of the token, in both backends.
What *is* read live:

- `"git"`: the owner's namespaces, resolved exactly the way a fresh login
  would be (`auth.login.resolve_namespaces` against the same
  `LOGIN_NAMESPACE_MAP`/`LOGIN_NAMESPACES` `login_password.py`/
  `login_oidc.py` already read) - this backend has no user table to disable
  at all, so `allowed` is always `True` here (OIDC subject/email allowlist
  revocation is out of scope, #134's "not included").
- `"postgres"`: `allowed` is `False` the moment the owner's `users` row is
  gone or `disabled_at` is set (`auth.users.get_user`) - the same check
  `auth.verifier._verify_oauth_access_token` already makes for an
  owner-bound OAuth access token. Live group ids (`auth.users.group_ids`)
  travel back for the verifier to put into `claims["groups"]`, same as that
  OAuth path. `namespaces` here is always `(ALL_NAMESPACES,)`: the actual
  namespace narrowing for this backend happens inside Postgres itself,
  through row-level security reading `app.oid` live on every query
  (`db/rls.py`, `migrations/0005_rls.sql`'s `mm_readable_ns`/
  `mm_writable_ns` - group membership and namespace/project settings
  included), not through this claim at all.

`scopes` has no field here on purpose: nothing in either backend restricts
which of `READ_SCOPE`/`WRITE_SCOPE` an owner may use (there is no
per-principal scope configuration anywhere in this codebase today), so "the
owner's current scopes" is always every scope - intersecting a token's own
`scopes` against that is a no-op. ADR-0012's "subset of what its owner may
do" is still true, just never observable as a narrowing while that stays
the case.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import asyncpg

from memory_manager.auth import login, users
from memory_manager.auth.tokens import ALL_NAMESPACES

__all__ = ["OwnerRights", "OwnerRightsResolver", "intersect_namespaces"]


def intersect_namespaces(a: Sequence[str], b: Sequence[str]) -> tuple[str, ...] | None:
    """The namespaces allowed by both `a` and `b`.

    `ALL_NAMESPACES` ("*") on either side means "no opinion, every namespace" -
    the same literal `auth.tokens`/`mcp.authz` already use - so it never
    narrows the other side. `None` if nothing is left in common: never an
    empty tuple, which `mcp.authz._token_namespaces` reads as *unrestricted*,
    the exact opposite of what an empty intersection means here.
    """
    a_all = ALL_NAMESPACES in a
    b_all = ALL_NAMESPACES in b
    if a_all and b_all:
        return (ALL_NAMESPACES,)
    if a_all:
        return tuple(b) if b else None
    if b_all:
        return tuple(a) if a else None
    narrowed = tuple(sorted(set(a) & set(b)))
    return narrowed or None


@dataclass(frozen=True)
class OwnerRights:
    """What `owner_oid` may do right now.

    `allowed=False` means the owner itself has gone away (no `users` row, or
    disabled) - the caller must reject the whole token, not narrow it.
    `namespaces` is the universe the owner may currently use, `()` only when
    `allowed` is `False` (meaningless otherwise). `groups` is `None` for
    `"git"` (no group concept at all) and a live tuple (possibly empty) for
    `"postgres"` - `auth.verifier` sets `claims["groups"]` only when this is
    not `None`, the same "omit the claim this backend has no concept of"
    rule a legacy/service token's claims already follow.
    """

    allowed: bool
    namespaces: tuple[str, ...] = (ALL_NAMESPACES,)
    groups: tuple[str, ...] | None = None


@dataclass(frozen=True)
class OwnerRightsResolver:
    """Built once per process (`app.open_services`) and reused for every
    `kind="personal"` token verification - never rebuilt per request.

    `pool` is `None` for `"git"` (unused there) and the backend's own pool
    for `"postgres"`; `namespace_map`/`default_namespaces` are `"git"`'s own
    `LOGIN_NAMESPACE_MAP`/`LOGIN_NAMESPACES`, parsed once, eagerly, by
    `open_services` (`auth.login.parse_namespace_map`/`parse_namespaces` -
    the same "a malformed value fails at startup" contract every other
    `*_from_env` call in that module already follows).
    """

    backend: str
    pool: asyncpg.Pool | None = None
    namespace_map: Mapping[str, Sequence[str]] = field(default_factory=dict)
    default_namespaces: Sequence[str] = (ALL_NAMESPACES,)

    async def resolve(self, owner_oid: str) -> OwnerRights:
        """`owner_oid`'s current `OwnerRights` - one Postgres round trip for
        `"postgres"` (`users.get_user` plus, only if the owner is still
        active, `users.group_ids`), none at all for `"git"`."""
        if self.backend != "postgres":
            namespaces = tuple(
                login.resolve_namespaces(
                    [owner_oid],
                    namespace_map=self.namespace_map,
                    default=self.default_namespaces,
                )
            )
            return OwnerRights(allowed=True, namespaces=namespaces)

        if self.pool is None:  # pragma: no cover - app.py always provides one for "postgres"
            raise AssertionError("OwnerRightsResolver('postgres', ...) built with no pool")
        owner = await users.get_user(self.pool, owner_oid)
        if owner is None or owner.disabled_at is not None:
            return OwnerRights(allowed=False, namespaces=())
        groups = await users.group_ids(self.pool, owner_oid)
        return OwnerRights(allowed=True, namespaces=(ALL_NAMESPACES,), groups=groups)
