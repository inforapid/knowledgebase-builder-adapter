# knowledgebase-builder-adapter

Adapters that convert data from other tools into an
[InfoRapid KnowledgeBase Builder](https://www.inforapid.org/) database (`.kdb`).

Each adapter lives in its own directory, named after the source it reads from — there is nothing to
install for the repository as a whole. The Anki adapter reuses the database layer of the Obsidian
adapter, so keep the two directories side by side.

## Adapters

| Directory | Source | Description |
| --- | --- | --- |
| [`obsidian/`](obsidian/) | Obsidian vault | Markdown files become items, `[[wikilinks]]` become relations, folders become categories, `#tags` become tags. Supports incremental re-import. |
| [`anki/`](anki/) | Anki decks (`.apkg`, `.colpkg`), Anki and Quizlet text exports | Decks become a tree of items, notes become items with the answer as description, clozes become gap cards of the learning mode, the learning state is carried over. Also exports a database back to Anki. |

## How the adapters work

A KnowledgeBase Builder database is a SQLite file. The adapters write into it directly, so no
running server and no web interface are needed — the resulting `.kdb` can be opened with the
desktop, mobile and web versions of the application alike.

Two conventions are shared by all adapters:

* **Template database.** A new database is created by copying an empty template (`empty.kdb`),
  which is included with each adapter.
* **URIs.** Every generated item carries a URI that names its source, for example
  `obsidian:note:Projects/Notes.md` or `anki:…`. The Obsidian adapter uses them to find the existing
  records again on a second run and only updates what has actually changed, so formatting applied
  by hand in the application — positions, colours, item properties — survives a re-import. The
  Anki adapter creates a new database per run (or adds to one with `--append`).

## Requirements

Python 3.6 or newer. The adapters get by with the standard library; optional packages that improve
the result are listed in the README of each adapter.

## Usage

See the README in the adapter's directory. For Obsidian, from inside `obsidian/`:

```bash
python obsidian_to_kbb.py --vault ~/Obsidian/MyVault --db myvault.kdb
```

For Anki, from inside `anki/`:

```bash
python anki_to_kbb.py import MyDeck.apkg --db mydeck.kdb
```

Make a copy of the database before running an adapter against an existing knowledge base.

## License

MIT — see [LICENSE](LICENSE).
