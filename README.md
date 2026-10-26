# evo-plugin-template

A GitHub template for an [evo-agents](https://github.com/maycuatroi1/evo-agents) plugin that a hub installs from a
signed GitHub Release and that a market lists. It holds a sample plugin that runs as it is, `lumen-hello` of the
fictional publisher `lumen`, and the CI that tests, signs and releases it.

`lumen-hello` is an isolated plugin, run by the hub in a process of its own:

- `evo-plugin.json`: the manifest (plugin-v1): id, version, publisher, `tier: isolated`, `plugin_api: 1`, a config
  field `greeting`, and what it contributes;
- `backend/main.py`: a backend in Python's standard library that speaks plugin/1 on stdin and stdout;
- a route, `GET|POST /v1/x/lumen-hello/<project>/waves`: the project's greeting and the waves its members left;
- an MCP tool, `lumen-hello__greet`: greets someone by name;
- a page, `pages/hello/index.html`, on the hub's UI kit (`/v1/plugin-ui/kit/1/evo-ui.css` and `evo-ui.js`), its words
  in English and Vietnamese under `pages/hello/i18n/`;
- `tests/test_plugin.py`: tests of the backend over plugin/1 and of the files the hub reads;
- `market-entry.json`: the plugin's entry for a market's `market.json` (plugin-market-v1).

The protocol, the page bridge and the UI kit are described in
[docs/plugins.md](https://github.com/maycuatroi1/evo-agents/blob/main/docs/plugins.md) of evo-agents; the market and
selling in [docs/market.md](https://github.com/maycuatroi1/evo-agents/blob/main/docs/market.md).

## Make your own plugin

1. Create a repository from the template and clone it:

   ```sh
   gh repo create OWNER/NAME --template maycuatroi1/evo-plugin-template --public --clone
   ```

2. Rename the id, the publisher and the signer as [docs/rename.md](docs/rename.md) says, then write the plugin.
3. Check it on your machine, with evo-ak installed (`pip install evo-ak`):

   ```sh
   python -m unittest discover -s tests -v   # the plugin's own tests
   evo-agents plugin test .                  # the conformance checks a hub relies on
   evo-agents plugin pack . --output /tmp/bundle.tar.gz
   ```

   `plugin pack .` packs every file of the directory but `.git`, `__pycache__` and `*.pyc`, so pack to a path
   outside the checkout and keep build outputs and virtual environments out of it.
4. Push `main`. The CI tests and packs on every push and pull request, and signs the bundle on `main`.
5. Release a version: raise `version` in `evo-plugin.json`, then tag it with that version and push the tag.

   ```sh
   git tag v0.1.0 && git push origin v0.1.0
   ```

   The CI fails when the tag is not `v` plus the manifest's version. For a free plugin it makes the GitHub Release
   with `ID-0.1.0.tar.gz` and its Sigstore signature `ID-0.1.0.tar.gz.sigstore.json`.

## The CI

`.github/workflows/ci.yml` installs evo-ak from the nightly tag of evo-agents and runs four jobs:

| Job | When | What |
| --- | --- | --- |
| `test` | every push and pull request | unit tests, `evo-agents plugin test .`, the market entry against plugin-market-v1, `plugin pack` |
| `sign` | `main` and tags `v*` | packs again, checks the bytes are the tested ones, signs keyless with Sigstore, checks the signature as a hub does |
| `release` | tags `v*`, free plugins | the GitHub Release of the tag with the bundle and its signature |
| `publish-seller` | tags `v*`, when the variable `EVO_SELLER_PUBLISH` is `true` | uploads the signed bundle to the seller's hub instead of a public release |

The signature names this workflow, `sigstore:OWNER/REPO/.github/workflows/ci.yml`, as the signer. Signing needs no
secret: the run's GitHub OIDC token is the only credential. Keyless signing writes the certificate to Sigstore's public
transparency log, so the repository's name, the workflow, the ref and the commit become public, even for a private
repository.

## Install it on a hub

A hub admin installs a release straight from GitHub. The first install of a publisher asks the admin to trust its
signer; `--trust` confirms it:

```sh
evo-agents hub plugin install --github OWNER/NAME@v0.1.0 --trust
```

The hub downloads the release's bundle and signature, checks that `sigstore:OWNER/NAME/.github/workflows/ci.yml`
signed them for the manifest's publisher, records the publisher as trusted, and installs the version. A project's
admin then enables it for the project (`evo-agents hub plugin enable ID --project PROJECT`), and its page shows in the
project's menu.

## List it on a market

**Free plugin.** Open a pull request on the market's index, by default
[maycuatroi1/evo-agents-market](https://github.com/maycuatroi1/evo-agents-market), that adds your `market-entry.json`
to the `plugins` of its `market.json`, as that repository's `CONTRIBUTING.md` says. Check it first:

```sh
evo-agents plugin market-check market.json --releases
```

`--releases` downloads your latest release and checks that its bundle and signature match the entry.

**Paid plugin.** The plugin is sold per hub per month through a seller's hub, such as
[agents.omelet.tech](https://agents.omelet.tech), which keeps the bundle and serves it only to a hub that holds a
licence; it makes no public release.

1. On the seller's hub, signed in with GitHub, add the listing:

   ```sh
   evo-agents hub market listing add ID --publisher PUBLISHER \
     --signer sigstore:OWNER/NAME/.github/workflows/ci.yml --usd 49
   ```

2. In the repository's settings, create the environment `seller` with the secrets `EVO_SELLER_URL` (the seller's
   hub), `EVO_SELLER_LOGIN` and `EVO_SELLER_TOKEN` (a machine token of the account that owns the listing), and set
   the repository variable `EVO_SELLER_PUBLISH` to `true`. Every tag `v*` then uploads its signed bundle to the seller
   instead of a GitHub Release.
3. Set `price` in `market-entry.json` to `{"kind": "subscription", "usd_per_hub_month": 49, "seller": "omelet"}` and
   open the pull request on the market's index.

## License

Apache-2.0, see [LICENSE](LICENSE).
