# Combined Google Ads Transparency scraper - V2 active creative scoped fix
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


def safe_update_image_url(row_num, image_url):
    """
    Thread-safe Google Sheet row update for Image URL in column O.
    """
    with SHEET_LOCK:
        sheets.update_image_url(row_num, image_url)


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
# IMAGE URL EXTRACTION LOGIC
# =========================

def extract_primary_image_url(page):
    """
    Extracts the primary/main image URL from the ad creative.
    Looks for large, visible images that aren't icons or logos.
    Returns the full image URL or "N/A".
    """
    js = r"""
    () => {
        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return (
                rect.width >= 100 &&
                rect.height >= 100 &&
                rect.bottom > 0 &&
                rect.right > 0 &&
                rect.top < window.innerHeight &&
                rect.left < window.innerWidth &&
                style.visibility !== 'hidden' &&
                style.display !== 'none' &&
                style.opacity !== '0'
            );
        };

        const images = Array.from(document.querySelectorAll('img')).map(img => {
            const src = String(img.getAttribute('src') || img.src || '');
            const alt = String(img.getAttribute('alt') || '').toLowerCase();
            const rect = img.getBoundingClientRect();
            
            // Skip logos, icons, and Google branding
            if (src.includes('googlelogo') || alt.includes('google') || 
                alt.includes('logo') || alt.includes('icon') ||
                src.includes('logo') || src.includes('icon')) {
                return null;
            }
            
            // Only include visible images
            if (!isVisible(img)) {
                return null;
            }
            
            // Calculate score based on size and position
            const area = rect.width * rect.height;
            const score = area + (rect.y < 400 ? 5000 : 0); // Prefer top images
            
            return { src, area, score };
        }).filter(Boolean);

        // Sort by score descending and return the best one
        if (images.length > 0) {
            images.sort((a, b) => b.score - a.score);
            return images[0].src;
        }

        return null;
    }
    """

    try:
        image_url = page.evaluate(js)
        if image_url and isinstance(image_url, str) and len(image_url) > 0:
            # Handle relative URLs
            if image_url.startswith("http"):
                return image_url
            elif image_url.startswith("//"):
                return "https:" + image_url
            elif image_url.startswith("/"):
                try:
                    base_url = urlparse(page.url).netloc
                    return f"https://{base_url}{image_url}"
                except:
                    return image_url
            return image_url
    except Exception:
        pass

    # Fallback: check iframes
    for frame in page.frames:
        try:
            image_url = frame.evaluate(js)
            if image_url and isinstance(image_url, str) and len(image_url) > 0:
                if image_url.startswith("http"):
                    return image_url
                elif image_url.startswith("//"):
                    return "https:" + image_url
                return image_url
        except Exception:
            continue

    return "N/A"




