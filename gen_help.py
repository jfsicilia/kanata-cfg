#!/usr/bin/env python3
"""
gen_help.py - Generates the kwanata help pages (help/*.hlp) from the kanata
config's own action definitions under actions/.

WHAT THIS PRODUCES

kwanata shows a small help overlay while a mod layer is held, listing every
combo available in that layer and what it does. Each layer gets a ".hlp"
file: help/global_<layer>.hlp for the layer's default (app-independent)
bindings, plus help/<app>/<app>_<layer>.hlp for every app that overrides
something in that layer (e.g. help/nvim/nvim_omni.hlp). Which file kwanata
shows depends only on which kanata layer is currently active and which app
has focus — this script's job is to have the right file, with the right
content, sitting at the path kwanata expects.

WHERE THE DATA COMES FROM

Nothing about which layers exist, which mods there are, or which apps
override what is hardcoded here. All of it is discovered by parsing the
.kbd files under actions/, following this repo's naming convention:

  - A root action is a top-level (defvar ...) entry named "action_<path>"
    or "~action_<path>" (the "~" is a sync-tooling flag, irrelevant here)
    — e.g. "action_omni+c", "action_domains+a+a". The "<path>" (everything
    after "action_") is a "+"-separated list of keys, and it doubles as
    both the layer the action lives in and the combo you'd show in the
    help text: "action_omni+c+h" is the "h" key inside the "omni+c"
    sub-layer, shown as "omni + c + h".

  - The layer itself ("omni+c" above) doesn't need a defvar of its own —
    it's implied by every root action whose path has that prefix. Layers
    can nest arbitrarily deep this way (a plain mod like "tabs", a
    sub-layer like "omni+c", a two-key system like "domains+a"), all
    through the exact same mechanism: parse every root action's name,
    and every prefix of its "+"-path is a real layer that needs a page.

  - Per-app overrides live in actions/<app>/<app>_<layer>*.kbd, as defvars
    named "<app>_action_<path>". A root action's own value is typically a
    "(switch ((input virtual vk_<app>)) ... () <default>)" form that picks
    one branch per app (see _unwrap_plumbing) — discover_apps() reads that
    switch once, per root action, to find out which apps it distinguishes.

  - From a chosen switch branch onward, the config is expected to route
    through a chain of plain "$some_descriptive_name" variable references
    before finally reaching a literal binding (a key chord, macro, cmd,
    etc.). That chain of names is what makes the help text readable: the
    LAST named variable before the literal becomes the entry's label (e.g.
    "$nvim_select_all" -> label "Select All"), and the literal itself is
    rendered as the "(detail)" shown alongside it. If a root action's
    value resolves straight to a literal with no named variable at all in
    between, there's nothing descriptive to show beyond the combo itself,
    so no help entry is generated for that combo.

HOW IT'S BUILT, END TO END (see main())

  1. Parse every .kbd file under actions/ into s-expressions (tokenize +
     parse_program) — real parsing, not regex block-slicing.
  2. Flatten every (defvar ...) form, across every file, into one
     {name: raw_value} lookup table (var_table).
  3. Collect every root "action_*"/"~action_*" defvar, wherever it lives
     (all_root_actions).
  4. From those root names alone, derive the full set of layers that need
     a help page — a name's path and every prefix of it (discover_layers).
  5. For each layer, generate_layer() finds every root action belonging to
     it or to one of its sub-layers (rolled up into the same page, since
     kanata only reports the topmost active layer to the help-lookup
     mechanism — a sub-layer's bindings need to show up on its parent's
     page too), resolves each one for the global default and for every
     app it distinguishes (resolve_root_for_app + build_chain, walking the
     "$var" chain described above), and writes out the .hlp file(s).

Run directly (`python3 gen_help.py`) to regenerate help/ from the current
state of actions/. This is the only source of help/'s contents — edit
actions/, not help/*.hlp directly.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
ACTIONS_DIR = ROOT / "actions"
HELP_DIR = ROOT / "help"


# ── tokenizer / parser ──────────────────────────────────────────────────────
#
# A tiny reader for the kanata subset used under actions/: parenthesized
# lists, atoms (anything not whitespace/paren), and double-quoted strings
# (kept as a single atom INCLUDING the quotes, so "\"APP:firefox\"" round-trips
# the same way the rest of this codebase's string literals already do).

_TOKEN_RE = re.compile(r'"[^"]*"|\(|\)|[^\s()]+')


def tokenize(text: str) -> list[str]:
    """Split kanata source into a flat list of tokens.

    Args:
        text: Raw contents of a .kbd file (or any kanata source snippet).

    Returns:
        Tokens in order: "(", ")", quoted strings (kept with their quotes),
        and other atoms. ";;..." line comments are stripped first.
    """
    text = re.sub(r";;.*$", "", text, flags=re.MULTILINE)
    return _TOKEN_RE.findall(text)


class ParseError(Exception):
    """Raised by parse_program() on malformed (unbalanced-paren) input."""


def parse_program(text: str) -> list:
    """Parse kanata source into nested Python lists/atoms.

    Args:
        text: Raw contents of a .kbd file.

    Returns:
        A list of top-level forms. Each form is either a str atom or a
        (possibly nested) list of atoms/lists, mirroring the s-expression
        structure of the source — e.g. "(defvar a b)" becomes
        ["defvar", "a", "b"].

    Raises:
        ParseError: if parens are unbalanced.
    """
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
    """True if `x` is a parsed atom (a str) rather than a nested list."""
    return isinstance(x, str)


def head(expr) -> str | None:
    """The leading atom of a parsed list expression, if any.

    Args:
        expr: A parsed form (str atom or list, as produced by
            parse_program).

    Returns:
        expr[0] if `expr` is a non-empty list whose first element is an
        atom (e.g. head(["switch", ...]) == "switch"); None otherwise
        (not a list, empty list, or first element isn't an atom).
    """
    if isinstance(expr, list) and expr and is_atom(expr[0]):
        return expr[0]
    return None


# ── variable table ───────────────────────────────────────────────────────────


def _iter_defvar_pairs(forms: list):
    """Yield every (name, value) pair defined across a file's top-level forms.

    Args:
        forms: Top-level forms as returned by parse_program().

    Yields:
        (name, value) tuples for every name/value pair in every top-level
        (defvar ...) form. defvar bodies are a flat
        name-value-name-value... sequence, not nested structure, so
        consecutive items are paired up.
    """
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
    """The flattened {name: parsed_value} table for the whole config.

    Scans every actions/**/*.kbd file once (cached after the first call).
    If the same name is defined more than once across files, the first
    definition encountered (in sorted file-path order) wins.

    Returns:
        Mapping from a defvar's name (with any leading "~" stripped) to
        its parsed (but not yet plumbing-unwrapped) value.
    """
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
    """Strip a variable reference down to its bare name.

    Args:
        name: A reference token, e.g. "$foo" or "$~foo".

    Returns:
        The bare name, e.g. "foo".
    """
    return name.lstrip("$").lstrip("~")


# ── rendering primitives ─────────────────────────────────────────────────────
#
# Turning a resolved literal binding (a key chord, a macro's key sequence,
# a cmd string, ...) into the human-readable "(detail)" text shown next to
# a help entry's label. Orthogonal to the resolution logic below — this is
# just presentation.

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
    """The shifted form of a base key character, if there is one.

    Args:
        base_char: A single "base" character as produced by _BASE_KEY_CHAR
            (e.g. "a", "1", "-").

    Returns:
        The shifted character (e.g. "A", "!", "_"), or None if `base_char`
        has no shifted form this table knows about.
    """
    if base_char.isalpha():
        return base_char.upper()
    return _SHIFT_SYMBOL.get(base_char)


def _render_macro_token(tok: str) -> tuple[str, str] | None:
    """Render one whitespace-separated token from a macro body.

    Args:
        tok: A single macro token, e.g. "a", "S-a", "ent", "500", "$var".

    Returns:
        A (kind, rendered) pair where kind is "text" (a character to be
        concatenated into a run of plain text, e.g. typed letters) or
        "word" (a standalone chunk, e.g. "[ent]", shown with surrounding
        spaces) — or None if `tok` has no known rendering, signalling the
        caller to abort rendering the whole macro rather than show a
        misleading partial result.
    """
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
    """Expand $var tokens in a macro body to their raw values where possible.

    Args:
        body: The macro's tokens joined by whitespace, e.g. "esc d d $x".
        seen: Variable names already expanded on this call stack, to
            detect and stop at cycles.

    Returns:
        `body` with every expandable "$var" token replaced by its own
        (recursively expanded) raw value. A $var that can't be expanded —
        unresolvable, a cycle, or itself a non-atom value (a bare
        key-tuple fragment like folder_trash, meant to be spliced into a
        containing macro rather than rendered alone) — is kept as a
        literal "$name" token instead of aborting the whole macro's
        render (see _render_macro_token). Never returns None itself; the
        Optional return type mirrors _render_macro_token's for symmetry.
    """
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
    """Render a macro's key-sequence body as human-readable text.

    Args:
        body: The macro's tokens joined by whitespace, e.g. "esc d d i".

    Returns:
        The rendered text (e.g. typed letters concatenated, with special
        keys like "[ent]" interspersed), or None if any token in the
        (var-expanded) body has no known rendering.
    """
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
    """"some_name+with+parts" -> "Some Name With Parts", for use as a label."""
    return name.replace("_", " ").replace("+", " ").title()


# ── the resolver ─────────────────────────────────────────────────────────────
#
# Follows a root action's value through the plumbing (switch/template
# wrappers) and any chain of "$var" references, until it bottoms out at a
# literal binding — see the module docstring's "HOW IT'S BUILT" section
# for the overall picture.


def _switch_branch_app(condition: list) -> str | None:
    """The app named by one (switch ...) branch's condition, if any.

    Args:
        condition: The condition list from one switch branch, e.g.
            [["input", "virtual", "vk_nvim"]] or [] (catch-all).

    Returns:
        "nvim" for a condition of the form
        "((input virtual vk_nvim))"; None for the catch-all "()" or any
        other condition shape.
    """
    if not condition:
        return None
    clause = condition[0]
    if isinstance(clause, list) and len(clause) >= 3 and clause[0] == "input" and clause[1] == "virtual":
        vk = clause[2]
        if vk.startswith("vk_"):
            return vk.removeprefix("vk_")
    return None


_TEMPLATE_CALL_HEADS = ("t!", "template-expand")


def _unwrap_plumbing(expr, app):
    """Peel structural wrappers off a value until a "real" shape is reached.

    Args:
        expr: A parsed value (atom or list) as found in var_table(), or
            reached partway through a resolution chain.
        app: The app to resolve a switch for ("nvim", ...), or None for
            the global/catch-all default.

    Returns:
        `expr` with every "(t! TEMPLATE ... LAST)"/"(template-expand
        TEMPLATE ... LAST)" wrapper peeled (kanata accepts either spelling
        for invoking a template) and every "(switch ...)" replaced by the
        single branch matching `app` (falling back to the "()" catch-all
        if no branch matches). This is pure structural plumbing — it never
        becomes a node of its own in a resolution chain, just a way of
        getting from one def's raw value to whatever it actually points at
        for the app being resolved. Stops as soon as what's left is
        neither shape: a bare atom (a literal, or a "$..." reference for
        the caller to follow) or an unrecognized call
        (macro/cmd/multi/push-msg/...).
    """
    while isinstance(expr, list):
        h = head(expr)
        if h in _TEMPLATE_CALL_HEADS:
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
    """One named def in a resolution chain.

    Attributes:
        name: The variable's own name (e.g. "nvim_select_all").
        value: Its raw value, already plumbing-unwrapped for the app this
            chain is being resolved for.
        child: Unused placeholder for a linked-list-style next pointer;
            build_chain() tracks the chain as a plain list instead, so
            this is always left at its default.
    """

    __slots__ = ("name", "value", "child")

    def __init__(self, name, value, child=None):
        self.name = name
        self.value = value
        self.child = child


def build_chain(name: str, app: str | None, seen: frozenset) -> tuple[list[Node], object]:
    """Follow a chain of "$var" references starting at `name` to its terminal value.

    Args:
        name: The variable name to start resolving from (no leading "$").
        app: The app to resolve for ("nvim", ...), or None for the
            global/catch-all default.
        seen: Names already visited earlier in this resolution (from the
            root action onward), to detect cycles.

    Returns:
        (chain, terminal): `chain` is the list of Nodes visited, one per
        "$..." hop followed (in order, root-relative); `terminal` is
        whatever the last Node's (plumbing-unwrapped) value turns out to
        be once it's no longer a bare "$..." reference — a literal, or a
        macro/cmd/multi/push-msg/... call, or None if `name` doesn't
        resolve to anything at all (a dangling reference) or a cycle was
        hit. See is_real(), which treats a None terminal as "not
        implemented" and prunes it from the help output, same as an
        explicit not-implemented marker.
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
    """Resolve a root action's own value for one app, chain and all.

    Args:
        root_value: The root action's raw (parsed) value, as found in
            var_table().
        app: The app to resolve for ("nvim", ...), or None for the
            global/catch-all default.
        seen: Names already visited (typically just the root action's own
            name, to guard against a self-referential cycle).

    Returns:
        (chain, terminal), same shape as build_chain()'s return. The
        root's value is plumbing-unwrapped for `app` first — this is
        where the root's own "(switch ...)" picks its one branch for this
        app (see discover_apps for where the app set itself comes from) —
        and then build_chain() takes over if what's left is a "$..."
        reference to keep following. If it's already a literal, the chain
        is empty and that literal is the terminal directly.
    """
    value = _unwrap_plumbing(root_value, app)
    if is_atom(value) and value.startswith("$"):
        return build_chain(deref(value), app, seen)
    return [], value


