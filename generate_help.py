#!/usr/bin/env python3
"""
generate_help.py - Generate .hlp help files from kanata action files.

Follows the source-of-truth chain:
  layers/layer_{domain}.kbd          → which hold combos are actually mapped
  actions/actions_{domain}.iface.kbd → per-app dispatch + global default
  actions/actions_{domain}.kbd       → non-iface domains (bookmarks, apps, …)
  actions/{app}/{app}_{domain}.kbd   → per-app variable labels

Generates:
  help/global_{domain_short}.hlp  - combos with a real global default (not XX/push-msg)
  help/{app}_{domain_short}.hlp   - combos the app actually implements (not push-msg)

Usage:
  python3 generate_help.py [--dry-run]
"""
import re
import sys
from pathlib import Path

ROOT        = Path(__file__).parent
ACTIONS_DIR = ROOT / "actions"
LAYERS_DIR  = ROOT / "layers"
HELP_DIR    = ROOT / "help"

DOMAIN_TITLES: dict[str, str] = {
    "domains":      "SPC mods",
    "panes":        "Panes",
    "tabs":         "Tabs",
    "groups":       "Groups",
    "sessions":     "Sessions",
    "omni":         "Omni",
    "open":         "Open",
    "replace":      "Replace",
    "search":       "Search",
    "seek_n_select": "Seek & Select",
    "physical_mods": "Physical mods",
    "opts":         "Opts",
    "bookmarks":    "Bookmarks",
    "apps":         "Apps",
    "workspaces":   "Workspaces",
    "windows":      "Windows",
    "lang":         "Lang",
    "layouts":      "Layouts",
}

DOMAIN_SHORT: dict[str, str] = {
    "seek_n_select": "seek",
    "physical_mods": "phys",
}


# ── helpers ──────────────────────────────────────────────────────────────────

def domain_from_actions_path(path: Path) -> str:
    name = path.name.removeprefix("actions_")
    if ".iface." in name:
        return name.removesuffix(".iface.kbd")
    return name.removesuffix(".kbd")


def domain_short(domain: str) -> str:
    return DOMAIN_SHORT.get(domain, domain)


def combo_str(action_name: str, domain: str,
              key_map: dict[str, str] | None = None) -> str:
    """Convert an action name to a 'spc + e + e' style combo string.

    Prefers the physical key from key_map (built from the layer file) over
    the semantic name embedded in the action name, so the combo reflects what
    the user actually presses (e.g. 'tabs + h' instead of 'tabs + prev').

    domains is handled separately (action prefix ≠ domain name).
    Falls back to name-parsing when key_map has no entry.
    """
    dk   = domain_short(domain)
    name = action_name.removeprefix("action_")

    if domain == "domains":
        parts = name.split("+")
        keys  = [p.removeprefix("mod").lower() for p in parts]
        if len(keys) == 2 and keys[0] == keys[1]:
            return f"spc + {keys[0]} + {keys[0]}"
        return "spc + " + " + ".join(keys)

    # Physical key from layer file takes priority over action-name semantics
    if key_map and action_name in key_map:
        return dk + " + " + key_map[action_name]

    # Fallback: derive key from action name, splitting on "+" for readable display
    prefix_plus  = f"{domain}+"
    prefix_under = f"{domain}_"
    if name.startswith(prefix_plus):
        sub = name[len(prefix_plus):]
        return dk + " + " + " + ".join(sub.split("+"))
    if name.startswith(prefix_under):
        sub = name[len(prefix_under):]
        return dk + " + " + " + ".join(sub.split("_"))
    return dk + " + " + " + ".join(name.split("+"))


def label_from_var(var_name: str, app: str) -> str:
    """'$nvim_action_page_up' → 'Page Up', '$folder_trash' → 'Trash'."""
    var = var_name.lstrip("$").lstrip("~")
    for prefix in [f"{app}_action_", f"{app}_", "folder_", "action_"]:
        if var.startswith(prefix):
            var = var[len(prefix):]
            break
    return var.replace("_", " ").replace("+", " ").title()


def label_from_action(action_name: str, domain: str) -> str:
    """Fallback label derived from action name when no $variable is available."""
    name         = action_name.removeprefix("action_")
    prefix_plus  = f"{domain}+"
    prefix_under = f"{domain}_"
    if name.startswith(prefix_plus):
        sub = name[len(prefix_plus):]
    elif name.startswith(prefix_under):
        sub = name[len(prefix_under):]
    else:
        sub = name
    return sub.replace("_", " ").replace("+", " ").title()


