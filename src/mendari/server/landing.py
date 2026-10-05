"""The developer landing at `/docs` (`dashboard/docs.html`, `docs.js`): what Mendari is, a
quickstart in three calls, FHIR in and out, authentication and links, on one static page.

**Public, also with sign-in on**, so it shows only what is the same for every deployment of this
release, plus this deployment's modes (sign-in, the SMART launch, Backend Services), which
`/api/auth/me`, `/fhir/metadata` and the SMART configuration already make public: never a pack,
a provider, a count or anything from the store. Open mode, where everything is public anyway,
adds a link to each pack's Decision Card.

**Its examples are the snippet scripts** (`server/snippets.py`) that CI runs against a local
server, so what the page shows works; the table of what each call needs is built from the
mounted routes and the route rules (`server/auth.py` `permission_for`), so it cannot drift.

**Filled once at startup**, from settings only and never from a request, so no request's `Host`
header can reach a page a cache might keep. `docs.js` puts this server's address into the
examples from `location.origin`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from html import escape
from urllib.parse import quote

from mendari.core.audit import KEY_DAYS, KEY_MAX_DAYS
from mendari.server import api_docs, scopes, sessions, snippets
from mendari.server.auth import EHR_ONLY, PEOPLE_ONLY, PERMISSIONS, ROLES

_MARKER = re.compile(r"<!-- landing:([a-z-]+) -->")

# What the quickstart's decide step prints (its `jq` filter), abridged: the id is new each time.
# The inbox automates only while a model screens for urgency (its policy's automation_requires):
# AUTOMATED with Jev set up, REVIEWED without a model. In open mode a server shows the answer it
# gives; with sign-in on it shows both, since its providers are not public (threat model I6).
# tests/test_landing.py checks both against what such a server decides.
QUICKSTART_PACK = "mendari/patient-inbox-router"
AUTOMATED = {
    "id": "…",
    "outcome": "AUTO",
    "recommendation": "medical_assistant",
    "priority": "routine",
    "reasons": ["confidence 1.00 >= auto threshold 0.90"],
}
REVIEWED = {
    **AUTOMATED,
    "outcome": "HUMAN_REVIEW",
    "reasons": [
        "confidence 1.00 >= auto threshold 0.90",
        "automation requires safety_screen = rules_and_jev or rules_and_llm (running: rules)",
    ],
}

# The setup the examples share. The address is filled in by docs.js from the address bar.
_SETUP = (
    'export MENDARI=<span data-origin="">https://your-mendari.example.org</span>   # this server\n'
    "export MENDARI_KEY=   # with sign-in on: your API key (ohk_…); open mode ignores it"
)

# Rows of the table in the order a developer meets them; a permission not listed here sorts last.
_ORDER = (
    "public",
    "page",
    "launch",
    "launch_decide",
    "read",
    "decide",
    "outcome",
    "review",
    "approve",
    "evaluate",
    "admin",
    "keys",
)
_WHO = {
    "public": "anyone, with no credential",
    "page": "anyone signed in (a browser that is not is sent to sign in)",
    "launch": "an EHR launch from an EHR registered here: never a person's session, a key or a "
    "token",
    "launch_decide": "an EHR launch from an EHR registered here to decide",
}
_LISTED = 8  # a row with more calls than this folds them away


def page(
    template: str,
    *,
    version: str,
    quickstart_automates: bool | None,
    sign_in: bool,
    smart_launch: bool,
    token_url: str | None,
    routes: Iterable[tuple[str, str]],
    permission_for: Callable[[str, str], str],
    cards: Mapping[str, str] | None,
) -> str:
    """The landing, from `docs.html` as `versioned_page` returns it. `quickstart_automates`: the
    inbox runs here with what its policy needs to automate (None: not to be shown, with sign-in
    on; the page then shows both answers); `routes` are the mounted
    `(method, path)` pairs; `token_url` is set when SMART Backend Services is on; `cards` (pack
    id -> name) is given in open mode only: with sign-in on, which packs run here is not public."""
    decided: list[tuple[str, Mapping[str, object]]] = (
        [
            ("Prints without a model in the screen, abridged", REVIEWED),
            ("Prints with Jev set up, abridged", AUTOMATED),
        ]
        if quickstart_automates is None
        else [("Prints, abridged", AUTOMATED if quickstart_automates else REVIEWED)]
    )
    prints = {("quickstart", "decide"): decided}
    return fill(
        template,
        {
            "intro": f"{escape(api_docs.PRODUCT)} Every decision ends in one of four outcomes "
            "and is recorded in a hash-chained audit trail.",
            "outcomes": _outcomes(),
            "setup": _code("code-setup", _SETUP, "name the server and your key"),
            "quickstart": _steps("quickstart", prints),
            "fhir": _steps("fhir", prints),
            "smart": _smart(smart_launch, sign_in=sign_in),
            "auth": _auth(sign_in, token_url),
            "auth-steps": _steps("auth", prints),
            "auth-table": _auth_table(routes, permission_for, sign_in=sign_in),
            "reference": " Sign in first: the OpenAPI document needs the <code>read</code> "
            "permission."
            if sign_in
            else "",
            "cards": _cards(cards),
            "version": escape(version),
        },
    )


def fill(template: str, parts: Mapping[str, str]) -> str:
    """Each `<!-- landing:<name> -->` replaced by its part. A marker without a part, or a part
    without a marker, is a bug in this file or the template: it fails at startup."""
    found = _MARKER.findall(template)
    if len(found) != len(set(found)) or set(found) != set(parts):
        raise RuntimeError(f"docs.html: markers {sorted(found)} do not match {sorted(parts)}")
    return _MARKER.sub(lambda marker: parts[marker[1]], template)


def _outcomes() -> str:
    """The four outcomes as the dashboard's chips (ui.js `outcomeBadge`), with what each means."""
    return "".join(
        f'<li><span class="badge o-{code}" title="{code}">{escape(words)}</span> '
        f"<code>{code}</code> <span>{escape(meaning)}</span></li>"
        for code, words, meaning in api_docs.OUTCOMES
    )


