# image-converter — monbooru plugin

Convert an image between **WebP, JPEG, PNG and AVIF** from right inside
monbooru, in the spirit of Obsidian's *image-converter*: a small settings
pop-in on the image page, one-click format buttons, and the same three buttons
for a batch. Conversion runs locally with [Pillow](https://python-pillow.org/)
and **replaces the image's file in place** — the row, its tags, sources,
collections and relations stay. No change to monbooru itself is needed.

## Buttons

| Button | Where | What it does |
| --- | --- | --- |
| `convert image` | image page → **Plugins** card | Opens a compact settings box: format, quality, width × height (ratio-locked) |
| `to webp` / `to jpeg` / `to png` | image page → **Plugins** card | One-click conversion with your saved quality/resize defaults |
| `to webp` / `to jpeg` / `to png` | batch bar, after selecting images | Converts the selection with the same defaults, in place |

Buttons only appear on static images (`media = "image"`): GIFs, videos and
CBZ/ZIP archives are left alone and dropped from a batch.

## The convert box

It is deliberately small: the image is already on screen behind the pop-in, so
the box shows only the settings.

- **Format** — WebP, JPEG, PNG, plus AVIF when Pillow has the AVIF codec
  (Pillow 11.3+ built with libavif).
- **Quality** — 1–100 for WebP/JPEG/AVIF; PNG ignores it (disabled).
- **Size** — width × height in pixels, prefilled with the current dimensions,
  with **keep ratio** on by default: editing one field follows the other.
  Uncheck it to stretch. Leaving the values untouched means no resize; typing
  a bigger number upscales, so downscaling on a batch is still the safe path.
- **Batch buttons skip images already in the target format** — default on.
- **Remember format & quality as my defaults** — what the one-click and batch
  buttons use. The hint under the fields shows the current batch defaults
  (`q82 · no resize · skip same format`).

On success the box reports `#123 · 1.4 MB → 320 KB (-77%)` and, when resized,
`640×480 → 320×240`, with **back to the image** and **convert again**.

## The batch message

monbooru shows one flash line of at most 200 characters, so the plugin fills
it with the files it finished plus their new size, as many as fit:

```
converted 4/6 to webp: 001.png 310 KB · 002.png 210 KB · 003.png 180 KB
· 004.png 150 KB; 2 already webp
```

When more than five fit, it appends `+N more`. A file that fails does not
abort the rest: the first error is named, and the count says how many failed.
A batch takes ~8 seconds before stopping (monbooru's relay timeout is 10 s) and
then says `N left, click again` — press the button again to continue. The
single-image box has no such limit.

## Quality

One quality value, default **100**, shared by WebP, JPEG and AVIF — this is an
image booru, so the default favors fidelity over file size. JPEG at quality
95+ also switches to 4:4:4 chroma, so line art keeps its color edges. Lower it
(82–90) if disk space matters. Change it in the convert box with *remember*
ticked, or edit `settings.json` next to the plugin:

```json
{ "format": "webp", "quality": 100, "max_side": 0, "skip_same": true }
```

`format` is what the box opens on; `max_side` (0 = off) is the resize the
one-click and batch buttons apply — a longest-side cap, so mixed portrait and
landscape batches keep their proportions. The batch buttons never resize unless
you set `max_side`, which is why the hint says `no resize` by default.

## Metadata

Prompts are carried across formats the way monbooru reads them, instead of
being dropped:

- **A1111 / Forge** (`parameters`) — kept as a PNG text chunk for PNG, written
  into EXIF **UserComment** for JPEG and WebP. Camera comments are not mistaken
  for prompts: only text with A1111's markers is carried.
- **ComfyUI** (`prompt` / `workflow`) — kept as PNG text chunks; for WebP the
  plugin writes them where monbooru looks, EXIF `Model` = `prompt:…` and
  `Make` = `workflow:…`. Monbooru does not read ComfyUI metadata from JPEG, so
  that direction cannot keep it.
- EXIF (orientation baked in, the rest passed through) and ICC profiles are
  kept best-effort. JPEG's EXIF segment is 64 KB, so a very large prompt block
  is skipped there with a log line rather than corrupting the file.

## Install

Put the folder next to `monbooru.toml`, inside `plugins/`:

```
<monbooru profile>/
├── monbooru.toml
└── plugins/
    └── image-converter/
        ├── plugin.toml
        ├── image-converter.py
        └── README.md
```

Windows desktop/portable profile is `%APPDATA%\monbooru\` (or the folder with
the portable `monbooru.exe`). Then restart monbooru, go to
**Settings → Plugins**, **Enable** `image-converter`, and approve its pairing
(asks `read` + `write`, never `delete`).

### Updating from an older copy

The button list is part of the pairing, so after replacing the files and
restarting, the plugin offers a **re-pair** with the new buttons. Approve it
once in Settings → Plugins; until then monbooru keeps rendering the old
buttons and the plugin keeps working with its old token.

### Python

`plugin.toml` launches `python3` on Linux/macOS and `python` on Windows; the
interpreter needs Pillow and must be on `PATH`. If monbooru's environment does
not find it, point `command_windows` at the full path (`py -0p` prints the
installed interpreters):

```toml
command_windows = "C:/Users/you/.../python.exe"
```

Change the port with `--port` in `args_windows`; the plugin then re-offers a
pairing, since monbooru calls it at the address from the pairing.

## Limits

- Inputs must be decodable by Pillow (JPEG, PNG, WebP, AVIF, GIF frames).
  **JPEG XL is not** — monbooru shows it, Pillow cannot read it, so the
  conversion fails with a clear message.
- No undo: the file is rewritten in place. Keep a backup of anything
  irreplaceable.
- No elevated rights: the plugin binds `127.0.0.1:7312`, keeps
  `credentials.json` / `settings.json` in its own folder, and exits when
  monbooru closes its stdin.

## Look and size

The pop-in has its own fixed dark palette (system UI font, rounded controls,
crimson accent) and does not read or copy monbooru's theme variables — it
always looks the same.

monbooru opens plugin pages in a fixed `90vw × 74vh` dialog. The page measures
its own content and shrinks the frame and the dialog to fit, so the settings
box does not leave three quarters of the pop-in empty.

The **plugin buttons themselves** (the `to webp` / `to jpeg` / `to png` buttons
in the image's *Plugins* card, in the batch bar, and the pop-in's title) are
painted by monbooru with its own `--plugin` accent. A plugin cannot restyle
them; only a monbooru theme that overrides `--plugin` changes that color.

## Credits and license

- Made by **GideonLovecraft**, with an assist from a Chinese artificial whale,
  in about five minutes and a few cents.
- Inspired by [xRyul/obsidian-image-converter](https://github.com/xRyul/obsidian-image-converter)
  (MIT, Copyright (c) 2023 xRyul) — the idea of a format/quality/resize
  converter next to the image. Thanks; no code was copied from it, or from
  monbooru (AGPL-3.0-or-later), so no third-party notice has to travel with
  this code.
- This plugin is therefore its own work and carries **MIT**, see
  [LICENSE](LICENSE). If you ever paste code from the Obsidian plugin or from
  monbooru, add that project's notice and follow its license (MIT / AGPL).

## How it works

1. `GET /api/v1/images/{id}` — type, dimensions, name.
2. `GET /api/v1/images/{id}/file` — original bytes.
3. Pillow re-encodes (orientation baked in, size box applied, prompts carried).
4. `POST /api/v1/images/{id}/file` (multipart, field `file`) — replaces the
   bytes in place; monbooru re-derives hash, dimensions, thumbnail and phash.

Contract `1`. Tested offline against Pillow 12.3 with WebP, AVIF and EXIF
round-trips; the pairing/button contract follows monbooru's
`examples/simple-edit`.