def wait_and_extract_text_below_primary_image(page, image_url=None, max_wait_seconds=15):
    """
    Extracts the headline and description that are visually BELOW the matched primary image.
    It filters CTA/button text such as Install, Get, Download, Open, etc.
    Works inside the active creative iframe first, then falls back to page/frames.
    """
    js = r"""
    (imageUrl) => {
        const cleanText = (txt) =>
            (txt || "").replace(/\n/g, " ").replace(/\s+/g, " ").trim();

        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return (
                rect.width > 0 &&
                rect.height > 0 &&
                style.visibility !== "hidden" &&
                style.display !== "none" &&
                style.opacity !== "0"
            );
        };

        const isBadText = (txt, el) => {
            const t = cleanText(txt).toLowerCase();
            if (!t) return true;
            if (t.includes("{{") || t.includes("}}")) return true;

            // Do not write CTA/action text into headline/description.
            const exactBad = new Set([
                "install",
                "get",
                "download",
                "open",
                "learn more",
                "shop now",
                "play now",
                "try now",
                "visit site",
                "sign up",
                "start now",
                "see more",
                "more",
                "continue",
                "next"
            ]);

            if (exactBad.has(t)) return true;

            // Common Google/App Store/page shell text that should never be headline/description.
            if (t.includes("google play")) return true;
            if (t.includes("app store")) return true;
            if (t.includes("ads transparency")) return true;
            if (t.includes("advertiser verified")) return true;
            if (t.includes("last shown")) return true;
            if (t.includes("shown in")) return true;
            if (t === "ad" || t === "ads" || t.includes("sponsored")) return true;

            // If a short clickable element is found below image, it is usually a CTA button.
            const clickable = el && el.closest ? el.closest("a, button, [role='button'], [role='link']") : null;
            if (clickable && t.length <= 35) return true;

            return false;
        };

        const normalizeUrl = (url) => {
            if (!url) return "";
            try {
                return decodeURIComponent(String(url).trim());
            } catch (e) {
                return String(url).trim();
            }
        };

        const wantedUrl = normalizeUrl(imageUrl);

        let images = Array.from(document.querySelectorAll("img"))
            .map(img => {
                const rawSrc = img.currentSrc || img.src || img.getAttribute("src") || "";
                const src = normalizeUrl(rawSrc);
                const alt = String(img.getAttribute("alt") || "").toLowerCase();
                const rect = img.getBoundingClientRect();

                if (!src) return null;
                if (!isVisible(img)) return null;

                // Skip logos/icons/google branding.
                const lowerSrc = src.toLowerCase();
                if (
                    lowerSrc.includes("googlelogo") ||
                    lowerSrc.includes("logo") ||
                    lowerSrc.includes("icon") ||
                    alt.includes("google") ||
                    alt.includes("logo") ||
                    alt.includes("icon")
                ) {
                    return null;
                }

                if (rect.width < 80 || rect.height < 60) return null;

                let score = rect.width * rect.height;

                // Strongly prefer the exact image URL already extracted by extract_primary_image_url().
                if (
                    wantedUrl &&
                    (
                        src === wantedUrl ||
                        src.includes(wantedUrl) ||
                        wantedUrl.includes(src)
                    )
                ) {
                    score += 100000000;
                }

                // Prefer images higher in the creative.
                if (rect.top >= -50 && rect.top <= 450) score += 5000;

                return { src: rawSrc, rect, score };
            })
            .filter(Boolean);

        if (!images.length) return null;

        images.sort((a, b) => b.score - a.score);
        const best = images[0];
        const imgRect = best.rect;

        const items = [];

        for (const el of Array.from(document.querySelectorAll("body *"))) {
            if (el.childElementCount > 0) continue;
            if (!isVisible(el)) continue;

            const txt = cleanText(el.innerText || el.textContent || "");
            if (txt.length < 2 || txt.length > 220) continue;
            if (isBadText(txt, el)) continue;

            const rect = el.getBoundingClientRect();

            // Text must be visually below the selected image.
            if (rect.top < imgRect.bottom - 4) continue;

            // Stay close to the image. Increase this if some creatives have more spacing.
            if (rect.top > imgRect.bottom + 320) continue;

            // Text must horizontally overlap with the selected image area.
            const overlapsHorizontally =
                rect.right >= imgRect.left - 120 &&
                rect.left <= imgRect.right + 120;

            if (!overlapsHorizontally) continue;

            items.push({
                text: txt,
                top: rect.top,
                left: rect.left,
                height: rect.height,
                fontSize: parseFloat(window.getComputedStyle(el).fontSize || "0")
            });
        }

        if (!items.length) return null;

        // Sort visually: top-to-bottom, left-to-right.
        items.sort((a, b) => {
            if (Math.abs(a.top - b.top) > 8) return a.top - b.top;
            return a.left - b.left;
        });

        // Group nearby pieces into visual lines.
        const lines = [];
        for (const item of items) {
            let line = lines.find(l => Math.abs(l.top - item.top) <= 8);
            if (!line) {
                line = { top: item.top, parts: [] };
                lines.push(line);
            }
            line.parts.push(item);
        }

        const finalLines = [];
        const seen = new Set();

        for (const line of lines) {
            line.parts.sort((a, b) => a.left - b.left);
            const text = cleanText(line.parts.map(p => p.text).join(" "));
            const key = text.toLowerCase();

            if (!text || seen.has(key)) continue;
            if (isBadText(text, document.body)) continue;

            seen.add(key);
            finalLines.push(text);
        }

        if (!finalLines.length) return null;

        const headline = finalLines[0] || "N/A";

        let description = "N/A";
        for (let i = 1; i < finalLines.length; i++) {
            if (finalLines[i].toLowerCase() !== headline.toLowerCase()) {
                description = finalLines[i];
                break;
            }
        }

        return {
            headline,
            description,
            image_url: best.src || "N/A"
        };
    }
    """

    def read_target(target):
        try:
            data = target.evaluate(js, image_url or "")
            if data and (
                data.get("headline", "N/A") != "N/A" or
                data.get("description", "N/A") != "N/A"
            ):
                return data
        except Exception:
            return None
        return None

    start_time = time.time()

    while time.time() - start_time < max_wait_seconds:
        targets = []

        # Prefer frames that look like the active non-video creative.
        try:
            ranked_targets = get_ranked_non_video_targets(page)
            targets.extend([item[1] for item in ranked_targets])
        except Exception:
            pass

        # Fallback targets.
        targets.append(page)
        for frame in page.frames:
            if frame != page.main_frame:
                targets.append(frame)

        # Deduplicate targets by object id.
        seen_ids = set()
        unique_targets = []
        for target in targets:
            target_id = id(target)
            if target_id in seen_ids:
                continue
            seen_ids.add(target_id)
            unique_targets.append(target)

        for target in unique_targets:
            data = read_target(target)
            if data:
                return data

        page.wait_for_timeout(1000)

    return {
        "headline": "N/A",
        "description": "N/A",
        "image_url": image_url or "N/A"
    }


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
# ACTIVE CREATIVE SCOPED EXTRACTION (V2 FIX)
# =========================