def discover_apps(root_value) -> set[str]:
    """Find every app a root action's own switch distinguishes.

    Args:
        root_value: The root action's raw (parsed) value, as found in
            var_table().

    Returns:
        The set of app names (e.g. {"nvim", "obsidian"}) named in the
        root action's OWN top-level "(switch ...)" (peeling any
        template-call wrapper first — see _TEMPLATE_CALL_HEADS). This is
        the only place app discovery happens: nothing reached via a
        further "$..." reference gets to introduce new apps of its own
        (see _unwrap_plumbing's single-branch picking, which already
        committed to one app by that point). Empty set if the root value
        isn't a switch at all.
    """
    expr = root_value
    while isinstance(expr, list) and head(expr) in _TEMPLATE_CALL_HEADS:
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
    """Human-readable "(detail)" text for a resolved terminal value.

    Args:
        expr: A terminal value as returned by build_chain()/
            resolve_root_for_app() — a literal atom, or a
            macro/cmd/multi/push-msg/... call.

    Returns:
        The rendered detail text, or None when there's nothing worth
        showing (a push-msg, or a value with no safe rendering — e.g. a
        macro containing a nested, non-atom token that can't be
        represented without risking a misleading truncated result).
    """
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
    """Whether a resolved terminal value is worth showing as a help entry.

    Args:
        expr: A terminal value as returned by build_chain()/
            resolve_root_for_app().

    Returns:
        False for None (unresolved/dangling) or the literal "XX"
        (kanata's no-op, meaning "not bound"). For a "(push-msg ...)",
        only a `"APP:<name>"` payload counts as real (an app-launch
        action); any other push-msg is treated as a not-implemented
        placeholder. Everything else counts as real.
    """
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