def _code(code_id: str, text: str, label: str, *, kind: str = "Shell", copy: bool = True) -> str:
    """An example (`text`, already HTML) that scrolls on its own, by keyboard too, with a copy
    button that docs.js shows. The button is named for what it copies, and the example, which
    takes focus, for its kind and step (two answers to one step are told apart by their kind)."""
    button = (
        f'<button type="button" class="docs-copy" data-copy="{code_id}" '
        f'aria-label="Copy: {escape(label)}" hidden>'
        '<svg class="icon" aria-hidden="true"><use href="#i-copy"/></svg><span>Copy</span></button>'
        if copy
        else ""
    )
    return (
        f'<div class="docs-code"><div class="docs-code-bar"><span>{kind}</span>{button}</div>'
        f'<pre tabindex="0" role="group" aria-label="{escape(kind)}: {escape(label)}">'
        f'<code id="{code_id}">{text}</code></pre></div>'
    )


def _steps(
    file: str, prints: Mapping[tuple[str, str], list[tuple[str, Mapping[str, object]]]]
) -> str:
    """One snippet file's steps as numbered items: a heading from the step's title, its commands,
    and for the quickstart's decision what it prints (`prints`, by file and step: a label and
    the answer, once per kind of server shown)."""
    items = []
    for number, step in enumerate(snippets.load(file), 1):
        anchor = f"{file}-{step.name}"
        shown = "".join(
            _code(
                f"code-{anchor}-prints" + (f"-{index}" if index else ""),
                escape(json.dumps(printed, indent=2, ensure_ascii=False)),
                step.title,
                kind=kind,
                copy=False,
            )
            for index, (kind, printed) in enumerate(prints.get((file, step.name), []))
        )
        items.append(
            f'<li class="docs-step"><h3 id="{anchor}"><span class="docs-step-number">{number}'
            f"</span> {escape(step.title)}</h3>"
            f"{_code(f'code-{anchor}', escape(step.text), step.title)}{shown}</li>"
        )
    return "".join(items)


