# evo-plugin-omniroute

An [evo-agents](https://github.com/maycuatroi1/evo-agents) hub plugin that connects a project to an
[OmniRoute](https://github.com/cheaper-inference/OmniRoute) AI gateway you run: one OpenAI-compatible endpoint in front
of many model providers, with combos such as `auto` that pick a model per request.

Once a project enables it:

- **Agents** of the project get three MCP tools on the hub's `/mcp`:
  - `omniroute__models` lists the gateway's models and combos, filtered by a word or a capability;
  - `omniroute__ask` sends one question to a model and answers its reply with the model that answered and the tokens
    it used, for a second opinion, a translation or a cheap summary;
  - `omniroute__usage` says what the key spent today and this week against its limits.
- **Members** get the page *OmniRoute* in the project's menu: the gateway's state, latency and model count, the key's
  usage, a searchable table of models and a box to ask one of them.
- **The CLI** gets `evo-agents hub omniroute status|models|ask --project <project>`.

The plugin is isolated: the hub runs its backend in a process of its own, and the backend is the only part that
holds the key. The page and the CLI reach the gateway through the plugin's routes, never with the key.

## Set it up

1. In OmniRoute's dashboard, open **API Manager** and create a key for the hub. Turn on its **usage command** if the
   plugin should show usage. A key with spending limits keeps a project's agents within a budget.
2. A hub admin installs the plugin from the Market, or from a release:

   ```sh
   evo-agents hub plugin install --github maycuatroi1/evo-plugin-omniroute@v0.1.1 --trust
   ```

3. Set the gateway, once for the whole hub (a hub admin) or per project (a project admin), and enable it:

   ```sh
   evo-agents hub plugin config omniroute --instance --set base_url=https://omniroute.example.com
   printf %s "$OMNIROUTE_API_KEY" | evo-agents hub plugin config omniroute --instance --secret api_key
   evo-agents hub plugin enable omniroute --project northwind
   evo-agents hub omniroute status --project northwind
   ```

A project's layer overrides the hub's field by field, so one project can point at another gateway or another key.

| Field | Default | What |
| --- | --- | --- |
| `base_url` | | The gateway's root, `https://...`, without `/v1` |
| `api_key` | | The OmniRoute key, sealed by the hub and never shown again |
| `default_model` | `auto` | The model a question goes to when it names none |
| `max_tokens` | `1024` | The most tokens one answer may take |
| `ask_role` | `writer` | The lowest project role that may ask from the page and the CLI |

The hub gives a plugin call 30 seconds, so a question must be answered within 25: pick fast models or combos for
`omniroute__ask`, and leave long work to the project's runs.

## Security

- The backend reaches only `https` URLs whose host resolves to public addresses, checked at every call and pinned for
  the connection, so a project's config cannot point it at the hub's own network. It follows no redirect.
- The key goes only in the `Authorization` header to the gateway; no answer, log line or error carries it.
- The plugin asks for no permission of the hub: it reads no run, plan, chat or memory.
- Everything a question holds goes to your gateway and from there to the provider of the model it picks.

## Develop

Standard library only, Python 3.12, with evo-ak installed (`pip install evo-ak`):

```sh
python -m unittest discover -s tests -v   # the backend against a fake OmniRoute, and the files the hub reads
evo-agents plugin test .                  # the plugin/1 conformance checks a hub relies on
evo-agents plugin pack . --output /tmp/omniroute.tar.gz
```

The CI (`.github/workflows/ci.yml`) runs the same on every push, signs the bundle keyless with Sigstore on `main`, and
on a tag `vX.Y.Z` that matches `version` in `evo-plugin.json` makes the GitHub Release a hub installs from.

## License

Apache-2.0, see [LICENSE](LICENSE). OmniRoute is a separate project under its own license.
