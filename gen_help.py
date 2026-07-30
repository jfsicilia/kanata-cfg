#!/usr/bin/env python3
"""
gen_help.py - Prototype help generator following an orthogonal, actions/-only
methodology (see conversation notes / commit message for the full rationale).

Unlike generate_help.py (which combines several different parsing paths —
switch catch-all regexes, inline per-app app_values, separate per-app files,
physical-mod folding, ...), this builds an explicit chain of named defs for
each (action, app) pair and reads the label/value straight off it — no
per-name heuristics anywhere in the resolver:

  1. Parse every .kbd file under actions/ into real s-expressions (see the
     tokenizer/parser below) — no regex block-slicing, no manual
     paren-counting.
  2. Build one flat {name: parsed_value} table from every (defvar ...) form
     found, across every file (var_table).
  3. For each mod's own actions_<mod>[.iface].kbd file, find its root
     "action_*"/"~action_*" defvar entries (root_actions) — the mod comes
     from the FILE, not from parsing the action's own name (see the
     "groups" case, whose actions used to be misnamed "!tabs+h" while
     living in actions_groups.iface.kbd).
  4. For each root action, discover_apps() looks at its OWN top-level
     "(switch ((input virtual vk_X)) ...)" once to find which apps exist —
     the only place app discovery happens.
  5. For each (action, app) pair (app=None meaning the global/catch-all
     default), resolve_root_for_app() + build_chain() walk a straight,
     explicit line of Nodes: unwrap "(t! TEMPLATE ... LAST)"/"(switch ...)"
     plumbing (_unwrap_plumbing — never itself a node, just how you get
     from one def's value to the next), and for every "$var" hop found
     along the way, append a Node(name, value) and keep following it. The
     chain stops at the first value that isn't a bare "$..." reference —
     that value is the terminal (rendered for the "(detail)" text by
     render_terminal); the LAST Node's name in the chain is the label
     source (label_for) — equivalently "the def right before the terminal
     value", with no name-shape filtering needed, because the config
     always routes a dispatch-shaped var (action_<mod>+<combo>,
     <app>_action_<mod>+<combo>) through a further, genuinely-named
     variable before reaching a literal (a per-app override pointing
     straight at a literal is a config gap to fix at the source instead —
     see e.g. the dolphin_delete fix). A chain with no hops at all (the
     root's own value was already terminal) falls back to the action's
     own name for the label.
  6. is_real() decides whether a resolved terminal is worth showing: not
     None/XX, and a "(push-msg ...)" only counts when its payload starts
     with "APP:" — anything else (including a dangling "$var" reference
     that never resolved to a value at all) is treated as not implemented
     and pruned from the output, same convention as generate_help.py.

Currently scoped to a handful of mods (see MODS below) as a prototype,
writing into hlp/ so its output can be diffed against generate_help.py's
help/ output. bookmarks/domains fold into the same resolver in principle
(see notes) but bookmarks.kbd needs to move under actions/ first for its
variables to be scannable, and domains' two-level nav page is a separate
concern layered on top — neither is wired up here yet.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
ACTIONS_DIR = ROOT / "actions"
HLP_DIR = ROOT / "hlp"

# Mods to generate in this prototype run.
MODS = ["tabs", "omni", "groups"]


# ── tokenizer / parser ──────────────────────────────────────────────────────
#
# A tiny reader for the kanata subset used under actions/: parenthesized
# lists, atoms (anything not whitespace/paren), and double-quoted strings
# (kept as a single atom INCLUDING the quotes, so "\"APP:firefox\"" round-trips
# the same way the rest of this codebase's string literals already do).

_TOKEN_RE = re.compile(r'"[^"]*"|\(|\)|[^\s()]+')


def tokenize(text: str) -> list[str]:
    # Strip ;; comments (to end of line) before tokenizing — a comment can't
    # contain an unbalanced quote/paren in this codebase.
    text = re.sub(r";;.*$", "", text, flags=re.MULTILINE)
    return _TOKEN_RE.findall(text)


class ParseError(Exception):
    pass


def parse_program(text: str) -> list:
    """Every top-level form in `text` as a nested list-of-lists/atoms."""
    tokens = tokenize(text)
    pos = 0
    forms = []

    def parse_one():
        nonlocal pos
        tok = tokens[pos]
        if tok == "(":
            pos += 1
            items = []
            while pos < len(tokens) and tokens[pos] != ")":
                items.append(parse_one())
            if pos >= len(tokens):
                raise ParseError("unbalanced parens")
            pos += 1  # consume ")"
            return items
        if tok == ")":
            raise ParseError("unexpected )")
        pos += 1
        return tok

    while pos < len(tokens):
        forms.append(parse_one())
    return forms


def is_atom(x) -> bool:
    return isinstance(x, str)


def head(expr) -> str | None:
    """The leading atom of a list expr, or None (not a list / empty)."""
    if isinstance(expr, list) and expr and is_atom(expr[0]):
        return expr[0]
    return None


# ── variable table ───────────────────────────────────────────────────────────


def _iter_defvar_pairs(forms: list):
    """Yield (name, value) for every name/value pair in every top-level
    (defvar ...) form. defvar bodies are a flat name-value-name-value...
    sequence, not nested structure, so consecutive items are paired up."""
    for form in forms:
        if head(form) != "defvar":
            continue
        body = form[1:]
        i = 0
        while i + 1 < len(body):
            name, value = body[i], body[i + 1]
            if not is_atom(name):
                # Malformed / not a name — skip just this one token to resync.
                i += 1
                continue
            yield name, value
            i += 2


_var_table: dict[str, object] | None = None


def var_table() -> dict[str, object]:
    """{name: parsed_value} across every (defvar ...) in every actions/**/*.kbd
    file. First definition wins (matches generate_help.py's convention)."""
    global _var_table
    if _var_table is None:
        table: dict[str, object] = {}
        for path in sorted(ACTIONS_DIR.rglob("*.kbd")):
            forms = parse_program(path.read_text())
            for name, value in _iter_defvar_pairs(forms):
                table.setdefault(name.lstrip("~"), value)
        _var_table = table
    return _var_table


def deref(name: str) -> str:
    """"$foo"/"$~foo" -> "foo"."""
    return name.lstrip("$").lstrip("~")


# ── rendering primitives (copied from generate_help.py — orthogonal to the
#    resolution architecture above, not something this prototype changes) ──

_BASE_KEY_CHAR = {c: c for c in "abcdefghijklmnopqrstuvwxyz0123456789,-./;'=[]\\`"}
_BASE_KEY_CHAR["grave"] = "`"
_BASE_KEY_CHAR["spc"] = " "

_SHIFT_SYMBOL = {
    "1": "!", "2": "@", "3": "#", "4": "$", "5": "%", "6": "^", "7": "&",
    "8": "*", "9": "(", "0": ")", "-": "_", "=": "+", "[": "{", "]": "}",
    "\\": "|", ";": ":", "'": '"', ",": "<", ".": ">", "/": "?", "`": "~",
}

_MOD_CHORD_RE = re.compile(r"^(?:[LR]?[CAMS]-)+\S+$")


def _shifted_char(base_char: str) -> str | None:
    if base_char.isalpha():
        return base_char.upper()
    return _SHIFT_SYMBOL.get(base_char)


def _render_macro_token(tok: str) -> tuple[str, str] | None:
    if tok.startswith("$"):
        # A $var left un-expanded by _expand_macro_var_tokens (its own
        # value isn't a plain atom to inline — e.g. a bare key-tuple
        # fragment like folder_trash, meant to be spliced into a LARGER
        # macro rather than shown on its own) — shown as-is rather than
        # aborting the whole macro's render.
        return ("word", tok)
    if tok.isdigit():
        return ("word", f"[{tok}ms]")
    m = re.fullmatch(r"Digit([0-9])", tok)
    if m:
        return ("text", m.group(1))
    if tok in _BASE_KEY_CHAR:
        return ("text", _BASE_KEY_CHAR[tok])
    if tok.startswith("S-") and tok[2:] in _BASE_KEY_CHAR:
        shifted = _shifted_char(_BASE_KEY_CHAR[tok[2:]])
        if shifted:
            return ("text", shifted)
    if _MOD_CHORD_RE.match(tok):
        return ("word", tok)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", tok):
        return ("word", f"[{tok}]")
    return None


def _expand_macro_var_tokens(body: str, seen: frozenset) -> str | None:
    """Expand $var tokens in a macro body to their raw values where
    possible. A $var that can't be expanded — unresolvable, a cycle, or
    itself a non-atom value (a bare key-tuple fragment like folder_trash,
    meant to be spliced into a containing macro rather than rendered
    alone) — is kept as a literal "$name" token instead of aborting the
    whole macro's render (see _render_macro_token)."""
    out = []
    for tok in body.split():
        if not tok.startswith("$"):
            out.append(tok)
            continue
        ref = deref(tok)
        if ref in seen:
            out.append(tok)
            continue
        raw = var_table().get(ref)
        if raw is None or not is_atom(raw):
            out.append(tok)
            continue
        expanded = _expand_macro_var_tokens(raw, seen | {ref})
        if expanded is None:
            out.append(tok)
            continue
        out.append(expanded)
    return " ".join(out)


def render_macro(body: str) -> str | None:
    expanded = _expand_macro_var_tokens(body, frozenset())
    if expanded is None:
        return None
    chunks: list[str] = []
    text_run = ""
    for tok in expanded.split():
        classified = _render_macro_token(tok)
        if classified is None:
            return None
        kind, rendered = classified
        if kind == "text":
            text_run += rendered
            continue
        if text_run:
            chunks.append(text_run)
            text_run = ""
        chunks.append(rendered)
    if text_run:
        chunks.append(text_run)
    return " ".join(chunks)


def _titleize(name: str) -> str:
    return name.replace("_", " ").replace("+", " ").title()


# ── the resolver ─────────────────────────────────────────────────────────────


def _switch_branch_app(condition: list) -> str | None:
    """"((input virtual vk_nvim))" -> "nvim". "()" -> None (catch-all)."""
    if not condition:
        return None
    clause = condition[0]
    if isinstance(clause, list) and len(clause) >= 3 and clause[0] == "input" and clause[1] == "virtual":
        vk = clause[2]
        if vk.startswith("vk_"):
            return vk.removeprefix("vk_")
    return None


def _unwrap_plumbing(expr, app):
    """Peel "(t! TEMPLATE ... LAST)" wrappers and, for a "(switch ...)",
    pick the single branch matching `app` (falling back to the "()"
    catch-all) — pure structural plumbing that never becomes a node of
    its own in the chain, just a way of getting from one def's raw value
    to whatever it actually points at for the app we're resolving for.
    Stops as soon as `expr` is neither shape — a bare atom (a literal, or
    a "$..." reference for the caller to follow) or an unrecognized call
    (macro/cmd/multi/push-msg/...), which is as far as plumbing goes.
    """
    while isinstance(expr, list):
        h = head(expr)
        if h == "t!":
            if len(expr) < 2:
                return expr
            expr = expr[-1]
            continue
        if h == "switch":
            chosen = None
            i = 1
            while i + 1 < len(expr):
                condition, value = expr[i], expr[i + 1]
                branch_app = _switch_branch_app(condition)
                if branch_app == app:
                    chosen = value
                    break
                if branch_app is None and chosen is None:
                    chosen = value  # catch-all — kept unless a matching app branch shows up
                i += 3 if (i + 2 < len(expr) and expr[i + 2] == "break") else 2
            expr = chosen
            continue
        return expr
    return expr


class Node:
    """One named def in a resolution chain: its own name, its raw
    (unwrapped-for-this-app) value, and the next Node reached by following
    that value as a "$..." reference — or None once the value isn't a
    reference anymore (see build_chain)."""

    __slots__ = ("name", "value", "child")

    def __init__(self, name, value, child=None):
        self.name = name
        self.value = value
        self.child = child


def build_chain(name: str, app: str | None, seen: frozenset) -> tuple[list[Node], object]:
    """The named-def chain starting at `name`, resolved for one app context
    (app=None = global), plus the terminal value it bottoms out at.

    Each Node is one "$..." hop; the terminal is whatever the last Node's
    (plumbing-unwrapped) value turns out to be once it's no longer a bare
    reference — a literal, or a macro/cmd/multi/push-msg/... call, or None
    if `name` itself doesn't resolve to anything (a dangling reference —
    see is_real, which prunes this from the help output entirely, same as
    an explicit "not implemented" marker).
    """
    chain: list[Node] = []
    seen = set(seen)
    while True:
        if name in seen:
            return chain, None  # cycle
        seen.add(name)
        raw = var_table().get(name)
        if raw is None:
            chain.append(Node(name, None))
            return chain, None
        value = _unwrap_plumbing(raw, app)
        chain.append(Node(name, value))
        if is_atom(value) and value.startswith("$"):
            name = deref(value)
            continue
        return chain, value


def resolve_root_for_app(root_value, app: str | None, seen: frozenset) -> tuple[list[Node], object]:
    """The chain + terminal for a root action's OWN value, resolved for one
    app (None = global) — the root's value is plumbing-unwrapped for that
    app first (this is where a root's own "(switch ...)" picks its one
    branch for this app; see discover_apps for where the app set itself
    comes from), then build_chain takes over if what's left is a "$..."
    reference to follow.
    """
    value = _unwrap_plumbing(root_value, app)
    if is_atom(value) and value.startswith("$"):
        return build_chain(deref(value), app, seen)
    return [], value


def discover_apps(root_value) -> set[str]:
    """Every app named in a root action's OWN top-level "(switch ...)"
    (peeling any "(t! ...)" wrapper first) — the only place app discovery
    happens; nothing reached via a further "$..." reference gets to
    introduce new apps of its own (see _unwrap_plumbing's single-branch
    picking)."""
    expr = root_value
    while isinstance(expr, list) and head(expr) == "t!":
        if len(expr) < 2:
            return set()
        expr = expr[-1]
    if not (isinstance(expr, list) and head(expr) == "switch"):
        return set()
    apps: set[str] = set()
    i = 1
    while i + 1 < len(expr):
        branch_app = _switch_branch_app(expr[i])
        if branch_app:
            apps.add(branch_app)
        i += 3 if (i + 2 < len(expr) and expr[i + 2] == "break") else 2
    return apps


# ── terminal-value rendering + reality check ────────────────────────────────


def render_terminal(expr) -> str | None:
    """Human-readable "(detail)" text for a resolved terminal value, or None
    when there's nothing worth showing (a push-msg, or something with no
    safe rendering)."""
    if expr is None:
        return None
    if is_atom(expr):
        if expr.upper() == "XX" or expr.startswith("$"):
            return None
        return expr
    h = head(expr)
    if h == "macro":
        # Every token must be a plain atom — a nested list (e.g. an
        # embedded (cmd ...) call) means there's no safe way to represent
        # the rest of the macro, so give up rather than silently drop it
        # and render a truncated, misleading result.
        if not all(is_atom(t) for t in expr[1:]):
            return None
        return render_macro(" ".join(expr[1:]))
    if h == "cmd":
        if not all(is_atom(t) for t in expr[1:]):
            return None
        cmd = " ".join(expr[1:])
        if '"' in cmd or "$(" in cmd:
            return None
        return cmd
    if h == "push-msg":
        return None
    if h == "multi":
        pieces = []
        for part in expr[1:]:
            if head(part) == "push-msg":
                continue
            rendered = render_terminal(part)
            if rendered is None:
                return None
            pieces.append(rendered)
        return "; ".join(pieces) if pieces else None
    return None


def is_real(expr) -> bool:
    """True when a resolved terminal value is worth showing at all — not
    None/XX, and not a not-implemented push-msg (only an "APP:<name>"
    payload counts as real, same convention as generate_help.py)."""
    if expr is None:
        return False
    if is_atom(expr):
        return expr.upper() != "XX"
    if head(expr) == "push-msg":
        text = expr[1] if len(expr) > 1 and is_atom(expr[1]) else ""
        m = re.fullmatch(r'"APP:([^"]*)"', text)
        return bool(m and m.group(1).strip())
    return True


# ── labels ───────────────────────────────────────────────────────────────────

_KNOWN_PREFIXES = ("folder_", "action_")


def label_for(chain: list["Node"], action_name: str, mod: str, app: str | None) -> str:
    """The last named def in the chain — the "penultimate node" once the
    terminal value is counted as the chain's final element (see
    build_chain/resolve_root_for_app) — is the label source. An empty
    chain means the root's own value was already terminal with no "$..."
    hop at all, so the label falls back to the action's own name."""
    if not chain:
        body = action_name.removeprefix("action_")
        for sep in (f"{mod}+", f"{mod}_"):
            if body.startswith(sep):
                body = body[len(sep):]
                break
        return _titleize(body)
    name = chain[-1].name
    prefixes = list(_KNOWN_PREFIXES)
    if app:
        prefixes = [f"{app}_action_", f"{app}_"] + prefixes
    for p in prefixes:
        if name.startswith(p):
            name = name[len(p):]
            break
    return _titleize(name)


def combo_for(action_name: str, mod: str) -> str:
    body = action_name.removeprefix("action_")
    for sep in (f"{mod}+", f"{mod}_"):
        if body.startswith(sep):
            body = body[len(sep):]
            break
    sep = "_" if action_name.removeprefix("action_").startswith(f"{mod}_") else "+"
    return mod + " + " + " + ".join(body.split(sep))


# ── mod discovery + driving loop ────────────────────────────────────────────


def mod_actions_file(mod: str) -> Path | None:
    for cand in (ACTIONS_DIR / f"actions_{mod}.iface.kbd", ACTIONS_DIR / f"actions_{mod}.kbd"):
        if cand.exists():
            return cand
    return None


def app_override_file(app: str, mod: str) -> Path | None:
    """The per-app override file for (app, mod), if one exists — e.g.
    actions/chrome/chrome_groups.4.kbd — used only to attribute a per-app
    help page's header to the file that actually customizes it, same as
    generate_help.py does."""
    app_dir = ACTIONS_DIR / app
    if not app_dir.is_dir():
        return None
    for cand in sorted(app_dir.glob(f"{app}_{mod}*.kbd")):
        return cand
    return None


def root_actions(mod: str) -> list[tuple[str, object]]:
    """[(action_name, raw_value)] for every "action_<mod>..."/"~action_<mod>..."
    root defvar in the mod's own actions file, in file order."""
    path = mod_actions_file(mod)
    if path is None:
        return []
    forms = parse_program(path.read_text())
    out = []
    for name, value in _iter_defvar_pairs(forms):
        bare = name.lstrip("~")
        if bare == f"action_{mod}" or bare.startswith(f"action_{mod}+") or bare.startswith(f"action_{mod}_"):
            out.append((bare, value))
    return out


def generate_mod(mod: str) -> None:
    actions = root_actions(mod)
    src = mod_actions_file(mod)

    # Per action: its combo string, the global (catch-all) branch, and
    # whichever per-app branches its own switch forked into. An app's page
    # isn't just "the combos it overrides" — same as generate_help.py, once
    # an app has *any* override in this mod, its page shows every combo,
    # falling back to the global default for the ones it didn't override.
    Resolved = tuple[list[Node], object]
    per_action: list[tuple[str, str, Resolved, dict[str, Resolved]]] = []
    apps_seen: set[str] = set()

    for action_name, raw_value in actions:
        combo = combo_for(action_name, mod)
        seen = frozenset({action_name})
        global_resolved = resolve_root_for_app(raw_value, None, seen)
        app_resolved = {
            app: resolve_root_for_app(raw_value, app, seen)
            for app in discover_apps(raw_value)
        }
        apps_seen.update(app_resolved)
        per_action.append((action_name, combo, global_resolved, app_resolved))

    def entry_text(resolved: tuple[list[Node], object] | None, action_name: str, app: str | None) -> str | None:
        if resolved is None:
            return None
        chain, terminal = resolved
        if not is_real(terminal):
            return None
        detail = render_terminal(terminal)
        label = label_for(chain, action_name, mod, app)
        return f"{label} ({detail})" if detail else label

    global_entries: list[tuple[str, str]] = []
    for action_name, combo, global_resolved, app_resolved in per_action:
        text = entry_text(global_resolved, action_name, None)
        if text is not None:
            global_entries.append((combo, text))

    per_app_entries: dict[str, list[tuple[str, str]]] = {}
    for app in apps_seen:
        for action_name, combo, global_resolved, app_resolved in per_action:
            resolved = app_resolved.get(app)
            # Fall back to the global default not just when the app has no
            # branch at all, but also when its own branch resolves to a
            # not-implemented placeholder (e.g. a "NOTIFY: ... not
            # implemented" push-msg) — an app's own explicit "not
            # implemented" marker shouldn't hide a real global default
            # that would otherwise apply.
            chosen = resolved if (resolved is not None and is_real(resolved[1])) else global_resolved
            text = entry_text(chosen, action_name, app)
            if text is not None:
                per_app_entries.setdefault(app, []).append((combo, text))

    HLP_DIR.mkdir(exist_ok=True)
    title = mod.capitalize()
    lines = [f"# {title} ({src.relative_to(ROOT)})"]
    width = max((len(f"**{c}**") for c, _ in global_entries), default=0)
    for combo, text in global_entries:
        lines.append(f"**{combo}**".ljust(width) + f" -- {text}")
    (HLP_DIR / f"global_{mod}.hlp").write_text("\n".join(lines) + "\n")

    for app, entries in per_app_entries.items():
        app_dir = HLP_DIR / app
        app_dir.mkdir(exist_ok=True)
        app_src = app_override_file(app, mod) or src
        lines = [f"# {title} - {app} ({app_src.relative_to(ROOT)})"]
        width = max(len(f"**{c}**") for c, _ in entries)
        for combo, text in entries:
            lines.append(f"**{combo}**".ljust(width) + f" -- {text}")
        (app_dir / f"{app}_{mod}.hlp").write_text("\n".join(lines) + "\n")


def main():
    for mod in MODS:
        generate_mod(mod)
        print(f"generated {mod}")


if __name__ == "__main__":
    main()