def label_from_global_default(default: str, action_name: str, domain: str) -> str:
    """Human-readable label from a global default value.

    '(push-msg "APP:claude_code")' → 'Claude Code'
    '(macro ...)'                  → derives from action name
    '$action_tabs_close'           → 'Tabs Close'  (via label_from_action)
    '$nvim_some_var'               → 'Some Var'    (strip $ + app prefix)
    'left'                         → 'Left'
    'C-c'                          → 'C-C'
    """
    # push-msg "APP:name" → app name as label (split camelCase + underscores)
    app_m = re.search(r'"APP:([^"]+)"', default)
    if app_m:
        name = app_m.group(1)
        name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)  # camelCase → words
        return name.replace("_", " ").title()
    # push-msg "NOTIFY:label" → strip trailing punctuation then title-case
    notify_m = re.search(r'"NOTIFY:\s*([^"]+)"', default)
    if notify_m:
        label = re.sub(r'\.+$', '', notify_m.group(1).strip())
        return label.title()
    # Complex kanata expression → derive label from action name
    if default.startswith("("):
        return label_from_action(action_name, domain)
    clean = default.lstrip("$").lstrip("~")
    if clean.startswith("action_"):
        return label_from_action(clean, domain)
    if default.startswith("$"):
        # Strip domain prefix (e.g. "$apps_app_finder" + domain "apps" → "App Finder")
        domain_pfx = f"{domain}_"
        if clean.startswith(domain_pfx):
            clean = clean[len(domain_pfx):]
        return clean.replace("_", " ").replace("+", " ").title()
    # Simple kanata key token (lrld, prnt, S-prnt, RS-spc, …) → derive from action name
    if re.match(r'^[A-Za-z][-A-Za-z0-9]*$', default):
        return label_from_action(action_name, domain)
    return default.replace("_", " ").replace("+", " ").title()


def is_real_default(value: str | None) -> bool:
    """True when value is a usable global default — not None, XX, or unset push-msg."""
    if not value:
        return False
    if value.upper() == "XX":
        return False
    if value.startswith("(push-msg"):
        app_m    = re.search(r'"APP:([^"]+)"',       value)
        notify_m = re.search(r'"NOTIFY:\s*([^"]+)"', value)
        return bool((app_m and app_m.group(1).strip()) or
                    (notify_m and notify_m.group(1).strip()))
    return True


def is_implemented(value: str) -> bool:
    """True when a per-app binding is a real implementation, not a no-op or push-msg marker."""
    if value.upper() == "XX":
        return False
    if value.startswith("(push-msg"):
        return False
    return True


# ── parsers ───────────────────────────────────────────────────────────────────

