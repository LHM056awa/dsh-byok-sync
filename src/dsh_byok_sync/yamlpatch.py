"""文本手术式 YAML 编辑：只重写目标路由的 ``models:`` 块。

本模块**不依赖 PyYAML**（SPEC §2 只允许标准库），也不用“解析 → 重新序列化”
的方式回写文件——那样会毁掉用户的注释、空行、引号风格与缩进。

工作方式：

1. 用按行切分的扫描器把文件读成 ``_Line`` 列表（每行记住自己的行尾，
   因此 CRLF / LF 混排以及“文件末尾有没有换行”都能字节级还原）；
2. 用缩进块扫描器定位 ``providers:`` → ``<route>:`` → ``models:``；
3. 把 ``models`` 块切成“条目 chunk”（``- `` 行 + 其后的深缩进行 + 前导注释 +
   尾部空行），然后按 plan 顺序把 chunk 原文拼接回去；
4. 新条目用 SPEC §4 的规范化渲染，缩进与既有条目对齐。

目标区域以外**一个字节都不改**；plan 与原状等价时原样返回输入文本。

已知不支持（会抛 :class:`YamlPatchError`，绝不静默猜测）：Tab 缩进、多文档
YAML、作用在将被重写区域上的锚点/别名/标签（``&``/``*``/``!!js``）、
``models`` 的块标量值、流风格的 provider 映射。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

KIND_PATCH = "patch"
KIND_SETTINGS = "settings"

#: SPEC §4 规定的新条目字段输出顺序。
MODEL_FIELD_ORDER: tuple[str, ...] = (
    "id",
    "name",
    "contextWindow",
    "maxTokens",
    "input",
    "reasoningEfforts",
    "compat",
)

#: SPEC §4：以这些字符开头的标量必须加引号。
_UNSAFE_PREFIX = set("-?:,[]{}#&*!|>'\"%@`")

#: 能被 YAML 解析成 bool / null 的裸字符串。
_BOOL_NULL_WORDS = {
    "true", "false", "yes", "no", "on", "off", "null", "~", "y", "n",
}

#: 能被 YAML 解析成数字的裸字符串。
_NUMBER_RE = re.compile(
    r"""^[-+]?(
          0[xX][0-9a-fA-F]+
        | 0[oO][0-7]+
        | 0[bB][01]+
        | \d[\d_]*(\.\d*)?([eE][-+]?\d+)?
        | \.\d+([eE][-+]?\d+)?
        | \.(inf|Inf|INF)
        | \.(nan|NaN|NAN)
    )$""",
    re.VERBOSE,
)

#: 锚点 / 别名出现在值位置（``key: &a``、``- *a``）。
_ANCHOR_VALUE_RE = re.compile(r"(?:^|[:\-]\s)[&*][A-Za-z0-9_]")
_JS_TAG = "!!js"


class YamlPatchError(Exception):
    """无法安全地按文本手术编辑该 YAML 文件。"""


# --------------------------------------------------------------------------- #
# 行模型
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Line:
    """一行文本：``content`` 不含行尾，``term`` 是 ``"\\n"`` / ``"\\r\\n"`` / ``""``。"""

    content: str
    term: str


def _split_lines(text: str) -> list[_Line]:
    lines: list[_Line] = []
    if not text:
        return lines
    start = 0
    total = len(text)
    while start < total:
        nl = text.find("\n", start)
        if nl < 0:
            lines.append(_Line(text[start:], ""))
            break
        chunk = text[start:nl]
        term = "\n"
        if chunk.endswith("\r"):
            chunk = chunk[:-1]
            term = "\r\n"
        lines.append(_Line(chunk, term))
        start = nl + 1
    return lines


def _join_lines(lines: Sequence[_Line]) -> str:
    return "".join(line.content + line.term for line in lines)


def _detect_newline(text: str) -> str:
    idx = text.find("\n")
    if idx > 0 and text[idx - 1] == "\r":
        return "\r\n"
    return "\n"


def _indent_of(line: _Line) -> int:
    count = 0
    for ch in line.content:
        if ch != " ":
            break
        count += 1
    return count


def _is_blank(line: _Line) -> bool:
    return line.content.strip() == ""


def _is_comment(line: _Line) -> bool:
    return line.content.lstrip().startswith("#")


def _is_dash_text(text: str) -> bool:
    return text == "-" or (text.startswith("-") and len(text) > 1 and text[1] in " \t")


def _reject_indent_tabs(lines: Sequence[_Line]) -> None:
    for index, line in enumerate(lines):
        for ch in line.content:
            if ch == "\t":
                raise YamlPatchError(
                    f"第 {index + 1} 行缩进里出现 Tab：为免破坏对齐，本工具只处理空格缩进"
                )
            if ch != " ":
                break


# --------------------------------------------------------------------------- #
# 标量 / 流风格解析
# --------------------------------------------------------------------------- #


def _split_trailing_comment(text: str) -> tuple[str, str]:
    """把 ``" value  # note"`` 拆成 ``(" value", "# note")``。

    引号内的 ``#`` 不算注释。
    """
    quote: str | None = None
    i = 0
    total = len(text)
    cut = total
    while i < total:
        ch = text[i]
        if quote is not None:
            if quote == '"' and ch == "\\":
                i += 2
                continue
            if ch == quote:
                if quote == "'" and i + 1 < total and text[i + 1] == "'":
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            i += 1
            continue
        if ch == "#" and (i == 0 or text[i - 1] in " \t"):
            cut = i
            break
        i += 1
    return text[:cut], text[cut:]


def _split_key(text: str) -> tuple[str, str] | None:
    """把 ``"key: rest"`` 拆成 ``("key", "rest")``；不是键行则返回 ``None``。"""
    if not text or text[0] == "#":
        return None
    if text[0] in "\"'":
        quote = text[0]
        i = 1
        buf: list[str] = []
        total = len(text)
        while i < total:
            ch = text[i]
            if quote == '"' and ch == "\\":
                if i + 1 >= total:
                    return None
                buf.append(_unescape_char(text[i + 1]))
                i += 2
                continue
            if ch == quote:
                i += 1
                break
            buf.append(ch)
            i += 1
        else:
            return None
        if i >= total or text[i] != ":":
            return None
        rest = text[i + 1:]
        if rest and rest[0] not in " \t":
            return None
        return "".join(buf), rest
    idx = text.find(":")
    if idx < 0:
        return None
    key = text[:idx].strip()
    if not key or "#" in key:
        return None
    rest = text[idx + 1:]
    if rest and rest[0] not in " \t":
        return None
    return key, rest


_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n",
    "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"',
    "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0",
}


def _unescape_char(ch: str) -> str:
    return _ESCAPES.get(ch, ch)


def _parse_double_quoted(text: str) -> str:
    buf: list[str] = []
    i = 1
    total = len(text)
    while i < total:
        ch = text[i]
        if ch == "\\" and i + 1 < total:
            nxt = text[i + 1]
            if nxt == "x" and i + 3 < total:
                try:
                    buf.append(chr(int(text[i + 2:i + 4], 16)))
                    i += 4
                    continue
                except ValueError:
                    pass
            if nxt == "u" and i + 5 < total:
                try:
                    buf.append(chr(int(text[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            buf.append(_unescape_char(nxt))
            i += 2
            continue
        if ch == '"':
            return "".join(buf)
        buf.append(ch)
        i += 1
    # 未闭合：当普通文本处理，绝不猜测
    return text


def _parse_single_quoted(text: str) -> str:
    buf: list[str] = []
    i = 1
    total = len(text)
    while i < total:
        ch = text[i]
        if ch == "'":
            if i + 1 < total and text[i + 1] == "'":
                buf.append("'")
                i += 2
                continue
            return "".join(buf)
        buf.append(ch)
        i += 1
    return text


def _parse_scalar(text: str) -> Any:
    """解析一个标量（不含注释、不含流风格括号）。"""
    value = text.strip()
    if value == "":
        return None
    if value[0] == '"':
        return _parse_double_quoted(value)
    if value[0] == "'":
        return _parse_single_quoted(value)
    lowered = value.lower()
    if lowered in ("null", "~"):
        return None
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if _NUMBER_RE.match(value):
        plain = value.replace("_", "")
        try:
            return int(plain, 0)
        except ValueError:
            pass
        try:
            return float(plain)
        except ValueError:
            pass
    return value


def _split_flow_items(text: str) -> list[str]:
    """按顶层逗号切分流风格内容（尊重引号与嵌套括号）。"""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    i = 0
    total = len(text)
    while i < total:
        ch = text[i]
        if quote is not None:
            buf.append(ch)
            if quote == '"' and ch == "\\" and i + 1 < total:
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                if quote == "'" and i + 1 < total and text[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch in "[{":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch in "]}":
            depth -= 1
            buf.append(ch)
            i += 1
            continue
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _split_flow_entry(text: str) -> tuple[str, str] | None:
    """在 ``{k: v, ...}`` 的一个元素里找出顶层 ``:`` 的位置。"""
    depth = 0
    quote: str | None = None
    i = 0
    total = len(text)
    while i < total:
        ch = text[i]
        if quote is not None:
            if quote == '"' and ch == "\\":
                i += 2
                continue
            if ch == quote:
                if quote == "'" and i + 1 < total and text[i + 1] == "'":
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            i += 1
            continue
        if ch in "[{":
            depth += 1
            i += 1
            continue
        if ch in "]}":
            depth -= 1
            i += 1
            continue
        if ch == ":" and depth == 0:
            nxt = text[i + 1] if i + 1 < total else ""
            if nxt == "" or nxt in " \t":
                return text[:i], text[i + 1:]
        i += 1
    return None


def _parse_flow(text: str) -> Any:
    """解析流风格 ``[...]`` / ``{...}``，其余按标量处理。"""
    value = text.strip()
    if value.startswith("["):
        if not value.endswith("]"):
            raise YamlPatchError(f"流风格列表未闭合：{value!r}")
        inner = value[1:-1].strip()
        if inner == "":
            return []
        return [_parse_flow(part) for part in _split_flow_items(inner) if part.strip() != ""]
    if value.startswith("{"):
        if not value.endswith("}"):
            raise YamlPatchError(f"流风格映射未闭合：{value!r}")
        inner = value[1:-1].strip()
        if inner == "":
            return {}
        result: dict[str, Any] = {}
        for part in _split_flow_items(inner):
            if part.strip() == "":
                continue
            pair = _split_flow_entry(part)
            if pair is None:
                raise YamlPatchError(f"流风格映射里有无法解析的元素：{part.strip()!r}")
            key = _parse_scalar(pair[0])
            result[str(key)] = _parse_flow(pair[1])
        return result
    return _parse_scalar(value)


# --------------------------------------------------------------------------- #
# 缩进块扫描器：把文件解析成带行号的节点树（只读，用于定位与浅层取值）
# --------------------------------------------------------------------------- #


@dataclass
class _Entry:
    key: str
    line: int       # 键所在行
    indent: int     # 键的缩进
    key_end: int    # 键行索引 + 1
    node: "_Node"


@dataclass
class _Item:
    line: int       # `- ` 所在行
    node: "_Node"


@dataclass
class _Node:
    kind: str                                   # map / seq / scalar / flow
    start: int
    end: int
    indent: int
    value: Any = None
    text: str | None = None
    entries: dict[str, _Entry] | None = None
    order: list[str] | None = None
    items: list[_Item] | None = None


def _make_inline_node(line: int, indent: int, head: str) -> _Node:
    value = head.strip()
    if value.startswith("[") or value.startswith("{"):
        return _Node("flow", line, line + 1, indent, value=_parse_flow(value), text=value)
    if value.startswith("!") or value.startswith("&") or value.startswith("*"):
        # 标签 / 锚点 / 别名：读取时保留原文，重写时另行拒绝
        return _Node("scalar", line, line + 1, indent, value=value, text=value)
    return _Node("scalar", line, line + 1, indent, value=_parse_scalar(value), text=value)


class _Parser:
    def __init__(self, lines: Sequence[_Line], end: int) -> None:
        self.lines = lines
        self.end = end

    # -- 工具 ---------------------------------------------------------------
    def ind(self, i: int) -> int:
        return _indent_of(self.lines[i])

    def at(self, i: int, col: int) -> str:
        return self.lines[i].content[col:]

    def blank(self, i: int) -> bool:
        return _is_blank(self.lines[i])

    def comment(self, i: int) -> bool:
        return _is_comment(self.lines[i])

    def skip(self, i: int, end: int | None = None) -> int:
        stop = self.end if end is None else end
        while i < stop and (self.blank(i) or self.comment(i)):
            i += 1
        return i

    def _continuation(self, i: int, entries: dict[str, _Entry], order: list[str]) -> _Node:
        """把深一层的裸行并入上一个标量（多行 plain scalar）。"""
        if order:
            entry = entries[order[-1]]
            if entry.node.kind == "scalar":
                extra = self.lines[i].content.strip()
                base = entry.node.text or ""
                entry.node.text = (base + " " + extra).strip() if base else extra
                entry.node.value = _parse_scalar(entry.node.text)
                entry.node.end = i + 1
                return entry.node
        raise YamlPatchError(
            f"第 {i + 1} 行缩进异常（{self.lines[i].content.strip()!r}），无法确定它属于哪个块"
        )

    # -- 入口 ---------------------------------------------------------------
    def parse_root(self) -> _Node:
        i = self.skip(0)
        while i < self.end:
            stripped = self.lines[i].content.strip()
            if stripped in ("---", "...") or stripped.startswith("%"):
                i = self.skip(i + 1)
                continue
            break
        if i >= self.end:
            raise YamlPatchError("文件为空或只有注释，无法解析 YAML 结构")
        indent = self.ind(i)
        if _is_dash_text(self.at(i, indent)):
            return self.parse_seq(i, self.end, indent)
        return self.parse_map(i, self.end, indent)

    # -- 映射 ---------------------------------------------------------------
    def parse_map(
        self,
        i: int,
        end: int,
        indent: int,
        *,
        key_col: int | None = None,
        allow_first_mismatch: bool = False,
    ) -> _Node:
        col = indent if key_col is None else key_col
        entries: dict[str, _Entry] = {}
        order: list[str] = []
        start_line = i
        last_end = i
        first = allow_first_mismatch
        while i < end:
            if not first:
                j = self.skip(i, end)
                if j >= end:
                    break
                i = j
                cur = self.ind(i)
                if cur > indent:
                    node = self._continuation(i, entries, order)
                    last_end = node.end
                    i += 1
                    continue
                if cur < indent:
                    break
            first = False
            text = self.at(i, col)
            if _is_dash_text(text):
                break
            pair = _split_key(text)
            if pair is None:
                raise YamlPatchError(
                    f"第 {i + 1} 行不是 `key: value` 形式：{self.lines[i].content.strip()!r}"
                )
            key, rest = pair
            if key in entries:
                raise YamlPatchError(
                    f"第 {i + 1} 行的键 {key!r} 与第 {entries[key].line + 1} 行重复，拒绝猜测语义"
                )
            head, _comment = _split_trailing_comment(rest)
            head = head.strip()
            key_end = i + 1
            if head.startswith("|") or head.startswith(">"):
                node = self._parse_block_scalar(i, key_end, end, indent, head)
            elif head == "":
                node = self._parse_empty_value(i, key_end, end, indent)
            else:
                node = _make_inline_node(i, indent, head)
            entries[key] = _Entry(key, i, indent, key_end, node)
            order.append(key)
            last_end = node.end
            i = node.end
        return _Node(
            "map",
            start_line,
            last_end,
            indent,
            entries=entries,
            order=order,
        )

    def _parse_block_scalar(self, i: int, key_end: int, end: int, indent: int, head: str) -> _Node:
        j = key_end
        while j < end:
            if self.blank(j):
                j += 1
                continue
            if self.ind(j) > indent:
                j += 1
                continue
            break
        return _Node("scalar", i, j, indent, text=head, value=None)

    def _parse_empty_value(self, i: int, key_end: int, end: int, indent: int) -> _Node:
        j = self.skip(key_end, end)
        if j < end:
            cur = self.ind(j)
            body = self.at(j, cur)
            if cur > indent:
                if _is_dash_text(body):
                    return self.parse_seq(j, end, cur)
                return self.parse_map(j, end, cur)
            if cur == indent and _is_dash_text(body):
                # YAML 允许序列与其键同缩进
                return self.parse_seq(j, end, cur)
        return _Node("scalar", i, key_end, indent, value=None)

    # -- 序列 ---------------------------------------------------------------
    def parse_seq(self, i: int, end: int, indent: int) -> _Node:
        items: list[_Item] = []
        first = True
        last_end = i
        while i < end:
            if not first:
                j = self.skip(i, end)
                if j >= end:
                    break
                i = j
                cur = self.ind(i)
                if cur > indent:
                    if items and items[-1].node.kind == "scalar":
                        node = items[-1].node
                        extra = self.lines[i].content.strip()
                        base = node.text or ""
                        node.text = (base + " " + extra).strip() if base else extra
                        node.value = _parse_scalar(node.text)
                        node.end = i + 1
                        last_end = i + 1
                        i += 1
                        continue
                    raise YamlPatchError(
                        f"第 {i + 1} 行缩进异常（{self.lines[i].content.strip()!r}），无法确定它属于哪个条目"
                    )
                if cur < indent:
                    break
            first = False
            text = self.at(i, indent)
            if not _is_dash_text(text):
                break
            content = self.lines[i].content
            col = indent + 1
            while col < len(content) and content[col] in " \t":
                col += 1
            item_text = content[col:]
            if item_text == "" or item_text.startswith("#"):
                j = self.skip(i + 1, end)
                if j < end and self.ind(j) > indent:
                    node = self.parse_seq(j, end, self.ind(j)) if _is_dash_text(
                        self.at(j, self.ind(j))
                    ) else self.parse_map(j, end, self.ind(j))
                else:
                    node = _Node("scalar", i, i + 1, col, value=None)
            else:
                pair = _split_key(item_text)
                if pair is not None:
                    node = self.parse_map(i, end, col, key_col=col, allow_first_mismatch=True)
                else:
                    head, _comment = _split_trailing_comment(item_text)
                    node = _make_inline_node(i, col, head)
            items.append(_Item(i, node))
            last_end = node.end
            i = node.end
        start_line = items[0].line if items else last_end
        return _Node("seq", start_line, last_end, indent, items=items)


def _node_value(node: _Node) -> Any:
    """把节点树折算成普通 Python 值（读取用）。"""
    if node.kind in ("scalar", "flow"):
        return node.value
    if node.kind == "map":
        return {
            key: _node_value(node.entries[key].node)  # type: ignore[index]
            for key in (node.order or [])
        }
    if node.kind == "seq":
        return [_node_value(item.node) for item in (node.items or [])]
    return None


# --------------------------------------------------------------------------- #
# 文档定位
# --------------------------------------------------------------------------- #


@dataclass
class _Doc:
    text: str
    lines: list[_Line]
    kind: str
    root: _Node
    newline: str
    ended_with_nl: bool


def _detect_kind_lines(lines: Sequence[_Line]) -> str:
    for index, line in enumerate(lines):
        stripped = line.content.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        if stripped in ("---", "...") or stripped.startswith("%"):
            continue
        if stripped in ("[]", "{}"):
            raise YamlPatchError(
                f"第 {index + 1} 行是空的 YAML 集合，无法判定文件形态"
            )
        if _is_dash_text(stripped):
            return KIND_PATCH
        if _split_key(stripped) is not None:
            return KIND_SETTINGS
        raise YamlPatchError(
            f"无法判定 YAML 形态：第 {index + 1} 行既不是列表项也不是映射键："
            f"{stripped!r}"
        )
    raise YamlPatchError("文件为空或只有注释，无法判定 YAML 形态")


def detect_kind(text: str) -> str:
    """判定文件形态：根是列表 → ``KIND_PATCH``；根是映射 → ``KIND_SETTINGS``。

    无法判定时抛 :class:`YamlPatchError`。
    """
    return _detect_kind_lines(_split_lines(_strip_bom(text)))


def _strip_bom(text: str) -> str:
    """Drop a leading UTF-8 BOM so the first meaningful line is still first."""
    return text[1:] if text.startswith("\ufeff") else text


def _prepare(text: str, kind: str) -> _Doc:
    if kind not in (KIND_PATCH, KIND_SETTINGS):
        raise YamlPatchError(
            f"未知的文件形态 {kind!r}（只支持 {KIND_PATCH!r} 或 {KIND_SETTINGS!r}）"
        )
    lines = _split_lines(text)
    _reject_indent_tabs(lines)
    actual = _detect_kind_lines(lines)
    if actual != kind:
        raise YamlPatchError(
            f"文件实际形态是 {actual!r}，与传入的 kind={kind!r} 不一致"
        )
    for index, line in enumerate(lines):
        if index == 0:
            continue
        stripped = line.content.strip()
        if stripped not in ("---", "...") or _indent_of(line) != 0:
            continue
        for later in lines[index + 1:]:
            following = later.content.strip()
            if following == "" or following.startswith("#") or following in ("---", "..."):
                continue
            raise YamlPatchError(
                f"第 {index + 1} 行出现文档分隔符 {stripped!r} 且其后还有其他内容："
                "不支持多文档 YAML"
            )
    root = _Parser(lines, len(lines)).parse_root()
    if actual == KIND_PATCH and root.kind != "seq":
        raise YamlPatchError("形态判定为 patch 但根节点不是列表")
    if actual == KIND_SETTINGS and root.kind != "map":
        raise YamlPatchError("形态判定为 settings 但根节点不是映射")
    return _Doc(text, lines, kind, root, _detect_newline(text), text.endswith("\n"))


def _scalar_text(node: _Node) -> str | None:
    if node.kind not in ("scalar", "flow"):
        return None
    if isinstance(node.value, str):
        return node.value
    if node.value is None:
        return None
    return str(node.value)


def _entry_node(doc: _Doc, entry: str) -> _Node:
    if doc.kind == KIND_PATCH:
        for item in doc.root.items or []:
            node = item.node
            if node.kind != "map" or not node.entries:
                continue
            id_entry = node.entries.get("id")
            if id_entry is not None and _scalar_text(id_entry.node) == entry:
                return node
        raise YamlPatchError(
            f"未找到 id 为 {entry!r} 的插件条目（文件形态：patch，根列表共 "
            f"{len(doc.root.items or [])} 项）"
        )
    entries = doc.root.entries or {}
    entry_obj = entries.get(entry)
    if entry_obj is None:
        known = "、".join(doc.root.order or []) or "无"
        raise YamlPatchError(
            f"未找到顶层键 {entry!r}（文件形态：settings，现有键：{known}）"
        )
    if entry_obj.node.kind != "map":
        raise YamlPatchError(f"顶层键 {entry!r} 不是块状映射，无法解析其 providers")
    return entry_obj.node


def _providers_node(entry_node: _Node, kind: str) -> _Node:
    if kind == KIND_PATCH:
        config = (entry_node.entries or {}).get("config")
        if config is None:
            raise YamlPatchError(
                "插件条目里没有 config 段，无法定位 providers"
            )
        if config.node.kind != "map":
            raise YamlPatchError(
                f"插件条目的 config 段不是块状映射（第 {config.line + 1} 行），暂不支持"
            )
        providers = (config.node.entries or {}).get("providers")
        where = "config.providers"
    else:
        providers = (entry_node.entries or {}).get("providers")
        where = "providers"
    if providers is None:
        raise YamlPatchError(f"插件条目下缺少 {where}，无法列出路由")
    if providers.node.kind != "map":
        raise YamlPatchError(f"{where} 不是块状映射，暂不支持")
    return providers.node


# --------------------------------------------------------------------------- #
# 读取接口
# --------------------------------------------------------------------------- #


def list_routes(text: str, kind: str, entry: str) -> list[str]:
    """返回 ``entry`` 下 ``providers`` 的路由名，按出现顺序。"""
    doc = _prepare(text, kind)
    providers = _providers_node(_entry_node(doc, entry), kind)
    return list(providers.order or [])


def read_route(text: str, kind: str, entry: str, route: str) -> dict | None:
    """返回该路由的浅层字段映射；路由不存在返回 ``None``。"""
    doc = _prepare(text, kind)
    providers = _providers_node(_entry_node(doc, entry), kind)
    route_entry = (providers.entries or {}).get(route)
    if route_entry is None:
        return None
    node = route_entry.node
    if node.kind == "scalar" and node.value is None:
        return {}
    if node.kind != "map":
        raise YamlPatchError(
            f"路由 {route!r} 不是块状映射（第 {route_entry.line + 1} 行），暂不支持读取"
        )
    return {key: _node_value(node.entries[key].node) for key in (node.order or [])}  # type: ignore[index]


def read_model_ids(text: str, kind: str, entry: str, route: str) -> list[str]:
    """该路由 ``models`` 列表中每个条目的 id（无 id 的条目跳过，块/流风格都支持）。"""
    doc = _prepare(text, kind)
    providers = _providers_node(_entry_node(doc, entry), kind)
    route_entry = (providers.entries or {}).get(route)
    if route_entry is None:
        return []
    node = route_entry.node
    if node.kind != "map":
        return []
    models = (node.entries or {}).get("models")
    if models is None:
        return []
    value = _node_value(models.node)
    if not isinstance(value, list):
        return []
    ids: list[str] = []
    for element in value:
        if isinstance(element, dict):
            mid = element.get("id")
            if isinstance(mid, str) and mid.strip():
                ids.append(mid)
        elif isinstance(element, str) and element.strip():
            # 流风格里直接把标量当条目（等价于 id）
            ids.append(element)
    return ids


def read_models_style(text: str, kind: str, entry: str, route: str) -> str | None:
    """返回该路由 ``models`` 的写法，供调用方判断能否安全改写。

    * ``"seq"`` —— 块序列（唯一可以按条目原文手术的写法）；
    * ``"flow"`` —— 非空流风格 ``[...]``（无法保留原文，调用方应拒绝改写）；
    * ``"empty-list"`` —— 空流风格 ``[]``（没有内容要保留，可以重写成块序列）；
    * ``"null"`` —— 键存在但值为空（``models:``），等价于空列表；
    * ``"scalar"`` —— 标量值（含块标量 ``models: |``）；
    * ``"map"`` —— 块状映射（例如按 id 索引的对象）；
    * ``None`` —— 路由或键不存在（由写入接口决定追加位置）。
    """
    doc = _prepare(text, kind)
    providers = _providers_node(_entry_node(doc, entry), kind)
    route_entry = (providers.entries or {}).get(route)
    if route_entry is None:
        return None
    node = route_entry.node
    if node.kind != "map":
        return None
    models = (node.entries or {}).get("models")
    if models is None:
        return None
    model_kind = models.node.kind
    if model_kind == "seq":
        return "seq"
    if model_kind == "flow":
        value = models.node.value
        if isinstance(value, (list, tuple)) and not value:
            return "empty-list"
        return "flow"
    if model_kind == "map":
        return "map"
    # ``models:`` (empty) and ``models: |`` (block scalar) both parse to a
    # scalar node with no value; only the raw header text tells them apart.
    return "scalar" if (models.node.text or "").strip() else "null"


# --------------------------------------------------------------------------- #
# 写入接口
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class KeepItem:
    """保留既有条目的原文片段（字节级不变）。"""

    id: str
    occurrence: int = 0  # 同 id 第几条，从 0 开始


@dataclass(frozen=True)
class NewItem:
    """插入的新条目（字段见 SPEC §4，按规范化渲染）。"""

    fields: dict


@dataclass
class _Chunk:
    """models 块里的一个条目：``[lead_start, body_start)`` 是前导注释，``[body_start, body_end)`` 是内容+尾部空行。"""

    lead_start: int
    body_start: int
    body_end: int


def _models_region(
    lines: Sequence[_Line], seq: _Node, key_indent: int, key_line: int
) -> tuple[int, int]:
    """返回 models 块原文区域的 ``[start, stop)``。"""
    start = seq.start
    probe = start - 1
    while probe > key_line and _is_comment(lines[probe]):
        probe -= 1
    start = probe + 1
    total = len(lines)
    j = seq.start
    while j < total:
        line = lines[j]
        if _is_blank(line):
            j += 1
            continue
        indent = _indent_of(line)
        if _is_comment(line):
            if indent <= key_indent:
                break
            j += 1
            continue
        if indent <= key_indent:
            break
        j += 1
    return start, j


def _split_chunks(
    lines: Sequence[_Line], start: int, stop: int, item_indent: int
) -> list[_Chunk]:
    chunks: list[_Chunk] = []
    pos = start
    while pos < stop:
        lead = pos
        while pos < stop and (_is_comment(lines[pos]) or _is_blank(lines[pos])):
            pos += 1
        if pos >= stop:
            break
        indent = _indent_of(lines[pos])
        if indent != item_indent or not _is_dash_text(lines[pos].content[indent:]):
            raise YamlPatchError(
                f"第 {pos + 1} 行的缩进与既有条目不一致（期望 {item_indent} 个空格），"
                "无法安全重排 models 块"
            )
        cursor = pos + 1
        while cursor < stop:
            line = lines[cursor]
            if _is_blank(line):
                cursor += 1
                continue
            if _indent_of(line) > item_indent:
                cursor += 1
                continue
            break
        chunks.append(_Chunk(lead, pos, cursor))
        pos = cursor
    if chunks and chunks[-1].body_end < stop:
        # 尾部只剩注释/空行：并入最后一个条目，保证不丢字节
        chunks[-1] = _Chunk(chunks[-1].lead_start, chunks[-1].body_start, stop)
    return chunks


def _chunk_fields(lines: Sequence[_Line], chunk: _Chunk) -> dict[str, _Node] | None:
    indent = _indent_of(lines[chunk.body_start])
    parser = _Parser(lines, chunk.body_end)
    node = parser.parse_seq(chunk.body_start, chunk.body_end, indent)
    if not node.items:
        return None
    item = node.items[0].node
    if item.kind != "map":
        return None
    return {key: item.entries[key].node for key in (item.order or [])}  # type: ignore[index]


def _chunk_id(lines: Sequence[_Line], chunk: _Chunk) -> str | None:
    fields = _chunk_fields(lines, chunk)
    if not fields:
        return None
    id_node = fields.get("id")
    if id_node is None:
        return None
    text = _scalar_text(id_node)
    if text is None or not text.strip():
        return None
    return text


def _reject_unsafe_region(lines: Sequence[_Line], start: int, stop: int) -> None:
    for i in range(start, stop):
        content = lines[i].content
        if _JS_TAG in content:
            raise YamlPatchError(
                f"第 {i + 1} 行含 {_JS_TAG} 表达式：改写该区域前请手工处理"
            )
        stripped = content.strip()
        if stripped.startswith("#"):
            continue
        if _ANCHOR_VALUE_RE.search(content):
            raise YamlPatchError(
                f"第 {i + 1} 行使用了锚点/别名（& / *）：本工具无法保证改写后引用仍成立"
            )


def _needs_quote(text: str) -> bool:
    if text == "":
        return True
    if text != text.strip():
        return True
    if text[0] in _UNSAFE_PREFIX:
        return True
    if ": " in text or " #" in text or text.endswith(":"):
        return True
    if any(ch in text for ch in "\n\r\t"):
        return True
    lowered = text.lower()
    if lowered in _BOOL_NULL_WORDS:
        return True
    if _NUMBER_RE.match(text):
        return True
    return False


def _quote(text: str) -> str:
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _render_scalar(value: Any) -> str:
    """按 SPEC §4 渲染一个标量。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _quote(value) if _needs_quote(value) else value
    raise YamlPatchError(f"无法渲染该值：{value!r}（只支持标量/列表/映射）")


