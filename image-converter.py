#!/usr/bin/env python3
"""image-converter: convert monbooru images between WebP, JPEG and PNG.

A monbooru plugin. It pairs with monbooru like any peer, adds a "convert
image" button to an image's page and three format buttons to the batch bar,
and does the conversion locally with Pillow. The result replaces the image's
file in place (POST /images/{id}/file), so the row and everything attached
to it -- tags, sources, collections, relations -- stay.

Needs Python 3.9+ and Pillow. Nothing else.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import io
import json
import logging
import os
import secrets
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from string import Template

from PIL import Image, ImageOps, PngImagePlugin

APP = "image-converter"
VERSION = "0.1.0"
# The monbooru plugin contract this was written against.
CONTRACT = 1
DEFAULT_PORT = 7312
RELAY_BUDGET = 8.0  # monbooru waits 10 s for a relay answer; leave a margin
MAX_FORM_BYTES = 1 << 20

CREDENTIALS_FILE = "credentials.json"
SETTINGS_FILE = "settings.json"

DEFAULT_SETTINGS = {"format": "webp", "quality": 100, "max_side": 0, "skip_same": True}

# EXIF tags monbooru reads generation metadata from: A1111 keeps its
# "parameters" text in UserComment, the ComfyUI savers use Make/Model with a
# "workflow:"/"prompt:" prefix (read for WebP).
EXIF_TAG_MAKE = 0x010F
EXIF_TAG_MODEL = 0x0110
EXIF_TAG_USER_COMMENT = 0x9286

log = logging.getLogger(APP)


# ---------------------------------------------------------------------------
# credentials, set at runtime
# ---------------------------------------------------------------------------

MONBOORU = "http://127.0.0.1:8455"
SELF_URL = "http://127.0.0.1:7312"
STATE_DIR = "."

STOP = threading.Event()
CREDS = {"token": "", "peer": "", "url": "", "buttons": ""}
CREDS_LOCK = threading.Lock()


class Unreachable(Exception):
    """monbooru did not answer at all."""


class ApiError(Exception):
    """monbooru answered with an error status."""


class ConvertError(Exception):
    """The image cannot be converted (or not sensibly)."""


def creds():
    with CREDS_LOCK:
        return dict(CREDS)


def load_credentials():
    path = os.path.join(STATE_DIR, CREDENTIALS_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            with CREDS_LOCK:
                CREDS["token"] = str(data.get("token") or "")
                CREDS["peer"] = str(data.get("peer") or "")
                CREDS["url"] = str(data.get("url") or "")
                CREDS["buttons"] = str(data.get("buttons") or "")
    except FileNotFoundError:
        pass
    except Exception as exc:  # a corrupt file must not stop the plugin
        log.warning("could not read %s: %s", path, exc)


def save_credentials(token, peer, url):
    with CREDS_LOCK:
        CREDS.update(token=token, peer=peer, url=url, buttons=buttons_fingerprint())
    path = os.path.join(STATE_DIR, CREDENTIALS_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"token": token, "peer": peer, "url": url,
                   "buttons": buttons_fingerprint()}, f)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def forget_credentials():
    with CREDS_LOCK:
        CREDS.update(token="", peer="", url="", buttons="")
    try:
        os.remove(os.path.join(STATE_DIR, CREDENTIALS_FILE))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# settings: the defaults the batch buttons use
# ---------------------------------------------------------------------------

SETTINGS_LOCK = threading.Lock()


def load_settings():
    out = dict(DEFAULT_SETTINGS)
    path = os.path.join(STATE_DIR, SETTINGS_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            if data.get("format") in FORMATS:
                out["format"] = data["format"]
            if isinstance(data.get("quality"), (int, float)):
                out["quality"] = max(1, min(100, int(data["quality"])))
            if isinstance(data.get("max_side"), (int, float)):
                out["max_side"] = max(0, int(data["max_side"]))
            if isinstance(data.get("skip_same"), bool):
                out["skip_same"] = data["skip_same"]
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("could not read %s: %s", path, exc)
    return out


def save_settings(values):
    with SETTINGS_LOCK:
        merged = load_settings()
        merged.update(values)
        path = os.path.join(STATE_DIR, SETTINGS_FILE)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)


# ---------------------------------------------------------------------------
# talking to monbooru
# ---------------------------------------------------------------------------


def api_call(method, path, *, json_body=None, raw_body=None, content_type=None,
             gallery="", timeout=120):
    """One API exchange. Returns (status, body); raises Unreachable."""
    url = MONBOORU.rstrip("/") + "/api/v1" + path
    if gallery:
        sep = "&" if "?" in url else "?"
        url += sep + urllib.parse.urlencode({"gallery": gallery})
    if json_body is not None:
        raw_body = json.dumps(json_body).encode("utf-8")
        content_type = "application/json"
    req = urllib.request.Request(url, data=raw_body, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    token = creds()["token"]
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise Unreachable(str(reason)) from exc


def error_text(raw, limit=300):
    try:
        doc = json.loads(raw)
        if isinstance(doc, dict):
            value = doc.get("error") or doc.get("message") or doc.get("code")
            if value:
                return str(value)[:limit]
    except Exception:
        pass
    text = raw.decode("utf-8", "replace").strip()
    return (text or "no details given")[:limit]


def expect_ok(code, raw, what):
    if 200 <= code < 300:
        return
    raise ApiError("{}: HTTP {}: {}".format(what, code, error_text(raw)))


def get_image(image_id, gallery=""):
    code, raw = api_call("GET", "/images/{}".format(image_id), gallery=gallery)
    expect_ok(code, raw, "reading image #{}".format(image_id))
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ApiError("reading image #{}: unreadable answer".format(image_id)) from exc


def get_image_bytes(image_id, gallery=""):
    code, raw = api_call("GET", "/images/{}/file".format(image_id), gallery=gallery, timeout=300)
    expect_ok(code, raw, "downloading image #{}".format(image_id))
    return raw


def multipart_body(field, filename, data):
    boundary = "----monbooru-image-converter-" + secrets.token_hex(16)
    safe_name = filename.replace('"', "_")
    head = (
        "--{b}\r\n"
        'Content-Disposition: form-data; name="{f}"; filename="{n}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).format(b=boundary, f=field, n=safe_name).encode("utf-8")
    tail = ("\r\n--{}--\r\n".format(boundary)).encode("utf-8")
    return head + data + tail, "multipart/form-data; boundary=" + boundary


def replace_image_file(image_id, filename, data, gallery=""):
    body, content_type = multipart_body("file", filename, data)
    code, raw = api_call("POST", "/images/{}/file".format(image_id), raw_body=body,
                         content_type=content_type, gallery=gallery, timeout=300)
    expect_ok(code, raw, "replacing image #{}".format(image_id))
    try:
        return json.loads(raw) or {}
    except ValueError:
        return {}


# ---------------------------------------------------------------------------
# conversion
# ---------------------------------------------------------------------------


FORMATS = {
    "webp": {"ext": ".webp", "pil": "WEBP", "label": "WebP", "lossy": True},
    "jpeg": {"ext": ".jpg", "pil": "JPEG", "label": "JPEG", "lossy": True},
    "png": {"ext": ".png", "pil": "PNG", "label": "PNG", "lossy": False},
}


def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            if unit == "B":
                return "{:.0f} B".format(n)
            return "{:.1f} {}".format(n, unit)
        n /= 1024.0
    return "?"


def percent_delta(before, after):
    try:
        if before <= 0:
            return ""
        return "{:+d}%".format(round((after - before) * 100.0 / before))
    except Exception:
        return ""


def _short_name(name, limit=16):
    name = str(name or "")
    if len(name) <= limit:
        return name
    stem, ext = os.path.splitext(name)
    keep = max(1, limit - len(ext) - 1)
    return stem[:keep] + "…" + ext


def _text_chunks(img):
    reserved = {"icc_profile", "exif", "transparency", "dpi", "loop", "duration", "background"}
    return {k: v for k, v in img.info.items() if isinstance(v, str) and k not in reserved}


# ---------------------------------------------------------------------------
# generation metadata: carry AI prompts across formats the way monbooru
# reads them (PNG text chunks, or EXIF UserComment/Make/Model elsewhere)
# ---------------------------------------------------------------------------


def _load_exif(raw):
    if not raw:
        return None
    try:
        exif = Image.Exif()
        exif.load(raw)
        return exif
    except Exception:
        return None


def _exif_value(exif, tag):
    if exif is None:
        return None
    try:
        return exif.get(tag)
    except Exception:
        return None


def _exif_text(exif, tag):
    value = _exif_value(exif, tag)
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip("\x00")
    try:
        return bytes(value).decode("utf-8", "replace").strip("\x00")
    except Exception:
        return ""


def _usercomment_text(exif):
    value = _exif_value(exif, EXIF_TAG_USER_COMMENT)
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip("\x00")
    try:
        data = bytes(value)
    except Exception:
        return ""
    if len(data) >= 8:
        head, body = data[:8], data[8:]
        if head == b"UNICODE\x00":
            try:
                if body[:2] == b"\xff\xfe":
                    return body[2:].decode("utf-16-le", "replace").rstrip("\x00")
                if body[:2] == b"\xfe\xff":
                    return body[2:].decode("utf-16-be", "replace").rstrip("\x00")
                return body.decode("utf-16-le", "replace").rstrip("\x00")
            except Exception:
                return ""
        if head[:5] == b"ASCII" or head == b"\x00" * 8:
            return body.split(b"\x00")[0].decode("utf-8", "replace")
    return data.split(b"\x00")[0].decode("utf-8", "replace")


def _set_usercomment(exif, text):
    try:
        if text.isascii():
            exif[EXIF_TAG_USER_COMMENT] = b"ASCII\x00\x00\x00" + text.encode("ascii")
        else:
            exif[EXIF_TAG_USER_COMMENT] = (
                b"UNICODE\x00" + b"\xff\xfe" + text.encode("utf-16-le"))
    except Exception as exc:
        log.warning("could not carry the prompt into EXIF: %s", exc)


def _comfy_texts(exif):
    """ComfyUI's savers put the API prompt in Model and the graph in Make."""
    model = _exif_text(exif, EXIF_TAG_MODEL)
    make = _exif_text(exif, EXIF_TAG_MAKE)
    prompt = model[len("prompt:"):] if model.startswith("prompt:") else ""
    workflow = make[len("workflow:"):] if make.startswith("workflow:") else ""
    return prompt, workflow


