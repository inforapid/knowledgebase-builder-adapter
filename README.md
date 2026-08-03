# knowledgebase-builder-adapter

Adapters that convert data from other tools into an
[InfoRapid KnowledgeBase Builder](https://www.inforapid.org/) database (`.kdb`).

Each adapter lives in its own directory, named after the source it reads from, and can be used on
its own — there is nothing to install for the repository as a whole.

## Adapters

| Directory | Source | Description |
| --- | --- | --- |
| [`obsidian/`](obsidian/) | Obsidian vault | Markdown files become items, `[[wikilinks]]` become relations, folders become categories, `#tags` become tags. Supports incremental re-import. |

## How the adapters work

A KnowledgeBase Builder database is a SQLite file. The adapters write into it directly, so no
running server and no web interface are needed — the resulting `.kdb` can be opened with the
desktop, mobile and web versions of the application alike.

Two conventions are shared by all adapters:

* **Template database.** A new database is created by copying an empty template (`empty.kdb`),
  which is included with each adapter.
* **Stable URIs.** Every generated item and relation carries a URI derived from its source object,
  for example `obsidian:note:Projects/Notes.md`. A second run finds the existing records again
  through those URIs and only updates what has actually changed, so formatting applied by hand in
  the application — positions, colours, item properties — survives a re-import.

## Requirements

Python 3.6 or newer. The adapters get by with the standard library; optional packages that improve
the result are listed in the README of each adapter.

## Usage

See the README in the adapter's directory. For Obsidian, from inside `obsidian/`:

```bash
python obsidian_to_kbb.py --vault ~/Obsidian/MyVault --db myvault.kdb
```

Make a copy of the database before running an adapter against an existing knowledge base.

## License

MIT — see [LICENSE](LICENSE).
