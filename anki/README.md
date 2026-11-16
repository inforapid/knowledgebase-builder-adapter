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
| deck and subdecks (`A::B::C`) | chain of items of the category `Deck` below a root item |
| note | item below its deck |
| first field (front) | name of the item, as plain text and shortened to `--max-name` characters |
| `Back` / `Answer` field (otherwise the next field with content) | description of the item |
| complete front and all remaining fields | note of the item (HTML attachment), one section per field name |
| top deck level below the root | category of the item (`--category`) |
| tags | tags; hierarchical tags that only repeat the deck path are dropped, others are shortened to their last part |
| cloze `{{c1::answer::hint}}` | description with the answer highlighted — every highlight becomes a gap card; the name is the sentence with the clozes filled in |
| Image Occlusion | item with the image as its description (the masks are not carried over) |
| images | embedded as `data:` URIs, scaled down to 360 px in the description and 900 px in the note |
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

### Learning state

KnowledgeBase Builder schedules with FSRS. Cards that Anki already schedules with FSRS keep their
stability and difficulty; for SM-2 cards the interval is taken as stability and the difficulty is
derived from the ease factor. A cloze card `c2` is assigned to the gap cards of the second cloze,
for other note types the most advanced card of the note counts for the description card. The rows
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
`--append` adds the deck to one instead.

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
| `--description-image-edge N`, `--note-image-edge N` | maximum edge length of images (default 360 / 900) |
| `--keep-latex` | do not convert formulas |
| `--separator`, `--row-separator` | separators of text imports |
| `export --deck NAME` | name of the topmost deck of the export (default: the root item) |

## Notes

* Images are stored inline as `data:` URIs, so a database with many images is larger than the
  `.apkg` it came from (base64, plus a smaller copy for the description).
* A cloze inside a formula (`\(x = {{c1::2}}\)`) is left as LaTeX source, since the formula cannot
  be converted without breaking the cloze.
* The export cannot represent relations between items — Anki has none — and overlapping clozes
  come back numbered `c1`, `c2`, … in order.
* During the import the undo triggers are dropped and restored afterwards, and the undo log is
  cleared — a bulk import is not meant to be undone step by step. The database is upgraded to
  version 5 (tag tables and the learning tables).