def _looks_like_a1111(text):
    """Camera UserComments are not generation parameters; A1111's markers are."""
    first = text.lstrip()
    return ("Negative prompt:" in text or first.startswith("Steps:")
            or "\nSteps:" in text)


def output_metadata(source_text, exif_raw, target):
    """(exif bytes, png text chunks) that keep the prompts monbooru can read."""
    text = dict(source_text)
    exif = _load_exif(exif_raw)

    params = text.get("parameters") or ""
    if not params:
        comment = _usercomment_text(exif)
        if _looks_like_a1111(comment):
            params = comment
    prompt = text.get("prompt") or ""
    workflow = text.get("workflow") or ""
    if exif is not None:
        comfy_prompt, comfy_workflow = _comfy_texts(exif)
        prompt = prompt or comfy_prompt
        workflow = workflow or comfy_workflow
    if params:
        text["parameters"] = params
    if prompt:
        text["prompt"] = prompt
    if workflow:
        text["workflow"] = workflow

    if target == "png":
        # PNG keeps its text chunks; the eXIf blob rides along untouched.
        return exif_raw, text
    if exif is None and not (params or prompt or workflow):
        return exif_raw, text

    if exif is None:
        exif = Image.Exif()
    if params:
        _set_usercomment(exif, params)
    if target == "webp":
        if prompt:
            exif[EXIF_TAG_MODEL] = "prompt:" + prompt
        if workflow:
            exif[EXIF_TAG_MAKE] = "workflow:" + workflow
    try:
        out = exif.tobytes()
    except Exception as exc:
        log.warning("could not write EXIF; the prompts travel in the pixels only: %s", exc)
        return exif_raw, text
    if target == "jpeg" and len(out) > 60000:
        log.warning("the EXIF block is over a JPEG segment; dropping the prompt copy")
        return exif_raw, text
    return out, text


