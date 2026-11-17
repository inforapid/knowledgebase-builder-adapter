# -*- coding: utf-8 -*-
"""
Import Anki decks and Quizlet sets directly into an InfoRapid KnowledgeBase Builder database (.kdb),
and export a .kdb back to Anki as a text file.

Import (writes into the SQLite file directly, no intermediate format):
    python anki_to_kbb.py import DECK.apkg   --db deck.kdb
    python anki_to_kbb.py import export.txt  --db set.kdb                  (Anki or Quizlet text)
    python anki_to_kbb.py import quizlet.txt --db set.kdb --separator "," --row-separator ";"

    If the target does not exist yet it is created from the template (--template, by default
    empty.kdb next to this script).

Export (from a .kdb or from a JSON file written by "Export JSON data" / kbb_export_database):
    python anki_to_kbb.py export deck.kdb --out anki.txt [--deck "My deck"]

See README.md for the mapping and all options.

The database layer (KbbDatabase) is shared with the Obsidian adapter: obsidian_to_kbb.py is looked
up next to this script first and then in ../obsidian/.
"""
import argparse
import base64
import csv
import html
import io
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (HERE, os.path.join(os.path.dirname(HERE), "obsidian")):
    if os.path.exists(os.path.join(_candidate, "obsidian_to_kbb.py")):
        sys.path.insert(0, _candidate)
        break
try:
    from obsidian_to_kbb import KbbDatabase, unique_id  # noqa: E402
except ImportError:
    raise SystemExit("obsidian_to_kbb.py not found - it provides the database layer. Keep the anki/ and "
                     "obsidian/ directories side by side or copy obsidian_to_kbb.py next to this script.")

try:
    from PIL import Image as _Image
except ImportError:
    _Image = None

import music_notation  # noqa: E402  (next to this script)

# Questions of melody notes (field "MelodyJSON"): the staff is the question, the syllables or the count
# the answer. A question mark makes the learning mode ask them front -> back as a flashcard.
MUSIC_TEXTS = {
    "en": {"sing": "Which syllables does this melody sing?", "rhythm": "How do you count this rhythm?",
           "key": "Key", "time": "Time", "clef": "Clef", "first": "First note", "tonic": "Tonic",
           "notes": "Notes", "count": "Count", "degrees": "Scale degrees",
           "legend": "bold = clap, grey = hold, ( ) = rest", "audio": "The recordings of the Anki deck (cadence, first note, "
           "melody, drone) are not imported."},
    "de": {"sing": "Welche Silben singt diese Melodie?", "rhythm": "Wie zählst du diesen Rhythmus?",
           "key": "Tonart", "time": "Takt", "clef": "Schlüssel", "first": "Anfangston", "tonic": "Grundton",
           "notes": "Noten", "count": "Zählweise", "degrees": "Stufen",
           "legend": "fett = klatschen, grau = aushalten, ( ) = Pause", "audio": "Die Tonaufnahmen des Anki-Stapels (Kadenz, "
           "Anfangston, Melodie, Bordun) werden nicht übernommen."},
    "es": {"sing": "¿Qué sílabas canta esta melodía?", "rhythm": "¿Cómo cuentas este ritmo?",
           "key": "Tonalidad", "time": "Compás", "clef": "Clave", "first": "Primera nota", "tonic": "Tónica",
           "notes": "Notas", "count": "Conteo", "degrees": "Grados",
           "legend": "negrita = palmada, gris = mantener, ( ) = silencio", "audio": "Las grabaciones del mazo de Anki (cadencia, primera "
           "nota, melodía, bordón) no se importan."},
}

FIELD_SEPARATOR = "\x1f"
HIGHLIGHT = "background-color:#fef08a"
DAY_MS = 86400000
DEFAULT_USER = "_default_user_"         # user of the web application and the apps (sqlitedataaccess.php)
SKIP_FIELDS = {"deck id", "image size", "occlusion", "verdeckung", "note id", "date stamp", "source"}   # technical fields of common note types
MAX_GAPS = 9                            # the application evaluates at most nine highlights per item

# ------------------------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------------------------

