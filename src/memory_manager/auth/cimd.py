# SPDX-License-Identifier: AGPL-3.0-only
"""Client ID Metadata Documents (CIMD, SEP-991) for OAuth client registration (#38,
ADR-0004).

docs/research/mcp-auth-and-connectors.md §3: a CIMD `client_id` is an HTTPS URL with a
path; the authorization server fetches the document it points at, checks that the
document's own `client_id` equals that URL, and validates `redirect_uri` against the
document's `redirect_uris`. §4: claude.ai only tries CIMD when the AS advertises both
`client_id_metadata_document_supported: true` and `"none"` in
`token_endpoint_auth_methods_supported` (`auth.metadata` adds both); Claude Code
registers loopback redirect URIs through its own CIMD. Neither ever presents a client
secret - a CIMD client is always public (`"none"`), regardless of what the document
itself claims, which is why this module never reads `token_endpoint_auth_method` from
it as anything other than a validation check.

Fetching a client-supplied URL at all is an SSRF surface (the issue this task starts
from), so `ClientMetadataFetcher.fetch_client_metadata` applies, in order: HTTPS-only,
a non-empty path required, DNS resolution through the injectable `Resolver` with every
resolved address (IPv4 and IPv6) checked **fail-closed** by `_reject_unsafe_address` -
an address must be `ip.is_global` (IPv4-mapped IPv6 unwrapped to its embedded IPv4
first, since the explicit checks after it do not understand that wrapper on their own)
to be allowed at all, which is itself already everything RFC 1918/loopback/link-
local/multicast/reserved - the earlier allow-everything-unless-flagged shape of this
check is exactly what missed `100.64.0.0/10` (CGNAT: on this stdlib, flagged by
neither `is_private` nor `is_reserved`, only by `is_global` being `False`, a documented
exception - see `IPv4Address.is_global`'s own docstring). The explicit
`is_private`/`is_loopback`/`is_link_local`/`is_multicast`/`is_reserved`/`is_unspecified`
checks, and the explicit `100.64.0.0/10`/`192.0.0.0/24`/`198.18.0.0/15`/`2001:db8::/32`
network list, still run afterwards anyway, as defence in depth against a future stdlib
`is_global` change alone silently reopening one of these - and NAT64 (`64:ff9b::/96`,
RFC 6052) gets its own recursive check of the embedded IPv4 address, because
`is_global` does not look inside that wrapper at all (`64:ff9b::7f00:1` - which embeds
`127.0.0.1` - is itself `is_global`). A request is then pinned to the first address
that passed every one of those checks (`Host`/SNI still carry the original hostname,
so TLS certificate validation and virtual-hosting both still see the right name)
rather than letting the HTTP client re-resolve DNS a second time between the check and
the connection, no redirects followed (a redirect response is simply rejected, the
same as any other non-200), a 5-second total timeout, a 64 KiB response-body cap
enforced while streaming (not after buffering the whole body), and `Content-Type` must
be JSON. Successful and failed fetches are both cached by URL (`ClientMetadataFetcher.
fetch` on a cache hit never touches the network at all) - successes for
`Cache-Control: max-age` clamped to `[_MIN_CACHE_SECONDS, _MAX_CACHE_SECONDS]`
(defaulting to the minimum when the header is absent or unparseable), failures for
`_NEGATIVE_CACHE_SECONDS`, so a client repeatedly hitting `/authorize` with a broken or
slow CIMD URL cannot turn every one of those into a fresh outbound request.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

__all__ = [
    "CimdError",
    "ClientMetadataDocument",
    "ClientMetadataFetcher",
    "Resolver",
    "default_resolver",
]

#: A CIMD document is capped well below any realistic `client_name`/`redirect_uris`
#: payload - generous for the legitimate case, small enough that a malicious server
#: streaming an unbounded body is cut off quickly.
_MAX_DOCUMENT_BYTES = 64 * 1024

_REQUEST_TIMEOUT_SECONDS = 5.0

#: Cache-Control `max-age` is honoured only inside this range - a CIMD document's
#: `redirect_uris` rarely change, but a value outside this window would either hammer
#: the client's server on every `/authorize` or make a rotated document take a full day
#: to take effect.
_MIN_CACHE_SECONDS = 5 * 60
_MAX_CACHE_SECONDS = 24 * 60 * 60

#: How long a failed fetch (unreachable, invalid, SSRF-rejected, ...) is remembered
#: before the next `/authorize` for the same `client_id` is allowed to try again.
_NEGATIVE_CACHE_SECONDS = 60


class CimdError(Exception):
    """`client_id` is not usable as a CIMD client: not fetchable as specified, or its
    document fails SEP-991 validation. `auth.provider.get_client` catches this and
    reports the client as simply not found - a client_id that merely looks like a CIMD
    URL is otherwise indistinguishable from one that is deliberately malformed or
    hostile, and both get the same "unknown client" treatment DCR already gives an
    unregistered `client_id`."""


@dataclass(frozen=True)
class ClientMetadataDocument:
    """The CIMD fields `auth.provider` actually uses. `token_endpoint_auth_method` is
    deliberately not carried here at all: whatever the document claims, a CIMD client
    is always public (docs/research/mcp-auth-and-connectors.md §3: "no secrets for CIMD
    clients") - `fetch_client_metadata` already rejects a document that asks for
    anything else, so by the time one of these exists the point is moot anyway."""

    client_id: str
    client_name: str | None
    redirect_uris: tuple[str, ...]


#: `host -> every resolved address` (plain, synchronous - run through `asyncio.to_thread`
#: by `ClientMetadataFetcher`), injectable so a test can hand back a fixed address
#: (public or deliberately private) without touching real DNS.
Resolver = Callable[[str], Sequence[str]]


def default_resolver(host: str) -> Sequence[str]:
    """`socket.getaddrinfo(host, None)`, deduplicated - every address this host
    currently resolves to, IPv4 and IPv6 alike."""
    infos = socket.getaddrinfo(host, None)
    addresses = {str(info[4][0]) for info in infos}
    return tuple(addresses)


@dataclass
class _CacheEntry:
    expires_at: float
    document: ClientMetadataDocument | None
    error: str | None


class ClientMetadataFetcher:
    """Fetches, validates and caches CIMD documents by URL (see this module's
    docstring for the SSRF guards and caching rule). One instance is shared for the
    lifetime of the OAuth authorization server (`http.py` builds it once and hands it
    to `MemoryManagerOAuthProvider`), so its cache actually does something across the
    several `get_client` calls one `/authorize` -> `/login` -> `/token` round trip makes.

    `transport`/`resolver` are the test seam: a test builds one with
    `httpx.MockTransport` and a `Resolver` that returns a fixed address, instead of
    `default_resolver` touching real DNS and `transport=None` opening a real socket.
    """

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = default_resolver,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._resolver = resolver
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}

    async def fetch_client_metadata(self, url: str) -> ClientMetadataDocument:
        """`url`'s CIMD document, from cache if still fresh. Raises `CimdError` if
        `url` cannot be fetched or its document is invalid - including a still-fresh
        negative cache entry from an earlier failed attempt."""
        now = self._clock()
        cached = self._cache.get(url)
        if cached is not None and cached.expires_at > now:
            if cached.document is None:
                raise CimdError(cached.error or f"{url!r} is cached as unfetchable")
            return cached.document

        try:
            document, ttl_seconds = await self._fetch_uncached(url)
        except CimdError as exc:
            self._cache[url] = _CacheEntry(now + _NEGATIVE_CACHE_SECONDS, None, str(exc))
            raise
        self._cache[url] = _CacheEntry(now + ttl_seconds, document, None)
        return document

    async def _fetch_uncached(self, url: str) -> tuple[ClientMetadataDocument, float]:
        host, pinned_url = await self._resolve_and_pin(url)

        async with httpx.AsyncClient(
            transport=self._transport,
            follow_redirects=False,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        ) as http_client:
            try:
                async with http_client.stream(
                    "GET",
                    pinned_url,
                    headers={"Host": host, "Accept": "application/json"},
                    extensions={"sni_hostname": host},
                ) as response:
                    if response.status_code != 200:
                        raise CimdError(
                            f"{url!r} returned HTTP {response.status_code}, expected 200"
                        )
                    content_type = response.headers.get("content-type", "")
                    if "json" not in content_type.lower():
                        raise CimdError(
                            f"{url!r} did not return JSON (Content-Type: {content_type!r})"
                        )
                    body = await _read_capped(response, _MAX_DOCUMENT_BYTES)
                    ttl_seconds = _cache_ttl_seconds(response.headers.get("cache-control"))
            except httpx.TimeoutException as exc:
                raise CimdError(f"timed out fetching {url!r}: {exc}") from exc
            except httpx.HTTPError as exc:
                raise CimdError(f"could not fetch {url!r}: {exc}") from exc

        document = _parse_document(url, body)
        return document, ttl_seconds

    async def _resolve_and_pin(self, url: str) -> tuple[str, str]:
        """Validate `url`'s shape, resolve its host and reject any disallowed address,
        then return `(host, pinned_url)` - `pinned_url` has the host replaced by the
        first address that passed the check, for `_fetch_uncached` to connect to
        directly rather than trusting a second DNS lookup to resolve the same way."""
        parsed = urlsplit(url)
        if parsed.scheme != "https":
            raise CimdError(f"client_id must be an https URL, got {url!r}")
        if not parsed.netloc:
            raise CimdError(f"client_id must be an absolute URL, got {url!r}")
        if not parsed.path or parsed.path == "/":
            raise CimdError(f"client_id must have a path, got {url!r}")
        host = parsed.hostname
        if not host:
            raise CimdError(f"client_id has no host, got {url!r}")

        try:
            addresses = await asyncio.to_thread(self._resolver, host)
        except OSError as exc:
            raise CimdError(f"could not resolve {host!r}: {exc}") from exc
        if not addresses:
            raise CimdError(f"could not resolve {host!r}: no addresses returned")
        for address in addresses:
            _reject_unsafe_address(address, host)

        port = parsed.port or 443
        pinned_host = addresses[0]
        pinned_netloc = f"[{pinned_host}]:{port}" if ":" in pinned_host else f"{pinned_host}:{port}"
        pinned_url = f"https://{pinned_netloc}{parsed.path}"
        if parsed.query:
            pinned_url = f"{pinned_url}?{parsed.query}"
        return host, pinned_url


#: Defence in depth alongside `ip.is_global` (this module's docstring explains why
#: `is_global` alone is not trusted blindly): shared address space / CGNAT (RFC 6598),
#: IETF protocol assignments (RFC 6890), and benchmarking (RFC 2544) - none of IPv4
#: `is_private`/`is_reserved` flags any of these on every stdlib version.
_EXTRA_BLOCKED_IPV4_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("198.18.0.0/15"),
)

#: Documentation range (RFC 3849) - routinely used in examples, never globally routable.
_DOCUMENTATION_IPV6_NETWORK = ipaddress.ip_network("2001:db8::/32")

#: NAT64 (RFC 6052): the low 32 bits of an address in this network embed an IPv4
#: address - `ip.is_global` evaluates the *wrapper*, not what it embeds, so
#: `64:ff9b::7f00:1` (which embeds `127.0.0.1`) reads as globally routable on its own.
_NAT64_IPV6_NETWORK = ipaddress.ip_network("64:ff9b::/96")

_IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _reject_unsafe_address(address: str, host: str) -> None:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise CimdError(f"{host!r} resolved to an unparseable address {address!r}") from exc
    _reject_if_unsafe(parsed, host, address)


def _reject_if_unsafe(parsed: _IpAddress, host: str, original: str) -> None:
    # IPv4-mapped IPv6 (`::ffff:a.b.c.d`): `is_global` already special-cases this itself
    # (`IPv4Address.is_global`'s docstring: "address.is_global == address.ipv4_mapped.
    # is_global"), but the explicit checks below do not - unwrap once, up front, so
    # every check from here on sees the address that actually matters.
    mapped = getattr(parsed, "ipv4_mapped", None)
    effective: _IpAddress = mapped if mapped is not None else parsed

    # The allowlist, not a denylist: an address must be affirmatively globally routable,
    # or it is rejected - this module's docstring: the earlier allow-everything-unless-
    # explicitly-flagged check missed `100.64.0.0/10` for exactly this reason.
    if not effective.is_global:
        raise CimdError(f"{host!r} resolved to a non-global address {original!r}")

    # Defence in depth - redundant with `is_global` on every stdlib version this was
    # checked against, kept so a future stdlib change to `is_global` alone cannot
    # silently reopen one of these ranges.
    if (
        effective.is_private
        or effective.is_loopback
        or effective.is_link_local
        or effective.is_multicast
        or effective.is_reserved
        or effective.is_unspecified
    ):
        raise CimdError(f"{host!r} resolved to a disallowed address {original!r}")

    if isinstance(effective, ipaddress.IPv4Address):
        for network in _EXTRA_BLOCKED_IPV4_NETWORKS:
            if effective in network:
                raise CimdError(f"{host!r} resolved to a disallowed address {original!r}")
    elif effective in _DOCUMENTATION_IPV6_NETWORK:
        raise CimdError(f"{host!r} resolved to a disallowed address {original!r}")

    # NAT64: validate the embedded IPv4 address too, not just the IPv6 wrapper around
    # it - `parsed`, not `effective`, since an IPv4-mapped address can never also be a
    # NAT64 one (disjoint prefixes).
    if isinstance(parsed, ipaddress.IPv6Address) and parsed in _NAT64_IPV6_NETWORK:
        embedded = ipaddress.IPv4Address(int(parsed) & 0xFFFFFFFF)
        _reject_if_unsafe(embedded, host, str(embedded))


async def _read_capped(response: httpx.Response, limit: int) -> bytes:
    """`response`'s full body, streamed - `CimdError` the moment more than `limit`
    bytes have been read, never after buffering an unbounded body first."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise CimdError(f"response body exceeded the {limit}-byte cap")
        chunks.append(chunk)
    return b"".join(chunks)


def _cache_ttl_seconds(cache_control: str | None) -> float:
    max_age = _parse_max_age(cache_control) if cache_control else None
    if max_age is None:
        return float(_MIN_CACHE_SECONDS)
    return min(max(max_age, _MIN_CACHE_SECONDS), _MAX_CACHE_SECONDS)


def _parse_max_age(cache_control: str) -> float | None:
    for directive in cache_control.split(","):
        name, _, value = directive.strip().partition("=")
        if name.strip().lower() == "max-age":
            try:
                return float(value.strip())
            except ValueError:
                return None
    return None


def _parse_document(url: str, body: bytes) -> ClientMetadataDocument:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CimdError(f"{url!r} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CimdError(f"{url!r} is not a JSON object")

    document_client_id = data.get("client_id")
    if document_client_id != url:
        raise CimdError(
            f"{url!r}'s document client_id {document_client_id!r} does not equal the URL"
        )

    redirect_uris_raw = data.get("redirect_uris")
    if not isinstance(redirect_uris_raw, list) or not redirect_uris_raw:
        raise CimdError(f"{url!r} has no redirect_uris")
    redirect_uris: list[str] = []
    for item in redirect_uris_raw:
        if not isinstance(item, str) or not item:
            raise CimdError(f"{url!r} has a non-string redirect_uri")
        redirect_uris.append(item)

    # docs/research/mcp-auth-and-connectors.md §3: "no secrets for CIMD clients" - a
    # document that asks for a secret-based method is simply not usable as one.
    auth_method = data.get("token_endpoint_auth_method")
    if auth_method is not None and auth_method != "none":
        raise CimdError(
            f"{url!r} requested token_endpoint_auth_method {auth_method!r}; "
            'a CIMD client must be public ("none")'
        )

    client_name = data.get("client_name")
    if not isinstance(client_name, str):
        client_name = None

    return ClientMetadataDocument(
        client_id=document_client_id,
        client_name=client_name,
        redirect_uris=tuple(redirect_uris),
    )
