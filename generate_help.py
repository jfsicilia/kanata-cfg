#!/usr/bin/env python3
"""
generate_help.py - Generate .hlp help files from kanata action files.

Follows the source-of-truth chain:
  layers/layer_{mod}.kbd          → which hold combos are actually mapped
  actions/actions_{mod}.iface.kbd → per-app dispatch + global default
  actions/actions_{mod}.kbd       → non-iface mods (bookmarks, apps, …)
  actions/{app}/{app}_{mod}.kbd   → per-app variable labels

Generates:
  help/global_{mod_short}.hlp  - combos with a real global default (not XX/push-msg)
  help/{app}_{mod_short}.hlp   - combos the app actually implements (not push-msg)

Usage:
  python3 generate_help.py [--dry-run]
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
ACTIONS_DIR = ROOT / "actions"
LAYERS_DIR = ROOT / "layers"
TOGGLES_DIR = ROOT / "layers_toggle"
HELP_DIR = ROOT / "help"

# A mod whose action names embed a shorter alias than the name its own
# files use, e.g. {"seek_n_select": "seek"} for "action_seek+spc" defined in
# actions_seek_n_select.iface.kbd. Empty right now — every mod spells its
# name the same way everywhere — but the indirection stays wired up so
# reintroducing one is a single entry rather than a code change.
MOD_SHORT: dict[str, str] = {}
# Reverse of MOD_SHORT: action names embed the short form, but mod
# files/details are keyed by the long form — action_mod() needs to
# translate back to find them.
_MOD_FROM_SHORT: dict[str, str] = {short: long for long, short in MOD_SHORT.items()}


# ── helpers ──────────────────────────────────────────────────────────────────

# The modifier tokens a physical-mod layer name is built from. "sft" is not a
# layer of its own — it is the *file* family holding lsft_layer/rsft_layer,
# mirroring the "sft+ent" action-name one-off (see parse_physical_mods_action).
# A token may carry an "|<name>" suffix ("!lctl|select"): the modifier and the
# mod it is always held with share one layer, and the whole thing is a single
# atomic name, not a composition to be taken apart.
_PHYS_MOD_TOKENS = {
    "lctl",
    "!lctl",
    "lalt",
    "!lalt",
    "lmet",
    "!lmet",
    "lsft",
    "rsft",
    "sft",
}


def _is_mod_name(name: str) -> bool:
    """True when `name` names a physical-mod layer/file — every "+"-joined
    token is a modifier, ignoring any "|<name>" suffix on it.

      "lctl", "!lctl|select", "lctl+lalt+lsft"  -> True
      "tabs", "omni", "domains"                 -> False
    """
    return all(t.split("|")[0] in _PHYS_MOD_TOKENS for t in name.split("+"))


def _mod_family(mod: str) -> str:
    """The file family a modifier belongs to — lsft/rsft share "sft"."""
    return "sft" if mod in ("lsft", "rsft") else mod


def _actions_stem(path: Path) -> str:
    """ "actions_!lctl|select.iface.kbd" -> "!lctl|select"."""
    name = path.name.removeprefix("actions_")
    return (
        name.removesuffix(".iface.kbd")
        if ".iface." in name
        else name.removesuffix(".kbd")
    )


def _app_stem(path: Path, app: str) -> str:
    """ "nvim_groups.1.kbd" -> "groups" (app prefix and priority suffix dropped)."""
    return re.sub(r"\.\d+$", "", path.name[len(app) + 1 :].removesuffix(".kbd"))


def _stem_keys(stem: str) -> list[str]:
    """The mod(s) a file-name stem provides.

    A bare physical-mod stem (only modifier tokens, e.g. "lctl",
    "lctl+lalt") folds into the single "physical_mods" key, since that one
    logical mod is spread over several files (one per modifier) — and,
    per _actions_index's docstring, is deliberately NOT registered under
    its own name too.

    A stem that also carries a "|<name>" suffix on one of its tokens (e.g.
    "!lctl|select") is a compound mod with its own identity — the
    modifier and the mod it's always held with share one file, but the
    attached mod is still reachable by that name on its own (e.g. a
    cross-mod reference to "action_!lctl|select+spc" from another mod).
    It keeps its own key alongside "physical_mods", so _actions_index()
    can find its file by either name.
    """
    if not _is_mod_name(stem):
        return [stem]
    return ["physical_mods", stem] if "|" in stem else ["physical_mods"]


_actions_index_cache: dict[str, list[Path]] | None = None


def _actions_index() -> dict[str, list[Path]]:
    """{mod: [top-level actions files defining it]}.

    Replaces assuming a mod lives in actions_{mod}.kbd: after the
    split, physical_mods spans seven files and select/search/replace each
    share one with the modifier they are always stacked under. Bare
    modifier names are deliberately NOT registered as mods of their own,
    so a stray "$action_lctl+x" reference stays unresolvable exactly as it
    was before the split — but select/search/replace ARE registered under
    their own compound name too (see _stem_keys), so a cross-mod reference
    to e.g. "action_!lctl|select+spc" from another mod can still resolve.
    """
    global _actions_index_cache
    if _actions_index_cache is None:
        idx: dict[str, list[Path]] = {}
        for f in sorted(ACTIONS_DIR.glob("actions_*.kbd")):
            for key in _stem_keys(_actions_stem(f)):
                idx.setdefault(key, []).append(f)
        _actions_index_cache = idx
    return _actions_index_cache


def find_actions_family_file(family: str) -> Path | None:
    """The top-level actions file holding `family`'s modifier actions."""
    for path in (
        ACTIONS_DIR / f"actions_{family}.iface.kbd",
        ACTIONS_DIR / f"actions_{family}.kbd",
    ):
        if path.exists():
            return path
    return None


def _deref(token: str) -> str:
    """ "$foo"/"$~foo" → "foo" — the bare name a value references."""
    return token.lstrip("$").lstrip("~")


def _titleize(name: str) -> str:
    """ "tab_new"/"tabs+w" → "Tab New"/"Tabs W" — the house style for turning
    an identifier into a help label."""
    return name.replace("_", " ").replace("+", " ").title()


def _strip_mod_prefix(name: str, mod: str) -> str:
    """Drop a leading "{mod}+" or "{mod}_" from an action name's body,
    leaving just the part that identifies the combo//action within its mod
    ("action_" must already be removed)."""
    for prefix in (f"{mod}+", f"{mod}_"):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def mod_short(mod: str) -> str:
    return MOD_SHORT.get(mod, mod)


def find_app_files(app_dir: Path, app: str, mod: str) -> list[Path]:
    """Every per-app file for (app, mod), in name order.

    Matched on what each file name declares rather than on an exact
    "{app}_{mod}.kbd" spelling, since physical_mods spans several files
    (obsidian_lctl.kbd, obsidian_!lctl|select.kbd, …). Optional numeric
    priority suffixes are ignored here.
    """
    return [
        f
        for f in sorted(app_dir.glob(f"{app}_*.kbd"))
        if mod in _stem_keys(_app_stem(f, app))
    ]


def find_app_file(app_dir: Path, app: str, mod: str) -> Path | None:
    """First per-app file for (app, mod), or None."""
    files = find_app_files(app_dir, app, mod)
    return files[0] if files else None