def strip_html(text):
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", text or "")
    text = re.sub(r"(?i)<br\s*/?>", " ", text)
    text = re.sub(r"(?i)</(div|p|li|tr)>", " ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


# Inline style properties copied along from web pages or editors: fonts, sizes and the text colour of the
# author's Anki theme. White or black text was meant for one theme - on the light answer card of the
# KnowledgeBase Builder white text is invisible. Other colours are kept, they usually mark something.
DROPPED_STYLE = re.compile(r"(?i)^\s*(font-family|font-size|line-height|caret-color|-webkit-[a-z-]+|"
                           r"text-wrap[a-z-]*|white-space[a-z-]*|word-spacing|letter-spacing|orphans|widows|"
                           r"text-indent|text-transform|font-variant[a-z-]*)\s*:")
NEUTRAL_COLOR = re.compile(r"(?i)^\s*color\s*:\s*(#fff(fff)?|#000(000)?|white|black|"
                           r"rgba?\(\s*(255\s*,\s*255\s*,\s*255|0\s*,\s*0\s*,\s*0)\s*(,\s*[\d.]+\s*)?\))\s*$")


def clean_style(match):
    kept = [part for part in match.group(2).split(";")
            if part.strip() and not DROPPED_STYLE.match(part) and not NEUTRAL_COLOR.match(part)]
    return ' style=%s%s%s' % (match.group(1), ";".join(p.strip() for p in kept), match.group(1)) if kept else ""


def repair_cloze(text):
    """Cloze deletions with a single colon, as typed by hand: {{c1:answer}}, {{c1:answer:}} and
    {{c1:<b>:answer}}. Anki shows them as plain text, but they are meant as clozes."""
    text = re.sub(r"\{\{c(\d+):((?:<[^>]+>)*):(?!:)", r"{{c\1::\2", text)
    text = re.sub(r"\{\{c(\d+):(?!:)(.*?):?\}\}", r"{{c\1::\2}}", text, flags=re.S)
    # a single closing brace, maybe with the second one behind a tag: {{c1::answer}</b> or {{c1::answer}</b>}
    return re.sub(r"(\{\{c\d+::[^{}]*?)\}(?!\})((?:\s*<[^>]+>)*)\}?", r"\1}}\2", text)


def sanitize_html(text):
    """Removes scripts, styles, event attributes and sound references from Anki HTML, the styles of
    other web pages (fonts, sizes, theme colours, <code> blocks pasted from a search page) and
    repairs hand-typed clozes."""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", text or "")
    text = re.sub(r"(?i)\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", text)
    text = re.sub(r"\[sound:[^\]]*\]", "", text)
    text = re.sub(r'(?i)\s+style=(["\'])(.*?)\1', clean_style, text)
    text = re.sub(r"(?i)\s+(jsaction|jscontroller|jsuid|jsname|data-sfc-[a-z-]+|data-ved|class)=(\"[^\"]*\"|'[^']*')", "", text)
    text = re.sub(r"(?i)<code(\s+dir=\"[^\"]*\")?>(.*?)</code>", r"<span>\2</span>", text, flags=re.S) \
        if re.search(r"(?i)<code\s+dir=", text) else text
    text = repair_cloze(text)
    return text.strip()


def balance_tags(content):
    """Splits HTML into (balanced content, leftover tags): a cloze can start inside one <span> and end
    in the next - its highlight would then close the wrong element."""
    stack = []
    leftover = []
    keep = []
    for match in re.finditer(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*?(/?)>|[^<]+|<", content):
        token = match.group(0)
        if match.group(2) and not match.group(3) and match.group(2).lower() not in ("br", "img", "hr", "wbr"):
            name = match.group(2).lower()
            if match.group(1):
                if stack and stack[-1][0] == name:
                    stack.pop()
                    keep.append(token)
                else:
                    leftover.append(token)
                continue
            stack.append((name, len(keep)))
            keep.append(token)
            continue
        keep.append(token)
    for name, index in reversed(stack):
        leftover.insert(0, keep[index])
        keep[index] = ""
    return "".join(keep), "".join(leftover)


def has_content(text):
    return bool(strip_html(text)) or "<img" in (text or "").lower()


def shorten(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    cut = cut if len(cut) > limit * 0.6 else text[:limit]
    # never cut inside a formula - jqMath would render the rest as a broken formula
    for opening, closing in (("\\(", "\\)"), ("\\[", "\\]")):
        if cut.count(opening) > cut.count(closing):
            cut = cut[:cut.rfind(opening)]
    return cut.rstrip(" ,;:") + "\u2026"


# Cloze deletion: {{c1::answer}} or {{c1::answer::hint}}
CLOZE = re.compile(r"\{\{c(\d+)::(.*?)(?:::(.*?))?\}\}", re.S)


def cloze_to_highlight(text):
    """Turns cloze deletions into highlights. data-cloze keeps the Anki number, so the learning mode
    hides all parts of c1 together (one card per number, as in Anki); data-hint keeps the hint.
    Also returns the cloze numbers in order of their first appearance - the order of the gap cards."""
    numbers = []

    def replace(match):
        number = int(match.group(1))
        if number not in numbers:
            numbers.append(number)
        hint = ' data-hint="%s"' % html.escape(strip_html(match.group(3)), quote=True) if match.group(3) else ""
        content, leftover = balance_tags(match.group(2))
        return '<span data-cloze="%d"%s style="%s">%s</span>%s' % (number, hint, HIGHLIGHT, content, leftover)
    return CLOZE.sub(replace, text), numbers


def cloze_reveal(text):
    return CLOZE.sub(lambda m: m.group(2), text)


def is_cloze(fields):
    return any(CLOZE.search(f or "") for f in fields)


def tag_name(tag, deck_tag_prefixes):
    """Anki tag -> KBB tag (--all-tags). None when the tag only repeats the deck path."""
    if re.sub(r"[\s_]+", "", tag.split("::", 1)[0]).lower() in deck_tag_prefixes:
        return None
    if "::" in tag:
        tag = tag.rsplit("::", 1)[1]
    return clean_tag(tag)


def clean_tag(tag):
    # A leading # marks special tags in KBB (#nolearn, #hidden, #board) - drop such markers
    return re.sub(r"^[#$!§%&*+~]+", "", tag).replace("_", " ").strip() or None


def build_tag_map(notes_tags, deck_tag_prefixes, min_notes):
    """Anki tag -> KBB tag name or None. Large shared decks carry thousands of nested tags (curricula of
    single universities, study plans) next to a few flags ("!Physikum", "!High-Yield"). Kept are the flags:
    tags that are a single level below a prefix shared by most tags (e.g. "#Deck_v6.0::"), used by at least
    min_notes notes. Tags that repeat the deck path are dropped."""
    counts = {}
    for tags in notes_tags:
        for tag in set(tags):
            counts[tag] = counts.get(tag, 0) + 1

    nested = [t for t in counts if "::" in t]
    first = {}
    for tag in nested:
        head = tag.split("::", 1)[0]
        first[head] = first.get(head, 0) + 1
    prefix = None
    if nested:
        head, number = max(first.items(), key=lambda item: item[1])
        if number >= 0.8 * len(nested) and number >= 0.5 * len(counts):
            prefix = head + "::"

    result = {}
    for tag, number in counts.items():
        rest = tag[len(prefix):] if prefix and tag.startswith(prefix) else tag
        if re.sub(r"[\s_]+", "", tag.split("::", 1)[0]).lower() in deck_tag_prefixes:
            result[tag] = None
        elif re.sub(r"[\s_]+", "", rest.split("::", 1)[0]).lower() in deck_tag_prefixes:
            result[tag] = None
        elif "::" in rest or number < min_notes:
            result[tag] = None
        else:
            result[tag] = clean_tag(rest)
    return result


# ------------------------------------------------------------------------------------------------
# Formulas: LaTeX (MathJax in Anki) -> the jqMath notation of the application
# ------------------------------------------------------------------------------------------------
# The application renders formulas between \( \) and \[ \] with jqMath. Its syntax is not LaTeX: it
# has no command names such as \vec, \frac or \times, but uses Unicode characters, {a}/{b} for
# fractions, √{x} for roots, (\table a,b;c,d) for matrices and \text"..." for text. ^ and _ work as
# in LaTeX.

LATEX_SYMBOLS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ",
    "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
    "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "ϕ", "varphi": "φ",
    "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "partial": "∂", "nabla": "∇", "times": "×", "cdot": "⋅", "div": "÷", "pm": "±", "mp": "∓", "circ": "∘",
    "ldots": "…", "dots": "…", "cdots": "⋯", "vdots": "⋮", "ddots": "⋱", "infty": "∞",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈", "equiv": "≡",
    "sim": "∼", "propto": "∝", "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆", "cup": "∪", "cap": "∩",
    "forall": "∀", "exists": "∃", "to": "→", "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒",
    "implies": "⟹", "iff": "⟺", "mapsto": "↦", "searrow": "↘", "swarrow": "↙", "nearrow": "↗", "nwarrow": "↖",
    "int": "∫", "iint": "∬", "iiint": "∭", "oint": "∮", "sum": "∑", "prod": "∏",
    "langle": "⟨", "rangle": "⟩", "triangle": "△", "angle": "∠", "perp": "⊥", "parallel": "∥",
    "ell": "ℓ", "hbar": "ℏ", "degree": "°", "prime": "′", "{": "\\{", "}": "\\}", "|": "‖", "%": "%",
}
LATEX_FUNCTIONS = {"sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan", "sinh", "cosh",
                   "tanh", "ln", "log", "exp", "det", "lim", "max", "min", "sup", "inf", "dim", "deg", "curl",
                   "div", "grad", "proj", "comp", "Re", "Im"}
LATEX_IGNORED = {"left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr", "large", "Large",
                 "LARGE", "huge", "Huge", "small", "displaystyle", "textstyle", "limits", "nolimits", "unicode",
                 "color"}
LATEX_SPACES = {",": " ", ":": " ", ";": " ", " ": " ", "quad": " ", "qquad": " ", "!": ""}
LATEX_TEXT = {"text", "textbf", "textit", "textrm", "mathrm", "operatorname", "textsf", "texttt"}
LATEX_STYLE = {"mathbf", "boldsymbol", "bm", "mathit", "mathsf", "mathcal", "mathscr", "mathfrak", "boxed",
               "pmb", "underline", "overline"}
DOUBLE_STRUCK = {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ"}
# A bracket written as \[ \] would end a display formula \[ ... \], so bmatrix uses plain brackets
MATRIX_DELIMITERS = {"matrix": ("", ""), "smallmatrix": ("", ""), "array": ("", ""), "aligned": ("", ""),
                     "align": ("", ""), "align*": ("", ""), "gathered": ("", ""), "pmatrix": ("(", ")"),
                     "bmatrix": ("[", "]"), "Bmatrix": ("\\{", "\\}"), "vmatrix": ("|", "|"),
                     "Vmatrix": ("‖", "‖"), "cases": ("\\{", "")}
ACCENTS = {"vec": "\u20d7", "overrightarrow": "\u20d7", "hat": "\u0302", "widehat": "\u0302", "bar": "\u0304",
           "tilde": "\u0303", "dot": "\u0307", "ddot": "\u0308"}


class LatexConverter(object):
    def __init__(self, source):
        self.s = source
        self.i = 0

    def peek(self):
        return self.s[self.i] if self.i < len(self.s) else ""

    def skip_spaces(self):
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n":
            self.i += 1

    def read_group_raw(self):
        """Content of {...} unchanged (environment names, column specs, \\text)."""
        self.skip_spaces()
        if self.peek() != "{":
            char = self.peek()
            self.i += 1
            return char
        depth, start = 0, self.i
        while self.i < len(self.s):
            char = self.s[self.i]
            if char == "\\":
                self.i += 2
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    self.i += 1
                    return self.s[start + 1:self.i - 1]
            self.i += 1
        return self.s[start + 1:]

    def read_arg(self):
        """One argument (group, command or character), converted."""
        self.skip_spaces()
        if self.peek() == "{":
            return LatexConverter(self.read_group_raw()).convert()
        if self.peek() == "\\":
            return self.command()
        char = self.peek()
        self.i += 1
        return char

    def command(self):
        self.i += 1                                   # backslash
        start = self.i
        if self.i < len(self.s) and self.s[self.i].isalpha():
            while self.i < len(self.s) and self.s[self.i].isalpha():
                self.i += 1
            name = self.s[start:self.i]
        else:
            name = self.s[self.i:self.i + 1]
            self.i += 1

        if name in LATEX_SPACES:
            return LATEX_SPACES[name]
        if name == "\\":
            return " "                                # line break outside a matrix
        if name in LATEX_IGNORED:
            if name in ("unicode", "color"):
                self.read_group_raw()
            return ""
        if name in LATEX_SYMBOLS:
            return LATEX_SYMBOLS[name]
        if name in LATEX_FUNCTIONS:
            return '\\text"%s"' % name
        if name in ACCENTS:
            arg = self.read_arg()
            plain = re.sub(r"[{}\s]", "", arg)
            if len(plain) == 1:
                return plain + ACCENTS[name]
            return "{" + arg + "}" + ACCENTS[name]
        if name in ("frac", "dfrac", "tfrac"):
            numerator = self.read_arg()
            denominator = self.read_arg()
            return "{%s}/{%s}" % (numerator, denominator)
        if name == "sqrt":
            self.skip_spaces()
            if self.peek() == "[":                    # the index of a root is dropped
                end = self.s.find("]", self.i)
                self.i = end + 1 if end > 0 else self.i + 1
            return "√{%s}" % self.read_arg()
        if name in LATEX_TEXT:
            text = re.sub(r"\\[a-zA-Z]+\s*|[{}\\]", "", self.read_group_raw())
            return '\\text"%s"' % text.replace('"', "'")
        if name == "mathbb":
            arg = self.read_group_raw().strip()
            return DOUBLE_STRUCK.get(arg, arg)
        if name in LATEX_STYLE:
            return "{" + self.read_arg() + "}"
        if name == "textcolor":
            self.read_group_raw()
            return "{" + self.read_arg() + "}"
        if name in ("underbrace", "overbrace"):
            body = self.read_arg()
            self.skip_spaces()
            if self.peek() in "_^":                   # the label of the brace is dropped
                self.i += 1
                self.read_arg()
            return "{" + body + "}"
        if name in ("xrightarrow", "xleftarrow"):
            self.read_arg()
            return "→" if name == "xrightarrow" else "←"
        if name == "begin":
            return self.environment(self.read_group_raw().strip())
        if name == "end":
            self.read_group_raw()
            return ""
        return '\\text"%s"' % name                   # unknown: the name as text instead of a broken formula

    def environment(self, env):
        if env == "array":
            self.read_group_raw()                     # column spec {ccc}
        end_tag = "\\end{%s}" % env
        end = self.s.find(end_tag, self.i)
        body = self.s[self.i:end if end >= 0 else len(self.s)]
        self.i = end + len(end_tag) if end >= 0 else len(self.s)
        rows = []
        for row in re.split(r"\\\\", body):
            cells = [LatexConverter(cell).convert().strip() for cell in row.split("&")]
            if any(cells):
                rows.append(",".join(cell or "\\text\" \"" for cell in cells))
        left, right = MATRIX_DELIMITERS.get(env, ("", ""))
        return "%s\\table %s%s" % (left, ";".join(rows), right)

    def convert(self):
        out = []
        while self.i < len(self.s):
            char = self.s[self.i]
            if char == "\\":
                out.append(self.command())
            elif char in "&~":
                out.append(" ")
                self.i += 1
            else:
                out.append(char)
                self.i += 1
        return "".join(out)


MATH_SEGMENT = re.compile(r"\\\((.*?)\\\)|\\\[(.*?)\\\]", re.S)


def latex_to_jqmath(html_text):
    """Converts the formulas of an HTML field. Formulas containing clozes ({{c1::...}}) are left alone."""
    if "\\(" not in html_text and "\\[" not in html_text:
        return html_text

    def replace(match):
        display = match.group(2) is not None
        source = match.group(2) if display else match.group(1)
        if "{{c" in source:
            return match.group(0)
        plain = html.unescape(re.sub(r"(?i)<br\s*/?>", " ", re.sub(r"<(?!br)[^>]+>", "", source)))
        try:
            converted = LatexConverter(plain).convert()
        except Exception:
            return match.group(0)
        converted = html.escape(re.sub(r"\s+", " ", converted).strip(), quote=False)
        return ("\\[%s\\]" if display else "\\(%s\\)") % converted
    return MATH_SEGMENT.sub(replace, html_text)


# ------------------------------------------------------------------------------------------------
# Media
# ------------------------------------------------------------------------------------------------

def zstd_decompress(data):
    try:
        import zstandard
    except ImportError:
        return None
    return zstandard.ZstdDecompressor().decompress(data, max_output_size=1 << 31)


def is_zstd(data):
    return data[:4] == b"\x28\xb5\x2f\xfd"


def read_varint(data, pos):
    result = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def protobuf_fields(data):
    """Minimal protobuf reader: yields (field number, value) - value is an int or bytes."""
    pos = 0
    while pos < len(data):
        key, pos = read_varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = read_varint(data, pos)
        elif wire == 2:
            length, pos = read_varint(data, pos)
            value = data[pos:pos + length]
            pos += length
        elif wire == 1:
            value = data[pos:pos + 8]
            pos += 8
        elif wire == 5:
            value = data[pos:pos + 4]
            pos += 4
        else:
            break
        yield number, value


def read_media_map(package):
    """File name -> zip entry. Old format: JSON {"0": "image.jpg"}; new format: zstd + protobuf
    MediaEntries { repeated MediaEntry entries = 1; } with MediaEntry { string name = 1; ... } -
    the entry with index n is stored as the file "n" in the package."""
    if "media" not in package.namelist():
        return {}
    raw = package.read("media")
    if raw[:1] in (b"{", b"[") or raw.strip() == b"":
        try:
            return {name: entry for entry, name in json.loads(raw.decode("utf-8") or "{}").items()}
        except ValueError:
            return {}
    if is_zstd(raw):
        raw = zstd_decompress(raw)
        if raw is None:
            return {}
    result = {}
    index = 0
    for number, value in protobuf_fields(raw):
        if number == 1 and isinstance(value, bytes):
            for inner_number, inner_value in protobuf_fields(value):
                if inner_number == 1:
                    result[inner_value.decode("utf-8", "replace")] = str(index)
                    break
            index += 1
    return result


class MediaStore(object):
    """Images of the package as data: URIs, cached per size. Animated GIFs stay unchanged as long as
    they are below the size limit - scaling them down would turn them into a still image."""

    def __init__(self, package, enabled, max_bytes):
        self.package = package
        self.enabled = enabled and package is not None
        self.max_bytes = max_bytes
        self.jpeg_quality = 80
        self.by_name = read_media_map(package) if package is not None else {}
        self.cache = {}
        self.embedded = 0
        self.skipped = 0
        self.missing = 0

    def _load(self, name, max_edge):
        entry = self.by_name.get(name) or self.by_name.get(html.unescape(name))
        if entry is None or entry not in self.package.namelist():
            self.missing += 1
            return None
        data = self.package.read(entry)
        if is_zstd(data):
            data = zstd_decompress(data)
            if data is None:
                return None
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if _Image is not None and not name.lower().endswith(".svg"):
            try:
                image = _Image.open(io.BytesIO(data))
                animated = getattr(image, "is_animated", False)
                too_big = max(image.size) > max_edge
                if (not animated) or (len(data) > self.max_bytes):
                    # Scale down if needed and store as JPEG whenever that is smaller and loses no
                    # transparency - images are embedded once per note, and shared decks reuse them in
                    # many notes (AMBOSS Vorklinik: 1266 images in 5001 places)
                    image.seek(0)
                    transparent = ("A" in image.getbands()) or ("transparency" in image.info)
                    if image.mode not in ("RGB", "RGBA"):
                        image = image.convert("RGBA" if transparent else "RGB")
                    if too_big:
                        image.thumbnail((max_edge, max_edge))
                    candidates = []
                    if not transparent:
                        buffer = io.BytesIO()
                        image.convert("RGB").save(buffer, format="JPEG", quality=self.jpeg_quality, optimize=True)
                        candidates.append((buffer.getvalue(), "image/jpeg"))
                    if transparent or mime == "image/png":
                        buffer = io.BytesIO()
                        image.save(buffer, format="PNG", optimize=True)
                        candidates.append((buffer.getvalue(), "image/png"))
                    if not too_big:
                        candidates.append((data, mime))      # the original may be the smallest
                    data, mime = min(candidates, key=lambda candidate: len(candidate[0]))
            except Exception:
                pass
        if len(data) > self.max_bytes:
            self.skipped += 1
            return None
        self.embedded += 1
        return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))

    def data_uri(self, name, max_edge):
        key = (name, max_edge)
        if key not in self.cache:
            self.cache[key] = self._load(name, max_edge) if self.enabled else None
        return self.cache[key]

    def embed(self, text, max_edge):
        def replace(match):
            uri = self.data_uri(match.group(2), max_edge)
            if uri:
                return match.group(1) + uri + match.group(3)
            return "<i>[%s]</i>" % html.escape(match.group(2))   # not embedded: show the file name
        return re.sub(r'(<img[^>]*?src=["\'])([^"\']+)(["\'][^>]*>)',
                      lambda m: replace(m) if not m.group(2).startswith("data:") else m.group(0), text, flags=re.I)


# ------------------------------------------------------------------------------------------------
# Reading the Anki collection
# ------------------------------------------------------------------------------------------------

class Collection(object):
    """Reads note types, decks, notes and cards from an Anki collection (both schemas)."""

    def __init__(self, path):
        self.con = sqlite3.connect(path)
        self.con.text_factory = lambda b: b.decode("utf-8", "replace")
        cur = self.con.cursor()
        tables = {row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.new_schema = "notetypes" in tables
        self.crt = cur.execute("SELECT crt FROM col").fetchone()[0]
        self.notetypes = {}   # id -> {"name", "fields": [..], "templates": [..], "cloze": bool}
        self.decks = {}       # id -> name with "::"

        if self.new_schema:
            raw = sqlite3.connect(path)          # config is a protobuf BLOB
            for ntid, name, config in raw.execute("SELECT id, name, config FROM notetypes"):
                cloze = False
                try:
                    for number, value in protobuf_fields(config or b""):
                        if number == 1 and value == 1:        # NotetypeConfig.kind: 1 = cloze
                            cloze = True
                except Exception:
                    pass
                self.notetypes[ntid] = {"name": name, "fields": [], "templates": [], "cloze": cloze}
            for ntid, _ord, name in raw.execute("SELECT ntid, ord, name FROM fields ORDER BY ntid, ord"):
                if ntid in self.notetypes:
                    self.notetypes[ntid]["fields"].append(name)
            try:
                for ntid, _ord, name in raw.execute("SELECT ntid, ord, name FROM templates ORDER BY ntid, ord"):
                    if ntid in self.notetypes:
                        self.notetypes[ntid]["templates"].append(name)
            except sqlite3.Error:
                pass
            for did, name in raw.execute("SELECT id, name FROM decks"):
                self.decks[did] = name.replace("\x1f", "::")
            raw.close()
        else:
            models_json, decks_json = cur.execute("SELECT models, decks FROM col").fetchone()
            for mid, model in json.loads(models_json or "{}").items():
                self.notetypes[int(mid)] = {
                    "name": model.get("name", "Note type"),
                    "fields": [f.get("name", "") for f in sorted(model.get("flds", []), key=lambda f: f.get("ord", 0))],
                    "templates": [t.get("name", "") for t in sorted(model.get("tmpls", []), key=lambda t: t.get("ord", 0))],
                    "cloze": model.get("type") == 1,
                }
            for did, deck in json.loads(decks_json or "{}").items():
                self.decks[int(did)] = deck.get("name", "Deck")

    def notes(self):
        cur = self.con.cursor()
        for nid, mid, tags, flds in cur.execute("SELECT id, mid, tags, flds FROM notes ORDER BY id"):
            yield {"id": nid, "mid": mid, "tags": tags.split(), "fields": flds.split(FIELD_SEPARATOR)}

    def cards(self):
        """nid -> list of cards with deck and learning state."""
        cur = self.con.cursor()
        last_review = {}
        try:
            for cid, last in cur.execute("SELECT cid, MAX(id) FROM revlog GROUP BY cid"):
                last_review[cid] = last
        except sqlite3.Error:
            pass
        result = {}
        for cid, nid, did, ord_, ctype, queue, due, ivl, factor, reps, lapses, odid, data in cur.execute(
                "SELECT id, nid, did, ord, type, queue, due, ivl, factor, reps, lapses, odid, data FROM cards"):
            result.setdefault(nid, []).append({
                "id": cid, "deck": odid or did, "ord": ord_, "type": ctype, "queue": queue, "due": due,
                "ivl": ivl, "factor": factor, "reps": reps, "lapses": lapses, "data": data or "",
                "lastReview": last_review.get(cid, 0),
            })
        return result

    def revlog(self):
        try:
            return list(self.con.execute("SELECT id, cid, ease, ivl, lastIvl, factor, time, type FROM revlog ORDER BY id"))
        except sqlite3.Error:
            return []

    def is_placeholder(self):
        """Packages in the new format also contain an old collection with a single note that asks the
        user to update Anki."""
        count = self.con.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        return count <= 1 and len(self.decks) <= 2


def open_package(path, workdir):
    """Returns (Collection, ZipFile, hints). The collection is extracted into a temporary file
    (SQLite cannot read from the zip); media stay in the package and are read one by one."""
    hints = []
    package = zipfile.ZipFile(path)
    names = package.namelist()
    candidates = [n for n in ("collection.anki21b", "collection.anki21", "collection.anki2") if n in names]
    if not candidates:
        raise SystemExit("No Anki collection found in the package (%s)." % ", ".join(names[:10]))

    for name in candidates:
        target = os.path.join(workdir, name + ".sqlite")
        with package.open(name) as source:
            head = source.read(4)
        if name.endswith("anki21b") or is_zstd(head):
            plain = zstd_decompress(package.read(name))
            if plain is None:
                hints.append("Package in the new format (collection.anki21b, zstd) - the Python package "
                             "'zstandard' is missing (pip install zstandard). Trying the older collection.")
                continue
            with open(target, "wb") as handle:
                handle.write(plain)
        else:
            with package.open(name) as source, open(target, "wb") as handle:
                shutil.copyfileobj(source, handle, 1 << 20)
        collection = Collection(target)
        if collection.is_placeholder() and name != candidates[-1]:
            hints.append("%s only contains the placeholder note - skipped." % name)
            continue
        if collection.is_placeholder():
            hints.append("Warning: %s only contains the placeholder note 'Please update Anki' - the real "
                         "content is in collection.anki21b (install zstandard)." % name)
        hints.append("Read %s (%s schema)." % (name, "new" if collection.new_schema else "old"))
        return collection, package, hints

    raise SystemExit("\n".join(hints) or "No readable collection.")


# ------------------------------------------------------------------------------------------------
# Learning state Anki -> KBB
# ------------------------------------------------------------------------------------------------

def learn_values(card, crt):
    """Learning state of an Anki card as (State, S, D, Due, LastReview, Reps, Lapses, ScheduledDays) -
    None for new cards. FSRS values from cards.data (s, d) when present, otherwise approximated from
    the SM-2 interval (S = interval, D from the ease factor)."""
    if card["type"] == 0 or card["reps"] == 0:
        return None
    try:
        data = json.loads(card["data"]) if card["data"].startswith("{") else {}
    except ValueError:
        data = {}

    if card["type"] == 2:            # review: due = days since the collection was created
        due_ms = (crt + card["due"] * 86400) * 1000
        state = 2
    else:                            # learning/relearning: due = Unix seconds
        due_ms = card["due"] * 1000 if card["due"] > 10 ** 9 else int(time.time() * 1000)
        state = 3 if card["type"] == 3 else 1

    stability = float(data.get("s") or 0) or max(0.5, float(card["ivl"] or 0))
    if data.get("d"):
        difficulty = float(data["d"])
    else:
        ease = (card["factor"] or 2500) / 1000.0           # 1.3 (hard) .. 3.0+ (easy)
        difficulty = max(1.0, min(10.0, 10.0 - (ease - 1.3) * 5.0))
    last = card["lastReview"] or int(due_ms - stability * DAY_MS)
    return (state, round(stability, 4), round(difficulty, 4), int(due_ms), int(last), card["reps"], card["lapses"],
            round(stability, 2))


# ------------------------------------------------------------------------------------------------
# Writing into the .kdb
# ------------------------------------------------------------------------------------------------

class KdbWriter(object):
    """Builds the deck tree, items, tags, notes and learning state directly in the .kdb."""

    def __init__(self, db, root_name, relation_label, deck_category):
        self.db = db
        self.relation_label = relation_label
        self.deck_template = db.get_or_create_item_template(deck_category)
        self.templates = {}
        self.deck_ids = {}
        self.value_ids = {}
        self.group_ids = {}
        self.max_children = 25
        self.group_label = "Part %s · %s"
        self.touched = set()
        self.root_name = root_name
        # Decks and groups are containers, not knowledge: "#nolearn" keeps the learning mode from asking
        # "Which category does Part 3 belong to?" or "What belongs to Anatomy?"
        self.nolearn = [self.template("#nolearn")]
        self.root = self.add_item(root_name, "", "", self.deck_template, self.nolearn)
        self.user_id = self._user_id()
        self._create_learn_tables()
        self.learn_rows = 0
        self.review_rows = 0

    # -- Building blocks -------------------------------------------------------------------------

    def template(self, name):
        key = name.strip().lower()
        if key not in self.templates:
            self.templates[key] = self.db.get_or_create_item_template(name)
        return self.templates[key]

    def add_item(self, name, description, note_html, template_id, tag_ids, search_text=None):
        # Name and description only count as HTML with a leading <html> (htmlToHtml in kmap.js) -
        # without it the application shows the tags as text
        if description and re.search(r"<[a-zA-Z/!]|&[#a-zA-Z0-9]+;", description) \
                and not description.lower().startswith("<html>"):
            description = "<html>%s</html>" % description
        item_id = self.db.new_item(template_id, name, description, "", "anki:" + unique_id())
        self.db.set_search_text(item_id, search_text if search_text is not None else name)
        if tag_ids:
            self.db.set_item_tags(item_id, list(dict.fromkeys(tag_ids)))
        if note_html:
            record_id = self.db.new_repository_record(
                note_html.encode("utf-8"), strip_html(note_html), None, "html", "text/html", name, "")
            self.db.attach_record(item_id, record_id, 0)
        return item_id

    def add_relation(self, source, target, label=None):
        relation_id = self.db.new_relation(source, target, -1, self.relation_label if label is None else label, "")
        self.touched.update((source, target))
        return relation_id

    def group_parent(self, deck_item, deck_key, index, total, label):
        """Parent for the index-th note of a deck with total notes. The diagram loads about 100 items at
        once, so a deck with more than max_children notes is split into groups of max_children
        ("Teil 3 · <first card>"), and into groups of groups when even those are too many."""
        size = self.max_children
        if size <= 1 or total <= size:
            return deck_item
        levels = 1
        while size ** (levels + 1) < total:
            levels += 1
        parent = deck_item
        path = []
        for level in range(levels, 0, -1):
            number = index // (size ** level)
            path.append(number % size if level < levels else number)
            key = (deck_key, tuple(path))
            if key not in self.group_ids:
                title = self.group_label % (".".join(str(p + 1) for p in path), shorten(label, 40))
                self.group_ids[key] = self.add_item(title, "", "", self.deck_template, self.nolearn)
                self.add_relation(parent, self.group_ids[key])
            parent = self.group_ids[key]
        return parent

    def value_item(self, text, category_id):
        """Shared item for a field value (e.g. a nerve that innervates several muscles)."""
        key = (text.lower(), category_id)
        if key not in self.value_ids:
            self.value_ids[key] = self.add_item(text, "", "", category_id, [])
        return self.value_ids[key]

    def set_relation_progress(self, relation_id, values):
        self.db.cur.execute("INSERT OR REPLACE INTO LearnProgress VALUES(?,1,?,1,0,?,?,?,?,?,?,?,?)",
                            (self.user_id, relation_id) + tuple(values))
        self.learn_rows += 1

    def deck(self, full_name):
        """Deck 'A::B::C' as a chain of items below the root."""
        parent = self.root
        path = []
        parts = [p.strip() for p in (full_name or "").split("::") if p.strip()]
        if parts and parts[0].lower() == self.root_name.lower():
            parts = parts[1:]        # topmost deck = root (package with one main deck, round trip)
        for part in parts:
            path.append(part)
            key = "::".join(path).lower()
            if key not in self.deck_ids:
                self.deck_ids[key] = self.add_item(part, "", "", self.deck_template, self.nolearn)
                self.add_relation(parent, self.deck_ids[key])
            parent = self.deck_ids[key]
        return parent

    # -- Learning state --------------------------------------------------------------------------

    def _user_id(self):
        cur = self.db.cur
        cur.execute("SELECT UserID FROM Users WHERE UserNameLC=?", (DEFAULT_USER,))
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute("INSERT INTO Users VALUES(NULL, ?, ?, '', '', '')", (DEFAULT_USER, DEFAULT_USER))
        return cur.lastrowid

    def _create_learn_tables(self):
        """Same as createLearnTables in sqlitedataaccess.php (schema version 5, no undo triggers)."""
        cur = self.db.cur
        cur.execute("CREATE TABLE IF NOT EXISTS LearnProgress(UserID INTEGER NOT NULL, TargetType INTEGER NOT NULL, "
                    "TargetID INTEGER NOT NULL, CardType INTEGER NOT NULL, Direction INTEGER NOT NULL, State INTEGER, "
                    "Stability REAL, Difficulty REAL, Due INTEGER, LastReview INTEGER, Reps INTEGER, Lapses INTEGER, "
                    "ScheduledDays REAL, PRIMARY KEY(UserID, TargetType, TargetID, CardType, Direction));")
        cur.execute("CREATE TABLE IF NOT EXISTS ReviewLog(ReviewID INTEGER PRIMARY KEY AUTOINCREMENT, UserID INTEGER NOT NULL, "
                    "TargetType INTEGER, TargetID INTEGER, CardType INTEGER, Direction INTEGER, Rating INTEGER, State INTEGER, "
                    "Due INTEGER, Stability REAL, Difficulty REAL, ElapsedDays REAL, ScheduledDays REAL, ReviewTimeMs INTEGER, "
                    "ReviewedAt INTEGER);")
        cur.execute("CREATE INDEX IF NOT EXISTS ReviewLog_UserID_IDX ON ReviewLog(UserID, ReviewedAt);")
        version = self.db.get_property("dbvers")
        if version and int(version) >= 4:
            self.db.set_property("dbvers", "5")

    def set_progress(self, item_id, card_type, direction, values):
        self.db.cur.execute("INSERT OR REPLACE INTO LearnProgress VALUES(?,0,?,?,?,?,?,?,?,?,?,?,?)",
                            (self.user_id, item_id, card_type, direction) + tuple(values))
        self.learn_rows += 1

    def add_review(self, item_id, card_type, direction, revlog_row):
        rid, _cid, ease, ivl, last_ivl, factor, duration, rtype = revlog_row
        state = {0: 0, 1: 2, 2: 3, 3: 2}.get(rtype, 2)
        days = ivl if ivl > 0 else 0
        ease_factor = (factor or 2500) / 1000.0
        difficulty = max(1.0, min(10.0, 10.0 - (ease_factor - 1.3) * 5.0))
        self.db.cur.execute("INSERT INTO ReviewLog VALUES(NULL,?,0,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (self.user_id, item_id, card_type, direction, max(1, min(4, ease)), state,
                             rid + days * DAY_MS, max(0.5, days), round(difficulty, 4),
                             max(0, last_ivl), days, max(0, min(duration, 600000)), rid))
        self.review_rows += 1

    def finish(self):
        for item_id in sorted(self.touched):
            self.db.update_relation_sequence(item_id)
        self.db.bump_diagram_update_count()


# ------------------------------------------------------------------------------------------------
# Import
# ------------------------------------------------------------------------------------------------

def field_index(names, candidates):
    lower = [n.lower() for n in names]
    for candidate in candidates:
        if candidate in lower:
            return lower.index(candidate)
    return -1


def note_section(title, content):
    return '<p><b>%s:</b></p><div>%s</div>' % (html.escape(title), content)


def import_package(args, db):
    workdir = tempfile.mkdtemp(prefix="anki2kbb_")
    try:
        collection, package, hints = open_package(args.input, workdir)
        media = MediaStore(package, args.media, args.max_media_kb * 1024)
        decks = sorted(collection.decks.values(), key=len)
        main_decks = {d.split("::", 1)[0] for d in decks if d.lower() != "default"}
        root_name = args.root_name or (list(main_decks)[0] if len(main_decks) == 1
                                       else os.path.splitext(os.path.basename(args.input))[0])
        writer = KdbWriter(db, root_name, args.relation, args.deck_category)
        deck_tag_prefixes = {re.sub(r"[\s_]+", "", d).lower() for d in main_decks}
        cards_by_note = collection.cards()
        writer.max_children = args.max_children
        writer.group_label = args.group_label
        deck_totals = {}         # deck -> number of notes, for splitting large decks into groups
        for note_cards in cards_by_note.values():
            deck_key = note_cards[0]["deck"]
            deck_totals[deck_key] = deck_totals.get(deck_key, 0) + 1
        deck_positions = {}
        card_target = {}         # Anki card ID -> (ItemID, KBB card type, direction)
        stats = {"notes": 0, "cloze": 0, "imageOcclusion": 0, "fieldCards": 0, "withProgress": 0, "empty": 0,
                 "melodies": 0, "audioOnly": 0}
        texts = MUSIC_TEXTS.get(args.lang, MUSIC_TEXTS["en"])
        tag_map = None
        if not args.all_tags:
            note_tags = [note["tags"] for note in collection.notes()]
            tag_map = build_tag_map(note_tags, deck_tag_prefixes, max(3, int(0.005 * len(note_tags))))

        for note in collection.notes():
            notetype = collection.notetypes.get(note["mid"], {"name": "Note type", "fields": [], "cloze": False})
            names = list(notetype["fields"]) + ["Field %d" % (i + 1) for i in range(len(notetype["fields"]), len(note["fields"]))]
            raw = [sanitize_html(f) for f in note["fields"]]
            if args.convert_math:
                raw = [latex_to_jqmath(f) for f in raw]
            cards = cards_by_note.get(note["id"], [])
            deck_name = collection.decks.get(cards[0]["deck"], "") if cards else ""

            # Melody notes (sight singing, rhythm and interval drills): the card template draws the staff
            # from the JSON in "MelodyJSON" with a script. Notes that compare the written melody with a
            # recording (field "ErrorVariants") cannot be asked without sound and are left out.
            lower_field_names = [n.lower() for n in notetype["fields"]]
            melody = None
            if "melodyjson" in lower_field_names:
                melody = music_notation.parse_melody(note["fields"][lower_field_names.index("melodyjson")])
                if melody is not None and "errorvariants" in lower_field_names:
                    stats["audioOnly"] += 1
                    continue

            parent = writer.deck(deck_name)
            deck_parts = [p.strip() for p in deck_name.split("::") if p.strip()]
            if deck_parts and deck_parts[0].lower() == root_name.lower():
                deck_parts = deck_parts[1:]
            used = set(i for i, n in enumerate(names) if n.lower() in SKIP_FIELDS)
            cloze_numbers = []
            lower_type = notetype["name"].lower()
            # Field cards: templates named after fields ("Ursprung", "Ansatz", ...) ask one field each - in
            # KBB these become labelled relations to shared value items
            lower_names = [n.lower() for n in names]
            answer_fields = [lower_names.index(t.lower()) for t in notetype.get("templates", []) if t.lower() in lower_names]
            field_cards = (len(answer_fields) >= 2) and not notetype["cloze"]

            name_html = None
            if melody is not None:
                stats["melodies"] += 1
                rhythm = ("rhythm" in lower_type) or not melody.get("degrees")
                question = texts["rhythm"] if rhythm else texts["sing"]
                stage = field_index(names, ["stageid"])
                stage_text = strip_html(raw[stage]) if stage >= 0 else ""
                label = question
                name_html = '<html><div>%s</div>%s</html>' % (html.escape(question), music_notation.melody_img(melody, 0.8))
                count = music_notation.rhythm_grid_text(melody)
                degrees = " ".join(str(d) for d in (melody.get("degrees") or []))
                if rhythm:
                    description = '%s<br><span style="font-size:0.8em;color:#777">%s</span>' % (
                        music_notation.rhythm_grid_html(melody), html.escape(texts["legend"]))
                else:
                    description = "<b>%s</b><br>%s" % (html.escape(music_notation.solfege(melody)),
                                                      html.escape(music_notation.note_names(melody)))
                    if degrees:
                        description += '<br><span style="font-size:0.8em;color:#777">%s: %s</span>' % (
                            html.escape(texts["degrees"]), html.escape(degrees))
                facts = [(texts["key"], "%s %s" % (melody.get("key", ""), str(melody.get("mode", "")).replace("_", " "))),
                         (texts["time"], melody.get("timeSig", "")), (texts["clef"], melody.get("clef", ""))]
                supports = melody.get("supports") or {}
                if not rhythm:
                    facts += [(texts["tonic"], supports.get("tonic", "")), (texts["first"], supports.get("firstNote", "")),
                              (texts["notes"], music_notation.note_names(melody))]
                facts.append((texts["count"], count))
                if stage_text:
                    facts.append(("Stage", stage_text))
                description_note = "".join("<p><b>%s:</b> %s</p>" % (html.escape(k), html.escape(str(v))) for k, v in facts if v)
                description_note += "<p><i>%s</i></p>" % html.escape(texts["audio"])
                used.update(range(len(raw)))      # JSON, IDs and sound fields: nothing else for the note
                front_full = ""
            elif "occlusion" in lower_type or "bildverdeckung" in lower_type:
                stats["imageOcclusion"] += 1
                header = field_index(names, ["header", "kopfzeile", "titel", "title"])
                image = field_index(names, ["image", "bild"])
                label = strip_html(raw[header]) if header >= 0 else ""
                label = label or "Image Occlusion %d" % stats["imageOcclusion"]
                description = raw[image] if image >= 0 else ""
                used.update(i for i in (header, image) if i >= 0)
                front_full = ""
            elif notetype["cloze"] or is_cloze(raw[:1]):
                stats["cloze"] += 1
                text = field_index(names, ["text"])
                text = text if text >= 0 else 0
                label = strip_html(cloze_reveal(raw[text]))
                description, cloze_numbers = cloze_to_highlight(raw[text])
                used.add(text)
                front_full = ""
            elif field_cards:
                stats["fieldCards"] += 1
                name_field = min(answer_fields) - 1 if min(answer_fields) > 0 else 0
                label = strip_html(raw[name_field])
                lines = ["<b>%s:</b> %s" % (html.escape(names[i]), raw[i]) for i in answer_fields if has_content(raw[i])]
                description = "<br>".join(lines)
                used.update([name_field] + answer_fields)
                front_full = ""
            else:
                front = 0
                back = field_index(names, ["back", "answer", "rückseite", "antwort", "definition", "bedeutung"])
                if back < 0 or not has_content(raw[back]):
                    back = next((i for i in range(1, len(raw)) if i not in used and has_content(raw[i])), -1)
                label = strip_html(raw[front])
                description = raw[back] if back >= 0 else ""
                used.update(i for i in (front, back) if i >= 0)
                front_full = raw[front]

            if not label and not has_content(description):
                stats["empty"] += 1
                continue

            # Note: the complete front (when the name is shortened or formatted), then all remaining
            # fields with their names
            sections = []
            if front_full and (len(label) > args.max_name or front_full != html.escape(label)):
                sections.append(note_section(names[0], front_full))
            for index, content in enumerate(raw):
                if index not in used and has_content(content):
                    sections.append(note_section(names[index], content))
            note_html = media.embed("".join(sections), args.note_image_edge) if sections else ""
            if melody is not None:
                note_html = description_note
            description = media.embed(description, args.description_image_edge)

            tag_ids = []
            for tag in note["tags"]:
                name = tag_map.get(tag) if tag_map is not None else tag_name(tag, deck_tag_prefixes)
                if name:
                    tag_ids.append(writer.template(name))

            # Category: the top deck level (gives sensible "Which category does ... belong to?"
            # questions and a colour per chapter), alternatively the note type or none
            if args.category == "notetype":
                category_id = writer.template(notetype["name"])
            elif args.category == "deck" and deck_parts:
                category_id = writer.template(deck_parts[0])
            else:
                category_id = -1

            deck_key = cards[0]["deck"] if cards else None
            position = deck_positions.get(deck_key, 0)
            deck_positions[deck_key] = position + 1
            parent = writer.group_parent(parent, deck_key, position, deck_totals.get(deck_key, 0), label)

            if name_html:
                item_id = writer.add_item(name_html, description, note_html, category_id, tag_ids, search_text=label)
            else:
                item_id = writer.add_item(shorten(label, args.max_name) or "(no text)", description, note_html,
                                          category_id, tag_ids)
            writer.add_relation(parent, item_id)
            stats["notes"] += 1

            # Field cards: one labelled relation per answer field to a shared value item; the Anki card
            # of template k carries its learning state to the relation card "Name — field → ?"
            if field_cards:
                relation_by_field = {}
                for i in answer_fields:
                    value = shorten(strip_html(raw[i]), args.max_name)
                    if value:
                        target = writer.value_item(value, writer.template(names[i]))
                        relation_by_field[i] = writer.add_relation(item_id, target, names[i])
                for card in cards:
                    if card["ord"] < len(notetype.get("templates", [])):
                        template = notetype["templates"][card["ord"]].lower()
                        field = lower_names.index(template) if template in lower_names else -1
                        values = learn_values(card, collection.crt)
                        if field in relation_by_field and values:
                            writer.set_relation_progress(relation_by_field[field], values)
                            stats["withProgress"] += 1
                continue

            # Learning state. Cloze: Anki card ord k asks for cloze c(k+1), which is one gap card in
            # KBB (gap index = position of the number in order of first appearance). Otherwise the
            # most advanced card of the note counts for the description card.
            progressed = False
            if cloze_numbers:
                for card in cards:
                    number = card["ord"] + 1
                    if number not in cloze_numbers[:MAX_GAPS]:
                        continue
                    gap = cloze_numbers.index(number)
                    card_target[card["id"]] = (item_id, 6, gap)
                    values = learn_values(card, collection.crt)
                    if values:
                        writer.set_progress(item_id, 6, gap, values)
                        progressed = True
            else:
                best = None
                for card in cards:
                    card_target[card["id"]] = (item_id, 0, 0)
                    values = learn_values(card, collection.crt)
                    if values and (best is None or values[1] > best[1]):
                        best = values
                if best:
                    writer.set_progress(item_id, 0, 0, best)
                    progressed = True
            if progressed:
                stats["withProgress"] += 1

        for row in collection.revlog():
            target = card_target.get(row[1])
            if target:
                writer.add_review(target[0], target[1], target[2], row)

        writer.finish()
        hints.append("Media embedded: %d, skipped as too large: %d, missing: %d%s." % (
            media.embedded, media.skipped, media.missing, "" if _Image else " (not scaled down without Pillow)"))
        hints.append("Learning state: %d cards, review log: %d entries." % (writer.learn_rows, writer.review_rows))
        if stats["melodies"] or stats["audioOnly"]:
            hints.append("Melodies drawn as staff: %d; left out because they need the recordings (error detection): %d."
                         % (stats["melodies"], stats["audioOnly"]))
        collection.con.close()
        return stats, hints
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def import_text(args, db):
    """Anki text export (with # header lines) or Quizlet export (term<TAB>definition per line)."""
    with open(args.input, encoding="utf-8-sig") as handle:
        raw = handle.read()

    header = {}
    lines = raw.splitlines()
    while lines and lines[0].startswith("#") and ":" in lines[0]:
        key, value = lines.pop(0)[1:].split(":", 1)
        header[key.strip().lower()] = value.strip()

    separator = args.separator
    if separator is None:
        named = {"tab": "\t", "comma": ",", "semicolon": ";", "space": " ", "pipe": "|", "colon": ":"}
        separator = named.get(header.get("separator", "").lower(), header.get("separator") or None)
    if separator is None:
        sample = "\n".join(lines[:20])
        separator = "\t" if "\t" in sample else ("," if sample.count(",") >= len(lines[:20]) else "\t")

    body = "\n".join(lines)
    if args.row_separator:
        body = body.replace(args.row_separator, "\n")

    is_html = header.get("html", "false").lower() == "true"

    def column(name):
        return int(header[name]) - 1 if name in header else -1
    deck_column, tags_column = column("deck column"), column("tags column")
    notetype_column, guid_column = column("notetype column"), column("guid column")
    skip = {c for c in (deck_column, tags_column, notetype_column, guid_column) if c >= 0}

    root_name = args.root_name or os.path.splitext(os.path.basename(args.input))[0]
    writer = KdbWriter(db, root_name, args.relation, args.deck_category)
    stats = {"notes": 0, "cloze": 0, "imageOcclusion": 0, "fieldCards": 0, "withProgress": 0, "empty": 0}
    default_type = "Quizlet" if not header else "Basic"

    reader = csv.reader(io.StringIO(body), delimiter=separator, quotechar='"') if len(separator) == 1 else \
        (line.split(separator) for line in body.splitlines())
    for columns in reader:
        if not columns or not any(c.strip() for c in columns):
            continue
        fields = [c for index, c in enumerate(columns) if index not in skip]
        if not is_html:
            fields = [html.escape(f).replace("\n", "<br>") for f in fields]
        fields = [sanitize_html(f) for f in fields]
        if args.convert_math:
            fields = [latex_to_jqmath(f) for f in fields]
        deck = columns[deck_column] if 0 <= deck_column < len(columns) else ""
        tags = columns[tags_column].split() if 0 <= tags_column < len(columns) else []
        notetype = columns[notetype_column] if 0 <= notetype_column < len(columns) else default_type
        deck_parts = [p.strip() for p in deck.split("::") if p.strip()]
        if deck_parts and deck_parts[0].lower() == root_name.lower():
            deck_parts = deck_parts[1:]

        if is_cloze(fields[:1]):
            stats["cloze"] += 1
            label = strip_html(cloze_reveal(fields[0]))
            description = cloze_to_highlight(fields[0])[0]
            extra = "<br>".join(f for f in fields[1:] if has_content(f))
        else:
            label = strip_html(fields[0])
            description = fields[1] if len(fields) > 1 else ""
            extra = "<br>".join(f for f in fields[2:] if has_content(f))

        if not label:
            stats["empty"] += 1
            continue

        if args.category == "notetype":
            category_id = writer.template(notetype)
        elif args.category == "deck" and deck_parts:
            category_id = writer.template(deck_parts[0])
        else:
            category_id = -1

        tag_ids = [writer.template(t.replace("_", " ")) for t in tags]
        item_id = writer.add_item(shorten(label, args.max_name), description, extra, category_id, tag_ids)
        writer.add_relation(writer.deck(deck), item_id)
        stats["notes"] += 1

    writer.finish()
    return stats, ["Text read, separator %r, HTML %s." % (separator, "yes" if is_html else "no")]


# Diagram formatting of a new database, the keys the formatting gallery writes into "dp".
DIAGRAM_PROPERTIES = {
    "dl": 1,       # layout: mind map radial (straight lines)
    "iex": 0,      # items collapsed - an expanded deck shows every answer at once
    "lcp": "anki", # learning card profile: no category and parts cards (category = top deck, parts = decks)
}


def set_diagram_layout(db):
    try:
        properties = json.loads(db.get_diagram_property("dp") or "{}")
    except ValueError:
        properties = {}
    properties.update(DIAGRAM_PROPERTIES)
    db.set_diagram_property("dp", json.dumps(properties, separators=(",", ":")))


def run_import(args):
    target = os.path.abspath(args.db)
    if os.path.exists(target):
        if not args.append:
            raise SystemExit("%s already exists - choose another name or pass --append." % target)
    else:
        template = os.path.abspath(args.template)
        if not os.path.exists(template):
            raise SystemExit("Template database not found: %s" % template)
        shutil.copyfile(template, target)

    started = time.time()
    db = KbbDatabase(target)
    try:
        db.ensure_tag_tables()
        db.disable_undo_triggers()      # an import is not an undo step - the undo log would grow huge
        if zipfile.is_zipfile(args.input):
            stats, hints = import_package(args, db)
        else:
            stats, hints = import_text(args, db)
        if not args.append:
            set_diagram_layout(db)
        db.enable_undo_triggers()
        db.cur.execute("DELETE FROM UndoLog")
        db.commit()
    except BaseException:
        db.con.rollback()
        db.close()
        if not args.append:
            os.remove(target)
        raise
    db.close()

    con = sqlite3.connect(target)
    counts = [con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ("Items", "Relations", "ItemTemplates")]
    con.execute("VACUUM")
    con.close()
    for hint in hints:
        print(hint)
    print("Notes: %(notes)d (cloze %(cloze)d, image occlusion %(imageOcclusion)d, field cards %(fieldCards)d, empty skipped %(empty)d), "
          "with learning state %(withProgress)d" % stats)
    print("Database: %s - %d items, %d relations, %d categories/tags, %.1f MB, %.1f s" % (
        target, counts[0], counts[1], counts[2], os.path.getsize(target) / 1048576.0, time.time() - started))


# ------------------------------------------------------------------------------------------------
# Export -> Anki text
# ------------------------------------------------------------------------------------------------

def unwrap_html(text):
    return re.sub(r"(?i)^<html>|</html>$", "", text or "")


def load_kdb_graph(path):
    """Items, relations and categories/tags of a .kdb in the shape of the JSON export."""
    con = sqlite3.connect(path)
    templates = dict(con.execute("SELECT ItemTemplateID, TemplateName FROM ItemTemplates"))
    tags = {}
    try:
        for item_id, template_id in con.execute("SELECT ItemID, ItemTemplateID FROM ItemTags ORDER BY Sequence"):
            tags.setdefault(item_id, []).append(template_id)
    except sqlite3.Error:
        pass
    nodes = []
    for item_id, template_id, name, description in con.execute(
            "SELECT ItemID, ItemTemplateID, Name, Description FROM Items ORDER BY ItemID"):
        nodes.append({"id": item_id, "label": name or "", "description": description or "",
                      "categoryId": [template_id] + tags.get(item_id, [])})
    edges = [{"source": a, "target": b, "label": label or ""} for a, b, label in con.execute(
        "SELECT IDFrom, IDTo, Name FROM Relations ORDER BY RelationID")]
    con.close()
    return {"nodes": nodes, "edges": edges,
            "nodeCategories": [{"id": k, "name": v} for k, v in templates.items()]}


def export_text(args):
    """One "Basic" note (name / description) per item with a description; deck = path of the parent
    items from the root, tags = KBB tags. Items with highlights become "Cloze" notes."""
    if args.input.lower().endswith(".kdb"):
        data = load_kdb_graph(args.input)
    else:
        with open(args.input, encoding="utf-8-sig") as handle:
            data = json.load(handle)
        if "data" in data and "nodes" not in data:
            data = data["data"]

    nodes = {n["id"]: n for n in data.get("nodes", [])}
    categories = {c["id"]: c["name"] for c in data.get("nodeCategories", [])}
    parent = {}
    for edge in data.get("edges", []):
        if edge.get("target") in nodes and edge.get("target") not in parent and not str(edge.get("label", "")).startswith("T:"):
            parent[edge["target"]] = edge.get("source")

    def path(node_id):
        names = []
        guard = 0
        while node_id in parent and guard < 50:
            node_id = parent[node_id]
            names.append(strip_html(unwrap_html(nodes[node_id]["label"])) if node_id in nodes else "")
            guard += 1
        names.reverse()
        if not names:
            return args.deck or "KBB"
        return "::".join([args.deck or names[0]] + names[1:])

    out = io.StringIO()
    out.write("#separator:tab\n#html:true\n#notetype column:1\n#deck column:2\n#tags column:5\n")
    writer = csv.writer(out, delimiter="\t", quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    count = cloze = 0
    marker = re.compile(r'<span([^>]*background-color[^>]*)>(.*?)</span>', re.I | re.S)
    for node in nodes.values():
        description = unwrap_html(node.get("description"))
        if not strip_html(description) and "<img" not in description:
            continue
        category = node.get("categoryId")
        ids = category if isinstance(category, list) else ([category] if category is not None else [])
        tags = [categories.get(i, "").replace(" ", "_") for i in ids[1:] if categories.get(i)]
        name = unwrap_html(node.get("label", ""))
        if marker.search(description):
            number = [0]

            def to_cloze(match):
                # the Anki number survives in data-cloze (overlapping clozes stay together), otherwise count
                given = re.search(r'data-cloze="(\d+)"', match.group(1))
                hint = re.search(r'data-hint="([^"]*)"', match.group(1))
                if given:
                    value = int(given.group(1))
                else:
                    number[0] += 1
                    value = number[0]
                return "{{c%d::%s%s}}" % (value, match.group(2), "::" + html.unescape(hint.group(1)) if hint else "")
            writer.writerow(["Cloze", path(node["id"]), marker.sub(to_cloze, description), name, " ".join(tags)])
            cloze += 1
        else:
            writer.writerow(["Basic", path(node["id"]), name, description, " ".join(tags)])
        count += 1

    with open(args.out, "w", encoding="utf-8", newline="") as handle:
        handle.write(out.getvalue())
    print("Exported %d notes (%d of them cloze) to %s" % (count, cloze, args.out))
    print("In Anki: File > Import, note types 'Basic' and 'Cloze'.")


# ------------------------------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    sub.required = True

    imp = sub.add_parser("import", help=".apkg/.colpkg or Anki/Quizlet text -> .kdb")
    imp.add_argument("input")
    imp.add_argument("--db", required=True, help="target .kdb (created from the template when missing)")
    imp.add_argument("--template", default=os.path.join(HERE, "empty.kdb"), help="empty template database")
    imp.add_argument("--append", action="store_true", help="import into an existing .kdb")
    imp.add_argument("--root-name", help="name of the root item (default: the main deck or the file name)")
    imp.add_argument("--relation", default="", help="label of the deck -> note relations (default: none)")
    imp.add_argument("--deck-category", default="Deck", help="category of the deck items")
    imp.add_argument("--category", choices=["deck", "notetype", "none"], default="deck",
                     help="category of the note items: top deck level (default), note type or none")
    imp.add_argument("--max-name", type=int, default=90, help="maximum length of an item name")
    imp.add_argument("--no-media", dest="media", action="store_false", help="do not embed images")
    imp.add_argument("--max-media-kb", type=int, default=1500, help="do not embed larger images")
    imp.add_argument("--description-image-edge", type=int, default=360, help="edge length of images in the description")
    imp.add_argument("--note-image-edge", type=int, default=700, help="edge length of images in the note")
    imp.add_argument("--max-children", type=int, default=25,
                     help="split decks with more notes into groups of this size (0 = never)")
    imp.add_argument("--group-label", default="Part %s · %s",
                     help="name of a group: number and start of its first card, e.g. 'Teil %%s · %%s'")
    imp.add_argument("--all-tags", action="store_true",
                     help="keep every tag (shortened to its last part) instead of only the single-level flags")
    imp.add_argument("--keep-latex", dest="convert_math", action="store_false",
                     help="do not convert formulas to the jqMath notation of the application")
    imp.add_argument("--lang", choices=sorted(MUSIC_TEXTS), default="en",
                     help="language of generated questions (melody notes)")
    imp.add_argument("--separator", help="text: separator between term and definition")
    imp.add_argument("--row-separator", help="text: separator between rows (Quizlet, e.g. ';')")

    exp = sub.add_parser("export", help=".kdb or KBB JSON -> Anki text file")
    exp.add_argument("input")
    exp.add_argument("--out", required=True)
    exp.add_argument("--deck", help="name of the topmost deck (default: the root item)")

    args = parser.parse_args()
    if args.command == "export":
        export_text(args)
    else:
        run_import(args)


if __name__ == "__main__":
    main()
