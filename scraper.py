# Combined Google Ads Transparency scraper
# Video-ad detection logic is kept from the original scrapper.txt.
# Non-video ads use text/image extraction + package matching from the uploaded non-video files.

from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import difflib
import re

def get_best_matching_package_for_text_ad(headline, description, package_list, min_score=0.70):
    """Matches package names with headline + description using character-level comparison."""
    import difflib
    def clean_text_for_comparison(text):
        if not text or text == "N/A":
            return ""
        return re.sub(r"[^a-z0-9]", "", text.lower())

    ad_text = clean_text_for_comparison(str(headline) + str(description))

    best_pkg = None
    best_score = 0.0

    for pkg in package_list:
        pkg_clean = clean_text_for_comparison(pkg)
        if not pkg_clean:
            continue
        ratio = difflib.SequenceMatcher(None, ad_text, pkg_clean).ratio()
        if ratio > best_score:
            best_score = ratio
            best_pkg = pkg

    if best_score >= min_score:
        return best_pkg, best_score
    return None, best_score

import time
import threading
import sheets


MAX_WORKERS = 2
SHEET_LOCK = threading.Lock()

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v", ".m3u8")

INSTALL_SELECTORS = [
    "a.install-button-anchor.svg-anchor",
    "a.install-button-anchor",
    'a[data-asoch-targets-ad-objective-type]',
    'a:has-text("Install")',
    'a:has-text("Get")',
    'a:has-text("Download")',
]


def safe_update_combined_row(row_num, data):
    """
    Thread-safe Google Sheet row update.
    Browser scraping runs parallel, but sheet writing is protected.
    """
    with SHEET_LOCK:
        sheets.update_combined_row(row_num, data)


def safe_update_headline_desc(row_num, headline, description):
    """
    Thread-safe Google Sheet row update for Headline and Description in cols M and N.
    """
    with SHEET_LOCK:
        sheets.update_headline_and_description(row_num, headline, description)


def safe_add_log(row_number, status, log_type, url="", video_id="", app_link="", message=""):
    """
    Thread-safe log writing.
    """
    with SHEET_LOCK:
        sheets.add_log(
            row_number=row_number,
            status=status,
            log_type=log_type,
            url=url,
            video_id=video_id,
            app_link=app_link,
            message=message
        )


def get_exact_time():
    return datetime.now().strftime("%I:%M:%S %p")


def clean_text(value):
    if not value:
        return "N/A"
    return re.sub(r"\s+", " ", str(value)).strip() or "N/A"


def extract_package_name(app_link):
    """
    Extracts package name from app store link.
    For Google Play: extracts the 'id' parameter
    For App Store: extracts app ID from URL
    """
    if not app_link or app_link == "N/A":
        return "N/A"
    
    try:
        # Google Play Store format: ...?id=com.example.app
        if "play.google.com" in app_link.lower():
            parsed = urlparse(app_link)
            query = parse_qs(parsed.query)
            package_name = query.get("id", [None])[0]
            if package_name:
                return package_name
        
        # Apple App Store format: ...app/app-name/id123456789
        if "apps.apple.com" in app_link.lower():
            # Extract the ID from the URL path
            match = re.search(r"/id(\d+)", app_link)
            if match:
                return f"id{match.group(1)}"
        
        # If we can't extract, return N/A
        return "N/A"
    
    except Exception:
        return "N/A"


# =========================
# VIDEO ID LOGIC (REVERTED TO YOUR ORIGINAL WORKING LOGIC)
# =========================

def is_real_video_response(response):
    try:
        url = response.url.lower()
        headers = response.headers
        content_type = headers.get("content-type", "").lower()

        if content_type.startswith("video/"):
            return True

        if "application/vnd.apple.mpegurl" in content_type:
            return True

        if "application/x-mpegurl" in content_type:
            return True

        if "videoplayback" in url:
            return True

        if any(ext in url for ext in VIDEO_EXTENSIONS):
            return True

    except Exception:
        pass

    return False


def extract_video_id_from_url(req_url):
    """
    Extracts only clean video IDs or filenames.
    Does NOT return full video links.
    """
    try:
        url_lower = req_url.lower()
        parsed = urlparse(req_url)
        query = parse_qs(parsed.query)

        if "videoplayback" in url_lower:
            video_id = query.get("id", [None])[0]

            if video_id:
                return video_id

            for key in ["itag", "ei", "source"]:
                value = query.get(key, [None])[0]
                if value:
                    return value

            return None

        for ext in VIDEO_EXTENSIONS:
            if ext in url_lower:
                filename = parsed.path.split("/")[-1]
                filename = filename.split("?")[0].strip()

                if filename:
                    return filename

        if "youtube.com/embed/" in url_lower:
            return req_url.split("youtube.com/embed/")[1].split("?")[0].split("&")[0]

        if "youtube.com/watch" in url_lower:
            return query.get("v", [None])[0]

        if "youtu.be/" in url_lower:
            return req_url.split("youtu.be/")[1].split("?")[0].split("&")[0]

    except Exception:
        return None

    return None


def extract_video_from_dom(page):
    """
    Checks actual video elements on page and inside frames.
    """
    try:
        video_sources = page.evaluate("""
            () => Array.from(document.querySelectorAll('video'))
                .map(v => v.currentSrc || v.src || '')
                .filter(Boolean)
        """)

        for src in video_sources:
            video_id = extract_video_id_from_url(src)
            if video_id:
                return video_id

    except Exception:
        pass

    for frame in page.frames:
        try:
            video_sources = frame.evaluate("""
                () => Array.from(document.querySelectorAll('video'))
                    .map(v => v.currentSrc || v.src || '')
                    .filter(Boolean)
            """)

            for src in video_sources:
                video_id = extract_video_id_from_url(src)
                if video_id:
                    return video_id

        except Exception:
            continue

    return "N/A"


def scan_browser_performance_for_video(page):
    """
    Scans performance entries for real video URLs only.
    """
    try:
        urls = page.evaluate("""
            () => performance.getEntriesByType('resource').map(r => r.name)
        """)

        for u in urls:
            u_lower = u.lower()

            if (
                "videoplayback" in u_lower
                or ".mp4" in u_lower
                or ".webm" in u_lower
                or ".mov" in u_lower
                or ".m4v" in u_lower
                or ".m3u8" in u_lower
                or "youtube.com/embed/" in u_lower
                or "youtube.com/watch" in u_lower
                or "youtu.be/" in u_lower
            ):
                video_id = extract_video_id_from_url(u)

                if video_id:
                    return video_id

    except Exception:
        pass

    return "N/A"


def click_possible_video_targets(page):
    """
    Clicks possible video preview areas.
    Avoids install buttons/app links.
    """
    selectors = [
        "video",
        "iframe",
        "creative-preview",
        'button[aria-label*="Play"]',
        'button[title*="Play"]',
        'div[aria-label*="Play"]',
        'img[src*="play"]'
    ]

    for sel in selectors:
        try:
            elements = page.locator(sel)
            count = elements.count()

            for i in range(count):
                el = elements.nth(i)

                if not el.is_visible():
                    continue

                try:
                    el.scroll_into_view_if_needed(timeout=2000)
                    box = el.bounding_box()

                    if not box:
                        continue

                    if box["width"] < 120 or box["height"] < 80:
                        continue

                    x = box["x"] + box["width"] / 2
                    y = box["y"] + box["height"] / 2

                    page.mouse.click(x, y)
                    page.wait_for_timeout(1500)
                    return True

                except Exception:
                    continue

        except Exception:
            continue

    return False


def wait_for_video_id(page, captured, max_seconds=20):
    waited = 0

    while waited < max_seconds:
        if captured.get("video_id") and captured["video_id"] != "N/A":
            return captured["video_id"]

        dom_video_id = extract_video_from_dom(page)
        if dom_video_id != "N/A":
            return dom_video_id

        page.wait_for_timeout(500)
        waited += 0.5

    return "N/A"


def detect_video_id(page, captured):
    """
    Main video detection flow.
    """
    video_id = extract_video_from_dom(page)

    if video_id == "N/A":
        click_possible_video_targets(page)
        video_id = wait_for_video_id(page, captured, max_seconds=15)

    if video_id == "N/A":
        video_id = scan_browser_performance_for_video(page)

    if video_id == "N/A":
        page.mouse.wheel(0, 400)
        page.wait_for_timeout(1500)

        click_possible_video_targets(page)
        video_id = wait_for_video_id(page, captured, max_seconds=10)

    return video_id