def combo_str(action_name: str, mod: str, key_map: dict[str, str] | None = None) -> str:
    """Convert an action name to a 'spc + e + e' style combo string.

    Prefers the physical key from key_map (built from the layer file) over
    the semantic name embedded in the action name, so the combo reflects what
    the user actually presses (e.g. 'tabs + h' instead of 'tabs + prev').

    domains is handled by its own dedicated section in main() (two-level
    nav), which builds its combo strings directly from the physical keys in
    layer_domains.kbd rather than calling this function.
    Falls back to name-parsing when key_map has no entry.
    """
    dk = mod_short(mod)
    name = action_name.removeprefix("action_")

    # Physical key from layer file takes priority over action-name semantics
    if key_map and action_name in key_map:
        return dk + " + " + key_map[action_name]

    # Fallback: derive key from action name, splitting on the same separator
    # the mod prefix used ("_" only for the "{mod}_name" spelling).
    first = name.split("+")[0]
    if first != mod and _mod_family(first) == _mod_family(mod):
        name = mod + name[len(first) :]  # "sft+ent" shown under lsft
    sub = _strip_mod_prefix(name, mod)
    sep = "_" if name.startswith(f"{mod}_") else "+"
    return dk + " + " + " + ".join(sub.split(sep))


# One defvar binding on one line: "name value", where value is either a
# fully parenthesized expression — "sleep_system (multi (push-msg "...")
# (cmd systemctl suspend))", whatever it wraps; _render_value() walks it
# later to decide what's worth showing — or a single token that does NOT
# start with "(". That exclusion is what keeps the opening line of a
# multi-line expression ("foo (switch", body on later lines) from being
# mistaken for a complete value: the balanced alternative can't match it
# (no closing ")" before the MULTILINE "$"), and the bare-token
# alternative refuses anything starting with "(". Multi-line values are
# simply skipped, which is the intent.
_VAR_VALUE_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_!+\\/.,;'\[\]=-]*)\s+(\(.*\)|[^\s(]\S*)\s*$",
    re.MULTILINE,
)
_var_value_cache: dict[str, str] | None = None


# Single visible characters a bare kanata key name types literally, mapped
# to themselves (so "c" -> "c") plus named keys that are a single character
# under another name ("grave" -> "`", "spc" -> " " — a plain space reads
# fine inline and doesn't need a "[spc]" callout like the other named keys).
_BASE_KEY_CHAR = {c: c for c in "abcdefghijklmnopqrstuvwxyz0123456789,-./;'=[]\\`"}
_BASE_KEY_CHAR["grave"] = "`"
_BASE_KEY_CHAR["spc"] = " "

# US-QWERTY shift mapping for the symbol row — letters are handled via
# .upper() instead, since that's uniform for every letter.
_SHIFT_SYMBOL = {
    "1": "!",
    "2": "@",
    "3": "#",
    "4": "$",
    "5": "%",
    "6": "^",
    "7": "&",
    "8": "*",
    "9": "(",
    "0": ")",
    "-": "_",
    "=": "+",
    "[": "{",
    "]": "}",
    "\\": "|",
    ";": ":",
    "'": '"',
    ",": "<",
    ".": ">",
    "/": "?",
    "`": "~",
}

_MOD_CHORD_RE = re.compile(r"^(?:[LR]?[CAMS]-)+\S+$")


def _shifted_char(base_char: str) -> str | None:
    if base_char.isalpha():
        return base_char.upper()
    return _SHIFT_SYMBOL.get(base_char)


def _render_macro_token(tok: str) -> tuple[str, str] | None:
    """Classify one whitespace-separated macro token for display.

    Returns (kind, text): kind "text" glues `text` onto the current run of
    typed characters with no separator (a literal key, or the shifted
    character a "S-x" step produces); kind "word" is its own space-separated
    piece — a "[Nms]" delay, a bracketed special key like "[ent]", or a real
    modifier chord (e.g. "C-t") shown as-is.

    None means `tok` isn't a plain kanata key/chord at all — e.g. it's part
    of a nested (cmd ...) shell call, a $variable, or a quoted string — and
    signals the caller to give up on rendering this macro entirely, since
    there's no safe way to represent the rest of it.
    """
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
        # falls through to the chord check below (e.g. base has no shifted form)
    if _MOD_CHORD_RE.match(tok):
        return ("word", tok)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", tok):
        return ("word", f"[{tok}]")
    return None


def _expand_macro_var_tokens(body: str, seen: frozenset[str]) -> str | None:
    """Expand any $var/~$var tokens in a macro body to their raw values
    before tokenizing for display — e.g. "$tmux_prefix [" (tmux_prefix =
    "A-b") -> "A-b [", so a macro built on a prefix-key variable still
    renders instead of being abandoned at the first "$" token. Returns
    None if a $ref can't be resolved (missing, a cycle, or itself a
    parenthesized expression too complex to inline here)."""
    out = []
    for tok in body.split():
        if not tok.startswith("$"):
            out.append(tok)
            continue
        ref = _deref(tok)
        if ref in seen:
            return None
        raw = _raw_leaf_table().get(ref)
        if raw is None or raw.startswith("("):
            return None
        expanded = _expand_macro_var_tokens(raw, seen | {ref})
        if expanded is None:
            return None
        out.append(expanded)
    return " ".join(out)


def render_macro(body: str) -> str | None:
    """Human-readable rendering of a (macro ...) body, e.g.

        "C-t 10 c h r o m e S-; / / e x t e n s i o n s / s h o r t c u t s ent"
        -> "C-t [10ms] chrome://extensions/shortcuts [ent]"

    $var/~$var tokens (e.g. a "$tmux_prefix" leading a tmux macro) are
    expanded to their raw value first (see _expand_macro_var_tokens).

    Returns None if any token isn't a plain key/chord — a nested (cmd ...)
    shell call, an unresolvable $variable, a quoted string — in which case
    nothing here is safe to render (rather than guess and show something
    misleading).
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


def _split_top_level(expr: str) -> list[str]:
    """Split `expr` on whitespace, but only at paren-depth 0 and outside
    quoted strings — so "(cmd foo)" or '"a b"' survive as single pieces
    instead of being torn apart. Used to walk one level of an s-expression
    without a full parser."""
    parts: list[str] = []
    depth = 0
    quoted = False
    start = 0
    for i, c in enumerate(expr):
        if c == '"':
            quoted = not quoted
        elif not quoted:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            elif c.isspace() and depth == 0:
                if i > start:
                    parts.append(expr[start:i])
                start = i + 1
    if start < len(expr):
        parts.append(expr[start:])
    return parts


def _render_value(value: str) -> str | None:
    """Recursively render a kanata value expression for display, e.g.

        "(t! unmod_all (macro RA-spc 300 s y s ... ent))"
        -> "RA-spc [300ms] sys..."
        "(multi (cmd kanata_sync.sh) (push-msg "RELOAD:") lrld)"
        -> "kanata_sync.sh; lrld"

    Understands a small fixed set of wrapper shapes:
      - "(t! TEMPLATE ... LAST)"   -> recurse into the last argument
      - "(macro ...)"              -> render_macro() on the remaining tokens
      - "(cmd ...)"                -> the shell command, verbatim
      - "(push-msg ...)"           -> never shown (notification only)
      - "(multi A B ...)"          -> each of A, B, … rendered and joined
                                       with "; " (push-msg pieces dropped)
      - a bare atom (no parens)    -> shown as-is, unless it's "$var" (a
                                       nested reference — out of scope here,
                                       the top-level $var chain is handled
                                       separately by _resolve_var_chain) or
                                       "XX" (not-yet-implemented marker)

    Anything else (switch, an unrecognized template, …) means there's no
    safe way to represent it, so this returns None rather than guess.
    """
    value = value.strip()
    if not value:
        return None
    if not value.startswith("("):
        if value.startswith("$") or value.upper() == "XX":
            return None
        return value
    if not value.endswith(")") or value.count("(") != value.count(")"):
        return None
    parts = _split_top_level(value[1:-1].strip())
    if not parts:
        return None
    head = parts[0]
    if head == "macro":
        return render_macro(" ".join(parts[1:]))
    if head == "t!":
        return _render_value(parts[-1]) if len(parts) >= 2 else None
    if head == "cmd":
        cmd = " ".join(parts[1:])
        if '"' in cmd or "$(" in cmd:
            return None
        return cmd
    if head == "push-msg":
        return None
    if head == "multi":
        pieces = []
        for p in parts[1:]:
            if p.startswith("(push-msg"):
                continue
            rendered = _render_value(p)
            if rendered is None:
                return None
            pieces.append(rendered)
        return "; ".join(pieces) if pieces else None
    return None


_raw_leaf_cache: dict[str, str] | None = None


def _raw_leaf_table() -> dict[str, str]:
    """Cached {name: raw_value_text} for every actions/ leaf-var defvar
    binding capturable on a single line — either a bare token/$ref
    (_VAR_VALUE_RE) or a fully parenthesized expression kept as its
    ORIGINAL, unrendered text (_VAR_VALUE_EXPR_RE; rendering for display
    happens later, once any $ chain has been followed to its terminal
    value — see _resolve_var_chain). Absence here just means "too complex
    to see on one line" (multi-line, a switch, …) — NOT the same as "not
    implemented"; callers checking for reality (e.g. _cross_ref_is_real)
    must not conflate the two. First definition found wins if a name is
    (re)defined more than once.
    """
    global _raw_leaf_cache
    if _raw_leaf_cache is None:
        raw: dict[str, str] = {}
        for path in sorted(ACTIONS_DIR.rglob("*.kbd")):
            # Strip both whole-line comments and trailing inline comments —
            # e.g. the ";;""" alignment-tooling marker (see CLAUDE.md) after
            # a real value on the same line, which would otherwise defeat
            # the "\s*$" end-of-value anchor in the regexes below.
            text = re.sub(r";;.*$", "", path.read_text(), flags=re.MULTILINE)
            for m in _VAR_VALUE_RE.finditer(text):
                raw.setdefault(m.group(1), m.group(2))
        _raw_leaf_cache = raw
    return _raw_leaf_cache


