# Google Ads Transparency Center scraper - IMAGE ADS ONLY
# This version intentionally ignores video detection and text/headline extraction.
# It writes Headline/Description as N/A and extracts only:
# Advertiser, package/app link, current transparency URL, image ad URL.

from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs, unquote, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import threading
import time
import re
import sheets


MAX_WORKERS = 2
SHEET_LOCK = threading.Lock()

IMAGE_CONTENT_TYPES = ("image/",)


# =========================
# SAFE SHEET WRITERS
# =========================

def safe_update_combined_row(row_num, data):
    with SHEET_LOCK:
        sheets.update_combined_row(row_num, data)


def safe_update_headline_desc(row_num, headline, description):
    with SHEET_LOCK:
        sheets.update_headline_and_description(row_num, headline, description)


def safe_update_image_url(row_num, image_url):
    with SHEET_LOCK:
        sheets.update_image_url(row_num, image_url)


def safe_add_log(row_number, status, log_type, url="", video_id="", app_link="", message=""):
    with SHEET_LOCK:
        sheets.add_log(
            row_number=row_number,
            status=status,
            log_type=log_type,
            url=url,
            video_id=video_id,
            app_link=app_link,
            message=message,
        )


def get_exact_time():
    return datetime.now().strftime("%I:%M:%S %p")


def clean_text(value):
    if not value:
        return "N/A"
    return re.sub(r"\s+", " ", str(value)).strip() or "N/A"


def ensure_region_anywhere(url):
    if "region=" in url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}region=anywhere"


def extract_creative_id(url):
    match = re.search(r"/creative/([^/?#]+)", url or "", flags=re.I)
    return match.group(1) if match else "N/A"


# =========================
# URL / PACKAGE HELPERS
# =========================

def decode_all(text):
    if not text:
        return ""
    text = str(text)
    text = re.sub(r"\\x3[Dd]", "=", text)
    text = re.sub(r"\\x26", "&", text)
    text = re.sub(r"\\x3[Ff]", "?", text)
    text = re.sub(r"\\x2[Ff]", "/", text)
    text = re.sub(r"\\u003[Dd]", "=", text)
    text = re.sub(r"\\u0026", "&", text)
    text = re.sub(r"\\u003[Ff]", "?", text)
    text = re.sub(r"%3[Dd]", "=", text, flags=re.I)
    text = re.sub(r"%26", "&", text, flags=re.I)
    text = re.sub(r"%3[Ff]", "?", text, flags=re.I)
    text = re.sub(r"%2[Ff]", "/", text, flags=re.I)
    text = re.sub(r"%3[Aa]", ":", text, flags=re.I)
    text = (
        text.replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#38;", "&")
        .replace("&#61;", "=")
        .replace("&#x3D;", "=")
        .replace("&#x26;", "&")
    )
    try:
        text = unquote(text)
    except Exception:
        pass
    return text


def clean_googleadservices_link(href):
    """Unwrap Google ad click URLs when destination exists in query params."""
    if not href:
        return "N/A"

    href = str(href).strip()
    if href.startswith("//"):
        href = "https:" + href

    # Unwrap multiple redirect layers when possible.
    current = href
    for _ in range(5):
        try:
            decoded_current = decode_all(current)
            parsed = urlparse(decoded_current)
            query = parse_qs(parsed.query)

            for key in ["adurl", "url", "q", "u", "ds_dest_url", "destination", "dest", "redirect"]:
                value = query.get(key, [None])[0]
                if value:
                    value = decode_all(value).strip()
                    if value and value != current:
                        current = value
                        break
            else:
                return decoded_current
        except Exception:
            return current

    return current


def is_good_app_link(href):
    if not href:
        return False
    href = decode_all(href).lower()
    return (
        "googleadservices.com/pagead/aclk" in href
        or "play.google.com/store/apps/details" in href
        or "market://" in href
        or "apps.apple.com" in href
        or "itunes.apple.com" in href
    )


def extract_package_name(app_link):
    if not app_link or app_link == "N/A":
        return "N/A"

    try:
        decoded = decode_all(app_link)
        parsed = urlparse(decoded)
        query = parse_qs(parsed.query)

        for key in ["id", "package", "appId", "appid"]:
            value = query.get(key, [None])[0]
            if value and _is_valid_pkg(value):
                return value

        # Sometimes the destination URL is still embedded inside the string.
        pkg_candidates = extract_packages_from_text(decoded)
        if pkg_candidates:
            return sorted(pkg_candidates)[0]

        if "apps.apple.com" in decoded.lower() or "itunes.apple.com" in decoded.lower():
            match = re.search(r"/id(\d+)", decoded)
            if match:
                return f"id{match.group(1)}"
    except Exception:
        pass

    return "N/A"


