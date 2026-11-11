# Video Agent Studio

**English** · [Tiếng Việt](README.vi.md)

A local web app that turns an idea or a script into a short Vietnamese video. Several models split the work: Claude Opus writes, plans and edits, Higgsfield generates the footage, Vbee reads the voiceover, and Pixabay supplies real stock clips.

It has two modes:

- **Storyboard film.** Opus breaks the idea into a shot list, Haiku/Sonnet write a prompt for each shot in parallel, Higgsfield quotes a price and renders once you approve, and Opus reviews three frames per shot to score it and order a retake if needed.
- **Talking presenter.** One portrait photo plus a script becomes a presenter video intercut with illustration scenes. It costs a single 5-second AI clip (or nothing, with a slow zoom instead).

## What the presenter mode does

1. Opus writes the script (or uses yours), and Vbee reads it in Vietnamese.
2. Every word gets a timestamp, measured on the voice file itself: ffmpeg and numpy find the pauses and syllables, and the detection adapts to the reading speed (0.9x to 1.5x).
3. Opus reads the whole script and decides what goes on screen: narration stays on the presenter, key points cut to an illustration. Cuts land inside breath pauses, never mid-sentence.
4. Illustrations are kinetic graphics built from the script's own content (big numbers, line charts, receipts, comparison tables, statements). Some scenes use real Pixabay footage instead: Opus picks the search terms, then looks at the candidate thumbnails and only inserts one that matches.
5. Word-by-word subtitles synced to the voice, transitions and synthesized sound effects are added, and everything is muxed into one MP4.

## Before you start

- **You need your own API accounts.** Claude and Higgsfield are paid; Vbee is paid for voiceover; Pixabay is free. Demo mode runs the whole pipeline with no keys and no cost, so you can try the app first.
- **ffmpeg is a separate install.** `pip` does not install it.
- **Built and tested on Windows.** It runs elsewhere, but on macOS/Linux the graphics fall back to Pillow's default font unless you point `_FONT_DIRS` in `text_effects.py` at a font with Vietnamese glyphs.
- **Presenter mode uploads your portrait to a public file host.** Higgsfield needs a public image URL, so in real (non-demo) runs the photo is uploaded to catbox.moe (public, not auto-deleted) or tmpfiles.org as a fallback. Don't use a photo you wouldn't publish.
- **Higgsfield models depend on your account.** If a model in the dropdown is not available to you, pick another or set `HF_VIDEO_MODEL`.

## Setup

Requires Python 3.10+ and `ffmpeg`/`ffprobe` on your PATH (on Windows, `C:\ffmpeg\` or `C:\ffmpeg\bin\` also works).

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in your keys, or enter them later in the web UI ("API keys")
```

| Variable | Used for | Required? |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude (script, planning, editing) | Yes, unless in demo mode |
| `HF_API_KEY_ID`, `HF_API_KEY_SECRET` | Higgsfield, from https://console.higgsfield.ai | Yes, unless in demo mode |
| `VBEE_APP_ID`, `VBEE_ACCESS_TOKEN` | Vbee Vietnamese text-to-speech (https://vbee.vn) | Only with voiceover on |
| `PIXABAY_API_KEY` | Stock video and photos (https://pixabay.com/api/docs) | No |

**Fonts:** the graphics use the SVN-Gilroy family if it is installed. It is a commercial font and is not included; without it the code falls back to a bold Windows system font.

## Run

```bash
python web_app.py          # open http://localhost:8080 (set PORT to change it)
```

On Windows you can double-click `start_web.bat`. Turn on **demo mode** in the form to run the whole pipeline without spending anything on APIs.

Storyboard mode also runs from the command line:

```bash
python agent.py "15-second ad for an herbal tea, 9:16, warm tones" --budget 5
```

## Project layout

| File | Role |
|---|---|
| `web_app.py`, `templates/index.html` | Tornado server and web UI |
| `agent.py`, `higgsfield_client.py` | Storyboard agent and Higgsfield client |
| `voice_client.py` | Vbee client, muxing voice into video |
| `word_timing.py` | Per-word timestamps from the voice audio |
| `shot_planner.py` | Decides presenter vs. illustration for each part of the script |
| `text_effects.py` | Kinetic graphics and subtitles |
| `speaker_overlay.py` | Caption card and subtitles over presenter shots |
| `stock_footage.py` | Pixabay search, visual review and download |
| `sfx.py` | Sound effects synthesized with numpy |
| `avatar_director.py` | Assembles the presenter video |

## Tests

```bash
python -m pytest -q
```

No API keys needed: calls to Claude, Higgsfield and Pixabay are mocked. Some tests need ffmpeg. If pytest fails with `INTERNALERROR ... NodeId ... _str_cache`, that is a pytest 9.2.0 bug; run `pip install "pytest<9.2"`.

## Safeguards

| Mechanism | Effect |
|---|---|
| Budget, shot and retry caps | Enforced in code; the model cannot exceed them |
| Approval before each render batch | A person approves spending (can be switched off) |
| `Idempotency-Key` on every Higgsfield submit | A retried request is never billed twice |
| Keys go only to their own API | Never sent to a CDN, never written to logs |
| Pixabay: searches cached 24h, files downloaded | Follows the Pixabay API terms |

Higgsfield keeps output files for 7 days only; the agent downloads them to `./output` as soon as they finish.