def parse_iface(path: Path) -> tuple[list[str], dict[str, dict]]:
    """Parse an actions .kbd file (iface or plain).

    Returns:
      - ordered list of action names as they appear in the file
      - details dict: {action_name: {
            'apps':       [str],        # vk_* app names from switch conditions
            'global':     str | None,   # () catch-all value
            'direct':     str | None,   # direct binding value (no switch)
            'app_values': {app: var},   # inline per-app values (bookmarks-style)
        }}
    """
    text    = path.read_text()
    order:   list[str]       = []
    details: dict[str, dict] = {}

    header_re = re.compile(r"^\s{2}(action_\S+)", re.MULTILINE)
    headers   = list(header_re.finditer(text))

    for i, m in enumerate(headers):
        name        = m.group(1)
        block_start = m.start()
        block_end   = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block       = text[block_start:block_end]

        order.append(name)
        # Strip commented-out lines before all pattern searches so that
        # commented-out alternatives (e.g. ";; () (push-msg ...)") don't shadow
        # the real binding on the next line.
        block_nc = re.sub(r'^\s*;;.*$', '', block, flags=re.MULTILINE)

        apps = re.findall(r"\(\(input virtual vk_(\w+)\)\)", block_nc)

        # Catch-all value: capture full push-msg expression or simple token
        global_m = re.search(
            r'\(\)\s+(\(push-msg\s+"[^"]*"\)|\S+)\s+break',
            block_nc,
        )
        global_val = global_m.group(1) if global_m else None

        # Direct binding fallback for non-switch actions
        direct_val = None
        if global_val is None and "switch" not in block_nc:
            # (t! unmod_all KEY)
            dt = re.search(r'action_\S+\s+\(t!\s+unmod_all\s+([^\s)]+)', block_nc)
            if dt:
                direct_val = dt.group(1)
            else:
                # action_name $var
                dt2 = re.search(r'action_\S+\s+(\$\S+)', block_nc)
                if dt2:
                    direct_val = dt2.group(1)
                else:
                    # action_name KEY (any simple token: S-prnt, lrld, prnt, RS-spc, …)
                    dt3 = re.search(r'action_\S+\s+([^\s()\n]+)', block_nc)
                    if dt3:
                        direct_val = dt3.group(1)
            if direct_val is None:
                # Complex expression: prefer NOTIFY: push-msg label when present
                notify_m = re.search(r'\(push-msg\s+"(NOTIFY:[^"]+)"\)', block_nc)
                if notify_m:
                    direct_val = f'(push-msg "{notify_m.group(1)}")'
                elif re.search(r'action_\S+\s+\(', block_nc):
                    # Any remaining complex binding → signal label_from_action
                    direct_val = "(action)"

        # Inline per-app values for bookmarks-style files (no separate app file)
        app_values: dict[str, str] = {}
        for av_m in re.finditer(
            r'\(\(input virtual vk_(\w+)\)\)\s+(.+?)\s+break',
            block_nc, re.DOTALL,
        ):
            av_app  = av_m.group(1)
            av_expr = av_m.group(2).strip()
            if av_expr.startswith("(push-msg") or av_expr.upper() == "XX":
                continue
            var_ref = re.search(r'\$(\w+)', av_expr)
            if var_ref:
                app_values[av_app] = "$" + var_ref.group(1)
            else:
                app_values[av_app] = av_expr

        details[name] = {
            "apps":       apps,
            "global":     global_val,
            "direct":     direct_val,
            "app_values": app_values,
        }

    return order, details


def parse_layer_actions(
    domain: str, iface_action_set: set[str]
) -> tuple[list[str], dict[str, str]]:
    """Return (ordered_actions, key_map).

    ordered_actions: action names from the layer file, in file order,
                     filtered to those defined in the iface.
    key_map:         {action_name: key_combo_suffix} — physical key path for
                     each action, e.g. "h", "lsft + t", "move + h".
                     Built from the first key that maps to each action in the
                     layer file, so the combo shows what the user actually presses.

    Returns ([], {}) when the layer file is absent (caller falls back to iface order).
    """
    layer_path = LAYERS_DIR / f"layer_{domain}.kbd"
    if not layer_path.exists():
        return [], {}

    text = layer_path.read_text()
    seen: set[str]           = set()
    order: list[str]         = []
    key_map: dict[str, str]  = {}
    current_mod: str | None  = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(";;"):
            continue

        # Track which deflayermap block we are in and extract its modifier
        dlm_m = re.match(r"\(deflayermap\s+\((\S+)\)", stripped)
        if dlm_m:
            layer_name = dlm_m.group(1)
            if layer_name == f"{domain}_layer":
                current_mod = None
            elif layer_name.startswith(f"{domain}+"):
                suffix      = layer_name[len(f"{domain}+"):]
                current_mod = suffix.removesuffix("_layer")
            else:
                current_mod = None   # unrelated layer (e.g. from another domain)
            continue

        if "$action_" not in stripped:
            continue

        # Physical key is the first token on the line
        parts   = stripped.split()
        raw_key = parts[0] if parts else ""

        # Skip template calls (but don't expand them — use iface_order for per-app)
        if not raw_key or raw_key.startswith("("):
            continue

        # Resolve $var|alias style keys: "$k|rsft" → "k", "$toggle" → "toggle"
        # Skip keys starting with "!" (complex physical-mod aliases)
        if raw_key.startswith("$"):
            base = raw_key[1:].split("|")[0]
            if not base or base.startswith("!"):
                continue
            physical_key = base
        else:
            physical_key = raw_key

        # First $action_* reference on the line
        action_m = re.search(r"\$(action_\S+)", stripped)
        if not action_m:
            continue
        action = action_m.group(1)
        if action not in iface_action_set:
            continue

        if action not in seen:
            seen.add(action)
            order.append(action)

        # Record the first physical key that triggers this action
        if action not in key_map:
            suffix = f"{current_mod} + {physical_key}" if current_mod else physical_key
            key_map[action] = suffix

    return order, key_map