def _render_flow_seq(values: Iterable[Any]) -> str:
    rendered = []
    for element in values:
        if isinstance(element, (dict, list, tuple)):
            raise YamlPatchError(
                "列表里嵌套映射/列表暂不支持渲染（本工具只需要 input: [text, image] 形态）"
            )
        rendered.append(_render_scalar(element))
    return "[" + ", ".join(rendered) + "]"


def _emit_map(
    fields: dict, order: Sequence[str], *, line_prefix: str, cont_prefix: str
) -> list[str]:
    out: list[str] = []
    first = True
    for key in order:
        prefix = line_prefix if first else cont_prefix
        first = False
        value = fields[key]
        if isinstance(value, dict):
            if value:
                out.append(f"{prefix}{key}:")
                out.extend(
                    _emit_map(
                        value,
                        list(value.keys()),
                        line_prefix=cont_prefix + "  ",
                        cont_prefix=cont_prefix + "  ",
                    )
                )
            else:
                out.append(f"{prefix}{key}: {{}}")
        elif isinstance(value, (list, tuple)):
            out.append(f"{prefix}{key}: {_render_flow_seq(value)}")
        else:
            out.append(f"{prefix}{key}: {_render_scalar(value)}")
    return out


def _ordered_keys(fields: dict) -> list[str]:
    known = [key for key in MODEL_FIELD_ORDER if key in fields]
    extra = [key for key in fields if key not in MODEL_FIELD_ORDER]
    return known + extra