def _resolve_var_chain(
    name: str, raw: dict[str, str], seen: frozenset[str]
) -> str | None:
    """Follow a chain of raw table values through $var/~$var references
    until landing on a terminal value, rendering it for display if it's a
    parenthesized expression. Returns None on a cycle, a reference to
    something absent from the table (e.g. an action whose only default is
    a per-app switch, which has no single combo to show), or an expression
    _render_value can't confidently render."""
    if name in seen:
        return None
    value = raw.get(name)
    if value is None:
        return None
    if value.startswith("$"):
        ref = _deref(value)
        return _resolve_var_chain(ref, raw, seen | {name})
    if value.startswith("("):
        return _render_value(value)
    return value


def _var_value_table() -> dict[str, str]:
    """Best-effort {name: display_combo} table for defvar bindings under
    actions/ that resolve to something worth showing next to a help label
    (e.g. "tab_new" -> "C-t", "chrome_keybindings" -> a rendered macro) —
    e.g. "Tab New (C-t)". Built from _raw_leaf_table() by following each
    name's $var chain to a terminal value (see _resolve_var_chain), so a
    leaf var that just points at another leaf var still resolves to a
    real combo instead of leaking the raw "$other_var" text.
    """
    global _var_value_cache
    if _var_value_cache is None:
        raw = _raw_leaf_table()
        table = {}
        for name in raw:
            resolved = _resolve_var_chain(name, raw, frozenset())
            if resolved is not None:
                table[name] = resolved
        _var_value_cache = table
    return _var_value_cache


def is_simple_combo(value: str | None) -> bool:
    """True when `value` is a single bare kanata token (e.g. "C-1", "A-S-w",
    "left") worth showing to the user as-is — not a $variable, macro,
    switch, or other multi-token/parenthesized expression."""
    if not value or value.upper() == "XX":
        return False
    if value.startswith(("(", "$", "~")):
        return False
    return len(value.split()) == 1


def with_combo(label: str, combo: str | None) -> str:
    """Append " (combo)" to `label`, or leave it alone when there's nothing
    worth showing. Callers pass either a value already validated by
    is_simple_combo, or a _var_value_table() lookup (whose entries are
    resolved and rendered, so they need no further validation)."""
    return f"{label} ({combo})" if combo else label


def label_from_var(var_name: str, app: str) -> str:
    """'$nvim_action_page_up' → 'Page Up', '$folder_trash' → 'Trash'."""
    var = _deref(var_name)
    stripped = var
    for prefix in [f"{app}_action_", f"{app}_", "folder_", "action_"]:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :]
            break
    label = _titleize(stripped)
    return with_combo(label, _var_value_table().get(var))


def label_from_action(action_name: str, mod: str) -> str:
    """Fallback label derived from action name when no $variable is available."""
    return _titleize(_strip_mod_prefix(action_name.removeprefix("action_"), mod))


def action_layer(action_name: str) -> str | None:
    """The layer an action is bound in — everything before its final key.

      "action_panes+move+h"     -> "panes+move"
      "action_panes+h"          -> "panes"
      "action_!lctl|select+a"   -> "!lctl|select"
      "action_sft+ent"          -> "lsft"   (one-off, see below)

    None when the name holds no "+" at all: those are plain definitions
    ("action_windows_close", "action_omni"), not combos — nothing is bound
    to a key called "close", and rendering them as "windows + close" would
    advertise a combo that cannot be pressed.

    "sft+ent" is a one-off: per layer_sft.kbd it only fires while holding
    lsft (lsft_layer), not from any "sft_layer".
    """
    name = action_name.removeprefix("action_")
    if "+" not in name:
        return None
    layer = name.rsplit("+", 1)[0]
    return "lsft" if layer == "sft" else layer


def action_mod(action_name: str) -> str:
    """The mod a fully-qualified action name belongs to — the part
    between "action_" and its first "+" (e.g. "action_tabs+move+h" -> "tabs").

    Where a mod aliases its name in action names, the short form is
    translated back to the long form used by its files/details via
    _MOD_FROM_SHORT (currently empty — see MOD_SHORT).
    """
    short = action_name.removeprefix("action_").split("+", 1)[0]
    return _MOD_FROM_SHORT.get(short, short)


_mod_actions_cache: dict[str, tuple[list[str], dict[str, dict]]] = {}


def parse_mod_actions(mod: str) -> tuple[list[str], dict[str, dict]]:
    """Cached (order, details) for `mod`, merged over every actions file
    that declares it (see _actions_index), so cross-mod references and
    the physical_mods pass alike work without re-reading files or assuming
    one file per mod.

    "domain<X>" (e.g. "domain,", "domainT") is a pseudo-domain: each
    punctuation key / nvim-style sub-domain of the "domains" (SPC+) system
    gets its own name so action names don't collide, but they're all
    defined in the single shared actions_domains.iface.kbd file rather than
    one file per pseudo-domain — so the lookup is redirected there.
    """
    if mod not in _mod_actions_cache:
        lookup = "domains" if mod.startswith("domain") and mod != "domains" else mod
        order: list[str] = []
        details: dict[str, dict] = {}
        for path in _actions_index().get(lookup, []):
            o, d = parse_iface(path)
            order += o
            details.update(d)
        _mod_actions_cache[mod] = (order, details)
    return _mod_actions_cache[mod]


def _mod_iface_details(mod: str) -> dict[str, dict]:
    """Just the details half of parse_mod_actions."""
    return parse_mod_actions(mod)[1]