def extract_creative_id_from_transparency_url(url):
    """
    Extracts CR... creative ID from Google Ads Transparency URL.
    Example: /advertiser/AR.../creative/CR...
    """
    if not url:
        return ""
    try:
        m = re.search(r"/creative/([A-Za-z0-9_-]+)", str(url))
        return m.group(1) if m else ""
    except Exception:
        return ""


def _safe_target_url(target):
    try:
        return getattr(target, "url", "") or ""
    except Exception:
        return ""


def _target_parent_box_score(target):
    """
    Scores the iframe element itself. Main page gets neutral score.
    """
    try:
        frame_el = target.frame_element()
        box = frame_el.bounding_box()
        if not box:
            return -100

        width = box.get("width", 0) or 0
        height = box.get("height", 0) or 0
        y = box.get("y", 99999) or 99999
        area = width * height

        score = 0
        if width >= 120 and height >= 70:
            score += min(area / 7000, 120)
        else:
            score -= 140

        # Active preview is usually visible near the top of the current viewport.
        if -80 <= y <= 900:
            score += 120
        elif 900 < y <= 1500:
            score += 25
        else:
            score -= 120

        return score
    except Exception:
        # Page object, not a frame.
        return 0


def _score_active_creative_target(target, creative_id=""):
    """
    Scores only this page/frame as a possible active creative container.
    Does NOT scan every ad globally for extraction.
    """
    js = r"""
    (creativeId) => {
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

        const bodyText = cleanText(document.body ? document.body.innerText : '');
        const bodyLower = bodyText.toLowerCase();
        const html = document.documentElement ? document.documentElement.outerHTML : '';

        const imageNodes = Array.from(document.querySelectorAll('img')).filter(img => {
            const src = String(img.currentSrc || img.src || img.getAttribute('src') || '').toLowerCase();
            const alt = String(img.getAttribute('alt') || '').toLowerCase();
            const rect = img.getBoundingClientRect();
            if (!isVisible(img)) return false;
            if (rect.width < 80 || rect.height < 60) return false;
            if (src.includes('googlelogo') || alt.includes('google')) return false;
            if (src.includes('logo') || src.includes('icon') || alt.includes('logo') || alt.includes('icon')) return false;
            return true;
        });

        const installNodes = Array.from(document.querySelectorAll('a[href], a[data-href], button, [role="button"], [role="link"]')).filter(el => {
            if (!isVisible(el)) return false;
            const txt = cleanText(el.innerText || el.textContent || '').toLowerCase();
            const cls = String(el.className || '').toLowerCase();
            const aria = String(el.getAttribute('aria-label') || '').toLowerCase();
            const href = String(el.href || el.getAttribute('href') || el.getAttribute('data-href') || '').toLowerCase();
            return (
                cls.includes('install-button-anchor') ||
                txt.includes('install') ||
                txt === 'get' ||
                txt.includes('download') ||
                aria.includes('install') ||
                href.includes('googleadservices.com/pagead/aclk') ||
                href.includes('play.google.com') ||
                href.includes('apps.apple.com') ||
                href.includes('itunes.apple.com')
            );
        });

        const leafTextNodes = Array.from(document.querySelectorAll('body *')).filter(el => {
            if (el.childElementCount > 0) return false;
            if (!isVisible(el)) return false;
            const txt = cleanText(el.innerText || el.textContent || '');
            if (txt.length < 3 || txt.length > 220) return false;
            if (txt.includes('{{') || txt.includes('}}')) return false;
            const lower = txt.toLowerCase();
            if (lower === 'install' || lower === 'get' || lower === 'download') return false;
            return true;
        });

        let score = 0;
        score += Math.min(imageNodes.length, 3) * 90;
        score += Math.min(installNodes.length, 3) * 80;
        score += Math.min(leafTextNodes.length, 10) * 10;

        // If the current creative ID is present in this target, strongly prefer it.
        if (creativeId && html.includes(creativeId)) score += 1000;

        // Penalize Google shell/chrome. We want the creative frame/card, not the page list.
        if (bodyLower.includes('ads transparency center') || bodyLower.includes('ads transparency centre')) score -= 220;
        if (bodyLower.includes('see more ads') || bodyLower.includes('report this ad')) score -= 120;
        if (bodyLower.includes('last shown') || bodyLower.includes('shown in')) score -= 60;

        return {
            score,
            imageCount: imageNodes.length,
            installCount: installNodes.length,
            textCount: leafTextNodes.length,
            bodyTextLength: bodyText.length,
            hasCreativeId: Boolean(creativeId && html.includes(creativeId))
        };
    }
    """
    try:
        return target.evaluate(js, creative_id or "") or {"score": 0}
    except Exception:
        return {"score": 0}