def _render_new_item(item: NewItem, item_indent: int) -> list[str]:
    if not isinstance(item.fields, dict):
        raise YamlPatchError(
            f"NewItem.fields 必须是 dict，收到的是 {type(item.fields).__name__}"
        )
    model_id = item.fields.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        raise YamlPatchError(
            f"NewItem 缺少非空字符串 id（收到 {model_id!r}），拒绝渲染无效条目"
        )
    if not item.fields:
        raise YamlPatchError("NewItem.fields 为空，拒绝渲染空条目")
    for key in item.fields:
        if not isinstance(key, str) or not key:
            raise YamlPatchError(f"NewItem.fields 里有非法键名：{key!r}")
    return _emit_map(
        item.fields,
        _ordered_keys(item.fields),
        line_prefix=" " * item_indent + "- ",
        cont_prefix=" " * (item_indent + 2),
    )


def _render_plan(
    plan: Sequence[Any],
    lines: Sequence[_Line],
    chunks: Sequence[_Chunk],
    item_indent: int,
) -> list[_Line]:
    """按 plan 顺序拼出 models 块的新内容行（KeepItem 用原文，NewItem 用渲染）。"""
    by_id: dict[str, list[int]] = {}
    for index, chunk in enumerate(chunks):
        chunk_id = _chunk_id(lines, chunk)
        if chunk_id is not None:
            by_id.setdefault(chunk_id, []).append(index)
    used: set[int] = set()
    output: list[_Line] = []
    for element in plan:
        if isinstance(element, KeepItem):
            indexes = by_id.get(element.id)
            if not indexes:
                raise YamlPatchError(
                    f"plan 引用了 models 中不存在的条目 id={element.id!r}"
                )
            occurrence = element.occurrence
            if not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence < 0:
                raise YamlPatchError(
                    f"KeepItem.occurrence 必须是从 0 开始的整数，收到 {occurrence!r}"
                    f"（id={element.id!r}）"
                )
            if occurrence >= len(indexes):
                raise YamlPatchError(
                    f"plan 要保留 id={element.id!r} 的第 {occurrence} 条，"
                    f"但该 id 只有 {len(indexes)} 条"
                )
            index = indexes[occurrence]
            if index in used:
                raise YamlPatchError(
                    f"plan 重复引用了 id={element.id!r} 的第 {occurrence} 条"
                )
            used.add(index)
            chunk = chunks[index]
            output.extend(lines[chunk.lead_start:chunk.body_end])
        elif isinstance(element, NewItem):
            for content in _render_new_item(element, item_indent):
                output.append(_Line(content, ""))
        else:
            raise YamlPatchError(
                f"plan 中出现未知元素类型 {type(element).__name__}（只支持 KeepItem / NewItem）"
            )
    return output