def label_for(chain: list["Node"], app: str | None) -> str:
    """The human-readable label for a help entry.

    Args:
        chain: The (non-empty) Node chain from build_chain()/
            resolve_root_for_app(). Callers only reach this with a
            non-empty chain (see entry_text in generate_layer): a root
            action with no "$var" hop at all has no descriptive name of
            its own, so no entry is generated for it in the first place.
        app: The app the chain was resolved for ("nvim", ...), or None
            for the global/catch-all default — used only to strip a
            per-app naming prefix (e.g. "nvim_action_") off the label
            source before titleizing it.

    Returns:
        The last named def in the chain — the "penultimate node" once the
        terminal value is counted as the chain's final element — with any
        known naming prefix stripped and the rest titleized (e.g.
        "$nvim_select_all" -> "Select All").
    """
    name = chain[-1].name
    prefixes = list(_KNOWN_PREFIXES)
    if app:
        prefixes = [f"{app}_action_", f"{app}_"] + prefixes
    for p in prefixes:
        if name.startswith(p):
            name = name[len(p):]
            break
    return _titleize(name)


def combo_for(action_name: str) -> str:
    """The key-combo text shown for a root action, e.g. "omni + c + h".

    Args:
        action_name: A root action's bare name (leading "~" already
            stripped), e.g. "action_omni+c+h" or "action_omni".

    Returns:
        The "+"-separated path (after "action_") rendered as
        " + "-separated text: "action_omni+c+h" -> "omni + c + h". A bare
        "action_X" with no "+" at all (X already established as its own
        layer, e.g. "action_omni" -> "omni") is its layer's synthetic
        self-combo, "X + X" (see layer_of).
    """
    body = action_name.removeprefix("action_")
    if "+" not in body:
        return f"{body} + {body}"
    return " + ".join(body.split("+"))


