#!/usr/bin/env python3
"""Fetch a web page and extract readable text content.
Claude calls this via shell to read URLs found in emails.

Usage:
  python fetch_url.py --url "https://example.com/program" [--max-chars 4000] [--js]
  python fetch_url.py --url "https://example.com/photo.jpg" --save-image <path>

A page read returns the text, the action links and the page's images
(og:image, then <img> sources with their alt text), so an image's own URL
can be found from the page that shows it. --save-image saves one image:
the same address policy and log as a page read, then only a PNG, JPEG,
GIF or WebP by its first bytes (never SVG, which can carry script), at
most MAX_IMAGE_BYTES, written to a new file (never over one, never
through a symlink, mode 0644) in a deck's folder or ~/.ea/state.

Safety: this is the EA's only general-purpose outbound-network primitive
and the URLs it receives come from untrusted email, so it enforces its
own address policy:
  * only http/https,
  * the host must resolve to a globally routable address (no loopback,
    RFC1918, link-local/169.254.169.254, CGNAT/Tailscale 100.64/10,
    unique-local IPv6, IPv4-mapped forms of any of those),
  * redirects are walked one hop at a time and re-checked, never
    followed by requests itself,
  * DNS is pinned to the validated addresses for the life of the
    request, so a rebind between check and connect cannot land on an
    internal service,
  * every fetch (URL, resolved IP, outcome) is appended to
    ~/.ea/logs/fetch-url.log for the sentinel.
"""

import argparse
import contextlib
import hashlib
import ipaddress
import json
import os
import re
import socket
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024  # 2 MB cap on raw HTML
MAX_IMAGE_BYTES = 15 * 1024 * 1024    # cap on one saved image
MAX_PAGE_IMAGES = 15
# Where --save-image may write: the decks' folders and the EA's state.
DECKS_ROOT = Path(os.environ.get("EA_DECKS_ROOT", str(Path.home() / "presentations"))) / "decks"
SAVE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
ACTION_LINK_KEYWORDS = re.compile(
    r"apply|submit|register|sign.?up|enroll|deadline|application|nomination|rsvp",
    re.IGNORECASE,
)

ALLOWED_SCHEMES = ("http", "https")
MAX_REDIRECTS = 5
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
LOG_PATH = EA_DIR / "logs" / "fetch-url.log"


class BlockedURL(Exception):
    """The URL failed the scheme/address policy. Nothing was fetched."""


# ── Audit log ───────────────────────────────────────────────────────────

def _log_event(event: str, **fields) -> None:
    """Append one JSONL record. Never raises: logging must not break a fetch."""
    record = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "event": event}
    record.update({k: v for k, v in fields.items() if v is not None})
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass


# ── Address policy ──────────────────────────────────────────────────────

def _classify_address(ip):
    """Reason the address is off limits, or None when it is fetchable.

    `is_global` is the gate (it already covers RFC1918, loopback,
    link-local, CGNAT 100.64/10, reserved and documentation space on
    both families); the named checks run first only to give the log a
    useful reason string.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        # An IPv4 address wearing a v6 hat still reaches the v4 host.
        mapped = ip.ipv4_mapped or ip.sixtofour
        if mapped is None and ip.teredo:
            mapped = ip.teredo[1]
        if mapped is not None:
            reason = _classify_address(mapped)
            return f"{reason} (IPv4-in-IPv6 {mapped})" if reason else None
    if str(ip) in ("169.254.169.254", "fd00:ec2::254"):
        return "cloud metadata service"
    if ip.is_unspecified:
        return "wildcard address"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_private:
        return "private address"
    if ip.is_reserved:
        return "reserved address"
    if not ip.is_global:
        return "non-routable address"
    return None


def _check_literal(host: str):
    """The host as an IP object when it is an IP literal, else None."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def validate_url(url: str) -> dict:
    """Scheme + address policy for one URL.

    Returns {url, scheme, host, port, ips, addrinfo}. Raises BlockedURL
    when policy refuses it, requests.ConnectionError when DNS fails.
    """
    parts = urlsplit((url or "").strip())
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedURL(f"scheme {scheme or '(none)'!r} not allowed "
                         f"(only http/https)")
    host = parts.hostname
    if not host:
        raise BlockedURL("URL has no host")
    port = parts.port or (443 if scheme == "https" else 80)

    literal = _check_literal(host)
    if literal is not None:
        reason = _classify_address(literal)
        if reason:
            raise BlockedURL(f"{host} is a {reason}")
        family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
        sockaddr = ((str(literal), port, 0, 0) if literal.version == 6
                    else (str(literal), port))
        return {"url": url, "scheme": scheme, "host": host, "port": port,
                "ips": [str(literal)],
                "addrinfo": [(family, socket.SOCK_STREAM,
                              socket.IPPROTO_TCP, "", sockaddr)]}

    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM,
                                   socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise requests.ConnectionError(f"DNS resolution failed for {host}: {e}")
    if not infos:
        raise requests.ConnectionError(f"DNS resolution failed for {host}")

    ips = []
    for family, socktype, proto, canon, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        reason = _classify_address(ip)
        if reason:
            # ANY bad answer blocks: a rotating record must not get in
            # by luck of the draw.
            raise BlockedURL(f"{host} resolves to {ip} ({reason})")
        ips.append(str(ip))
    return {"url": url, "scheme": scheme, "host": host, "port": port,
            "ips": ips, "addrinfo": infos}