def get_active_non_video_target(page, transparency_url=""):
    """
    Finds the active creative frame/card for the current transparency URL.
    This fixes the old bug where extraction scanned the whole advertiser page
    and repeatedly picked the first/old creative.

    Returns: (target, final_score, target_kind)
    """
    creative_id = extract_creative_id_from_transparency_url(transparency_url or page.url)
    ranked = []

    for frame in page.frames:
        if frame == page.main_frame:
            continue

        details = _score_active_creative_target(frame, creative_id)
        score = float(details.get("score", 0) or 0) + float(_target_parent_box_score(frame) or 0)

        frame_url = _safe_target_url(frame)
        if creative_id and creative_id in frame_url:
            score += 1000

        if score > 0:
            ranked.append((score, frame, "iframe", details))

    # Main page fallback only. It is usually the Google shell, so penalize it.
    details = _score_active_creative_target(page, creative_id)
    main_score = float(details.get("score", 0) or 0) - 160
    if creative_id and creative_id in _safe_target_url(page):
        main_score += 120
    if main_score > 0:
        ranked.append((main_score, page, "main_page", details))

    if not ranked:
        return page, 0, "main_page_fallback"

    ranked.sort(key=lambda item: item[0], reverse=True)
    best_score, best_target, best_kind, best_details = ranked[0]

    try:
        print(
            f"🎯 Active creative target: {best_kind} | score={round(best_score, 2)} | "
            f"images={best_details.get('imageCount')} | installs={best_details.get('installCount')} | "
            f"texts={best_details.get('textCount')} | creative_id={creative_id or 'N/A'}"
        )
    except Exception:
        pass

    return best_target, best_score, best_kind


def extract_primary_image_url_from_target(target, page_url=""):
    """
    Extracts primary image ONLY from the active creative target.
    It no longer scans the whole page first, preventing same image for every ad.
    """
    js = r"""
    () => {
        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return (
                rect.width >= 70 &&
                rect.height >= 50 &&
                rect.bottom > 0 &&
                rect.right > 0 &&
                rect.top < window.innerHeight &&
                rect.left < window.innerWidth &&
                style.visibility !== 'hidden' &&
                style.display !== 'none' &&
                style.opacity !== '0'
            );
        };

        const images = Array.from(document.querySelectorAll('img')).map(img => {
            const src = String(img.currentSrc || img.src || img.getAttribute('src') || '');
            const alt = String(img.getAttribute('alt') || '').toLowerCase();
            const rect = img.getBoundingClientRect();
            const lowerSrc = src.toLowerCase();

            if (!src || !isVisible(img)) return null;
            if (lowerSrc.includes('googlelogo') || alt.includes('google')) return null;
            if (lowerSrc.includes('logo') || lowerSrc.includes('icon') || alt.includes('logo') || alt.includes('icon')) return null;

            const area = rect.width * rect.height;
            let score = area;

            // Prefer main creative image area. Do not select tiny app icons.
            if (rect.width >= 180 && rect.height >= 120) score += 20000;
            if (rect.top >= -50 && rect.top <= 500) score += 8000;

            return { src, score, area, top: rect.top };
        }).filter(Boolean);

        if (!images.length) return null;
        images.sort((a, b) => b.score - a.score);
        return images[0].src;
    }
    """
    try:
        image_url = target.evaluate(js)
        if image_url and isinstance(image_url, str):
            image_url = image_url.strip()
            if image_url.startswith("http"):
                return image_url
            if image_url.startswith("//"):
                return "https:" + image_url
            if image_url.startswith("/"):
                try:
                    base_url = urlparse(page_url or _safe_target_url(target)).netloc
                    return f"https://{base_url}{image_url}" if base_url else image_url
                except Exception:
                    return image_url
            return image_url
    except Exception:
        pass

    return "N/A"