# =========================
# APP LINK LOGIC
# =========================

def clean_googleadservices_link(href):
    if not href:
        return "N/A"

    href = href.strip()

    if href.startswith("//"):
        href = "https:" + href

    try:
        parsed = urlparse(href)
        query = parse_qs(parsed.query)

        possible_keys = [
            "adurl",
            "url",
            "q",
            "u",
            "ds_dest_url",
            "destination",
        ]

        for key in possible_keys:
            value = query.get(key, [None])[0]
            if value:
                return unquote(value)

    except Exception:
        pass

    return href


def is_good_app_link(href):
    if not href:
        return False

    href = href.lower()

    return (
        "googleadservices.com/pagead/aclk" in href
        or "play.google.com" in href
        or "apps.apple.com" in href
        or "itunes.apple.com" in href
    )


def get_visible_install_candidates_from_target(target):
    candidates = []

    for selector in INSTALL_SELECTORS:
        try:
            loc = target.locator(selector)
            count = loc.count()

            for i in range(count):
                try:
                    el = loc.nth(i)

                    href = el.get_attribute("href", timeout=1500)
                    data_href = el.get_attribute("data-href", timeout=1000)

                    final_href = href or data_href

                    if not final_href or not is_good_app_link(final_href):
                        continue

                    box = el.bounding_box(timeout=1500)

                    if not box:
                        continue

                    if box["width"] < 20 or box["height"] < 10:
                        continue

                    text = ""
                    try:
                        text = el.inner_text(timeout=1000).strip().lower()
                    except Exception:
                        pass

                    score = 0

                    try:
                        class_name = el.get_attribute("class", timeout=1000) or ""
                        if "install-button-anchor" in class_name:
                            score += 100
                    except Exception:
                        pass

                    if "install" in text:
                        score += 80
                    elif "get" in text or "download" in text:
                        score += 40

                    center_x = box["x"] + box["width"] / 2
                    center_y = box["y"] + box["height"] / 2

                    if 350 <= center_x <= 850:
                        score += 40

                    if 50 <= center_y <= 700:
                        score += 40

                    if center_y > 700:
                        score -= 100

                    candidates.append({
                        "href": final_href,
                        "score": score,
                        "box": box,
                        "text": text,
                    })

                except Exception:
                    continue

        except Exception:
            continue

    return candidates


def extract_visible_install_link(page):
    """
    Extracts only the visible install button from the active creative.
    Does not scan random adservice links.
    """
    all_candidates = []

    try:
        all_candidates.extend(get_visible_install_candidates_from_target(page))
    except Exception:
        pass

    for frame in page.frames:
        try:
            all_candidates.extend(get_visible_install_candidates_from_target(frame))
        except Exception:
            continue

    if not all_candidates:
        return "N/A"

    all_candidates.sort(key=lambda x: x["score"], reverse=True)

    best = all_candidates[0]

    if best["score"] <= 0:
        return "N/A"

    return clean_googleadservices_link(best["href"])


def extract_install_link_by_precise_js(page):
    """
    Strict JS fallback:
    only install-button-anchor / Install text links,
    not every googleadservices link.
    """
    js = r"""
    () => {
        const anchors = Array.from(document.querySelectorAll('a[href], a[data-href]'));
        const candidates = anchors.map(a => {
            const href = a.href || a.getAttribute('href') || a.getAttribute('data-href') || '';
            const text = (a.innerText || a.textContent || '').trim().toLowerCase();
            const cls = String(a.className || '').toLowerCase();
            const aria = String(a.getAttribute('aria-label') || '').toLowerCase();
            const rect = a.getBoundingClientRect();

            const goodLink =
                href.includes('googleadservices.com/pagead/aclk') ||
                href.includes('play.google.com') ||
                href.includes('apps.apple.com') ||
                href.includes('itunes.apple.com');

            const looksInstall =
                cls.includes('install-button-anchor') ||
                text.includes('install') ||
                text.includes('get') ||
                text.includes('download') ||
                aria.includes('install');

            const visible =
                rect.width > 20 &&
                rect.height > 10 &&
                rect.bottom > 0 &&
                rect.right > 0 &&
                rect.top < window.innerHeight &&
                rect.left < window.innerWidth;

            if (!goodLink || !looksInstall || !visible) {
                return null;
            }

            let score = 0;
            if (cls.includes('install-button-anchor')) score += 100;
            if (text.includes('install')) score += 80;
            if (text.includes('get') || text.includes('download')) score += 40;
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            if (cx >= 350 && cx <= 850) score += 40;
            if (cy >= 50 && cy <= 700) score += 40;
            if (cy > 700) score -= 100;
            return {
                href,
                score
            };
        }).filter(Boolean);

        candidates.sort((a, b) => b.score - a.score);

        return candidates.length ? candidates[0].href : null;
    }
    """

    try:
        href = page.evaluate(js)
        if href and is_good_app_link(href):
            return clean_googleadservices_link(href)
    except Exception:
        pass

    for frame in page.frames:
        try:
            href = frame.evaluate(js)
            if href and is_good_app_link(href):
                return clean_googleadservices_link(href)
        except Exception:
            continue

    return "N/A"


def wait_and_extract_install_link(page, max_wait_seconds=35):
    start = time.time()

    while time.time() - start < max_wait_seconds:
        app_link = extract_visible_install_link(page)

        if app_link != "N/A":
            return app_link

        app_link = extract_install_link_by_precise_js(page)

        if app_link != "N/A":
            return app_link

        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass

        page.wait_for_timeout(1500)

    return "N/A"


# =========================
# HEADLINE AND DESCRIPTION LOGIC
# =========================

def wait_and_extract_headline_description(page, max_wait_seconds=15):
    """
    Polls for Headline and Description inside iframes ONLY.
    Uses structural class patterns (-e-15, -e-67) and visibility checks 
    to avoid grabbing hidden template text.
    """
    js = r"""
    () => {
        let headText = "N/A";
        let descText = "N/A";

        // Helper to ensure we don't grab hidden/template elements
        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
        };

        // SEARCH HEADLINE: Matches any class containing '-e-15' OR 'headline'
        const headNodes = document.querySelectorAll('[class*="-e-15"], [class*="headline"]');
        for (let el of headNodes) {
            if (isVisible(el)) {
                let text = (el.innerText || el.textContent || "").replace(/\n/g, ' ').trim();
                // Ensure it's not a template placeholder like {{headline}}
                if (text.length > 1 && !text.includes('{{')) { 
                    headText = text; 
                    break; 
                }
            }
        }

        // SEARCH DESCRIPTION: Matches any class containing '-e-67' OR 'long-description'
        const descNodes = document.querySelectorAll('[class*="-e-67"], [class*="long-description"]');
        for (let el of descNodes) {
            if (isVisible(el)) {
                let text = (el.innerText || el.textContent || "").replace(/\n/g, ' ').trim();
                if (text.length > 1 && text !== headText && !text.includes('{{')) { 
                    descText = text; 
                    break; 
                }
            }
        }

        // If we found either one, return it
        if (headText !== "N/A" || descText !== "N/A") {
            return { headline: headText, description: descText };
        }

        return null;
    }
    """

    start = time.time()
    
    # Retry loop: Keeps trying for up to max_wait_seconds (15s)
    while time.time() - start < max_wait_seconds:
        
        # STRICTLY CHECK IFRAMES ONLY.
        for frame in page.frames:
            try:
                result = frame.evaluate(js)
                if result and (result.get("headline", "N/A") != "N/A" or result.get("description", "N/A") != "N/A"):
                    return result.get("headline", "N/A"), result.get("description", "N/A")
            except Exception:
                continue
        
        # Wait 1 second and loop again to let the ad iframe fully load
        page.wait_for_timeout(1000)

    # If the timer runs out, return N/A
    return "N/A", "N/A"

# =========================
# STRICT TEXT-AD PACKAGE MATCHER
# =========================

MIN_PACKAGE_MATCH_SCORE = 0.76

_GENERIC_PACKAGE_TOKENS = {
    "com", "net", "org", "co", "io", "app", "apps", "android", "mobile",
    "google", "play", "store", "free", "pro", "lite", "online", "official",
    "inc", "ltd", "llc", "studio", "studios", "company", "group", "digital",
    "ai", "all", "new", "best", "easy", "fast"
}


