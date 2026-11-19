# Third-party notices and provenance

Original Anima Brain (Companion Engine) source is distributed under the root MIT license.
Dependencies retain their own licenses; this does not relicense third-party code.

The prebuilt MCP Apps HTML assets bundle JavaScript dependencies. Their license text
is shipped at `src/companion_engine/static/voice-player-notices.txt` and included in
the wheel. `ui/build.mjs` gathers notices when rebuilding with the locked dependencies.
The same MCP Apps dependency family is used by the memory and settings cards.

Python dependencies are installed separately by pip and are not vendored here.
Model weights, benchmark corpora, provider keys, conversation databases and user media
are not distributed. Claude and DeepSeek Harness packages are not vendored either;
generated host setup files refer to a developer's own installation.

Memory/reader/emotion design and numerical reference fixtures were adapted from the
repository owner's Anima implementation. Customer data, production credentials,
hosted backend configuration and private conversation exports are not included.
Anima is not a runtime dependency or a benchmark guarantee.

The README includes Anima's app icon and two public character portraits in `docs/assets/`.
Copies appear in the optional `examples/showcase/public/portraits/` application.
These document the project's origin and link to the consumer product. They are not
preinstalled SDK characters. Anima names, marks and character artwork remain Anima brand
material, separate from the MIT license on the source code. No trademark or endorsement
rights are granted. See `docs/assets/README.md` for the exact public sources and usage terms.

The optional web example installs React, Motion, Radix UI and Lucide via npm; it does
not vendor their source into the Python wheel. Its locally served Inter and Noto Serif JP
fonts use the SIL Open Font License. Package licenses remain in the locked dependencies.
The example build emits `dist/dependency-notices.md` alongside its assets. Font licenses
are copied to `public/portraits/` so redistributing the build retains those notices.


## Optional live-call frontend

The optional showcase uses `@volcengine/rtc` (BSD-3-Clause) for managed video rooms.
Vite includes the full dependency notices in the built `dependency-notices.md` file.
Doubao and Vidu are separately configured external services; their accounts and service
terms are separate from this repository’s MIT code license. No provider credentials are bundled.