_SKIP_EXT = re.compile(
    r"\.(jpg|jpeg|png|gif|webp|svg|ico|css|js|json|xml|html|htm|"
    r"woff|woff2|ttf|otf|eot|pdf|zip|apk|mp4|mp3|ogg|m3u8)$",
    re.I,
)
_SKIP_PFX = re.compile(
    r"^(com\.google\.android\.(gms|vending|inputmethod|tts|webview)|"
    r"com\.android\.|android\.|androidx\.|kotlin\.|kotlinx\.|"
    r"com\.squareup\.|io\.reactivex\.|okhttp3\.|javax\.|java\.|"
    r"org\.json\.|org\.apache\.)",
    re.I,
)


def _is_valid_pkg(pkg):
    if not pkg:
        return False
    pkg = str(pkg).strip().rstrip(".,;'\"\\ ")
    parts = pkg.split(".")
    if len(parts) < 3 or len(pkg) < 8:
        return False
    if _SKIP_EXT.search(pkg):
        return False
    if _SKIP_PFX.match(pkg):
        return False
    for part in parts:
        if not part or not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", part):
            return False
    return True


def extract_packages_from_text(raw_text):
    text = decode_all(raw_text)
    candidates = set()

    patterns = [
        r"""['\"]appId['\"]\s*:\s*['\"]([A-Za-z][\w.]+)['\"]""",
        r"""['\"]applicationId['\"]\s*:\s*['\"]([A-Za-z][\w.]+)['\"]""",
        r"""['\"]packageName['\"]\s*:\s*['\"]([A-Za-z][\w.]+)['\"]""",
        r"""['\"]androidPackageName['\"]\s*:\s*['\"]([A-Za-z][\w.]+)['\"]""",
        r"""play\.google\.com/store/apps/details[^\s'\"<>]*[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})""",
        r"""market://[^\s'\"]*[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})""",
        r"""[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})""",
        r"""[?&]package=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})""",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            pkg = match.group(1).rstrip(".,;'\"\\ ")
            if _is_valid_pkg(pkg):
                candidates.add(pkg)

    return candidates


# =========================
# ADVERTISER
# =========================

def extract_advertiser_from_page(page):
    try:
        loc = page.locator('.advertiser-title, [data-test-id="advertiser-name"]').first
        loc.wait_for(timeout=4000)
        text = loc.inner_text().strip()
        if text and len(text) > 1 and "Sign in" not in text:
            return clean_text(text)
    except Exception:
        pass

    js = r"""
    () => {
        const badWords = [
            'sign in', 'log in', 'home', 'menu', 'search', 'help', 'privacy',
            'terms', 'ad details', 'see more ads', 'ads transparency'
        ];
        let maxFont = 0;
        let advertiserName = "N/A";

        for (const el of document.querySelectorAll('body *')) {
            if (el.childElementCount > 0) continue;
            const txt = (el.innerText || '').trim();
            const lower = txt.toLowerCase();
            if (txt.length < 2 || txt.length > 80 || badWords.some(b => lower.includes(b))) continue;

            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0 || rect.y < 0 || rect.y > 360 || rect.width < 10) continue;

            const style = window.getComputedStyle(el);
            if (style.opacity === '0' || style.display === 'none' || style.visibility === 'hidden') continue;

            const font = parseFloat(style.fontSize || '0');
            if (font > maxFont) {
                maxFont = font;
                advertiserName = txt;
            }
        }
        return advertiserName;
    }
    """
    try:
        value = page.evaluate(js)
        return clean_text(value)
    except Exception:
        return "N/A"


# =========================
# ACTIVE CREATIVE TARGET + IMAGE URL
# =========================

def _normalize_image_url(raw_url, base_url=""):
    if not raw_url:
        return "N/A"

    raw_url = str(raw_url).strip().strip('"\'')
    if not raw_url or raw_url.lower() in {"none", "null", "undefined"}:
        return "N/A"

    if raw_url.lower().startswith("data:image"):
        return "N/A"

    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url

    if base_url and not raw_url.lower().startswith(("http://", "https://", "blob:")):
        try:
            raw_url = urljoin(base_url, raw_url)
        except Exception:
            pass

    return raw_url or "N/A"


def is_bad_image_url(url):
    if not url:
        return True
    lower = url.lower()

    bad_parts = [
        "googlelogo",
        "google_logo",
        "branding/google",
        "favicon",
        "adchoices",
        "doubleclick.net/pagead/images/adchoices",
        "fonts.gstatic.com",
        "gstatic.com/images/icons",
        "ssl.gstatic.com/ui/",
        "www.gstatic.com/images/branding",
        "transparent_pixel",
        "1x1",
        "pixel",
        "gen_204",
    ]
    if any(part in lower for part in bad_parts):
        return True

    if lower.startswith("data:image") or lower.startswith("blob:"):
        return True

    return False


def image_url_priority(url):
    if not url:
        return 0
    lower = url.lower()
    score = 0

    # Most common Google display creative image hosts.
    if "tpc.googlesyndication.com/simgad" in lower:
        score += 250000
    if "googlesyndication.com" in lower:
        score += 130000
    if "googleusercontent.com" in lower or "ggpht.com" in lower:
        score += 65000
    if "gstatic.com" in lower:
        score += 25000
    if "encrypted-tbn" in lower:
        score += 20000
    if "play-lh.googleusercontent.com" in lower:
        score += 8000

    # Avoid app-store badges/icons beating the real creative.
    if "badge" in lower or "store_badge" in lower:
        score -= 60000
    if "logo" in lower:
        score -= 35000
    if "icon" in lower:
        score -= 15000

    return score


def extract_best_image_data_from_target(target):
    """
    Returns best visible creative image inside exactly this page/frame.
    It reads normal img/srcset, picture source, svg image, and CSS background images.
    Shadow DOM is included because some Google ad previews render there.
    """
    js = r"""
    () => {
        const absUrl = (raw) => {
            if (!raw) return '';
            raw = String(raw).trim().replace(/^['"]|['"]$/g, '');
            if (!raw || raw === 'none' || raw === 'null' || raw === 'undefined') return '';
            if (raw.startsWith('data:image') || raw.startsWith('blob:')) return '';
            try { return new URL(raw, location.href).href; } catch (e) { return raw; }
        };

        const pickBestFromSrcset = (srcset) => {
            if (!srcset) return '';
            let bestUrl = '';
            let bestScore = -1;
            for (const rawPart of String(srcset).split(',')) {
                const part = rawPart.trim();
                if (!part) continue;
                const pieces = part.split(/\s+/).filter(Boolean);
                const url = pieces[0] || '';
                const descriptor = pieces[1] || '';
                let score = 1;
                if (descriptor.endsWith('w')) score = parseFloat(descriptor) || 1;
                if (descriptor.endsWith('x')) score = (parseFloat(descriptor) || 1) * 1000;
                if (score >= bestScore) {
                    bestScore = score;
                    bestUrl = url;
                }
            }
            return bestUrl;
        };

        const isVisibleBox = (el, minW = 40, minH = 40) => {
            if (!el || !el.getBoundingClientRect) return null;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            if (rect.width < minW || rect.height < minH) return null;
            if (rect.bottom <= 0 || rect.right <= 0 || rect.top >= window.innerHeight || rect.left >= window.innerWidth) return null;
            if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') return null;
            return rect;
        };

        const badImage = (url, el) => {
            const lower = String(url || '').toLowerCase();
            const alt = String(el?.getAttribute?.('alt') || '').toLowerCase();
            if (!lower) return true;
            if (lower.startsWith('data:image') || lower.startsWith('blob:')) return true;
            if (lower.includes('googlelogo') || alt.includes('google')) return true;
            if (lower.includes('favicon') || lower.endsWith('/favicon.ico')) return true;
            if (lower.includes('adchoices')) return true;
            if (lower.includes('transparent_pixel') || lower.includes('gen_204')) return true;
            if (lower.includes('fonts.gstatic.com')) return true;
            if (lower.includes('www.gstatic.com/images/branding')) return true;
            return false;
        };

        const all = [];
        const walk = (root) => {
            if (!root || !root.querySelectorAll) return;
            for (const el of Array.from(root.querySelectorAll('*'))) {
                all.push(el);
                if (el.shadowRoot) walk(el.shadowRoot);
            }
        };
        walk(document);

        const candidates = [];

        const urlPriority = (url) => {
            const lower = String(url || '').toLowerCase();
            let score = 0;
            if (lower.includes('tpc.googlesyndication.com/simgad')) score += 250000;
            if (lower.includes('googlesyndication.com')) score += 130000;
            if (lower.includes('googleusercontent.com') || lower.includes('ggpht.com')) score += 65000;
            if (lower.includes('gstatic.com')) score += 25000;
            if (lower.includes('encrypted-tbn')) score += 20000;
            if (lower.includes('play-lh.googleusercontent.com')) score += 8000;
            if (lower.includes('badge') || lower.includes('store_badge')) score -= 60000;
            if (lower.includes('logo')) score -= 35000;
            if (lower.includes('icon')) score -= 15000;
            return score;
        };

        const addCandidate = (rawUrl, el, kind, bonus = 0) => {
            const url = absUrl(rawUrl);
            if (!url || badImage(url, el)) return;
            const rect = isVisibleBox(el);
            if (!rect) return;

            const area = rect.width * rect.height;
            let score = area + bonus + urlPriority(url);

            if (rect.width >= 250 && rect.height >= 100) score += 30000;
            if (rect.width >= 300 && rect.height >= 250) score += 45000;
            if (rect.top >= -20 && rect.top <= 700) score += 10000;
            if (kind.includes('currentSrc')) score += 7000;
            if (kind.includes('srcset')) score += 6000;
            if (kind.includes('background')) score += 3500;

            // Penalize tiny/square logo-like assets unless they are the only option.
            const ratio = rect.width / Math.max(1, rect.height);
            if (area < 12000) score -= 30000;
            if (ratio > 0.75 && ratio < 1.35 && area < 25000) score -= 25000;

            candidates.push({
                url,
                kind,
                score,
                area,
                top: rect.top,
                bottom: rect.bottom,
                left: rect.left,
                right: rect.right,
                width: rect.width,
                height: rect.height
            });
        };

        for (const img of all.filter(el => el.tagName && el.tagName.toLowerCase() === 'img')) {
            addCandidate(img.currentSrc, img, 'img-currentSrc', 9000);
            addCandidate(img.getAttribute('src'), img, 'img-src', 7500);
            addCandidate(pickBestFromSrcset(img.getAttribute('srcset')), img, 'img-srcset', 8500);

            for (const attr of ['data-src', 'data-lazy-src', 'data-original', 'data-image', 'data-image-url', 'data-thumbnail-url', 'data-iurl']) {
                addCandidate(img.getAttribute(attr), img, `img-${attr}`, 4500);
            }
        }

        for (const source of all.filter(el => el.tagName && el.tagName.toLowerCase() === 'source' && el.getAttribute('srcset'))) {
            const picture = source.closest('picture');
            const visualEl = picture?.querySelector('img') || picture || source;
            addCandidate(pickBestFromSrcset(source.getAttribute('srcset')), visualEl, 'source-srcset', 7000);
        }

        for (const svgImage of all.filter(el => el.tagName && el.tagName.toLowerCase() === 'image')) {
            addCandidate(svgImage.getAttribute('href') || svgImage.getAttribute('xlink:href'), svgImage, 'svg-image', 5000);
        }

        for (const el of all) {
            const rect = isVisibleBox(el, 80, 60);
            if (!rect) continue;
            const bg = window.getComputedStyle(el).backgroundImage || '';
            if (!bg || bg === 'none' || !bg.includes('url(')) continue;

            const matches = Array.from(bg.matchAll(/url\((['"]?)(.*?)\1\)/g));
            for (const match of matches) {
                addCandidate(match[2], el, 'background-image', 6500);
            }
        }

        if (!candidates.length) return null;

        const deduped = [];
        const seen = new Set();
        for (const c of candidates) {
            if (seen.has(c.url)) continue;
            seen.add(c.url);
            deduped.push(c);
        }

        deduped.sort((a, b) => b.score - a.score);
        return deduped[0] || null;
    }
    """
    try:
        data = target.evaluate(js)
        if not data:
            return None
        base_url = getattr(target, "url", "") or ""
        data["url"] = _normalize_image_url(data.get("url"), base_url=base_url)
        if data["url"] == "N/A" or is_bad_image_url(data["url"]):
            return None
        return data
    except Exception:
        return None


def _frame_parent_box(frame):
    try:
        iframe_el = frame.frame_element()
        box = iframe_el.bounding_box()
        if not box:
            return None
        return box
    except Exception:
        return None


def get_ranked_image_targets(page):
    """
    Finds the active creative frame/page by ranking visible image candidates.
    This avoids using random text/page chrome and keeps everything tied to one opened creative URL.
    """
    ranked = []

    for frame in page.frames:
        if frame == page.main_frame:
            continue

        image_data = extract_best_image_data_from_target(frame)
        if not image_data:
            continue

        parent_bonus = 0
        box = _frame_parent_box(frame)
        if box:
            width = box.get("width", 0) or 0
            height = box.get("height", 0) or 0
            y = box.get("y", 99999) or 99999
            area = width * height

            if width >= 120 and height >= 70:
                parent_bonus += min(area / 2, 120000)
            else:
                parent_bonus -= 80000

            if -80 <= y <= 900:
                parent_bonus += 50000
            elif 900 < y <= 1400:
                parent_bonus += 15000
            else:
                parent_bonus -= 50000
        else:
            parent_bonus -= 15000

        final_score = float(image_data.get("score", 0) or 0) + parent_bonus
        ranked.append((final_score, frame, "iframe", image_data))

    # Main page is fallback only; it often contains Google page chrome.
    image_data = extract_best_image_data_from_target(page)
    if image_data:
        final_score = float(image_data.get("score", 0) or 0) - 30000
        ranked.append((final_score, page, "main_page", image_data))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def choose_best_network_image(captured_images):
    if not captured_images:
        return "N/A"

    usable = []
    for item in captured_images:
        url = item.get("url", "")
        if not url or is_bad_image_url(url):
            continue
        score = image_url_priority(url)
        score += min(item.get("content_length", 0) or 0, 500000)
        if item.get("resource_type") == "image":
            score += 10000
        usable.append((score, url))

    if not usable:
        return "N/A"

    usable.sort(key=lambda x: x[0], reverse=True)
    return usable[0][1]


def wait_and_extract_image_url_with_target(page, captured_images=None, max_wait_seconds=25):
    start_time = time.time()

    while time.time() - start_time < max_wait_seconds:
        ranked = get_ranked_image_targets(page)
        if ranked:
            _, target, _, image_data = ranked[0]
            image_url = image_data.get("url", "N/A")
            if image_url != "N/A":
                return image_url, target, image_data

        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass

        try:
            page.mouse.wheel(0, 450)
            page.wait_for_timeout(800)
            page.mouse.wheel(0, -450)
        except Exception:
            pass

        page.wait_for_timeout(1000)

    # Last fallback: image response URLs captured while THIS exact transparency URL loaded.
    network_url = choose_best_network_image(captured_images or [])
    if network_url != "N/A":
        return network_url, None, None

    return "N/A", None, None


# =========================
# SAME-TARGET APP LINK / PACKAGE
# =========================

def extract_visible_install_link_from_target(target):
    js = r"""
    () => {
        const clean = (txt) => (txt || '').replace(/\s+/g, ' ').trim();
        const all = [];
        const walk = (root) => {
            if (!root || !root.querySelectorAll) return;
            for (const el of Array.from(root.querySelectorAll('*'))) {
                all.push(el);
                if (el.shadowRoot) walk(el.shadowRoot);
            }
        };
        walk(document);

        const visible = (el) => {
            if (!el || !el.getBoundingClientRect) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 10 && rect.height > 6 && rect.bottom > 0 && rect.right > 0 &&
                   rect.top < window.innerHeight && rect.left < window.innerWidth &&
                   style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
        };

        const candidates = [];
        for (const el of all) {
            const tag = (el.tagName || '').toLowerCase();
            if (!['a', 'button'].includes(tag) && !el.getAttribute('href') && !el.getAttribute('data-href')) continue;
            if (!visible(el)) continue;

            const href = el.href || el.getAttribute('href') || el.getAttribute('data-href') || '';
            const hrefLower = String(href).toLowerCase();
            const text = clean(el.innerText || el.textContent || '').toLowerCase();
            const cls = String(el.className || '').toLowerCase();
            const aria = String(el.getAttribute('aria-label') || '').toLowerCase();
            const rect = el.getBoundingClientRect();

            const goodHref = hrefLower.includes('googleadservices.com/pagead/aclk') ||
                             hrefLower.includes('play.google.com/store/apps/details') ||
                             hrefLower.includes('market://') ||
                             hrefLower.includes('apps.apple.com') ||
                             hrefLower.includes('itunes.apple.com');
            const looksInstall = cls.includes('install-button') || cls.includes('install') ||
                                 text.includes('install') || text === 'get' || text.includes('download') ||
                                 aria.includes('install') || text.includes('open') || text.includes('learn more');

            if (!goodHref && !looksInstall) continue;

            let score = 0;
            if (goodHref) score += 200;
            if (cls.includes('install-button')) score += 150;
            if (text.includes('install')) score += 120;
            if (text === 'get' || text.includes('download')) score += 70;
            if (tag === 'a') score += 40;
            score += Math.min(rect.width * rect.height / 20, 5000);

            candidates.push({href, score, text, top: rect.top, left: rect.left});
        }

        candidates.sort((a, b) => b.score - a.score);
        return candidates.length ? candidates[0].href : null;
    }
    """
    try:
        href = target.evaluate(js)
        if href and is_good_app_link(href):
            return clean_googleadservices_link(href)
    except Exception:
        pass
    return "N/A"


def extract_package_from_target_html(target):
    try:
        raw = target.evaluate(
            """
            () => {
                const scripts = Array.from(document.querySelectorAll('script')).map(s => s.textContent || '').join('\n');
                const hrefs = Array.from(document.querySelectorAll('a[href], a[data-href]'))
                    .map(a => [a.href || '', a.getAttribute('href') || '', a.getAttribute('data-href') || ''].join('\n'))
                    .join('\n');
                const html = document.documentElement ? document.documentElement.outerHTML : '';
                return scripts + '\n' + hrefs + '\n' + html;
            }
            """
        )
        packages = extract_packages_from_text(raw)
        if packages:
            return sorted(packages)[0]
    except Exception:
        pass
    return "N/A"


def extract_app_id_from_current_page_ad_data(page):
    """
    Strict fallback for the opened creative page only.
    It looks for appId/adData style fields, not random text matching.
    """
    try:
        raw = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('script'))
                .map(s => s.textContent || '')
                .join('\n')
            """
        )
        packages = extract_packages_from_text(raw)
        if packages:
            return sorted(packages)[0]
    except Exception:
        pass
    return "N/A"


def resolve_app_link_and_package(page, active_target):
    # 1) Same target visible install/app link.
    if active_target is not None:
        app_link = extract_visible_install_link_from_target(active_target)
        package_name = extract_package_name(app_link)
        if package_name != "N/A":
            return app_link, package_name, "same_target_install_link"

        # 2) Same target HTML/script appId/package.
        package_name = extract_package_from_target_html(active_target)
        if package_name != "N/A":
            return f"https://play.google.com/store/apps/details?id={package_name}", package_name, "same_target_appid"

    # 3) Strict current creative page adData/script fallback.
    # This is not headline/description matching; it only reads appId/package fields from the opened URL page.
    package_name = extract_app_id_from_current_page_ad_data(page)
    if package_name != "N/A":
        return f"https://play.google.com/store/apps/details?id={package_name}", package_name, "current_page_adData_appid"

    return "N/A", "N/A", "not_found"


# =========================
# NETWORK IMAGE CAPTURE
# =========================

def is_image_response(response):
    try:
        content_type = response.headers.get("content-type", "").lower()
        if any(content_type.startswith(prefix) for prefix in IMAGE_CONTENT_TYPES):
            return True
        url_lower = response.url.lower()
        return any(ext in url_lower for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"])
    except Exception:
        return False


def capture_image_response(response, captured_images):
    try:
        if not is_image_response(response):
            return

        url = response.url
        if is_bad_image_url(url):
            return

        headers = response.headers or {}
        content_length = 0
        try:
            content_length = int(headers.get("content-length", "0") or "0")
        except Exception:
            content_length = 0

        resource_type = ""
        try:
            resource_type = response.request.resource_type
        except Exception:
            pass

        captured_images.append({
            "url": url,
            "content_type": headers.get("content-type", ""),
            "content_length": content_length,
            "resource_type": resource_type,
        })
    except Exception:
        pass


# =========================
# MAIN IMAGE-ONLY SCRAPER
# =========================

def scrape_single_url(url_row):
    row_num, url = url_row
    original_url = url
    url = ensure_region_anywhere(url)
    creative_id = extract_creative_id(url)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            service_workers="block",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()
        captured_images = []
        page.on("response", lambda response: capture_image_response(response, captured_images))

        try:
            print(f"🖼 Row {row_num}: opening IMAGE ad transparency URL | creative={creative_id}")
            safe_add_log(
                row_number=row_num,
                status="STARTED",
                log_type="IMAGE_ONLY",
                url=url,
                video_id="image",
                app_link="",
                message=f"Started image-only extraction for creative_id={creative_id}",
            )

            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)

            # Let lazy-loaded ad iframes/images appear.
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            advertiser = extract_advertiser_from_page(page)

            image_url, active_target, image_data = wait_and_extract_image_url_with_target(
                page,
                captured_images=captured_images,
                max_wait_seconds=25,
            )

            app_link, package_name, package_source = resolve_app_link_and_package(page, active_target)
            process_time = get_exact_time()

            # Image-only mode: never write Google page text as headline/description.
            headline = "N/A"
            description = "N/A"
            ad_type = "image"

            data = [
                advertiser,
                package_name,
                url,
                app_link,
                process_time,
                ad_type,
                process_time,
            ]

            safe_update_combined_row(row_num, data)
            safe_update_headline_desc(row_num, headline, description)
            safe_update_image_url(row_num, image_url)

            if image_url == "N/A":
                status = "IMAGE_URL_NOT_FOUND"
                message = (
                    f"Image-only extraction finished but image URL was not found. "
                    f"creative_id={creative_id}; package_source={package_source}; "
                    f"captured_image_responses={len(captured_images)}"
                )
                print(f"⚠️ Row {row_num}: image URL not found | captured={len(captured_images)}")
            else:
                status = "SUCCESS"
                message = (
                    f"Image ad data saved from the opened transparency URL only. "
                    f"creative_id={creative_id}; package_source={package_source}"
                )
                print(f"✅ Row {row_num}: image URL saved -> {image_url[:140]}")

            safe_add_log(
                row_number=row_num,
                status=status,
                log_type="IMAGE_ONLY",
                url=url,
                video_id=ad_type,
                app_link=app_link,
                message=message,
            )

        except Exception as e:
            error_time = get_exact_time()
            print(f"❌ Row {row_num} error at {error_time}: {e}")

            try:
                data = [
                    "",
                    "N/A",
                    url,
                    "ERROR",
                    error_time,
                    "ERROR",
                    error_time,
                ]
                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, "N/A", "N/A")
                safe_update_image_url(row_num, "N/A")
            except Exception:
                pass

            try:
                safe_add_log(
                    row_number=row_num,
                    status="ERROR",
                    log_type="IMAGE_ONLY",
                    url=url,
                    video_id="ERROR",
                    app_link="ERROR",
                    message=str(e),
                )
            except Exception:
                pass

        finally:
            page.close()
            context.close()
            browser.close()


def run_parallel_combined_scraper(max_workers=MAX_WORKERS):
    """
    Kept same function name as the old combined scraper so your existing runner can call it.
    This function now runs IMAGE-ONLY scraping.
    """
    urls = sheets.get_urls_with_retry()

    url_rows = [
        (i + 2, u.strip())
        for i, u in enumerate(urls)
        if u and u.strip()
    ]

    if not url_rows:
        print("No transparency URLs found in column H.")
        return

    print(f"🚀 Starting IMAGE-ONLY Transparency scraper for {len(url_rows)} rows")
    print(f"⚡ Running parallel with max_workers={max_workers}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(scrape_single_url, url_row): url_row
            for url_row in url_rows
        }

        for future in as_completed(futures):
            row_num, _ = futures[future]
            try:
                future.result()
            except Exception as e:
                print(f"❌ Worker failed for row {row_num}: {e}")
                try:
                    safe_add_log(
                        row_number=row_num,
                        status="WORKER_ERROR",
                        log_type="IMAGE_ONLY",
                        url="",
                        video_id="ERROR",
                        app_link="ERROR",
                        message=str(e),
                    )
                except Exception:
                    pass

    print("✅ IMAGE-ONLY scraping finished")


# Optional alias if you prefer a clearer function name.
def run_parallel_image_scraper(max_workers=MAX_WORKERS):
    return run_parallel_combined_scraper(max_workers=max_workers)


if __name__ == "__main__":
    run_parallel_combined_scraper(max_workers=MAX_WORKERS)
