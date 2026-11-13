"""YAML for sensor definitions and stimuli, read with the YAML 1.2 core rules.

Only ``true``/``false`` are booleans, so a key such as ``on:`` stays the word ``"on"`` (YAML 1.1
readers, PyYAML's default among them, turn it into a boolean), ``012`` is twelve, ``0o17`` octal,
``1e5`` a float and ``2026-01-01`` a string. PyYAML is used when it is installed (the ``yaml``
extra), with its resolvers replaced by the 1.2 core ones; otherwise a small built-in reader
covers what definitions use: block and flow collections, quoted and plain scalars, ``|`` and
``>`` blocks, anchors and aliases, comments. The reader is the one in the sensor library's
``tools/yaml2json.py``, so both give the same JSON.
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["YamlError", "load_yaml", "plain_scalar"]

_INT_RE = re.compile(r"^[-+]?(0x[0-9a-fA-F_]+|0o[0-7_]+|0b[01_]+|[0-9][0-9_]*)$")
_FLOAT_RE = re.compile(r"^[-+]?(\.[0-9]+|[0-9][0-9_]*(\.[0-9]*)?)([eE][-+]?[0-9]+)?$")


def plain_scalar(s):
    """YAML 1.2 core schema for a plain (unquoted) scalar."""
    if s in ("", "~", "null", "Null", "NULL"):
        return None
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if _INT_RE.match(s):
        t = s.replace("_", "")
        sign = -1 if t.startswith("-") else 1
        t = t.lstrip("+-")
        if t.startswith(("0x", "0X")):
            return sign * int(t[2:], 16)
        if t.startswith("0o"):
            return sign * int(t[2:], 8)
        if t.startswith("0b"):
            return sign * int(t[2:], 2)
        return sign * int(t, 10)
    if _FLOAT_RE.match(s):
        return float(s.replace("_", ""))
    if s in (".inf", "+.inf"):
        return float("inf")
    if s == "-.inf":
        return float("-inf")
    return s


class YamlError(ValueError):
    pass


class MiniYaml:
    """A reader for the YAML subset the definition files use."""

    def __init__(self, text):
        self.lines = []          # (indent, text without comment, line number)
        self.raw = text.split("\n")
        for n, line in enumerate(self.raw):
            stripped = self._strip_comment(line).rstrip()
            if stripped.strip() == "" or stripped.strip() in ("---", "..."):
                self.lines.append(None)
                continue
            indent = len(stripped) - len(stripped.lstrip(" "))
            if "\t" in stripped[:indent + 1]:
                raise YamlError(f"line {n + 1}: tabs are not allowed for indentation")
            self.lines.append((indent, stripped[indent:], n))
        self.anchors = {}
        self.pos = 0

    @staticmethod
    def _strip_comment(line):
        out, q, i = [], None, 0
        while i < len(line):
            c = line[i]
            if q:
                out.append(c)
                if q == '"' and c == "\\" and i + 1 < len(line):
                    out.append(line[i + 1])
                    i += 1
                elif c == q:
                    if q == "'" and i + 1 < len(line) and line[i + 1] == "'":
                        out.append("'")
                        i += 1
                    else:
                        q = None
            elif c in ('"', "'") and (i == 0 or line[i - 1] in " \t[{,:-"):
                q = c
                out.append(c)
            elif c == "#" and (i == 0 or line[i - 1] in " \t"):
                break
            else:
                out.append(c)
            i += 1
        return "".join(out)

    def _skip_blank(self):
        while self.pos < len(self.lines) and self.lines[self.pos] is None:
            self.pos += 1

    def parse(self):
        self._skip_blank()
        if self.pos >= len(self.lines):
            return None
        v = self._block(self.lines[self.pos][0])
        self._skip_blank()
        if self.pos < len(self.lines):
            raise YamlError(f"line {self.lines[self.pos][2] + 1}: unexpected indentation")
        return v

    def _block(self, indent):
        self._skip_blank()
        ind, text, _ = self.lines[self.pos]
        if text.startswith("- ") or text == "-":
            return self._seq(ind)
        return self._map(ind)

    def _seq(self, indent):
        out = []
        while True:
            self._skip_blank()
            if self.pos >= len(self.lines):
                break
            ind, text, n = self.lines[self.pos]
            if ind < indent:
                break
            if ind > indent:
                raise YamlError(f"line {n + 1}: bad indentation")
            if not (text.startswith("- ") or text == "-"):
                break
            rest = text[1:].lstrip(" ")
            if rest == "":
                self.pos += 1
                out.append(self._block(indent + 1))
                continue
            # "- key: value" starts a mapping whose keys sit at the item's column.
            col = indent + (len(text) - len(rest))
            if self._looks_like_key(rest):
                self.lines[self.pos] = (col, rest, n)
                out.append(self._map(col))
            else:
                self.pos += 1
                out.append(self._value_inline(rest, n, col))
        return out

    @staticmethod
    def _split_key(text):
        """'key: rest' -> (key, rest) or None."""
        if text[0] in ('"', "'"):
            q = text[0]
            end = text.find(q, 1)
            while q == "'" and end != -1 and end + 1 < len(text) and text[end + 1] == "'":
                end = text.find(q, end + 2)
            if end == -1:
                return None
            key = text[1:end].replace("''", "'") if q == "'" else json.loads(text[:end + 1])
            after = text[end + 1:]
            if after.startswith(":") and (len(after) == 1 or after[1] == " "):
                return key, after[1:].strip()
            return None
        if text[0] in "[{":
            return None
        m = re.match(r"^([^\s:#][^:#]*?)\s*:(\s+|$)", text)
        if not m:
            return None
        return m.group(1), text[m.end():].strip()

    def _looks_like_key(self, text):
        return self._split_key(text) is not None

    def _map(self, indent):
        out = {}
        while True:
            self._skip_blank()
            if self.pos >= len(self.lines):
                break
            ind, text, n = self.lines[self.pos]
            if ind < indent:
                break
            if ind > indent:
                raise YamlError(f"line {n + 1}: bad indentation")
            if text.startswith("- "):
                break
            kv = self._split_key(text)
            if kv is None:
                raise YamlError(f"line {n + 1}: expected 'key: value'")
            key, rest = kv
            key = plain_scalar(key) if isinstance(key, str) and text[0] not in "\"'" else key
            if isinstance(key, bool) or key is None:
                key = text.split(":")[0].strip()
            key = str(key)
            if key in out:
                raise YamlError(f"line {n + 1}: duplicate key '{key}'")
            self.pos += 1
            anchor = None
            m = re.match(r"^&([A-Za-z0-9_-]+)\s*(.*)$", rest)
            if m:
                anchor, rest = m.group(1), m.group(2)
            if rest == "":
                self._skip_blank()
                if self.pos < len(self.lines) and (self.lines[self.pos][0] > ind or
                                                    (self.lines[self.pos][0] == ind and
                                                     self.lines[self.pos][1].startswith("- "))):
                    val = self._block(self.lines[self.pos][0])
                else:
                    val = None
            elif rest in ("|", "|-", "|+", ">", ">-", ">+"):
                val = self._block_scalar(rest, ind)
            else:
                val = self._value_inline(rest, n, ind)
            if anchor:
                self.anchors[anchor] = val
            out[key] = val
        return out

    def _block_scalar(self, style, parent_indent):
        lines = []
        block_indent = None
        while self.pos < len(self.lines):
            raw_line = self.raw[self._line_no(self.pos)] if self.lines[self.pos] else ""
            if self.lines[self.pos] is None:
                lines.append("")
                self.pos += 1
                continue
            ind = len(raw_line) - len(raw_line.lstrip(" "))
            if ind <= parent_indent:
                break
            if block_indent is None:
                block_indent = ind
            lines.append(raw_line[block_indent:].rstrip())
            self.pos += 1
        while lines and lines[-1] == "":
            lines.pop()
            if self.pos > 0:
                pass
        if style.startswith(">"):
            text = " ".join(lines)
        else:
            text = "\n".join(lines)
        if not style.endswith("-"):
            text += "\n"
        return text

    def _line_no(self, pos):
        return self.lines[pos][2]

    def _value_inline(self, text, n, indent):
        m = re.match(r"^&([A-Za-z0-9_-]+)\s+(.*)$", text)
        anchor = None
        if m:
            anchor, text = m.group(1), m.group(2)
        if text[0] in "[{":
            # A flow collection may continue on the following, more indented lines.
            while not self._balanced(text):
                self._skip_blank()
                if self.pos >= len(self.lines):
                    raise YamlError(f"line {n + 1}: unterminated flow collection")
                text += " " + self.lines[self.pos][1]
                self.pos += 1
            val, end = self._flow(text, 0, n)
            if text[end:].strip():
                raise YamlError(f"line {n + 1}: text after a flow collection")
        else:
            val = self._scalar_text(text, n)
        if anchor:
            self.anchors[anchor] = val
        return val

    @staticmethod
    def _balanced(text):
        depth, q, i = 0, None, 0
        while i < len(text):
            c = text[i]
            if q:
                if q == '"' and c == "\\":
                    i += 1
                elif c == q:
                    q = None
            elif c in "\"'":
                q = c
            elif c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
            i += 1
        return depth == 0

    def _scalar_text(self, text, n):
        text = text.strip()
        if text.startswith("*"):
            name = text[1:]
            if name not in self.anchors:
                raise YamlError(f"line {n + 1}: unknown alias '{name}'")
            return self.anchors[name]
        if text.startswith('"'):
            if not text.endswith('"') or len(text) < 2:
                raise YamlError(f"line {n + 1}: unterminated string")
            return json.loads(text)
        if text.startswith("'"):
            if not text.endswith("'") or len(text) < 2:
                raise YamlError(f"line {n + 1}: unterminated string")
            return text[1:-1].replace("''", "'")
        return plain_scalar(text)

    def _flow(self, s, i, n):
        def ws(j):
            while j < len(s) and s[j] == " ":
                j += 1
            return j

        i = ws(i)
        m = re.match(r"&([A-Za-z0-9_-]+) +", s[i:])
        if m:   # an anchor inside a flow collection: "&name [ ... ]"
            v, j = self._flow(s, i + m.end(), n)
            self.anchors[m.group(1)] = v
            return v, j
        c = s[i]
        if c == "[":
            out, i = [], ws(i + 1)
            if s[i] == "]":
                return out, i + 1
            while True:
                v, i = self._flow(s, i, n)
                out.append(v)
                i = ws(i)
                if s[i] == ",":
                    i = ws(i + 1)
                    if s[i] == "]":
                        return out, i + 1
                    continue
                if s[i] == "]":
                    return out, i + 1
                raise YamlError(f"line {n + 1}: expected ',' or ']' in a flow list")
        if c == "{":
            out, i = {}, ws(i + 1)
            if s[i] == "}":
                return out, i + 1
            while True:
                k, i = self._flow_scalar(s, i, n, key=True)
                i = ws(i)
                if s[i] != ":":
                    raise YamlError(f"line {n + 1}: expected ':' in a flow mapping")
                v, i = self._flow(s, i + 1, n)
                if str(k) in out:
                    raise YamlError(f"line {n + 1}: duplicate key '{k}'")
                out[str(k)] = v
                i = ws(i)
                if s[i] == ",":
                    i = ws(i + 1)
                    if s[i] == "}":
                        return out, i + 1
                    continue
                if s[i] == "}":
                    return out, i + 1
                raise YamlError(f"line {n + 1}: expected ',' or '}}' in a flow mapping")
        return self._flow_scalar(s, i, n)

    def _flow_scalar(self, s, i, n, key=False):
        if s[i] == '"':
            j = i + 1
            while s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            return json.loads(s[i:j + 1]), j + 1
        if s[i] == "'":
            j = i + 1
            while True:
                if s[j] == "'" and j + 1 < len(s) and s[j + 1] == "'":
                    j += 2
                    continue
                if s[j] == "'":
                    break
                j += 1
            return s[i + 1:j].replace("''", "'"), j + 1
        j = i
        while j < len(s):
            if s[j] in ",]}":
                break
            if s[j] == ":" and (j + 1 >= len(s) or s[j + 1] in " ,]}"):
                break
            j += 1
        text = s[i:j].strip()
        if key:
            return text, j
        if text.startswith("*"):
            return self._scalar_text(text, n), j
        return plain_scalar(text), j



def _pyyaml_loader():
    import yaml  # type: ignore[import-untyped]

    class Loader(yaml.SafeLoader):
        """SafeLoader with the YAML 1.2 core schema and duplicate keys refused."""

    Loader.yaml_implicit_resolvers = {}
    Loader.add_implicit_resolver("tag:yaml.org,2002:bool",
                                 re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
    Loader.add_implicit_resolver("tag:yaml.org,2002:null",
                                 re.compile(r"^(?:~|null|Null|NULL|)$"), ["~", "n", "N", ""])
    Loader.add_implicit_resolver("tag:yaml.org,2002:int", _INT_RE, list("-+0123456789"))
    Loader.add_implicit_resolver("tag:yaml.org,2002:float", re.compile(
        _FLOAT_RE.pattern + r"|^(?:[-+]?\.inf|\.nan)$"), list("-+0123456789."))
    Loader.add_implicit_resolver("tag:yaml.org,2002:merge", re.compile(r"^(?:<<)$"), ["<"])

    def number(loader: Any, node: Any) -> Any:
        text = loader.construct_scalar(node)
        if text == ".nan":
            return float("nan")
        return plain_scalar(text)

    def mapping(loader: Any, node: Any) -> Any:
        loader.flatten_mapping(node)
        out = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=True)
            # As the built-in reader: a number key is its value as text, anything else as written.
            key = str(key) if isinstance(key, (str, int)) and not isinstance(key, bool) else key_node.value
            if key in out:
                raise YamlError(f"line {key_node.start_mark.line + 1}: duplicate key '{key}'")
            out[key] = loader.construct_object(value_node, deep=True)
        return out

    Loader.add_constructor("tag:yaml.org,2002:int", number)
    Loader.add_constructor("tag:yaml.org,2002:float", number)
    Loader.add_constructor("tag:yaml.org,2002:map", mapping)
    return yaml, Loader


def load_yaml(text: str) -> Any:
    """Parse YAML (or JSON, which is YAML) ``text`` with the 1.2 core rules; raises
    :class:`YamlError` with the line on a syntax error."""
    try:
        yaml, loader = _pyyaml_loader()
    except ImportError:
        return MiniYaml(text).parse()
    try:
        return yaml.load(text, Loader=loader)  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as exc:
        raise YamlError(str(exc)) from None