def resolve_cross_mod_label(
    action_name: str, mod: str, app: str | None, _seen: frozenset[str] = frozenset()
) -> str:
    """Label for a fully-qualified action name (e.g. "action_tabs+w") found
    as the value of another action — possibly in a different mod than the
    one currently being rendered. Since actions are now named after the bare
    combo that triggers them (e.g. "tabs+w", not "tabs_close"), there's no
    semantic word left to title-case once you cross into another mod —
    this instead resolves the referenced action's own real label:

      1. if `app` overrides it in the referenced mod, recurse into that
         per-app value (e.g. obsidian's own $obsidian_new_tab);
      2. else fall back to the referenced mod's real global default;
      3. else degrade to the naive name-derived guess, same as before.

    `_seen` guards against reference cycles.
    """
    if action_name in _seen:
        return label_from_action(action_name, mod)
    seen = _seen | {action_name}
    ref_mod = action_mod(action_name)

    if app is not None:
        app_file = find_app_file(ACTIONS_DIR / app, app, ref_mod)
        if app_file is not None:
            impl = parse_app_file(app_file, app)
            if action_name in impl:
                return label_for_implemented_value(
                    impl[action_name], action_name, ref_mod, app, seen
                )

    ref_default = effective_global_default(
        _mod_iface_details(ref_mod), action_name, ref_mod
    )
    if ref_default is not None:
        return label_from_global_default(ref_default, action_name, ref_mod, seen)

    return label_from_action(action_name, mod)


def label_from_global_default(
    default: str, action_name: str, mod: str, _seen: frozenset[str] = frozenset()
) -> str:
    """Human-readable label from a global default value.

    '$copy'                        → 'Copy'           (leaf var name, title-cased)
    '$action_tabs+w'               → resolved recursively (see
                                      resolve_cross_mod_label) since the
                                      action name itself is just "tabs+w" —
                                      no semantic word left to title-case.
    '(macro ...)', 'prnt', 'C-w'   → derives from action name
    """
    # $leaf_var or $action_... → label from variable name
    if default.startswith("$"):
        var_name = _deref(default)
        clean = var_name
        if clean.startswith("action_"):
            return resolve_cross_mod_label(clean, mod, None, _seen)
        mod_pfx = f"{mod}_"
        if clean.startswith(mod_pfx):
            clean = clean[len(mod_pfx) :]
        label = _titleize(clean)
        return with_combo(label, _var_value_table().get(var_name))
    # Everything else (complex expressions, raw key tokens) → action name
    return with_combo(
        label_from_action(action_name, mod),
        default if is_simple_combo(default) else None,
    )


def _terminal_expr(value: str) -> str:
    """Unwrap "(t! TEMPLATE ... LAST)" template-call wrappers (e.g.
    unmod_all) down to the expression a value ultimately reduces to, the
    same unwrapping _render_value does for the "t!" case — but returning
    the terminal text unrendered, so the caller can inspect it. A value
    that isn't a "(t! ...)" wrapper (bare token, "(multi ...)", …) is
    already its own terminal expression: "multi" in particular is left
    alone on purpose, since a push-msg bundled inside one is just a
    side-effect notification, not the action's whole meaning (see
    is_real_default).
    """
    value = value.strip()
    if not value.startswith("(t!") or not value.endswith(")"):
        return value
    if value.count("(") != value.count(")"):
        return value
    parts = _split_top_level(value[1:-1].strip())
    if len(parts) < 2 or parts[0] != "t!":
        return value
    return _terminal_expr(parts[-1])


def is_real_default(value: str | None) -> bool:
    """True when value is a usable global default — not None, XX, or a
    not-implemented push-msg.

    A push-msg only disqualifies a value when it's the value's terminal
    expression (see _terminal_expr) — the whole point of the value, not
    just one side-effect bundled into a "(multi ...)" alongside real
    actions (e.g. sync_and_reload_kanata's reload notification). A
    terminal push-msg is real only when it launches an app (APP:...);
    every other terminal push-msg (NOTIFY:..., DEBUG:..., an empty
    APP:) is the "not implemented" convention, same as XX — checked
    through any "(t! TEMPLATE ...)" wrapper (e.g. unmod_all), so a $var
    whose whole job is to send one such message is still recognized as
    a placeholder regardless of what mod it belongs to.
    """
    if not value:
        return False
    if value.upper() == "XX":
        return False
    terminal = _terminal_expr(value)
    if terminal.startswith("(push-msg"):
        app_m = re.search(r'"APP:([^"]+)"', terminal)
        return bool(app_m and app_m.group(1).strip())
    return True


_PHYS_MOD_LETTER = {
    "lctl": "C",
    "!lctl": "C",
    "lalt": "A",
    "!lalt": "A",
    "lmet": "M",
    "!lmet": "M",
    "sft": "S",
    "lsft": "S",
    "rsft": "S",
}


def is_physical_mods_passthrough(action_name: str, mod: str, value: str) -> bool:
    """True when a physical_mods default is just the bare chord its own name
    describes (e.g. action "lctl+a" defaulting to "C-a") — i.e. holding that
    physical combo does exactly what it would do unremapped. Not worth a help
    entry since it tells the user nothing they don't already know.
    """
    if mod != "physical_mods":
        return False
    parts = action_name.removeprefix("action_").split("+")
    if len(parts) < 2:
        return False
    *mods, key = parts
    # Ignore any "|<name>" suffix: "!lctl|select" is still the C modifier.
    letters = [_PHYS_MOD_LETTER.get(m.split("|")[0]) for m in mods]
    if any(letter is None for letter in letters):
        return False
    return value == "-".join(letters + [key])


_DEFLAYERMAP_RE = re.compile(r"\(deflayermap\s+\((\S+)\)")


def parse_layer_names() -> list[str]:
    """Every physical_mods layer declared anywhere under layers/
    (deflayermap block names, "_layer" suffix stripped) — the authoritative
    list of every such layer that actually exists, independent of whether
    any action is currently bound to it (unlike deriving nodes from the
    actions files, which would miss layers with no bindings yet, e.g.
    multi-real-modifier combos).

    A layer belongs to physical_mods when its name is nothing but
    "+"-joined modifier tokens (see _is_mod_name),
    so these are found wherever they live — layer_lctl.kbd,
    layer_!lctl|select.kbd, layer_sft.kbd, … — rather than by assuming one
    fixed filename holds them all.
    """
    names: list[str] = []
    for path in sorted(LAYERS_DIR.glob("layer_*.kbd")):
        for m in _DEFLAYERMAP_RE.finditer(path.read_text()):
            names.append(m.group(1).removesuffix("_layer"))
    return names


_layer_aliases_cache: dict[str, str] | None = None


def _layer_root_aliases() -> dict[str, str]:
    """{layer root: mod that owns it}, for the roots that don't already name
    their own mod — a mod is free to call its layer something else than
    "{mod}_layer" ("keyb" opens alt_chars_layer, since the key is named after
    what you press and the layer after what it does).

    Ownership comes from the declaring file: layers/layer_{mod}.kbd holds
    {mod}'s layers whatever they are called (the same convention
    _actions_index() reads actions_{mod}.kbd by), so nothing here needs
    maintaining by hand.

    Roots that are their own mod are deliberately left out, so every lookup
    through here is the identity for them — physical modifiers included,
    whose roots ("lsft" in layer_sft.kbd) name a modifier rather than the
    file family they live in.
    """
    global _layer_aliases_cache
    if _layer_aliases_cache is None:
        aliases: dict[str, str] = {}
        for path in sorted(LAYERS_DIR.glob("layer_*.kbd")):
            mod = path.name.removeprefix("layer_").removesuffix(".kbd")
            for m in _DEFLAYERMAP_RE.finditer(path.read_text()):
                root = m.group(1).removesuffix("_layer").split("+")[0]
                if root != mod and not _is_mod_name(root):
                    aliases.setdefault(root, mod)
        _layer_aliases_cache = aliases
    return _layer_aliases_cache


def mod_of_layer_root(root: str) -> str:
    """The mod a layer root belongs to ("alt_chars" -> "keyb"), unchanged for
    every layer named after its own mod."""
    return _layer_root_aliases().get(root, root)


def mod_base_layer(mod: str) -> str:
    """Reverse of mod_of_layer_root: the root of the layer `mod` opens
    ("keyb" -> "alt_chars"). Layer names — not mod names — are what kanata
    reports and so what the .hlp files are named after."""
    for root, owner in _layer_root_aliases().items():
        if owner == mod:
            return root
    return mod


