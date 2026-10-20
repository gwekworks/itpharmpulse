"""
Benmore mustache HTML → pure Django templates.

Adapted from ben-095-grantsync-django/scripts/convert_templates.py.
Transformations (same semantics):
  <page title="X" layout="none|app" auth="...">   → captured into meta
  </page>                                          → removed
  <query sql="..." as="name" (/|></query>)>        → removed
  <query from="t" ...> BODY </query>               → just BODY
  <include src="partials/x.html"/>                 → {% include "pharmacypulse/partials/x.html" %}
  {{^var}}...{{/var}}                              → {% if not var %}...{% endif %}
  {{#role "x"}}...{{/role}}                        → {% if user.role == "x" %}...{% endif %}
  {{#if x}}...{{/if}}                              → {% if x %}...{% endif %}
  {{#if x = "y"}}...{{/if}}                        → {% if x == "y" %}...{% endif %}
  {{#each xs}}...{{/each}}                         → {% for item in xs %}...{% endfor %}
  {{#var}}...{{/var}}                              → {% for itemN in var %}...{% endfor %} (bare fields → itemN.field)
  {{val | currency|date|ago}}                      → {{ val|currency|as_date|ago }}
  {{t "key"}}                                      → {% t "key" %}
  {{csrf_token}} and the hidden input              → {% csrf_token %}
"""
from __future__ import annotations
import json
import re
from pathlib import Path


SRC = Path("/Users/richardbuehling/Desktop/repos/pharmacypulse-prod-source")
DST = Path("/Users/richardbuehling/Desktop/repos/BEN-156-pharmacypulse-django/pharmacypulse/templates/pharmacypulse")

# Hand-maintained pages: don't overwrite
SKIP = set()

PAGE_OPEN_RE = re.compile(r"<page\b([^>]*)>", re.IGNORECASE)
PAGE_CLOSE_RE = re.compile(r"</page\s*>", re.IGNORECASE)
QUERY_RE = re.compile(r'<query\s+sql="[^"]+"\s+as="[^"]+"\s*(?:/>|>\s*</query\s*>)\s*', re.IGNORECASE)
QUERY_FROM_RE = re.compile(r'<query\s+from="[^"]+"[^>]*>(.*?)</query\s*>', re.IGNORECASE | re.DOTALL)
# Also strip multiline <query sql="..." as="..."> (sql can span lines in some Benmore dialects)
QUERY_MULTILINE_RE = re.compile(
    r'<query\s+sql="[^"]*"\s+as="[^"]+"\s*(?:/>|>\s*</query\s*>)\s*',
    re.IGNORECASE | re.DOTALL,
)
INCLUDE_RE = re.compile(r'<include\s+src="([^"]+)"\s*/>', re.IGNORECASE)

CSRF_HIDDEN_INPUT_RE = re.compile(
    r'<input\s+type="hidden"\s+name="\{\{csrf_name\}\}"\s+value="\{\{csrf(_token)?\}\}">'
)
CSRF_BARE_RE = re.compile(r"\{\{csrf_token\}\}")

FILTER_RE = re.compile(r"\{\{\s*([^{}|]+?)\s*\|\s*(\w+)(?::([^}]+?))?\s*\}\}")
FILTER_MAP = {"currency": "currency", "date": "as_date", "ago": "ago", "timeago": "timeago",
              "initials": "initials", "pct": "pct", "stars": "stars",
              "truncate": "truncate", "default": "default"}

ROLE_OPEN_RE = re.compile(r'\{\{#role\s+"([^"]+)"\}\}')
ROLE_CLOSE_RE = re.compile(r"\{\{/role\}\}")

# {{t "some.key"}} — handled before if/each because it's a tag, not a section
T_RE = re.compile(r'\{\{\s*t\s+"([^"]+)"\s*\}\}')

IF_OPEN_RE = re.compile(
    r'\{\{#if\s+([a-zA-Z0-9_.]+)(?:\s*(==|!=|>=|<=|=|>|<)\s*(?:"([^"]+)"|([0-9]+)))?\s*\}\}'
)
IF_CLOSE_RE = re.compile(r"\{\{/if\}\}")
IF_ELSE_RE = re.compile(r"\{\{else\}\}")
EACH_OPEN_RE = re.compile(r"\{\{#each\s+([a-zA-Z0-9_.]+)\}\}")
EACH_CLOSE_RE = re.compile(r"\{\{/each\}\}")

