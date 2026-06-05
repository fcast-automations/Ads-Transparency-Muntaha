
from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import difflib
import re
import time
import threading
import sheets

MAX_WORKERS = 2
SHEET_LOCK = threading.Lock()

# =========================
# SHEET WRITING HELPERS
# =========================

def safe_update_combined_row(row_num, data):
    with SHEET_LOCK:
        sheets.update_combined_row(row_num, data)

def safe_update_headline_desc(row_num, headline, description):
    with SHEET_LOCK:
        sheets.update_headline_and_description(row_num, headline, description)

def safe_add_log(row_number, status, log_type, url="", video_id="", app_link="", message=""):
    with SHEET_LOCK:
        sheets.add_log(
            row_number=row_number, status=status, log_type=log_type,
            url=url, video_id=video_id, app_link=app_link, message=message
        )

def get_exact_time():
    return datetime.now().strftime("%I:%M:%S %p")


# =========================
# STRING SIMILARITY MATCHER
# =========================

def clean_text_for_comparison(text):
    """Strips spaces, punctuation, and makes text lowercase for a pure letter-to-letter comparison."""
    if not text or text == "N/A": return ""
    return re.sub(r'[^a-z0-9]', '', str(text).lower())

def get_best_matching_package(headline, advertiser, package_list, strict_threshold=0.5):
    """
    A much stricter two-pass matching system to prevent false positives 
    from 'More ads' sections in the DOM.
    """
    if not package_list: 
        return None

    clean_headline = clean_text_for_comparison(headline)
    clean_adv = clean_text_for_comparison(advertiser)
    
    best_pkg = None
    highest_ratio = 0.0

    for pkg in package_list:
        # Clean package (e.g., 'com.calculator.lock' -> 'calculatorlock')
        clean_pkg = re.sub(r'^(com\.|net\.|org\.|android\.)', '', pkg.lower())
        clean_pkg = re.sub(r'[^a-z0-9]', '', clean_pkg)
        
        # Skip empty strings just in case
        if not clean_pkg: continue

        # --- PASS 1: DIRECT SUBSTRING MATCH ---
        # If 'boltvpn' is literally inside 'boltvpnfastsecure...', it's a guaranteed win.
        if clean_pkg in clean_headline or clean_pkg in clean_adv:
            return pkg

        # --- PASS 2: FOCUSED SIMILARITY MATCH ---
        # Don't compare against the whole massive description.
        # Just compare against the first chunk of the headline (the length of the package + a 5 char buffer).
        short_headline_target = clean_headline[:len(clean_pkg) + 5]
        
        ratio = difflib.SequenceMatcher(None, short_headline_target, clean_pkg).ratio()
        
        if ratio > highest_ratio:
            highest_ratio = ratio
            best_pkg = pkg

    # If Pass 1 failed, rely on Pass 2's highest score, but strictly enforce the 50% threshold
    if highest_ratio >= strict_threshold:
        return best_pkg

    # If it fails both passes, it's a false positive from the "More Ads" section
    return None
    """
    Compares the visible headline/advertiser against all found package names.
    Returns the package ONLY if it crosses the min_threshold, otherwise returns None.
    """
    if not package_list: 
        return None

    best_pkg = None
    highest_ratio = 0.0

    # Combine the visible target text
    visible_target = clean_text_for_comparison(f"{headline}{advertiser}")
    if not visible_target:
        return None

    for pkg in package_list:
        # Clean package string (e.g., 'com.calculator.lock' -> 'calculatorlock')
        clean_pkg = re.sub(r'^(com\.|net\.|org\.|android\.)', '', pkg.lower())
        clean_pkg = re.sub(r'[^a-z0-9]', '', clean_pkg)

        # Calculate character similarity ratio (0.0 to 1.0)
        ratio = difflib.SequenceMatcher(None, visible_target, clean_pkg).ratio()
        
        # Track the best match
        if ratio > highest_ratio:
            highest_ratio = ratio
            best_pkg = pkg

    # CRITICAL FIX: If the best match is garbage (below threshold), reject it completely!
    if highest_ratio < min_threshold:
        return None

    return best_pkg
    """
    Compares the visible headline/advertiser against all found package names
    and returns the one with the highest similarity score, provided it meets a minimum threshold.
    """
    if not package_list: 
        return None
    
    best_pkg = None
    highest_ratio = 0.0

    # Combine the visible text we know is on the screen
    visible_target = clean_text_for_comparison(f"{headline}{advertiser}")
    
    # If the visible target is completely empty after cleaning, we cannot reliably match
    if not visible_target:
        return None

    for pkg in package_list:
        # Clean up the package name (remove com., net., android., etc.)
        clean_pkg = re.sub(r'^(com\.|net\.|org\.|android\.)', '', pkg.lower())
        clean_pkg = re.sub(r'[^a-z0-9]', '', clean_pkg)

        # Calculate how similar the letters are (0.0 to 1.0)
        ratio = difflib.SequenceMatcher(None, visible_target, clean_pkg).ratio()
        
        # If it's the best match so far AND it meets our minimum acceptable threshold, save it
        if ratio > highest_ratio and ratio >= min_threshold:
            highest_ratio = ratio
            best_pkg = pkg

    # Will return None if no package met the min_threshold, triggering "NOT FOUND" in your main loop
    return best_pkg
    """
    Compares the visible headline/advertiser against all found package names
    and returns the one with the highest similarity score.
    """
    if not package_list: 
        return None
    
    # If we only found one package on the whole page, just use it
    if len(package_list) == 1: 
        return list(package_list)[0]

    best_pkg = None
    highest_ratio = 0.0

    # Combine the visible text we know is on the screen
    visible_target = clean_text_for_comparison(f"{headline}{advertiser}")

    for pkg in package_list:
        # Clean up the package name (remove com., net., android., etc.)
        clean_pkg = re.sub(r'^(com\.|net\.|org\.|android\.)', '', pkg.lower())
        clean_pkg = re.sub(r'[^a-z0-9]', '', clean_pkg)

        # Calculate how similar the letters are (0.0 to 1.0)
        ratio = difflib.SequenceMatcher(None, visible_target, clean_pkg).ratio()
        
        # If it's the best match so far, save it
        if ratio > highest_ratio:
            highest_ratio = ratio
            best_pkg = pkg

    return best_pkg