def clean_text_for_comparison(text):
    """Lowercase and remove punctuation/spaces for ad text vs package comparison."""
    if not text or text == "N/A":
        return ""
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def split_words_for_comparison(text):
    if not text or text == "N/A":
        return []
    return re.findall(r"[a-z0-9]+", str(text).lower())


def package_tokens_for_matching(pkg):
    """Turn com.example.musicplayer into useful tokens like example/musicplayer."""
    if not pkg:
        return []

    raw_tokens = re.split(r"[._-]+", pkg.lower())
    tokens = []

    for token in raw_tokens:
        token = re.sub(r"[^a-z0-9]", "", token)
        if not token or token in _GENERIC_PACKAGE_TOKENS:
            continue
        if len(token) < 3 or token.isdigit():
            continue
        tokens.append(token)

    return tokens


def score_package_against_text(pkg, headline, description):
    """
    STRICT score for non-video ads: compare package ONLY with visible headline + description.
    This prevents image ads from using random hidden package names from the page HTML.
    """
    visible_raw = f"{headline or ''} {description or ''}"
    visible_clean = clean_text_for_comparison(visible_raw)
    visible_words = split_words_for_comparison(visible_raw)
    visible_word_set = set(visible_words)

    if not visible_clean or not visible_words:
        return 0.0

    tokens = package_tokens_for_matching(pkg)
    if not tokens:
        return 0.0

    package_core = "".join(tokens)
    score = 0.0

    # Very strong signal: useful package core appears directly in visible ad text.
    if package_core and len(package_core) >= 6 and package_core in visible_clean:
        score = max(score, 0.98)

    # Direct token hits only. Generic tokens were already removed by package_tokens_for_matching().
    exact_hits = []
    partial_hits = []

    for token in tokens:
        if token in visible_word_set:
            exact_hits.append(token)
            continue

        # Allow long tokens like musicplayer/pdfreader to match joined visible text.
        if len(token) >= 6 and token in visible_clean:
            exact_hits.append(token)
            continue

        for word in visible_words:
            if len(token) >= 5 and len(word) >= 5 and (token in word or word in token):
                partial_hits.append(token)
                break

    exact_hits = list(dict.fromkeys(exact_hits))
    partial_hits = list(dict.fromkeys(partial_hits))
    total_hits = len(set(exact_hits + partial_hits))

    # One weak/fuzzy word is NOT enough now. This is the main image-ad false-match fix.
    if len(exact_hits) >= 2:
        score = max(score, 0.92)
    elif len(exact_hits) == 1 and len(exact_hits[0]) >= 8:
        score = max(score, 0.78)
    elif total_hits >= 2:
        score = max(score, 0.76)

    # Fuzzy matching can only boost when the whole package core is extremely close.
    # It cannot pass alone on one random similar word.
    if package_core and len(package_core) >= 8:
        core_ratio = difflib.SequenceMatcher(None, visible_clean, package_core).ratio()
        if core_ratio >= 0.88:
            score = max(score, 0.82)

    return round(score, 4)


def get_best_matching_package(headline, description, package_list, min_score=MIN_PACKAGE_MATCH_SCORE):
    """
    Compare headline + description with every found package.
    Returns (package, score). If no package score is at least 0.76, returns (None, best_score).
    """
    if not package_list:
        return None, 0.0

    best_pkg = None
    best_score = 0.0

    for pkg in sorted(package_list):
        score = score_package_against_text(pkg, headline, description)
        if score > best_score:
            best_score = score
            best_pkg = pkg

    if best_pkg and best_score >= min_score:
        return best_pkg, best_score

    return None, best_score

def decode_all(text):
    """Decode every encoding variant so no package name is missed."""
    text = str(text or "")
    text = text.replace('\\/', '/')
    text = re.sub(r'\\u002[Ff]', '/', text)
    text = re.sub(r'\\x3[Dd]', '=', text)
    text = re.sub(r'\\x26',    '&', text)
    text = re.sub(r'\\x3[Ff]', '?', text)
    text = re.sub(r'\\x2[Ff]', '/', text)
    text = re.sub(r'\\u003[Dd]', '=', text)
    text = re.sub(r'\\u0026',    '&', text)
    text = re.sub(r'\\u003[Ff]', '?', text)
    text = re.sub(r'%3[Dd]', '=', text, flags=re.I)
    text = re.sub(r'%26',    '&', text, flags=re.I)
    text = re.sub(r'%3[Ff]', '?', text, flags=re.I)
    text = re.sub(r'%2[Ff]', '/', text, flags=re.I)
    text = re.sub(r'%3[Aa]', ':', text, flags=re.I)
    text = (text.replace('&amp;', '&').replace('&quot;', '"')
                .replace('&#38;', '&').replace('&#61;', '=')
                .replace('&#x3D;', '=').replace('&#x26;', '&'))
    return text


_SKIP_EXT = re.compile(
    r'\.(jpg|jpeg|png|gif|webp|svg|ico|css|js|json|xml|html|htm|'
    r'woff|woff2|ttf|otf|eot|pdf|zip|apk|mp4|mp3|ogg|m3u8)$', re.I)
_SKIP_PFX = re.compile(
    r'^(com\.google\.android\.(gms|vending|inputmethod|tts|webview)|'
    r'com\.android\.|android\.|androidx\.|kotlin\.|kotlinx\.|'
    r'com\.squareup\.|io\.reactivex\.|okhttp3\.|javax\.|java\.|'
    r'org\.json\.|org\.apache\.)', re.I)

def _is_valid_pkg(pkg):
    parts = pkg.split('.')
    if len(parts) < 3 or len(pkg) < 8:  return False
    if _SKIP_EXT.search(pkg):            return False
    if _SKIP_PFX.match(pkg):             return False
    for p in parts:
        if not p or not re.match(r'^[A-Za-z][A-Za-z0-9_]*$', p):
            return False
    return True

def extract_packages_from_text(raw_text):
    """Returns a SET of all unique, valid package names found in the text."""
    text = decode_all(raw_text)
    candidates = set()   

    patterns = [
        r"""['"]appId['"]\s*:\s*['"]([A-Za-z][\w.]+)['"]""",
        r"""play\.google\.com/store/apps/details[^\s'"<>]*[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})""",
        r"""market://[^\s'"]*[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})""",
        r"""(?:destination_url|final_url|click_url|destUrl|clickUrl|landingUrl)['"\s]*:['"\s]*['"][^'"]*[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})""",
        r"""[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})""",
        r"""[?&]package=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})"""
    ]

    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            pkg = m.group(1).rstrip('.,;\'"\\ ')
            if _is_valid_pkg(pkg):
                candidates.add(pkg)

    return candidates

def extract_package_from_page(page):
    """
    Scans strictly the rendered DOM and visible links. 
    Removes the background network fetching that caused cross-contamination.
    """
    collected_texts = []

    for frame in page.frames:
        try:
            frame_html = frame.evaluate("() => document.documentElement.outerHTML")
            if frame_html and len(frame_html) > 200:
                collected_texts.append(frame_html)

            hrefs = frame.evaluate("""
                () => Array.from(document.querySelectorAll('a[href]'))
                           .map(a => a.href).filter(Boolean)
            """)
            if hrefs:
                collected_texts.append('\n'.join(hrefs))

            visible = frame.evaluate("() => document.body ? document.body.innerText : ''")
            if visible:
                collected_texts.append(visible)

        except Exception:
            continue

    try:
        visible = page.evaluate("() => document.body ? document.body.innerText : ''")
        if visible:
            collected_texts.append(visible)
        
        hrefs = page.evaluate("""
            () => Array.from(document.querySelectorAll('a[href]'))
                       .map(a => a.href).filter(Boolean)
        """)
        if hrefs:
            collected_texts.append('\n'.join(hrefs))
            
        main_html = page.evaluate("() => document.documentElement.outerHTML")
        if main_html:
            collected_texts.append(main_html)
    except Exception:
        pass

    combined = '\n'.join(collected_texts)
    return extract_packages_from_text(combined)