SECT_OPEN_RE = re.compile(r"\{\{#([a-zA-Z0-9_.]+)\}\}")
INV_OPEN_RE = re.compile(r"\{\{\^([a-zA-Z0-9_.]+)\}\}")
CLOSE_RE = re.compile(r"\{\{/([a-zA-Z0-9_.]+)\}\}")

RESERVED = {
    "user", "request", "csrf_token", "forloop", "block", "True", "False",
    "None", "item", "this", "empty", "now", "settings", "debug", "messages",
    "is_paginated", "brand", "i18n", "lang", "user_snapshot",
}


def convert_mustache_sections(text: str) -> str:
    out = []
    i = 0
    stack: list[tuple[str, str, str]] = []
    while i < len(text):
        m_sect = SECT_OPEN_RE.search(text, i)
        m_inv = INV_OPEN_RE.search(text, i)
        m_close = CLOSE_RE.search(text, i)
        candidates = []
        for m in (m_sect, m_inv, m_close):
            if m is None:
                continue
            var = m.group(1)
            if var in ("if", "each", "role"):
                continue
            candidates.append((m.start(), m))
        if not candidates:
            out.append(text[i:])
            break
        candidates.sort(key=lambda x: x[0])
        pos, m = candidates[0]
        out.append(text[i:pos])
        tok = m.group(0)

        if m is m_sect and tok.startswith("{{#"):
            var = m.group(1)
            depth = sum(1 for s in stack if s[0] == "section")
            loop_var = "item" if depth == 0 else f"item{depth+1}"
            stack.append(("section", var, loop_var))
            out.append(f"{{% for {loop_var} in {var} %}}")
        elif m is m_inv and tok.startswith("{{^"):
            var = m.group(1)
            stack.append(("empty", var, ""))
            out.append(f"{{% if not {var} %}}")
        else:
            if stack:
                kind, _, _ = stack.pop()
                out.append("{% endfor %}" if kind == "section" else "{% endif %}")
            else:
                out.append("")
        i = m.end()
    return "".join(out)


def rewrite_field_refs(text: str) -> str:
    out = []
    i = 0
    loop_stack: list[str] = []
    token_re = re.compile(
        r"(\{%\s*for\s+(\w+)\s+in\s+[\w.]+\s*%\})"
        r"|(\{%\s*endfor\s*%\})"
        # Match {{ ident }}, {{ ident|filter }}, and {{ ident|filter:arg }}
        # (arg may be quoted, numeric, or a bare word — anything up to }} ).
        r"|(\{\{\s*(?!\s*%)([a-zA-Z_][a-zA-Z0-9_]*)(\s*\|\s*[a-zA-Z0-9_]+(?::[^}]+?)?)?\s*\}\})"
    )
    while i < len(text):
        m = token_re.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        if m.group(1):
            loop_stack.append(m.group(2))
            out.append(m.group(1))
        elif m.group(3):
            if loop_stack:
                loop_stack.pop()
            out.append(m.group(3))
        else:
            tag = m.group(4)
            ident = m.group(5)
            flt = (m.group(6) or "").strip()
            if loop_stack and ident not in RESERVED:
                loop_var = loop_stack[-1]
                if flt:
                    out.append(f"{{{{ {loop_var}.{ident}{flt} }}}}")
                else:
                    out.append(f"{{{{ {loop_var}.{ident} }}}}")
            else:
                out.append(tag)
        i = m.end()
    return "".join(out)