@contextlib.contextmanager
def pinned_dns(host: str, addrinfo: list):
    """Force resolution of `host` to the addresses we just validated.

    Closes the check-then-connect window: urllib3 calls
    socket.getaddrinfo at connect time, and without this a hostile
    nameserver can answer publicly for our check and privately for the
    connection (DNS rebinding).
    """
    real = socket.getaddrinfo

    def fake(h, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        if h != host:
            return real(h, port, family, type, proto, flags)
        entries = list(addrinfo)
        if family not in (0, socket.AF_UNSPEC):
            narrowed = [e for e in entries if e[0] == family]
            if narrowed:
                entries = narrowed
        return entries

    socket.getaddrinfo = fake
    try:
        yield
    finally:
        socket.getaddrinfo = real


# ── Fetchers ────────────────────────────────────────────────────────────

def fetch_with_requests(url: str, timeout: int = 15,
                        max_redirects: int = MAX_REDIRECTS,
                        max_bytes: int = MAX_DOWNLOAD_BYTES) -> requests.Response:
    """GET `url`, validating every redirect hop. Redirects are walked by
    hand (allow_redirects=False) so a public URL cannot bounce us into an
    internal service."""
    current = url
    for hop in range(max_redirects + 1):
        info = validate_url(current)
        with pinned_dns(info["host"], info["addrinfo"]):
            resp = requests.get(
                current,
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
                stream=True,
                allow_redirects=False,
            )
        _log_event("fetch", url=current, host=info["host"],
                   ip=info["ips"][0], ips=info["ips"], method="requests",
                   status=resp.status_code, hop=hop)
        if resp.status_code in REDIRECT_STATUSES:
            location = resp.headers.get("Location", "")
            resp.close()
            if not location:
                raise requests.ConnectionError(
                    f"HTTP {resp.status_code} with no Location header")
            current = urljoin(current, location)
            continue
        resp.raise_for_status()
        chunks = []  # capped at max_bytes
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                break
        resp._content = b"".join(chunks)
        return resp
    raise BlockedURL(f"more than {max_redirects} redirects")


def _playwright_route_guard():
    """Abort any browser request that fails the same address policy.

    Covers subresources and in-page navigations, which --host-resolver-rules
    does not."""
    cache: dict = {}

    def guard(route, request):
        url = request.url
        key = urlsplit(url)[:2]
        verdict = cache.get(key)
        if verdict is None:
            try:
                validate_url(url)
                verdict = ""
            except BlockedURL as e:
                verdict = str(e)
            except Exception:
                verdict = ""  # DNS failure: let the browser report it
            cache[key] = verdict
        try:
            if verdict:
                _log_event("blocked", url=url, reason=verdict,
                           method="playwright", context="subrequest")
                route.abort()
            else:
                route.continue_()
        except Exception:
            pass  # page navigated away mid-route

    return guard


def fetch_with_playwright(url: str, timeout: int = 20000) -> str:
    info = validate_url(url)
    from playwright.sync_api import sync_playwright

    pin_ip = info["ips"][0]
    if ":" in pin_ip:
        pin_ip = f"[{pin_ip}]"
    args = [f"--host-resolver-rules=MAP {info['host']} {pin_ip}"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=args)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.route("**/*", _playwright_route_guard())
            page.goto(url, wait_until="networkidle", timeout=timeout)
            html = page.content()
        finally:
            browser.close()
    _log_event("fetch", url=url, host=info["host"], ip=info["ips"][0],
               ips=info["ips"], method="playwright", status=None)
    return html


def extract(html: str, max_chars: int):
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None

    meta_desc = None
    meta_tag = soup.find("meta", attrs={"name": "description"})
    if meta_tag and meta_tag.get("content"):
        meta_desc = meta_tag["content"].strip()

    for tag_name in ("script", "style", "nav", "footer", "header", "aside", "iframe", "noscript"):
        for el in soup.find_all(tag_name):
            el.decompose()

    content_el = (
        soup.find("main")
        or soup.find("article")
        or soup.find("div", attrs={"role": "main"})
        or soup.find("body")
    )

    if not content_el:
        return title, meta_desc, "", [], 0, False

    # Action links are read off the DOM before the text conversion.
    links = []
    seen_hrefs = set()
    for a in content_el.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        # Keep links with action-oriented text or href keywords
        if ACTION_LINK_KEYWORDS.search(text) or ACTION_LINK_KEYWORDS.search(href):
            if href not in seen_hrefs:
                seen_hrefs.add(href)
                links.append({"text": text[:120], "href": href[:500]})
                if len(links) >= 10:
                    break

    raw_text = content_el.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", raw_text)
    full_length = len(text)

    # Truncate at sentence/paragraph boundary
    truncated = False
    if len(text) > max_chars:
        cut = text[:max_chars]
        last_break = max(cut.rfind(". "), cut.rfind(".\n"), cut.rfind("\n\n"))
        if last_break > max_chars * 0.5:
            text = text[: last_break + 1]
        else:
            text = cut
        truncated = True

    return title, meta_desc, text, links, full_length, truncated


def page_images(html: str, base: str) -> list[dict]:
    """The page's images, the share image first: [{src, alt}], absolute
    http(s) URLs only, at most MAX_PAGE_IMAGES."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()

    def add(src, alt=""):
        src = urljoin(base, (src or "").strip())
        if urlsplit(src).scheme not in ALLOWED_SCHEMES or src in seen:
            return
        seen.add(src)
        out.append({"src": src, "alt": " ".join(str(alt or "").split())[:120]})

    for prop in ("og:image", "og:image:url", "twitter:image"):
        m = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if m and m.get("content"):
            add(m["content"], "share image")
    for img in soup.find_all("img"):
        add(img.get("src") or img.get("data-src"), img.get("alt"))
        if len(out) >= MAX_PAGE_IMAGES:
            break
    return out[:MAX_PAGE_IMAGES]


def image_kind(data: bytes) -> str:
    """The file suffix for a PNG, JPEG, GIF or WebP by its first bytes;
    '' for anything else."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ""


def _save_roots() -> list[Path]:
    roots = []
    for r in (DECKS_ROOT, EA_DIR / "state"):
        try:
            roots.append(r.resolve(strict=True))
        except (OSError, RuntimeError):
            continue
    return roots


def save_image(url: str, dest: str) -> dict:
    """Fetch one image under the address policy and write it to a new
    file at `dest` (the suffix follows the image's real type)."""
    result = {"url": url, "saved": None, "error": None}
    target = Path(dest).expanduser()
    if not SAVE_NAME_RE.match(target.name):
        result["error"] = "the file name may use letters, digits, '.', '_' and '-' only"
        return result
    try:
        folder = target.parent.resolve(strict=True)
    except (OSError, RuntimeError):
        result["error"] = f"no such folder: {target.parent}"
        return result
    if not any(folder.is_relative_to(r) for r in _save_roots()):
        result["error"] = (f"{target.parent} is outside the folders an image may be saved in "
                           f"({DECKS_ROOT}, {EA_DIR / 'state'})")
        return result
    try:
        info = validate_url(url)
        resp = fetch_with_requests(url, max_bytes=MAX_IMAGE_BYTES + 1)
    except BlockedURL as e:
        _log_event("blocked", url=url, reason=str(e), method="save-image")
        result["error"] = f"Blocked: {e}"
        return result
    except requests.HTTPError as e:
        result["error"] = f"HTTP {e.response.status_code}"
        _log_event("error", url=url, method="save-image", reason=result["error"])
        return result
    except (requests.RequestException, OSError) as e:
        result["error"] = f"Fetch failed: {e}"
        _log_event("error", url=url, method="save-image", reason=str(e))
        return result
    data = resp.content or b""
    ctype = resp.headers.get("content-type", "")
    if len(data) > MAX_IMAGE_BYTES:
        result["error"] = f"over {MAX_IMAGE_BYTES // (1024 * 1024)} MB"
        _log_event("refused", url=url, method="save-image", reason=result["error"])
        return result
    kind = image_kind(data)
    if not kind:
        result["error"] = (f"not a PNG, JPEG, GIF or WebP image (content-type {ctype or 'none'}); "
                           "for a page, read it and take an image URL from `images`")
        _log_event("refused", url=url, method="save-image", reason="not an image", ctype=ctype)
        return result
    final = folder / (target.stem + kind)
    try:
        fd = os.open(final, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        result["error"] = f"{final} exists; pick another name"
        return result
    except OSError as e:
        result["error"] = f"cannot write {final}: {e}"
        return result
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    digest = hashlib.sha256(data).hexdigest()
    _log_event("saved", url=url, ip=info["ips"][0], method="save-image", bytes=len(data),
               type=kind[1:], sha256=digest, dest=str(final))
    result.update(saved=str(final), bytes=len(data), type=kind[1:], sha256=digest)
    return result


def run(url: str, max_chars: int, force_js: bool):
    result = {
        "url": url,
        "title": None,
        "meta_description": None,
        "content": None,
        "content_length": 0,
        "truncated": False,
        "links": [],
        "images": [],
        "method": None,
        "error": None,
    }

    html = None
    method = "requests"

    if url.lower().endswith(".pdf"):
        result["error"] = "URL points to a PDF. Download it or use scan-attachments instead."
        return result

    # Scheme + address policy before anything opens a socket or a browser.
    try:
        info = validate_url(url)
    except BlockedURL as e:
        _log_event("blocked", url=url, reason=str(e),
                   method="playwright" if force_js else "requests")
        result["error"] = f"Blocked: {e}"
        return result
    except requests.ConnectionError as e:
        _log_event("error", url=url, reason=str(e), outcome="dns")
        result["error"] = f"Connection failed: {e}"
        return result
    resolved_ip = info["ips"][0]

    if force_js:
        method = "playwright"
        try:
            html = fetch_with_playwright(url)
        except ImportError:
            result["error"] = "Playwright not installed. Cannot render JS pages."
            _log_event("error", url=url, preflight_ip=resolved_ip, method=method,
                       reason=result["error"])
            return result
        except BlockedURL as e:
            result["error"] = f"Blocked: {e}"
            _log_event("blocked", url=url, reason=str(e), method=method)
            return result
        except Exception as e:
            result["error"] = f"Playwright error: {e}"
            _log_event("error", url=url, preflight_ip=resolved_ip, method=method,
                       reason=str(e))
            return result
    else:
        try:
            resp = fetch_with_requests(url)
            content_type = resp.headers.get("content-type", "")
            if "application/pdf" in content_type:
                result["error"] = "URL returned a PDF. Download it or use scan-attachments instead."
                _log_event("done", url=url, preflight_ip=resolved_ip, method=method,
                           outcome="pdf")
                return result
            html = resp.text
        except BlockedURL as e:
            result["error"] = f"Blocked: {e}"
            _log_event("blocked", url=url, reason=str(e), method=method)
            return result
        except requests.Timeout:
            result["error"] = "Timeout after 15s"
            _log_event("error", url=url, preflight_ip=resolved_ip, method=method,
                       reason="timeout")
            return result
        except requests.HTTPError as e:
            result["error"] = f"HTTP {e.response.status_code}"
            _log_event("error", url=url, preflight_ip=resolved_ip, method=method,
                       reason=result["error"])
            return result
        except requests.ConnectionError as e:
            result["error"] = f"Connection failed: {e}"
            _log_event("error", url=url, preflight_ip=resolved_ip, method=method,
                       reason=str(e))
            return result
        except Exception as e:
            result["error"] = str(e)
            _log_event("error", url=url, preflight_ip=resolved_ip, method=method,
                       reason=str(e))
            return result

    extracted = extract(html, max_chars)
    title, meta_desc, text, links, full_length, truncated = extracted

    # Auto-fallback to Playwright if content is suspiciously short or empty
    has_scripts = "<script" in html.lower()
    empty_response = len(html.strip()) < 500
    if len(text) < 200 and (has_scripts or empty_response) and not force_js:
        try:
            html = fetch_with_playwright(url)
            extracted = extract(html, max_chars)
            title, meta_desc, text, links, full_length, truncated = extracted
            method = "playwright"
        except Exception:
            pass  # Keep the requests result

    result["title"] = title
    result["meta_description"] = meta_desc
    result["content"] = text
    result["content_length"] = full_length
    result["truncated"] = truncated
    result["links"] = links
    try:
        result["images"] = page_images(html, url)
    except Exception:
        result["images"] = []
    result["method"] = method

    if len(text) < 50:
        result["warning"] = "Very little content extracted. Page may require login or is mostly images/JS."

    _log_event("done", url=url, preflight_ip=resolved_ip, method=method, outcome="ok",
               chars=full_length)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch and extract web page content", allow_abbrev=False)
    parser.add_argument("--url", required=True, help="URL to fetch")
    parser.add_argument("--max-chars", type=int, default=4000,
                        help="Max characters of extracted text (default 4000)")
    parser.add_argument("--js", action="store_true",
                        help="Force Playwright rendering for JS-heavy pages")
    parser.add_argument("--save-image", metavar="PATH",
                        help="save the image at --url to a new file (a deck's folder or ~/.ea/state)")
    args = parser.parse_args()

    try:
        output = (save_image(args.url, args.save_image) if args.save_image
                  else run(args.url, args.max_chars, args.js))
    except Exception as e:
        output = {"url": args.url, "error": f"Unexpected error: {e}"}

    print(json.dumps(output, indent=2))