def _smart(launch: bool, *, sign_in: bool) -> str:
    if launch:
        then = (
            " Only an EHR the deployment registers (<code>MENDARI_SMART_EHRS</code>) may launch "
            "it, for a practitioner the EHR's sign-in names: the launch may view that patient's "
            "work (<code>/api/smart/patient</code>, <code>/api/smart/focus</code>), or also "
            "decide it (<code>/api/smart/decide</code>), as registered, and its decisions are the "
            "registered organization's. It opens the dashboard on that patient's work alone."
            if sign_in
            else " It opens the dashboard on that patient's work."
        )
        return (
            "<p>Mendari runs as a SMART app launched from an EHR: register <code>/smart/launch"
            "</code> on this server as the app's launch URL. The launch reads the patient's record "
            f"with the EHR's own authorization.{then}</p>"
        )
    return (
        "<p>The SMART launch from an EHR is off here (it needs "
        "<code>MENDARI_SMART_CLIENT_ID</code>). When it is on, an EHR launches Mendari at "
        "<code>/smart/launch</code>, and the dashboard opens on that patient's work.</p>"
    )


# Requests from other sites (`server/origin.py`), in every mode.
_CROSS_SITE = (
    "A browser request that another site starts reaches only public routes and pages, with "
    "<code>GET</code> or <code>HEAD</code>; anything else is a <code>403</code> (cross-site "
    "request refused), and a form on another site that posts here gets it whether or not the "
    "browser sends the session cookie (so another site cannot sign anyone out). Scripts and "
    "integrations send no such header and are not affected."
)


def _auth(sign_in: bool, token_url: str | None) -> str:
    """How to authenticate here, for this deployment's mode: the same facts as the API
    reference's introduction (`api_docs.description`). Session lengths and rates are given as
    the defaults only: this deployment's own are in `/api/health` and the API reference, for
    those who may read (stage A of Phase 4b keeps a deployment's settings off public pages)."""
    if not sign_in:
        return (
            '<p><span class="badge s-warning">Sign-in is off here</span></p>'
            "<p>Anyone who can reach this deployment can call every operation, and the "
            "<code>Authorization</code> header is ignored. To require sign-in, set "
            "<code>MENDARI_OIDC_ISSUER</code> and <code>MENDARI_OIDC_CLIENT_ID</code>: the table "
            f"below is then what each call needs.</p><p>{_CROSS_SITE}</p>"
        )
    default = sessions.Lifetime()
    backend = (
        "<li><strong>SMART Backend Services is on.</strong> A client a signed-in administrator "
        "registers (<code>POST /api/auth/clients</code>, with its public JWK Set) gets a "
        f"five-minute access token at <code>POST {escape(token_url)}</code>: a "
        "<code>client_credentials</code> grant with an assertion signed RS384 or ES384 "
        "(<code>private_key_jwt</code>), never a client secret. It then sends "
        "<code>Authorization: Bearer mat_…</code>. "
        '<a href="/fhir/.well-known/smart-configuration">The SMART configuration</a> '
        "describes this.</li>"
        if token_url
        else "<li>SMART Backend Services (signed client assertions for five-minute tokens) is "
        "off here.</li>"
    )
    return (
        '<p><span class="badge s-ok">Sign-in is on here</span></p>'
        '<ul class="docs-facts">'
        '<li><strong>People</strong> sign in at <a href="/auth/login">/auth/login</a> with '
        "their organization's identity provider; the dashboard and the API reference use that "
        "session, which ends after a time without a request or a time after signing in, "
        f"whichever comes first ({default.idle // 60} minutes and {default.absolute // 3600} "
        "hours unless the deployment sets otherwise).</li>"
        "<li><strong>Machines</strong> send <code>Authorization: Bearer ohk_…</code>: an API key "
        "a signed-in administrator creates with <code>POST /api/auth/keys</code>, shown once. A "
        "key has one role, <code>viewer</code> or <code>service</code>, and expires "
        f"({KEY_DAYS} days unless asked, {KEY_MAX_DAYS} at most). Optional scopes narrow it: "
        "SMART v2 reads such as <code>system/Task.rs</code>, and "
        f"<code>{scopes.DECIDE}</code> or <code>{scopes.OUTCOME}</code>. A scoped key reaches "
        "only what its scopes name.</li>"
        f"{backend}"
        "<li>An <code>Authorization</code> header decides alone: a credential in it that is not "
        "valid is a <code>401</code>, even with a session cookie. A credential never goes in a "
        "URL or a form. Refusals carry <code>WWW-Authenticate</code>.</li>"
        "<li><strong>Costly calls are rate limited</strong> unless the deployment only counts "
        "them: decisions, the AI guard, evaluations and exports per caller, signing in and the "
        "token endpoint per address; reads are not. Over a limit the answer is a "
        "<code>429</code> with <code>Retry-After</code> (seconds): wait that long, then "
        "retry. The API reference gives the rates, signed in.</li>"
        f"<li>{_CROSS_SITE}</li>"
        "</ul>"
    )


