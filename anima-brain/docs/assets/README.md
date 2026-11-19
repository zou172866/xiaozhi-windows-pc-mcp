# Anima Lab reference assets

These are Anima Lab's public brand and character assets, included at the project owner's
request to identify the origin of Anima Brain and link to the consumer apps.
They are documentation assets, also copied into the optional `examples/showcase/public/portraits/`
frontend. The Python package does not load or bundle them at runtime.

| Local file | Original source |
| --- | --- |
| `companion-studio.png` | Screenshot of the local optional showcase, using synthetic offline preview data and the public portraits below |
| `anima-icon.png` | [Anima website icon](https://anima-lab.io/icons/icon-512.png), copied unchanged from the website's public icon |
| `rina.jpg` | [Rina aquarium image](https://makima-io-assets.s3.us-east-1.amazonaws.com/assets/landing/2026-10/rina-aquarium-v1.jpg) used on the public Anima landing page |
| `sean.jpg` | [Sean portrait](https://makima-io-assets.s3.us-east-1.amazonaws.com/assets/landing/2026-10/sean-portrait-v1.jpg) used on the public Anima landing page |

The character images are copied unchanged. They depict the Anima consumer product;
including them here does not imply feature parity with the SDK. The optional showcase
offers lightweight starter personas using these portraits; those are not production
Anima character definitions.
No customer conversations, app databases or private screenshots are included.

Anima names, marks and character artwork remain Anima Lab brand materials and are not
covered by the source code's MIT license. Their inclusion for project identification
does not grant trademark rights or permission to imply endorsement of derivative products.

Official product links:

- [Anima Lab](https://anima-lab.io)
- [iPhone / iPad App Store listing](https://apps.apple.com/app/id6776954266)
- [Android direct APK download page](https://anima-lab.io/download)

Links checked on 2026-11-11. Use the Android download page rather than a version-pinned
APK URL so the README continues to point to the current release.


`brain-flow.json` owns the interactive graph geometry, connections and English explanations (MIT).
`scripts/render_brain_diagram.py` reflows its overview labels into a larger-type SVG for
README reading. The SVG intentionally has a separate presentation layout.
`brain-flow.zh-CN.json` supplies Chinese text for the same view/node IDs, steps and UI.
It contains no separate geometry or implementation of the formulas. Keep its `version`,
IDs and step order aligned with the graph when editing; the architecture browser checks
verify that alignment, label fit and formula behavior in both languages.

| Language | Static overview | Four-view animation |
| --- | --- | --- |
| English | `brain-flow.svg` | `brain-flow.gif` |
| 简体中文 | `brain-flow.zh-CN.svg` | `brain-flow.zh-CN.gif` |

Each animation captures all four Motion-powered Brain Atlas chapters over 24 seconds.
These assets contain illustrative architecture, no user conversation or credentials.
Both READMEs embed the SVG in the appropriate language and link to the GIF separately.
The interactive page has a language switch. The November 2026 GIFs were refreshed after
the contextual-retrieval and knowledge-update changes.
The interactive source is `examples/showcase/src/Architecture.tsx`.

Regenerate the static diagrams and animations with:

```bash
python scripts/render_brain_diagram.py
python scripts/render_brain_diagram.py --lang zh-CN
# Start the showcase first. Requires ffmpeg and showcase npm dependencies.
node examples/showcase/scripts/render-architecture.mjs http://127.0.0.1:8787
node examples/showcase/scripts/render-architecture.mjs http://127.0.0.1:8787 --lang zh-CN
```

Use a system with Chinese fonts installed when rendering the Chinese GIF. SVGs use
system font fallbacks (PingFang SC, Microsoft YaHei, Noto Sans CJK SC); no external
font requests are required. Rendered frames stay in ignored `output/brain-animation-<language>/`.

## English demo previews

`demos/voice-message.jpg`, `demos/audio-call.jpg` and `demos/video-call.jpg` are
frames from the owner's English launch demonstration. Only these small posters are
tracked in Git; the three MP4 clips are distributed as [release assets](https://github.com/spearsheep/anima-brain/releases/tag/demo-media-2026-11).
See [demo provenance and limitations](../demos.md). No source voice recordings, clone
identifiers, API keys, private databases or local configuration are included.