def extract_advertiser_from_page(page):
    try:
        loc = page.locator('.advertiser-title, [data-test-id="advertiser-name"]').first
        loc.wait_for(timeout=4000)
        text = loc.inner_text().strip()
        if text and len(text) > 1 and "Sign in" not in text:
            return text
    except Exception:
        pass

    js = r"""
    () => {
        const badWords = ['sign in', 'log in', 'home', 'menu', 'search', 'help', 'privacy', 'terms', 'ad details', 'see more ads', 'ads transparency'];
        let maxFont = 0;
        let advertiserName = "N/A";

        for (let el of document.querySelectorAll('body *')) {
            if (el.childElementCount > 0) continue;
            let txt = (el.innerText || "").trim();
            let lower = txt.toLowerCase();
            if (txt.length < 2 || txt.length > 60 || badWords.some(b => lower.includes(b))) continue;

            let rect = el.getBoundingClientRect();
            // Strict visual bounds check
            if (rect.width === 0 || rect.height === 0 || rect.y < 0 || rect.y > 350 || rect.width < 10) continue;

            let style = window.getComputedStyle(el);
            if (style.opacity === '0' || style.display === 'none' || style.visibility === 'hidden') continue;

            let font = parseFloat(style.fontSize || '0');
            if (font > maxFont) {
                maxFont = font;
                advertiserName = txt;
            }
        }
        return advertiserName;
    }
    """
    try:
        if advertiser := page.evaluate(js): return advertiser
    except Exception:
        pass
    return "N/A"

def _frame_parent_box(frame):
    """
    Returns the iframe element box in the parent page.
    This is important because a hidden/stale iframe can still return text from inside itself.
    """
    try:
        iframe_el = frame.frame_element()
        box = iframe_el.bounding_box()
        if not box:
            return None
        return box
    except Exception:
        return None


def _score_non_video_target(target):
    """
    Scores a page/frame by checking whether it looks like the active ad creative.
    Higher score = more likely to be the current transparency URL preview.
    """
    js = r"""
    () => {
        const cleanText = (txt) => (txt || '').replace(/\n/g, ' ').replace(/\s+/g, ' ').trim();

        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return (
                rect.width > 0 &&
                rect.height > 0 &&
                rect.bottom > 0 &&
                rect.right > 0 &&
                rect.top < window.innerHeight &&
                rect.left < window.innerWidth &&
                style.visibility !== 'hidden' &&
                style.display !== 'none' &&
                style.opacity !== '0'
            );
        };

        const visibleText = cleanText(document.body ? document.body.innerText : '');
        const lowerText = visibleText.toLowerCase();

        const headlineNodes = Array.from(document.querySelectorAll(
            '[class*="-e-15"], [class*="headline"], [aria-label*="Headline"], [aria-label*="headline"]'
        )).filter(el => {
            const txt = cleanText(el.innerText || el.textContent || '');
            return txt.length >= 4 && txt.length <= 180 && isVisible(el) && !txt.includes('{{');
        });

        const descNodes = Array.from(document.querySelectorAll(
            '[class*="-e-67"], [class*="long-description"], [class*="description"], [aria-label*="Description"], [aria-label*="description"]'
        )).filter(el => {
            const txt = cleanText(el.innerText || el.textContent || '');
            return txt.length >= 8 && txt.length <= 260 && isVisible(el) && !txt.includes('{{');
        });

        const installNodes = Array.from(document.querySelectorAll('a[href], a[data-href], button')).filter(el => {
            const txt = cleanText(el.innerText || el.textContent || '').toLowerCase();
            const cls = String(el.className || '').toLowerCase();
            const aria = String(el.getAttribute('aria-label') || '').toLowerCase();
            const href = String(el.href || el.getAttribute('href') || el.getAttribute('data-href') || '').toLowerCase();
            const looksInstall = cls.includes('install-button-anchor') || txt.includes('install') || txt === 'get' || txt.includes('download') || aria.includes('install');
            const goodHref = href.includes('googleadservices.com/pagead/aclk') || href.includes('play.google.com') || href.includes('apps.apple.com') || href.includes('itunes.apple.com');
            return isVisible(el) && (looksInstall || goodHref);
        });

        const imageNodes = Array.from(document.querySelectorAll('img, picture, canvas, svg')).filter(el => {
            const src = String(el.getAttribute('src') || '').toLowerCase();
            const alt = String(el.getAttribute('alt') || '').toLowerCase();
            if (src.includes('googlelogo') || alt.includes('google')) return false;
            const rect = el.getBoundingClientRect();
            return isVisible(el) && rect.width >= 80 && rect.height >= 50;
        });

        const leafTextNodes = Array.from(document.querySelectorAll('*')).filter(el => {
            if (el.childElementCount > 0) return false;
            const txt = cleanText(el.innerText || el.textContent || '');
            if (txt.length < 4 || txt.length > 220) return false;
            if (txt.includes('{{') || txt.includes('}}')) return false;
            return isVisible(el);
        });

        let score = 0;
        score += Math.min(headlineNodes.length, 2) * 120;
        score += Math.min(descNodes.length, 2) * 100;
        score += Math.min(installNodes.length, 2) * 80;
        score += Math.min(imageNodes.length, 3) * 25;
        score += Math.min(leafTextNodes.length, 8) * 8;

        // The Google transparency shell/page chrome should not beat the actual creative iframe.
        if (lowerText.includes('ads transparency center') || lowerText.includes('ads transparency centre')) score -= 180;
        if (lowerText.includes('see more ads') || lowerText.includes('report this ad')) score -= 90;
        if (lowerText.includes('last shown') || lowerText.includes('shown in')) score -= 50;

        return {
            score,
            headlineCount: headlineNodes.length,
            descriptionCount: descNodes.length,
            installCount: installNodes.length,
            imageCount: imageNodes.length,
            leafTextCount: leafTextNodes.length,
            visibleTextLength: visibleText.length
        };
    }
    """
    try:
        return target.evaluate(js) or {"score": 0}
    except Exception:
        return {"score": 0}


def get_ranked_non_video_targets(page):
    """
    Returns frames/page ordered by the most likely active creative.
    Old logic checked page.frames in browser order, which can be wrong for repeated ads.
    """
    ranked = []

    for frame in page.frames:
        if frame == page.main_frame:
            continue

        parent_bonus = 0
        box = _frame_parent_box(frame)

        if box:
            width = box.get("width", 0) or 0
            height = box.get("height", 0) or 0
            y = box.get("y", 99999) or 99999
            area = width * height

            # Active ad preview iframe is normally visible and reasonably large.
            if width >= 120 and height >= 70:
                parent_bonus += min(area / 8000, 80)
            else:
                parent_bonus -= 120

            # Prefer currently visible/near-top preview, not repeated ads farther down the page.
            if -50 <= y <= 900:
                parent_bonus += 80
            elif 900 < y <= 1400:
                parent_bonus += 20
            else:
                parent_bonus -= 80
        else:
            parent_bonus -= 40

        inner = _score_non_video_target(frame)
        final_score = float(inner.get("score", 0) or 0) + parent_bonus

        if final_score > 0:
            ranked.append((final_score, frame, "iframe", inner))

    # Main page is only a fallback. It contains Google page chrome, so keep it below real creative frames.
    main_inner = _score_non_video_target(page)
    main_score = float(main_inner.get("score", 0) or 0) - 60
    if main_score > 0:
        ranked.append((main_score, page, "main_page", main_inner))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def wait_and_extract_text_ad_details(page, max_wait_seconds=15):
    """
    Extracts headline and description for non-video ads.
    - Prefers visible elements from the active creative (main DOM).
    - Uses specific selectors: <div role="link">, div.HFTpmd-WsjYwc-hgDUwe, div.cS4Vcb-vnv8ic
    - Falls back to iframe if necessary.
    - Relaxed visibility check to allow offscreen or special-language creatives (e.g., Arabic).
    """
    js = r"""
    () => {
        const cleanText = (txt) => (txt || "").replace(/\n/g, " ").replace(/\s+/g, " ").trim();

        // RELAXED visibility: ignore offscreen top/bottom/left/right but still require positive width/height
        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 &&
                   style.visibility !== 'hidden' &&
                   style.display !== 'none' &&
                   style.opacity !== '0';
        };

        let headline = "N/A";
        let description = "N/A";

        // 1️⃣ Main visible creative first
        const headlineEl = document.querySelector('div[role="link"] span, div.HFTpmd-WsjYwc-hgDUwe, div.cS4Vcb-vnv8ic');
        if (headlineEl && isVisible(headlineEl)) {
            headline = cleanText(headlineEl.innerText || headlineEl.textContent);
        }

        const descriptionEl = document.querySelector('div.HFTpmd-WsjYwc-hgDUwe, div.cS4Vcb-vnv8ic');
        if (descriptionEl && isVisible(descriptionEl)) {
            description = cleanText(descriptionEl.innerText || descriptionEl.textContent);
        }

        return { headline, description };
    }
    """

    def read_target(target):
        try:
            data = target.evaluate(js)
            if data and (data.get("headline") != "N/A" or data.get("description") != "N/A"):
                return data
        except Exception:
            return None
        return None

    start_time = time.time()

    while time.time() - start_time < max_wait_seconds:
        # 1) Check main page DOM first (active visible creative)
        data = read_target(page)
        if data:
            return data

        # 2) Fallback: check iframes only if main DOM didn't yield headline/description
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            data = read_target(frame)
            if data:
                return data

        page.wait_for_timeout(1000)

    return {"headline": "N/A", "description": "N/A"}
