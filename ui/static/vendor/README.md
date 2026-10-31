# Vendored third-party files

The UI loads nothing from the internet at runtime. The one third-party library
is committed here, unmodified, so it works offline under a strict
Content-Security-Policy and cannot drift.

| file | what | version | license | SHA-256 |
| --- | --- | --- | --- | --- |
| `lightweight-charts.standalone.production.js` | TradingView Lightweight Charts™, standalone production build (`dist/` of the npm package) | 5.2.1 | Apache-2.0 | `e21cc5caa0226ef30bd8549c50b9ef926615f2a4ee6b4e486353477a55f598cf` |
| `LICENSE` | the package's Apache License 2.0 text | 5.2.1 | | `70c9d5382506dd184465425c08a99ad9bd6d9ac1313c252968ba0b585e5ef823` |
| `NOTICE` | the attribution notice from the upstream repository at tag `v5.2.1` | 5.2.1 | | `f76c6afab94884448f0426e30d6e9d555ca7247894cd3484e477d2f87513036e` |

Source: the npm registry tarball
`https://registry.npmjs.org/lightweight-charts/-/lightweight-charts-5.2.1.tgz`,
whose registry integrity is
`sha512-IVwoK1RLFiLPubaKIjNbtjWLnpPMqiABSrTay6whmNa8L1+19292VtHJ+BWyPUuLCwF0tcQlhEWd1CLB2a1nsQ==`
(checked 2026-11-01). `tests/test_ui.py` checks the SHA-256 values above;
`.gitattributes` here stops git from converting line endings (on Windows), so
they hold on every checkout.

Attribution, as the license and upstream README ask:

- the chart's `attributionLogo` option stays on (the default), which shows
  TradingView's logo linking to their site on every chart;
- this `NOTICE` ships next to the library, the repository's `NOTICE` names it,
  and the UI's About page shows it.

The library adds one small `<style>` element for that logo. The server's CSP
allows exactly that stylesheet by its SHA-256 hash (`LWC_LOGO_STYLE` in
`ui/serve.py`); when you upgrade the library, recompute the hash.

To upgrade: download the new tarball from the registry, check its integrity
against `npm view lightweight-charts@VERSION dist.integrity`, copy
`package/dist/lightweight-charts.standalone.production.js` and
`package/LICENSE` here, take `NOTICE` from the matching upstream tag, and
update the table above.