def _splice(
    doc: _Doc, start: int, stop: int, insert: Sequence[_Line], newline: str
) -> list[_Line]:
    """把 ``doc.lines[start:stop]`` 换成 ``insert``，并修好行尾。"""
    result: list[_Line] = list(doc.lines[:start])
    for line in insert:
        result.append(_Line(line.content, line.term or newline))
    result.extend(doc.lines[stop:])
    for index in range(len(result) - 1):
        if result[index].term == "":
            result[index] = _Line(result[index].content, newline)
    if result and not doc.ended_with_nl:
        result[-1] = _Line(result[-1].content, "")
    return result


def _rewrite_models_line(content: str, key_indent: int, value_repr: str | None) -> str:
    """把 ``models: <旧值>  # 注释`` 重写成 ``models: <value_repr>  # 注释``。"""
    rest = content[key_indent:]
    pair = _split_key(rest)
    if pair is None:
        raise YamlPatchError(f"无法解析 models 键行：{content.strip()!r}")
    prefix = rest[: len(rest) - len(pair[1])]
    head, comment = _split_trailing_comment(pair[1])
    gap = head[len(head.rstrip()):] or " "  # 原行尾注释前的空白，尽量原样保留
    new = prefix if not value_repr else f"{prefix} {value_repr}"
    if comment:
        new = f"{new}{gap}{comment}"
    return content[:key_indent] + new