def fsafe(key: str) -> str:
    """Escape filesystem-unsafe characters in a layer name for use in a filename.

    Args:
        key: A layer name or path segment, e.g. "domains+/" (containing a
            literal "/" key name).

    Returns:
        `key` with "/" replaced by "slash" and "\\" replaced by "bslash" —
        kwanata escapes them the same way when it looks the help file up,
        since "/" and "\\" can't appear in a filename.
    """
    return key.replace("/", "slash").replace("\\", "bslash")


# ── layer discovery + driving loop ──────────────────────────────────────────
#
# No hardcoded list of mods/layers anywhere: every layer that needs a help
# page is discovered purely by parsing action names across actions/ — a mod
# is just the top-level segment of whatever layer paths turn up. This is
# what lets a compound layer like "domains+a" (the two-key level of the
# domains system) or "omni+c" (a sub-layer of omni) get its own page
# automatically, with the exact same mechanism that gives "tabs" or "omni"
# their pages — no per-mod, per-level code anywhere.


def all_root_actions() -> list[tuple[str, object, Path]]:
    """Collect every root action defined anywhere under actions/.

    Returns:
        A list of (action_name, raw_value, source_path) tuples, one per
        top-level "action_..."/"~action_..." defvar found (leading "~"
        stripped from action_name). Per-app override files never match
        here — their own entries are named "<app>_action_...", not
        "action_..." — so this naturally separates root actions from
        per-app overrides without needing a separate mechanism.
    """
    out = []
    for path in sorted(ACTIONS_DIR.rglob("*.kbd")):
        forms = parse_program(path.read_text())
        for name, value in _iter_defvar_pairs(forms):
            bare = name.lstrip("~")
            if bare.startswith("action_"):
                out.append((bare, value, path))
    return out