# =========================
# PACKAGE NAME EXTRACTOR
# =========================

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


# =========================
# ADVERTISER LOGIC 
# =========================

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


# =========================
# TEXT AD EXTRACTION
# =========================

def wait_and_extract_text_ad_details(page, max_wait_seconds=15):
    js = r"""
    () => {
        let result = { headline: "N/A", description: "N/A" };

        const isBadText = (txt) => {
            const lower = txt.toLowerCase();
            const exactBlock = [
                'install', 'download', 'get', 'open', 'visit site', 
                'learn more', 'sign in', 'google', 'search', 'ad details', 
                'ads transparency', 'about this ad', 'why this ad?'
            ];
            if (exactBlock.includes(lower)) return true;
            if (lower.length < 15 && (lower.startsWith('install') || lower.startsWith('download') || lower.startsWith('get '))) return true;
            
            // NEW: Block common UI and legal noise that often trick the scraper
            if (lower.includes('terms of service') || lower.includes('privacy policy') || lower.includes('google llc')) return true;
            
            return false;
        };

        // 1. EXTRACT HEADLINE (Visual only)
        let maxFont = 0;
        let bestEl = null;
        
        for (let el of document.querySelectorAll('*')) {
            if (el.childElementCount > 0) continue;
            let txt = (el.innerText || "").trim();
            
            // FIX: Google Ad headlines max out around 90 chars. Reject massive paragraphs.
            if (txt.length < 4 || txt.length > 120 || isBadText(txt)) continue;
            
            let rect = el.getBoundingClientRect();
            // Ignore elements rendered off-screen
            if (rect.width === 0 || rect.height === 0 || rect.y < 0) continue;

            let style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
            
            let fontSize = parseFloat(style.fontSize || '0');
            
            // FIX: If font sizes are tied, pick the one higher up on the screen
            if (fontSize > maxFont || (fontSize === maxFont && bestEl && rect.y < bestEl.getBoundingClientRect().y)) {
                maxFont = fontSize;
                bestEl = el;
            }
        }

        if (bestEl) {
            result.headline = bestEl.innerText.replace(/\n/g, ' ').trim();

            // 2. EXTRACT DESCRIPTION (Visual only)
            let maxLen = 0;
            
            for (let el of document.querySelectorAll('*')) {
                if (el.childElementCount > 0) continue;
                let txt = (el.innerText || "").replace(/\n/g, ' ').trim();
                
                // FIX: Descriptions are max ~180 chars. Cap it at 250 to reject massive legal texts.
                if (txt === result.headline || txt.length < 15 || txt.length > 250 || isBadText(txt)) continue;
                
                let rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0 || rect.y < 0) continue;

                let style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                
                if (txt.length > maxLen) {
                    maxLen = txt.length;
                    result.description = txt;
                }
            }
        }
        return result;
    }
    """

    start_time = time.time()

    while time.time() - start_time < max_wait_seconds:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                data = frame.evaluate(js)
                if data["headline"] != "N/A":
                    return data
            except Exception:
                continue
        page.wait_for_timeout(1000)

    return {"headline": "N/A", "description": "N/A"}
    js = r"""
    () => {
        let result = { headline: "N/A", description: "N/A" };
        const isBadText = (txt) => {
            const lower = txt.toLowerCase();
            const exactBlock = ['install', 'download', 'get', 'open', 'visit site', 'learn more', 'sign in', 'google', 'search', 'ad details', 'ads transparency'];
            if (exactBlock.includes(lower)) return true;
            if (lower.length < 15 && (lower.startsWith('install') || lower.startsWith('download') || lower.startsWith('get '))) return true;
            return false;
        };
        
        // 1. EXTRACT HEADLINE (Visual only)
        let maxFont = 0;
        let bestEl = null;
        for (let el of document.querySelectorAll('*')) {
            if (el.childElementCount > 0) continue;
            let txt = (el.innerText || "").trim();
            if (txt.length < 4 || isBadText(txt)) continue;
            
            let rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;

            let style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
            
            let fontSize = parseFloat(style.fontSize || '0');
            if (fontSize > maxFont) {
                maxFont = fontSize;
                bestEl = el;
            }
        }

        if (bestEl) {
            result.headline = bestEl.innerText.replace(/\n/g, ' ').trim();
            
            // 2. EXTRACT DESCRIPTION (Visual only)
            let maxLen = 0;
            for (let el of document.querySelectorAll('*')) {
                if (el.childElementCount > 0) continue;
                let txt = (el.innerText || "").replace(/\n/g, ' ').trim();
                if (txt === result.headline || txt.length < 15 || isBadText(txt)) continue;
                
                let rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) continue;

                let style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                
                if (txt.length > maxLen) {
                    maxLen = txt.length;
                    result.description = txt;
                }
            }
        }
        return result;
    }
    """

    start_time = time.time()

    while time.time() - start_time < max_wait_seconds:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                data = frame.evaluate(js)
                if data["headline"] != "N/A":
                    return data
            except Exception:
                continue
        page.wait_for_timeout(1000)

    return {"headline": "N/A", "description": "N/A"}