def apply_plan(
    text: str,
    kind: str,
    entry: str,
    route: str,
    plan: Sequence[Any],
) -> str:
    """按 ``plan`` 重写该路由的 ``models`` 块，返回新文本。

    * ``KeepItem`` 精确复用原条目片段（含前导注释与尾部空行），不重新缩进；
    * ``NewItem`` 用 SPEC §4 的规范化渲染，缩进与该块既有条目一致；
    * plan 顺序即最终顺序；内容等价时原样返回输入文本（no-op 不重写文件）；
    * 缺 ``models`` 键时在路由块末尾插入；流风格（``models: []``）覆盖成块风格；
    * provider 的其他字段一个字节都不动。
    """
    doc = _prepare(text, kind)
    entry_node = _entry_node(doc, entry)
    providers = _providers_node(entry_node, kind)
    route_entry = (providers.entries or {}).get(route)
    if route_entry is None:
        known = "、".join(providers.order or []) or "无"
        raise YamlPatchError(
            f"路由 {route!r} 不存在（现有路由：{known}）"
        )
    route_node = route_entry.node
    if route_node.kind == "flow":
        raise YamlPatchError(
            f"路由 {route!r} 是流风格映射，无法安全追加 models（请先改成块风格）"
        )
    if route_node.kind == "scalar" and route_node.value is None:
        raise YamlPatchError(
            f"路由 {route!r} 没有任何块状字段，无法安全写入 models"
        )
    if route_node.kind != "map":
        raise YamlPatchError(f"路由 {route!r} 不是块状映射，无法安全写入 models")

    plan = list(plan)
    newline = doc.newline
    models_entry = (route_node.entries or {}).get("models")

    if models_entry is None:
        if not plan:
            return text
        indent = route_node.indent
        block = [_Line(" " * indent + "models:", newline)]
        block.extend(_render_plan(plan, doc.lines, [], indent + 2))
        return _finish(doc, _splice(doc, route_node.end, route_node.end, block, newline))

    key_line = doc.lines[models_entry.line]
    if _JS_TAG in key_line.content:
        raise YamlPatchError(
            f"第 {models_entry.line + 1} 行的 models 值含 {_JS_TAG} 表达式，拒绝改写"
        )
    key_indent = models_entry.indent
    node = models_entry.node

    if node.kind == "seq":
        item_indent = node.indent
        start, stop = _models_region(doc.lines, node, key_indent, models_entry.line)
        _reject_unsafe_region(doc.lines, start, stop)
        chunks = _split_chunks(doc.lines, start, stop, item_indent)
        render = _render_plan(plan, doc.lines, chunks, item_indent)
        result = _splice(doc, start, stop, render, newline)
        if not render:
            original = result[models_entry.line]
            result[models_entry.line] = _Line(
                _rewrite_models_line(original.content, key_indent, "[]"), original.term
            )
        return _finish(doc, result)

    if node.kind == "flow":
        if not isinstance(node.value, list):
            raise YamlPatchError(
                f"第 {models_entry.line + 1} 行的 models 值不是列表，无法安全重写"
            )
        for element in plan:
            if isinstance(element, KeepItem):
                raise YamlPatchError(
                    "models 目前是流风格（[...]），无法按条目原文保留；"
                    "请先把该块改成块风格再用 KeepItem"
                )
        value_repr = None if plan else "[]"
        rewritten = _Line(
            _rewrite_models_line(key_line.content, key_indent, value_repr), key_line.term
        )
        render = _render_plan(plan, doc.lines, [], key_indent + 2)
        replace_start = models_entry.line
        result = _splice(
            doc, replace_start, models_entry.line + 1, [rewritten] + render, newline
        )
        return _finish(doc, result)

    if node.kind == "scalar":
        if node.value is not None:
            raise YamlPatchError(
                f"第 {models_entry.line + 1} 行的 models 值无法安全重写："
                f"{key_line.content.strip()!r}"
            )
        if not plan:
            return text
        for element in plan:
            if isinstance(element, KeepItem):
                raise YamlPatchError(
                    f"路由 {route!r} 的 models 没有任何条目，plan 却引用了 id={element.id!r}"
                )
        render = _render_plan(plan, doc.lines, [], key_indent + 2)
        insert_at = models_entry.key_end
        return _finish(doc, _splice(doc, insert_at, insert_at, render, newline))

    raise YamlPatchError(
        f"第 {models_entry.line + 1} 行的 models 是块状映射（不是列表），无法安全重写"
    )


def _finish(doc: _Doc, result: Sequence[_Line]) -> str:
    new_text = _join_lines(result)
    if new_text == doc.text:
        return doc.text
    return new_text


__all__ = [
    "KIND_PATCH",
    "KIND_SETTINGS",
    "KeepItem",
    "NewItem",
    "YamlPatchError",
    "apply_plan",
    "detect_kind",
    "list_routes",
    "read_model_ids",
    "read_route",
]