def parse_app_file(path: Path, app: str) -> dict[str, str]:
    """Return {action_name: first_value_token} for implemented actions only.

    Skips:
      - commented-out lines (starting with ;;)
      - (push-msg ...) not-implemented markers
    """
    text    = path.read_text()
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

def parse_domains_layer() -> tuple[
    list[tuple[str, str, str]],          # spc_entries: [(phys_key, mod_name, title)]
    dict[str, list[tuple[str, str]]],    # sublayers:   {mod_name: [(sub_key, action)]}
]:
    """Parse layer_domains.kbd into:
      spc_entries  — ordered (phys_key, mod_name, domain_title) for each key in domains_layer
      sublayers    — {mod_name: [(sub_key, action_name)]} for each spc+mod*_layer
    """
    path = LAYERS_DIR / "layer_domains.kbd"
    text = path.read_text()

    layer_titles: dict[str, str]                  = {}
    sublayers:    dict[str, list[tuple[str, str]]] = {}
    domains_raw:  list[tuple[str, str]]            = []  # (phys_key, mod_name)

    current_ctx  = None   # "domains" | mod_name | None
    pending_cmnt = ""

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue

        # Close of a deflayermap block: lone ")" possibly followed by a comment
        if re.match(r'^\)\s*(;.*)?$', s):
            current_ctx = None
            continue

        if s.startswith(";;"):
            c = s.lstrip(";").strip()
            c = re.sub(r'\s*;;.*$', '', c).strip()
            pending_cmnt = c
            continue

        dlm = re.match(r'\(deflayermap\s+\((\S+)\)', s)
        if dlm:
            lname = dlm.group(1)
            if lname == "domains_layer":
                current_ctx = "domains"
            elif lname.startswith("domains+") and lname.endswith("_layer"):
                mod = lname[8:-6]           # strip "domains+" prefix and "_layer" suffix
                layer_titles[mod] = pending_cmnt
                sublayers.setdefault(mod, [])
                current_ctx = mod
            else:
                current_ctx = None
            pending_cmnt = ""
            continue

        # Data line inside a block
        pending_cmnt = ""

        if current_ctx == "domains":
            key_m = re.match(r'^(\S+)', s)
            vk_m  = re.search(r'press-vkey vk_([^\s)]+)', s)
            if key_m and vk_m:
                raw_key  = key_m.group(1)
                mod_name = vk_m.group(1)
                if raw_key.startswith("$"):
                    base = raw_key[1:].split("|")[0]
                    phys = base[3:] if base.startswith("mod") and len(base) > 3 else base
                else:
                    phys = raw_key
                # vk name is like "domains+A"; normalize to "a" to match sublayer keys
                mod_key = mod_name.split("+", 1)[-1].lower() if "+" in mod_name else mod_name.lower()
                domains_raw.append((phys, mod_key))

        elif current_ctx is not None:
            if s.startswith("_"):
                continue
            m = re.match(r'^(\S+)\s+(\$action_\S+)', s)
            if not m:
                continue
            raw_key = m.group(1)
            action  = m.group(2).lstrip("$")
            if raw_key.startswith("$"):
                base = raw_key[1:].split("|")[0]
                phys = base[3:] if base.startswith("mod") and len(base) > 3 else base
            else:
                phys = raw_key
            sublayers[current_ctx].append((phys, action))

    spc_entries = [
        (phys, mod, layer_titles.get(mod, ""))
        for phys, mod in domains_raw
    ]
    return spc_entries, sublayers


# ── formatting ────────────────────────────────────────────────────────────────

def format_hlp(title: str, entries: list[tuple[str, str]], source_file: Path | None = None) -> str:
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

