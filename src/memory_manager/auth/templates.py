# SPDX-License-Identifier: AGPL-3.0-only
"""Tiny HTML pages for `/login` (ADR-0004 L1/L2, #37) - deliberately no template engine.

Four pages, all built with plain f-strings and `html.escape` on every piece of
caller-supplied text (a client's self-reported `client_name`, the client's
`redirect_uri`, a rendered error message): `login_password_page` (the
password form, `auth.login_password.PasswordAuthenticator`),
`oidc_interstitial_page` (the "Continue to sign in" page shown before
redirecting to the upstream IdP, `auth.login_oidc.OidcAuthenticator`),
`login_denied_page` (an upstream identity was authenticated but is not on
the allowlist) and `login_error_page` (anything else that went wrong - an
expired pending authorization, a failed upstream call). None of these pages
ever embeds note content or anything else this server did not itself
generate, so there is nothing here for a template engine to earn its
dependency against (CLAUDE.md "few dependencies").

`_redirect_notice` is the one place both interactive pages (password form,
OIDC interstitial) satisfy the spec's consent-screen requirement: "MUST show
the redirect hostname" (`docs/research/mcp-auth-and-connectors.md`) - the
host is `html.escape`d like everything else here, since it ultimately comes
from a client-controlled `redirect_uri` (the SDK already validated it is one
of the client's registered URIs, but validated does not mean trusted to be
HTML-safe). A `127.0.0.1`/`localhost`/`::1` host gets an extra note: that
host is never reachable from outside the machine the browser itself is
running on, so it is always the user's own computer, not memory-manager's
server - worth saying explicitly rather than leaving a user to wonder why a
sign-in flow redirects to their own machine.

`html_response` is the one place the security headers every login page must
carry are set (ADR-0004/#37's login-page guidance): `Cache-Control: no-store`
(a login page, even a failed-attempt one, must never be cached), `X-Frame-
Options: DENY` (no clickjacking the password form into a frame) and a strict
`Content-Security-Policy` (`default-src 'none'` - no scripts, no images, no
external anything; `style-src 'unsafe-inline'` only because the pages below
use a single inline `<style>` block rather than pulling in a stylesheet
dependency; `form-action 'self'` - the password form may only ever submit
back to this origin; `frame-ancestors 'none'` - the CSP-level equivalent of
`X-Frame-Options: DENY`, added for `/account` (#229, ADR-0008 addendum
2026-10-08's explicit "`frame-ancestors 'none'`" requirement on every
account response) and kept here, applied to every page this module renders,
rather than only the account shell - strictly stronger than `DENY` alone,
never a behaviour change for an existing login page).
"""

from __future__ import annotations

import html
from urllib.parse import urlsplit

from starlette.responses import HTMLResponse

from memory_manager.auth.login import LOGIN_PATH

__all__ = [
    "SECURITY_HEADERS",
    "html_response",
    "login_denied_page",
    "login_error_page",
    "login_password_page",
    "oidc_interstitial_page",
]

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
    ),
}

#: Hosts that are never reachable from outside the machine the browser runs on -
#: `_redirect_notice`'s cue for the "this is your own computer" note (e.g. Claude
#: Code's loopback redirect, ADR-0004).
_LOCALHOST_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:28rem;margin:4rem auto;padding:0 1rem}"
    "form{margin-top:1rem}input{display:block;width:100%;box-sizing:border-box;padding:.5rem;"
    "margin-top:.25rem}button,.button{margin-top:1rem;padding:.5rem 1rem;display:inline-block}"
    ".error{color:#b00020}.hint{color:#555;font-size:.9em}"
)


def html_response(body: str, *, status_code: int = 200) -> HTMLResponse:
    """`body` (already a full HTML document, see `_page`) with the login-page security
    headers (`SECURITY_HEADERS`) attached."""
    return HTMLResponse(body, status_code=status_code, headers=SECURITY_HEADERS)


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


def _client_label(client_name: str | None) -> str:
    """`client_name`, `html.escape`d, or a generic label if the client never sent one
    (a DCR client that skipped it, or a static client registered without a name) -
    rendered rather than left blank, so the page never implies an anonymous request
    is asking nothing of the user."""
    return html.escape(client_name) if client_name else "An application"


def _redirect_notice(redirect_uri: str) -> str:
    """ "You will be redirected to `<host>`" (the spec's consent-screen requirement),
    plus a note when that host is only ever reachable from the user's own machine."""
    host = urlsplit(redirect_uri).hostname or redirect_uri
    notice = (
        f"<p>Once you sign in, you will be redirected to <strong>{html.escape(host)}</strong>.</p>"
    )
    if host.lower() in _LOCALHOST_HOSTS:
        notice += '<p class="hint">This app runs on your own computer (e.g. Claude Code).</p>'
    return notice


def login_password_page(
    *, pending_id: str, client_name: str | None, redirect_uri: str, error: str | None = None
) -> str:
    """The password form (`PasswordAuthenticator`'s `GET`/failed-`POST` response)."""
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    body = (
        "<h1>Sign in to memory-manager</h1>"
        f"<p>{_client_label(client_name)} wants to connect to your memory-manager server.</p>"
        f"{_redirect_notice(redirect_uri)}"
        f"{error_html}"
        f'<form method="post" action="{LOGIN_PATH}">'
        f'<input type="hidden" name="pending" value="{html.escape(pending_id)}">'
        '<label for="password">Admin password</label>'
        '<input type="password" id="password" name="password" '
        'autocomplete="current-password" autofocus required>'
        '<button type="submit">Sign in</button>'
        "</form>"
    )
    return _page("Sign in", body)


def oidc_interstitial_page(*, client_name: str | None, redirect_uri: str, continue_url: str) -> str:
    """Shown before `OidcAuthenticator` redirects to the upstream IdP - the consent
    screen's redirect-hostname notice has to live somewhere before that redirect, since
    the upstream's own login page is not this server's to control."""
    body = (
        "<h1>Sign in to memory-manager</h1>"
        f"<p>{_client_label(client_name)} wants to connect to your memory-manager server.</p>"
        f"{_redirect_notice(redirect_uri)}"
        f'<p><a class="button" href="{html.escape(continue_url)}">Continue to sign in</a></p>'
    )
    return _page("Continue to sign in", body)


def login_denied_page(message: str) -> str:
    """An authenticated-but-not-allowed identity (`OidcAuthenticator`'s allowlist check)."""
    body = f"<h1>Access denied</h1><p>{html.escape(message)}</p>"
    return _page("Access denied", body)


def login_error_page(message: str) -> str:
    """Anything else that failed - deliberately generic: `message` is this module's
    caller's job to keep free of internal detail (CLAUDE.md: no detail leakage)."""
    body = f"<h1>Sign-in failed</h1><p>{html.escape(message)}</p>"
    return _page("Sign-in failed", body)
