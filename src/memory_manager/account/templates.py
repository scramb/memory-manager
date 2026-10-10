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

`token_created_page` (ADR-0012, #135) is the one other full page this
module renders: `account.tokens`'s create route returns it directly, in the
POST response body, rather than redirecting back to `/account` - the
plaintext token is shown exactly once, here, and never again on a later
`GET` (CLAUDE.md "never overwrite silently" extends to "never show a secret
twice"). The copy hint is a plain, selectable, read-only text input plus a
line of static text - no inline script, no `onclick`: the CSP this page
already carries (`SECURITY_HEADERS`, `auth.templates`'s own `frame-ancestors
'none'`) forbids one, and #135's own "Nicht dabei" excludes clipboard JS
regardless.
"""

from __future__ import annotations

import html

from starlette.responses import HTMLResponse

from memory_manager.auth.templates import SECURITY_HEADERS

__all__ = ["CSRF_FIELD_NAME", "account_page", "account_response", "token_created_page"]

_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem}"
    "dl{display:grid;grid-template-columns:max-content 1fr;gap:.25rem 1rem;margin:1rem 0}"
    "dt{font-weight:600}dd{margin:0}"
    "section{margin-bottom:2rem}"
    "button{margin-top:1rem;padding:.5rem 1rem}"
    "table{border-collapse:collapse;margin:1rem 0;width:100%}"
    "th,td{border:1px solid #ccc;padding:.4rem .6rem;text-align:left;vertical-align:top}"
    "code,.token-value{font-family:ui-monospace,Menlo,Consolas,monospace}"
    ".token-value{width:100%;padding:.5rem;margin:.5rem 0;box-sizing:border-box}"
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


def token_created_page(*, name: str, plaintext: str, back_path: str) -> str:
    """The one page a new personal token's plaintext is ever shown on (ADR-0012,
    #135, this module's own docstring) - `account.tokens`'s create route renders this
    directly in its POST response rather than redirecting, so a reload or a shared
    link can never show it a second time.

    `name` is the server-generated token name (never a user-chosen label - CLAUDE.md
    "token `name` generated server-side unique"), shown only so the same row is
    recognisable in the list `back_path` leads back to."""
    body = (
        "<h1>Token created</h1>"
        "<p>Copy this token now - it will not be shown again. Select the text "
        "below and copy it.</p>"
        f'<input type="text" class="token-value" value="{html.escape(plaintext)}" '
        f'readonly aria-label="Your new personal token">'
        f"<p>Name: <code>{html.escape(name)}</code></p>"
        f'<p><a href="{html.escape(back_path)}">Back to your account</a></p>'
    )
    return _page("Token created", body)