def discover_layers(roots: list[tuple[str, object, Path]]) -> set[str]:
    """Derive the full set of layers that need a help page.

    Args:
        roots: Root actions as returned by all_root_actions().

    Returns:
        Every layer path implied by a root action name, plus every
        ancestor of that path — e.g. "action_omni+c+h" establishes both
        "omni" and "omni+c" as real layers (each gets its own .hlp page
        and rolls up its descendants' entries), even though an ancestor
        doesn't necessarily have a root action of its own at that exact
        path.
    """
    layers: set[str] = set()
    for name, _, _ in roots:
        body = name.removeprefix("action_")
        if "+" not in body:
            continue
        parts = body.split("+")
        for i in range(1, len(parts)):
            layers.add("+".join(parts[:i]))
    return layers


def layer_of(action_name: str, known_layers: set[str]) -> str | None:
    """The layer a root action belongs to.

    Args:
        action_name: A root action's bare name, e.g. "action_omni+c+h" or
            "action_omni".
        known_layers: The full set of discovered layers, as returned by
            discover_layers().

    Returns:
        For "action_<path>+<key>", the layer is "<path>" (everything
        before the last "+"-segment) — e.g. "action_omni+c+h" belongs to
        layer "omni+c". For a bare "action_X" with no "+" at all, it
        belongs to layer X itself, as X's own self/tap entry (e.g.
        "action_omni" -> the "omni" page's "omni + omni" row) — but only
        when X is already a real, independently-discovered layer;
        otherwise this is a plain definition with no layer of its own
        (e.g. "action_windows_close", where nothing is bound to a key
        called "close" and "windows_close" isn't established as a layer
        by any other action either) and this returns None.
    """
    body = action_name.removeprefix("action_")
    if "+" in body:
        return body.rsplit("+", 1)[0]
    return body if body in known_layers else None


def app_override_file(app: str, top_layer: str) -> Path | None:
    """Find the file that customizes a given (app, layer) pair, for attribution.

    Args:
        app: An app name, e.g. "chrome".
        top_layer: A layer's top-level segment (see generate_layer), e.g.
            "groups" — per-app override files are named after the mod
            only, never a compound sub-layer, so a sub-layer like "omni+c"
            is looked up under its top-level segment "omni".

    Returns:
        The first matching actions/<app>/<app>_<top_layer>*.kbd path in
        sorted order (e.g. actions/chrome/chrome_groups.4.kbd), used only
        to attribute a per-app help page's header comment to the file
        that actually customizes it. None if no such file exists.
    """
    app_dir = ACTIONS_DIR / app
    if not app_dir.is_dir():
        return None
    for cand in sorted(app_dir.glob(f"{app}_{top_layer}*.kbd")):
        return cand
    return None