_SINGLE_LAYER_TOGGLE_RE = re.compile(
    r"^\s*(toggle_\S+)\s+\(t!\s+toggle_(?:\d?mod_)?layer\s+(.+?)\)\s*$", re.MULTILINE
)


def directly_toggled_layers() -> set[str]:
    """Layers with their own single-layer toggle (not stacked with anything
    else) that's genuinely bound to a real key somewhere — referenced as
    "$toggle_..._layer" somewhere other than the layers_toggle/ file that
    declares it.

    Every real-modifier combination gets such a toggle var *declared*
    (e.g. holding both lctl+lalt has one), but most are never actually wired
    to a key — only genuinely-used ones count as independently reachable.
    The home-row mods are the prototypical case: holding physical f/j
    directly fires $toggle_lctl_layer (setup.kbd), completely independent of
    the physical-Alt stack that also happens to use "lctl_layer" as its base
    layer — so "lctl" needs its own help file for that reason alone,
    regardless of whether it's also shadowed somewhere else.
    """
    # {toggle var: (target layer, file that declares it)} — the declaring file
    # is tracked per variable rather than assumed to be one shared file,
    # since these declarations are spread across layers_toggle/ by mod.
    declared: dict[str, tuple[str, Path]] = {}
    for decl_path in sorted(TOGGLES_DIR.glob("*.kbd")):
        for m in _SINGLE_LAYER_TOGGLE_RE.finditer(decl_path.read_text()):
            args = m.group(2).split()
            if args:
                declared[m.group(1)] = (args[-1].removesuffix("_layer"), decl_path)

    # A variable counts as used only where it is referenced somewhere other
    # than its own declaration.
    used: set[str] = set()
    for path in ROOT.rglob("*.kbd"):
        text = path.read_text()
        used.update(
            var
            for var, (_target, decl) in declared.items()
            if path != decl and f"${var}" in text
        )

    return {declared[var][0] for var in used}


def reachable_layers(layer_stacks: list["LayerStack"]) -> list[str]:
    """Every declared physical_mods layer (see parse_physical_mods_layer_names)
    that a user can actually hold and have kwanata report as the active
    layer — every one except those always shadowed by some LayerStack's top
    and never independently, genuinely toggled on their own (see
    physical_mods_directly_toggled_nodes).

    E.g. "lctl" and "lctl+lsft" are shadowed mid-layers of the physical-Alt
    3-stack but ALSO directly reachable via home-row mods, so they stay
    available; "lctl+lalt" is shadowed by the !lctl+lalt 2-layer stack with
    no independent toggle wired to it anywhere, so it stays hidden (its
    content only ever shows up folded into "!lctl+lalt"'s file).
    """
    declared = parse_layer_names()
    tops = {s.layers[-1] for s in layer_stacks}
    shadowed = {n for s in layer_stacks for n in s.layers[:-1]}
    directly_toggled = directly_toggled_layers()
    shadowed_only = shadowed - tops - directly_toggled
    return [name for name in declared if name not in shadowed_only]


class LayerStack:
    """An ordered kanata layer stack (base → top) pushed in one go by
    holding one or more physical modifier keys — see the toggle_mod_2layer /
    toggle_2mod_2layer / toggle_mod_3layer / toggle_2mod_3layer / … templates
    in templates.kbd, all called from layers_toggle/*.kbd. Kanata (and so
    kwanata, which drives the help popup) only ever reports the topmost
    layer as active, so every earlier ("shadowed") layer's own help content
    is otherwise unreachable while the key is held — it needs folding into
    the top layer's file instead.

    Every shadowed layer is itself a physical_mods layer (e.g. "lctl"
    under "!lctl|select", or "lctl+lalt" under "!lctl|select+lalt"), so
    folding one in is just taking its own entries — see
    physical_mods_node_entries(rollup=False).
    """

    def __init__(self, layer_names: list[str]):
        self.layers = layer_names  # base -> top, "_layer" suffix already stripped

    @property
    def top_node(self) -> tuple[str, ...]:
        return tuple(self.layers[-1].split("+"))

    def shadowed_physical_mods_nodes(self) -> list[tuple[str, ...]]:
        """physical_mods mods-tuples for every shadowed layer that belongs
        to physical_mods itself (not some other mod)."""
        return [
            tuple(name.split("+")) for name in self.layers[:-1] if _is_mod_name(name)
        ]


_LAYER_STACK_RE = re.compile(r"\(t!\s+toggle_(?:\d?mod)_(\d)layer\s+(.+?)\)")


def parse_layer_stacks() -> list[LayerStack]:
    """Parse every layers_toggle/*.kbd file for toggle_{mod,2mod,3mod,4mod}_
    {2,3}layer calls into LayerStack objects (ordered base → top layer
    names, "_layer" suffix stripped). The last N whitespace-separated
    tokens of each such call are always the N stacked layer names (N taken
    from the template name itself), regardless of how many leading
    modifier-name args precede them or which file the call lives in — so
    this needs no per-file or per-combination-shape special-casing.

    Several toggle variables can describe the exact same stack — e.g.
    holding lctl+lalt vs. lalt+lctl both toggle "lctl+lalt_layer" /
    "!lctl+lalt_layer", just reached via a differently-ordered modifier
    hold — so duplicates (same layer list) are collapsed to one.
    """
    seen: dict[tuple[str, ...], None] = {}
    for path in sorted(TOGGLES_DIR.glob("*.kbd")):
        text = path.read_text()
        for m in _LAYER_STACK_RE.finditer(text):
            n_layers = int(m.group(1))
            tokens = m.group(2).split()
            if len(tokens) < n_layers:
                continue
            layer_names = tuple(t.removesuffix("_layer") for t in tokens[-n_layers:])
            seen.setdefault(layer_names, None)
    return [LayerStack(list(layer_names)) for layer_names in seen]


def is_implemented(value: str) -> bool:
    """True when a per-app binding is a real implementation, not a no-op or push-msg marker."""
    if value.upper() == "XX":
        return False
    if value.startswith("(push-msg"):
        return False
    return True


def _cross_ref_is_real(ref: str, seen: frozenset[str]) -> bool:
    """Whether a $ref (already stripped of leading "$"/"~") used as a
    global-default value ultimately resolves to a genuine implementation,
    rather than just being a reference to a placeholder.

    A reference into ANOTHER mod's action (ref starts with "action_")
    only counts as real when that action's own effective_global_default is
    real — every mod's per-app switch catch-all defaults to a "NOTIFY:
    ... not implemented" push-msg (or XX) until someone actually wires up
    a real global default, and a bare $reference to it is not that. A
    plain leaf var counts as real unless its own raw value is positively a
    placeholder or itself an unreal cross-mod reference; a leaf var
    whose value is too complex to see on one line (absent from
    _raw_leaf_table) is assumed real — see that function's docstring.
    """
    if ref in seen:
        return False
    if ref.startswith("action_"):
        # Don't add `ref` to `seen` here — effective_global_default's own
        # "action in _seen" guard checks this exact name (ref *is* the
        # action being evaluated), so pre-adding it would make every
        # first-time cross-mod reference look like an immediate cycle.
        ref_mod = action_mod(ref)
        return (
            effective_global_default(_mod_iface_details(ref_mod), ref, ref_mod, seen)
            is not None
        )
    seen = seen | {ref}
    raw = _raw_leaf_table().get(ref)
    if raw is None:
        return True
    if raw.startswith("$"):
        return _cross_ref_is_real(_deref(raw), seen)
    return is_real_default(raw)


