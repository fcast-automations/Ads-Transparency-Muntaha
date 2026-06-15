# Combined Google Ads Transparency scraper
# Video-ad detection logic is kept from the original scrapper.txt.
# Non-video ads use text/image extraction + package matching from the uploaded non-video files.

from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs, unquote, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import difflib
import re
import html

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
    Returns True/False and never raises, so a small Sheets/log error cannot overwrite good scraped data.
    """
    with SHEET_LOCK:
        try:
            sheets.update_combined_row(row_num, data)
            return True
        except Exception as e:
            print(f"⚠️ Failed to update A-G for row {row_num}: {e}")
            return False


def safe_update_headline_desc(row_num, headline, description):
    """
    Thread-safe Google Sheet update for Headline and Description in cols M and N.
    Never raises.
    """
    with SHEET_LOCK:
        try:
            sheets.update_headline_and_description(row_num, headline, description)
            return True
        except Exception as e:
            print(f"⚠️ Failed to update headline/description for row {row_num}: {e}")
            return False


def safe_update_image_url(row_num, image_url):
    """
    Thread-safe Google Sheet update for Landscape Image URL in column O.
    Never raises. This prevents a column-O write issue from turning a successful row into ERROR.
    """
    image_url = clean_text(image_url)
    with SHEET_LOCK:
        try:
            if hasattr(sheets, "update_image_url"):
                sheets.update_image_url(row_num, image_url)
            elif hasattr(sheets, "get_sheet"):
                # Fallback for older sheets.py files. update_cell is compatible with more gspread versions.
                sheets.get_sheet().update_cell(row_num, 15, image_url)
            else:
                print("⚠️ sheets.update_image_url() missing. Add it to sheets.py to write column O.")
            return True
        except Exception as e:
            print(f"⚠️ Failed to update image URL for row {row_num}; keeping existing scraped data. Error: {e}")
            return False


def safe_add_log(row_number, status, log_type, url="", video_id="", app_link="", message=""):
    """
    Thread-safe log writing.
    Never raises. Logs should not be able to overwrite a successful scrape with ERROR.
    """
    with SHEET_LOCK:
        try:
            sheets.add_log(
                row_number=row_number,
                status=status,
                log_type=log_type,
                url=url,
                video_id=video_id,
                app_link=app_link,
                message=message
            )
            return True
        except Exception as e:
            print(f"⚠️ Failed to add log for row {row_number}: {e}")
            return False

def get_exact_time():
    return datetime.now().strftime("%I:%M:%S %p")


def clean_text(value):
    if not value:
        return "N/A"
    return re.sub(r"\s+", " ", str(value)).strip() or "N/A"

def _looks_like_bad_ad_copy(value):
    """Reject Google UI / CTA / metadata text that sometimes appears near the creative."""
    value = clean_text(value)
    if value == "N/A":
        return True

    lower = value.lower().strip(" .:-|•")
    exact_bad = {
        "install", "get", "download", "open", "learn more", "play", "skip",
        "sponsored", "ad", "ads", "menu", "search", "sign in", "privacy", "terms",
        "ads transparency center", "ads transparency centre", "see more ads", "report this ad",
        "about this ad", "why this ad", "ad details", "last shown", "shown in",
        "app store", "google play", "itunes", "visit site", "shop now", "watch now",
        "close", "dismiss", "hide", "x", "×", "ok", "cancel", "done", "back", "next",
        "feedback", "send feedback", "ad choices", "adchoices", "not interested",
        "stop seeing this ad", "always positive", "always negative"
    }
    contains_bad = [
        "ads transparency", "report this ad", "see more ads", "why this ad",
        "last shown", "shown in", "google llc", "doubleclick", "googleadservices",
        "play.google.com", "apps.apple.com", "http://", "https://", "www.",
        "privacy policy", "terms of service", "cookie", "all ads",
        "always positive", "always negative", "feedback", "ad choices", "adchoices",
        "stop seeing", "not interested", "close ad", "hide ad", "mute ad", "unmute",
        "choose a reason", "why am i seeing", "control the ads", "my ad center", "my ad centre"
    ]
    if lower in exact_bad:
        return True
    if re.fullmatch(r"always\s+(positive|negative|on|off|allow|deny)", lower):
        return True
    if any(b in lower for b in contains_bad):
        return True
    if re.fullmatch(r"[\d\W_]+", lower):
        return True
    if len(value) > 260:
        return True
    return False


def clean_extracted_ad_copy(value):
    """Final Python cleanup for headline/description before writing to Sheets."""
    value = clean_text(html.unescape(str(value or "")))
    if value == "N/A":
        return "N/A"

    # Remove repeated whitespace and common trailing CTA-only fragments.
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n-|•")
    value = re.sub(r"\s+(Install|Get|Download|Open|Learn More|Learn more)$", "", value).strip()

    if _looks_like_bad_ad_copy(value):
        return "N/A"
    return value or "N/A"


def normalize_copy_for_compare(value):
    value = clean_text(value)
    return re.sub(r"[\W_]+", "", value.lower(), flags=re.UNICODE)


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
    Video-ad headline/description extraction now uses the same safer visual extractor
    as non-video ads, instead of relying only on fragile class names.
    """
    data = wait_and_extract_text_ad_details(page, max_wait_seconds=max_wait_seconds)
    return data.get("headline", "N/A"), data.get("description", "N/A")


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
    Extract headline and description from the active creative.

    Important fix for image ads/card ads:
    - First looks for the largest visible ad image/media box.
    - Then reads the visible text directly BELOW that image.
    - This matches the layout shown in your screenshot:
          image
          Headline
          Description
          CTA / Ad label
    - Text printed inside the image itself is ignored.
    - Install/Get/Ad labels, menu dots, feedback text, and Google UI text are ignored.
    """
    js = r"""
    () => {
        const cleanText = (txt) => (txt || "")
            .replace(/\u00a0/g, " ")
            .replace(/\n/g, " ")
            .replace(/\s+/g, " ")
            .trim();

        const badExact = new Set([
            'install', 'get', 'download', 'open', 'learn more', 'play', 'skip',
            'sponsored', 'ad', 'ads', 'menu', 'search', 'sign in', 'privacy', 'terms',
            'ads transparency center', 'ads transparency centre', 'see more ads',
            'report this ad', 'about this ad', 'why this ad', 'ad details',
            'last shown', 'shown in', 'app store', 'google play', 'itunes',
            'visit site', 'shop now', 'watch now', 'details',
            'close', 'dismiss', 'hide', 'x', '×', 'ok', 'cancel', 'done', 'back', 'next',
            'feedback', 'send feedback', 'ad choices', 'adchoices', 'not interested',
            'stop seeing this ad', 'always positive', 'always negative',
            'main in price', 'main in prize', 'more options'
        ]);

        const badContains = [
            'ads transparency', 'report this ad', 'see more ads', 'why this ad',
            'last shown', 'shown in', 'google llc', 'doubleclick', 'googleadservices',
            'play.google.com', 'apps.apple.com', 'http://', 'https://', 'www.',
            'privacy policy', 'terms of service', 'cookie', 'all ads',
            'always positive', 'always negative', 'feedback', 'ad choices', 'adchoices',
            'stop seeing', 'not interested', 'close ad', 'hide ad', 'mute ad', 'unmute',
            'choose a reason', 'why am i seeing', 'control the ads', 'my ad center', 'my ad centre'
        ];

        const isBadUiText = (txt) => {
            const text = cleanText(txt);
            if (!text) return true;
            const lower = text.toLowerCase().replace(/^[\s.:-|•·]+|[\s.:-|•·]+$/g, '');
            if (!lower) return true;
            if (badExact.has(lower)) return true;
            if (/^ad\s*[·•:.,\-]/i.test(lower)) return true;          // Ad · MAIN IN PRICE
            if (/^[·•⋮︙.\-–—]+$/u.test(lower)) return true;          // menu dots / decorative symbols
            if (/^always\s+(positive|negative|on|off|allow|deny)$/i.test(lower)) return true;
            if (badContains.some(b => lower.includes(b))) return true;
            if (/^[\d\W_]+$/u.test(lower)) return true;
            if (text.length > 260) return true;
            return false;
        };

        const norm = (txt) => cleanText(txt).toLowerCase().replace(/[\W_]+/gu, '');

        const metaFor = (el) => {
            const cls = String(el?.className || '').toLowerCase();
            const aria = String(el?.getAttribute?.('aria-label') || '').toLowerCase();
            const role = String(el?.getAttribute?.('role') || '').toLowerCase();
            const tag = String(el?.tagName || '').toLowerCase();
            return {
                cls,
                aria,
                role,
                tag,
                isHeadlineClass: cls.includes('headline') || cls.includes('-e-15') || aria.includes('headline') || role === 'heading',
                isDescriptionClass: cls.includes('description') || cls.includes('long-description') || cls.includes('-e-67') || aria.includes('description'),
                isButtonLike: tag === 'button' || role === 'button' || cls.includes('button') || cls.includes('install')
            };
        };

        const visibleRect = (el, rect = null, viewportStrict = true) => {
            if (!el) return null;
            const r = rect || el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            if (r.width <= 1 || r.height <= 1) return null;
            if (viewportStrict) {
                if (r.bottom <= 0 || r.right <= 0 || r.top >= window.innerHeight || r.left >= window.innerWidth) return null;
            }
            if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') return null;
            return r;
        };

        const badImage = (el) => {
            const src = String(el?.currentSrc || el?.src || el?.getAttribute?.('src') || '').toLowerCase();
            const alt = String(el?.getAttribute?.('alt') || '').toLowerCase();
            const cls = String(el?.className || '').toLowerCase();
            if (src.includes('googlelogo') || alt.includes('google logo')) return true;
            if (src.includes('/branding/') && src.includes('google')) return true;
            if (src.includes('favicon') || src.endsWith('/favicon.ico')) return true;
            if (src.includes('adchoices') || cls.includes('adchoices')) return true;
            return false;
        };

        const getMediaBoxes = () => {
            const boxes = [];
            const addBox = (el, kind, bonus = 0) => {
                const r = visibleRect(el);
                if (!r) return;
                if (r.width < 110 || r.height < 70) return;
                if (badImage(el)) return;

                let score = r.width * r.height + bonus;
                const ratio = r.width / Math.max(r.height, 1);
                if (ratio >= 1.1 && ratio <= 3.5) score += 25000;      // common landscape ad image/card media
                if (r.top >= -10 && r.top <= 520) score += 10000;       // ad image is normally above text
                if (kind === 'background') score += 6000;
                if (kind === 'img') score += 7000;
                if (kind === 'canvas' || kind === 'svg') score += 3500;

                boxes.push({
                    el, kind, score,
                    top: r.top, bottom: r.bottom, left: r.left, right: r.right,
                    width: r.width, height: r.height,
                    centerX: r.left + r.width / 2,
                    centerY: r.top + r.height / 2
                });
            };

            for (const img of Array.from(document.querySelectorAll('img'))) {
                addBox(img, 'img', 7000);
            }
            for (const el of Array.from(document.querySelectorAll('picture, canvas, svg'))) {
                addBox(el, String(el.tagName || '').toLowerCase(), 3500);
            }
            for (const el of Array.from(document.querySelectorAll('body *'))) {
                const r = visibleRect(el);
                if (!r || r.width < 120 || r.height < 80) continue;
                const bg = window.getComputedStyle(el).backgroundImage || '';
                if (bg && bg !== 'none' && bg.includes('url(')) {
                    addBox(el, 'background', 6000);
                }
            }

            // Deduplicate nested boxes. Keep the bigger/more specific one.
            boxes.sort((a, b) => b.score - a.score);
            const kept = [];
            for (const b of boxes) {
                const duplicate = kept.some(k =>
                    Math.abs(k.left - b.left) <= 4 &&
                    Math.abs(k.top - b.top) <= 4 &&
                    Math.abs(k.width - b.width) <= 8 &&
                    Math.abs(k.height - b.height) <= 8
                );
                if (!duplicate) kept.push(b);
            }
            return kept.slice(0, 8);
        };

        const collectTextLines = () => {
            const textItems = [];
            if (!document.body) return [];

            const walker = document.createTreeWalker(
                document.body,
                NodeFilter.SHOW_TEXT,
                {
                    acceptNode: (node) => {
                        const text = cleanText(node.nodeValue || '');
                        if (text.length < 2 || isBadUiText(text)) return NodeFilter.FILTER_REJECT;
                        const el = node.parentElement;
                        if (!el) return NodeFilter.FILTER_REJECT;
                        const tag = String(el.tagName || '').toLowerCase();
                        if (['script', 'style', 'noscript', 'template'].includes(tag)) return NodeFilter.FILTER_REJECT;
                        const meta = metaFor(el);
                        if (meta.isButtonLike && isBadUiText(text)) return NodeFilter.FILTER_REJECT;
                        return NodeFilter.FILTER_ACCEPT;
                    }
                }
            );

            while (walker.nextNode()) {
                const node = walker.currentNode;
                const el = node.parentElement;
                try {
                    const range = document.createRange();
                    range.selectNodeContents(node);
                    const rects = Array.from(range.getClientRects());
                    const style = window.getComputedStyle(el);
                    const fontSize = parseFloat(style.fontSize || '0') || 0;
                    const rawWeight = String(style.fontWeight || '400');
                    const fontWeight = rawWeight === 'bold' ? 700 : (parseInt(rawWeight, 10) || 400);
                    const meta = metaFor(el);
                    const text = cleanText(node.nodeValue || '');

                    for (const rect of rects) {
                        const r = visibleRect(el, rect);
                        if (!r || r.width < 3 || r.height < 3) continue;
                        textItems.push({
                            text,
                            el,
                            top: r.top, bottom: r.bottom, left: r.left, right: r.right,
                            width: r.width, height: r.height,
                            centerX: r.left + r.width / 2,
                            centerY: r.top + r.height / 2,
                            fontSize, fontWeight,
                            ...meta
                        });
                    }
                } catch (e) {}
            }

            textItems.sort((a, b) => (a.top - b.top) || (a.left - b.left));
            const groups = [];
            for (const item of textItems) {
                let g = groups.find(group => Math.abs(group.centerY - item.centerY) <= 6);
                if (!g) {
                    g = { items: [], centerY: item.centerY };
                    groups.push(g);
                }
                g.items.push(item);
                g.centerY = (g.centerY * (g.items.length - 1) + item.centerY) / g.items.length;
            }

            const lines = [];
            for (const g of groups) {
                const items = g.items.slice().sort((a, b) => a.left - b.left);
                const rawText = cleanText(items.map(x => x.text).join(' '));
                if (!rawText || isBadUiText(rawText)) continue;
                const words = rawText.split(/\s+/).filter(Boolean);
                if (words.length > 26) continue;

                const left = Math.min(...items.map(x => x.left));
                const right = Math.max(...items.map(x => x.right));
                const top = Math.min(...items.map(x => x.top));
                const bottom = Math.max(...items.map(x => x.bottom));
                const fontSize = Math.max(...items.map(x => x.fontSize || 0));
                const fontWeight = Math.max(...items.map(x => x.fontWeight || 400));
                const meta = items.reduce((acc, x) => ({
                    isHeadlineClass: acc.isHeadlineClass || x.isHeadlineClass,
                    isDescriptionClass: acc.isDescriptionClass || x.isDescriptionClass,
                    isButtonLike: acc.isButtonLike || x.isButtonLike,
                    role: acc.role || x.role,
                    tag: acc.tag || x.tag
                }), {isHeadlineClass:false, isDescriptionClass:false, isButtonLike:false, role:'', tag:''});

                lines.push({
                    text: rawText,
                    top, bottom, left, right,
                    width: right - left,
                    height: bottom - top,
                    centerX: left + (right - left) / 2,
                    centerY: top + (bottom - top) / 2,
                    fontSize, fontWeight,
                    el: items[0].el,
                    ...meta
                });
            }

            // Deduplicate exact normalized text/position.
            const deduped = [];
            const seen = new Set();
            for (const line of lines.sort((a, b) => (a.top - b.top) || (a.left - b.left))) {
                const key = `${norm(line.text)}:${Math.round(line.top / 4)}:${Math.round(line.left / 8)}`;
                if (!norm(line.text) || seen.has(key)) continue;
                seen.add(key);
                deduped.push(line);
            }
            return deduped;
        };

        const pickBelowMediaText = () => {
            const mediaBoxes = getMediaBoxes();
            const lines = collectTextLines();
            if (!mediaBoxes.length || !lines.length) return null;

            for (const media of mediaBoxes) {
                const nearby = lines
                    .filter(line => {
                        if (line.text.length > 180) return false;
                        if (line.isButtonLike && isBadUiText(line.text)) return false;

                        const gap = line.top - media.bottom;
                        if (gap < -3 || gap > 270) return false;       // must be below image, not printed inside image

                        const overlap = Math.min(line.right, media.right) - Math.max(line.left, media.left);
                        const centerAligned = line.centerX >= media.left - 65 && line.centerX <= media.right + 65;
                        const enoughOverlap = overlap >= Math.min(line.width, media.width) * 0.18;
                        if (!centerAligned && !enoughOverlap) return false;

                        // Avoid tiny labels/icons near the bottom row.
                        if (line.height < 5 || line.width < 12) return false;
                        return true;
                    })
                    .sort((a, b) => (a.top - b.top) || (a.left - b.left));

                const valid = [];
                const seenText = new Set();
                for (const line of nearby) {
                    const key = norm(line.text);
                    if (!key || seenText.has(key)) continue;
                    seenText.add(key);
                    valid.push(line);
                }

                if (!valid.length) continue;

                // The first real line below the image is the headline. In the screenshot this is:
                // "Calculator Lock - Hide Photos".
                const headlineLine = valid.find(line => {
                    if (line.text.length < 3 || line.text.length > 120) return false;
                    if (isBadUiText(line.text)) return false;
                    return true;
                });
                if (!headlineLine) continue;

                const descriptionLine = valid.find(line => {
                    if (line === headlineLine) return false;
                    if (line.top < headlineLine.bottom - 2) return false;
                    if (line.top - headlineLine.bottom > 95) return false;
                    if (line.text.length < 3 || line.text.length > 180) return false;
                    if (isBadUiText(line.text)) return false;
                    if (norm(line.text) === norm(headlineLine.text)) return false;
                    if (norm(line.text) && norm(headlineLine.text).includes(norm(line.text))) return false;
                    return true;
                });

                return {
                    headline: headlineLine.text,
                    description: descriptionLine ? descriptionLine.text : 'N/A',
                    method: 'below-media'
                };
            }
            return null;
        };

        const belowMedia = pickBelowMediaText();
        if (belowMedia && belowMedia.headline && belowMedia.headline !== 'N/A') {
            return belowMedia;
        }

        // Fallback for text-only creatives and layouts without a separate image/media box.
        const candidates = [];
        const addCandidate = (rawText, el, rect, source, boost = 0) => {
            const text = cleanText(rawText);
            if (isBadUiText(text)) return;
            const words = text.split(/\s+/).filter(Boolean);
            if (words.length > 24) return; // usually a wrapper/container, not an ad line

            const r = visibleRect(el, rect);
            if (!r) return;
            if (r.width < 5 || r.height < 5) return;

            const style = window.getComputedStyle(el);
            const fontSize = parseFloat(style.fontSize || '0') || 0;
            const rawWeight = String(style.fontWeight || '400');
            const fontWeight = rawWeight === 'bold' ? 700 : (parseInt(rawWeight, 10) || 400);
            const meta = metaFor(el);

            candidates.push({
                text,
                top: r.top,
                bottom: r.bottom,
                left: r.left,
                right: r.right,
                width: r.width,
                height: r.height,
                fontSize,
                fontWeight,
                source,
                boost,
                ...meta
            });
        };

        const explicitSelectors = [
            '[class*="-e-15"]', '[class*="headline"]', '[aria-label*="Headline"]', '[aria-label*="headline"]',
            '[role="heading"]', 'h1', 'h2', 'h3', 'h4',
            'div[role="link"] span', 'a[role="link"]',
            '[class*="-e-67"]', '[class*="long-description"]', '[class*="description"]',
            '[aria-label*="Description"]', '[aria-label*="description"]'
        ];

        for (const el of Array.from(document.querySelectorAll(explicitSelectors.join(',')))) {
            const text = cleanText(el.innerText || el.textContent || '');
            if (!text) continue;
            const meta = metaFor(el);
            const boost = meta.isHeadlineClass ? 600 : (meta.isDescriptionClass ? 420 : 120);
            addCandidate(text, el, el.getBoundingClientRect(), 'explicit', boost);
        }

        const textLines = collectTextLines();
        for (const line of textLines) {
            const metaBoost = line.isHeadlineClass ? 320 : (line.isDescriptionClass ? 240 : 0);
            addCandidate(line.text, line.el, line, 'text-line', 60 + metaBoost);
        }

        if (!candidates.length) {
            return { headline: 'N/A', description: 'N/A', method: 'none' };
        }

        const uniqueByText = new Map();
        for (const c of candidates) {
            const key = norm(c.text);
            if (!key) continue;
            let preScore = c.boost + c.fontSize * 100 + Math.min((c.width * c.height) / 25, 220);
            if (c.fontWeight >= 600) preScore += 70;
            if (c.source === 'text-line') preScore += 50;
            if (c.isButtonLike) preScore -= 500;
            const prev = uniqueByText.get(key);
            if (!prev || preScore > prev.preScore) {
                uniqueByText.set(key, { ...c, preScore });
            }
        }
        const unique = Array.from(uniqueByText.values());

        const headlineCandidates = unique.map(c => {
            const words = c.text.split(/\s+/).filter(Boolean).length;
            let score = c.boost;
            score += c.fontSize * 100;
            score += Math.min((c.width * c.height) / 25, 220);
            if (c.fontWeight >= 600) score += 80;
            if (c.isHeadlineClass) score += 700;
            if (c.role === 'link') score += 80;
            if (c.source === 'text-line') score += 70;
            if (c.isDescriptionClass) score -= 180;
            if (c.isButtonLike) score -= 600;
            if (c.text.length > 105) score -= 100;
            if (words > 16) score -= 150;
            if (c.top < -10 || c.top > window.innerHeight - 5) score -= 300;
            if (c.top > 760) score -= 120;
            return { ...c, headlineScore: score };
        }).sort((a, b) => b.headlineScore - a.headlineScore);

        const headlineObj = headlineCandidates[0] || null;
        const headline = headlineObj ? headlineObj.text : 'N/A';

        let descCandidates = [];
        if (headlineObj) {
            descCandidates = unique
                .filter(c => norm(c.text) !== norm(headlineObj.text))
                .filter(c => c.text.length >= 5)
                .map(c => {
                    const distanceBelow = c.top - headlineObj.bottom;
                    const absVertical = Math.abs(c.top - headlineObj.bottom);
                    let score = 0;
                    if (c.isDescriptionClass) score += 750;
                    if (distanceBelow >= -6 && distanceBelow <= 260) score += 520 - Math.min(Math.max(distanceBelow, 0), 260);
                    else if (absVertical <= 90) score += 180 - absVertical;
                    else score -= Math.min(absVertical, 260);
                    if (c.fontSize <= headlineObj.fontSize + 2) score += 90;
                    if (c.fontSize < headlineObj.fontSize) score += 60;
                    if (c.fontSize > headlineObj.fontSize + 4) score -= 120;
                    score += Math.min(c.text.length, 190) / 2;
                    score -= Math.min(Math.abs(c.left - headlineObj.left) / 4, 85);
                    if (c.isHeadlineClass || c.role === 'link') score -= 80;
                    if (c.isButtonLike) score -= 600;
                    if (c.top < -10 || c.top > window.innerHeight - 5) score -= 300;
                    return { ...c, descScore: score };
                })
                .sort((a, b) => b.descScore - a.descScore);
        }

        let descriptionObj = descCandidates.length ? descCandidates[0] : null;
        if (!descriptionObj) {
            const explicitDesc = unique
                .filter(c => norm(c.text) !== norm(headline))
                .filter(c => c.isDescriptionClass && c.text.length >= 5)
                .sort((a, b) => b.text.length - a.text.length);
            descriptionObj = explicitDesc.length ? explicitDesc[0] : null;
        }

        const description = descriptionObj ? descriptionObj.text : 'N/A';
        return { headline, description, method: 'visual-candidate' };
    }
    """

    def read_target(target, required_method=None):
        try:
            data = target.evaluate(js)
            if not data:
                return None
            if required_method and data.get("method") != required_method:
                return None

            headline = clean_extracted_ad_copy(data.get("headline"))
            description = clean_extracted_ad_copy(data.get("description"))

            # Avoid writing the same text in both columns.
            if headline != "N/A" and description != "N/A":
                if normalize_copy_for_compare(headline) == normalize_copy_for_compare(description):
                    description = "N/A"
                elif normalize_copy_for_compare(description) and normalize_copy_for_compare(description) in normalize_copy_for_compare(headline):
                    description = "N/A"

            if headline != "N/A" or description != "N/A":
                return {"headline": headline, "description": description, "method": data.get("method", "unknown")}
        except Exception:
            return None
        return None

    start_time = time.time()

    while time.time() - start_time < max_wait_seconds:
        seen_targets = set()

        try:
            ranked_targets = get_ranked_non_video_targets(page)
        except Exception:
            ranked_targets = []

        # 1) Very reliable pass: find text directly below the ad image/media.
        # This is the layout from the screenshot and prevents picking text inside the image or Google UI.
        for score, target, label, _ in ranked_targets:
            if id(target) in seen_targets:
                continue
            seen_targets.add(id(target))
            data = read_target(target, required_method="below-media")
            if data:
                return data

        for frame in page.frames:
            if frame == page.main_frame or id(frame) in seen_targets:
                continue
            seen_targets.add(id(frame))
            data = read_target(frame, required_method="below-media")
            if data:
                return data

        data = read_target(page, required_method="below-media")
        if data:
            return data

        # 2) Fallback pass for pure text ads or creatives without a separate image/media box.
        seen_targets.clear()
        for score, target, label, _ in ranked_targets:
            if id(target) in seen_targets:
                continue
            if label == "main_page" and score < 220:
                continue
            seen_targets.add(id(target))
            data = read_target(target)
            if data:
                return data

        for frame in page.frames:
            if frame == page.main_frame or id(frame) in seen_targets:
                continue
            data = read_target(frame)
            if data:
                return data

        if not ranked_targets:
            data = read_target(page)
            if data:
                return data

        page.wait_for_timeout(1000)

    return {"headline": "N/A", "description": "N/A", "method": "timeout"}

def clean_image_url(raw_url, base_url=""):
    """Normalize image URLs found in src/srcset/background-image."""
    if not raw_url:
        return "N/A"

    raw_url = str(raw_url).strip().strip('"\'')
    if not raw_url or raw_url.lower() in {"none", "null", "undefined"}:
        return "N/A"

    # Avoid writing huge base64 images into the sheet.
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


def extract_image_url_from_target(target):
    """
    Extracts the actual visible image URL from the same DOM attributes you see in Inspect Element:
    img.currentSrc/src/srcset, picture source srcset, SVG image href, and CSS background-image URL.
    """
    js = r"""
    () => {
        const absUrl = (raw) => {
            if (!raw) return '';
            raw = String(raw).trim().replace(/^['"]|['"]$/g, '');
            if (!raw || raw === 'none') return '';
            if (raw.startsWith('data:image')) return '';
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

        const isVisibleBox = (el, minW = 80, minH = 50) => {
            if (!el) return null;
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
            if (lower.startsWith('data:image')) return true;
            if (lower.includes('googlelogo') || alt.includes('google logo')) return true;
            if (lower.includes('/branding/') && lower.includes('google')) return true;
            if (lower.includes('favicon') || lower.endsWith('/favicon.ico')) return true;
            if (lower.includes('doubleclick') && lower.includes('adchoices')) return true;
            return false;
        };

        const candidates = [];

        const addCandidate = (rawUrl, el, kind, bonus = 0) => {
            const url = absUrl(rawUrl);
            if (!url || badImage(url, el)) return;
            const rect = isVisibleBox(el);
            if (!rect) return;

            let score = rect.width * rect.height;
            score += bonus;
            if (kind.includes('currentSrc')) score += 5000;
            if (kind.includes('srcset')) score += 3000;
            if (kind.includes('background')) score += 2000;
            if (url.startsWith('blob:')) score -= 50000;

            candidates.push({
                url,
                kind,
                score,
                width: rect.width,
                height: rect.height,
                top: rect.top,
                left: rect.left
            });
        };

        for (const img of Array.from(document.querySelectorAll('img'))) {
            addCandidate(img.currentSrc, img, 'img-currentSrc', 5000);
            addCandidate(img.getAttribute('src'), img, 'img-src', 4000);
            addCandidate(pickBestFromSrcset(img.getAttribute('srcset')), img, 'img-srcset', 4500);

            for (const attr of ['data-src', 'data-lazy-src', 'data-original', 'data-image', 'data-image-url', 'data-thumbnail-url', 'data-iurl']) {
                addCandidate(img.getAttribute(attr), img, `img-${attr}`, 2000);
            }
        }

        for (const source of Array.from(document.querySelectorAll('picture source[srcset], source[srcset]'))) {
            const picture = source.closest('picture');
            const visualEl = picture?.querySelector('img') || picture || source;
            addCandidate(pickBestFromSrcset(source.getAttribute('srcset')), visualEl, 'source-srcset', 3500);
        }

        for (const svgImage of Array.from(document.querySelectorAll('image'))) {
            addCandidate(svgImage.getAttribute('href') || svgImage.getAttribute('xlink:href'), svgImage, 'svg-image', 2500);
        }

        // Background-image creatives often show the URL in Inspect Element under computed CSS.
        for (const el of Array.from(document.querySelectorAll('body *'))) {
            const rect = isVisibleBox(el, 120, 80);
            if (!rect) continue;
            const bg = window.getComputedStyle(el).backgroundImage || '';
            if (!bg || bg === 'none' || !bg.includes('url(')) continue;

            const matches = Array.from(bg.matchAll(/url\((['"]?)(.*?)\1\)/g));
            for (const match of matches) {
                addCandidate(match[2], el, 'background-image', 2500);
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
        return deduped[0].url || null;
    }
    """
    try:
        base_url = getattr(target, "url", "") or ""
        return clean_image_url(target.evaluate(js), base_url=base_url)
    except Exception:
        return "N/A"


def wait_and_extract_image_url(page, max_wait_seconds=15):
    """
    Waits for the active image creative and returns the visible image URL for column O.
    """
    start_time = time.time()

    while time.time() - start_time < max_wait_seconds:
        seen_targets = set()

        try:
            ranked_targets = get_ranked_non_video_targets(page)
        except Exception:
            ranked_targets = []

        for _, target, _, _ in ranked_targets:
            if id(target) in seen_targets:
                continue
            seen_targets.add(id(target))
            image_url = extract_image_url_from_target(target)
            if image_url != "N/A":
                return image_url

        image_url = extract_image_url_from_target(page)
        if image_url != "N/A":
            return image_url

        for frame in page.frames:
            if frame == page.main_frame or id(frame) in seen_targets:
                continue
            image_url = extract_image_url_from_target(frame)
            if image_url != "N/A":
                return image_url

        page.wait_for_timeout(1000)

    return "N/A"


# =========================
# MAIN COMBINED SCRAPER: VIDEO ADS + TEXT ADS
# =========================

def is_valid_text_ad(headline, description):
    """
    Treat it as a text ad only when we have a real headline.
    Description-only results are too risky because Google UI text such as
    close/feedback/ad-choice labels can appear inside the same iframe.
    """
    headline = clean_extracted_ad_copy(headline)
    description = clean_extracted_ad_copy(description)

    if headline != "N/A" and len(clean_text(headline)) >= 3:
        return True

    # Do not let description-only text turn an image ad into a fake text ad.
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

        # Once this becomes True, the exception handler will NOT overwrite the row with ERROR.
        row_written = False

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

                row_written = safe_update_combined_row(row_num, data) or row_written
                safe_update_headline_desc(row_num, headline, description)
                safe_update_image_url(row_num, "N/A")

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
            # NON-VIDEO PATH: TEXT + IMAGE ADS
            # =========================
            print(f"📄 Row {row_num}: no video found, checking text/image ad")

            text_data = wait_and_extract_text_ad_details(page, max_wait_seconds=15)
            headline = clean_extracted_ad_copy(text_data.get("headline"))
            description = clean_extracted_ad_copy(text_data.get("description"))

            # Rows like 6/10 were caused by UI-only text: headline="close" and
            # description="Always positive/negative". If the headline is not real,
            # do not keep a description-only value.
            if headline == "N/A":
                description = "N/A"

            process_time = get_exact_time()
            has_text = is_valid_text_ad(headline, description)

            # Extract the visible creative image URL for column O.
            image_url = wait_and_extract_image_url(page, max_wait_seconds=12)
            if image_url != "N/A":
                print(f"🖼 Row {row_num}: image URL found -> {image_url[:120]}")

            # First try visible install/app link from the active creative.
            visible_app_link = wait_and_extract_install_link(page, max_wait_seconds=8)
            visible_package = extract_package_name(visible_app_link)

            is_image_like = image_url != "N/A" or has_visible_image_creative(page)
            ad_type = "image" if (is_image_like or (not has_text and visible_package != "N/A")) else "text" if has_text else "N/A"

            if not has_text and visible_package == "N/A" and not is_image_like:
                data = [
                    advertiser,
                    "N/A",
                    url,
                    "N/A",
                    process_time,
                    "N/A",
                    process_time
                ]

                row_written = safe_update_combined_row(row_num, data) or row_written
                safe_update_headline_desc(row_num, "N/A", "N/A")
                safe_update_image_url(row_num, "N/A")

                safe_add_log(
                    row_number=row_num,
                    status="NO_VIDEO_NO_TEXT_IMAGE",
                    log_type="COMBINED",
                    url=url,
                    video_id="N/A",
                    app_link="N/A",
                    message="No video ID and no valid text/image creative found"
                )

                print(f"⏭ Row {row_num}: no video and no valid text/image ad found")
                return

            if has_text:
                print(f"🔎 Row {row_num}: text/image headline -> {headline}")
            else:
                print(f"🖼 Row {row_num}: likely image ad, headline/description not found")

            print(f"📦 Row {row_num}: resolving package from visible install link first")

            if visible_package != "N/A":
                package_name = visible_package
                app_link = visible_app_link
                match_score = 1.0
                status = "SUCCESS"
                message = f"Non-video {ad_type} ad package extracted from visible install link"
                print(f"✅ Row {row_num}: package from visible install link -> {package_name}")
            else:
                package_name = None
                match_score = 0.0

                if has_text:
                    print(f"📦 Row {row_num}: visible install link not found, strict matching with headline + description")
                    all_found_packages = extract_package_from_page(page)
                    package_name, match_score = get_best_matching_package(headline, description, all_found_packages)

                if package_name:
                    app_link = f"https://play.google.com/store/apps/details?id={package_name}"
                    status = "SUCCESS"
                    message = f"Non-video {ad_type} ad package strictly matched with score {match_score}"
                    print(f"✅ Row {row_num}: strict matched package -> {package_name} | score={match_score}")
                else:
                    package_name = "N/A"
                    app_link = "N/A"
                    status = "NON_VIDEO_PACKAGE_NOT_FOUND"
                    message = f"Non-video {ad_type} ad found, but package score below 0.76. Best score={match_score}"
                    print(f"⚠️ Row {row_num}: package score below 0.76, writing N/A | best score={match_score}")

            data = [
                advertiser,
                package_name,
                url,
                app_link,
                process_time,
                ad_type,      # Column F: text/image for non-video ads
                process_time
            ]

            row_written = safe_update_combined_row(row_num, data) or row_written
            safe_update_headline_desc(row_num, headline if has_text else "N/A", description if has_text else "N/A")
            safe_update_image_url(row_num, image_url)

            safe_add_log(
                row_number=row_num,
                status=status,
                log_type="NON_VIDEO_AD",
                url=url,
                video_id=ad_type,
                app_link=app_link,
                message=message
            )

            print(f"✅ Row {row_num}: saved NON-VIDEO {ad_type} ad advertiser + package + headline + description + image URL")

        except Exception as e:
            error_time = get_exact_time()
            print(f"❌ Row {row_num} error at {error_time}: {e}")

            # Important fix: if the scraper already saved correct data, do NOT overwrite it with ERROR.
            # This is what made the row look correct for a second and then change to ERROR.
            if row_written:
                print(f"⚠️ Row {row_num}: error happened after data was already saved. Keeping saved row, not writing ERROR.")
                safe_add_log(
                    row_number=row_num,
                    status="POST_SAVE_ERROR_IGNORED",
                    log_type="COMBINED",
                    url=url,
                    message=str(e)
                )
            else:
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
                    safe_update_image_url(row_num, "N/A")
                except Exception:
                    pass

                safe_add_log(
                    row_number=row_num,
                    status="ERROR",
                    log_type="COMBINED",
                    url=url,
                    message=str(e)
                )

        finally:
            # Closing Playwright objects can sometimes raise after a successful scrape.
            # Never let close errors affect the saved row.
            for close_name, close_func in [
                ("page", page.close),
                ("context", context.close),
                ("browser", browser.close),
            ]:
                try:
                    close_func()
                except Exception as close_error:
                    print(f"⚠️ Row {row_num}: ignored {close_name}.close() error: {close_error}")

def run_parallel_combined_scraper(max_workers=2, only_unprocessed=True, row_filter=None):
    """
    Runs scraper in parallel.

    Default behavior is safer now:
    - uses sheets.get_agent_rows_snapshot() when available
    - skips rows that already have a value in column F
    - retries rows where column F is ERROR, if you use the updated sheets.py

    To force a full rescrape, call:
        run_parallel_combined_scraper(max_workers=MAX_WORKERS, only_unprocessed=False)

    To rerun selected wrong rows only, pass row_filter={5, 8, 12}.
    """
    url_rows = []

    try:
        if hasattr(sheets, "get_agent_rows_snapshot"):
            rows = sheets.get_agent_rows_snapshot()
            url_rows = [
                (r["row_num"], str(r["url"]).strip())
                for r in rows
                if r.get("url") and str(r.get("url")).strip()
                and (row_filter is None or r.get("row_num") in row_filter)
                and (not only_unprocessed or not r.get("processed"))
            ]
        else:
            raise AttributeError("sheets.get_agent_rows_snapshot is not available")
    except Exception as e:
        print(f"⚠️ Could not use agent snapshot, falling back to column H list: {e}")
        urls = sheets.get_urls_with_retry()
        url_rows = [
            (i + 2, u.strip())
            for i, u in enumerate(urls)
            if u and u.strip()
            and (row_filter is None or (i + 2) in row_filter)
        ]

    if not url_rows:
        print("No transparency URLs found to process. Existing completed rows were skipped.")
        return

    if row_filter is not None:
        print(f"🎯 Reprocessing selected rows only: {sorted(row_filter)}")

    print(f"🚀 Starting combined VIDEO + TEXT + IMAGE scraper for {len(url_rows)} rows")
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
                safe_add_log(
                    row_number=row_num,
                    status="WORKER_ERROR",
                    log_type="COMBINED",
                    message=str(e)
                )

    print("✅ Finished combined video + text + image scraping")


def parse_row_filter_arg(args):
    """
    Supports command line row filters like:
        --rows=5
        --rows=5,8,12-15
    """
    for arg in args:
        if not arg.startswith("--rows="):
            continue
        raw = arg.split("=", 1)[1].strip()
        rows = set()
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start, end = part.split("-", 1)
                start, end = int(start), int(end)
                rows.update(range(min(start, end), max(start, end) + 1))
            else:
                rows.add(int(part))
        return rows or None
    return None


if __name__ == "__main__":
    import sys

    selected_rows = parse_row_filter_arg(sys.argv[1:])

    # Default: skip completed rows.
    # --all: rerun every URL row.
    # --rows=5,8,12-15: rerun only those rows, even if they are already completed.
    only_unprocessed = "--all" not in sys.argv[1:] and selected_rows is None

    run_parallel_combined_scraper(
        max_workers=MAX_WORKERS,
        only_unprocessed=only_unprocessed,
        row_filter=selected_rows
    )