# =========================
# MAIN TEXT AD SCRAPER
# =========================

def scrape_single_text_ad(url_row):
    row_num, url = url_row

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-dev-shm-usage", "--disable-web-security"]
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            if "region=" not in url:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}region=anywhere"

            print(f"📄 Row {row_num}: Opening URL")

            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)

            print(f"🔎 Row {row_num}: Waiting for iframes to load ad text...")
            text_data = wait_and_extract_text_ad_details(page, max_wait_seconds=15)

            headline    = text_data["headline"]
            description = text_data["description"]

            if headline == "N/A" or len(headline) < 3:
                print(f"⏭  Row {row_num}: No valid text ad headline found visually. Skipping.")
                return  

            advertiser = extract_advertiser_from_page(page)
            print(f"🏷️  Row {row_num}: Advertiser -> {advertiser}")

            print(f"📦 Row {row_num}: Finding all package names...")
            # 1. Get ALL packages hidden on the page
            all_found_packages = extract_package_from_page(page)
            
            # 2. Pick the one that closely matches the headline we just extracted
            package_name = get_best_matching_package(headline, advertiser, all_found_packages)

            if package_name:
                app_link = f"https://play.google.com/store/apps/details?id={package_name}"
                print(f"✅ Row {row_num}: Best Matched Package -> {package_name}")
            else:
                app_link = "N/A"
                print(f"⚠️  Row {row_num}: No package matched")

            process_time = get_exact_time()

            data = [
                advertiser,     
                package_name if package_name else "NOT FOUND",  
                url,            
                app_link,       
                process_time,   
                "TEXT_AD",      
                process_time,   
            ]

            safe_update_combined_row(row_num, data)
            safe_update_headline_desc(row_num, headline, description)
            safe_add_log(
                row_number=row_num, status="SUCCESS", log_type="TEXT_AD",
                url=url, video_id="TEXT_AD",
                app_link=app_link,
                message=f"Package: {package_name or 'NOT FOUND'}"
            )
            print(f"✅ Row {row_num}: Saved — visually matched ad data.")

        except Exception as e:
            error_time = get_exact_time()
            print(f"❌ Row {row_num} error: {e}")
            try:
                data = ["", "ERROR", url, "ERROR", error_time, "ERROR", error_time]
                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, "N/A", "N/A")
                safe_add_log(row_number=row_num, status="ERROR", log_type="TEXT_AD", url=url, message=str(e))
            except Exception:
                pass
        finally:
            page.close()
            context.close()
            browser.close()


def run_parallel_text_scraper(max_workers=2):
    urls = sheets.get_urls_with_retry()
    url_rows = [(i + 2, u.strip()) for i, u in enumerate(urls) if u and u.strip()]

    if not url_rows:
        print("No transparency URLs found in sheet.")
        return

    print(f"🚀 Starting TEXT AD scraper for {len(url_rows)} rows (Max workers: {max_workers})")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(scrape_single_text_ad, url_row): url_row for url_row in url_rows}
        for future in as_completed(futures):
            row_num, _ = futures[future]
            try:
                future.result()
            except Exception as e:
                print(f"❌ Worker failed for row {row_num}: {e}")

    print("✅ Finished Text Ad scraping")


if __name__ == "__main__":
    run_parallel_text_scraper(max_workers=MAX_WORKERS)