def effective_global_default(
    details: dict[str, dict], action: str, mod: str, _seen: frozenset[str] = frozenset()
) -> str | None:
    """The global-default value to show in help for `action`, or None if there
    isn't one worth showing.

    Prefers the switch catch-all, falls back to a direct (non-switch) binding,
    and — for physical_mods — drops a bare passthrough chord (see
    is_physical_mods_passthrough). A value that's just a $reference to
    ANOTHER mod's action (directly or through a chain of leaf vars) is
    only kept when that reference is itself real (see _cross_ref_is_real)
    — otherwise a cross-mod link to something unimplemented would
    misleadingly look implemented here.
    """
    d = details.get(action, {})
    g = d.get("global")
    if is_real_default(g):
        effective = g
    else:
        direct = d.get("direct")
        effective = direct if is_real_default(direct) else None
    if effective is None or is_physical_mods_passthrough(action, mod, effective):
        return None
    if effective.startswith("$"):
        if action in _seen:
            return None
        ref = _deref(effective)
        if not _cross_ref_is_real(ref, _seen | {action}):
            return None
    return effective


def label_for_implemented_value(
    value: str,
    action_name: str,
    mod: str,
    app: str,
    _seen: frozenset[str] = frozenset(),
) -> str:
    """Label for a value a per-app file binds directly to an action.

    A value like "$obsidian_action_tabs+t" is a per-app cross-mod
    reference (obsidian's own override of the "tabs" mod's action, reused
    here) — resolved recursively the same way as a global one (see
    resolve_cross_mod_label), rather than title-cased into "Tabs T".
    """
    if value.startswith("$"):
        clean = _deref(value)
        app_prefix = f"{app}_"
        if clean.startswith(app_prefix) and clean[len(app_prefix) :].startswith(
            "action_"
        ):
            ref_action = clean[len(app_prefix) :]
            if ref_action not in _seen:
                return resolve_cross_mod_label(ref_action, mod, app, _seen)
        return label_from_var(value, app)
    if value.startswith("("):
        return label_from_action(action_name, mod)
    return label_from_global_default(value, action_name, mod, _seen)


def entry_label(
    action: str,
    mod: str,
    details: dict[str, dict],
    impl: dict[str, str] | None = None,
    app: str | None = None,
) -> str | None:
    """The help label for one action: the app's own implementation when it
    overrides it, else the mod's real global default — or None when
    neither exists, meaning the action gets no help entry at all.

    This is the single resolution rule every help file follows (global
    catalog, per-app files, shadowed-mod merges, physical_mods roll-ups);
    passing impl={} / app=None gives the globals-only form.
    """
    if impl and action in impl:
        return label_for_implemented_value(impl[action], action, mod, app)
    effective = effective_global_default(details, action, mod)
    if effective is None:
        return None
    return label_from_global_default(effective, action, mod)


# ── parsers ───────────────────────────────────────────────────────────────────


def parse_iface(path: Path) -> tuple[list[str], dict[str, dict]]:
    """Parse an actions .kbd file (iface or plain).

    Returns:
      - ordered list of action names as they appear in the file
      - details dict: {action_name: {
            'global':     str | None,   # () catch-all value
            'direct':     str | None,   # direct binding value (no switch)
            'app_values': {app: var},   # inline per-app values (bookmarks-style)
        }}
    """
    text = path.read_text()
    order: list[str] = []
    details: dict[str, dict] = {}

    header_re = re.compile(r"^\s{2}~?(action_\S+)", re.MULTILINE)
    headers = list(header_re.finditer(text))

    for i, m in enumerate(headers):
        name = m.group(1)
        block_start = m.start()
        block_end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = text[block_start:block_end]

        order.append(name)
        # Strip commented-out lines before all pattern searches so that
        # commented-out alternatives (e.g. ";; () (push-msg ...)") don't shadow
        # the real binding on the next line.
        block_nc = re.sub(r"^\s*;;.*$", "", block, flags=re.MULTILINE)

        # Catch-all value: capture full push-msg expression or simple token
        global_m = re.search(
            r'\(\)\s+(\(push-msg\s+"[^"]*"\)|\S+)\s+break',
            block_nc,
        )
        global_val = global_m.group(1) if global_m else None

        # Direct binding fallback for non-switch actions — first shape that
        # matches anywhere in the block wins, most specific first.
        direct_val = None
        if global_val is None and "switch" not in block_nc:
            for pattern in (
                r"action_\S+\s+\(t!\s+unmod_all\s+([^\s)]+)",  # (t! unmod_all KEY)
                r"action_\S+\s+(\$\S+)",  # action_name $var
                r"action_\S+\s+([^\s()\n]+)",  # action_name KEY (S-prnt, lrld, …)
            ):
                m_direct = re.search(pattern, block_nc)
                if m_direct:
                    direct_val = m_direct.group(1)
                    break
            # Complex expression → signal label_from_action
            if direct_val is None and re.search(r"action_\S+\s+\(", block_nc):
                direct_val = "(action)"

        # Inline per-app values for bookmarks-style files (no separate app file)
        app_values: dict[str, str] = {}
        for av_m in re.finditer(
            r"\(\(input virtual vk_(\w+)\)\)\s+(.+?)\s+break",
            block_nc,
            re.DOTALL,
        ):
            av_app = av_m.group(1)
            av_expr = av_m.group(2).strip()
            if av_expr.startswith("(push-msg") or av_expr.upper() == "XX":
                continue
            var_ref = re.search(r"\$(\w+)", av_expr)
            if var_ref:
                app_values[av_app] = "$" + var_ref.group(1)
            else:
                app_values[av_app] = av_expr

        details[name] = {
            "global": global_val,
            "direct": direct_val,
            "app_values": app_values,
        }

    return order, details


def parse_layer_bindings(
    mod: str, iface_action_set: set[str]
) -> tuple[dict[str, str], dict[str, str]]:
    """{action_name: key_combo_suffix} — the physical key path for each
    action, e.g. "h", "lsft + t", "move + h". Built from the first key that
    maps to each action in the mod's own deflayermap blocks, so the
    combo shown in help is what the user actually presses.

    All of layers/ is scanned rather than assuming a layer_{mod}.kbd,
    because a mod's layers don't necessarily live in a file named after
    it — "select" is declared inside layer_!lctl|select.kbd, alongside the
    physical modifier it's always pushed with. Blocks belonging to any
    other layer are skipped, so an action referenced from a foreign layer
    (e.g. "tab $action_tabs+l" inside lmet_layer) can't be mistaken for
    that action's own binding.

    Also returns {action_name: layer} — which deflayermap the binding sits
    in. That is ground truth: an action's name usually mirrors its layer,
    but not always (groups_layer binds "$~action_!tabs+h"), so callers
    prefer this over parsing the name. These are layer names, not mod names:
    a mod whose layer goes by another name (keyb -> alt_chars_layer) reports
    the layer, since that is what the caller matches its pages against.

    Both are empty when the mod declares no layer, or declares it through
    templates rather than explicit key lines (physical modifiers) — callers
    then fall back to the action name.
    """
    key_map: dict[str, str] = {}
    layer_of: dict[str, str] = {}
    current_mod: str | None = None
    in_mod = False
    base = mod_base_layer(mod)

    text = "\n".join(p.read_text() for p in sorted(LAYERS_DIR.glob("layer_*.kbd")))
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(";;"):
            continue

        # Track which deflayermap block we are in and extract its modifier
        dlm_m = re.match(r"\(deflayermap\s+\((\S+)\)", stripped)
        if dlm_m:
            layer_name = dlm_m.group(1)
            current_mod = None
            in_mod = True
            if layer_name.startswith(f"{base}+"):
                current_mod = layer_name[len(f"{base}+") :].removesuffix("_layer")
            elif layer_name != f"{base}_layer":
                in_mod = False  # unrelated layer (another mod's, or a mod layer)
            continue

        if not in_mod or "$action_" not in stripped:
            continue

        # Physical key is the first token on the line
        parts = stripped.split()
        raw_key = parts[0] if parts else ""

        # Skip template calls (but don't expand them — use iface_order for per-app)
        if not raw_key or raw_key.startswith("("):
            continue

        # Resolve $var|alias style keys: "$k|rsft" → "k", "$toggle" → "toggle"
        # Skip keys starting with "!" (complex physical-mod aliases)
        if raw_key.startswith("$"):
            alias_base = raw_key[1:].split("|")[0]
            if not alias_base or alias_base.startswith("!"):
                continue
            physical_key = alias_base
        else:
            physical_key = raw_key

        # First $action_* reference on the line
        action_m = re.search(r"\$~?(action_\S+)", stripped)
        if not action_m:
            continue
        action = action_m.group(1)
        if action not in iface_action_set:
            continue

        # Record the first physical key that triggers this action
        if action not in key_map:
            key_map[action] = (
                f"{current_mod} + {physical_key}" if current_mod else physical_key
            )
            layer_of[action] = f"{base}+{current_mod}" if current_mod else base

    return key_map, layer_of


