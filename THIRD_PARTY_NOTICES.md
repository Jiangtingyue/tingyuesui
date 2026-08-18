# Third-party notices

## dwell-on-something front-end

JTYHome's optional reading interface includes adapted layout and presentation
rules from the `web/index.html` front-end in
[xinwithyu/dwell-on-something](https://github.com/xinwithyu/dwell-on-something).

Required Notice: Copyright 2026 xinwithyu (https://github.com/xinwithyu)

The adapted portions are used under the
[PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/).
They may only be used for permitted noncommercial purposes. JTYHome keeps the
upstream notice and license URL with every packaged copy.

## Ocean Listen / 听海

JTYHome vendors the Ocean Listen source at commit
`928dfba62a2c074ccb0154f7ddd42743e4ce9e75` and wraps it with an isolated
macOS runtime, a sequential local worker, bounded model reports, durable status
and explicit privacy/delivery receipts. The vendored upstream `LICENSE`,
`NOTICES` and README remain in `vendor/ocean-listen/`.

Ocean Listen is distributed under the MIT License, copyright (c) 2026
migratorywhale. Its own notices credit MIT-licensed components from
whale-listen, Tinggu 听骨 by SeithAsync, and eryu by
sebastianevan200-stack. The upstream source and lineage are documented at
[ennisaaaaaaaa-stack/ocean-listen](https://github.com/ennisaaaaaaaa-stack/ocean-listen).

## cc-cache-warmer state machine reference

JTYHome v8.4 adapts the per-session cache-warming state machine, consecutive
unanswered-warm cap, lifetime fuse, activity reset and self-loop safety ideas
from `dissipative-system/cc-cache-warmer`. The upstream MIT license is bundled
under `third_party/cache-reference/cc-cache-warmer/`; the release package omits
the non-runtime `vigil` reference executable. The JTYHome transport adapter uses APScheduler and
OpenRouter/Anthropic wire snapshots instead of Claude Code/systemd.

Copyright (c) 2026 dissipative-structure. Used under the MIT License.

## Eventide state-machine reference/adaptation
Required Notice: Copyright 2026 Chuli (@chuli1122)
Upstream: https://github.com/chuli1122/Eventide
License: PolyForm Noncommercial License 1.0.0 — https://polyformproject.org/licenses/noncommercial/1.0.0/
JTYHome 8.6 adapts the documented state-machine structure/rules for the user's personal noncommercial JTYHome runtime; upstream code is not vendored as a Python dependency.

## archisvaze/liquid-glass

JTYHome 8.8 adapts both rendering paths from `archisvaze/liquid-glass`: the SVG optical/refraction pipeline from `index.html` (surface/refraction profile, displacement map, specular map, SVG `feDisplacementMap`) and the rounded-rectangle WebGL refraction model from `webgl.html` (IOR/thickness/bezel refraction, local glass blur, rim/specular and tint). The Home hero uses WebGL with the SVG engine as fallback; the four Home portals use the SVG engine.

Source: https://github.com/archisvaze/liquid-glass

## React 16 runtime (Hydrangea Water Hero integration)

The hydrangea water hero includes local production UMD copies of React 16.0.0 and ReactDOM 16.0.1 so the private local app does not depend on a third-party CDN at runtime. React is distributed under the MIT License; the vendored files retain their upstream copyright/license headers.
