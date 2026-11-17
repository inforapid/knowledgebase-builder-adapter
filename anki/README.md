# Anki / Quizlet → InfoRapid KnowledgeBase Builder

`anki_to_kbb.py` imports [Anki](https://apps.ankiweb.net/) decks (`.apkg`, `.colpkg`) and Anki or
[Quizlet](https://quizlet.com/) text exports into an InfoRapid KnowledgeBase Builder database
(`.kdb`). The deck hierarchy becomes a tree of items, every note becomes an item with its answer as
the description and all remaining fields in its note, and cloze deletions become highlights the
learning mode of the KnowledgeBase Builder turns into gap cards. The learning state of the cards is
carried over.

It also works the other way round: `export` writes a `.kdb` as an Anki text file that Anki imports
with *File › Import*.

The importer writes into the SQLite file directly — no running server and no web interface are
needed. Packages of several hundred megabytes are fine: the collection is extracted into a
temporary file and the media are read from the package one by one. The resulting `.kdb` can be
opened with the desktop and mobile apps or uploaded to the web version (*Upload Knowledge Base*).

## What is mapped

| Anki | KnowledgeBase Builder |
| --- | --- |
| deck and subdecks (`A::B::C`) | chain of items of the category `Deck` below a root item; decks, groups and the root carry the tag `#nolearn`, so the learning mode asks nothing about the containers themselves |
| note | item below its deck; decks with more than `--max-children` (25) notes are split into groups of 25 in Anki order, and into groups of groups when needed – the diagram loads about 100 items at once. A group is named after its number and the start of its first card (`--group-label`, e.g. `"Teil %s · %s"` → *Teil 3 · Glykolyse …*) |
| first field (front) | name of the item, as plain text and shortened to `--max-name` characters |
| `Back` / `Answer` field (otherwise the next field with content) | description of the item |
| complete front and all remaining fields | note of the item (HTML attachment), one section per field name |
| top deck level below the root | category of the item (`--category`) |
| tags | only single-level flags such as `!Physikum` or `High-Yield` (below a prefix shared by most tags, used by at least 0.5 % of the notes), without leading `#`/`!`/`$`/`§` – `#` marks special tags in KBB. Nested curricula and tags that repeat the deck path are dropped; `--all-tags` keeps every tag shortened to its last part |
| note type with one card per field (templates named after fields, e.g. *Ursprung*, *Ansatz*, *Funktion*, *Innervation*) | the field before the asked fields becomes the item (*M. supraspinatus*), every asked field a labelled relation to a shared value item (*Innervation → N. suprascapularis*, shared by all muscles with that nerve) – relation cards instead of one card per field; the description lists all fields |
| cloze `{{c1::answer::hint}}` | description with the answer highlighted (`data-cloze`, `data-hint`) — one gap card per cloze number; the name is the sentence with the clozes filled in |
| Image Occlusion (any note type with *occlusion* in its name) | item named after the header/title field with the image as its description; the masks are not carried over, and since the description has no text the learning mode asks no card for it |
| images | embedded as `data:` URIs, scaled down to 360 px in the description and 700 px in the note, stored as JPEG (quality 80) whenever that is smaller and the image has no transparency |
| formulas `\(...\)` / `\[...\]` | converted to the jqMath notation the application renders (see below) |
| `[sound:...]` | dropped |
| learning state (FSRS, or SM-2 approximated) | table `LearnProgress` |
| review history (`revlog`) | table `ReviewLog`, so the learning statistics start with the Anki history |

### Formulas

Anki renders formulas with MathJax, the KnowledgeBase Builder with jqMath. jqMath does not know
LaTeX command names — `\vec`, `\frac` or `\times` would show up as words — so the importer
converts them: symbols to Unicode (`\partial` → ∂, `\times` → ×, Greek letters, arrows, integrals),
`\frac{a}{b}` to `{a}/{b}`, `\sqrt{x}` to `√{x}`, `\vec a` to a⃗, matrices and `aligned`/`cases`
environments to `\table`, `\text{...}` and functions like `\sin` to upright text. Colours, sizes and
`\left`/`\right` are dropped. `--keep-latex` leaves the formulas untouched.

### How the cards are asked

The learning mode of the KnowledgeBase Builder treats an item as a term with an explanation: it
first shows both, then asks with multiple choice in both directions, and switches to flashcards
once a card is consolidated. Imported Anki notes are asked the way Anki asks them instead:

| Note | Card in KnowledgeBase Builder |
| --- | --- |
| Basic (front is a question or a sentence) | one card front → back, as a flashcard from the start: think of the answer, reveal, rate *Again / Hard / Good / Easy*. No reverse direction, no multiple choice. After revealing, *Notes* shows the remaining fields (Back Extra, …). |
| Cloze | one card per cloze number. All parts of `c1` are hidden together, the other clozes are plain text. The back shows only the hidden parts — the sentence is already
in the question; when a cloze has several parts, they are numbered in the question and the back
lists one line per part with its number. The Anki hint is not part of the text, it appears as a
tooltip on the placeholder. Answers that are a formula, several parts or four words and more are flashcards; single words are asked with multiple choice among other single-word clozes, and can be typed once consolidated. |
| the note item itself (cloze) | not asked — its name and description are the same sentence |

These rules are part of the learning mode, not of the importer: they apply to every item whose
name is a question or a sentence and to every highlight with `data-cloze`.

### Learning state

KnowledgeBase Builder schedules with FSRS. Cards that Anki already schedules with FSRS keep their
stability and difficulty; for SM-2 cards the interval is taken as stability and the difficulty is
derived from the ease factor. A cloze card `c2` is assigned to the gap card of cloze number 2, for
other note types the most advanced card of the note counts for the front → back card. The rows
are written for the user `_default_user_`, which is the one the web application and the apps use.

## Requirements

Python 3.6 or newer, standard library only. The importer uses the database layer of the Obsidian
adapter: keep `anki/` and [`obsidian/`](../obsidian/) side by side, or copy `obsidian_to_kbb.py`
next to `anki_to_kbb.py`. The empty template database `empty.kdb` is part of this directory.

Optional packages:

```bash
pip install pillow zstandard
```

| Package | Without it |
| --- | --- |
| `pillow` | images are embedded at their original size; images above `--max-media-kb` are left out |
| `zstandard` | packages in the new format of Anki 2.1.50 and later (`collection.anki21b`) cannot be read. Such packages usually still contain the older `collection.anki21`, which is used instead; when they only contain the placeholder note *Please update Anki*, the importer says so |

## Usage

```bash
python anki_to_kbb.py import Deck.apkg --db deck.kdb
```

`deck.kdb` is created from `empty.kdb`. The importer refuses to overwrite an existing database;
`--append` adds the deck to one instead. A new database opens with the layout *Mind Map radial
(straight lines)* and collapsed items, so that the answers stay hidden until an item is opened, and it
is marked as an Anki deck (`"lcp": "anki"` in the diagram properties): the learning mode then starts
without category and parts cards – the category is only the top deck, the parts are decks and groups.
Both can still be switched on in the learning dialog. `--append` leaves the database properties as they are.

Text exports — Anki's *Notes in Plain Text* with its `#separator:` / `#deck column:` header lines,
or Quizlet's *Export* with one `term<TAB>definition` per line:

```bash
python anki_to_kbb.py import quizlet.txt --db vocabulary.kdb
python anki_to_kbb.py import quizlet.txt --db vocabulary.kdb --separator "," --row-separator ";"
```

Back to Anki:

```bash
python anki_to_kbb.py export deck.kdb --out deck.txt
```

Every item with a description becomes a `Basic` note (name / description), every item with
highlights a `Cloze` note. The deck is the path of the parent items from the root; tags stay tags.
Instead of a `.kdb`, a JSON file written by *Export JSON data* can be exported as well.

## Options

`--help` lists them all.

| Option | Meaning |
| --- | --- |
| `--template FILE` | empty database, copied when `--db` does not exist yet (default `empty.kdb` next to the script) |
| `--append` | import into an existing database |
| `--root-name NAME` | name of the root item (default: the main deck, otherwise the file name) |
| `--category deck\|notetype\|none` | category of the note items (default: the top deck level) |
| `--deck-category NAME` | category of the deck items (default `Deck`) |
| `--relation LABEL` | label of the deck → note relations (default: none) |
| `--max-name N` | maximum length of an item name (default 90) |
| `--no-media` | leave images out |
| `--max-media-kb N` | leave out images that are still larger after scaling (default 1500) |
| `--description-image-edge N`, `--note-image-edge N` | maximum edge length of images (default 360 / 700) |
| `--all-tags` | keep every tag (shortened to its last part) instead of only the single-level flags |
| `--max-children N`, `--group-label TEXT` | group size for large decks (default 25, 0 = no groups) and the group name with two `%s` for number and first card (default `"Part %s · %s"`) |
| `--keep-latex` | do not convert formulas |
| `--separator`, `--row-separator` | separators of text imports |
| `export --deck NAME` | name of the topmost deck of the export (default: the root item) |

The learning state is deliberately not turned into tags or categories (Leitner boxes): it changes with every
review and per card. The app shows it live instead – the *knowledge map* colours every item by its state.
As in Anki, the learning mode reviews due cards of the whole database, while new cards come from the
selected branch in deck order (*New cards from: Branch of …*) – select a deck or a group and learn.

## Notes

* Images are stored inline as `data:` URIs, once per note that uses them, so a database with many
  images can be larger than the `.apkg` it came from. Example: AMBOSS feat. Ankiphil Vorklinik (7541
  notes, 1266 images used in 5001 places, 140 MB package) becomes 8435 items (318 of them groups) and 234 MB in about 50 s.
  The web version allows 550 MB per user in total; `--note-image-edge`/`--description-image-edge` or
  `--no-media` make the database smaller.
* A cloze inside a formula (`\(x = {{c1::2}}\)`) is left as LaTeX source, since the formula cannot
  be converted without breaking the cloze.
* The export cannot represent relations between items — Anki has none. Cloze numbers and hints
  survive the round trip through `data-cloze`/`data-hint`; highlights set by hand are numbered in
  order. Formulas are exported in the converted jqMath notation, which MathJax in Anki does not
  render.
* The learning-mode rules above need the KnowledgeBase Builder version of 2026-11 or later.
* During the import the undo triggers are dropped and restored afterwards, and the undo log is
  cleared — a bulk import is not meant to be undone step by step. The database is upgraded to
  version 5 (tag tables and the learning tables).