def wait_and_extract_text_below_primary_image_scoped(page, target, image_url=None, max_wait_seconds=15):
    """
    Extracts headline/description only from text BELOW the matched image inside the active target.
    It avoids scanning other creatives on the advertiser page.
    """
    js = r"""
    (imageUrl) => {
        const cleanText = (txt) => (txt || '').replace(/\n/g, ' ').replace(/\s+/g, ' ').trim();

        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return (
                rect.width > 0 &&
                rect.height > 0 &&
                style.visibility !== 'hidden' &&
                style.display !== 'none' &&
                style.opacity !== '0'
            );
        };

        const isBadText = (txt, el) => {
            const t = cleanText(txt).toLowerCase();
            if (!t) return true;
            if (t.includes('{{') || t.includes('}}')) return true;

            const exactBad = new Set([
                'install', 'get', 'download', 'open', 'learn more', 'shop now',
                'play now', 'try now', 'visit site', 'sign up', 'start now',
                'see more', 'more', 'continue', 'next', 'ad', 'ads'
            ]);
            if (exactBad.has(t)) return true;

            if (t.includes('google play')) return true;
            if (t.includes('app store')) return true;
            if (t.includes('ads transparency')) return true;
            if (t.includes('advertiser verified')) return true;
            if (t.includes('last shown')) return true;
            if (t.includes('shown in')) return true;
            if (t.includes('sponsored')) return true;

            const clickable = el && el.closest ? el.closest('a, button, [role="button"], [role="link"]') : null;
            if (clickable && t.length <= 45) return true;

            return false;
        };

        const normalizeUrl = (url) => {
            if (!url) return '';
            try { return decodeURIComponent(String(url).trim()); }
            catch (e) { return String(url).trim(); }
        };

        const wantedUrl = normalizeUrl(imageUrl);

        const images = Array.from(document.querySelectorAll('img')).map(img => {
            const rawSrc = img.currentSrc || img.src || img.getAttribute('src') || '';
            const src = normalizeUrl(rawSrc);
            const alt = String(img.getAttribute('alt') || '').toLowerCase();
            const rect = img.getBoundingClientRect();
            const lowerSrc = src.toLowerCase();

            if (!src || !isVisible(img)) return null;
            if (rect.width < 70 || rect.height < 50) return null;
            if (lowerSrc.includes('googlelogo') || alt.includes('google')) return null;
            if (lowerSrc.includes('logo') || lowerSrc.includes('icon') || alt.includes('logo') || alt.includes('icon')) return null;

            let score = rect.width * rect.height;
            if (wantedUrl && (src === wantedUrl || src.includes(wantedUrl) || wantedUrl.includes(src))) score += 100000000;
            if (rect.width >= 180 && rect.height >= 120) score += 20000;
            if (rect.top >= -50 && rect.top <= 500) score += 8000;

            return { src: rawSrc, rect, score };
        }).filter(Boolean);

        let imgRect = null;
        let bestImage = 'N/A';

        if (images.length) {
            images.sort((a, b) => b.score - a.score);
            imgRect = images[0].rect;
            bestImage = images[0].src || 'N/A';
        }

        const items = [];
        const candidates = Array.from(document.querySelectorAll('body *'));

        for (const el of candidates) {
            if (el.childElementCount > 0) continue;
            if (!isVisible(el)) continue;

            const txt = cleanText(el.innerText || el.textContent || '');
            if (txt.length < 2 || txt.length > 240) continue;
            if (isBadText(txt, el)) continue;

            const rect = el.getBoundingClientRect();

            if (imgRect) {
                // Must be visually below the selected image.
                if (rect.top < imgRect.bottom - 4) continue;
                if (rect.top > imgRect.bottom + 380) continue;

                const overlapsHorizontally = rect.right >= imgRect.left - 140 && rect.left <= imgRect.right + 140;
                if (!overlapsHorizontally) continue;
            }

            items.push({
                text: txt,
                top: rect.top,
                left: rect.left,
                fontSize: parseFloat(window.getComputedStyle(el).fontSize || '0')
            });
        }

        if (!items.length) return null;

        items.sort((a, b) => {
            if (Math.abs(a.top - b.top) > 8) return a.top - b.top;
            return a.left - b.left;
        });

        const lines = [];
        for (const item of items) {
            let line = lines.find(l => Math.abs(l.top - item.top) <= 8);
            if (!line) {
                line = { top: item.top, parts: [] };
                lines.push(line);
            }
            line.parts.push(item);
        }

        const finalLines = [];
        const seen = new Set();

        for (const line of lines) {
            line.parts.sort((a, b) => a.left - b.left);
            const text = cleanText(line.parts.map(p => p.text).join(' '));
            const key = text.toLowerCase();
            if (!text || seen.has(key)) continue;
            if (isBadText(text, document.body)) continue;
            seen.add(key);
            finalLines.push(text);
        }

        if (!finalLines.length) return null;

        return {
            headline: finalLines[0] || 'N/A',
            description: finalLines.length > 1 ? finalLines.slice(1, 3).join(' ') : 'N/A',
            image_url: bestImage
        };
    }
    """

    start_time = time.time()
    while time.time() - start_time < max_wait_seconds:
        try:
            data = target.evaluate(js, image_url or "")
            if data and (data.get("headline", "N/A") != "N/A" or data.get("description", "N/A") != "N/A"):
                return data
        except Exception:
            pass
        page.wait_for_timeout(1000)

    return {"headline": "N/A", "description": "N/A", "image_url": image_url or "N/A"}


