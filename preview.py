#!/usr/bin/env python3
"""
Local preview for this Jekyll site — no gems, no bundler, no CI.

GitHub Pages builds the real site with Jekyll whenever you push. This script
only exists so you can look at the page on your own machine first, using
nothing but the Python 3 standard library:

    python3 preview.py                 # serve on http://127.0.0.1:4000
    python3 preview.py --port 8080     # pick another port
    python3 preview.py --build         # render the site into _site/ and exit
    python3 preview.py --check         # validate the data files and exit

It understands the small subset of Jekyll that this site actually uses:

    _config.yml                  -> site.*
    _data/*.yml                  -> site.data.*
    _layouts/<layout>.html       -> the page shell, with {{ content }}
    YAML front matter in pages   -> page.*
    Liquid                       -> {{ output }}, {% assign %}, {% for %},
                                    {% if / elsif / else %} with and, or,
                                    not, ==, !=, >, <, >=, <=, contains,
                                    and the filters listed in FILTERS below.

Anything outside that subset raises a clear error instead of quietly
rendering something different from what GitHub Pages will produce.
"""

from __future__ import annotations

import argparse
import html
import http.server
import mimetypes
import os
import re
import shutil
import socketserver
import sys
import urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "_data")
PAGES = ("index.html", "404.html")
ASSET_DIRS = ("assets",)


class PreviewError(Exception):
    """Raised for anything this preview cannot render faithfully."""


# ==========================================================================
# A small YAML reader: block mappings, block sequences, flow sequences/maps,
# quoted and plain scalars. The data files deliberately stay inside this
# subset, which is ordinary, valid YAML for Jekyll as well.
# ==========================================================================

QUOTES = "\"'"
NULL_WORDS = ("", "~", "null", "Null", "NULL")


