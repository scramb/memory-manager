# SPDX-License-Identifier: AGPL-3.0-only
"""The `/account` page shell's own markup (#229).

Same "no template engine" choice as `auth/templates.py` (that module's own
docstring explains why): plain f-strings, `html.escape` on every piece of
caller-supplied text, one inline `<style>` block. Kept as its own module
rather than extending `auth.templates` because this page is reached only
after a session already exists - it never shows a login form, a client
name or a redirect-hostname notice, the three things that module's pages
exist for - and because `account.routes` is this module's only caller,
while `auth.templates`'s pages are rendered by three different
`Authenticator`s that must not import anything account-specific.

`SECURITY_HEADERS` carries the same `Cache-Control: no-store` every login
page already sets (a signed-in account page must never be cached either),
plus `frame-ancestors 'none'` on top of `X-Frame-Options: DENY` - the CSP
directive is the one `auth.templates.SECURITY_HEADERS` is missing today,
added there instead of duplicated here (`auth.templates`'s own change,
#229) so every login page gains the same protection, not just this one.
`account_page` takes the logout path as a plain parameter rather than
importing it from `account.routes`, so this module never has to import the
module that already imports it (`account.routes` owns every path constant).
"""

from __future__ import annotations

import html

from starlette.responses import HTMLResponse

from memory_manager.auth.templates import SECURITY_HEADERS

__all__ = ["CSRF_FIELD_NAME", "account_page", "account_response"]

_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem}"
    "dl{display:grid;grid-template-columns:max-content 1fr;gap:.25rem 1rem;margin:1rem 0}"
    "dt{font-weight:600}dd{margin:0}"
    "section{margin-bottom:2rem}"
    "button{margin-top:1rem;padding:.5rem 1rem}"
)

#: The hidden form field every `/account` form carries its CSRF token under -
#: public so `account.routes` reads the same name back out of the submitted form.
CSRF_FIELD_NAME = "csrf_token"


def account_response(body: str, *, status_code: int = 200) -> HTMLResponse:
    """`body` (already a full HTML document, see `_page`) with the same security
    headers every `/login` page carries (`auth.templates.SECURITY_HEADERS`)."""
    return HTMLResponse(body, status_code=status_code, headers=SECURITY_HEADERS)


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


def account_page(*, sections_html: str, logout_path: str, csrf_token: str) -> str:
    """The signed-in `/account` page: every enabled section's own markup
    (`account.sections.render_sections`), followed by a logout form carrying a
    per-session CSRF token (`account.sessions.csrf_token`, ADR-0008 addendum: "every
    state-changing form carries a per-session CSRF token that is checked
    server-side")."""
    body = (
        "<h1>Your account</h1>"
        f"{sections_html}"
        f'<form method="post" action="{html.escape(logout_path)}">'
        f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{html.escape(csrf_token)}">'
        '<button type="submit">Log out</button>'
        "</form>"
    )
    return _page("Your account", body)
