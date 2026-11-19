# English demo clips

[Watch the demos with sound](https://spearsheep.github.io/anima-brain-demos/) or
[download the MP4 files](https://github.com/spearsheep/anima-brain/releases/tag/demo-media-2026-11).
Each clip is a short excerpt from the English Anima Brain launch film, without narration
or background music. The clips demonstrate configured providers, not bundled offline models.

| Clip | Length | What it shows | MP4 |
| --- | --- | --- | --- |
| Voice messages | 6.5 seconds | A character speech sample synthesized through the Python SDK | [Download](https://github.com/spearsheep/anima-brain/releases/download/demo-media-2026-11/voice-message.mp4) |
| Audio call | 5.9 seconds | Spoken input → ASR → brain response → character speech | [Download](https://github.com/spearsheep/anima-brain/releases/download/demo-media-2026-11/audio-call.mp4) |
| Animated video call | 6.6 seconds | The S2 avatar and its speech, recorded together over RTC | [Download](https://github.com/spearsheep/anima-brain/releases/download/demo-media-2026-11/video-call.mp4) |

The presentation is 3840 × 2160 at 30 fps, H.264 video with AAC audio and fast-start
metadata. The avatar provider's 768 × 1364 stream is displayed inside that composition;
it is not a native 4K avatar render. Speech is captioned in the picture. A plain-text
transcript is also available beside each player.

## What was recorded

- **Voice sample:** “So, are we being productive tonight, or just pretending?” The
  waveform presents speech generated through the SDK, not a recording of the chat UI.
- **Audio call:** the model's actual reply to a late-night scrolling message. The
  showcase interface was re-recorded around the recorded response and speaking state;
  the clip is labeled accordingly. It is not an unedited latency benchmark.
- **Video call:** the model's actual reply to a pizza-or-cooking question. The default
  Anima Lab Rina avatar and its audio come from the same RTC recording and were cut
  together. No unrelated speech was placed over the animation.

These are fictional demonstration exchanges, not customer conversations. Rina is a
sample character; developers can supply their own character and supported voice.
The custom English voice follows the project owner's selected
[voice-line reference](https://www.youtube.com/watch?v=BsNbGhomaI8). This identifies the
source, not endorsement by its performer or channel. Reference recordings, clone IDs
and provider configuration are not distributed. The clips and character/voice assets
are demonstration media, not covered by the source code's MIT license.

## Run your own

Start with the [web showcase](../examples/showcase/README.md). For speech operations,
see [SDK media options](sdk-capabilities.md). For ASR, voice configuration, RTC and S2,
follow [live-call setup](live-calls.md).

The published clips are release attachments so cloning the code does not download
video exports. Small preview images live in `docs/assets/demos/`. The release includes
`manifest.json` with durations, dimensions, file sizes and SHA-256 checksums.