def _strip_comment(line):
    """Remove a trailing '# comment', ignoring '#' inside quotes."""
    out, quote, index = [], None, 0
    while index < len(line):
        char = line[index]
        if quote:
            out.append(char)
            if quote == '"' and char == "\\" and index + 1 < len(line):
                out.append(line[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in QUOTES:
            quote = char
            out.append(char)
        elif char == "#" and (not out or out[-1] in " \t"):
            break
        else:
            out.append(char)
        index += 1
    return "".join(out).rstrip()


def _split_flow(text, separator=","):
    """Split on `separator`, ignoring separators inside quotes or brackets."""
    parts, buffer, quote, depth = [], [], None, 0
    for char in text:
        if quote:
            buffer.append(char)
            if char == quote:
                quote = None
        elif char in QUOTES:
            quote = char
            buffer.append(char)
        elif char in "[{":
            depth += 1
            buffer.append(char)
        elif char in "]}":
            depth -= 1
            buffer.append(char)
        elif char == separator and depth == 0:
            parts.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)
    parts.append("".join(buffer).strip())
    return [part for part in parts if part != ""]


def _scalar(text):
    text = text.strip()
    if text.startswith("["):
        if not text.endswith("]"):
            raise PreviewError("unterminated flow sequence: %s" % text)
        return [_scalar(item) for item in _split_flow(text[1:-1])]
    if text.startswith("{"):
        if not text.endswith("}"):
            raise PreviewError("unterminated flow mapping: %s" % text)
        mapping = {}
        for item in _split_flow(text[1:-1]):
            key, _, value = item.partition(":")
            mapping[_scalar(key)] = _scalar(value)
        return mapping
    if text[:1] == '"':
        if text[-1:] != '"':
            raise PreviewError("unterminated double-quoted string: %s" % text)
        body = text[1:-1]
        for escape, replacement in (("\\n", "\n"), ("\\t", "\t"), ('\\"', '"'), ("\\\\", "\\")):
            body = body.replace(escape, replacement)
        return body
    if text[:1] == "'":
        if text[-1:] != "'":
            raise PreviewError("unterminated single-quoted string: %s" % text)
        return text[1:-1].replace("''", "'")
    if text in NULL_WORDS:
        return None
    if text in ("true", "True", "yes"):
        return True
    if text in ("false", "False", "no"):
        return False
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def _is_mapping_line(text):
    return bool(re.match(r"^[^:#\"']+:(?:\s|$)", text))


def load_yaml(path):
    with open(path, encoding="utf-8") as handle:
        raw = handle.read()

    lines = []
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip() or line.strip().startswith("#"):
            continue
        content = _strip_comment(line)
        if not content.strip():
            continue
        stripped = content.strip()
        if stripped in ("|", ">", "|-", ">-") or stripped.endswith((" |", " >", " |-", " >-")):
            raise PreviewError(
                "%s:%d uses a block scalar, which this preview does not read; "
                "use a single quoted line instead." % (path, number)
            )
        lines.append((len(content) - len(content.lstrip(" ")), stripped, number))

    if not lines:
        return None
    value, index = _parse_block(lines, 0, lines[0][0], path)
    if index != len(lines):
        raise PreviewError("%s:%d could not be parsed" % (path, lines[index][2]))
    return value


def _parse_block(lines, index, indent, path):
    if lines[index][1] == "-" or lines[index][1].startswith("- "):
        return _parse_sequence(lines, index, indent, path)
    return _parse_mapping(lines, index, indent, path)


def _parse_sequence(lines, index, indent, path):
    items = []
    while index < len(lines):
        level, text, number = lines[index]
        if level != indent or not (text == "-" or text.startswith("- ")):
            if level > indent:
                raise PreviewError("%s:%d is indented unexpectedly" % (path, number))
            break
        rest = text[2:].strip() if text.startswith("- ") else ""
        index += 1
        if not rest:
            if index < len(lines) and lines[index][0] > indent:
                value, index = _parse_block(lines, index, lines[index][0], path)
            else:
                value = None
            items.append(value)
        elif _is_mapping_line(rest):
            value, index = _parse_mapping(lines, index, indent + 2, path, first_line=rest)
            items.append(value)
        else:
            items.append(_scalar(rest))
    return items, index


def _parse_mapping(lines, index, indent, path, first_line=None):
    mapping = {}
    pending = first_line
    while True:
        if pending is not None:
            text, number = pending, 0
            pending = None
        else:
            if index >= len(lines):
                break
            level, text, number = lines[index]
            if level != indent:
                if level > indent:
                    raise PreviewError("%s:%d is indented unexpectedly" % (path, number))
                break
            if text == "-" or text.startswith("- "):
                break
            index += 1

        key, separator, rest = text.partition(":")
        if not separator:
            raise PreviewError("%s:%d is missing a ':'" % (path, number))
        key = key.strip().strip(QUOTES)
        rest = rest.strip()
        if rest:
            value = _scalar(rest)
        elif index < len(lines) and lines[index][0] > indent:
            value, index = _parse_block(lines, index, lines[index][0], path)
        elif (
            index < len(lines)
            and lines[index][0] == indent
            and (lines[index][1] == "-" or lines[index][1].startswith("- "))
        ):
            value, index = _parse_sequence(lines, index, indent, path)
        else:
            value = None
        mapping[key] = value
    return mapping, index


# ==========================================================================
# Liquid: parsing into a tiny node tree.
# ==========================================================================

TOKEN_RE = re.compile(r"\{\{(.*?)\}\}|\{%(.*?)%\}", re.S)


class Text:
    def __init__(self, text):
        self.text = text

    def render(self, ctx, out):
        out.append(self.text)


class Output:
    def __init__(self, expression):
        self.expression = expression

    def render(self, ctx, out):
        value = evaluate(self.expression, ctx)
        out.append("" if value is None else str(value))


class Assign:
    def __init__(self, name, expression):
        self.name = name
        self.expression = expression

    def render(self, ctx, out):
        ctx[self.name] = evaluate(self.expression, ctx)


class For:
    def __init__(self, variable, sequence, body, else_body):
        self.variable = variable
        self.sequence = sequence
        self.body = body
        self.else_body = else_body

    def render(self, ctx, out):
        sequence = evaluate(self.sequence, ctx)
        if isinstance(sequence, dict):
            items = [[key, value] for key, value in sequence.items()]
        elif isinstance(sequence, (list, tuple)):
            items = list(sequence)
        elif sequence is None:
            items = []
        else:
            raise PreviewError("cannot loop over %r" % (sequence,))

        if not items:
            render_nodes(self.else_body, ctx, out)
            return

        total = len(items)
        for position, item in enumerate(items):
            scope = dict(ctx)
            scope[self.variable] = item
            scope["forloop"] = {
                "index": position + 1,
                "index0": position,
                "rindex": total - position,
                "rindex0": total - position - 1,
                "first": position == 0,
                "last": position == total - 1,
                "length": total,
            }
            render_nodes(self.body, scope, out)


class If:
    def __init__(self, branches, else_body, negate_first=False):
        self.branches = branches
        self.else_body = else_body
        self.negate_first = negate_first

    def render(self, ctx, out):
        for position, (condition, body) in enumerate(self.branches):
            truth = truthy(evaluate_condition(condition, ctx))
            if position == 0 and self.negate_first:
                truth = not truth
            if truth:
                render_nodes(body, ctx, out)
                return
        render_nodes(self.else_body, ctx, out)


def render_nodes(nodes, ctx, out):
    for node in nodes:
        node.render(ctx, out)


def tokenize(text):
    text = (
        text.replace("{{-", "{{")
        .replace("-}}", "}}")
        .replace("{%-", "{%")
        .replace("-%}", "%}")
    )
    tokens, position = [], 0
    for match in TOKEN_RE.finditer(text):
        if match.start() > position:
            tokens.append(("text", text[position:match.start()]))
        if match.group(1) is not None:
            tokens.append(("output", match.group(1).strip()))
        else:
            tokens.append(("tag", match.group(2).strip()))
        position = match.end()
    if position < len(text):
        tokens.append(("text", text[position:]))
    return tokens


def parse(tokens, index=0, terminators=()):
    nodes = []
    while index < len(tokens):
        kind, payload = tokens[index]
        if kind == "text":
            nodes.append(Text(payload))
            index += 1
            continue
        if kind == "output":
            nodes.append(Output(payload))
            index += 1
            continue

        name, _, rest = payload.partition(" ")
        name, rest = name.strip(), rest.strip()

        if name in terminators:
            return nodes, index, name, rest

        if name == "assign":
            target, separator, expression = rest.partition("=")
            if not separator:
                raise PreviewError("{%% assign %s %%} needs an '='" % rest)
            nodes.append(Assign(target.strip(), expression.strip()))
            index += 1
            continue

        if name == "for":
            variable, separator, sequence = rest.partition(" in ")
            if not separator:
                raise PreviewError("{%% for %s %%} needs 'in'" % rest)
            body, index, terminator, _ = parse(tokens, index + 1, ("endfor", "else"))
            else_body = []
            if terminator == "else":
                else_body, index, terminator, _ = parse(tokens, index + 1, ("endfor",))
            if terminator != "endfor":
                raise PreviewError("{%% for %s %%} is missing {%% endfor %%}" % rest)
            nodes.append(For(variable.strip(), sequence.strip(), body, else_body))
            index += 1
            continue

        if name in ("if", "unless"):
            if not rest:
                raise PreviewError("{%% %s %%} needs a condition" % name)
            body, index, terminator, next_condition = parse(
                tokens, index + 1, ("elsif", "else", "endif")
            )
            branches = [(rest, body)]
            else_body = []
            while terminator == "elsif":
                body, index, terminator, next_condition = parse(
                    tokens, index + 1, ("elsif", "else", "endif")
                )
                branches.append((next_condition, body))
            if terminator == "else":
                else_body, index, terminator, _ = parse(tokens, index + 1, ("endif",))
            if terminator != "endif":
                raise PreviewError("{%% %s %s %%} is missing {%% endif %%}" % (name, rest))
            nodes.append(If(branches, else_body, negate_first=(name == "unless")))
            index += 1
            continue

        if name == "comment":
            _, index, terminator, _ = parse(tokens, index + 1, ("endcomment",))
            if terminator != "endcomment":
                raise PreviewError("{%% comment %%} is missing {%% endcomment %%}")
            index += 1
            continue

        raise PreviewError("unsupported Liquid tag: {%% %s %%}" % payload)

    if terminators:
        raise PreviewError("missing {%% %s %%}" % " / ".join(terminators))
    return nodes, index, None, None


# ==========================================================================
# Liquid: evaluation.
# ==========================================================================

EMPTY = {"__empty__": True}


def truthy(value):
    if value is None or value is False:
        return False
    if isinstance(value, dict) and value.get("__empty__"):
        return False
    return True


def split_top(text, separator):
    """Split on `separator` at depth 0, ignoring quotes."""
    parts, buffer, quote, depth, index = [], [], None, 0, 0
    while index < len(text):
        char = text[index]
        if quote:
            buffer.append(char)
            if char == quote and (index == 0 or text[index - 1] != "\\"):
                quote = None
        elif char in QUOTES:
            quote = char
            buffer.append(char)
        elif char in "([{":
            depth += 1
            buffer.append(char)
        elif char in ")]}":
            depth -= 1
            buffer.append(char)
        elif depth == 0 and text.startswith(separator, index):
            parts.append("".join(buffer))
            buffer = []
            index += len(separator)
            continue
        else:
            buffer.append(char)
        index += 1
    parts.append("".join(buffer))
    return [part.strip() for part in parts]


def resolve(path, ctx):
    path = path.strip()
    if path in ("nil", "null"):
        return None
    if path == "true":
        return True
    if path == "false":
        return False
    if path == "empty":
        return EMPTY
    if re.fullmatch(r"-?\d+(\.\d+)?", path):
        return float(path) if "." in path else int(path)
    if len(path) > 1 and path[:1] in QUOTES and path[-1:] == path[:1]:
        return path[1:-1]

    segments = [segment for segment in re.split(r"\.|\[|\]", path) if segment != ""]
    value = None
    for position, segment in enumerate(segments):
        if position == 0:
            if segment not in ctx:
                return None
            value = ctx[segment]
        else:
            value = lookup(value, segment)
        if value is None:
            return None
    return value


def lookup(value, key):
    if isinstance(value, dict):
        return value.get(key)
    if isinstance(value, (list, tuple, str)):
        if key == "size":
            return len(value)
        if key == "first":
            return value[0] if value else None
        if key == "last":
            return value[-1] if value else None
        if isinstance(value, (list, tuple)) and re.fullmatch(r"\d+", key):
            position = int(key)
            return value[position] if position < len(value) else None
    return None


def evaluate_condition(text, ctx):
    parts = split_top(text, " or ")
    if len(parts) > 1:
        return any(truthy(evaluate_condition(part, ctx)) for part in parts)

    parts = split_top(text, " and ")
    if len(parts) > 1:
        return all(truthy(evaluate_condition(part, ctx)) for part in parts)

    if text.startswith("not "):
        return not truthy(evaluate_condition(text[4:], ctx))

    parts = split_top(text, " contains ")
    if len(parts) == 2:
        haystack = evaluate(parts[0], ctx)
        needle = evaluate(parts[1], ctx)
        if isinstance(haystack, str):
            return str(needle) in haystack
        if isinstance(haystack, (list, tuple)):
            return needle in haystack
        return False

    for operator in ("==", "!=", ">=", "<=", ">", "<"):
        parts = split_top(text, operator)
        if len(parts) == 2:
            return compare(evaluate(parts[0], ctx), evaluate(parts[1], ctx), operator)

    return evaluate(text, ctx)


def compare(left, right, operator):
    if operator == "==":
        if isinstance(left, dict) and left.get("__empty__"):
            return not truthy(right)
        if isinstance(right, dict) and right.get("__empty__"):
            return not truthy(left)
        return left == right
    if operator == "!=":
        return not compare(left, right, "==")
    try:
        if operator == ">":
            return left > right
        if operator == "<":
            return left < right
        if operator == ">=":
            return left >= right
        if operator == "<=":
            return left <= right
    except TypeError:
        return False
    raise PreviewError("unknown operator %s" % operator)


def evaluate(text, ctx):
    stages = split_top(text, "|")
    value = resolve(stages[0], ctx)
    for stage in stages[1:]:
        name, _, argument = stage.partition(":")
        value = apply_filter(name.strip(), value, argument.strip(), ctx)
    return value


def apply_filter(name, value, argument, ctx):
    if name == "relative_url":
        return relative_url(value, ctx)
    if name == "absolute_url":
        site = ctx.get("site") or {}
        return (site.get("url") or "") + relative_url(value, ctx)
    if name == "default":
        if value is None or value is False or value == "" or value == []:
            return evaluate(argument, ctx)
        return value
    if name in ("escape", "escape_once"):
        return html.escape("" if value is None else str(value), quote=True)
    if name in ("strip", "downcase", "upcase"):
        text = "" if value is None else str(value)
        return {"strip": text.strip, "downcase": text.lower, "upcase": text.upper}[name]()
    if name == "slugify":
        text = "" if value is None else str(value)
        return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")
    if name == "join":
        separator = str(evaluate(argument, ctx)) if argument else " "
        return separator.join("" if item is None else str(item) for item in (value or []))
    if name == "size":
        return len(value) if value is not None else 0
    if name in ("append", "prepend"):
        addition = "" if not argument else str(evaluate(argument, ctx))
        text = "" if value is None else str(value)
        return text + addition if name == "append" else addition + text
    if name in ("replace", "remove"):
        first, _, second = argument.partition(",")
        text = "" if value is None else str(value)
        old = str(evaluate(first.strip(), ctx))
        new = str(evaluate(second.strip(), ctx)) if second.strip() else ""
        return text.replace(old, new) if name == "replace" else text.replace(old, "")
    if name == "date":
        return value  # dates are written out by hand in this site's data files
    raise PreviewError("unsupported Liquid filter: %s" % name)


def relative_url(value, ctx):
    text = "" if value is None else str(value)
    if text.startswith(("http://", "https://", "//", "#", "mailto:")):
        return text
    site = ctx.get("site") or {}
    baseurl = (site.get("baseurl") or "").rstrip("/")
    if not text.startswith("/"):
        text = "/" + text
    return baseurl + text


# ==========================================================================
# The site: config, data, pages, layouts.
# ==========================================================================

class Site:
    def __init__(self, root=ROOT):
        self.root = root
        self.config = load_yaml(os.path.join(root, "_config.yml")) or {}
        for key, fallback in (
            ("title", ""),
            ("description", ""),
            ("url", ""),
            ("baseurl", ""),
            ("lang", "en"),
        ):
            self.config.setdefault(key, fallback)
        self.config["data"] = self.load_data()

    def load_data(self):
        data = {}
        if os.path.isdir(DATA_DIR):
            for name in sorted(os.listdir(DATA_DIR)):
                if name.endswith((".yml", ".yaml")):
                    key = os.path.splitext(name)[0]
                    data[key] = load_yaml(os.path.join(DATA_DIR, name))
        return data

    def context_for(self, page):
        return {"site": self.config, "page": page, "content": ""}

    def render_page(self, relative_path):
        path = os.path.join(self.root, relative_path)
        with open(path, encoding="utf-8") as handle:
            front_matter, body = split_front_matter(handle.read())

        page = dict(front_matter or {})
        page.setdefault("url", "/" if relative_path == "index.html" else "/" + relative_path)

        context = self.context_for(page)
        rendered = render(body, context)

        layout = page.get("layout")
        if layout:
            layout_path = os.path.join(self.root, "_layouts", "%s.html" % layout)
            if not os.path.exists(layout_path):
                raise PreviewError("missing layout: _layouts/%s.html" % layout)
            with open(layout_path, encoding="utf-8") as handle:
                layout_front_matter, layout_body = split_front_matter(handle.read())
            context["content"] = rendered
            for key, value in (layout_front_matter or {}).items():
                context[key] = value
            rendered = render(layout_body, context)

        return rendered


def split_front_matter(source):
    if not source.startswith("---"):
        return None, source
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", source, re.S)
    if not match:
        raise PreviewError("front matter is not closed with '---'")
    return _parse_inline_yaml(match.group(1)), source[match.end():]


def _parse_inline_yaml(text):
    lines = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.strip().startswith("#"):
            continue
        content = _strip_comment(line)
        if content.strip():
            lines.append((len(content) - len(content.lstrip(" ")), content.strip(), number))
    if not lines:
        return {}
    value, _ = _parse_block(lines, 0, lines[0][0], "front matter")
    if not isinstance(value, dict):
        raise PreviewError("front matter must be a mapping")
    return value


def render(source, ctx):
    nodes, _, _, _ = parse(tokenize(source))
    out = []
    render_nodes(nodes, ctx, out)
    return "".join(out)


# ==========================================================================
# Serving and building.
# ==========================================================================

class Handler(http.server.BaseHTTPRequestHandler):
    site = None
    server_version = "homepage-preview"

    def do_GET(self):
        self.respond(head_only=False)

    def do_HEAD(self):
        self.respond(head_only=True)

    def respond(self, head_only):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("", "/"):
                self.send(self.site.render_page("index.html").encode("utf-8"),
                          "text/html; charset=utf-8", head_only)
                return
            if path.endswith("/"):
                path += "index.html"
            relative = path.lstrip("/")
            if relative in PAGES:
                self.send(self.site.render_page(relative).encode("utf-8"),
                          "text/html; charset=utf-8", head_only)
                return
            if relative.startswith(ASSET_DIRS) or os.path.isfile(os.path.join(self.site.root, relative)):
                self.send_file(relative, head_only)
                return
            self.not_found(head_only)
        except PreviewError as error:
            self.send(("Preview error\n\n%s\n" % error).encode("utf-8"),
                      "text/plain; charset=utf-8", head_only, status=500)

    def send(self, body, content_type, head_only, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def send_file(self, relative, head_only):
        full = os.path.abspath(os.path.join(self.site.root, relative))
        if not full.startswith(os.path.abspath(self.site.root)) or not os.path.isfile(full):
            self.not_found(head_only)
            return
        content_type = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("image/svg+xml", "application/javascript"):
            content_type += "; charset=utf-8"
        with open(full, "rb") as handle:
            body = handle.read()
        self.send(body, content_type, head_only)

    def not_found(self, head_only):
        if os.path.exists(os.path.join(self.site.root, "404.html")):
            body = self.site.render_page("404.html").encode("utf-8")
        else:
            body = b"404 Not Found\n"
        self.send(body, "text/html; charset=utf-8", head_only, status=404)

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def build(site, out_dir):
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    for page in PAGES:
        if os.path.exists(os.path.join(site.root, page)):
            with open(os.path.join(out_dir, page), "w", encoding="utf-8") as handle:
                handle.write(site.render_page(page))
    for directory in ASSET_DIRS:
        source = os.path.join(site.root, directory)
        if os.path.isdir(source):
            shutil.copytree(source, os.path.join(out_dir, directory))
    return out_dir


def check(site):
    problems = 0
    for name, data in sorted(site.config["data"].items()):
        count = len(data) if isinstance(data, (list, dict)) else 1
        print("  _data/%s.yml — %s, %d entries" % (name, type(data).__name__, count))
    try:
        import yaml
    except ImportError:
        print("  PyYAML is not installed — skipping the independent cross-check")
        return problems
    for name in sorted(site.config["data"]):
        with open(os.path.join(DATA_DIR, "%s.yml" % name), encoding="utf-8") as handle:
            reference = yaml.safe_load(handle)
        if reference != site.config["data"][name]:
            problems += 1
            print("  ! _data/%s.yml parses differently from PyYAML" % name)
    if not problems:
        print("  all data files cross-check against PyYAML")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description="Preview this Jekyll site without installing Jekyll.")
    parser.add_argument("--port", type=int, default=4000, help="port to serve on (default: 4000)")
    parser.add_argument("--host", default="127.0.0.1", help="host to bind (default: 127.0.0.1)")
    parser.add_argument("--build", nargs="?", const="_site", metavar="DIR",
                        help="render the site into DIR (default: _site) and exit")
    parser.add_argument("--check", action="store_true", help="check the data files and exit")
    args = parser.parse_args(argv)

    try:
        site = Site(ROOT)
        if args.check:
            print("Checking site data in %s" % ROOT)
            return 1 if check(site) else 0
        if args.build:
            out_dir = args.build if os.path.isabs(args.build) else os.path.join(ROOT, args.build)
            build(site, out_dir)
            print("Rendered into %s" % out_dir)
            return 0
    except PreviewError as error:
        print("error: %s" % error, file=sys.stderr)
        return 1

    Handler.site = site
    for port in range(args.port, args.port + 12):
        try:
            httpd = Server((args.host, port), Handler)
        except OSError:
            continue
        break
    else:
        print("error: no free port between %d and %d" % (args.port, args.port + 11), file=sys.stderr)
        return 1

    print("Previewing %s" % ROOT)
    if port != args.port:
        print("  port %d was busy, using %d instead" % (args.port, port))
    print("  http://%s:%d   (Ctrl-C to stop)" % (args.host, port))
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