def _fit_mode(img, target):
    """Put the pixels in a mode the encoder takes, flattening alpha for JPEG."""
    if target == "jpeg":
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            return flat
        return img if img.mode in ("RGB", "L") else img.convert("RGB")
    if target == "png":
        if img.mode == "P":
            return img.convert("RGBA")
        return img
    # webp
    if img.mode in ("RGB", "RGBA"):
        return img
    if img.mode == "L":
        return img.convert("RGB")
    if img.mode in ("LA", "P"):
        return img.convert("RGBA")
    return img.convert("RGB")


def _encode(img, target, quality, exif, icc, text):
    spec = FORMATS[target]
    out = io.BytesIO()
    if target == "png":
        kwargs = {"optimize": True}
        if text:
            info = PngImagePlugin.PngInfo()
            for key, value in text.items():
                info.add_text(key, value)
            kwargs["pnginfo"] = info
        if icc:
            kwargs["icc_profile"] = icc
        if exif:
            kwargs["exif"] = exif
        img.save(out, "PNG", **kwargs)
    elif target == "jpeg":
        kwargs = {"quality": quality, "optimize": True, "progressive": True}
        if quality >= 95:
            # 4:4:4 chroma: at near-max quality, line art should not lose it.
            kwargs["subsampling"] = 0
        if icc:
            kwargs["icc_profile"] = icc
        if exif:
            kwargs["exif"] = exif
        img.save(out, "JPEG", **kwargs)
    else:  # webp
        kwargs = {"quality": quality, "method": 4}
        if icc:
            kwargs["icc_profile"] = icc
        if exif:
            kwargs["exif"] = exif
        img.save(out, "WEBP", **kwargs)
    return out.getvalue()


def encode(img, target, quality, exif, icc, text):
    try:
        return _encode(img, target, quality, exif, icc, text)
    except Exception as exc:
        # A bad ICC profile or EXIF blob should not cost the conversion.
        log.warning("re-encoding %s without embedded metadata: %s", target, exc)
        return _encode(img, target, quality, None, None, {})


def _target_size(w, h, width=0, height=0, max_side=0):
    """(w, h) from the page's box, else from a longest-side cap; None = keep."""
    width = max(0, min(32768, int(width or 0)))
    height = max(0, min(32768, int(height or 0)))
    max_side = max(0, min(32768, int(max_side or 0)))
    if width and height:
        return (width, height)
    if width:
        return (width, max(1, round(h * width / w)))
    if height:
        return (max(1, round(w * height / h)), height)
    if max_side and max(w, h) > max_side:
        scale = float(max_side) / float(max(w, h))
        return (max(1, round(w * scale)), max(1, round(h * scale)))
    return None


def convert_bytes(raw, target, quality, *, width=0, height=0, max_side=0):
    """Re-encode raw bytes as target. Returns a dict of data and dimensions."""
    if target not in FORMATS:
        raise ConvertError("unsupported target format: {}".format(target))
    try:
        src = Image.open(io.BytesIO(raw))
        src.load()
    except Exception as exc:
        raise ConvertError("the stored bytes do not decode as an image") from exc
    if getattr(src, "is_animated", False):
        raise ConvertError("animated images are not supported")

    # A phone photo's pixels are stored sideways; the viewer applies its EXIF
    # orientation. Converted images should be upright on their own.
    img = ImageOps.exif_transpose(src)
    if img is not src:
        img.load()
    icc = img.info.get("icc_profile")
    exif_out, text_out = output_metadata(_text_chunks(img), img.info.get("exif"), target)

    size = _target_size(img.size[0], img.size[1], width, height, max_side)
    if size and size != img.size:
        img = img.resize(size, Image.LANCZOS)

    img = _fit_mode(img, target)
    data = encode(img, target, max(1, min(100, int(quality))), exif_out, icc, text_out)
    return {
        "data": data,
        "ext": FORMATS[target]["ext"],
        "width": img.size[0],
        "height": img.size[1],
        "source_format": (src.format or "").lower(),
    }


