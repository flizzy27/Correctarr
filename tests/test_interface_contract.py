"""Tests for the contract between server, interface and translations.

These catch the class of mistake that no amount of reading finds reliably: a
key composed at runtime (``t("rules." + name + ".title")``) that has no string
behind it, a route the frontend calls that does not exist, or a page the
gatekeeper lets through that was never defined. All three only show up when
somebody opens exactly that view.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from app import i18n
from app import settings as S
from app.rules import ALL, CATEGORIES

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
JS = (APP / "static" / "app.js").read_text(encoding="utf-8")
MAIN = (APP / "main.py").read_text(encoding="utf-8")
ENGLISH = set(i18n.bundle("en"))

# The eight views the sidebar can reach.
VIEWS = ("overview", "fixed", "findings", "queue", "rules", "indexers",
         "services", "notifications", "settings")


# ---------------------------------------------------------------------------
# Translation keys built at runtime
# ---------------------------------------------------------------------------
def _expected_runtime_keys() -> set[str]:
    keys: set[str] = set()
    for view in VIEWS:
        keys.add(f"nav.{view}")
        keys.add(f"page.{view}")
    for category in CATEGORIES:
        keys.add(f"category.{category}")
    for rule in ALL:
        keys.add(f"rules.{rule.name}.title")
        keys.add(f"rules.{rule.name}.help")
    for group in S.GROUPS:
        keys.add(f"settings_page.group_{group}")
        keys.add(f"settings_page.group_{group}_help")
    for field in S.FIELDS:
        keys.add(f"settings.{field.key}.label")
        keys.add(f"settings.{field.key}.help")
        if field.unit:
            keys.add(field.unit)
        for choice in field.choices:
            if field.key == "theme":
                keys.add(f"theme.{choice}")
            elif field.key == "density":
                keys.add(f"density.{choice}")
            else:
                keys.add(f"choice.{choice}")
    for theme in S.THEMES:
        keys.add(f"theme.{theme}")
    # Every notification channel and every field it declares.
    from app.notifications import KINDS
    for kind, channel in KINDS.items():
        keys.add(f"channels.{kind}.name")
        keys.add(f"channels.{kind}.help")
        for channel_field in channel.FIELDS:
            keys.add(f"channels.{kind}.{channel_field.key}.label")
            keys.add(f"channels.{kind}.{channel_field.key}.help")
    for trigger in ("schedule", "manual"):
        keys.add(f"run.trigger_{trigger}")
    for severity in ("error", "warning", "info"):
        keys.add(f"severity.{severity}")
    for direction in ("higher", "lower"):
        keys.add(f"direction.{direction}")
    # Every action a rule can take, and every condition it can carry. These are
    # composed at runtime, so nothing else would catch a missing one.
    from app import policy
    for action in policy.ACTIONS:
        keys.add(f"policy.action.{action}")
        keys.add(f"policy.explain.{action}")
    for condition in policy.CONDITIONS:
        keys.add(f"policy.{condition}")
        keys.add(f"policy.{condition}_help")
    # And every reason a finding can be held back, so the explanation is never
    # a bare key in front of the user.
    for reason in ("report_only", "too_young", "age_unknown", "too_large",
                   "size_unknown", "not_confident_enough", "confidence_unknown"):
        keys.add(f"policy.{reason}")
    return keys


@pytest.mark.parametrize("key", sorted(_expected_runtime_keys()))
def test_every_runtime_key_exists(key):
    assert key in ENGLISH, f"missing translation: {key}"


def test_every_runtime_key_exists_in_every_language():
    expected = _expected_runtime_keys()
    for code in i18n.AVAILABLE:
        bundle = set(i18n.bundle(code))
        missing = sorted(expected - bundle)
        assert not missing, f"{code} is missing: {missing[:10]}"


# ---------------------------------------------------------------------------
# Literal keys in the interface
# ---------------------------------------------------------------------------
def _literal_keys(source: str) -> set[str]:
    """Keys passed to t() as a plain string, not composed with +."""
    found = set()
    for match in re.finditer(r'\bt\(\s*"([a-z0-9_.]+)"\s*([,)])', source):
        found.add(match.group(1))
    return found


def test_app_js_only_uses_keys_that_exist():
    missing = sorted(_literal_keys(JS) - ENGLISH)
    assert not missing, f"app.js uses keys that do not exist: {missing}"


@pytest.mark.parametrize("template", ["index.html", "login.html", "setup.html"])
def test_templates_only_use_keys_that_exist(template):
    html = (APP / "templates" / template).read_text(encoding="utf-8")
    used = set(re.findall(r'data-t(?:-placeholder)?="([a-z0-9_.]+)"', html))
    used |= _literal_keys(html)
    missing = sorted(used - ENGLISH)
    assert not missing, f"{template} uses keys that do not exist: {missing}"


def test_python_only_uses_keys_that_exist():
    missing = set()
    for module in APP.glob("*.py"):
        source = module.read_text(encoding="utf-8")
        for key in _literal_keys(source):
            if key not in ENGLISH:
                missing.add(f"{module.name}: {key}")
    assert not missing, sorted(missing)


def test_every_finding_message_key_exists():
    """Every message key a rule can emit has to have a string behind it."""
    missing = set()
    for module in (APP / "rules.py",):
        source = module.read_text(encoding="utf-8")
        for key in re.findall(r'message="([a-z0-9_.]+)"', source):
            if key not in ENGLISH:
                missing.add(key)
        for key in re.findall(r'message=\("([a-z0-9_.]+)"', source):
            if key not in ENGLISH:
                missing.add(key)
        for key in re.findall(r'else "([a-z0-9_.]+)"\)', source):
            if key.startswith("finding.") and key not in ENGLISH:
                missing.add(key)
    assert not missing, f"rules emit message keys with no string: {sorted(missing)}"


def test_every_placeholder_in_german_exists_in_english():
    """A translation must not invent a placeholder the code never fills."""
    english = i18n.bundle("en")
    for code in i18n.AVAILABLE:
        if code == "en":
            continue
        other = i18n.bundle(code)
        for key, text in other.items():
            expected = set(re.findall(r"\{(\w+)\}", english.get(key, "")))
            actual = set(re.findall(r"\{(\w+)\}", text))
            assert actual == expected, (
                f"{code}:{key} has placeholders {sorted(actual)}, "
                f"English has {sorted(expected)}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def _routes() -> set[str]:
    return set(re.findall(r'@app\.(?:get|post|delete|put)\("([^"]+)"', MAIN))


def _matches_a_route(path: str, routes: set[str]) -> bool:
    return any(re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", route), path)
               for route in routes)


def _called_paths() -> set[str]:
    called = set()
    for pattern in (r'\bapi\(\s*[`"\']([^`"\'?]+)',
                    r'\bpost\(\s*[`"\']([^`"\'?]+)'):
        for match in re.finditer(pattern, JS):
            called.add("/" + match.group(1).rstrip("/"))
    for template in ("login.html", "setup.html"):
        html = (APP / "templates" / template).read_text(encoding="utf-8")
        for match in re.finditer(r'fetch\("([^"?]+)"', html):
            called.add("/" + match.group(1).rstrip("/"))
    # Paths built with a variable, e.g. api("api/services/" + id)
    for match in re.finditer(r'\bapi\(\s*"([^"?]+/)"\s*\+', JS):
        called.add("/" + match.group(1) + "1")
    for match in re.finditer(r'\bpost\(\s*`([^`?]+)\$\{', JS):
        called.add("/" + match.group(1) + "x")
    return called


def test_every_path_the_interface_calls_exists():
    routes = _routes()
    missing = sorted(p for p in _called_paths() if not _matches_a_route(p, routes))
    assert not missing, f"the interface calls routes that do not exist: {missing}"


def test_open_paths_all_exist():
    """A path listed as open but never defined would be a silent hole in the
    routing table rather than in the gate, but it is still a mistake."""
    routes = _routes()
    block = re.search(r"OPEN_PATHS = \{(.*?)\}", MAIN, re.S).group(1)
    missing = [p for p in re.findall(r'"([^"]+)"', block)
               if not _matches_a_route(p, routes)]
    assert not missing, f"OPEN_PATHS lists undefined routes: {missing}"


def test_only_the_expected_routes_are_open():
    """Anything reachable without a session has to be there on purpose."""
    block = re.search(r"OPEN_PATHS = \{(.*?)\}", MAIN, re.S).group(1)
    open_paths = set(re.findall(r'"([^"]+)"', block))
    expected = {
        "/api/alive",          # container health check, no data
        "/api/auth",           # signing in
        "/api/auth/state",     # tells the page whether to redirect
        "/api/auth/setup",     # first run only, refuses once an account exists
        "/api/language",       # the gate pages need their strings
        "/login", "/setup",    # the gate pages themselves
        "/api/event",          # webhook, protected by its own token
    }
    assert open_paths == expected, (
        f"unexpected open paths: {sorted(open_paths - expected)}; "
        f"missing: {sorted(expected - open_paths)}")


def _route_handlers() -> list[tuple[str, str, ast.FunctionDef]]:
    """(path, method, function) for every route, parsed rather than matched.

    A regular expression gets this wrong: ``Depends(require_user)`` contains a
    closing bracket, so a naive pattern stops reading the parameter list half
    way through and reports every guarded route as unguarded.
    """
    tree = ast.parse(MAIN)
    out = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            target = decorator.func
            if (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "app"
                    and target.attr in ("get", "post", "delete", "put")
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)):
                out.append((decorator.args[0].value, target.attr, node))
    return out


def _depends_on_user(function: ast.AST) -> bool:
    for default in list(function.args.defaults) + list(function.args.kw_defaults):
        if (isinstance(default, ast.Call)
                and isinstance(default.func, ast.Name)
                and default.func.id == "Depends"
                and default.args
                and isinstance(default.args[0], ast.Name)
                and default.args[0].id == "require_user"):
            return True
    return False


def test_every_data_route_requires_a_user():
    """Every route that is not deliberately open has to depend on a user.

    Forgetting the dependency would expose data to anyone who can reach the
    port. The middleware catches it today, but two guards are better than one
    for something this cheap — and a future refactor of the middleware must not
    be able to open everything at once.
    """
    block = re.search(r"OPEN_PATHS = \{(.*?)\}", MAIN, re.S).group(1)
    open_paths = set(re.findall(r'"([^"]+)"', block))
    # Signing out has to work even when the session has already expired,
    # otherwise a stale cookie leaves someone stuck on a redirect loop.
    exempt = open_paths | {"/", "/login", "/setup", "/api/auth/signout"}

    unguarded = [f"{path} ({function.name})"
                 for path, _, function in _route_handlers()
                 if path not in exempt and not _depends_on_user(function)]
    assert not unguarded, f"routes without a user dependency: {unguarded}"


def test_state_changing_routes_are_not_open():
    """Nothing that writes may be reachable without a session.

    The one exception is the webhook, and that carries its own token.
    """
    block = re.search(r"OPEN_PATHS = \{(.*?)\}", MAIN, re.S).group(1)
    open_paths = set(re.findall(r'"([^"]+)"', block))
    allowed_writes = {"/api/auth", "/api/auth/setup", "/api/event"}

    offenders = [path for path, method, _ in _route_handlers()
                 if method in ("post", "delete", "put")
                 and path in open_paths and path not in allowed_writes]
    assert not offenders, f"open routes that change state: {offenders}"


# ---------------------------------------------------------------------------
# Element ids the interface expects
# ---------------------------------------------------------------------------
def test_every_id_the_script_uses_exists_in_the_page():
    """A renamed id turns into a silent null dereference at runtime."""
    html = (APP / "templates" / "index.html").read_text(encoding="utf-8")
    present = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', html))
    # Ids the script creates itself rather than finding. Everything the setup
    # wizard renders is prefixed "w-", so it is covered by the prefix rule
    # below rather than by an ever-growing list.
    created = {"loading", "change-password", "compact",
               "password-form", "pw-current", "pw-new", "pw-repeat"}
    used = set(re.findall(r'\$\("#([a-zA-Z0-9_-]+)"\)', JS))
    missing = sorted(i for i in used - present - created
                     if not i.startswith("w-"))
    assert not missing, f"app.js looks for ids that are not in the page: {missing}"


def test_every_view_has_a_page_and_a_nav_button():
    html = (APP / "templates" / "index.html").read_text(encoding="utf-8")
    for view in VIEWS:
        assert f'id="page-{view}"' in html, f"no section for {view}"
        assert f'data-target="{view}"' in html, f"no nav button for {view}"
    loaders = re.search(r"const LOADERS = \{(.*?)\};", JS, re.S).group(1)
    for view in VIEWS:
        assert f"{view}:" in loaders, f"no loader for {view}"


# ---------------------------------------------------------------------------
# The public description
# ---------------------------------------------------------------------------
def test_the_readme_lists_every_rule_with_the_right_default():
    """The README is the first thing anyone reads, and it promises behaviour.

    Claiming a rule deletes by default when it does not — or the other way
    round — is worse than saying nothing, so the two are kept in step here.
    """
    import re

    from app.rules import ALL, BY_NAME
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    rows = dict(re.findall(r"^\| `([a-z_]+)` \| .+? \| (.+?) \|$", readme, re.M))

    missing = sorted({r.name for r in ALL} - set(rows))
    assert not missing, f"the README does not mention: {missing}"

    invented = sorted(set(rows) - set(BY_NAME))
    assert not invented, f"the README mentions rules that do not exist: {invented}"

    labels = json.loads(
        (APP / "locales" / "en.json").read_text(encoding="utf-8"))["policy"]["action"]
    for name, stated in rows.items():
        expected = labels[BY_NAME[name].default_action]
        assert expected.lower() in stated.lower(), \
            f"{name}: the README says {stated!r}, the rule does {expected!r}"


# ---------------------------------------------------------------------------
# Layout at every screen size
# ---------------------------------------------------------------------------
CSS = (APP / "static" / "style.css").read_text(encoding="utf-8")


def test_every_page_declares_the_viewport():
    """Without this a phone renders the page at desktop width and shrinks it,
    which makes every text too small to read and every target too small to hit."""
    for name in ("index.html", "login.html", "setup.html"):
        page = (APP / "templates" / name).read_text(encoding="utf-8")
        assert 'name="viewport"' in page, name
        assert "width=device-width" in page, name


def test_no_grid_forces_a_column_wider_than_the_screen():
    """``minmax(280px, 1fr)`` is wider than a phone once padding is taken off,
    and a grid column that cannot shrink drags the whole page sideways with it.
    ``minmax(min(280px, 100%), 1fr)`` behaves the same everywhere else and
    collapses to one column when it has to.
    """
    import re
    offenders = [m.group(0) for m in re.finditer(r"minmax\(\s*\d+px", CSS)]
    assert not offenders, (
        f"a grid column cannot shrink below its minimum: {offenders} — "
        "wrap the size in min(..., 100%)")


def test_the_phone_breakpoints_are_there():
    for query in ("max-width: 880px", "max-width: 620px", "max-width: 380px"):
        assert f"@media ({query})" in CSS, query


def test_touch_targets_are_raised_on_phones():
    """A finger is about 9 mm across. Anything smaller has to be aimed at."""
    phone = CSS.split("@media (max-width: 620px)")[1]
    assert "min-height: 40px" in phone
    assert ".toggle { width: 44px" in phone


def test_a_list_box_is_not_dressed_up_as_a_drop_down():
    assert "select[multiple]" in CSS and "background-image: none" in CSS


# ---------------------------------------------------------------------------
# What an action reports
# ---------------------------------------------------------------------------
ENGINE = (APP / "engine.py").read_text(encoding="utf-8")


def _outcome_keys() -> set[str]:
    """Every key an action can hand back, read out of the engine itself.

    These are not passed to ``t()`` where they are written — they travel as
    data and are rendered much later, possibly in another language and
    possibly months after the run. So nothing else would catch a missing one:
    it would surface as a bare ``action.result.something`` on the findings
    page of whoever happened to hit that outcome.
    """
    return set(re.findall(r'"(action\.(?:result|dry)\.[a-z0-9_]+)"', ENGINE))


def test_the_engine_names_outcomes_that_exist():
    found = _outcome_keys()
    assert len(found) > 20, "the outcome keys are no longer where this looks"
    for code in i18n.AVAILABLE:
        bundle = set(i18n.bundle(code))
        missing = sorted(found - bundle)
        assert not missing, f"{code} has no string for: {missing}"


def test_every_action_can_say_what_it_would_have_done():
    """A dry run that says nothing is a dry run nobody can judge."""
    from app import policy
    for action in policy.ACTIONS:
        if action == policy.REPORT:
            continue
        assert f"action.dry.{action}" in ENGINE, (
            f"{action} has no dry run text, so a dry run of it says nothing")


def test_the_prefixes_stay_english_wherever_they_are_stored():
    """The store is queried for them with LIKE, and the query is not localised.

    A row written while the interface was in German would otherwise be counted
    as work done by every tile, badge and notification that asks the store.
    """
    from app import policy
    from app.storage import Store
    assert policy.DRY_PREFIX in Store._REALLY_DONE
    assert policy.FAILED_PREFIX in Store._REALLY_DONE
    assert policy.really_happened("blocklisted") is True
    assert policy.really_happened(f"{policy.DRY_PREFIX}: would blocklist") is False
    assert policy.really_happened(f"{policy.FAILED_PREFIX}: no") is False
    assert policy.really_happened("") is False
    assert policy.really_happened(None) is False


def test_a_result_reads_in_both_languages():
    from app import policy
    outcome = policy.done("action.result.blocklisted_searched")
    assert outcome.text("en") != outcome.text("de")
    dry = policy.dry("action.dry.delete", what="412 MB")
    assert dry.text("en").startswith(policy.DRY_PREFIX)
    assert "412 MB" in dry.text("de")
    assert not dry.text("de").startswith(policy.DRY_PREFIX)


# ---------------------------------------------------------------------------
# Layout, continued
# ---------------------------------------------------------------------------
def test_full_height_is_measured_against_what_is_on_screen():
    """A phone's toolbars slide in and out. 100vh is the height without them,
    so a full-height element is taller than the visible area and its last row
    sits under the address bar with no way to reach it."""
    for selector in (".shell", ".sidebar", ".gate"):
        block = CSS.split(selector, 1)[1][:400]
        assert "100dvh" in block, f"{selector} still measures against 100vh only"


def test_the_navigation_can_scroll_on_a_short_window():
    """A flex child refuses to shrink below its content unless told to, and a
    list that grows past the bottom of a short window takes the sign-out button
    with it."""
    block = CSS.split(".nav {", 1)[1].split("}", 1)[0]
    assert "overflow-y: auto" in block
    assert "min-height: 0" in block


def test_messages_queue_rather_than_stack_on_top_of_each_other():
    assert "#toasts {" in CSS, "the message strip has no styling of its own"
    block = CSS.split("#toasts {", 1)[1].split("}", 1)[0]
    assert "flex-direction: column" in block
    assert "position: fixed" in block
