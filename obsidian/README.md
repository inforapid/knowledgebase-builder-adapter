# Obsidian → InfoRapid KnowledgeBase Builder

`obsidian_to_kbb.py` imports an [Obsidian](https://obsidian.md/) vault into an InfoRapid
KnowledgeBase Builder database (`.kdb`). Every Markdown file becomes an item of the knowledge
base, the note text is stored as an HTML attachment, and `[[wikilinks]]`, folders and tags become
relations, categories and tags of the diagram.

The importer writes into the SQLite file directly — no running server and no web interface are
needed. The resulting `.kdb` can be opened with the desktop, mobile and web versions of the
KnowledgeBase Builder alike.

## What is mapped

| Obsidian | KnowledgeBase Builder |
| --- | --- |
| Markdown file | item, with the note text as an HTML attachment |
| folder | item of the category `Folder`, plus a `contains` relation |
| parent folder | category of the item |
| `#tag` and frontmatter `tags:` | tags |
| `[[wikilink]]` | `links to` relation, plus a clickable `itemid://` link inside the note that loads the target item as the new root of the diagram |
| `![[embed]]` | `embeds` relation; `![[note#heading]]` and `![[note#^block]]` are resolved inline (up to three levels deep) |
| `field:: [[target]]` (Dataview) | relation labelled with the field name |
| frontmatter `title:`, first `# heading`, first alias | name of the item |
| frontmatter `aliases:` | additional names for link resolution |
| frontmatter `description:` / `summary:`, otherwise the first paragraph of running text | description of the item |
| first external URL | `DirectLinkURL` of the item |
| images in the text | embedded into the note as a `data:` URI; the first one is additionally attached as the item icon |
| ` ```mermaid ` block | rendered to SVG and embedded when mermaid-cli is available, otherwise left as source code |
| `$formula$` / `$$formula$$` | MathML, rendered by the browser itself (needs `latex2mathml`) |
| `==highlight==`, `~~strikethrough~~`, `> [!callout]`, `%%comment%%` | converted to HTML, comments are dropped |

Emoji at the start of a note name are moved into their own cell of the node label, so they are
displayed larger than the text beside them — the same structure the application produces itself.

### Index pages

Vaults such as the Obsidian Hub contain map-of-content notes that link to hundreds of other
notes. Turning all of those links into relations would bury the actual structure of the vault, so
notes with many links and little text are detected and their links are left out. The threshold
adapts to the vault (at least four times the median number of links) and can be tuned with
`--index-page-links`, `--index-page-words` or switched off with `--keep-index-page-links`.

## Requirements

Python 3.6 or newer — the standard library alone is enough for a basic import. The empty template
database the importer copies when the target does not exist yet, `empty.kdb`, is part of this
directory.

Optional packages improve the result and are used automatically when they are installed:

```bash
pip install markdown pyyaml pillow latex2mathml
```

| Package | Without it |
| --- | --- |
| `markdown` | a frugal built-in converter is used, which covers only the common cases |
| `pyyaml` | frontmatter is parsed by a minimal fallback parser (`key: value` and `-` lists) |
| `pillow` | images are embedded at their original size instead of being scaled down |
| `latex2mathml` | LaTeX formulas stay source code |

Mermaid diagrams additionally need [mermaid-cli](https://github.com/mermaid-js/mermaid-cli)
(`mmdc`). If it is not installed, the code block simply stays source code and nothing is
downloaded. `--install-mermaid` fetches it temporarily through `npx` (needs Node.js and, the first
time, a few hundred megabytes for the headless Chromium).

## Usage

```bash
python obsidian_to_kbb.py --vault ~/Obsidian/MyVault --db myvault.kdb
```

`myvault.kdb` is created from `empty.kdb` on the first run. `--template` is only needed when the
importer is called from another directory or another template is to be used.

Run it again after editing the vault to bring the database up to date:

```bash
python obsidian_to_kbb.py --vault ~/Obsidian/MyVault --db myvault.kdb --prune
```

`--prune` also removes the items whose Markdown file no longer exists. Without it, deleted notes
stay in the database.

## Incremental import

The importer creates the table `ObsidianSync` in the database and remembers a content hash per
file. On the next run only changed files are rewritten; `--force` rewrites everything. Items and
relations are found again through stable URIs:

```
obsidian:note:<path>              item of a note
obsidian:folder:<path>            item of a folder
obsidian:rel:<from>|<to>|<kind>   relation
```

Because of that, formatting applied by hand survives a re-import: positions, colours and all other
item properties are never touched, only name, description, URL and the note text are updated.

Notes that embed a changed note are rewritten as well, so their inline transclusions stay in sync.

## Options

Only the ones worth knowing about; `--help` lists them all.

| Option | Meaning |
| --- | --- |
| `--template FILE` | empty database, copied when `--db` does not exist yet (default `empty.kdb` in the current directory) |
| `--category folder\|fixed\|none` | where the category of an item comes from (default: the folder name) |
| `--category-name NAME` | category for `--category fixed` and for notes in the root folder |
| `--no-folder-nodes` | do not create an item per folder |
| `--root-name NAME` | name of the topmost folder item (default `Vault`) |
| `--no-images` | leave images out entirely |
| `--no-mermaid`, `--install-mermaid`, `--mermaid-cli PATH` | control the Mermaid rendering |
| `--link-relation`, `--embed-relation`, `--folder-relation` | labels of the generated relations |
| `--no-dataview-relations` | treat `field:: [[target]]` like an ordinary wikilink |
| `--pipe-as-relation-label` | use the text behind the `\|` of `[[target\|text]]` as the relation label |
| `--no-format-diagram` | leave the formatting of the diagram as it is |
| `--prune` | remove items whose Markdown file no longer exists |
| `--force` | rewrite every note, including unchanged ones |
| `--keep-undo` | leave the undo triggers active during the import (slower, larger file) |

Unless `--no-format-diagram` is given, the diagram is formatted on the first run: radial mindmap
layout, colouring by category, the *Spectral* colour scheme, a cross-stitch background and
transparent items.

## Notes

* The database is upgraded to version 4 if needed: the tag tables and their undo triggers are
  created when they are missing.
* During the import the undo triggers are dropped and restored afterwards, and the undo log is
  cleared — a bulk import is not meant to be undone step by step. `--keep-undo` prevents this.
* Old blobs left behind by an update are removed by *shrink database* in the application.
* Make a copy of the database before the first run against an existing knowledge base.
