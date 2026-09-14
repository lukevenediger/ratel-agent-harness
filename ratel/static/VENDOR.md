# Vendored assets

The board has no build step and no package manager. Anything third-party is
committed here as a single pinned file, served from `/static/`, and recorded
below so the blob's provenance stays checkable.

## mermaid.min.js

| | |
|---|---|
| version | 10.9.1 |
| upstream | https://cdnjs.cloudflare.com/ajax/libs/mermaid/10.9.1/mermaid.min.js |
| mirror | https://unpkg.com/mermaid@10.9.1/dist/mermaid.min.js (byte-identical) |
| sha256 | `61b335a46df05a7ce1c98378f60e5f3e77a7fb608a1056997e8a649304a936d6` |
| bytes | 3335717 (989837 gzipped) |
| licence | MIT |

The full MIT licence text and copyright line are reproduced in
[THIRD-PARTY-NOTICES.md](../../THIRD-PARTY-NOTICES.md).

Verify:

```bash
shasum -a 256 ratel/static/mermaid.min.js
```

Only the `.js` is committed. The board gzips it in memory on first use and
caches that for the process, keyed by mtime and size — a checked-in `.gz` would
drift the moment someone updated the `.js` and forgot to regenerate it, and the
server would then answer one URL two ways. To reproduce the bytes a client
receives:

```bash
python3 -c "import gzip,pathlib; \
  print(len(gzip.compress(pathlib.Path('ratel/static/mermaid.min.js').read_bytes(), 9)))"
```

### Why this build, and what was checked before vendoring it

- **UMD, single file.** It sets `globalThis.mermaid` and pulls in nothing else.
  Mermaid's ESM build (`mermaid.esm.min.mjs`) is a 76-byte stub that lazily
  `import()`s dozens of chunk files, every one of which would need its own
  route and its own allow-list entry.
- **Nothing the board's CSP does not already allow.** The bundle contains no
  `eval(`, no `new Function(`, no `import()`, no `import.meta`, no `blob:` URL
  and no chunk reference, so `script-src 'self' 'unsafe-inline'` stays as it is.
- **One `new Worker(`, in bundled ELK.** See the next section — this is the
  question to re-ask on every upgrade.

## The ELK / `secure` question — RE-CHECK THIS ON EVERY MERMAID UPGRADE

Diagram source is agent-authored text arriving over the bus, so a
`%%{init: {...}}%%` directive inside a fence is reachable by anything that can
post. Two separate facts keep `elk` — the one renderer in this bundle that
would construct a `Worker`, which the board's CSP has no `worker-src` for —
away from it. **They are independent, and only one of them is ours.** Measured
in a live board at 10.9.1, not inferred:

1. **`secure` is ours, and it is load-bearing.** `secure` is the list of config
   keys a directive may NOT override. Mermaid's default is `secure`,
   `securityLevel`, `startOnLoad`, `maxTextSize`, `suppressErrorRendering` — it
   does **not** include `flowchart`. Under that default, rendering
   `%%{init: {"flowchart":{"defaultRenderer":"elk"}}}%%` really does change
   `mermaid.mermaidAPI.getConfig().flowchart.defaultRenderer` to `elk`, and the
   change PERSISTS into every later diagram on the page, because it mutates the
   running config. `board.html` extends `secure` with `flowchart`, `layout` and
   `elk`; with that list the config stays `dagre-wrapper`. Listing the whole
   `flowchart` key protects the subtree, so `curve` cannot be poisoned either.
2. **No `Worker` is constructed — but not because of (1).** At 10.9.1 the
   built-in flowchart detectors return false when `defaultRenderer === "elk"`,
   and the elk flowchart is an EXTERNAL diagram (`flowchart-elk`) that only
   exists after `registerExternalDiagrams`, which this board never calls.
   Forcing `flowchart: { defaultRenderer: "elk" }` directly in `initialize()`
   constructs no Worker either.

So "no Worker appeared" is **not** evidence that the `secure` list works: it
holds under mermaid's defaults too. The config read is the evidence. A future
mermaid that bundles elk as a built-in flips fact 2 while fact 1 keeps holding
— which is exactly why the `secure` entry stays even though it looks redundant
today.

On upgrade, re-run all of it: grep the new bundle for `eval(`, `new Function(`,
`import(`, `import.meta`, `blob:` and chunk references; load it in a real board
under the real CSP and confirm the console is clean; render a fence carrying
that directive and read `getConfig().flowchart.defaultRenderer` back. Do not
enable the `elk` renderer. Showing a fence as code is a better outcome than
widening the header this board is designed around.