def parse_app_file(path: Path, app: str) -> dict[str, str]:
    """Return {action_name: first_value_token} for implemented actions only.

    Skips:
      - commented-out lines (starting with ;;)
      - (push-msg ...) not-implemented markers
    """
    text = path.read_text()
    impl_re = re.compile(
        rf"^(?!\s*;;)\s*{re.escape(app)}_action_(\S+)\s+(\S+)",
        re.MULTILINE,
    )
    result: dict[str, str] = {}
    for m in impl_re.finditer(text):
        value = m.group(2)
        if is_implemented(value):
            result["action_" + m.group(1)] = value
    return result


# ── Domains layer parser ─────────────────────────────────────────────────────


def _domains_physical_key(raw_key: str) -> str:
    """The physical key a layer_domains.kbd line binds: "$modA|x" → "a",
    "$toggle" → "toggle", "," → ",". The "mod" prefix on an alias is the
    domains-layer naming convention for the key that opens a sub-domain,
    not part of the key itself."""
    if not raw_key.startswith("$"):
        return raw_key
    base = raw_key[1:].split("|")[0]
    return base[3:] if base.startswith("mod") and len(base) > 3 else base


def parse_domains_layer() -> tuple[
    list[tuple[str, str, str]],  # spc_entries: [(phys_key, mod_name, title)]
    dict[str, list[tuple[str, str]]],  # sublayers:   {mod_name: [(sub_key, action)]}
]:
    """Parse layer_domains.kbd into:
    spc_entries  — ordered (phys_key, mod_name, mod_title) for each key in domains_layer
    sublayers    — {mod_name: [(sub_key, action_name)]} for each spc+mod*_layer
    """
    path = LAYERS_DIR / "layer_domains.kbd"
    text = path.read_text()

    layer_titles: dict[str, str] = {}
    sublayers: dict[str, list[tuple[str, str]]] = {}
    domains_raw: list[tuple[str, str]] = []  # (phys_key, mod_name)

    current_ctx = None  # "domains" | mod_name | None
    pending_cmnt = ""

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue

        # Close of a deflayermap block: lone ")" possibly followed by a comment
        if re.match(r"^\)\s*(;.*)?$", s):
            current_ctx = None
            continue

        if s.startswith(";;"):
            c = s.lstrip(";").strip()
            c = re.sub(r"\s*;;.*$", "", c).strip()
            pending_cmnt = c
            continue

        dlm = re.match(r"\(deflayermap\s+\((\S+)\)", s)
        if dlm:
            lname = dlm.group(1)
            if lname == "domains_layer":
                current_ctx = "domains"
            elif lname.startswith("domains+") and lname.endswith("_layer"):
                mod = lname[8:-6]  # strip "domains+" prefix and "_layer" suffix
                layer_titles[mod] = pending_cmnt
                sublayers.setdefault(mod, [])
                current_ctx = mod
            else:
                current_ctx = None
            pending_cmnt = ""
            continue

        # Data line inside a block: the physical key is always its first token
        pending_cmnt = ""
        key_m = re.match(r"^(\S+)", s)
        if not key_m or current_ctx is None:
            continue
        phys = _domains_physical_key(key_m.group(1))

        if current_ctx == "domains":
            vk_m = re.search(r"press-vkey vk_([^\s)]+)", s)
            if vk_m:
                # vk name is like "domains+A"; normalize to "a" to match sublayer keys
                domains_raw.append((phys, vk_m.group(1).split("+", 1)[-1].lower()))
        elif not s.startswith("_"):
            # Search (not match) so the action is found even when wrapped in
            # a template call, e.g. "\ (t! sft_switch $action_mod\+mod\ ...)"
            action_m = re.search(r"\$(action_\S+)", s)
            if action_m:
                sublayers[current_ctx].append((phys, action_m.group(1)))

    spc_entries = [(phys, mod, layer_titles.get(mod, "")) for phys, mod in domains_raw]
    return spc_entries, sublayers


# ── formatting ────────────────────────────────────────────────────────────────


def format_hlp(
    title: str, entries: list[tuple[str, str]], source_file: Path | None = None
) -> str:
    """Render (combo, label) pairs as a .hlp file."""
    header = f"# {title}"
    if source_file is not None:
        header += f" ({source_file.relative_to(ROOT)})"
    lines = [header]
    if entries:
        max_combo = max(len(c) for c, _ in entries)
        for combo, label in entries:
            pad = " " * (max_combo - len(combo))
            lines.append(f"**{combo}**{pad} -- {label}")
    return "\n".join(lines) + "\n"


# ── main ──────────────────────────────────────────────────────────────────────


