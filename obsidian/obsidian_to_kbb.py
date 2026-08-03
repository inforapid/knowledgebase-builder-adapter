#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Imports an Obsidian vault into a KnowledgeBase Builder database (.kdb).

Mapping
-------
    Markdown file         -> item (Items) with an HTML text note (Repository + ItemAttachments)
    folder                -> item of category "Folder" plus a "contains" relation
    parent folder         -> category of the item (ItemTemplates)
    #tag / frontmatter    -> tags (ItemTags)
    [[wikilink]]          -> "links to" relation, plus a clickable itemid:// link inside the
                             note that loads the target item as the new root of the diagram
    ![[embed]]            -> "embeds" relation
    first external URL    -> DirectLinkURL of the item
    images in the text    -> embedded into the note as a data: URI; the first one is additionally
                             attached as the item icon (attachment with preview image)
    ```mermaid block      -> rendered to SVG and embedded, when mermaid-cli is installed
                             (or --install-mermaid fetches it through npx); otherwise it stays
                             source code

Incremental import
------------------
The importer creates the table ObsidianSync and remembers a content hash per file in it. On the
next run only changed files are rewritten. Items and relations are found again through stable
URIs so that formatting applied by hand survives.

    obsidian:note:<path>            item of a note
    obsidian:folder:<path>          item of a folder
    obsidian:rel:<from>|<to>|<kind> relation

Usage
-----
    python obsidian_to_kbb.py --vault PATH --db target.kdb [--template empty.kdb]

See --help for the remaining options.
"""

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import uuid

# ------------------------------------------------------------------------------------------------
# Optional dependencies
# ------------------------------------------------------------------------------------------------

try:
    import markdown as _markdown
except ImportError:
    _markdown = None

try:
    import yaml as _yaml
except ImportError:
    _yaml = None

try:
    from PIL import Image as _Image
except ImportError:
    _Image = None

try:
    import latex2mathml.converter as _latex2mathml
except ImportError:
    _latex2mathml = None


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"}
SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", ".stfolder"}

ICON_MAX_EDGE = 96          # edge length of the item icon
EMBED_MAX_EDGE = 900        # edge length of images embedded into the note
EMBED_MAX_BYTES = 2_000_000 # larger images are not embedded
EMBED_MAX_DEPTH = 3         # nesting depth for ![[note#section]] transclusions

# Formatting of the diagram, written into DiagramProperties. The keys are the ones the
# formatting gallery itself writes.
DIAGRAM_PROPERTIES = {
    "dl": 1,                    # layout: mindmap radial, straight lines
    "col": 3,                   # colouring: category wise
    "ics": "Spectral",          # colour scheme: spectral colours
    "bp": "cross-stitch",       # background image: cross stitch
    "bc": [255, 255, 255],      # background colour: white
    "ia": 1,                    # item transparency 100 %
    "irc": 1,                   # invert relation colours
}

# Node label: the symbol sits in its own table cell of an HTML label and is therefore shown
# larger than the text beside it - the same structure the application itself produces when an
# emoji is put in front of a node name (skill create-edit-nodes).
FOLDER_ICON = "\U0001F4C1"
ICON_LABEL_HTML = ('<html><span style="display:inline-table">'
                   '<span style="display:table-cell;vertical-align:middle;font-size:2em;">%s</span>'
                   '<span style="display:table-cell;vertical-align:middle"> %s</span>'
                   '</span></html>')

DESCRIPTION_MAX = 400       # character budget of a description - only whole sentences are taken


# ================================================================================================
# Helpers
# ================================================================================================

def unique_id():
    """Matches DataAccessHelper::getUniqueID (32 hex characters)."""
    return uuid.uuid4().hex


def html_escape(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))


def html_to_text(html):
    """Matches DataAccessHelper::htmlToText - only applies to a <html> prefix."""
    if html[:6].lower() != "<html>":
        return html
    html = re.sub(r"<br[ /]*>", " ", html, flags=re.I)
    html = re.sub(r"<[^>]+>", "", html)
    for entity, char in (("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"),
                         ("&quot;", '"'), ("&#39;", "'"), ("&#x27;", "'"), ("&amp;", "&")):
        html = re.sub(re.escape(entity), char, html, flags=re.I)
    return html


def strip_tags(html):
    """Rough text extraction from arbitrary HTML - used for description and search content."""
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br[ /]*>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", html)
    text = re.sub(r"<[^>]+>", "", html)
    for entity, char in (("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"),
                         ("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&")):
        text = text.replace(entity, char)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def get_next_search_string(search_string, start_pos):
    """
    Reimplementation of DataAccessHelper::getNextSearchString.

    isStopWord always returns false there, so the function advances to the beginning of the next
    word exactly once. Returns (remaining string, new start position).
    """
    match = re.search(r"[ \t\r\n]", search_string)
    if not match:
        return "", start_pos

    pos = match.start()
    length = len(search_string)
    pos += 1
    while pos < length and not search_string[pos].isalnum():
        pos += 1

    if length - pos > 0:
        return search_string[pos:], start_pos + pos
    return "", start_pos


def strip_leading_symbols(text):
    """Removes leading symbols and emoji - matches the preg_replace in setSearchText."""
    index = 0
    while index < len(text) and unicodedata.category(text[index]).startswith("S"):
        index += 1
    return text[index:]


def icon_label(name, icon):
    """
    Label eines Elements. Mit Symbol wird daraus ein HTML-Label, in dem das Symbol in einer
    eigenen Tabellenzelle groesser dargestellt wird; ohne Symbol bleibt der reine Text.
    """
    icon = (icon or "").strip()
    if not icon:
        return name

    # Der Abstand entsteht durch die Tabellenzelle, nicht durch ein Leerzeichen im Symbol
    return ICON_LABEL_HTML % (html_escape(icon), html_escape(name))


# Ein Symbol samt allem, was dazugehoert: Variantenselektor, Hautton, und ueber ZWJ verbundene
# Folgesymbole. Ohne das wuerde aus einem zusammengesetzten Emoji nur das erste Zeichen geholt.
EMOJI_RE = re.compile(
    "[\\U0001F000-\\U0001FAFF\\u2190-\\u2BFF\\u2600-\\u27BF]"
    "[\\uFE0E\\uFE0F\\U0001F3FB-\\U0001F3FF]*"
    "(?:\\u200D[\\U0001F000-\\U0001FAFF\\u2600-\\u27BF][\\uFE0E\\uFE0F\\U0001F3FB-\\U0001F3FF]*)*")


def split_leading_icon(text):
    """
    Trennt ein im Text enthaltenes Symbol ab. Zurueck kommen Symbol und der Text ohne dieses
    Symbol - das Symbol wandert damit aus dem Namen nach vorne in die eigene Zelle des Labels.
    Ohne Symbol bleibt der Text unveraendert.
    """
    match = EMOJI_RE.search(text or "")
    if not match:
        return "", text

    rest = (text[:match.start()] + text[match.end():])
    rest = re.sub(r"\s{2,}", " ", rest).strip(" -–—:")

    return match.group(0), (rest or text)


def slugify_key(name):
    """Comparison key for note names - Obsidian matches case insensitively."""
    return name.strip().lower()


def split_wikilink(inner):
    """
    Splits the inside of a [[wikilink]] into target, anchor and label. The Digital Garden
    exporter escapes the pipe and writes "[[path\\|text]]"; without undoing that the target
    would keep a trailing backslash and resolve to nothing.
    """
    inner = inner.replace("\\|", "|")
    head = inner.split("|")[0].strip()
    label = inner.split("|", 1)[1].strip() if "|" in inner else ""
    target = head.split("#")[0].strip()
    anchor = head.split("#", 1)[1].strip() if "#" in head else ""
    return target, anchor, label


def clean_title(title):
    """
    Strips Markdown and Obsidian syntax from a heading so that it can serve as an item name.
    Headings such as "# [[Obsidian Publish|Publish]] sites" would otherwise keep their brackets.
    """
    title = re.sub(r"!?\[\[([^\]\[]+?)\]\]",
                   lambda m: (split_wikilink(m.group(1))[2] or split_wikilink(m.group(1))[0]), title)
    title = re.sub(r"!?\[([^\]]*)\]\([^)\s]*\)", r"\1", title)
    title = re.sub(r"`([^`]*)`", r"\1", title)
    title = re.sub(r"\*\*(.+?)\*\*", r"\1", title)
    title = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", title)
    title = re.sub(r"^#+\s*", "", title)
    return re.sub(r"\s+", " ", title).strip()


# ================================================================================================
# Markdown -> HTML
# ================================================================================================

def markdown_to_html(text):
    """Converts Markdown to HTML. Uses the markdown package when it is available."""
    if _markdown is not None:
        return _markdown.markdown(text, extensions=["extra", "sane_lists"])
    return _minimal_markdown(text)


def _minimal_markdown(text):
    """Frugal replacement in case the markdown package is missing. Covers the common cases."""
    out = []
    in_list = False
    in_code = False

    for line in text.split("\n"):
        if line.strip().startswith("```"):
            if in_code:
                out.append("</pre>")
                in_code = False
            else:
                out.append("<pre>")
                in_code = True
            continue

        if in_code:
            out.append(html_escape(line))
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        bullet = re.match(r"^\s*[-*+]\s+(.*)$", line)

        if bullet:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>%s</li>" % _inline_markdown(bullet.group(1)))
            continue

        if in_list:
            out.append("</ul>")
            in_list = False

        if heading:
            level = len(heading.group(1))
            out.append("<h%d>%s</h%d>" % (level, _inline_markdown(heading.group(2)), level))
        elif line.strip() == "":
            out.append("")
        else:
            out.append("<p>%s</p>" % _inline_markdown(line))

    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("</pre>")

    return "\n".join(out)


def _inline_markdown(text):
    text = html_escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


# ================================================================================================
# Obsidian parsing
# ================================================================================================

WIKILINK_RE = re.compile(r"(!?)\[\[([^\]\[]+?)\]\]")
MD_LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
INLINE_TAG_RE = re.compile(r"(?<![\w&#/])#([A-Za-z0-9_\-/À-ɏ]{1,60})")
# Masked away before the search for inline tags: inside HTML or SVG markup a "#" is a colour
# value (fill="#1e1e1e") or an identifier, and inside a wikilink it points at a heading. Obsidian
# does not read a tag in either place. The notes of this Digital Garden carry whole Excalidraw
# drawings as inline SVG, which alone produced 29 colour codes as tags.
NON_TAG_CONTEXT_RE = re.compile(r"(?is)<style[^>]*>.*?</style>|<script[^>]*>.*?</script>"
                                r"|<[^>]+>|\[\[[^\]\[]*\]\]")
# Dataview inline field at the start of a line: "field:: value". Only this form is supported,
# the bracketed variant "(field:: value)" in the middle of a sentence is too hard to delimit.
DATAVIEW_FIELD_RE = re.compile(r"^[ \t]*([A-Za-z0-9_À-ɏ][A-Za-z0-9 _/\-À-ɏ]{0,40})::[ \t]*(.+)$",
                               re.M)
CODE_BLOCK_RE = re.compile(r"(?s)```.*?```|`[^`\n]*`")
FENCED_CODE_RE = re.compile(r"(?s)```.*?```")

# Obsidian specific syntax that plain Markdown does not know
OBSIDIAN_COMMENT_RE = re.compile(r"(?s)%%.*?%%")
HIGHLIGHT_RE = re.compile(r"==([^=\n]+)==")
STRIKETHROUGH_RE = re.compile(r"~~([^~\n]+)~~")
BLOCK_REF_RE = re.compile(r"[ \t]*\^[A-Za-z0-9\-]{2,}[ \t]*$", re.M)
# Obsidian writes a nested callout as "> > [!info]", with a blank between the quote markers.
# Matching only ">+" left those unconverted, so the "[!info]" stayed in the text.
CALLOUT_RE = re.compile(r"^([ \t]*>(?:[ \t]*>)*)[ \t]*\[!([A-Za-z\-]+)\][+-]?[ \t]*(.*)$", re.M)

# Lines that start a block which Python-Markdown only recognises after a blank line
# Quote markers of a line ("> ", "> > ") and what starts a block once they are stripped off
QUOTE_PREFIX_RE = re.compile(r"^[ \t]*(?:>[ \t]*)*")
BLOCK_CONTENT_RE = re.compile(r"^[ \t]*(?:[-*+][ \t]+|\d+[.)][ \t]+|\|)")


def _apply_outside(pattern, text, func):
    """Applies func only to the parts of the text that pattern does not match."""
    parts = []
    last = 0
    for match in pattern.finditer(text):
        parts.append(func(text[last:match.start()]))
        parts.append(match.group(0))
        last = match.end()
    parts.append(func(text[last:]))
    return "".join(parts)


def _apply_outside_code(text, func):
    """Applies func only outside of fenced code blocks and inline code spans."""
    return _apply_outside(CODE_BLOCK_RE, text, func)


def strip_obsidian_comments(text):
    """
    Removes %% comments %%. They are meant for the author only and do not belong in the note.

    Only fenced code blocks are protected here, deliberately not inline code spans: comments
    regularly contain a piece of inline code, and protecting those spans would split the text
    exactly between the opening and the closing %%, leaving both markers behind.
    """
    return _apply_outside(FENCED_CODE_RE, text,
                          lambda segment: OBSIDIAN_COMMENT_RE.sub("", segment))


# $$block$$ and $inline$ formulas. The block form is looked for first, otherwise its two dollar
# signs would be read as an empty inline formula.
MATH_BLOCK_RE = re.compile(r"\$\$(.+?)\$\$", re.S)
MATH_INLINE_RE = re.compile(r"\$([^$\n]+)\$")
MATH_PLACEHOLDER = "xKBBMATHx%dxKBBMATHx"
MATH_PLACEHOLDER_RE = re.compile(r"xKBBMATHx(\d+)xKBBMATHx")


def extract_math(text):
    """
    Replaces LaTeX formulas with placeholders and converts them to MathML, which is the W3C
    standard and is rendered by browsers themselves. It has to happen before the Markdown
    conversion, otherwise "$a_1$" would lose its underscore to the italics rule.

    Returns the text with placeholders and the list of MathML fragments. Without the
    latex2mathml package the formulas are left untouched, exactly as before.
    """
    formulas = []

    if _latex2mathml is None:
        return text, formulas

    def convert(source, display):
        try:
            mathml = _latex2mathml.convert(source.strip(), display=display)
        except Exception:
            return None                                 # unconvertible: leave the source alone
        formulas.append(mathml)
        return MATH_PLACEHOLDER % (len(formulas) - 1)

    def replace_block(match):
        source = match.group(1)

        # A block formula inside a callout carries the "> " markers of the quote on every line.
        # Those of the continuation lines sit inside the match and have to come out of the
        # source; the marker of the opening line stands before the match and stays where it is,
        # so the quote does not fall apart and no marker ends up as plain text.
        line_start = match.string.rfind("\n", 0, match.start()) + 1
        lead = match.string[line_start:match.start()]

        if re.match(r"^[ \t]*(?:>[ \t]*)+$", lead):
            source = re.sub(r"(?m)^[ \t]*(?:>[ \t]*)*", "", source)

        return convert(source, "block") or match.group(0)

    def replace_inline(match):
        return convert(match.group(1), "inline") or match.group(0)

    # Formulas inside fenced code blocks are meant to be read, not rendered
    text = _apply_outside(FENCED_CODE_RE, text, lambda s: MATH_BLOCK_RE.sub(replace_block, s))
    text = _apply_outside(FENCED_CODE_RE, text, lambda s: MATH_INLINE_RE.sub(replace_inline, s))

    return text, formulas


def insert_math(html, formulas):
    """Puts the MathML back in place of the placeholders."""
    if not formulas:
        return html

    return MATH_PLACEHOLDER_RE.sub(lambda m: formulas[int(m.group(1))], html)


def convert_obsidian_syntax(text):
    """
    Translates the Obsidian extensions that Markdown does not cover:
    ==highlight== and ~~strikethrough~~ become HTML, callouts become a blockquote with a bold
    heading, and block references (^abc123) are dropped.
    """
    text = BLOCK_REF_RE.sub("", text)
    text = CALLOUT_RE.sub(
        lambda m: "%s **%s**%s" % (m.group(1), m.group(2).capitalize(),
                                   (" - " + m.group(3)) if m.group(3).strip() else ""),
        text)

    def inline(segment):
        segment = HIGHLIGHT_RE.sub(r"<mark>\1</mark>", segment)
        segment = STRIKETHROUGH_RE.sub(r"<del>\1</del>", segment)
        return segment

    return _apply_outside_code(text, inline)


def ensure_blank_line_before_blocks(text):
    """
    Obsidian starts a list, quote or table right after a line of text; Python-Markdown needs a
    blank line in front of it, otherwise the whole block ends up in a single paragraph.

    Inside a quote the same applies, and there the blank line has to carry the quote markers of
    its surroundings - a truly empty line would cut the quote in two. That is why the comparison
    runs on the content behind the markers and not on the raw line.
    """
    lines = text.split("\n")
    out = []
    in_fence = False

    for line in lines:
        if line.strip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue

        if not in_fence and out:
            prefix = QUOTE_PREFIX_RE.match(line).group(0)
            previous_prefix = QUOTE_PREFIX_RE.match(out[-1]).group(0)
            content = line[len(prefix):]
            previous_content = out[-1][len(previous_prefix):]
            level = prefix.count(">")
            previous_level = previous_prefix.count(">")

            if previous_content.strip() != "":
                if level > previous_level:
                    # a quote begins right after a line of text
                    out.append(previous_prefix.rstrip())
                elif (level == previous_level) and BLOCK_CONTENT_RE.match(content) \
                        and not BLOCK_CONTENT_RE.match(previous_content):
                    # a list or a table begins, on the same quote level
                    out.append(prefix.rstrip())

        out.append(line)

    return "\n".join(out)


class Note(object):
    """A parsed Markdown file."""

    def __init__(self, path, rel_path):
        self.path = path
        self.rel_path = rel_path            # posix notation, relative to the vault
        self.title = os.path.splitext(os.path.basename(rel_path))[0]
        self.folder = os.path.dirname(rel_path)
        self.frontmatter = {}
        self.body = ""
        self.tags = []
        self.links = []                     # [(target, embedded)]
        self.embedded_files = []            # file names taken from ![[...]]
        self.description = ""
        self.description_from_text = True   # False when it comes from the frontmatter
        self.expanded_body = ""             # body with ![[embeds]] resolved, set while rendering
        self.typed_links = []               # [(field, target)] from "field:: [[target]]"
        self.url = ""
        self.content_hash = ""
        self.item_id = -1
        self.aliases = []


def split_frontmatter(text):
    """Separates YAML frontmatter from the body."""
    if not text.startswith("---"):
        return {}, text

    end = re.search(r"^---\s*$", text[3:], flags=re.M)
    if not end:
        return {}, text

    raw = text[3:3 + end.start()]
    body = text[3 + end.end():]

    if _yaml is None:
        return _minimal_yaml(raw), body.lstrip("\n")

    try:
        data = _yaml.safe_load(raw)
        return (data if isinstance(data, dict) else {}), body.lstrip("\n")
    except Exception:
        return _minimal_yaml(raw), body.lstrip("\n")


def _minimal_yaml(raw):
    """Very simple replacement in case PyYAML is missing: key: value and '-' lists."""
    data = {}
    key = None
    for line in raw.split("\n"):
        if not line.strip():
            continue
        list_item = re.match(r"^\s+-\s+(.*)$", line)
        if list_item and key:
            data.setdefault(key, [])
            if isinstance(data[key], list):
                data[key].append(list_item.group(1).strip().strip("\"'"))
            continue
        pair = re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*(.*)$", line)
        if pair:
            key = pair.group(1)
            value = pair.group(2).strip()
            if value == "":
                data[key] = []
            elif value.startswith("[") and value.endswith("]"):
                data[key] = [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
            else:
                data[key] = value.strip("\"'")
    return data


def normalize_tag_list(value):
    """Frontmatter tags may be a string, a list, or comma separated."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,\s]+", value)
    elif isinstance(value, (list, tuple)):
        parts = []
        for entry in value:
            if entry is None:
                continue
            parts.extend(re.split(r"[,\s]+", str(entry)))
    else:
        return []
    return [p.strip().lstrip("#") for p in parts if p and p.strip().lstrip("#")]


def normalize_alias_list(value):
    """
    Frontmatter aliases, unlike tags, may contain spaces: "Import GitHub Readme" is one alias
    and not three. Splitting it would let that note claim the names "Import", "GitHub" and
    "Readme" and shadow the real README of the vault. Only a plain string is split, on commas.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = [str(entry) for entry in value if entry is not None]
    else:
        return []
    return [p.strip() for p in parts if p and p.strip()]


def parse_note(path, rel_path):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        raw = handle.read()

    note = Note(path, rel_path)
    note.content_hash = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()
    note.frontmatter, body = split_frontmatter(raw)

    # Comments are removed right away so that neither tags nor links are picked up from them
    note.body = strip_obsidian_comments(body)

    # Obsidian accepts both spellings of the key
    aliases = normalize_alias_list(note.frontmatter.get("aliases"))
    aliases += normalize_alias_list(note.frontmatter.get("alias"))

    seen_aliases = set()
    for alias in aliases:
        key = slugify_key(alias)
        if key and (key not in seen_aliases):
            seen_aliases.add(key)
            note.aliases.append(alias)

    body_without_code = CODE_BLOCK_RE.sub(" ", note.body)

    # Name: frontmatter title, first H1 heading, first alias, file name. The alias comes after
    # the heading on purpose - in Obsidian it is an additional name, not the primary one. The
    # heading is looked for outside of code blocks, otherwise a shell comment such as
    # "# cat file.c" would end up as the name of the note.
    fm_title = note.frontmatter.get("title")
    if isinstance(fm_title, str) and fm_title.strip():
        note.title = clean_title(fm_title)
    else:
        cleaned = ""
        h1 = re.search(r"^#\s+(.+)$", body_without_code, flags=re.M)
        if h1:
            cleaned = clean_title(h1.group(1))
        if not cleaned and note.aliases:
            cleaned = clean_title(note.aliases[0])
        if cleaned:
            note.title = cleaned

    # Tags: frontmatter plus inline tags outside of code blocks, HTML markup and wikilinks
    tags = normalize_tag_list(note.frontmatter.get("tags"))
    for match in INLINE_TAG_RE.finditer(NON_TAG_CONTEXT_RE.sub(" ", body_without_code)):
        tags.append(match.group(1))

    seen = set()
    for tag in tags:
        # Obsidian does not allow a tag to consist of digits only. Without this check every
        # "#3" or "#811" in the text - issue numbers, anchors - would become a tag.
        if tag.isdigit():
            continue

        key = tag.lower()
        if key not in seen:
            seen.add(key)
            note.tags.append(tag)

    # Dataview inline fields: "field:: [[target]]" is a typed link, the field name becomes the
    # label of the relation. Their links are recorded here so that the generic pass below does
    # not additionally create an unlabelled relation to the same target.
    typed_spans = []
    for match in DATAVIEW_FIELD_RE.finditer(body_without_code):
        field = re.sub(r"\s+", " ", match.group(1)).strip()
        targets = list(WIKILINK_RE.finditer(match.group(2)))
        if not field or not targets:
            continue

        for link in targets:
            if link.group(1) == "!":                    # an embed is not a typed link
                continue
            target = split_wikilink(link.group(2))[0]
            if not target:
                continue
            note.typed_links.append((field, target))
            typed_spans.append((match.start(2) + link.start(), match.start(2) + link.end()))

    def is_typed_span(start, end):
        for span in typed_spans:
            if (start >= span[0]) and (end <= span[1]):
                return True
        return False

    # Wikilinks
    for match in WIKILINK_RE.finditer(body_without_code):
        embedded = match.group(1) == "!"
        inner = match.group(2)
        target, anchor, alias = split_wikilink(inner)
        if not target:
            continue
        if embedded and os.path.splitext(target)[1].lower() in IMAGE_SUFFIXES:
            note.embedded_files.append(target)
        elif not is_typed_span(match.start(), match.end()):
            note.links.append((target, embedded, alias))

    # Markdown links: internal .md targets become relations, the first external URL becomes the URL
    for match in MD_LINK_RE.finditer(body_without_code):
        embedded = match.group(1) == "!"
        target = match.group(3).strip()
        if target.startswith(("http://", "https://")):
            if not embedded and not note.url:
                note.url = target
        elif embedded and os.path.splitext(target)[1].lower() in IMAGE_SUFFIXES:
            note.embedded_files.append(target)
        elif target.lower().endswith(".md"):
            note.links.append((os.path.splitext(os.path.basename(target))[0], False, ""))

    for key in ("url", "source", "link"):
        value = note.frontmatter.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            note.url = value
            break

    # Description. A description kept by hand is shortened by the same rule, but never replaced
    # by the note text later on.
    fm_desc = note.frontmatter.get("description") or note.frontmatter.get("summary")
    if isinstance(fm_desc, str) and fm_desc.strip():
        note.description = build_description(fm_desc)
        note.description_from_text = False
    else:
        note.description = build_description(plain_preview(note.body, note.title))
        note.description_from_text = True

    return note


def _body_without_title(body, title):
    """
    Removes a leading H1 that merely repeats the title. The comparison runs on the cleaned
    heading, because the title itself has already been stripped of Markdown syntax.
    """
    def drop(match):
        return "" if clean_title(match.group(1)) == title else match.group(0)

    return re.sub(r"^#\s+(.+)$", drop, body, count=1, flags=re.M)


def heading_key(text):
    """
    Comparison key for heading names. Obsidian matches them loosely, so the embed
    ![[README#What is the Obsidian Hub]] finds the heading "## What is the Obsidian Hub?".
    """
    return re.sub(r"\W+", " ", text.lower(), flags=re.UNICODE).strip()


def find_heading_section(body, anchor):
    """
    Returns the Markdown of the section that a "#Heading" embed refers to, including the heading
    itself - Obsidian displays it that way. The section ends at the next heading of the same or a
    higher level. Returns None when there is no such heading.
    """
    wanted = heading_key(anchor)
    if not wanted:
        return None

    lines = body.split("\n")
    start = -1
    level = 0

    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if not match:
            continue
        if start < 0:
            if heading_key(clean_title(match.group(2))) == wanted:
                start = index
                level = len(match.group(1))
        elif len(match.group(1)) <= level:
            return "\n".join(lines[start:index])

    return None if start < 0 else "\n".join(lines[start:])


def find_block(body, block_id):
    """
    Returns the block that a "#^blockid" embed refers to: everything from the preceding blank
    line up to the block marker. Returns None when the marker does not exist.
    """
    match = re.search(r"\^" + re.escape(block_id) + r"[ \t]*$", body, flags=re.M)
    if not match:
        return None

    start = body.rfind("\n\n", 0, match.start())
    start = 0 if start < 0 else start + 2
    return body[start:match.start()].rstrip()


# A dot closing one of these does not end a sentence
SENTENCE_ABBREVIATIONS = ("z. b.", "u. a.", "d. h.", "u. u.", "s. o.", "s. u.", "vgl.", "bzw.",
                          "ca.", "ggf.", "inkl.", "evtl.", "etc.", "nr.", "dr.", "prof.",
                          "e.g.", "i.e.", "cf.", "mr.", "mrs.", "ms.", "vs.", "approx.", "fig.")

# End of sentence: western punctuation followed by whitespace, or CJK punctuation, which is not
# followed by a space. Closing quotes and brackets still belong to the sentence.
SENTENCE_END_RE = re.compile("(?<=[.!?])[\"')\\]”’]*\\s+|(?<=[。！？])[”’」』）】]*")


def split_sentences(text):
    """
    Splits running text into sentences. Every piece carries its own trailing separator, so that
    joining them yields the original text again - Chinese has no space after 。 and must not gain
    one. Abbreviations and single initials do not end a sentence.
    """
    pieces = []
    start = 0

    for match in SENTENCE_END_RE.finditer(text):
        piece = text[start:match.end()]
        candidate = piece.strip()
        if not candidate:
            continue

        lowered = candidate.lower()
        if any(lowered.endswith(abbreviation) for abbreviation in SENTENCE_ABBREVIATIONS):
            continue                                    # abbreviation, the sentence goes on
        if re.search(r"(?:^|\s)\w\.$", candidate):
            continue                                    # single initial such as "J."

        pieces.append(piece)
        start = match.end()

    if text[start:].strip():
        pieces.append(text[start:])

    return pieces


def build_description(text, limit=DESCRIPTION_MAX):
    """
    Takes whole sentences until the character budget is used up. A sentence that would exceed the
    budget is left out completely, so a description never breaks off in the middle of a sentence.
    Only when the very first sentence is already too long is it cut at a word boundary - otherwise
    exactly the most long-winded notes would end up without any description at all.
    """
    text = re.sub(r"[ \t\r\n\f\v]+", " ", text or "").strip()
    if not text:
        return ""

    pieces = split_sentences(text)
    if not pieces:
        return ""

    result = ""
    taken = 0

    for piece in pieces:
        candidate = (result + piece).rstrip()
        if len(candidate) > limit:
            break
        result += piece
        taken += 1

    result = result.rstrip()

    if not result:
        # The ellipsis has to fit into the budget as well
        cut = pieces[0][:limit - 1]
        space = cut.rfind(" ")
        if space > (limit // 2):
            cut = cut[:space]
        return cut.rstrip(" ,;:-") + "…"

    if taken < len(pieces):
        result += "…"

    return result


# Lines that do not carry running text: headings, quotes, tables, lists, images, callouts
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
# "Plugin ID: ...", "Developed by: ...", "Modes: ..." - a short label followed by a value
KEY_VALUE_RE = re.compile(r"^\s*[A-Za-zÀ-ɏ][A-Za-z0-9 /_\-À-ɏ]{0,30}:\s")


def first_prose_paragraph(body):
    """
    Returns the first block of running text. Headings, lists, tables, quotes, code blocks and
    lines holding nothing but an image are skipped, because a description made of "🗂️ 02.01
    Plugins by Category" or a row of badges says nothing about the note.

    A block whose lines all read "Label: value" is skipped as well. Thousands of notes in the
    Obsidian Hub open with such a header ("Plugin ID:", "Developed by:", "Mobile compatible:")
    and only state what they are about in the paragraph after it.
    """
    blocks = []
    block = []
    in_code = False

    def close_block():
        if block:
            blocks.append(list(block))
            del block[:]

    for line in body.split("\n"):
        stripped = line.strip()

        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code = not in_code
            close_block()
            continue

        if in_code:
            continue

        bullet = LIST_ITEM_RE.match(line)
        content = LIST_ITEM_RE.sub("", line).strip() if bullet else stripped

        usable = bool(content) \
            and not stripped.startswith(("#", ">", "|", ":::", "![", "[!", "<")) \
            and not DATAVIEW_FIELD_RE.match(content)    # "field:: value" is metadata

        if usable and KEY_VALUE_RE.match(content):
            block.append(("header", content))
        elif usable and not bullet:
            block.append(("prose", content))
        else:
            close_block()                               # plain list item or nothing usable

    close_block()

    header = ""
    for candidate in blocks:
        text = " ".join(entry[1] for entry in candidate)

        # A block of nothing but "Label: value" is header data. Thousands of notes in the
        # Obsidian Hub open with such a block ("Plugin ID:", "Developed by:", "Modes:") and only
        # say what they are about in the paragraph after it.
        if (len(candidate) >= 2) and all(entry[0] == "header" for entry in candidate):
            if not header:
                header = text
            continue

        return text

    # Notes without any running text - a person or theme note for instance - are described better
    # by their header data than by their whole flattened content
    return header


def markdown_to_plain(text):
    """Removes Obsidian and Markdown syntax so that no brackets remain in the description."""
    text = re.sub(r"!\[\[[^\]\[]+?\]\]", " ", text)                       # image embeds
    text = re.sub(r"!\[[^\]]*\]\([^)\s]+\)", " ", text)

    # A displayed equation says nothing in a one line description; an inline formula keeps its
    # letters but loses the dollar signs and the LaTeX commands around them
    text = MATH_BLOCK_RE.sub(" ", text)
    text = MATH_INLINE_RE.sub(
        lambda m: re.sub(r"\\[A-Za-z]+|[{}]", "", m.group(1)).strip(), text)
    text = WIKILINK_RE.sub(
        lambda m: (split_wikilink(m.group(2))[2] or split_wikilink(m.group(2))[0]), text)
    text = MD_LINK_RE.sub(lambda m: m.group(2) or "", text)
    text = CODE_BLOCK_RE.sub(" ", text)
    text = convert_obsidian_syntax(text)
    return strip_tags(markdown_to_html(text))


def plain_preview(body, title):
    """
    Plain text for the description: the first block of running text of the note, without any
    Obsidian syntax. Notes that hold nothing but lists, tables or comments fall back to their
    whole text - the check runs on the converted text, because a block may well turn into
    nothing at all (an HTML comment, for instance).
    """
    text = _body_without_title(body, title)
    # HTML comments span several lines; their closing "-->" would otherwise stand there alone
    # and pass as running text
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    paragraph = first_prose_paragraph(text)
    preview = markdown_to_plain(paragraph) if paragraph else ""

    if not preview.strip():
        preview = markdown_to_plain(text)

    return preview


def scan_vault(vault):
    """Returns all Markdown files plus a file name -> path lookup used for attachments."""
    notes = []
    assets = {}

    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, vault).replace(os.sep, "/")
            suffix = os.path.splitext(name)[1].lower()
            if suffix == ".md":
                notes.append((full, rel))
            elif suffix in IMAGE_SUFFIXES:
                assets.setdefault(name, full)
                assets.setdefault(rel, full)

    notes.sort(key=lambda entry: entry[1])
    return notes, assets


def resolve_link(target, notes_by_key, notes_by_path):
    """
    Obsidian resolves [[target]] by exact path first, then by file name. notes_by_key holds the
    file name and heading matches ahead of the alias matches, so the first candidate wins.
    Used for relations, for the itemid:// links in the notes and for ![[embeds]] alike - one
    resolution for all three, otherwise a link and its relation can end up on different items.
    """
    key = slugify_key(target)
    if key in notes_by_path:
        return notes_by_path[key]
    if key + ".md" in notes_by_path:
        return notes_by_path[key + ".md"]

    candidates = notes_by_key.get(slugify_key(os.path.basename(target)))
    if candidates:
        return candidates[0]
    return None


# ================================================================================================
# Database access
# ================================================================================================

class KbbDatabase(object):
    """Direct access to a .kdb file, mirroring the behaviour of sqlitedataaccess.php."""

    def __init__(self, path):
        self.path = path
        self.con = sqlite3.connect(path)
        self.con.text_factory = str
        self.cur = self.con.cursor()
        self._saved_triggers = []

    # -- Infrastructure --------------------------------------------------------------------------

    def close(self):
        self.con.commit()
        self.con.close()

    def commit(self):
        self.con.commit()

    def table_exists(self, name):
        self.cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
        return self.cur.fetchone() is not None

    def disable_undo_triggers(self):
        """
        The undo triggers write one row into UndoLog per change. During a bulk import that bloats
        the file considerably, and an import is not meant to be undone step by step anyway. The
        triggers are therefore removed for the duration of the import and restored verbatim
        afterwards.
        """
        self.cur.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL")
        self._saved_triggers = self.cur.fetchall()
        for name, _sql in self._saved_triggers:
            self.cur.execute('DROP TRIGGER IF EXISTS "%s"' % name)

    def enable_undo_triggers(self):
        for _name, sql in self._saved_triggers:
            self.cur.execute(sql)
        self._saved_triggers = []

    # Undo triggers of the tag tables, identical to the migration in sqlitedataaccess.php.
    # Without them a later tag change made in the application could not be undone - and since
    # this script sets dbvers to 4, the PHP migration would never run again.
    TAG_TRIGGERS = [
        ('ItemTags_Insert_Trigger',
         'CREATE TRIGGER IF NOT EXISTS ItemTags_Insert_Trigger AFTER INSERT ON ItemTags BEGIN '
         'INSERT INTO UndoLog VALUES(NULL, "DELETE FROM ItemTags WHERE rowid="||new.rowid); END;'),
        ('ItemTags_Update_Trigger',
         'CREATE TRIGGER IF NOT EXISTS ItemTags_Update_Trigger AFTER UPDATE ON ItemTags BEGIN '
         'INSERT INTO UndoLog VALUES(NULL, "UPDATE ItemTags SET ItemTagID="||old.ItemTagID||'
         '",ItemID="||old.ItemID||",ItemTemplateID="||old.ItemTemplateID||",Sequence="||old.Sequence||'
         '" WHERE rowid="||old.rowid); END;'),
        ('ItemTags_Delete_Trigger',
         'CREATE TRIGGER IF NOT EXISTS ItemTags_Delete_Trigger BEFORE DELETE ON ItemTags BEGIN '
         'INSERT INTO UndoLog VALUES(NULL, "INSERT INTO ItemTags(rowid,ItemTagID,ItemID,'
         'ItemTemplateID,Sequence) VALUES("||old.rowid||","||old.ItemTagID||","||old.ItemID||","||'
         'old.ItemTemplateID||","||old.Sequence||")"); END;'),
        ('RelationTags_Insert_Trigger',
         'CREATE TRIGGER IF NOT EXISTS RelationTags_Insert_Trigger AFTER INSERT ON RelationTags BEGIN '
         'INSERT INTO UndoLog VALUES(NULL, "DELETE FROM RelationTags WHERE rowid="||new.rowid); END;'),
        ('RelationTags_Update_Trigger',
         'CREATE TRIGGER IF NOT EXISTS RelationTags_Update_Trigger AFTER UPDATE ON RelationTags BEGIN '
         'INSERT INTO UndoLog VALUES(NULL, "UPDATE RelationTags SET RelationTagID="||old.RelationTagID||'
         '",RelationID="||old.RelationID||",RelationTemplateID="||old.RelationTemplateID||'
         '",Sequence="||old.Sequence||" WHERE rowid="||old.rowid); END;'),
        ('RelationTags_Delete_Trigger',
         'CREATE TRIGGER IF NOT EXISTS RelationTags_Delete_Trigger BEFORE DELETE ON RelationTags BEGIN '
         'INSERT INTO UndoLog VALUES(NULL, "INSERT INTO RelationTags(rowid,RelationTagID,RelationID,'
         'RelationTemplateID,Sequence) VALUES("||old.rowid||","||old.RelationTagID||","||'
         'old.RelationID||","||old.RelationTemplateID||","||old.Sequence||")"); END;'),
    ]

    def ensure_tag_tables(self):
        """Creates ItemTags/RelationTags including undo triggers if the database is still at version 3."""
        created = False
        if not self.table_exists("ItemTags"):
            created = True
            self.cur.execute("CREATE TABLE IF NOT EXISTS ItemTags(ItemTagID INTEGER PRIMARY KEY "
                             "AUTOINCREMENT, ItemID INTEGER, ItemTemplateID INTEGER, Sequence INTEGER)")
            self.cur.execute("CREATE INDEX IF NOT EXISTS ItemTags_ItemID_IDX ON ItemTags(ItemID)")
            self.cur.execute("CREATE INDEX IF NOT EXISTS ItemTags_ItemTemplateID_IDX "
                             "ON ItemTags(ItemTemplateID)")
        if not self.table_exists("RelationTags"):
            created = True
            self.cur.execute("CREATE TABLE IF NOT EXISTS RelationTags(RelationTagID INTEGER PRIMARY "
                             "KEY AUTOINCREMENT, RelationID INTEGER, RelationTemplateID INTEGER, "
                             "Sequence INTEGER)")
            self.cur.execute("CREATE INDEX IF NOT EXISTS RelationTags_RelationID_IDX "
                             "ON RelationTags(RelationID)")
            self.cur.execute("CREATE INDEX IF NOT EXISTS RelationTags_RelationTemplateID_IDX "
                             "ON RelationTags(RelationTemplateID)")

        for name, sql in self.TAG_TRIGGERS:
            self.cur.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (name,))
            if not self.cur.fetchone():
                self.cur.execute(sql)
                created = True

        if created:
            self.set_property("dbvers", "4")
        return created

    def ensure_sync_table(self):
        self.cur.execute("CREATE TABLE IF NOT EXISTS ObsidianSync("
                         "Path TEXT PRIMARY KEY, Kind TEXT, ItemID INTEGER, RecordID INTEGER, "
                         "Hash TEXT, IconRecordID INTEGER)")

    # -- Properties ------------------------------------------------------------------------------

    def get_property(self, name):
        self.cur.execute("SELECT Value FROM Properties WHERE Name=?", (name,))
        row = self.cur.fetchone()
        return row[0] if row else None

    def set_property(self, name, value):
        self.cur.execute("SELECT 1 FROM Properties WHERE Name=?", (name,))
        if self.cur.fetchone():
            self.cur.execute("UPDATE Properties SET Value=? WHERE Name=?", (str(value), name))
        else:
            self.cur.execute("INSERT INTO Properties VALUES(?, ?)", (name, str(value)))

    def get_diagram_property(self, name):
        """The formatting of the diagram lives in its own table, not in Properties."""
        self.cur.execute("SELECT Value FROM DiagramProperties WHERE Name=?", (name,))
        row = self.cur.fetchone()
        return row[0] if row else None

    def set_diagram_property(self, name, value):
        self.cur.execute("SELECT 1 FROM DiagramProperties WHERE Name=?", (name,))
        if self.cur.fetchone():
            self.cur.execute("UPDATE DiagramProperties SET Value=? WHERE Name=?", (value, name))
        else:
            self.cur.execute("INSERT INTO DiagramProperties VALUES(?, ?)", (name, value))

    def bump_diagram_update_count(self):
        current = self.get_property("duc")
        try:
            value = int(current) + 1
        except (TypeError, ValueError):
            value = 1
        self.set_property("duc", value)

    # -- Large data ------------------------------------------------------------------------------

    def new_big_blob(self, data):
        if not data:
            return -1
        self.cur.execute("INSERT INTO BigBlob VALUES(NULL, ?)", (sqlite3.Binary(data),))
        return self.cur.lastrowid

    def new_big_text(self, text):
        if not text:
            return -1
        self.cur.execute("INSERT INTO BigText VALUES(NULL, ?)", (text,))
        return self.cur.lastrowid

    # -- Templates (categories and tags share this table) ----------------------------------------

    def get_or_create_item_template(self, name):
        name = (name or "").strip()
        if not name:
            return -1
        self.cur.execute("SELECT ItemTemplateID FROM ItemTemplates WHERE TemplateName=? COLLATE NOCASE",
                         (name,))
        row = self.cur.fetchone()
        if row:
            return row[0]
        self.cur.execute("INSERT INTO ItemTemplates VALUES(NULL, ?, '', '', '', ?, '')",
                         (name, unique_id()))
        return self.cur.lastrowid

    def get_or_create_relation_template(self, name):
        name = (name or "").strip()
        if not name:
            return -1
        self.cur.execute("SELECT RelationTemplateID FROM RelationTemplates "
                         "WHERE TemplateName=? COLLATE NOCASE", (name,))
        row = self.cur.fetchone()
        if row:
            return row[0]
        self.cur.execute("INSERT INTO RelationTemplates VALUES(NULL, ?, ?, '', '', '', '', ?, '')",
                         (999999999, name, unique_id()))
        return self.cur.lastrowid

    # -- Items -----------------------------------------------------------------------------------

    def find_item_by_uri(self, uri):
        self.cur.execute("SELECT ItemID FROM Items WHERE URI=?", (uri,))
        row = self.cur.fetchone()
        return row[0] if row else -1

    def new_item(self, template_id, name, description, url, uri):
        self.cur.execute("INSERT INTO Items VALUES(NULL, ?, ?, ?, ?, ?, '')",
                         (template_id, name, description, url, uri))
        return self.cur.lastrowid

    def update_item(self, item_id, template_id, name, description, url):
        """Properties are deliberately left alone - that is where the user's formatting lives."""
        self.cur.execute("UPDATE Items SET ItemTemplateID=?, Name=?, Description=?, DirectLinkURL=? "
                         "WHERE ItemID=?", (template_id, name, description, url, item_id))

    def delete_item(self, item_id):
        if self.table_exists("ItemTags"):
            self.cur.execute("DELETE FROM ItemTags WHERE ItemID=?", (item_id,))
        self.cur.execute("SELECT RecordID FROM ItemAttachments WHERE ItemID=?", (item_id,))
        for (record_id,) in self.cur.fetchall():
            self.cur.execute("DELETE FROM Repository WHERE RecordID=?", (record_id,))
        self.cur.execute("DELETE FROM ItemAttachments WHERE ItemID=?", (item_id,))
        self.cur.execute("SELECT RelationID FROM Relations WHERE IDFrom=? OR IDTo=?",
                         (item_id, item_id))
        relation_ids = [row[0] for row in self.cur.fetchall()]
        for relation_id in relation_ids:
            if self.table_exists("RelationTags"):
                self.cur.execute("DELETE FROM RelationTags WHERE RelationID=?", (relation_id,))
            self.cur.execute("DELETE FROM RelationAttachments WHERE RelationID=?", (relation_id,))
        self.cur.execute("DELETE FROM Relations WHERE IDFrom=? OR IDTo=?", (item_id, item_id))
        self.cur.execute("DELETE FROM SearchText WHERE ItemID=?", (item_id,))
        self.cur.execute("DELETE FROM Items WHERE ItemID=?", (item_id,))

    # -- Search index ----------------------------------------------------------------------------

    def set_search_text(self, item_id, name):
        """Reimplementation of SQLiteDataAccess::setSearchText."""
        self.cur.execute("DELETE FROM SearchText WHERE ItemID=?", (item_id,))

        text = strip_leading_symbols(html_to_text(name).strip().lower()).strip()
        start_pos = 0
        original = True

        while text:
            truncated = text if original else text[:20]
            self.cur.execute("INSERT INTO SearchText VALUES(?, ?, ?)",
                             (item_id, start_pos, truncated))
            text, start_pos = get_next_search_string(text, start_pos)
            original = False

    # -- Tags ------------------------------------------------------------------------------------

    def get_item_tags(self, item_id):
        self.cur.execute("SELECT ItemTemplateID FROM ItemTags WHERE ItemID=? ORDER BY Sequence ASC",
                         (item_id,))
        return [row[0] for row in self.cur.fetchall()]

    def set_item_tags(self, item_id, tag_ids):
        """Writes only on an actual change - like setItemTags in the PHP layer."""
        if self.get_item_tags(item_id) == tag_ids:
            return False
        self.cur.execute("DELETE FROM ItemTags WHERE ItemID=?", (item_id,))
        for sequence, tag_id in enumerate(tag_ids):
            self.cur.execute("INSERT INTO ItemTags VALUES(NULL, ?, ?, ?)",
                             (item_id, tag_id, sequence))
        return True

    # -- Attachments and notes -------------------------------------------------------------------

    def new_repository_record(self, data, content, icon, data_type, mime_type,
                              name, description, properties=""):
        data_id = self.new_big_blob(data)
        content_id = self.new_big_text(content)
        small_id = self.new_big_blob(icon)
        self.cur.execute("INSERT INTO Repository VALUES(NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (data_id, content_id, small_id, -1, data_type, mime_type,
                          name, description, unique_id(), properties))
        return self.cur.lastrowid

    def update_repository_record(self, record_id, data, content, icon, name, description):
        """Creates new large data like the PHP layer does; the old rows are cleaned up by
        "shrink database"."""
        data_id = self.new_big_blob(data)
        content_id = self.new_big_text(content)
        small_id = self.new_big_blob(icon)
        self.cur.execute("UPDATE Repository SET DataID=?, ContentID=?, SmallPreviewImgID=?, "
                         "Name=?, Description=? WHERE RecordID=?",
                         (data_id, content_id, small_id, name, description, record_id))

    def attach_record(self, item_id, record_id, sequence):
        self.cur.execute("SELECT AttachmentID FROM ItemAttachments WHERE ItemID=? AND RecordID=?",
                         (item_id, record_id))
        if self.cur.fetchone():
            return
        self.cur.execute("INSERT INTO ItemAttachments VALUES(NULL, ?, ?, ?, ?, '')",
                         (item_id, record_id, sequence, unique_id()))

    # -- Relations -------------------------------------------------------------------------------

    def find_relation_by_uri(self, uri):
        self.cur.execute("SELECT RelationID FROM Relations WHERE URI=?", (uri,))
        row = self.cur.fetchone()
        return row[0] if row else -1

    def new_relation(self, id_from, id_to, template_id, name, uri):
        self.cur.execute("INSERT INTO Relations VALUES(NULL, ?, ?, ?, ?, ?, ?, ?, '', '', '', ?, '')",
                         (id_from, 0, id_to, 0, template_id, 999999999, name, uri))
        return self.cur.lastrowid

    def delete_relation(self, relation_id):
        if self.table_exists("RelationTags"):
            self.cur.execute("DELETE FROM RelationTags WHERE RelationID=?", (relation_id,))
        self.cur.execute("DELETE FROM RelationAttachments WHERE RelationID=?", (relation_id,))
        self.cur.execute("DELETE FROM Relations WHERE RelationID=?", (relation_id,))

    def update_relation_sequence(self, item_id):
        """Reimplementation of SQLiteDataAccess::updateRelationSequence (steps of 10)."""
        self.cur.execute(
            "SELECT RelationID, RelationOrder FROM ("
            "  SELECT RelationID AS RelationID, SequenceFrom AS RelationOrder FROM Relations WHERE IDFrom=?"
            "  UNION"
            "  SELECT RelationID AS RelationID, SequenceTo AS RelationOrder FROM Relations WHERE IDTo=?"
            ") ORDER BY RelationOrder ASC", (item_id, item_id))
        sequence = 10
        for relation_id, _order in self.cur.fetchall():
            self.cur.execute("UPDATE Relations SET SequenceFrom=? WHERE RelationID=? AND IDFrom=?",
                             (sequence, relation_id, item_id))
            self.cur.execute("UPDATE Relations SET SequenceTo=? WHERE RelationID=? AND IDTo=?",
                             (sequence, relation_id, item_id))
            sequence += 10


# ================================================================================================
# Images
# ================================================================================================

def load_image(path, max_edge):
    """Returns (bytes, mime). Downscales with Pillow when it is available."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None, None

    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"

    if _Image is None or path.lower().endswith(".svg"):
        return raw, mime

    try:
        from io import BytesIO
        image = _Image.open(BytesIO(raw))
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        image.thumbnail((max_edge, max_edge))
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue(), "image/png"
    except Exception:
        return raw, mime


def data_uri(data, mime):
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))


# ================================================================================================
# Mermaid diagrams
# ================================================================================================

MERMAID_BLOCK_RE = re.compile(
    r"(?ms)^([ \t]*(?:>[ \t]*)*)```[ \t]*mermaid[ \t]*$\n(.*?)^[ \t]*(?:>[ \t]*)*```[ \t]*$")
MERMAID_PLACEHOLDER = "xKBBMERMAIDx%dxKBBMERMAIDx"
MERMAID_PLACEHOLDER_RE = re.compile(r"xKBBMERMAIDx(\d+)xKBBMERMAIDx")

MERMAID_PACKAGE = "@mermaid-js/mermaid-cli"
MERMAID_MAX_BYTES = 400_000 # a rendered diagram larger than this stays a code block
MERMAID_TIMEOUT = 600       # seconds granted to a single call - the first one through npx has to
                            # fetch the package and a headless Chromium first


def find_mermaid_cli(explicit=None, allow_install=False):
    """
    Looks for mermaid-cli and returns the command that starts it, as a list of arguments, or None.

    Three steps, in this order:

        1. an installation of its own, given with --mermaid-cli
        2. mmdc as npm installed it - globally on the PATH, or locally in node_modules/.bin
        3. only with allow_install, and only when npx is there: "npx -y @mermaid-js/mermaid-cli".
           npx fetches the renderer into the npm cache the first time it is used; that costs a
           few minutes and a few hundred megabytes, because a headless Chromium comes with it.
           Nothing lands in the global npm installation, and later runs use the cache.

    Without any of those the ```mermaid blocks stay what they are - source code. Nothing is ever
    downloaded unless it was asked for, which is what the third step is gated on: an import must
    not install software behind the back of whoever started it.

    The search leaves the vault out on purpose. A vault often is a repository of somebody else,
    and an executable that happens to lie around in it is not one to start.
    """
    if explicit:
        found = os.path.abspath(explicit) if os.path.isfile(explicit) else shutil.which(explicit)
        return [found] if found else None

    found = shutil.which("mmdc")
    if found:
        return [found]

    # installed locally, next to the script or in the current directory
    for directory in (os.path.dirname(os.path.abspath(__file__)), os.getcwd()):
        for name in ("mmdc.cmd", "mmdc"):
            candidate = os.path.join(directory, "node_modules", ".bin", name)
            if os.path.isfile(candidate):
                return [candidate]

    if allow_install:
        npx = shutil.which("npx")
        if npx:
            return [npx, "-y", MERMAID_PACKAGE]

    return None


class MermaidRenderer(object):
    """
    Turns the source of a ```mermaid block into an embedded SVG image.

    Mermaid only renders inside a browser - there is no renderer for Python itself - so
    mermaid-cli drives a headless Chromium. That costs a few seconds per call, which is why every
    distinct diagram is rendered only once per run: a vault that repeats the same diagram in
    several notes pays for one call, not for one per note.
    """

    def __init__(self, command):
        self.command = command          # list of arguments, from find_mermaid_cli
        self.cache = {}
        self.rendered = 0
        self.failed = 0
        self.reported = False

    def render(self, source):
        """Returns the HTML of the diagram, or None when it could not be rendered."""
        key = hashlib.sha1(source.encode("utf-8")).hexdigest()

        if key not in self.cache:
            svg = self._run(source)
            html = self._image_html(svg) if svg else None
            if html:
                self.rendered += 1
            else:
                self.failed += 1
            self.cache[key] = html

        return self.cache[key]

    def _run(self, source):
        """Calls mermaid-cli and returns the SVG it wrote, or None."""
        directory = tempfile.mkdtemp(prefix="kbb-mermaid-")
        data = None

        try:
            source_file = os.path.join(directory, "diagram.mmd")
            svg_file = os.path.join(directory, "diagram.svg")

            with open(source_file, "w", encoding="utf-8") as handle:
                handle.write(source)

            # White background, not transparent: the text of a diagram is dark, so on a dark note
            # background a transparent one would be unreadable.
            try:
                result = subprocess.run(
                    self.command + ["-i", source_file, "-o", svg_file, "-b", "white", "-q"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=MERMAID_TIMEOUT)
            except (OSError, subprocess.SubprocessError) as error:
                self._report(str(error))
                return None

            if result.returncode != 0:
                self._report((result.stdout or b"").decode("utf-8", "replace").strip()[-300:])
                return None

            try:
                with open(svg_file, "rb") as handle:
                    data = handle.read()
            except OSError:
                return None
        finally:
            shutil.rmtree(directory, ignore_errors=True)

        return data.decode("utf-8", "replace") if data else None

    @staticmethod
    def _image_html(svg):
        """
        Embeds the SVG as a data: URI, the same way as any other image of a note.

        mermaid-cli writes width="100%" into the SVG. Inside an <img> that leaves the browser
        without an intrinsic size, so the dimensions of the viewBox take its place and the
        scaling is left to the style.
        """
        box = re.search(r'viewBox="\s*[\d.+-]+\s+[\d.+-]+\s+([\d.+-]+)\s+([\d.+-]+)', svg)
        end = svg.find(">")

        if box and end > 0:
            head = re.sub(r'\s(?:width|height)="[^"]*"', "", svg[:end])
            svg = '%s width="%d" height="%d">%s' % (head, round(float(box.group(1))),
                                                    round(float(box.group(2))), svg[end + 1:])

        data = svg.encode("utf-8")
        if len(data) > MERMAID_MAX_BYTES:
            return None

        return '<img src="%s" alt="Mermaid diagram" style="max-width:100%%;height:auto;">' \
               % data_uri(data, "image/svg+xml")

    def _report(self, message):
        """Only the first failure is reported - one broken diagram usually means many."""
        if self.reported:
            return
        self.reported = True
        print("  mermaid-cli could not render a diagram: %s" % (message or "unknown reason"))


def extract_mermaid(text, renderer):
    """
    Replaces ```mermaid blocks with a placeholder and renders them to an image. It happens before
    the Markdown conversion for the same reason as with the formulas: what comes back is finished
    HTML that the converter must not get at any more.

    Without a renderer the text stays as it is and the block is shown as source code.
    """
    diagrams = []

    if renderer is None:
        return text, diagrams

    def replace(match):
        prefix, source = match.group(1), match.group(2)

        # Inside a callout every line of the block carries the "> " markers of the quote
        if ">" in prefix:
            source = re.sub(r"(?m)^[ \t]*(?:>[ \t]?)+", "", source)

        html = renderer.render(source.strip("\n"))
        if not html:
            return match.group(0)

        diagrams.append(html)
        return prefix + MERMAID_PLACEHOLDER % (len(diagrams) - 1)

    return MERMAID_BLOCK_RE.sub(replace, text), diagrams


def insert_mermaid(html, diagrams):
    """Puts the rendered diagrams back in place of the placeholders."""
    if not diagrams:
        return html

    return MERMAID_PLACEHOLDER_RE.sub(lambda m: diagrams[int(m.group(1))], html)


# ================================================================================================
# HTML generation
# ================================================================================================

def build_note_html(note, assets, embed_images, notes_by_key=None, notes_by_path=None,
                    mermaid=None):
    """
    Converts the body of the note into HTML. Wikilinks become itemid:// references so that they
    are clickable inside the application; images are embedded as data: URIs and ![[embeds]] of
    other notes are replaced with their content.
    """
    body = _body_without_title(note.body, note.title)

    # The note itself counts as "currently being expanded", so that ![[itself]] - directly or
    # through another note - falls back to a link instead of duplicating the text.
    own = frozenset([(slugify_key(note.rel_path), "")])

    body = expand_wikilinks(body, assets, embed_images, notes_by_key or {}, notes_by_path or {},
                            0, own)

    # Für die Beschreibung merken: eine Notiz, die mit ![[...]] beginnt, hat ihren einleitenden
    # Text erst nach der Auflösung. Vorher stünde dort der Text weiter unten in der Notiz.
    note.expanded_body = body

    def replace_md_image(match):
        if match.group(1) != "!":
            return match.group(0)
        target = match.group(3).strip()
        if target.startswith(("http://", "https://")):
            return match.group(0)
        if os.path.splitext(target)[1].lower() in IMAGE_SUFFIXES:
            return _image_html(target, assets, embed_images, match.group(2))
        return match.group(0)

    body = MD_LINK_RE.sub(replace_md_image, body)

    # Diagrams and formulas are taken out before the Markdown conversion and put back afterwards,
    # so that neither Markdown nor the Obsidian rules can get at their innards
    body, diagrams = extract_mermaid(body, mermaid)
    body, formulas = extract_math(body)

    body = convert_obsidian_syntax(body)
    body = ensure_blank_line_before_blocks(body)

    html = insert_mermaid(insert_math(markdown_to_html(body), formulas), diagrams)
    return "<html>" + html + "</html>"


def expand_wikilinks(text, assets, embed_images, notes_by_key, notes_by_path, depth, active):
    """
    Replaces [[wikilinks]] and ![[embeds]] in a piece of Markdown. Runs on the text that an
    embed pulled in as well, which is why it takes the recursion depth and the set of embeds
    that are currently being expanded.
    """
    def replace(match):
        embedded = match.group(1) == "!"
        inner = match.group(2)
        target, anchor, label = split_wikilink(inner)
        if not label:
            label = target

        if embedded and os.path.splitext(target)[1].lower() in IMAGE_SUFFIXES:
            return _image_html(target, assets, embed_images, label)

        found = resolve_link(target, notes_by_key, notes_by_path) if target else None

        if embedded and found is not None:
            content = _expand_embed(found, anchor, assets, embed_images, notes_by_key,
                                    notes_by_path, depth, active)
            if content is not None:
                return content

        # itemid:// is the scheme the application uses for links that point at another item.
        # The note panel evaluates it through loadItemFromItemIdUrl in kmap.js; a click selects
        # the target item, or loads it as the new root when it is not on screen.
        if found is not None and found.item_id >= 0:
            return '[%s](itemid://%d)' % (label, found.item_id)

        # Unresolved link - keep the text, but do not fake a clickable link.
        return "<b>%s</b>" % html_escape(label)

    return WIKILINK_RE.sub(replace, text)


def _expand_embed(source, anchor, assets, embed_images, notes_by_key, notes_by_path, depth,
                  active):
    """
    Resolves ![[note]], ![[note#section]] and ![[note#^block]] to the Markdown they point at -
    Obsidian shows that content inline. Returns None when it cannot be resolved, so that the
    caller can fall back to a plain link.
    """
    if depth >= EMBED_MAX_DEPTH:
        return None

    key = (slugify_key(source.rel_path), heading_key(anchor))
    if key in active:
        return None                                     # embed contains itself

    body = _body_without_title(source.body, source.title)
    if not anchor:
        section = body
    elif anchor.startswith("^"):
        section = find_block(body, anchor[1:])
    else:
        section = find_heading_section(body, anchor)

    if not section or not section.strip():
        return None

    section = expand_wikilinks(section, assets, embed_images, notes_by_key, notes_by_path,
                              depth + 1, active | {key})

    # Inserted as a block of its own, otherwise headings and lists end up inside the surrounding
    # paragraph.
    return "\n\n" + section.strip() + "\n\n"


def _image_html(target, assets, embed_images, label):
    if not embed_images:
        return "*%s*" % (label or os.path.basename(target))

    path = assets.get(target) or assets.get(os.path.basename(target))
    if not path:
        return "*%s*" % (label or os.path.basename(target))

    data, mime = load_image(path, EMBED_MAX_EDGE)
    if not data or len(data) > EMBED_MAX_BYTES:
        return "*%s*" % (label or os.path.basename(target))

    return '<img src="%s" alt="%s">' % (data_uri(data, mime), html_escape(label or ""))


# ================================================================================================
# Import
# ================================================================================================

class Importer(object):

    def __init__(self, db, options):
        self.db = db
        self.options = options
        self.stats = dict(created=0, updated=0, unchanged=0, removed=0, reembedded=0,
                          relations_created=0, relations_removed=0, folders=0, tags=0,
                          index_pages=0, index_links_skipped=0, coloring_set=0,
                          mermaid=0, mermaid_failed=0)
        self.touched_items = set()

        # The renderer stays None when mermaid-cli is not installed; the import then runs exactly
        # as it did before and leaves the diagrams as source code.
        self.mermaid = None
        if options.mermaid:
            command = find_mermaid_cli(options.mermaid_cli, options.install_mermaid)
            if command:
                self.mermaid = MermaidRenderer(command)
            elif options.mermaid_cli:
                print("mermaid-cli not found: %s" % options.mermaid_cli)

    # ---------------------------------------------------------------------------------------

    def run(self, vault):
        notes_files, assets = scan_vault(vault)
        if not notes_files:
            print("No Markdown files found in %s." % vault)
            return

        print("Found %d Markdown files." % len(notes_files))

        if self.mermaid:
            print("Rendering mermaid diagrams with %s." % " ".join(self.mermaid.command))
            if MERMAID_PACKAGE in self.mermaid.command:
                print("  The first diagram may take a few minutes: npx fetches the renderer and "
                      "a headless Chromium into the npm cache.")

        notes = []
        for full, rel in notes_files:
            try:
                notes.append(parse_note(full, rel))
            except Exception as error:
                print("  skipped: %s (%s)" % (rel, error))

        manifest = self._load_manifest()

        folder_ids = self._create_folders(notes) if self.options.folder_nodes else {}
        self._create_items(notes, manifest)
        self._set_diagram_coloring()

        notes_by_key = {}
        notes_by_path = {}
        for note in notes:
            notes_by_path[slugify_key(note.rel_path)] = note
            notes_by_key.setdefault(slugify_key(note.title), []).append(note)
            notes_by_key.setdefault(
                slugify_key(os.path.splitext(os.path.basename(note.rel_path))[0]), []).append(note)

        # Aliases only afterwards, so that a file name or heading match always comes first
        for note in notes:
            for alias in note.aliases:
                notes_by_key.setdefault(slugify_key(alias), []).append(note)

        self._mark_embedding_notes_changed(notes, notes_by_key, notes_by_path)
        self._write_notes(notes, manifest, assets, notes_by_key, notes_by_path)
        self._write_tags(notes)
        self._write_relations(notes, notes_by_key, notes_by_path, folder_ids)

        if self.options.prune:
            self._prune(notes, manifest)

        if self.mermaid:
            self.stats["mermaid"] = self.mermaid.rendered
            self.stats["mermaid_failed"] = self.mermaid.failed

        self._update_sequences()
        self.db.bump_diagram_update_count()
        self._save_manifest(notes)

    # ---------------------------------------------------------------------------------------

    def _load_manifest(self):
        self.db.ensure_sync_table()
        self.db.cur.execute("SELECT Path, Kind, ItemID, RecordID, Hash, IconRecordID FROM ObsidianSync")
        manifest = {}
        for path, kind, item_id, record_id, content_hash, icon_id in self.db.cur.fetchall():
            manifest[path] = dict(kind=kind, item_id=item_id, record_id=record_id,
                                  hash=content_hash, icon_id=icon_id)
        return manifest

    def _save_manifest(self, notes):
        for note in notes:
            entry = self._manifest_entry(note)
            self.db.cur.execute(
                "INSERT OR REPLACE INTO ObsidianSync(Path, Kind, ItemID, RecordID, Hash, IconRecordID) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (note.rel_path, "note", note.item_id, entry.get("record_id", -1),
                 note.content_hash, entry.get("icon_id", -1)))

    def _manifest_entry(self, note):
        return getattr(note, "_manifest", {})

    # ---------------------------------------------------------------------------------------

    def _set_diagram_coloring(self):
        """
        Stellt die Färbung des Diagramms auf "kategorieweise". Die Anwendung wählt die Farben
        dann selbst anhand der Kategorie - es wird keine Farbe je Kategorie hinterlegt, damit
        eine von Hand gesetzte Formatierung nicht überschrieben wird.
        """
        if not self.options.format_diagram:
            return

        try:
            properties = json.loads(self.db.get_diagram_property("dp") or "{}")
        except ValueError:
            properties = {}

        changed = False
        for name, value in DIAGRAM_PROPERTIES.items():
            if properties.get(name) != value:
                properties[name] = value
                changed = True

        if not changed:
            return

        self.db.set_diagram_property("dp", json.dumps(properties, separators=(",", ":")))
        self.stats["coloring_set"] = 1

    def _create_folders(self, notes):
        """Creates one item per folder so that unlinked notes are still attached to the diagram."""
        folders = set()
        for note in notes:
            parts = note.folder.split("/") if note.folder else []
            for index in range(len(parts)):
                folders.add("/".join(parts[:index + 1]))

        template_id = self.db.get_or_create_item_template(self.options.folder_category)
        folder_ids = {}

        for folder in sorted(folders):
            uri = "obsidian:folder:" + folder
            item_id = self.db.find_item_by_uri(uri)
            # Auch beim Ordner gilt: ein vorhandenes Symbol wird uebernommen
            icon, plain = split_leading_icon(os.path.basename(folder))
            name = icon_label(plain, icon or self.options.folder_icon)
            if item_id < 0:
                item_id = self.db.new_item(template_id, name, "", "", uri)
                self.db.set_search_text(item_id, name)
                self.stats["folders"] += 1
            folder_ids[folder] = item_id
            self.touched_items.add(item_id)

        # Root item so that the diagram has a common starting point
        root_uri = "obsidian:folder:"
        root_id = self.db.find_item_by_uri(root_uri)
        if root_id < 0:
            root_name = icon_label(self.options.root_name, self.options.folder_icon)
            root_id = self.db.new_item(template_id, root_name, "", "", root_uri)
            self.db.set_search_text(root_id, root_name)
            self.stats["folders"] += 1
        folder_ids[""] = root_id
        self.touched_items.add(root_id)

        return folder_ids

    def _category_for(self, note):
        if self.options.category == "none":
            return -1
        if self.options.category == "fixed":
            return self.db.get_or_create_item_template(self.options.category_name)
        name = os.path.basename(note.folder) if note.folder else self.options.category_name
        return self.db.get_or_create_item_template(name)

    def _item_name(self, note):
        """Name des Elements, mit vorangestelltem Symbol, falls der Name eines enthaelt."""
        if not self.options.note_icons:
            return note.title

        # Nur ein Symbol, das ohnehin im Namen steht, wandert nach vorne in die eigene Zelle.
        # Notizen ohne Symbol bekommen keines - ein aus dem Text erratenes Symbol trifft zu oft
        # daneben, um zu helfen.
        icon, title = split_leading_icon(note.title)
        if not icon:
            return note.title

        return icon_label(title, icon)

    def _create_items(self, notes, manifest):
        for note in notes:
            uri = "obsidian:note:" + note.rel_path
            template_id = self._category_for(note)
            item_id = self.db.find_item_by_uri(uri)
            entry = manifest.get(note.rel_path)

            if item_id < 0:
                item_id = self.db.new_item(template_id, self._item_name(note), note.description,
                                           note.url, uri)
                self.db.set_search_text(item_id, self._item_name(note))
                self.stats["created"] += 1
                note._changed = True
            else:
                unchanged = bool(entry) and entry.get("hash") == note.content_hash
                if unchanged and not self.options.force:
                    self.stats["unchanged"] += 1
                    note._changed = False
                else:
                    self.db.update_item(item_id, template_id, self._item_name(note), note.description,
                                        note.url)
                    self.db.set_search_text(item_id, self._item_name(note))
                    self.stats["updated"] += 1
                    note._changed = True

            note.item_id = item_id
            note._manifest = dict(entry or {})
            self.touched_items.add(item_id)

    # ---------------------------------------------------------------------------------------

    def _mark_embedding_notes_changed(self, notes, notes_by_key, notes_by_path):
        """
        A note that embeds another one carries that note's content in its HTML, so it has to be
        rewritten when the embedded note changes - even if the embedding file itself is untouched.
        Repeated until stable, because embeds can be nested.
        """
        for _ in range(EMBED_MAX_DEPTH):
            spread = False
            for note in notes:
                if note._changed:
                    continue
                for target, embedded, alias in note.links:
                    if not embedded:
                        continue
                    found = resolve_link(target, notes_by_key, notes_by_path)
                    if found is not None and found._changed:
                        note._changed = True
                        spread = True
                        self.stats["unchanged"] -= 1
                        self.stats["reembedded"] += 1
                        break

            if not spread:
                return

    def _write_notes(self, notes, manifest, assets, notes_by_key, notes_by_path):
        for note in notes:
            if not note._changed:
                continue

            html = build_note_html(note, assets, self.options.images, notes_by_key,
                                   notes_by_path, self.mermaid)
            text = strip_tags(html)
            name = note.title[:100]

            # Die Beschreibung entsteht erst hier neu, weil erst jetzt die Einbettungen aufgelöst
            # sind. Das Element wird nur angefasst, wenn sie sich dadurch tatsächlich ändert.
            description = note.description
            if note.description_from_text:
                description = build_description(
                    plain_preview(note.expanded_body, note.title)) or note.description

                if description != note.description:
                    note.description = description
                    self.db.update_item(note.item_id, self._category_for(note), name, description,
                                        note.url)

            entry = manifest.get(note.rel_path) or {}
            record_id = entry.get("record_id", -1) or -1

            if record_id > 0:
                self.db.update_repository_record(record_id, html.encode("utf-8"), text, None,
                                                 name, description)
            else:
                record_id = self.db.new_repository_record(
                    html.encode("utf-8"), text, None, "html", "text/html", name, description)
                self.db.attach_record(note.item_id, record_id, 0)

            note._manifest["record_id"] = record_id

            if self.options.images:
                self._write_icon(note, assets, entry)

    def _write_icon(self, note, assets, entry):
        """Attaches the first embedded image as the item icon."""
        if not note.embedded_files:
            return

        path = None
        for candidate in note.embedded_files:
            path = assets.get(candidate) or assets.get(os.path.basename(candidate))
            if path:
                break
        if not path:
            return

        icon, mime = load_image(path, ICON_MAX_EDGE)
        if not icon:
            return

        icon_id = entry.get("icon_id", -1) or -1
        name = os.path.basename(path)

        if icon_id > 0:
            self.db.update_repository_record(icon_id, icon, "", icon, name, "")
        else:
            icon_id = self.db.new_repository_record(icon, "", icon, "image", mime, name, "")
            self.db.attach_record(note.item_id, icon_id, 1)

        note._manifest["icon_id"] = icon_id

    # ---------------------------------------------------------------------------------------

    def _write_tags(self, notes):
        for note in notes:
            if not note._changed:
                continue
            tag_ids = []
            for tag in note.tags:
                tag_id = self.db.get_or_create_item_template(tag)
                if tag_id >= 0 and tag_id not in tag_ids:
                    tag_ids.append(tag_id)
            if self.db.set_item_tags(note.item_id, tag_ids):
                self.stats["tags"] += len(tag_ids)

    # ---------------------------------------------------------------------------------------

    @staticmethod
    def _words_outside_links(note):
        """Counts the words of the note that are not part of a wikilink."""
        text = CODE_BLOCK_RE.sub(" ", note.body)
        text = re.sub(r"(?is)<[^>]+>", " ", text)
        text = WIKILINK_RE.sub(" ", text)
        return len(re.findall(r"\S+", text))

    def _find_index_pages(self, notes, notes_by_key, notes_by_path):
        """
        Finds the notes whose links say nothing: an index or a table of contents points at
        everything and therefore distinguishes nothing. In the diagram such a note pulls a
        bundle of edges onto itself and makes the layout unreadable.

        Two conditions have to hold together, because either one alone would catch the wrong
        notes. A long article may well link a dozen times without being an index, and a short
        stub may consist of two links without being one either:

          many links   - measured against this vault, not against a fixed number, so that a
                         densely linked Zettelkasten is judged by its own standard
          little text  - an index is made of links, an article carries its links inside prose

        In the Digital Garden the two are a factor of twelve apart: the home page has 7 words
        per link, the most densely linked article 86.
        """
        outgoing = {}

        for note in notes:
            targets = set()
            for target, embedded, alias in note.links:
                if embedded:
                    continue                            # an embed carries its content along
                found = resolve_link(target, notes_by_key, notes_by_path)
                if (found is not None) and (found is not note):
                    targets.add(found.rel_path)
            if targets:
                outgoing[note.rel_path] = (note, len(targets))

        if not outgoing:
            return set()

        # Der Median läuft über ALLE Notizen, nicht nur über die verlinkenden. Sonst besteht die
        # Stichprobe in einem kleinen Vault gerade aus den Knotenpunkten, und die Schwelle steigt
        # genau dort, wo sie greifen müsste.
        counts = sorted(len(set(t for t, e, a in note.links if not e)) for note in notes)
        median = counts[len(counts) // 2] if counts else 0
        threshold = max(self.options.index_page_links, 4 * median)

        index_pages = set()
        for rel_path, (note, count) in outgoing.items():
            if count < threshold:
                continue
            if (self._words_outside_links(note) / float(count)) <= self.options.index_page_words:
                index_pages.add(rel_path)
                self.stats["index_pages"] += 1
                self.stats["index_links_skipped"] += count

        return index_pages

    def _write_relations(self, notes, notes_by_key, notes_by_path, folder_ids):
        """
        Relations are reconciled through stable URIs: missing ones are created, superfluous ones
        removed, existing ones left untouched. That way formatting applied by hand survives the
        next import.
        """
        wanted = {}

        link_template = self.db.get_or_create_relation_template(self.options.link_relation)
        embed_template = self.db.get_or_create_relation_template(self.options.embed_relation)
        folder_template = self.db.get_or_create_relation_template(self.options.folder_relation)

        if folder_ids:
            for folder, item_id in folder_ids.items():
                if folder == "":
                    continue
                parent = os.path.dirname(folder)
                parent_id = folder_ids.get(parent, folder_ids.get(""))
                if parent_id and parent_id != item_id:
                    uri = "obsidian:rel:%s|%s|folder" % (parent, folder)
                    wanted[uri] = (parent_id, item_id, folder_template, self.options.folder_relation)

            for note in notes:
                parent_id = folder_ids.get(note.folder, folder_ids.get(""))
                if parent_id and parent_id != note.item_id:
                    uri = "obsidian:rel:%s|%s|folder" % (note.folder, note.rel_path)
                    wanted[uri] = (parent_id, note.item_id, folder_template,
                                   self.options.folder_relation)

        # Notizen, deren Links nichts aussagen, weil sie auf alles zeigen
        index_pages = set()
        if not self.options.keep_index_page_links:
            index_pages = self._find_index_pages(notes, notes_by_key, notes_by_path)

        for note in notes:
            # Typed links from Dataview fields first, so that they win over a plain wikilink
            # to the same target
            if self.options.dataview_relations:
                for field, target in note.typed_links:
                    found = resolve_link(target, notes_by_key, notes_by_path)
                    if not found or found.item_id == note.item_id:
                        continue
                    uri = "obsidian:rel:%s|%s|field:%s" % (note.rel_path, found.rel_path, field)
                    if uri in wanted:
                        continue
                    wanted[uri] = (note.item_id, found.item_id,
                                   self.db.get_or_create_relation_template(field), field)
            plain_links = note.links
            if not self.options.dataview_relations:
                plain_links = plain_links + [(target, False, "") for field, target
                                             in note.typed_links]

            for target, embedded, alias in plain_links:
                # Von einer Indexseite bleiben die Einbettungen, die gewöhnlichen Links nicht.
                # Im Notiztext sind sie weiterhin anklickbar - nur das Diagramm bleibt lesbar.
                if (not embedded) and (note.rel_path in index_pages):
                    continue

                found = resolve_link(target, notes_by_key, notes_by_path)
                if not found or found.item_id == note.item_id:
                    continue

                kind = "embed" if embedded else "link"
                template = embed_template if embedded else link_template
                label = self.options.embed_relation if embedded else self.options.link_relation

                # Der Anzeigetext hinter dem | ist in Obsidian normalerweise ein Anzeigename und
                # keine Beziehung. Deshalb nur auf Wunsch - und auch dann nicht, wenn er selbst
                # einen vorhandenen Notiznamen auflöst, denn dann ist er sicher ein Anzeigename.
                if self.options.pipe_as_relation_label and alias and not embedded \
                        and not resolve_link(alias, notes_by_key, notes_by_path):
                    label = alias
                    template = self.db.get_or_create_relation_template(alias)
                    kind = "pipe:%s" % alias

                uri = "obsidian:rel:%s|%s|%s" % (note.rel_path, found.rel_path, kind)
                if uri in wanted:
                    continue

                wanted[uri] = (note.item_id, found.item_id, template, label)

        self.db.cur.execute("SELECT RelationID, URI FROM Relations WHERE URI LIKE 'obsidian:rel:%'")
        existing = {uri: relation_id for relation_id, uri in self.db.cur.fetchall()}

        for uri, (id_from, id_to, template_id, label) in wanted.items():
            if uri in existing:
                continue
            self.db.new_relation(id_from, id_to, template_id, label, uri)
            self.stats["relations_created"] += 1
            self.touched_items.add(id_from)
            self.touched_items.add(id_to)

        for uri, relation_id in existing.items():
            if uri not in wanted:
                self.db.cur.execute("SELECT IDFrom, IDTo FROM Relations WHERE RelationID=?",
                                    (relation_id,))
                row = self.db.cur.fetchone()
                if row:
                    self.touched_items.add(row[0])
                    self.touched_items.add(row[1])
                self.db.delete_relation(relation_id)
                self.stats["relations_removed"] += 1

    # ---------------------------------------------------------------------------------------

    def _prune(self, notes, manifest):
        current = {note.rel_path for note in notes}
        for path, entry in manifest.items():
            if path in current:
                continue
            item_id = entry.get("item_id", -1)
            if item_id and item_id > 0:
                self.db.delete_item(item_id)
                self.stats["removed"] += 1
            self.db.cur.execute("DELETE FROM ObsidianSync WHERE Path=?", (path,))

    def _update_sequences(self):
        for item_id in sorted(self.touched_items):
            self.db.update_relation_sequence(item_id)


# ================================================================================================
# Command line
# ================================================================================================

def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Imports an Obsidian vault into a KnowledgeBase Builder database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python obsidian_to_kbb.py --vault ~/Obsidian/MyVault --db myvault.kdb\n"
               "  python obsidian_to_kbb.py --vault ~/Obsidian/MyVault --db myvault.kdb --prune\n")

    parser.add_argument("--vault", required=True, help="directory of the Obsidian vault")
    parser.add_argument("--db", required=True, help="target database (.kdb)")
    parser.add_argument("--template", default="empty.kdb",
                        help="empty template database, copied when --db does not exist yet")

    parser.add_argument("--category", choices=["folder", "fixed", "none"], default="folder",
                        help="category of the items: from the folder name, fixed, or none")
    parser.add_argument("--category-name", default="Note",
                        help="category name for --category fixed and for notes in the root folder")

    parser.add_argument("--folder-nodes", dest="folder_nodes", action="store_true", default=True,
                        help="create an item per folder (default)")
    parser.add_argument("--no-folder-nodes", dest="folder_nodes", action="store_false",
                        help="do not create folder items")
    parser.add_argument("--folder-category", default="Folder", help="category of the folder items")
    parser.add_argument("--root-name", default="Vault", help="name of the topmost folder item")

    parser.add_argument("--images", dest="images", action="store_true", default=True,
                        help="embed images and attach the first one as the item icon (default)")
    parser.add_argument("--no-images", dest="images", action="store_false",
                        help="leave images out")

    parser.add_argument("--mermaid", dest="mermaid", action="store_true", default=True,
                        help="render ```mermaid blocks to SVG, provided mermaid-cli (mmdc) is "
                             "already installed. Without it the block stays source code and "
                             "nothing is downloaded (default)")
    parser.add_argument("--no-mermaid", dest="mermaid", action="store_false",
                        help="leave mermaid blocks as source code")
    parser.add_argument("--install-mermaid", dest="install_mermaid", action="store_true",
                        default=False,
                        help="when mermaid-cli is not installed, fetch it temporarily through "
                             "npx - needs Node.js and, the first time, a few minutes and a few "
                             "hundred megabytes for the headless Chromium. It goes into the npm "
                             "cache, not into the global installation")
    parser.add_argument("--mermaid-cli", dest="mermaid_cli", default=None,
                        help="path to mmdc, for an installation that is not on the PATH")

    parser.add_argument("--link-relation", default="links to", help="label for [[wikilinks]]")
    parser.add_argument("--embed-relation", default="embeds", help="label for ![[embeds]]")
    parser.add_argument("--folder-relation", default="contains",
                        help="label for the folder structure")
    parser.add_argument("--dataview-relations", dest="dataview_relations", action="store_true",
                        default=True,
                        help="use \"field:: [[target]]\" as the label of the relation (default)")
    parser.add_argument("--no-dataview-relations", dest="dataview_relations",
                        action="store_false", help="treat such links like any other wikilink")
    # Kein Emoji im Hilfetext: die Windows-Konsole liegt auf cp1252 und bricht daran ab
    parser.add_argument("--folder-icon", default=FOLDER_ICON + " ",
                        help="put in front of the name of a folder item, empty string for none "
                             "(default: a folder symbol)")
    parser.add_argument("--format-diagram", dest="format_diagram", action="store_true",
                        default=True,
                        help="set colouring, colour scheme, background and transparency of the "
                             "diagram (default)")
    parser.add_argument("--no-format-diagram", dest="format_diagram", action="store_false",
                        help="leave the formatting of the diagram as it is")
    parser.add_argument("--note-icons", dest="note_icons", action="store_true", default=True,
                        help="move a symbol that is already part of a note name to the front "
                             "(default)")
    parser.add_argument("--no-note-icons", dest="note_icons", action="store_false",
                        help="leave the names of the notes as they are")
    parser.add_argument("--index-page-links", type=int, default=10,
                        help="from this many outgoing links on, a note may count as an index "
                             "page; the actual threshold is at least four times the median of "
                             "the vault (default 10)")
    parser.add_argument("--index-page-words", type=int, default=20,
                        help="a note only counts as an index page if it holds at most this many "
                             "words per link (default 20)")
    parser.add_argument("--keep-index-page-links", dest="keep_index_page_links",
                        action="store_true", default=False,
                        help="also turn the links of index pages into relations")
    parser.add_argument("--pipe-as-relation-label", dest="pipe_as_relation_label",
                        action="store_true", default=False,
                        help="use the text behind the | of [[target|text]] as the label of the "
                             "relation. Off by default: in Obsidian that text usually is a "
                             "display name and not a relation")

    parser.add_argument("--prune", action="store_true",
                        help="remove items whose Markdown file no longer exists")
    parser.add_argument("--force", action="store_true",
                        help="rewrite every note, including unchanged ones")
    parser.add_argument("--keep-undo", action="store_true",
                        help="leave the undo triggers active during the import "
                             "(slower, larger file)")

    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv if argv is not None else sys.argv[1:])

    vault = os.path.abspath(os.path.expanduser(options.vault))
    if not os.path.isdir(vault):
        print("Vault directory not found: %s" % vault)
        return 2

    target = os.path.abspath(os.path.expanduser(options.db))
    if not os.path.exists(target):
        template = os.path.abspath(os.path.expanduser(options.template))
        if not os.path.exists(template):
            print("Template database not found: %s" % template)
            return 2
        shutil.copyfile(template, target)
        print("Created a new database from %s." % os.path.basename(template))

    db = KbbDatabase(target)

    try:
        if db.ensure_tag_tables():
            print("Tag tables created (database version 4).")

        if not options.keep_undo:
            db.disable_undo_triggers()

        importer = Importer(db, options)
        importer.run(vault)

        if not options.keep_undo:
            db.enable_undo_triggers()
            db.cur.execute("DELETE FROM UndoLog")

        db.commit()
    finally:
        db.close()

    stats = importer.stats
    print("")
    print("Done: %s" % target)
    print("  notes created ......... %d" % stats["created"])
    print("  notes updated ......... %d" % stats["updated"])
    print("  notes unchanged ....... %d" % stats["unchanged"])
    if stats["reembedded"]:
        print("  notes re-embedded ..... %d" % stats["reembedded"])
    if options.prune:
        print("  notes removed ......... %d" % stats["removed"])
    print("  folder items created .. %d" % stats["folders"])
    if stats["coloring_set"]:
        print("  diagram formatted ..... colouring, colour scheme, background")
    if stats["mermaid"] or stats["mermaid_failed"]:
        print("  mermaid diagrams ...... %d rendered, %d left as source code"
              % (stats["mermaid"], stats["mermaid_failed"]))
    if stats["index_pages"]:
        print("  index pages ........... %d (%d links left out)"
              % (stats["index_pages"], stats["index_links_skipped"]))
    print("  relations created ..... %d" % stats["relations_created"])
    print("  relations removed ..... %d" % stats["relations_removed"])
    print("  tag assignments ....... %d" % stats["tags"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
