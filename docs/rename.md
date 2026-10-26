# Renaming the template's plugin

A repository made from this template still carries the sample plugin `lumen-hello` of the fictional publisher
`lumen`, signed by the template's own workflow. Rename three things before the first release.

| What | Template value | Becomes | Where |
| --- | --- | --- | --- |
| Plugin id | `lumen-hello` | your id: `^[a-z][a-z0-9-]{0,62}$`, unique on the market | `evo-plugin.json` (`id`), `market-entry.json` (`id`), log lines in `backend/main.py` |
| Publisher | `lumen` (name `Lumen Labs`) | your publisher id, the same for every plugin you publish | `evo-plugin.json` (`publisher`), `market-entry.json` (`publisher.id`, `publisher.name`) |
| Signer | `sigstore:maycuatroi1/evo-plugin-template/.github/workflows/ci.yml` | `sigstore:OWNER/REPO/.github/workflows/ci.yml` of your repository | `market-entry.json` (`publisher.signer`, `repo`, `homepage`) |

The manifest names no signer: a hub reads it from the market's entry, or takes the repository's
`.github/workflows/ci.yml` by default when an admin installs with `--github OWNER/REPO`. Keep the workflow's file name
`ci.yml`, or every hub that trusts you has to be told the new one.

Then make the plugin yours:

- `evo-plugin.json`: `name`, `description`, `version` (start at `0.1.0`), `permissions` (ask for no more than the
  plugin needs; a project's admin grants them), `config` and `contributes`.
- `backend/main.py`: the routes and tools the manifest contributes. `initialize` lists the manifest's tools, so a tool
  needs only its handler here.
- `pages/`: one directory per page, its words in `i18n/en.js` and `i18n/vi.js` beside it.
- `tests/test_plugin.py`: the tests of what you changed.
- `market-entry.json`: `name`, `description` (280 characters at most), `industries`, `categories`, `icon`, `price`,
  `license`.
- `README.md`: what the plugin does, for its users.

## Find what is left

```sh
git grep -n -i -e lumen -e evo-plugin-template -- ':!docs/rename.md' ':!README.md' ':!LICENSE'
```

On a fresh copy, this renames the three in one go (GNU sed; set the three values first):

```sh
ID=northwind-ledger PUBLISHER=northwind REPO=northwind/northwind-ledger
git grep -l -e lumen-hello -e evo-plugin-template -e '"lumen"' -- evo-plugin.json market-entry.json backend pages \
  | xargs sed -i -e "s#maycuatroi1/evo-plugin-template#$REPO#g" -e "s#lumen-hello#$ID#g" -e "s#\"lumen\"#\"$PUBLISHER\"#g"
```

Then change `Lumen Labs` and the words of the pages by hand, and run the checks of the README.