def extract_install_link_by_precise_js_from_target(target):
    js = r"""
    () => {
        const anchors = Array.from(document.querySelectorAll('a[href], a[data-href], button, [role="button"], [role="link"]'));
        const candidates = anchors.map(a => {
            const href = a.href || a.getAttribute('href') || a.getAttribute('data-href') || '';
            const text = (a.innerText || a.textContent || '').trim().toLowerCase();
            const cls = String(a.className || '').toLowerCase();
            const aria = String(a.getAttribute('aria-label') || '').toLowerCase();
            const rect = a.getBoundingClientRect();
            const style = window.getComputedStyle(a);

            const goodLink =
                href.includes('googleadservices.com/pagead/aclk') ||
                href.includes('play.google.com') ||
                href.includes('apps.apple.com') ||
                href.includes('itunes.apple.com');

            const looksInstall =
                cls.includes('install-button-anchor') ||
                text.includes('install') ||
                text === 'get' ||
                text.includes('download') ||
                aria.includes('install') ||
                goodLink;

            const visible =
                rect.width > 10 && rect.height > 8 &&
                rect.bottom > 0 && rect.right > 0 &&
                rect.top < window.innerHeight && rect.left < window.innerWidth &&
                style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';

            if (!looksInstall || !visible) return null;

            let score = 0;
            if (goodLink) score += 120;
            if (cls.includes('install-button-anchor')) score += 100;
            if (text.includes('install')) score += 80;
            if (text === 'get' || text.includes('download')) score += 40;
            return { href, score };
        }).filter(Boolean);

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


def wait_and_extract_install_link_scoped(page, target, max_wait_seconds=8):
    """
    Extracts install/app link only from the active creative target.
    Prevents same app/package from a different visible ad card.
    """
    start = time.time()
    while time.time() - start < max_wait_seconds:
        try:
            candidates = get_visible_install_candidates_from_target(target)
            if candidates:
                candidates.sort(key=lambda x: x["score"], reverse=True)
                best = candidates[0]
                if best["score"] > 0 and is_good_app_link(best["href"]):
                    return clean_googleadservices_link(best["href"])
        except Exception:
            pass

        app_link = extract_install_link_by_precise_js_from_target(target)
        if app_link != "N/A":
            return app_link

        page.wait_for_timeout(1000)

    return "N/A"


def extract_package_from_target(target):
    """
    Extract package names only from the active target, not the full advertiser page.
    """
    collected_texts = []

    try:
        html = target.evaluate("() => document.documentElement ? document.documentElement.outerHTML : ''")
        if html:
            collected_texts.append(html)

        hrefs = target.evaluate("""
            () => Array.from(document.querySelectorAll('a[href]')).map(a => a.href).filter(Boolean)
        """)
        if hrefs:
            collected_texts.append('\n'.join(hrefs))

        visible = target.evaluate("() => document.body ? document.body.innerText : ''")
        if visible:
            collected_texts.append(visible)
    except Exception:
        pass

    combined = '\n'.join(collected_texts)
    return extract_packages_from_text(combined)


def has_visible_image_creative_scoped(target):
    js = r"""
    () => {
        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return (
                rect.width >= 100 &&
                rect.height >= 70 &&
                rect.bottom > 0 &&
                rect.right > 0 &&
                rect.top < window.innerHeight &&
                rect.left < window.innerWidth &&
                style.visibility !== 'hidden' &&
                style.display !== 'none' &&
                style.opacity !== '0'
            );
        };

        return Array.from(document.querySelectorAll('img, picture, canvas, svg')).some(el => {
            const src = String(el.getAttribute('src') || '').toLowerCase();
            const alt = String(el.getAttribute('alt') || '').toLowerCase();
            if (src.includes('googlelogo') || alt.includes('google')) return false;
            return isVisible(el);
        });
    }
    """
    try:
        return bool(target.evaluate(js))
    except Exception:
        return False

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
                
                # Extract image URL for video ads too
                image_url = extract_primary_image_url(page)

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
                safe_update_image_url(row_num, image_url)

                safe_add_log(
                    row_number=row_num,
                    status=status,
                    log_type="VIDEO_AD",
                    url=url,
                    video_id=video_id,
                    app_link=app_link,
                    message=message
                )

                print(f"✅ Row {row_num}: saved VIDEO ad advertiser + package + video ID + text + image")
                return

            # =========================
            # NON-VIDEO PATH: TEXT + IMAGE ADS
            # =========================
            print(f"📄 Row {row_num}: no video found, checking text/image ad")

            # IMPORTANT FIX:
            # Google transparency pages can contain multiple visible creatives from the same advertiser.
            # First lock extraction to the active creative frame/card for THIS URL, then extract image/text/package only from that target.
            active_target, active_score, active_kind = get_active_non_video_target(page, transparency_url=url)

            # Extract image URL only from the active creative target.
            image_url = extract_primary_image_url_from_target(active_target, page_url=page.url)

            # Extract headline/description only from below that image inside the same active target.
            text_data = wait_and_extract_text_below_primary_image_scoped(
                page,
                target=active_target,
                image_url=image_url,
                max_wait_seconds=15
            )

            headline = clean_text(text_data.get("headline"))
            description = clean_text(text_data.get("description"))

            if text_data.get("image_url") and text_data.get("image_url") != "N/A":
                image_url = text_data.get("image_url")

            process_time = get_exact_time()
            has_text = is_valid_text_ad(headline, description)

            # Extract install/app link only from the same active creative target.
            visible_app_link = wait_and_extract_install_link_scoped(page, active_target, max_wait_seconds=8)
            visible_package = extract_package_name(visible_app_link)

            is_image_like = has_visible_image_creative_scoped(active_target)
            ad_type = "text" if has_text else "image" if (is_image_like or visible_package != "N/A" or image_url != "N/A") else "N/A"

            if not has_text and visible_package == "N/A" and not is_image_like and image_url == "N/A":
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
                    # Search packages only inside the active target first. Full-page scanning caused repeated/wrong package names.
                    all_found_packages = extract_package_from_target(active_target)
                    package_name, match_score = get_best_matching_package(headline, description, all_found_packages)

                    # Last fallback only if active target has no package candidates.
                    if not package_name and not all_found_packages:
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

            safe_update_combined_row(row_num, data)
            safe_update_headline_desc(row_num, headline, description)
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

            print(f"✅ Row {row_num}: saved NON-VIDEO {ad_type} ad advertiser + package + headline + description + image")

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
                safe_update_image_url(row_num, "N/A")
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