# =========================
# MAIN COMBINED SCRAPER: VIDEO ADS + TEXT ADS
# =========================

def is_valid_text_ad(headline, description):
    if headline and headline != "N/A" and len(clean_text(headline)) >= 3:
        return True
    if description and description != "N/A" and len(clean_text(description)) >= 15:
        return True
    return False

def has_visible_image_creative(page):
    """
    Detects likely image/display creative for non-video ads.
    Used only after video detection returns N/A.
    """
    js = r"""
    () => {
        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return (
                rect.width >= 120 &&
                rect.height >= 80 &&
                rect.bottom > 0 &&
                rect.right > 0 &&
                rect.top < window.innerHeight &&
                rect.left < window.innerWidth &&
                style.visibility !== 'hidden' &&
                style.display !== 'none' &&
                style.opacity !== '0'
            );
        };

        const imageLike = Array.from(document.querySelectorAll('img, picture, canvas, svg')).some(el => {
            const src = String(el.getAttribute('src') || '').toLowerCase();
            const alt = String(el.getAttribute('alt') || '').toLowerCase();
            if (src.includes('googlelogo') || alt.includes('google')) return false;
            return isVisible(el);
        });

        if (imageLike) return true;

        return Array.from(document.querySelectorAll('*')).some(el => {
            if (!isVisible(el)) return false;
            const bg = window.getComputedStyle(el).backgroundImage || '';
            return bg && bg !== 'none' && bg.includes('url(');
        });
    }
    """

    try:
        if page.evaluate(js):
            return True
    except Exception:
        pass

    for frame in page.frames:
        try:
            if frame.evaluate(js):
                return True
        except Exception:
            continue

    return False


# =========================
# IMAGE AD LOGIC (STRICT SAME CREATIVE)
# =========================

_IMAGE_BAD_TEXT_WORDS = {
    "install", "get", "open", "download", "play", "learn more", "visit site",
    "sponsored", "ad", "ads", "advertisement", "google", "google play",
    "apps", "app", "privacy", "terms", "report this ad", "see more ads",
    "ads transparency center", "ads transparency centre", "last shown", "shown in",
    "more options", "close", "menu"
}


def _clean_image_url(url):
    """Return the exact usable image src URL, especially tpc.googlesyndication.com/archive/simgad."""
    if not url or url == "N/A":
        return "N/A"
    url = str(url).strip().strip('"\'')
    if not url:
        return "N/A"
    if url.startswith("//"):
        return "https:" + url
    return url