def _who(permission: str) -> str:
    if permission in _WHO:
        return _WHO[permission]
    holders = ", ".join(role for role in ROLES if permission in PERMISSIONS[role])
    if permission in PEOPLE_ONLY:
        return f"{holders}, signed in as a person: never a key or a token"
    return holders


def _rank(permission: str) -> tuple[int, str]:
    return (_ORDER.index(permission) if permission in _ORDER else len(_ORDER), permission)


def _scope(method: str, path: str, permission: str) -> str:
    """What a scoped key or token needs for the call (`server/scopes.py`)."""
    if permission in ("public", "page") or permission in PEOPLE_ONLY | EHR_ONLY:
        return ""
    needed = scopes.needed(method, path)
    if needed is None:
        return ' <span class="docs-scope">not with a scoped key</span>'
    return f' <span class="docs-scope">scope <code>{escape(needed)}</code></span>'


def _auth_table(
    routes: Iterable[tuple[str, str]], permission_for: Callable[[str, str], str], *, sign_in: bool
) -> str:
    """Every route this server mounts, under the permission the route rules give it."""
    rows: dict[str, list[tuple[str, str]]] = {}
    for method, path in sorted(set(routes), key=lambda route: (route[1], route[0])):
        rows.setdefault(permission_for(method, path), []).append((method, path))
    caption = (
        "What each call on this server needs: a person's role, or a key's role and scopes."
        if sign_in
        else "With sign-in on, what each call on this server needs: a person's role, or a "
        "key's role and scopes."
    )
    body = []
    for permission in sorted(rows, key=_rank):
        calls = "".join(
            f"<li><code>{escape(method)} {escape(path)}</code>{_scope(method, path, permission)}"
            "</li>"
            for method, path in rows[permission]
        )
        listed = f'<ul class="docs-calls">{calls}</ul>'
        if len(rows[permission]) > _LISTED:
            listed = f"<details><summary>{len(rows[permission])} calls</summary>{listed}</details>"
        body.append(
            f'<tr data-permission="{escape(permission)}"><th scope="row">'
            f'<code class="chip">{escape(permission)}</code>'
            f'<span class="docs-who">{escape(_who(permission))}</span></th><td>{listed}</td></tr>'
        )
    return (
        '<div class="table-wrap"><table class="docs-auth">'
        f"<caption>{caption}</caption>"
        '<thead><tr><th scope="col">Needs</th><th scope="col">Calls</th></tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


def _cards(cards: Mapping[str, str] | None) -> str:
    """Each pack's Decision Card in the dashboard, in open mode; otherwise only where they are."""
    if cards is None:
        return (
            '<a href="/#card">Decision Cards</a>: what each pack decides, how it was tested and '
            "its limits, in the dashboard once you sign in."
        )
    links = "".join(
        f'<li><a href="/?pack={escape(quote(pack_id, safe="/"))}#card">{escape(name)}</a> '
        f"<code>{escape(pack_id)}</code></li>"
        for pack_id, name in sorted(cards.items())
    )
    return (
        "Decision Cards: what each pack decides, how it was tested and its limits."
        f'<ul class="docs-cards">{links}</ul>'
    )
