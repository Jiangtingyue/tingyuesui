# JTYHome 8.8 — GitHub Glass Fusion notices

This file preserves attribution for an earlier v8.8 experimental glass path. The front-end repair disables and removes that experimental runtime path from the shipped page because it conflicted with the established `liquid-glass-webgl.js` renderer. The license notices remain bundled for provenance.

## Upstream projects used

- `Jakubantalik/metal-fx` — MIT. Used for the light silver preset vocabulary and flowing plasma field. Copyright (c) 2026 Jakub Antalik.
- `seangeng/argent` — MIT. Used for the native silver liquid-metal banding, simplex-noise flow, curvature warp and chromatic dispersion parameters. Copyright (c) 2026 Sean Geng.
- `rdev/liquid-glass-react` — MIT. Used for the clean-center / edge-only displacement-aberration structure and pointer elasticity defaults. Copyright 2025 Max Rovensky.
- `Z1Code/glass-refraction` — MIT. Used for asymmetric chromatic edge tinting and breathing specular treatment. Copyright (c) 2025 Moeez Shabbir.

## Runtime status

The experimental `github-glass-fusion.js` / `archisvaze-liquid-glass-webgl.js` files are no longer loaded or shipped by this repaired package. JTYHome now uses `static/js/liquid-glass-webgl.js` as the single active glass renderer.

## License

The upstream projects above are MIT-licensed. Their MIT permission notices require preserving copyright and permission notices in substantial copies. This file records the integration notes and attribution. Exact upstream license texts are bundled under `THIRD_PARTY_LICENSES/`.