def convert_body(text: str) -> str:
    text = PAGE_OPEN_RE.sub("", text, count=1)
    text = PAGE_CLOSE_RE.sub("", text)
    text = QUERY_MULTILINE_RE.sub("", text)
    text = QUERY_RE.sub("", text)
    text = QUERY_FROM_RE.sub(lambda m: m.group(1), text)
    text = INCLUDE_RE.sub(
        lambda m: '{% include "pharmacypulse/partials/' + Path(m.group(1)).name + '" %}', text
    )
    text = CSRF_HIDDEN_INPUT_RE.sub("{% csrf_token %}", text)
    text = CSRF_BARE_RE.sub("{% csrf_token %}", text)

    # {{t "key"}} -> {% t "key" %}
    text = T_RE.sub(lambda m: '{% t "' + m.group(1) + '" %}', text)

    def _filter(m):
        var, f, arg = m.group(1).strip(), m.group(2), m.group(3)
        f = FILTER_MAP.get(f, f)
        if arg is not None:
            arg = arg.strip()
            # Benmore accepts unquoted multi-word default values ({{x|default:Verified Patient}}).
            # Django requires quoted strings — wrap non-numeric args.
            if not arg.replace(".", "", 1).isdigit() and not (arg.startswith('"') or arg.startswith("'")):
                arg = '"' + arg.replace('"', '\\"') + '"'
            return f"{{{{ {var}|{f}:{arg} }}}}"
        return f"{{{{ {var}|{f} }}}}"
    text = FILTER_RE.sub(_filter, text)

    text = ROLE_OPEN_RE.sub(lambda m: f'{{% if user.role == "{m.group(1)}" %}}', text)
    text = ROLE_CLOSE_RE.sub("{% endif %}", text)

    def _if(m):
        name = m.group(1)
        op = m.group(2)
        strval = m.group(3)
        intval = m.group(4)
        if op is None:
            return f"{{% if {name} %}}"
        # Mustache uses single `=` for equality; Django uses `==`.
        django_op = {"=": "==", "==": "==", "!=": "!=",
                     ">": ">", "<": "<", ">=": ">=", "<=": "<="}.get(op, "==")
        if strval is not None:
            return f'{{% if {name} {django_op} "{strval}" %}}'
        return f"{{% if {name} {django_op} {intval} %}}"
    text = IF_OPEN_RE.sub(_if, text)
    text = IF_ELSE_RE.sub("{% else %}", text)
    text = IF_CLOSE_RE.sub("{% endif %}", text)

    text = EACH_OPEN_RE.sub(lambda m: f"{{% for item in {m.group(1)} %}}", text)
    text = EACH_CLOSE_RE.sub("{% endfor %}", text)

    text = convert_mustache_sections(text)
    text = rewrite_field_refs(text)
    return text


def parse_page_attrs(text: str) -> dict:
    m = PAGE_OPEN_RE.search(text)
    if not m:
        return {}
    attrs = {}
    for am in re.finditer(r'(\w+)="([^"]*)"', m.group(0)):
        attrs[am.group(1)] = am.group(2)
    return attrs


def wrap_page(body: str, meta: dict) -> str:
    layout = meta.get("layout", "app")
    title = meta.get("title", "PharmacyPulse")
    if layout == "none":
        extend = '{% extends "pharmacypulse/layouts/blank.html" %}'
    else:
        extend = '{% extends "pharmacypulse/layouts/app.html" %}'
    return (
        extend + "\n"
        "{% load pharmacypulse %}\n"
        f"{{% block title %}}{title}{{% endblock %}}\n"
        "{% block content %}\n"
        + body.strip()
        + "\n{% endblock %}\n"
    )


def convert_partial(path: Path) -> str:
    text = convert_body(path.read_text(encoding="utf-8"))
    return "{% load pharmacypulse %}\n" + text


def convert_layout_file(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    text = INCLUDE_RE.sub(
        lambda m: '{% include "pharmacypulse/partials/' + Path(m.group(1)).name + '" %}', text
    )
    text = text.replace("{{content}}", "{% block content %}{% endblock %}")
    text = CSRF_HIDDEN_INPUT_RE.sub("{% csrf_token %}", text)
    return text


def main():
    manifest = {}
    # Partials
    partials_dir = SRC / "partials"
    if partials_dir.exists():
        for p in partials_dir.glob("*.html"):
            (DST / "partials" / p.name).write_text(convert_partial(p), encoding="utf-8")
    if (SRC / "head.html").exists():
        (DST / "partials" / "head.html").write_text(
            convert_partial(SRC / "head.html"), encoding="utf-8",
        )
    # Pages (top-level HTML)
    for p in sorted(SRC.glob("*.html")):
        if p.name in SKIP:
            continue
        text = p.read_text(encoding="utf-8")
        meta = parse_page_attrs(text)
        body = convert_body(text)
        (DST / "pages" / p.name).write_text(wrap_page(body, meta), encoding="utf-8")
        manifest[p.name] = meta
    (DST / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Converted {len(manifest)} pages (skipped: {sorted(SKIP)}).")


if __name__ == "__main__":
    main()