def convert_image(image_id, target, *, gallery="", quality=82, width=0, height=0,
                  max_side=0, skip_same=False):
    meta = get_image(image_id, gallery)
    file_type = str(meta.get("file_type") or "")
    if file_type in ("gif", "mp4", "webm", "cbz", "zip"):
        raise ConvertError("{} files cannot be converted".format(file_type or "these"))
    if skip_same and file_type == target:
        return {"skipped": True, "reason": "already " + target}
    raw = get_image_bytes(image_id, gallery)
    result = convert_bytes(raw, target, quality, width=width, height=height, max_side=max_side)
    original = os.path.basename(str(meta.get("canonical_path") or "")) or "image-{}".format(image_id)
    stem = os.path.splitext(original)[0]
    filename = (stem or "image-{}".format(image_id)) + result["ext"]
    reply = replace_image_file(image_id, filename, result["data"], gallery)
    return {
        "skipped": False,
        "name": original,
        "replaced": bool(reply.get("replaced", True)),
        "before": len(raw),
        "after": len(result["data"]),
        "old_width": meta.get("width"),
        "old_height": meta.get("height"),
        "width": result["width"],
        "height": result["height"],
        "source_format": file_type,
        "target": target,
    }


# ---------------------------------------------------------------------------
# pairing
# ---------------------------------------------------------------------------


def buttons():
    """The button list is part of the pairing: changing it asks for a re-pair.

    detail-actions: the picker plus one relay per format, so any of the three
    is one click away under the image.
    """
    quick = []
    for target in ("webp", "jpeg", "png"):
        quick.append({"slot": "detail-actions", "label": "to " + target,
                      "mode": "relay",
                      "path": "/relay/convert?format=" + target,
                      "media": "image"})
    batch = []
    for target in ("webp", "jpeg", "png"):
        batch.append({"slot": "batch-bar", "label": "to " + target,
                      "mode": "relay",
                      "path": "/relay/convert?format=" + target,
                      "media": "image"})
    return [{
        "slot": "detail-actions", "label": "convert image", "mode": "open",
        "path": "/convert?image={image_id}&gallery={gallery}&back={back_url}",
        "media": "image",
    }] + quick + batch