def _extract_image_ad_details_from_target(target):
    """
    Extracts image URL and visible headline/description from the SAME visual creative.

    Important fixes:
    - Uses the exact DOM attribute: img.getAttribute('src'), the same URL you see in Inspect Element.
    - Chooses the visible tpc.googlesyndication.com/archive/simgad image closest to the active preview area.
    - Reads text only in the visual band directly below that exact image.
    - Stops before the next visible image, so it cannot steal title/description from another ad.
    - If no clean text is visually attached to the selected image, returns N/A instead of random text.
    """
    js = r"""
    () => {
        const cleanText = (txt) => (txt || '').replace(/\n/g, ' ').replace(/\s+/g, ' ').trim();

        const isVisible = (el, relaxed=false) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            if (!(rect.width > 0 && rect.height > 0)) return false;
            if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') return false;
            if (relaxed) return true;
            return rect.bottom > 0 && rect.right > 0 && rect.top < window.innerHeight && rect.left < window.innerWidth;
        };

        const viewportVisibleArea = (rect) => {
            const left = Math.max(0, rect.left);
            const right = Math.min(window.innerWidth, rect.right);
            const top = Math.max(0, rect.top);
            const bottom = Math.min(window.innerHeight, rect.bottom);
            return Math.max(0, right - left) * Math.max(0, bottom - top);
        };

        const absoluteUrl = (raw) => {
            raw = String(raw || '').trim().replace(/^["']|["']$/g, '');
            if (!raw) return '';
            // For img src this remains exact. Only srcset fallback needs descriptor trimming.
            raw = raw.split(',')[0].trim().split(/\s+/)[0].trim();
            if (!raw || raw.startsWith('data:') || raw.startsWith('blob:')) return '';
            if (raw.startsWith('//')) raw = 'https:' + raw;
            try { return new URL(raw, location.href).href; } catch(e) { return raw; }
        };

        const isBadImageUrl = (url) => {
            const u = String(url || '').toLowerCase();
            return (
                !u ||
                u.startsWith('data:') ||
                u.startsWith('blob:') ||
                u.includes('googlelogo') ||
                u.includes('favicon') ||
                u.includes('sprite') ||
                u.includes('/icons/') ||
                u.includes('gstatic.com/images/branding') ||
                u.includes('ssl.gstatic.com') ||
                u.includes('doubleclick.net/pagead/images')
            );
        };

        const hasAncestorMatch = (el, regex) => {
            let cur = el;
            let depth = 0;
            while (cur && depth < 12) {
                const key = `${cur.tagName || ''} ${String(cur.id || '')} ${String(cur.className || '')}`.toLowerCase();
                if (regex.test(key)) return true;
                cur = cur.parentElement;
                depth += 1;
            }
            return false;
        };

        const imageCandidates = [];
        const imgNodes = Array.from(document.querySelectorAll('img[src]'));

        for (const img of imgNodes) {
            if (!isVisible(img)) continue;
            const rect = img.getBoundingClientRect();

            // Real ad creative image. This avoids 72x72 app icons and tiny tracking pixels.
            if (rect.width < 140 || rect.height < 90) continue;

            const rawSrc = img.getAttribute('src') || '';       // EXACT inspect-element src first
            const url = absoluteUrl(rawSrc || img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || img.src || img.currentSrc || '');
            if (isBadImageUrl(url)) continue;

            const lowerUrl = url.toLowerCase();
            const alt = String(img.getAttribute('alt') || '').toLowerCase();
            const cls = String(img.className || '').toLowerCase();
            const id = String(img.id || '').toLowerCase();
            if (alt.includes('google') || cls.includes('logo') || id.includes('logo')) continue;

            const area = rect.width * rect.height;
            const visibleArea = viewportVisibleArea(rect);
            const visibleRatio = visibleArea / Math.max(1, area);
            const centerY = rect.top + rect.height / 2;
            const centerX = rect.left + rect.width / 2;

            let score = 0;
            score += area;
            score += visibleArea * 1.8;
            score += visibleRatio * 120000;

            // Strongly prefer the actual creative URL pattern from Inspect Element.
            if (lowerUrl.includes('tpc.googlesyndication.com')) score += 650000;
            if (lowerUrl.includes('/archive/') || lowerUrl.includes('/simgad/')) score += 450000;
            if (hasAncestorMatch(img, /(html-renderer|html-container|creative|ad-container|landscape|portrait|preview)/)) score += 180000;
            if (rawSrc && rawSrc === img.getAttribute('src')) score += 120000;

            // Prefer the active preview in the current viewport, not another visible repeated ad.
            score += Math.max(0, 520 - Math.abs(centerY - window.innerHeight * 0.42)) * 650;
            score += Math.max(0, 520 - Math.abs(centerX - window.innerWidth * 0.50)) * 120;
            if (rect.top < -10) score -= 300000;
            if (rect.top > window.innerHeight * 0.72) score -= 260000;
            if (rect.bottom < 60) score -= 260000;

            imageCandidates.push({
                el: img,
                url,
                rawSrc,
                score,
                rect: {top: rect.top, left: rect.left, bottom: rect.bottom, right: rect.right, width: rect.width, height: rect.height}
            });
        }

        imageCandidates.sort((a, b) => b.score - a.score);
        if (!imageCandidates.length) {
            return {
                image_url: 'N/A', headline: 'N/A', description: 'N/A',
                candidate_score: 0, debug: 'no_visible_large_img_src'
            };
        }

        const image = imageCandidates[0];

        // Find the next visible image below the selected image. Text after this belongs to another creative.
        let nextImageTop = Infinity;
        for (const cand of imageCandidates) {
            if (cand.url === image.url && Math.abs(cand.rect.top - image.rect.top) < 2) continue;
            if (cand.rect.top > image.rect.bottom + 8) {
                nextImageTop = Math.min(nextImageTop, cand.rect.top);
            }
        }

        const badExact = new Set([
            'install', 'get', 'open', 'download', 'play', 'learn more', 'visit site',
            'sponsored', 'ad', 'ads', 'advertisement', 'google', 'google play',
            'privacy', 'terms', 'report this ad', 'see more ads', 'ads transparency center',
            'ads transparency centre', 'last shown', 'shown in', 'more options', 'close', 'menu'
        ]);

        const textLooksUseful = (txt) => {
            const t = cleanText(txt);
            const lower = t.toLowerCase();
            if (t.length < 3 || t.length > 220) return false;
            if (t.includes('{{') || t.includes('}}')) return false;
            if (/https?:\/\//i.test(t)) return false;
            if (badExact.has(lower)) return false;
            if (/^(install|get|open|download|play|open app)$/i.test(t)) return false;
            if (/^(\d+\s*)?$/.test(t)) return false;
            if (lower.includes('ads transparency') || lower.includes('report this ad')) return false;
            if (lower.includes('install') || lower.includes('download')) return false;
            if (/^\W+$/.test(t)) return false;
            return true;
        };

        const horizontalOverlapRatio = (a, b) => {
            const overlap = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left));
            const minWidth = Math.max(1, Math.min(a.width, b.width));
            return overlap / minWidth;
        };

        const getOwnText = (el) => {
            const own = Array.from(el.childNodes)
                .filter(n => n.nodeType === Node.TEXT_NODE)
                .map(n => n.textContent || '')
                .join(' ');
            return cleanText(own);
        };

        const textItems = [];
        const textSelectors = 'span, div, p, h1, h2, h3, h4, a';
        const maxTextTop = Math.min(image.rect.bottom + 260, nextImageTop - 6);
        const maxTextBottom = Math.min(image.rect.bottom + 310, nextImageTop - 4);

        for (const el of Array.from(document.querySelectorAll(textSelectors))) {
            if (!isVisible(el, true)) continue;
            const rect = el.getBoundingClientRect();

            // Strict visual rule: text must be directly below the selected image only.
            if (rect.top < image.rect.bottom - 4) continue;
            if (rect.top > maxTextTop || rect.bottom > maxTextBottom) continue;

            const overlap = horizontalOverlapRatio(rect, image.rect);
            const alignedByLeft = Math.abs(rect.left - image.rect.left) <= 70;
            const alignedByCenter = Math.abs((rect.left + rect.width / 2) - (image.rect.left + image.rect.width / 2)) <= Math.max(120, image.rect.width * 0.35);
            if (!(overlap >= 0.28 || alignedByLeft || alignedByCenter)) continue;

            const key = `${String(el.id || '')} ${String(el.className || '')} ${String(el.getAttribute('aria-label') || '')}`.toLowerCase();
            if (/(action|button|install|more-vert|menu|icon|logo|svg)/.test(key)) continue;

            const classIsDirectText = /(app-title|app-text|headline|description|desc|title|body|subtitle|long-description|text)/i.test(key);
            const ownText = getOwnText(el);
            let txt = ownText || cleanText(el.innerText || el.textContent || '');

            // Avoid wrappers that combine multiple unrelated texts. Only accept wrappers if their class is a known text class.
            if (el.childElementCount > 0 && !classIsDirectText) continue;
            if (el.childElementCount > 0 && classIsDirectText && ownText.length < 3) {
                txt = cleanText(el.innerText || el.textContent || '');
            }

            if (!textLooksUseful(txt)) continue;

            const style = window.getComputedStyle(el);
            const fontSize = parseFloat(style.fontSize || '0');
            const fontWeight = parseInt(style.fontWeight || '400', 10) || 400;

            let score = 0;
            score += 1200;
            score += Math.max(0, 260 - Math.abs(rect.top - image.rect.bottom)) * 4;
            score += overlap * 450;
            if (/(app-title|headline|title)/.test(key)) score += 850;
            if (/(app-text|description|desc|body|subtitle|long-description)/.test(key)) score += 780;
            score += Math.min(fontSize, 34) * 8;
            if (fontWeight >= 600) score += 180;

            textItems.push({
                text: txt,
                key,
                score,
                rect: {top: rect.top, left: rect.left, bottom: rect.bottom, right: rect.right, width: rect.width, height: rect.height},
                fontSize,
                fontWeight
            });
        }

        // Deduplicate by text, keeping the visually strongest item.
        const byText = new Map();
        for (const item of textItems) {
            const k = item.text.toLowerCase();
            if (!byText.has(k) || byText.get(k).score < item.score) byText.set(k, item);
        }

        const items = Array.from(byText.values()).sort((a, b) => {
            const dy = a.rect.top - b.rect.top;
            if (Math.abs(dy) > 4) return dy;
            return b.score - a.score;
        });

        let headline = 'N/A';
        let description = 'N/A';

        const titleCandidate = items
            .filter(x => /(app-title|headline|title)/.test(x.key) && !/(action|button|install|bar)/.test(x.key))
            .sort((a, b) => b.score - a.score)[0];
        if (titleCandidate) headline = titleCandidate.text;

        const descCandidate = items
            .filter(x => x.text !== headline && /(app-text|description|desc|body|subtitle|long-description)/.test(x.key) && !/(action|button|install|bar)/.test(x.key))
            .sort((a, b) => b.score - a.score)[0];
        if (descCandidate) description = descCandidate.text;

        // If there are no class-labelled title/desc nodes, use visual order only.
        if (headline === 'N/A' && items[0]) headline = items[0].text;
        if (description === 'N/A') {
            const second = items.find(x => x.text !== headline);
            if (second) description = second.text;
        }

        return {
            image_url: image.url || 'N/A',
            headline: headline || 'N/A',
            description: description || 'N/A',
            candidate_score: image.score,
            image_rect: image.rect,
            debug: `imgCandidates=${imageCandidates.length}, visualTextItems=${items.length}, nextImageTop=${nextImageTop}`
        };
    }
    """
    try:
        return target.evaluate(js) or {"image_url": "N/A", "headline": "N/A", "description": "N/A", "candidate_score": 0}
    except Exception:
        return {"image_url": "N/A", "headline": "N/A", "description": "N/A", "candidate_score": 0}


def wait_and_extract_image_ad_details(page, max_wait_seconds=15):
    """
    Returns (data, target). Checks the main page and all frames, then chooses the best
    same-creative image candidate. The image candidate itself carries a very strong
    tpc/archive/simgad score and viewport score, so repeated ads lower on the page lose.
    """
    start_time = time.time()

    while time.time() - start_time < max_wait_seconds:
        candidates = []

        target_list = []
        # Main page is important because html-renderer often lives directly in the page DOM.
        target_list.append((page, "main_page", 0))

        for frame in page.frames:
            if frame == page.main_frame:
                continue

            parent_bonus = 0
            box = _frame_parent_box(frame)
            if box:
                width = box.get("width", 0) or 0
                height = box.get("height", 0) or 0
                y = box.get("y", 99999) or 99999
                area = width * height
                if width >= 120 and height >= 70:
                    parent_bonus += min(area / 5000, 140)
                else:
                    parent_bonus -= 160
                # Prefer active visible preview, not hidden/lower repeated ad slots.
                if -30 <= y <= 820:
                    parent_bonus += 180
                elif 820 < y <= 1100:
                    parent_bonus -= 80
                else:
                    parent_bonus -= 220
            else:
                parent_bonus -= 80

            target_list.append((frame, "iframe", parent_bonus))

        for target, kind, target_bonus in target_list:
            data = _extract_image_ad_details_from_target(target)
            image_url = _clean_image_url(data.get("image_url"))
            if image_url == "N/A":
                continue

            data["image_url"] = image_url
            data["target_kind"] = kind
            try:
                final_score = float(data.get("candidate_score", 0) or 0) + float(target_bonus or 0)
            except Exception:
                final_score = 0
            data["final_candidate_score"] = final_score
            candidates.append((final_score, data, target))

        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            return candidates[0][1], candidates[0][2]

        page.wait_for_timeout(1000)

    return {"image_url": "N/A", "headline": "N/A", "description": "N/A", "target_kind": "N/A"}, None