def main(dry_run: bool = False) -> None:
    HELP_DIR.mkdir(exist_ok=True)

    iface_files = sorted(ACTIONS_DIR.glob("actions_*.iface.kbd"))
    plain_files = sorted(
        f for f in ACTIONS_DIR.glob("actions_*.kbd")
        if ".iface." not in f.name
    )
    # domains is handled separately (two-level navigation structure)
    all_files = [
        f for f in iface_files + plain_files
        if domain_from_actions_path(f) != "domains"
    ]
    app_dirs  = sorted(d for d in ACTIONS_DIR.iterdir() if d.is_dir())

    written: set[Path] = set()

    for actions_path in all_files:
        domain = domain_from_actions_path(actions_path)
        short  = domain_short(domain)
        title  = DOMAIN_TITLES.get(domain, domain.replace("_", " ").title())

        iface_order, details = parse_iface(actions_path)
        iface_set = set(iface_order)

        # Layer file provides the physical-key map for accurate combo strings.
        # Action order always follows the actions file (iface_order).
        _, key_map = parse_layer_actions(domain, iface_set)
        actions = iface_order

        # ── Global help ───────────────────────────────────────────────────────
        # Only combos with a real global default (or a direct binding)
        global_entries: list[tuple[str, str]] = []
        for a in actions:
            d       = details.get(a, {})
            g       = d.get("global")
            direct  = d.get("direct")
            effective = g if is_real_default(g) else (direct if is_real_default(direct) else None)
            if effective is None:
                continue
            global_entries.append((
                combo_str(a, domain, key_map),
                label_from_global_default(effective, a, domain),
            ))

        if global_entries:
            out = HELP_DIR / f"global_{short}.hlp"
            written.add(out)
            print(f"  {'[dry]' if dry_run else 'wrote'} {out.relative_to(ROOT)}")
            if not dry_run:
                out.write_text(format_hlp(title, global_entries, actions_path))

        # ── Per-app help from separate app files ──────────────────────────────
        # Use iface_order (not layer_actions) so that domains where the layer
        # uses templates (e.g. physical_mods) still expose all overridden actions.
        for app_dir in app_dirs:
            app      = app_dir.name
            app_file = app_dir / f"{app}_{domain}.kbd"
            if not app_file.exists():
                continue

            impl    = parse_app_file(app_file, app)
            entries: list[tuple[str, str]] = []
            for a in iface_order:
                if a not in impl:
                    continue
                value = impl[a]
                if value.startswith("$"):
                    label = label_from_var(value, app)
                elif value.startswith("("):
                    label = label_from_action(a, domain)
                else:
                    label = label_from_global_default(value, a, domain)
                entries.append((combo_str(a, domain, key_map), label))

            if not entries:
                continue

            (HELP_DIR / app).mkdir(exist_ok=True)
            out = HELP_DIR / app / f"{app}_{short}.hlp"
            written.add(out)
            print(f"  {'[dry]' if dry_run else 'wrote'} {out.relative_to(ROOT)}")
            if not dry_run:
                out.write_text(format_hlp(f"{title} - {app}", entries, app_file))

        # ── Per-app help from inline values ───────────────────────────────────
        # For non-iface files (bookmarks, …) where per-app implementations are
        # embedded in the main actions file rather than separate app files.
        inline_by_app: dict[str, list[tuple[str, str]]] = {}
        for a in actions:
            avs = details.get(a, {}).get("app_values", {})
            for av_app, av_val in avs.items():
                # Skip if a separate app file already handles this domain
                if (ACTIONS_DIR / av_app / f"{av_app}_{domain}.kbd").exists():
                    continue
                if av_val.startswith("$"):
                    label = label_from_var(av_val, av_app)
                elif av_val.startswith("("):
                    label = label_from_action(a, domain)
                else:
                    label = av_val.replace("_", " ").replace("+", " ").title()
                inline_by_app.setdefault(av_app, []).append(
                    (combo_str(a, domain, key_map), label)
                )

        for av_app, app_entries in inline_by_app.items():
            if not app_entries:
                continue
            (HELP_DIR / av_app).mkdir(exist_ok=True)
            out = HELP_DIR / av_app / f"{av_app}_{short}.hlp"
            written.add(out)
            print(f"  {'[dry]' if dry_run else 'wrote'} {out.relative_to(ROOT)}")
            if not dry_run:
                out.write_text(format_hlp(f"{title} - {av_app}", app_entries, actions_path))

    # ── Domains: two-level navigation ────────────────────────────────────────
    # Level 1  domains.hlp          shown when holding spc
    # Level 2  domains+{key}.hlp    shown when holding spc + key (global default)
    #          {app}_domains+{key}  shown when holding spc + key in a specific app
    domains_iface = ACTIONS_DIR / "actions_domains.iface.kbd"
    if domains_iface.exists():
        _, iface_details = parse_iface(domains_iface)
        spc_entries, sublayers = parse_domains_layer()

        # Level 1 — domains.hlp: spc + key → domain title
        overview = [
            (f"spc + {key}", title)
            for key, mod, title in spc_entries
            if title
        ]
        if overview:
            out = HELP_DIR / "domains.hlp"
            written.add(out)
            print(f"  {'[dry]' if dry_run else 'wrote'} {out.relative_to(ROOT)}")
            if not dry_run:
                out.write_text(format_hlp("Domains", overview, domains_iface))

        # Level 2 — one file per sub-domain key
        # "/" can't appear in filenames; replace problematic chars
        def fsafe(k: str) -> str:
            return k.replace("/", "slash").replace("\\", "bslash")

        for key, mod, title in spc_entries:
            if not title or mod not in sublayers or not sublayers[mod]:
                continue
            sub_actions = sublayers[mod]

            # Global: domains+{key}.hlp
            global_entries: list[tuple[str, str]] = []
            for sub_key, action_name in sub_actions:
                g = iface_details.get(action_name, {}).get("global")
                if is_real_default(g):
                    global_entries.append((
                        sub_key,
                        label_from_global_default(g, action_name, "domains"),
                    ))
            fname = fsafe(key)
            if global_entries:
                out = HELP_DIR / f"domains+{fname}.hlp"
                written.add(out)
                print(f"  {'[dry]' if dry_run else 'wrote'} {out.relative_to(ROOT)}")
                if not dry_run:
                    out.write_text(format_hlp(title, global_entries, domains_iface))

        # Per-app: help/{app}/domains.hlp (overview) + help/{app}/domains+{key}.hlp
        for app_dir in app_dirs:
            app      = app_dir.name
            app_file = app_dir / f"{app}_domains.kbd"
            if not app_file.exists():
                continue
            impl         = parse_app_file(app_file, app)
            app_hlp_dir  = HELP_DIR / app
            app_overview: list[tuple[str, str]] = []

            for key, mod, title in spc_entries:
                if not title or mod not in sublayers or not sublayers[mod]:
                    continue
                sub_actions = sublayers[mod]
                fname = fsafe(key)

                entries: list[tuple[str, str]] = []
                for sub_key, action_name in sub_actions:
                    if action_name not in impl:
                        continue
                    value = impl[action_name]
                    if value.startswith("$"):
                        label = label_from_var(value, app)
                    elif value.startswith("("):
                        label = label_from_action(action_name, "domains")
                    else:
                        label = label_from_global_default(value, action_name, "domains")
                    entries.append((sub_key, label))

                if entries:
                    app_overview.append((f"spc + {key}", title))
                    app_hlp_dir.mkdir(exist_ok=True)
                    out = app_hlp_dir / f"{app}_domains+{fname}.hlp"
                    written.add(out)
                    print(f"  {'[dry]' if dry_run else 'wrote'} {out.relative_to(ROOT)}")
                    if not dry_run:
                        out.write_text(format_hlp(f"{title} - {app}", entries, app_file))

            if app_overview:
                app_hlp_dir.mkdir(exist_ok=True)
                out = app_hlp_dir / f"{app}_domains.hlp"
                written.add(out)
                print(f"  {'[dry]' if dry_run else 'wrote'} {out.relative_to(ROOT)}")
                if not dry_run:
                    out.write_text(format_hlp(f"Domains - {app}", app_overview, app_file))

    # Remove stale .hlp files (root and all app subdirectories)
    for stale in sorted(HELP_DIR.glob("**/*.hlp")):
        if stale not in written:
            print(f"  {'[dry]' if dry_run else 'removed'} {stale.relative_to(ROOT)}")
            if not dry_run:
                stale.unlink()
    # Remove empty app subdirectories
    if not dry_run:
        for subdir in sorted(HELP_DIR.iterdir()):
            if subdir.is_dir() and not any(subdir.glob("*.hlp")):
                subdir.rmdir()


if __name__ == "__main__":
    main("--dry-run" in sys.argv)