def buttons_fingerprint():
    raw = json.dumps(buttons(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def wait_for_monbooru():
    """monbooru starts us; give its listener a moment, then keep checking."""
    while not STOP.is_set():
        try:
            api_call("GET", "/galleries", timeout=10)
            return True
        except Unreachable:
            log.info("waiting for monbooru at %s", MONBOORU)
            STOP.wait(2)
    return False


def offer_pairing():
    while not STOP.is_set():
        peer = secrets.token_urlsafe(32)
        body = {
            "app": APP,
            "url": SELF_URL,
            "requested_scopes": ["read", "write"],
            "peer_token": peer,
            "version": VERSION,
            "buttons": buttons(),
            "contract": CONTRACT,
        }
        try:
            code, raw = api_call("POST", "/pair/request", json_body=body, timeout=20)
        except Unreachable as exc:
            log.info("cannot reach monbooru at %s: %s", MONBOORU, exc)
            STOP.wait(5)
            continue
        if code == 409:
            log.error("monbooru refused the pairing: %s", error_text(raw))
            return
        if code == 429:
            log.info("monbooru has too many pending pairing requests; retrying")
            STOP.wait(10)
            continue
        if code != 200:
            log.warning("pairing request failed (HTTP %s): %s", code, error_text(raw))
            STOP.wait(10)
            continue
        try:
            request_id = json.loads(raw).get("request_id") or ""
        except ValueError:
            request_id = ""
        if not request_id:
            log.warning("pairing request answered without an id; retrying")
            STOP.wait(10)
            continue
        log.info("waiting for approval in monbooru: Settings -> Plugins")
        while not STOP.is_set():
            try:
                code, raw = api_call(
                    "GET", "/pair/status?id=" + urllib.parse.quote(request_id), timeout=20)
            except Unreachable as exc:
                log.info("monbooru went away while pairing: %s", exc)
                STOP.wait(5)
                continue
            if code == 404:
                log.info("the pairing offer expired; offering again")
                break
            if code != 200:
                log.warning("pairing status failed (HTTP %s): %s", code, error_text(raw))
                STOP.wait(5)
                continue
            try:
                answer = json.loads(raw)
            except ValueError:
                answer = {}
            if answer.get("token"):
                save_credentials(answer["token"], peer, SELF_URL)
                log.info("paired as %s", APP)
                return
            if answer.get("status") == "denied":
                log.info("the pairing was denied; restart the plugin to offer again")
                return
            STOP.wait(2)


def bootstrap():
    load_credentials()
    if not wait_for_monbooru():
        return
    while not STOP.is_set():
        current = creds()
        if current["token"] and current["peer"]:
            if current["url"] and current["url"] != SELF_URL:
                log.info("the plugin now answers at %s but paired at %s; pairing again",
                         SELF_URL, current["url"])
                forget_credentials()
                continue
            if current["buttons"] != buttons_fingerprint():
                # The user approves buttons at pairing, so a changed list is a
                # new offer. The stored token keeps working until it is approved.
                log.info("the button set changed; offering it for approval in monbooru")
                offer_pairing()
                return
            try:
                code, _ = api_call("GET", "/galleries", timeout=15)
            except Unreachable:
                STOP.wait(2)
                continue
            if code == 200:
                log.info("paired as %s", APP)
                return
            if code in (401, 403):
                log.info("monbooru no longer accepts the stored token; pairing again")
                forget_credentials()
                continue
            log.info("monbooru answered HTTP %s; retrying", code)
            STOP.wait(10)
            continue
        offer_pairing()
        return


# ---------------------------------------------------------------------------
# the pages
# ---------------------------------------------------------------------------

CSS = """
:root {
  color-scheme: dark;
  /* The page's own fixed palette: it does not read or copy monbooru's theme
     variables, so it always looks the same. */
  --bg: #0e0e0e;
  --bg-surface: #1a1818;
  --bg-input: #080808;
  --border: #2a2828;
  --fg: #e8e4e2;
  --fg-dim: #a09898;
  --accent: #9d2235;
  --link: #ef8f99;
  --success: #7fc99a;
  --warning: #e8c06a;
  --error: #ef8f99;
  --plugin: #aa66cc;
  --on-accent: var(--fg);
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 12px 14px; background: var(--bg); color: var(--fg);
  font: 13px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
h1 { font-size: 15px; margin: 0 0 4px; font-weight: 600; }
p { margin: 0 0 10px; }
.meta { color: var(--fg-dim); }
a { color: var(--link); }
form { display: grid; gap: 10px; width: 100%; max-width: 460px; }
fieldset {
  border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px;
  margin: 0; display: grid; gap: 9px;
}
legend { color: var(--fg-dim); font-size: 12px; padding: 0 4px; }
label { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
label.check { align-items: flex-start; color: var(--fg-dim); font-size: 12px; }
label.inline { gap: 4px; color: var(--fg-dim); font-size: 12px; }
.row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.lbl { color: var(--fg-dim); min-width: 42px; }
.unit { color: var(--fg-dim); }
select, input[type=number] {
  background: var(--bg-input); color: var(--fg); border: 1px solid var(--border);
  border-radius: 4px; padding: 4px 6px; font: inherit;
}
input[type=number] { width: 82px; }
input[type=checkbox] { accent-color: var(--accent); }
input[type=range] { accent-color: var(--accent); width: 160px; }
output { color: var(--link); min-width: 26px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 2px; }
button, .btn {
  font: inherit; color: var(--fg); background: var(--bg-surface);
  border: 1px solid var(--border); border-radius: 4px; padding: 5px 10px;
  cursor: pointer; text-decoration: none; display: inline-block;
}
button:hover, .btn:hover { border-color: var(--link); }
button.primary, .btn.primary {
  color: var(--accent); background: var(--bg-surface); border-color: var(--accent);
}
button.primary:hover, .btn.primary:hover {
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 18%, var(--bg-surface));
  border-color: var(--accent);
}
.hint { color: var(--fg-dim); font-size: 12px; margin: 0; max-width: 460px; }
.error {
  border: 1px solid var(--error); color: var(--error); border-radius: 6px;
  padding: 8px 10px; margin-bottom: 10px; white-space: pre-wrap; max-width: 460px;
}
"""

SHELL = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title}</title>
<style>${css}</style>
</head>
<body>
<script>
(function () {
  // monbooru opens plugin pages in a fixed 90vw x 74vh dialog. The settings
  // box is short, so fit the frame and the dialog to it instead of leaving
  // most of the pop-in empty.
  function fitFrame() {
    try {
      var frame = window.frameElement;
      if (!frame) return;
      var dlg = frame.closest ? frame.closest("dialog") : null;
      if (dlg) {
        dlg.style.width = "min(572px, 94vw)";
        dlg.style.maxWidth = "94vw";
      }
      // Cap against the page's viewport, not the frame's own height, or each
      // pass would shrink the next one.
      var limit = Math.round(
        ((window.parent && window.parent.innerHeight) || window.innerHeight || 800) * 0.82);
      // Read after setting the width: that read forces the narrower layout,
      // so the height includes any rewrapped lines. The extra pixels cover
      // the iframe's border and subpixel rounding, and the 10% slack keeps a
      // scrollbar from ever appearing.
      var content = Math.ceil(
        (Math.max(document.body.offsetHeight, document.body.scrollHeight) + 12) * 1.1);
      frame.style.height = Math.min(content, limit) + "px";
      // Second pass: if anything still overflows (a wrapped label, a
      // scrollbar), grow by exactly that much.
      requestAnimationFrame(function () {
        try {
          var overflow = document.documentElement.scrollHeight - frame.clientHeight;
          if (overflow > 0 && frame.clientHeight < limit) {
            frame.style.height = Math.min(frame.clientHeight + overflow + 4, limit) + "px";
          }
        } catch (e) { /* ignore */ }
      });
    } catch (e) { /* opened on its own, or the parent is not readable */ }
  }
  if (window.frameElement) {
    fitFrame();
    window.addEventListener("load", fitFrame);
    window.addEventListener("resize", fitFrame);
  }
})();
</script>
${body}
</body>
</html>
""")


def page(title, body):
    return SHELL.substitute(title=html.escape(title), css=CSS, body=body)


def sanitize_back(raw):
    raw = (raw or "").strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return "/"
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return raw
    return "/"


def error_banner(message):
    return '<div class="error">{}</div>'.format(html.escape(message))


FORM_HTML = Template("""
<h1>convert image</h1>
<p class="meta">#${id} · ${kind} · ${dims} · ${size} · ${name}</p>
${error}
<form method="post" action="convert">
  <input type="hidden" name="image" value="${id}">
  <input type="hidden" name="gallery" value="${gallery_hidden}">
  <input type="hidden" name="back" value="${back}">
  <fieldset>
    <legend>target</legend>
    <label>Format <select id="format" name="format">${options}</select></label>
    <label id="quality-row">Quality
      <input id="quality" type="range" name="quality" min="1" max="100" value="${quality}">
      <output id="quality-out">${quality}</output>
    </label>
    <div class="row">
      <span class="lbl">Size</span>
      <input id="width" type="number" name="width" min="1" max="32768" step="1" value="${width}">
      <span class="unit">×</span>
      <input id="height" type="number" name="height" min="1" max="32768" step="1" value="${height}">
      <span class="unit">px</span>
      <label class="inline"><input type="checkbox" id="lock" checked> keep ratio</label>
    </div>
    <label class="check"><input type="checkbox" name="skip_batch" value="1"${checked}>
      batch buttons skip images already in the target format</label>
    <label class="check"><input type="checkbox" name="remember" value="1" checked>
      remember format &amp; quality as my defaults</label>
  </fieldset>
  <p class="hint">one-click buttons: ${defaults_hint}</p>
  <div class="actions">
    <button type="submit" class="primary">convert &amp; replace</button>
    <a class="btn" href="${back}">cancel</a>
  </div>
  <p class="hint">The converted file replaces the original in place, so tags, sources,
  collections and relations stay. Videos, GIFs and comic archives are left alone.</p>
</form>
<script>
(function () {
  var format = document.getElementById('format');
  var quality = document.getElementById('quality');
  var out = document.getElementById('quality-out');
  var row = document.getElementById('quality-row');
  function sync() {
    out.value = quality.value;
    out.textContent = quality.value;
    var lossy = format.value !== 'png';
    quality.disabled = !lossy;
    row.style.opacity = lossy ? '1' : '.45';
  }
  quality.addEventListener('input', sync);
  format.addEventListener('change', sync);
  sync();

  var width = document.getElementById('width');
  var height = document.getElementById('height');
  var lock = document.getElementById('lock');
  var ratio = ${ratio};
  function fromWidth() {
    if (!lock.checked) return;
    var v = parseInt(width.value, 10);
    if (v > 0) height.value = Math.max(1, Math.round(v / ratio));
  }
  function fromHeight() {
    if (!lock.checked) return;
    var v = parseInt(height.value, 10);
    if (v > 0) width.value = Math.max(1, Math.round(v * ratio));
  }
  width.addEventListener('input', fromWidth);
  height.addEventListener('input', fromHeight);
})();
</script>
""")


def render_form(image_id, gallery, meta, settings, back, *, error="", values=None):
    values = values or {}
    target = values.get("format") or settings.get("format") or "webp"
    if target not in FORMATS:
        target = "webp"
    quality = values.get("quality", settings.get("quality", 82))
    try:
        quality = max(1, min(100, int(quality)))
    except (TypeError, ValueError):
        quality = 82
    skip_batch = bool(values.get("skip_batch", settings.get("skip_same", True)))
    batch_max = settings.get("max_side", 0) or 0

    options = []
    for name, spec in FORMATS.items():
        selected = " selected" if name == target else ""
        options.append('<option value="{}"{}>{}</option>'.format(
            name, selected, html.escape(spec["label"])))

    original_w = meta.get("width") or 0
    original_h = meta.get("height") or 0
    form_w = values.get("width", original_w) or 0
    form_h = values.get("height", original_h) or 0
    ratio = (float(original_w) / float(original_h)) if original_w and original_h else 1.0
    dimensions = ("{}×{}".format(original_w, original_h)
                  if original_w and original_h else "unknown size")
    filename = os.path.basename(str(meta.get("canonical_path") or "")) or "(no name)"
    size_text = human_size(meta.get("file_size") or 0)
    checked = " checked" if skip_batch else ""
    defaults_hint = "q{} · {} · {}".format(
        settings.get("quality", 82),
        "max {} px".format(batch_max) if batch_max else "no resize",
        "skip same format" if settings.get("skip_same", True) else "re-encode same format")

    body = FORM_HTML.substitute(
        id=image_id,
        kind=html.escape(str(meta.get("file_type") or "?")),
        dims=html.escape(dimensions),
        size=html.escape(size_text),
        name=html.escape(filename),
        error=error_banner(error) if error else "",
        gallery_hidden=html.escape(gallery or "", quote=True),
        back=html.escape(back, quote=True),
        options="".join(options),
        quality=quality,
        width=form_w or "",
        height=form_h or "",
        ratio=round(ratio, 6) or 1,
        checked=checked,
        defaults_hint=html.escape(defaults_hint),
    )
    return page("convert image", body)


def render_result(image_id, gallery, back, result):
    target = result["target"]
    before = result.get("before", 0)
    after = result.get("after", 0)
    delta = percent_delta(before, after)
    delta_text = " ({})".format(delta) if delta else ""
    lines = ['<p class="meta">#{} · {} → {}{}</p>'.format(
        image_id, human_size(before), human_size(after), delta_text)]
    old_w, old_h = result.get("old_width"), result.get("old_height")
    new_w, new_h = result.get("width"), result.get("height")
    if old_w and old_h and new_w and new_h and (old_w, old_h) != (new_w, new_h):
        lines.append('<p class="meta">resized {}×{} → {}×{}</p>'.format(
            old_w, old_h, new_w, new_h))
    if not result.get("replaced", True):
        lines.append('<p class="hint">The bytes were already identical, so the file was left as it was.</p>')
    gallery_q = urllib.parse.quote(gallery or "")
    again = "convert?image={}&amp;gallery={}&amp;back={}".format(
        image_id, gallery_q, urllib.parse.quote(back, safe=""))
    body = """
<h1>converted to {target}</h1>
{lines}
<p class="hint">Tags, sources, collections and relations stayed on the image.</p>
<div class="actions">
  <a class="btn primary" href="{back}">back to the image</a>
  <a class="btn" href="{again}">convert again</a>
</div>
""".format(target=html.escape(target), lines="\n".join(lines),
           back=html.escape(back, quote=True), again=again)
    return page("converted", body)


def render_skipped(image_id, back, target):
    body = """
<h1>nothing to do</h1>
<p class="meta">#{id} is already {target}.</p>
<div class="actions">
  <a class="btn primary" href="{back}">back to the image</a>
</div>
""".format(id=image_id, target=html.escape(target), back=html.escape(back, quote=True))
    return page("nothing to do", body)


def render_error(title, message, back):
    body = """
<h1>{title}</h1>
{error}
<div class="actions">
  <a class="btn" href="{back}">back to the image</a>
</div>
""".format(title=html.escape(title), error=error_banner(message),
           back=html.escape(back, quote=True))
    return page(title, body)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    # -- plumbing ---------------------------------------------------------

    def send_body(self, status, body, content_type, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, doc):
        self.send_body(status, json.dumps(doc).encode("utf-8"),
                       "application/json; charset=utf-8")

    def send_text(self, status, text, content_type="text/plain; charset=utf-8"):
        self.send_body(status, text.encode("utf-8"), content_type)

    def send_html(self, status, markup):
        self.send_body(status, markup.encode("utf-8"), "text/html; charset=utf-8")

    def send_empty(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def read_body(self, limit=MAX_FORM_BYTES):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > limit:
            raise ValueError("request body too large")
        return self.rfile.read(length) if length else b""

    def authorized(self):
        header = self.headers.get("Authorization", "")
        peer = creds()["peer"]
        if not peer or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[len("Bearer "):], peer)

    # -- routing ----------------------------------------------------------

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        body = b""
        try:
            if method == "POST":
                body = self.read_body()
            if path == "/health" and method == "GET":
                self.send_json(200, {"version": VERSION})
                return
            if not self.authorized():
                self.send_text(401, "unauthorized")
                return
            if path == "/api/v1/pair/remove" and method == "POST":
                self.handle_unpair()
                return
            if path == "/convert" and method == "GET":
                self.handle_page(query)
                return
            if path == "/convert" and method == "POST":
                self.handle_convert(body)
                return
            if path == "/relay/convert" and method == "POST":
                self.handle_relay(query, body)
                return
            self.send_text(404, "not found")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Unreachable as exc:
            log.warning("%s %s: monbooru unreachable: %s", method, path, exc)
            self.send_text(502, "monbooru did not answer: {}".format(exc))
        except ValueError as exc:
            self.send_text(400, str(exc))
        except Exception:
            log.exception("%s %s", method, path)
            try:
                self.send_text(500, "the plugin failed; see monbooru's log")
            except OSError:
                pass

    # -- handlers ---------------------------------------------------------

    def handle_unpair(self):
        forget_credentials()
        log.info("the pairing was removed in monbooru; offering again")
        self.send_json(200, {"status": "removed"})
        threading.Thread(target=bootstrap, name="pair", daemon=True).start()

    def handle_page(self, query):
        def one(name):
            return (query.get(name) or [""])[0]

        back = sanitize_back(one("back"))
        try:
            image_id = int(one("image"))
        except ValueError:
            self.send_html(400, render_error(
                "cannot open the image", "the page was opened without a valid image id", back))
            return
        gallery = one("gallery")
        try:
            meta = get_image(image_id, gallery)
        except (ApiError, Unreachable) as exc:
            self.send_html(502, render_error("cannot open the image", str(exc), back))
            return
        self.send_html(200, render_form(image_id, gallery, meta, load_settings(), back))

    def handle_convert(self, body):
        form = urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        back = sanitize_back(self.first(form, "back"))
        try:
            image_id = int(self.first(form, "image"))
        except ValueError:
            self.send_html(400, render_error(
                "cannot convert the image", "the form carried no valid image id", back))
            return
        gallery = self.first(form, "gallery")
        target = self.first(form, "format") or "webp"
        if target not in FORMATS:
            self.send_html(400, render_error(
                "cannot convert the image", "unknown target format: " + target, back))
            return
        try:
            quality = int(self.first(form, "quality") or 82)
        except ValueError:
            quality = 82
        quality = max(1, min(100, quality))

        def dimension(name):
            raw = self.first(form, name).strip()
            if not raw:
                return 0
            try:
                return max(0, min(32768, int(raw)))
            except ValueError:
                return 0

        width = dimension("width")
        height = dimension("height")
        submitted_width, submitted_height = width, height
        skip_batch = self.first(form, "skip_batch") == "1"
        remember = self.first(form, "remember") == "1"

        if remember:
            try:
                save_settings({"format": target, "quality": quality,
                               "skip_same": skip_batch})
            except OSError as exc:
                log.warning("could not save settings: %s", exc)

        try:
            meta = get_image(image_id, gallery)
        except (ApiError, Unreachable) as exc:
            self.send_html(502, render_error("cannot open the image", str(exc), back))
            return

        # The size box starts at the current dimensions; leaving it alone is
        # not a resize request.
        if width == (meta.get("width") or 0) and height == (meta.get("height") or 0):
            width = height = 0

        try:
            result = convert_image(image_id, target, gallery=gallery, quality=quality,
                                   width=width, height=height, skip_same=False)
        except (ConvertError, ApiError, Unreachable) as exc:
            values = {"format": target, "quality": quality, "width": submitted_width,
                      "height": submitted_height, "skip_batch": skip_batch}
            self.send_html(200, render_form(image_id, gallery, meta, load_settings(), back,
                                            error=str(exc), values=values))
            return

        if result.get("skipped"):
            self.send_html(200, render_skipped(image_id, back, target))
            return
        self.send_html(200, render_result(image_id, gallery, back, result))

    def handle_relay(self, query, body):
        settings = load_settings()
        target = (query.get("format") or [settings["format"]])[0]
        if target not in FORMATS:
            self.send_json(200, {"ok": False,
                                 "message": "unknown target format: " + target,
                                 "refresh": False})
            return
        try:
            click = json.loads(body.decode("utf-8", "replace") or "{}")
        except ValueError:
            click = {}
        gallery = str(click.get("gallery") or "")
        ids = []
        for value in click.get("image_ids") or []:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                pass
        if not ids:
            self.send_json(200, {"ok": False, "message": "no images selected",
                                 "refresh": False})
            return

        deadline = time.monotonic() + RELAY_BUDGET
        converted = skipped = failed = left = 0
        first_error = ""
        entries = []
        for index, image_id in enumerate(ids):
            if index and time.monotonic() >= deadline:
                left = len(ids) - index
                break
            try:
                result = convert_image(
                    image_id, target, gallery=gallery,
                    quality=settings["quality"], max_side=settings["max_side"],
                    skip_same=settings["skip_same"])
            except Unreachable as exc:
                failed += 1
                first_error = "monbooru did not answer"
                log.warning("relay %s #%s: monbooru unreachable: %s", target, image_id, exc)
                break
            except (ConvertError, ApiError) as exc:
                failed += 1
                first_error = first_error or str(exc)
                log.warning("relay %s #%s: %s", target, image_id, exc)
                continue
            except Exception as exc:  # one bad image must not take the batch with it
                failed += 1
                first_error = first_error or str(exc)
                log.exception("relay %s #%s", target, image_id)
                continue
            if result.get("skipped"):
                skipped += 1
            else:
                converted += 1
                entries.append((result.get("name") or "#{}".format(image_id),
                                result.get("before", 0), result.get("after", 0)))

        total = len(ids)
        tail = []
        if skipped:
            tail.append("{} already {}".format(skipped, target))
        if failed:
            tail.append("{} failed: {}".format(failed, first_error or "see the log"))
        if left:
            tail.append("{} left, click again".format(left))
        tail_text = "; ".join(tail)

        if total == 1 and converted == 1:
            name, before, after = entries[0]
            delta = percent_delta(before, after)
            head = "converted {} to {}: {} -> {}{}".format(
                name, target, human_size(before), human_size(after),
                " (" + delta + ")" if delta else "")
            message = head
        else:
            head = "converted {}/{} to {}".format(converted, total, target)
            message = head
            if entries:
                # Listed by name and result size, as many as monbooru's one
                # flash line (200 characters) holds.
                for limit in (5, 4, 3, 2, 1):
                    shown = entries[:limit]
                    piece = " · ".join(
                        "{} {}".format(_short_name(name), human_size(after))
                        for name, _before, after in shown)
                    if len(entries) > limit:
                        piece += " · +{} more".format(len(entries) - limit)
                    candidate = head + ": " + piece
                    if tail_text:
                        candidate += "; " + tail_text
                    if len(candidate) <= 200 or limit == 1:
                        message = head + ": " + piece
                        break
        if tail_text:
            message += "; " + tail_text
        self.send_json(200, {
            "ok": converted > 0 or (failed == 0 and skipped > 0),
            "message": message[:200],
            "refresh": converted > 0,
        })

    @staticmethod
    def first(form, name):
        return (form.get(name) or [""])[0]


# ---------------------------------------------------------------------------
# startup and shutdown
# ---------------------------------------------------------------------------


def watch_stdin():
    """monbooru stops a managed plugin by closing its stdin."""
    try:
        if sys.stdin is None:
            return
        while True:
            chunk = sys.stdin.buffer.read(1024)
            if not chunk:
                break
    except Exception:
        pass
    log.info("stdin closed; stopping")
    STOP.set()


def install_signal_handlers():
    def stop(signum, _frame):
        log.info("signal %d; stopping", signum)
        STOP.set()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, stop)
        except (ValueError, OSError):
            pass


def main(argv=None):
    global MONBOORU, SELF_URL, STATE_DIR

    parser = argparse.ArgumentParser(description="convert monbooru images between formats")
    parser.add_argument("--monbooru", default=os.environ.get("MONBOORU_URL") or "http://127.0.0.1:8455",
                        help="monbooru base url (default: $MONBOORU_URL or http://127.0.0.1:8455)")
    parser.add_argument("--addr", default="127.0.0.1", help="address to serve on")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to serve on")
    parser.add_argument("--url", default="",
                        help="address monbooru should call the plugin at")
    parser.add_argument("--state", default=".",
                        help="directory holding credentials.json and settings.json")
    args = parser.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(message)s")

    MONBOORU = args.monbooru.rstrip("/")
    STATE_DIR = args.state or "."
    host = "127.0.0.1" if args.addr in ("0.0.0.0", "::") else args.addr
    SELF_URL = (args.url or "http://{}:{}".format(host, args.port)).rstrip("/")

    try:
        server = ThreadingHTTPServer((args.addr, args.port), Handler)
    except OSError as exc:
        log.error("cannot listen on %s:%s: %s", args.addr, args.port, exc)
        return 1
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    log.info("%s %s listening on %s:%s", APP, VERSION, args.addr, args.port)

    if sys.stdin is not None and not sys.stdin.isatty():
        threading.Thread(target=watch_stdin, name="stdin", daemon=True).start()
    install_signal_handlers()
    threading.Thread(target=bootstrap, name="pair", daemon=True).start()

    STOP.wait()
    log.info("shutting down")
    server.shutdown()
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