def _collect_target_text_for_package(target):
    """Collect DOM/script text from a target. This does not click or use visible Install links."""
    collected = []
    if target is None:
        return ""

    try:
        scripts = target.evaluate("""
            () => Array.from(document.scripts)
                .map(s => s.textContent || '')
                .filter(Boolean)
                .join('\n')
        """)
        if scripts:
            collected.append(scripts)
    except Exception:
        pass

    try:
        html = target.evaluate("() => document.documentElement ? document.documentElement.outerHTML : ''")
        if html:
            collected.append(html)
    except Exception:
        pass

    try:
        hrefs = target.evaluate("""
            () => Array.from(document.querySelectorAll('a[href]'))
                .map(a => a.href || a.getAttribute('href') || '')
                .filter(Boolean)
                .join('\n')
        """)
        if hrefs:
            collected.append(hrefs)
    except Exception:
        pass

    return "\n".join(collected)


def extract_packages_from_target(target):
    """Package candidates from the selected creative target only."""
    raw = _collect_target_text_for_package(target)
    if not raw:
        return set()
    return extract_packages_from_text(raw)


def _image_url_keys_for_context(image_url):
    """Keys from the selected image URL used to prove that an appId belongs to the same ad."""
    keys = []
    if not image_url or image_url == "N/A":
        return keys
    try:
        decoded = decode_all(image_url)
        keys.append(decoded)
        parsed = urlparse(decoded)
        parts = [p for p in parsed.path.split('/') if p]
        if parts:
            keys.append(parts[-1])
        # tpc image URLs often include numeric simgad IDs. Keep long numeric IDs only.
        for m in re.finditer(r"\b\d{6,}\b", decoded):
            keys.append(m.group(0))
    except Exception:
        pass
    return list(dict.fromkeys([k for k in keys if k]))


def _context_contains_text(context, value):
    if not value or value == "N/A":
        return False
    a = clean_text_for_comparison(context)
    b = clean_text_for_comparison(value)
    return bool(b and len(b) >= 6 and b in a)


def extract_appid_candidates_from_addata_text(raw_text, image_url=None, headline=None, description=None, source_priority=0, require_related=True):
    """
    Returns scored appId candidates from adData/script text.

    For IMAGE ads, require_related=True is critical:
    an appId is accepted only when the same adData/script context also contains the
    selected image URL/simgad id OR the selected headline/description. This prevents
    package names from another ad on the same Transparency page.
    """
    if not raw_text:
        return []

    text = decode_all(raw_text)
    candidates = []

    patterns = [
        r"""['\"]appId['\"]\s*:\s*['\"]([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})['\"]""",
        r"""\bappId\b\s*[:=]\s*['\"]([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})['\"]""",
        r"""['\"]app_id['\"]\s*:\s*['\"]([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})['\"]""",
        r"""\bapp_id\b\s*[:=]\s*['\"]([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})['\"]""",
    ]

    image_keys = _image_url_keys_for_context(image_url)

    for pattern in patterns:
        for m in re.finditer(pattern, text, flags=re.IGNORECASE):
            pkg = m.group(1).rstrip('.,;\'"\\ ')
            if not _is_valid_pkg(pkg):
                continue

            start = max(0, m.start() - 12000)
            end = min(len(text), m.end() + 12000)
            context = text[start:end]
            context_lower = context.lower()

            score = float(source_priority)
            related = False

            if re.search(r"\badData\b", context, flags=re.IGNORECASE):
                score += 180
            if 'appid' in context_lower or 'app_id' in context_lower:
                score += 40

            for key in image_keys:
                if key and key in context:
                    related = True
                    score += 900 if key == image_url else 650

            if _context_contains_text(context, headline):
                related = True
                score += 260
            if _context_contains_text(context, description):
                related = True
                score += 220

            # When searching a full page with many ads, never accept unrelated appId values.
            if require_related and not related:
                continue

            candidates.append({"package": pkg, "score": score, "related": related, "context_start": start})

    # Deduplicate package names, keeping highest score.
    best_by_pkg = {}
    for c in candidates:
        pkg = c["package"]
        if pkg not in best_by_pkg or c["score"] > best_by_pkg[pkg]["score"]:
            best_by_pkg[pkg] = c

    return sorted(best_by_pkg.values(), key=lambda x: x["score"], reverse=True)


def extract_appid_from_addata_text(raw_text, image_url=None, headline=None, description=None, source_priority=0, require_related=True):
    candidates = extract_appid_candidates_from_addata_text(
        raw_text,
        image_url=image_url,
        headline=headline,
        description=description,
        source_priority=source_priority,
        require_related=require_related,
    )
    return candidates[0]["package"] if candidates else None


def extract_appid_from_addata_target(target, image_url=None, headline=None, description=None, source_priority=0, require_related=True):
    raw = _collect_target_text_for_package(target)
    return extract_appid_from_addata_text(
        raw,
        image_url=image_url,
        headline=headline,
        description=description,
        source_priority=source_priority,
        require_related=require_related,
    )


def extract_appid_from_addata_page(page, image_target=None, image_url=None, headline=None, description=None):
    """
    Search adData.appId but only accept it when it is tied to the selected image/text.
    This means wrong packages from other ads are ignored and N/A is written instead.
    """
    all_candidates = []

    def add_candidates_from_target(target, priority):
        raw = _collect_target_text_for_package(target)
        all_candidates.extend(
            extract_appid_candidates_from_addata_text(
                raw,
                image_url=image_url,
                headline=headline,
                description=description,
                source_priority=priority,
                require_related=True,
            )
        )

    if image_target is not None:
        add_candidates_from_target(image_target, 450)

    try:
        add_candidates_from_target(page, 120)
    except Exception:
        pass

    try:
        for frame in page.frames:
            if image_target is not None and frame == image_target:
                continue
            add_candidates_from_target(frame, 80)
    except Exception:
        pass

    if not all_candidates:
        return None

    best_by_pkg = {}
    for c in all_candidates:
        pkg = c["package"]
        if pkg not in best_by_pkg or c["score"] > best_by_pkg[pkg]["score"]:
            best_by_pkg[pkg] = c

    best = sorted(best_by_pkg.values(), key=lambda x: x["score"], reverse=True)[0]
    return best["package"]


def resolve_image_ad_package(headline, description, image_target, page=None, image_url=None):
    """
    Image ads package flow:
    1) Compare package candidates from the selected creative target with headline + description.
    2) If no reliable match, read script var adData -> appId only when tied to the same image/text.
    3) If still not found, return N/A.

    Visible Install/Get links are NOT used for image ads.
    """
    best_score = 0.0
    candidates = set()

    # Only same target. Do not scan all page package names for matching; that caused cross-ad contamination.
    if image_target is not None:
        candidates.update(extract_packages_from_target(image_target))

    if is_valid_text_ad(headline, description) and candidates:
        matched_pkg, best_score = get_best_matching_package(headline, description, candidates)
        if matched_pkg:
            return matched_pkg, best_score, "same_target_headline_description_match"

    # adData fallback is strict: appId must share context with the selected image URL/simgad id or selected text.
    app_id = None
    if page is not None:
        app_id = extract_appid_from_addata_page(
            page,
            image_target=image_target,
            image_url=image_url,
            headline=headline,
            description=description,
        )
    elif image_target is not None:
        app_id = extract_appid_from_addata_target(
            image_target,
            image_url=image_url,
            headline=headline,
            description=description,
            source_priority=450,
            require_related=True,
        )

    if app_id:
        return app_id, best_score, "related_addata_appId_fallback"

    return "N/A", best_score, "not_found"