class HelpWriter:
    """Writes .hlp files and remembers which ones it wrote, so whatever is
    left over from a previous run can be pruned at the end."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.written: set[Path] = set()

    def emit(
        self, out: Path, title: str, entries: list[tuple[str, str]], source: Path
    ) -> None:
        """Render, write and track one .hlp file (no-op write in dry-run mode)."""
        self.written.add(out)
        print(f"  {'[dry]' if self.dry_run else 'wrote'} {out.relative_to(ROOT)}")
        if not self.dry_run:
            out.parent.mkdir(exist_ok=True)
            out.write_text(format_hlp(title, entries, source))

    def prune(self) -> None:
        """Delete .hlp files this run didn't write (root and app subdirs),
        then drop any app subdirectory left with nothing in it."""
        for stale in sorted(HELP_DIR.glob("**/*.hlp")):
            if stale not in self.written:
                print(
                    f"  {'[dry]' if self.dry_run else 'removed'} {stale.relative_to(ROOT)}"
                )
                if not self.dry_run:
                    stale.unlink()
        if not self.dry_run:
            for subdir in sorted(HELP_DIR.iterdir()):
                if subdir.is_dir() and not any(subdir.glob("*.hlp")):
                    subdir.rmdir()


def layer_mod(layer: str) -> str:
    """The mod a layer belongs to — every physical-modifier layer belongs to
    the single "physical_mods" mod, anything else to its first segment, or to
    whatever mod declares that segment when the two differ (see
    mod_of_layer_root)."""
    return (
        "physical_mods"
        if _is_mod_name(layer)
        else mod_of_layer_root(layer.split("+")[0])
    )


def layer_title(layer: str) -> str:
    """MOD_TITLES is keyed by mod, so a base layer named after something
    other than its mod (alt_chars -> keyb) still gets its mod's title.
    Sub-layers keep their own name, which already says what they narrow."""
    return (layer if "+" in layer else layer_mod(layer)).capitalize()
    # return MOD_TITLES.get(layer if "+" in layer else layer_mod(layer), layer)


def fsafe(key: str) -> str:
    """ "/" and "\\" can't appear in filenames; kwanata escapes them the same
    way when it looks the file up."""
    return key.replace("/", "slash").replace("\\", "bslash")


def emit_mod_help(
    w: HelpWriter,
    mod: str,
    layers: list[str],
    app_dirs: list[Path],
    layer_stacks: list["LayerStack"],
) -> None:
    """One help file per reachable layer of `mod`, plus its per-app files.

    Every layer gets its own page because kanata reports whichever layer is
    active and kwanata looks the file up by that name — so a layer without a
    file means the help overlay goes blank while you hold those keys. Each
    page rolls up its descendants, so the base page still lists everything
    under the mod and the sub-pages narrow it down.
    """
    iface_order, details = parse_mod_actions(mod)
    key_map, bound_in = parse_layer_bindings(mod, set(iface_order))
    # The layer file wins; the action name is the fallback for mods whose
    # layers are generated by templates rather than explicit key lines.
    layer_of = {a: bound_in.get(a) or action_layer(a) for a in iface_order}

    def subtree(layer: str) -> list[str]:
        """Actions bound in `layer` or any layer nested under it."""
        return [
            a
            for a in iface_order
            if layer_of[a]
            and (layer_of[a] == layer or layer_of[a].startswith(layer + "+"))
        ]

    def entries_for(acts, impl=None, app=None):
        out = []
        for a in acts:
            label = entry_label(a, mod, details, impl, app)
            if label is not None:
                out.append(
                    (
                        combo_str(
                            a, mod_of_layer_root(layer_of[a].split("+")[0]), key_map
                        ),
                        label,
                    )
                )
        return out

    def shadowed_by(layer: str) -> list[str]:
        """Actions of the layers a stack topped at `layer` hides. Kanata
        reports only the topmost layer, so without this their combos would
        be unreachable from any help page."""
        out = []
        for s in layer_stacks:
            top = s.layers[-1]
            if top != layer and not top.startswith(layer + "+"):
                continue
            for hidden in s.layers[:-1]:
                out += [a for a in iface_order if layer_of[a] == hidden]
        return out

    for layer in layers:
        src = find_actions_family_file(
            _mod_family(layer.split("+")[0])
        ) or find_actions_family_file(mod)
        acts = subtree(layer) + shadowed_by(layer)

        # The global page is always emitted — even title-only — so every
        # layer you can hold has a reminder of what it is for, regardless of
        # how much of it is implemented yet.
        w.emit(
            HELP_DIR / f"global_{fsafe(layer)}.hlp",
            layer_title(layer),
            entries_for(acts),
            src,
        )

        for app_dir in app_dirs:
            app = app_dir.name
            app_files = find_app_files(app_dir, app, mod)
            if not app_files:
                continue
            impl: dict[str, str] = {}
            for f in app_files:
                impl.update(parse_app_file(f, app))
            # An app earns its own page only where it actually overrides
            # something; otherwise the page would just repeat the global
            # one, which kwanata already falls back to.
            if not any(a in impl for a in acts):
                continue
            entries = entries_for(acts, impl, app)
            if entries:
                w.emit(
                    HELP_DIR / app / f"{app}_{fsafe(layer)}.hlp",
                    f"{layer_title(layer)} - {app}",
                    entries,
                    app_files[0],
                )

    # ── Per-app help from inline values ──────────────────────────────────
    # For non-iface files (bookmarks, …) where per-app implementations are
    # embedded in the main actions file rather than separate app files.
    inline_by_app: dict[str, list[tuple[str, str]]] = {}
    for a in iface_order:
        for av_app, av_val in details.get(a, {}).get("app_values", {}).items():
            if find_app_file(ACTIONS_DIR / av_app, av_app, mod) is not None:
                continue
            if av_val.startswith("$"):
                label = label_from_var(av_val, av_app)
            elif av_val.startswith("("):
                label = label_from_action(a, mod)
            else:
                label = _titleize(av_val)
            inline_by_app.setdefault(av_app, []).append(
                (combo_str(a, mod, key_map), label)
            )

    for av_app, app_entries in inline_by_app.items():
        if app_entries:
            w.emit(
                HELP_DIR / av_app / f"{av_app}_{fsafe(mod)}.hlp",
                f"{mod.capitalize()} - {av_app}",
                app_entries,
                find_actions_family_file(mod),
            )


def emit_domains_nav_help(w: HelpWriter, app_dirs: list[Path]) -> None:
    """The SPC-mods system's two-level navigation help:
    Level 1  global_domains.hlp       shown when holding spc
    Level 2  global_domains+{key}.hlp shown when holding spc + key
             {app}_domains+{key}      the same, inside a specific app
    """
    domains_iface = ACTIONS_DIR / "actions_domains.iface.kbd"
    if not domains_iface.exists():
        return
    _, iface_details = parse_iface(domains_iface)
    spc_entries, sublayers = parse_domains_layer()

    # Sub-domains worth walking: titled, and with at least one binding.
    walkable = [
        (key, title, sublayers[mod])
        for key, mod, title in spc_entries
        if title and sublayers.get(mod)
    ]

    # Level 2 first, so the level-1 overview can be filtered to the same
    # set — a mod with no real global default anywhere inside it (e.g.
    # Git, per-app only) shouldn't be advertised as globally available at
    # the top level either.
    overview: list[tuple[str, str]] = []
    for key, title, sub_actions in walkable:
        entries = [
            (
                f"spc + {key} + {sub_key}",
                label_from_global_default(
                    iface_details[action_name]["global"], action_name, "domains"
                ),
            )
            for sub_key, action_name in sub_actions
            if is_real_default(iface_details.get(action_name, {}).get("global"))
        ]
        if entries:
            overview.append((f"spc + {key}", title))
            w.emit(
                HELP_DIR / f"global_domains+{fsafe(key)}.hlp",
                title,
                entries,
                domains_iface,
            )

    if overview:
        w.emit(HELP_DIR / "global_domains.hlp", "Domains", overview, domains_iface)

    for app_dir in app_dirs:
        app = app_dir.name
        app_file = app_dir / f"{app}_domains.kbd"
        if not app_file.exists():
            continue
        impl = parse_app_file(app_file, app)
        app_overview: list[tuple[str, str]] = []

        for key, title, sub_actions in walkable:
            entries = [
                (
                    f"spc + {key} + {sub_key}",
                    label_for_implemented_value(
                        impl[action_name], action_name, "domains", app
                    ),
                )
                for sub_key, action_name in sub_actions
                if action_name in impl
            ]
            if entries:
                app_overview.append((f"spc + {key}", title))
                w.emit(
                    HELP_DIR / app / f"{app}_domains+{fsafe(key)}.hlp",
                    f"{title} - {app}",
                    entries,
                    app_file,
                )

        if app_overview:
            w.emit(
                HELP_DIR / app / f"{app}_domains.hlp",
                f"Domains - {app}",
                app_overview,
                app_file,
            )


def main(dry_run: bool = False) -> None:
    HELP_DIR.mkdir(exist_ok=True)
    w = HelpWriter(dry_run)

    layer_stacks = parse_layer_stacks()
    layers = reachable_layers(layer_stacks)
    app_dirs = sorted(d for d in ACTIONS_DIR.iterdir() if d.is_dir())

    # Group the reachable layers by the mod that owns them, then emit one
    # page per layer. "domains" is the one mod whose action names don't
    # mirror its layer names (action_domainT+T lives in domains+t_layer),
    # so it keeps its own two-level pass below.
    by_mod: dict[str, list[str]] = {}
    for layer in layers:
        by_mod.setdefault(layer_mod(layer), []).append(layer)

    for mod in sorted(by_mod):
        if mod == "domains" or not _actions_index().get(mod):
            continue
        emit_mod_help(w, mod, by_mod[mod], app_dirs, layer_stacks)

    emit_domains_nav_help(w, app_dirs)
    w.prune()


if __name__ == "__main__":
    main("--dry-run" in sys.argv)