def generate_layer(
    layer: str, roots: list[tuple[str, object, Path]], known_layers: set[str]
) -> None:
    """Write the help/*.hlp file(s) for one layer.

    Args:
        layer: The layer to generate a page for, e.g. "omni" or
            "domains+a".
        roots: Every root action, as returned by all_root_actions().
        known_layers: The full set of discovered layers, as returned by
            discover_layers().

    Writes:
        help/global_<layer>.hlp with the layer's default entries, plus
        help/<app>/<app>_<layer>.hlp for every app that overrides
        anything in this layer's scope. A layer's scope is its own root
        actions PLUS every descendant sub-layer's (e.g. "omni"'s page
        includes "omni+c"'s entries too) — this rollup exists because
        kanata only reports the topmost active layer to the help-lookup
        mechanism, so a sub-layer's bindings need to already be visible
        on its parent's page. Does nothing if the layer's scope turns out
        to be empty.
    """
    scope = []
    for name, value, path in roots:
        lo = layer_of(name, known_layers)
        if lo == layer or (lo is not None and lo.startswith(layer + "+")):
            scope.append((name, value, path))
    if not scope:
        return

    Resolved = tuple[list[Node], object]
    per_action: list[tuple[str, str, Resolved, dict[str, Resolved]]] = []
    apps_seen: set[str] = set()

    for action_name, raw_value, _path in scope:
        combo = combo_for(action_name)
        seen = frozenset({action_name})
        global_resolved = resolve_root_for_app(raw_value, None, seen)
        app_resolved = {
            app: resolve_root_for_app(raw_value, app, seen)
            for app in discover_apps(raw_value)
        }
        apps_seen.update(app_resolved)
        per_action.append((action_name, combo, global_resolved, app_resolved))

    def entry_text(resolved: tuple[list[Node], object] | None, app: str | None) -> str | None:
        """The "Label (detail)" text for one resolved (action, app) pair, or
        None if no entry should be shown for it (see label_for/is_real)."""
        if resolved is None:
            return None
        chain, terminal = resolved
        # A root action pointing straight at a literal with no "$var" hop
        # in between (empty chain) has no name of its own to describe it
        # beyond the combo itself (e.g. "action_lalt+a" -> "A-a" directly)
        # — nothing worth saying that the combo doesn't already say, so no
        # entry is generated for it.
        if not chain:
            return None
        if not is_real(terminal):
            return None
        detail = render_terminal(terminal)
        label = label_for(chain, app)
        return f"{label} ({detail})" if detail else label

    global_entries: list[tuple[str, str]] = []
    for action_name, combo, global_resolved, _app_resolved in per_action:
        text = entry_text(global_resolved, None)
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
            text = entry_text(chosen, app)
            if text is not None:
                per_app_entries.setdefault(app, []).append((combo, text))

    HELP_DIR.mkdir(exist_ok=True)
    title = layer.capitalize()
    src = scope[0][2]
    lines = [f"# {title} ({src.relative_to(ROOT)})"]
    width = max((len(f"**{c}**") for c, _ in global_entries), default=0)
    for combo, text in global_entries:
        lines.append(f"**{combo}**".ljust(width) + f" -- {text}")
    (HELP_DIR / f"global_{fsafe(layer)}.hlp").write_text("\n".join(lines) + "\n")

    top_layer = layer.split("+")[0]
    for app, entries in per_app_entries.items():
        if not entries:
            continue
        app_dir = HELP_DIR / app
        app_dir.mkdir(exist_ok=True)
        app_src = app_override_file(app, top_layer) or src
        lines = [f"# {title} - {app} ({app_src.relative_to(ROOT)})"]
        width = max(len(f"**{c}**") for c, _ in entries)
        for combo, text in entries:
            lines.append(f"**{combo}**".ljust(width) + f" -- {text}")
        (app_dir / f"{app}_{fsafe(layer)}.hlp").write_text("\n".join(lines) + "\n")


def main():
    """Regenerate help/ from scratch: discover every layer under actions/
    and write its .hlp file(s)."""
    HELP_DIR.mkdir(exist_ok=True)
    roots = all_root_actions()
    known_layers = discover_layers(roots)
    for layer in sorted(known_layers):
        generate_layer(layer, roots, known_layers)
        print(f"generated {layer}")


if __name__ == "__main__":
    main()