def scrape_single_url(url_row):
    row_num, url = url_row

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
            ]
        )

        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            service_workers="block",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )

        page = context.new_page()
        captured = {"video_id": "N/A"}

        # ORIGINAL VIDEO RESPONSE HANDLER - kept unchanged.
        def handle_response(response):
            try:
                if not is_real_video_response(response):
                    return

                video_id = extract_video_id_from_url(response.url)

                if video_id and captured["video_id"] == "N/A":
                    captured["video_id"] = video_id

            except Exception:
                pass

        page.on("response", handle_response)

        try:
            if "region=" not in url:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}region=anywhere"

            print(f"🔍 Row {row_num}: opening transparency URL")

            safe_add_log(
                row_number=row_num,
                status="STARTED",
                log_type="COMBINED",
                url=url,
                message="Started combined video/text/image ad extraction"
            )

            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)

            advertiser = extract_advertiser_from_page(page)

            # VIDEO LOGIC: same original flow. No text/image extraction runs before this.
            video_id = detect_video_id(page, captured)
            video_time = get_exact_time()

            # =========================
            # VIDEO AD PATH
            # =========================
            if video_id != "N/A":
                print(f"🎬 Row {row_num}: video ID found first: {video_id}")

                app_link = wait_and_extract_install_link(page, max_wait_seconds=35)
                app_link_time = get_exact_time()

                headline, description = wait_and_extract_headline_description(page, max_wait_seconds=15)

                if app_link == "N/A":
                    status = "VIDEO_FOUND_APP_LINK_NOT_FOUND"
                    message = "Video ID found, but exact visible install link not found"
                else:
                    status = "SUCCESS"
                    message = "Video ID and app link saved"

                package_name = extract_package_name(app_link)

                data = [
                    advertiser,
                    package_name,
                    url,
                    app_link,
                    app_link_time,
                    video_id,      # Column F: actual video ID for video ads
                    video_time
                ]

                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, headline, description)

                safe_add_log(
                    row_number=row_num,
                    status=status,
                    log_type="VIDEO_AD",
                    url=url,
                    video_id=video_id,
                    app_link=app_link,
                    message=message
                )

                print(f"✅ Row {row_num}: saved VIDEO ad advertiser + package + video ID + text")
                return

            # =========================
            # NON-VIDEO PATH: IMAGE ADS FIRST, THEN TEXT ADS
            # =========================
            print(f"📄 Row {row_num}: no video found, checking image/text ad")
            process_time = get_exact_time()

            # IMAGE AD PATH
            # Extract image URL + headline + description from the SAME creative/frame.
            # Do NOT use visible Install/Get links for image ads.
            image_data, image_target = wait_and_extract_image_ad_details(page, max_wait_seconds=15)
            image_url = clean_text(image_data.get("image_url"))

            if image_url != "N/A":
                headline = clean_text(image_data.get("headline"))
                description = clean_text(image_data.get("description"))
                has_text = is_valid_text_ad(headline, description)

                print(f"🖼 Row {row_num}: image ad found -> {image_url}")
                print(f"🔎 Row {row_num}: image headline -> {headline}")
                print(f"🔎 Row {row_num}: image description -> {description}")
                print(f"📦 Row {row_num}: resolving IMAGE package by headline/description, then adData.appId only")

                package_name, match_score, package_source = resolve_image_ad_package(headline, description, image_target, page=page, image_url=image_url)

                if package_name != "N/A":
                    app_link = f"https://play.google.com/store/apps/details?id={package_name}"
                    status = "SUCCESS"
                    message = (
                        f"Image ad saved. Package source={package_source}. "
                        f"Headline/description match score={match_score}. Visible install link was not used."
                    )
                    print(f"✅ Row {row_num}: image package -> {package_name} | source={package_source} | score={match_score}")
                else:
                    app_link = "N/A"
                    status = "IMAGE_PACKAGE_NOT_FOUND"
                    message = (
                        f"Image ad saved, but package not found. Best headline/description score={match_score}. "
                        "adData.appId fallback also not found. Visible install link was not used."
                    )
                    print(f"⚠️ Row {row_num}: image package not found, writing N/A | best score={match_score}")

                data = [
                    advertiser,
                    package_name,
                    url,
                    app_link,
                    process_time,
                    image_url,      # Column F: image URL for image ads
                    process_time
                ]

                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, headline if has_text else "N/A", description if has_text else "N/A")

                safe_add_log(
                    row_number=row_num,
                    status=status,
                    log_type="IMAGE_AD",
                    url=url,
                    video_id=image_url,
                    app_link=app_link,
                    message=message
                )

                print(f"✅ Row {row_num}: saved IMAGE ad advertiser + package + image URL + headline + description")
                return

            # TEXT AD PATH
            # Keep your old text-ad logic because it is already working for text ads.
            text_data = wait_and_extract_text_ad_details(page, max_wait_seconds=15)
            headline = clean_text(text_data.get("headline"))
            description = clean_text(text_data.get("description"))
            has_text = is_valid_text_ad(headline, description)

            if not has_text:
                data = [
                    advertiser,
                    "N/A",
                    url,
                    "N/A",
                    process_time,
                    "N/A",
                    process_time
                ]

                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, "N/A", "N/A")

                safe_add_log(
                    row_number=row_num,
                    status="NO_VIDEO_NO_TEXT_IMAGE",
                    log_type="COMBINED",
                    url=url,
                    video_id="N/A",
                    app_link="N/A",
                    message="No video ID and no valid image/text creative found"
                )

                print(f"⏭ Row {row_num}: no video and no valid image/text ad found")
                return

            print(f"🔎 Row {row_num}: text headline -> {headline}")
            print(f"📦 Row {row_num}: resolving TEXT package from visible install link first")

            # Visible install link is still allowed for TEXT ads only.
            visible_app_link = wait_and_extract_install_link(page, max_wait_seconds=8)
            visible_package = extract_package_name(visible_app_link)

            if visible_package != "N/A":
                package_name = visible_package
                app_link = visible_app_link
                match_score = 1.0
                status = "SUCCESS"
                message = "Text ad package extracted from visible install link"
                print(f"✅ Row {row_num}: text package from visible install link -> {package_name}")
            else:
                print(f"📦 Row {row_num}: visible install link not found, strict matching with headline + description")
                all_found_packages = extract_package_from_page(page)
                package_name, match_score = get_best_matching_package(headline, description, all_found_packages)

                if package_name:
                    app_link = f"https://play.google.com/store/apps/details?id={package_name}"
                    status = "SUCCESS"
                    message = f"Text ad package strictly matched with score {match_score}"
                    print(f"✅ Row {row_num}: strict matched text package -> {package_name} | score={match_score}")
                else:
                    package_name = "N/A"
                    app_link = "N/A"
                    status = "TEXT_PACKAGE_NOT_FOUND"
                    message = f"Text ad found, but package score below 0.76. Best score={match_score}"
                    print(f"⚠️ Row {row_num}: text package score below 0.76, writing N/A | best score={match_score}")

            data = [
                advertiser,
                package_name,
                url,
                app_link,
                process_time,
                "text",      # Column F: text marker for text ads
                process_time
            ]

            safe_update_combined_row(row_num, data)
            safe_update_headline_desc(row_num, headline, description)

            safe_add_log(
                row_number=row_num,
                status=status,
                log_type="TEXT_AD",
                url=url,
                video_id="text",
                app_link=app_link,
                message=message
            )

            print(f"✅ Row {row_num}: saved TEXT ad advertiser + package + headline + description")

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
                    error_time
                ]

                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, "N/A", "N/A")
            except Exception:
                pass

            try:
                safe_add_log(
                    row_number=row_num,
                    status="ERROR",
                    log_type="COMBINED",
                    url=url,
                    message=str(e)
                )
            except Exception:
                pass

        finally:
            page.close()
            context.close()
            browser.close()

def run_parallel_combined_scraper(max_workers=2):
    urls = sheets.get_urls_with_retry()

    url_rows = [
        (i + 2, u.strip())
        for i, u in enumerate(urls)
        if u and u.strip()
    ]

    if not url_rows:
        print("No transparency URLs found in column H.")
        return

    print(f"🚀 Starting combined VIDEO + TEXT scraper for {len(url_rows)} rows")
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
                        log_type="COMBINED",
                        message=str(e)
                    )
                except Exception:
                    pass

    print("✅ Finished combined video + text scraping")


if __name__ == "__main__":
    run_parallel_combined_scraper(max_workers=MAX_WORKERS)
