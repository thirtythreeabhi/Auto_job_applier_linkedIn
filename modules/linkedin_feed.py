"""Read visible LinkedIn feed posts in a dedicated, user-visible browser session."""
from pathlib import Path
import hashlib
import re
import os
import sys
import threading
import time
import logging
from logging.handlers import RotatingFileHandler
from urllib.parse import urljoin

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / '.linkedin_feed_chrome_profile'
LOG_PATH = ROOT / '.linkedin_feed.log'
HTML_SNAPSHOT_PATH = ROOT / '.linkedin_feed_debug.html'
COMMENT_SNAPSHOT_PATH = ROOT / '.linkedin_feed_comment_debug.html'
COMMENT_SCREENSHOT_PATH = ROOT / '.linkedin_feed_comment_debug.png'
_lock = threading.Lock()
_driver = None

_logger = logging.getLogger('linkedin_feed')
if not _logger.handlers:
    _handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding='utf-8')
    _handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def log_event(message, *args):
    """Write redacted feed automation diagnostics to the local rotating log."""
    _logger.info(message, *args)


def read_log_tail(lines=80):
    try:
        content = LOG_PATH.read_text(encoding='utf-8', errors='replace').splitlines()
        return '\n'.join(content[-max(1, min(int(lines), 300)):])
    except OSError:
        return ''


def _save_feed_snapshot(driver, cards, pass_number):
    """Save a small local-only feed DOM sample, with executable/private fields removed."""
    try:
        fragment = driver.execute_script("""
          const cards = arguments[0].slice(0, 20);
          const chunks = [];
          for (const source of cards) {
            const clone = source.cloneNode(true);
            clone.querySelectorAll('script,style,iframe,canvas,video,audio,form,input,textarea,select').forEach(n => n.remove());
            for (const node of [clone, ...clone.querySelectorAll('*')]) {
              for (const attr of [...node.attributes]) {
                const name = attr.name.toLowerCase();
                if (name.startsWith('on') || name === 'src' || name === 'srcset' || name === 'value')
                  node.removeAttribute(attr.name);
              }
            }
            chunks.push(clone.outerHTML);
          }
          return chunks.join('\\n<!-- feed card -->\\n');
        """, cards)
        page = ('<!doctype html><html><head><meta charset="utf-8"><title>LinkedIn feed DOM snapshot</title>'
                '<style>body{font:14px sans-serif;max-width:1000px;margin:24px auto} .snapshot-card{border:1px solid #aaa;padding:12px;margin:12px 0}</style>'
                '</head><body><h1>Sanitized LinkedIn feed snapshot</h1>'
                f'<p>Scan pass {int(pass_number)}. Scripts, form controls, media sources, and event handlers removed.</p>'
                f'{fragment}</body></html>')
        HTML_SNAPSHOT_PATH.write_text(page[:1_500_000], encoding='utf-8')
        try:
            os.chmod(HTML_SNAPSHOT_PATH, 0o600)
        except OSError:
            pass
        log_event('Saved sanitized DOM snapshot: candidate_cards=%d bytes=%d path=%s',
                  min(len(cards), 20), min(len(page), 1_500_000), HTML_SNAPSHOT_PATH.name)
    except WebDriverException as error:
        log_event('DOM snapshot failed: %s', type(error).__name__)
    except OSError as error:
        log_event('DOM snapshot could not be saved: %s', type(error).__name__)


def _chrome_environment():
    """Strip VS Code Snap GTK paths that can make system Chrome exit on Linux."""
    env = os.environ.copy()
    if not sys.platform.startswith('linux') or not any(
        '/snap/code/' in env.get(name, '')
        for name in ('GSETTINGS_SCHEMA_DIR', 'GTK_EXE_PREFIX', 'XDG_DATA_DIRS')
    ):
        return env
    for name in ('GSETTINGS_SCHEMA_DIR', 'GIO_MODULE_DIR', 'GTK_EXE_PREFIX', 'GTK_PATH',
                 'GTK_IM_MODULE_FILE', 'GTK_MODULES', 'XDG_DATA_DIRS'):
        env.pop(name, None)
    env['GSETTINGS_SCHEMA_DIR'] = '/usr/share/glib-2.0/schemas'
    env['XDG_DATA_DIRS'] = '/usr/local/share:/usr/share/ubuntu:/usr/share/gnome:/usr/share:/var/lib/snapd/desktop'
    for name in tuple(env):
        if name.endswith('_VSCODE_SNAP_ORIG'):
            env.pop(name, None)
    return env


def _fill_visible(elements, value):
    for element in elements:
        try:
            if element.is_displayed() and element.is_enabled():
                element.clear()
                element.send_keys(value)
                return True
        except WebDriverException:
            continue
    return False


def _auto_login(driver):
    """Use the same configured account credentials as the job application bot."""
    from config._overrides import load_user_config
    saved = load_user_config().get('secrets', {})
    username = saved.get('username', '') if isinstance(saved, dict) else ''
    password = saved.get('password', '') if isinstance(saved, dict) else ''
    if (not username or not password or username == 'username@example.com'
            or password == 'example_password'):
        log_event('Auto-login skipped: saved credentials are absent or placeholders')
        return False
    driver.get('https://www.linkedin.com/login')
    time.sleep(2)
    email_filled = _fill_visible(driver.find_elements(By.CSS_SELECTOR, "input[type='email'], input[name='session_key']"), username)
    password_filled = _fill_visible(driver.find_elements(By.CSS_SELECTOR, "input[type='password'], input[name='session_password']"), password)
    if not email_filled or not password_filled:
        log_event('Auto-login fields missing: email_filled=%s password_filled=%s', email_filled, password_filled)
        return False
    buttons = driver.find_elements(By.XPATH, '//button[normalize-space(.)="Sign in"]')
    for button in buttons:
        try:
            if button.is_displayed() and button.is_enabled():
                button.click()
                log_event('Auto-login form submitted; waiting for feed or verification checkpoint')
                return True
        except WebDriverException:
            continue
    log_event('Auto-login could not locate a visible enabled Sign in button')
    return False


def _expand_post(card):
    """Expand LinkedIn's truncated post body when its see-more control exists."""
    def visible_in_viewport(control):
        try:
            return bool(card.parent.execute_script("""
              const r=arguments[0].getBoundingClientRect();
              return r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight &&
                     r.right>0 && r.left<innerWidth;
            """, control))
        except WebDriverException:
            return False

    selectors = (
        '.feed-shared-inline-show-more-text__see-more-less-toggle',
        '.feed-shared-text__see-more-less-toggle',
        '[class*="see-more"]',
        'button[aria-label*="more"]',
    )
    for selector in selectors:
        try:
            for control in card.find_elements(By.CSS_SELECTOR, selector):
                if control.is_displayed() and control.is_enabled() and visible_in_viewport(control):
                    card.parent.execute_script('arguments[0].click()', control)
                    log_event('Expanded truncated post using selector=%s', selector)
                    time.sleep(0.25)
                    return
        except WebDriverException:
            continue
    # The current feed sometimes renders the truncation control as a plain
    # text span with no stable class or button role.
    try:
        controls = card.find_elements(By.XPATH,
            './/*[self::button or self::span or self::a][contains(translate(normalize-space(.), "MORE", "more"), "more")]')
        for control in controls:
            label = control.text.strip().lower()
            if (label in {'more', 'see more', '… more', '... more'} and control.is_displayed()
                    and control.is_enabled() and visible_in_viewport(control)):
                card.parent.execute_script('arguments[0].click()', control)
                log_event('Expanded post using visible text control; label_chars=%d', len(label))
                time.sleep(0.25)
                return
    except WebDriverException:
        pass
    log_event('No expandable text control found on candidate post')


def _find_post_cards(driver):
    """Return one actual feed list item per post, not LinkedIn's zero-size text wrapper."""

    def normalize(candidates):
        try:
            roots = driver.execute_script("""
              const found = arguments[0], roots = new Set();
              for (const node of found) {
                const post = node.matches('[data-view-name*="feed-full-update"]')
                  ? node : node.querySelector('[data-view-name*="feed-full-update"]');
                const root = (post || node).closest('[role="listitem"]') || post || node;
                if (root && (root.innerText || '').trim().length >= 80) roots.add(root);
              }
              return [...roots];
            """, candidates)
            return roots
        except WebDriverException as error:
            log_event('Could not normalize post containers to feed list items: %s', type(error).__name__)
            return candidates

    selectors = (
        'div.feed-shared-update-v2',
        '[data-urn*="activity"]',
        '[data-id*="urn:li:activity"]',
        '[data-view-name*="feed-full-update"]',
        'article',
    )
    for selector in selectors:
        found = driver.find_elements(By.CSS_SELECTOR, selector)
        log_event('Post container selector %s matched %d elements', selector, len(found))
        if found:
            # Avoid generic articles if a LinkedIn-specific container is available.
            if selector == 'article':
                linked = []
                for card in found:
                    try:
                        if card.find_elements(By.CSS_SELECTOR, 'a[href*="/feed/update/"], a[href*="urn:li:activity"]'):
                            linked.append(card)
                    except WebDriverException:
                        continue
                if linked:
                    normalized = normalize(linked)
                    log_event('Using %d normalized semantic article post containers', len(normalized))
                    return normalized
                continue
            normalized = normalize(found)
            log_event('Using %d normalized post containers from selector=%s (raw=%d)',
                      len(normalized), selector, len(found))
            return normalized
    # Last-resort fallback for LinkedIn's ongoing component rewrites: locate
    # activity permalinks and climb to their visible update container.
    try:
        cards = driver.execute_script("""
            const links = [...document.querySelectorAll('a[href*="/feed/update/"], a[href*="urn:li:activity"]')];
            const roots = new Set();
            for (const link of links) {
              let node = link;
              for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
                const text = (node.innerText || '').trim();
                const rect = node.getBoundingClientRect();
                if (text.length >= 100 && rect.width >= 350 && rect.height >= 120) {
                  roots.add(node);
                  break;
                }
              }
            }
            return [...roots];
        """)
        normalized = normalize(cards)
        log_event('Activity-permalink ancestor fallback matched %d normalized elements', len(normalized))
        return normalized
    except WebDriverException as error:
        log_event('Activity-permalink fallback failed: %s', type(error).__name__)
        return []


def _scroll_feed(driver):
    """Advance the feed viewport so lazy-loaded posts can enter the DOM."""
    started = time.monotonic()
    try:
        state = driver.execute_script("""
            const before = window.scrollY;
            const root = document.scrollingElement || document.documentElement;
            const step = Math.max(320, Math.floor(window.innerHeight * 0.6));
            const posts = [...document.querySelectorAll('[data-view-name*="feed-full-update"]')];
            const ancestors = new Set();
            for (const post of posts) {
              let el = post;
              while (el && el !== document.body && el !== document.documentElement) {
                const style = getComputedStyle(el);
                const overflow = `${style.overflowY} ${style.overflow}`;
                if (el.scrollHeight > el.clientHeight + 80 && el.clientHeight > 180 &&
                    /(auto|scroll|overlay)/.test(overflow)) ancestors.add(el);
                el = el.parentElement;
              }
            }
            const candidates = [...ancestors].sort((a,b) =>
              (a.scrollHeight-a.clientHeight) - (b.scrollHeight-b.clientHeight));
            let target = 'none', innerBefore = null, innerAfter = null, targetEl = null;
            if (candidates.length) {
              const el = candidates[0];
              targetEl = el;
              innerBefore = el.scrollTop;
              el.scrollTo({top:Math.min(innerBefore + step, el.scrollHeight - el.clientHeight), behavior:'smooth'});
              innerAfter = el.scrollTop;
              target = el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
                (el.getAttribute('role') ? '[role=' + el.getAttribute('role') + ']' : '');
            } else if (root.scrollHeight > window.innerHeight + 80) {
              window.scrollBy({top:step, behavior:'smooth'});
              target = 'window';
            }
            return {target, before, after: window.scrollY, pageHeight: root.scrollHeight,
                    innerBefore, innerAfter, candidateCount:candidates.length, targetEl};
        """)
        if state.get('target') != 'none':
            time.sleep(0.8)
            try:
                final_state = driver.execute_script("""
                  return {windowY:window.scrollY, innerTop:arguments[0]
                    ? arguments[0].scrollTop : null};
                """, state.get('targetEl'))
                if final_state.get('windowY') is not None:
                    state['after'] = final_state['windowY']
                if final_state.get('innerTop') is not None:
                    state['innerAfter'] = final_state['innerTop']
            except WebDriverException:
                pass
        moved = (state.get('before') != state.get('after') or
                 state.get('innerBefore') != state.get('innerAfter'))
        log_event('Feed scroll completed in %.3fs: target=%s candidates=%s moved=%s window_y=%s->%s inner_top=%s->%s page_height=%s',
                  time.monotonic() - started,
                  state.get('target'), state.get('candidateCount'), moved,
                  state.get('before'), state.get('after'), state.get('innerBefore'),
                  state.get('innerAfter'), state.get('pageHeight'))
        return moved
    except WebDriverException as error:
        log_event('Feed scroll failed: %s', type(error).__name__)
        return False


def _post_permalink(driver, card):
    """Resolve a stable activity URL from visible links or LinkedIn data attributes."""
    try:
        found = driver.execute_script("""
          const root = arguments[0];
          const links = [...root.querySelectorAll('a[href]')];
          for (const a of links) {
            const href = a.href || '';
            if (href.includes('/feed/update/') && /urn:li:(activity|ugcPost|share):\\d+/.test(href)) return href;
          }
          const nodes = [root, ...root.querySelectorAll('*')];
          const rx = /urn:li:(activity|ugcPost|share):\\d+/;
          for (const node of nodes) {
            for (const attr of [...node.attributes || []]) {
              const match = (attr.value || '').match(rx);
              if (match) return 'https://www.linkedin.com/feed/update/' + match[0] + '/';
            }
          }
          return '';
        """, card)
        return str(found or '')
    except WebDriverException as error:
        log_event('Permalink extraction failed: %s', type(error).__name__)
        return ''


def post_comment(post_url, comment, card_ref=None, lock_held=False):
    """Click Comment, fill that post's composer, submit, and verify it clears."""
    global _driver
    acquired = False
    if not lock_held:
        acquired = _lock.acquire(blocking=False)
    if not lock_held and not acquired:
        raise RuntimeError('A feed scan is already running.')
    try:
        if _driver is None:
            raise RuntimeError('Open the feed scanner before posting a comment.')
        driver = _driver
        if card_ref is not None:
            try:
                driver.execute_script('arguments[0].scrollIntoView({block:"center"})', card_ref)
                log_event('Using current feed card for comment submission')
            except WebDriverException:
                card_ref = None
        if card_ref is None and post_url.startswith('local-linkedin-post:'):
            log_event('Cannot reopen a local-only feed reference without its live post card')
            return {'posted': False, 'error': 'The live post card is no longer available.'}
        if card_ref is None:
            log_event('Opening post detail page for comment submission')
            driver.get(post_url)
            time.sleep(2)

        def find_post_card():
            if card_ref is not None:
                try:
                    if driver.execute_script('return arguments[0].isConnected', card_ref) and card_ref.is_displayed():
                        return card_ref
                except WebDriverException:
                    pass
            for selector in ('[data-view-name*="feed-full-update"]', 'div.feed-shared-update-v2', 'article'):
                for candidate in driver.find_elements(By.CSS_SELECTOR, selector):
                    try:
                        if candidate.is_displayed() and candidate.find_elements(By.CSS_SELECTOR,
                                'button[aria-label*="Comment"], button.comments-comment-box__submit-button'):
                            return candidate
                    except WebDriverException:
                        continue
            return None

        card = find_post_card()
        if card is None:
            log_event('Comment action unavailable: no visible post card with a Comment control')
            _save_comment_snapshot(driver)
            return {'posted': False, 'error': 'Could not locate the post card or its Comment action.'}
        actions = card.find_elements(By.CSS_SELECTOR, 'button[aria-label="Comment"], button[aria-label^="Comment"]')
        action = next((item for item in actions if item.is_displayed() and item.is_enabled()), None)
        if action is None:
            log_event('Comment action unavailable: %d candidate controls were found', len(actions))
            _save_comment_snapshot(driver)
            return {'posted': False, 'error': 'Could not find an enabled Comment action on the post.'}
        log_event('Comment action target: aria=%s title=%s class=%s text_chars=%d',
            (action.get_attribute('aria-label') or '')[:100],
            (action.get_attribute('title') or '')[:100],
            str(action.get_attribute('class') or '')[:120], len(action.text or ''))
        action.click()
        log_event('Clicked the post Comment action; waiting for its composer')
        time.sleep(0.5)

        def belongs_to_clicked_post(element):
            """Match a portaled editor/control to the post whose action we clicked."""
            try:
                return bool(driver.execute_script("""
                  const action = arguments[0], candidate = arguments[1];
                  const rootFor = node => node.closest('[role="listitem"], [data-view-name*="feed-full-update"], div.feed-shared-update-v2');
                  const actionRoot = rootFor(action), candidateRoot = rootFor(candidate);
                  return actionRoot && candidateRoot ? actionRoot === candidateRoot : false;
                """, action, element))
            except WebDriverException:
                return False

        card = find_post_card() or card
        editor_selectors = (
            '.comments-comment-box__editor[contenteditable="true"]',
            '[data-placeholder*="comment" i][contenteditable="true"]',
            '[aria-label*="comment" i][contenteditable="true"]',
            'div[role="textbox"][contenteditable="true"]',
            # LinkedIn's current feed UI renders the composer as a page-level
            # ProseMirror textbox, outside the feed-full-update card.
            'div[role="textbox"][aria-label="Text editor for creating comment"][contenteditable="true"]',
        )
        editors = []
        editor = None
        for attempt in range(8):
            editors = []
            for selector in editor_selectors:
                try:
                    editors.extend(card.find_elements(By.CSS_SELECTOR, selector))
                except WebDriverException:
                    continue
            editor = next((item for item in editors if item.is_displayed() and item.is_enabled()), None)
            if editor is None:
                # The new LinkedIn layout portals its composer outside the post
                # card. Only use it when there is exactly one visible comment
                # editor, which was exposed by the Comment action just clicked.
                try:
                    global_editors = driver.find_elements(By.CSS_SELECTOR,
                        'div[role="textbox"][aria-label="Text editor for creating comment"][contenteditable="true"]')
                    visible_global_editors = [item for item in global_editors
                                              if item.is_displayed() and item.is_enabled()
                                              and belongs_to_clicked_post(item)]
                    if len(visible_global_editors) == 1:
                        editor = visible_global_editors[0]
                        log_event('Using unique visible page-level LinkedIn comment editor')
                    elif len(visible_global_editors) > 1:
                        focused = [item for item in visible_global_editors
                                   if 'ProseMirror-focused' in (item.get_attribute('class') or '')]
                        if len(focused) == 1:
                            editor = focused[0]
                            log_event('Using focused page-level LinkedIn comment editor (visible=%d)',
                                      len(visible_global_editors))
                except WebDriverException:
                    pass
            if editor is not None:
                log_event('Post editor appeared after %.1f seconds (selector attempt %d)',
                          0.5 + attempt * 0.5, attempt + 1)
                break
            time.sleep(0.5)
        if editor is None:
            log_event('Comment submission stopped: click did not expose a post editor (editors_found=%d)', len(editors))
            try:
                global_editors = driver.find_elements(By.CSS_SELECTOR,
                    '[contenteditable="true"], textarea, [role="textbox"]')
                log_event('Page-level editor diagnostics: total=%d visible=%d', len(global_editors),
                          sum(1 for item in global_editors if item.is_displayed()))
                for item in global_editors[:12]:
                    log_event('Editor candidate: tag=%s role=%s aria=%s placeholder=%s class=%s visible=%s',
                        item.tag_name, item.get_attribute('role') or '',
                        (item.get_attribute('aria-label') or '')[:80],
                        (item.get_attribute('data-placeholder') or item.get_attribute('placeholder') or '')[:80],
                        str(item.get_attribute('class') or '')[:100], item.is_displayed())
            except WebDriverException:
                pass
            _save_comment_snapshot(driver)
            return {'posted': False, 'error': 'Could not find the comment editor.'}
        editor.click()
        editor.send_keys(comment)
        actual_text = (editor.get_attribute('innerText') or editor.text or '').strip()
        normalize = lambda value: re.sub(r'\s+', ' ', str(value)).strip()
        if normalize(comment) not in normalize(actual_text):
            log_event('Comment input not verified: expected_chars=%d actual_chars=%d',
                      len(comment), len(actual_text))
            _save_comment_snapshot(driver)
            return {'posted': False, 'error': 'Comment text was not entered into the post composer.'}
        log_event('Comment entered into scoped editor; chars=%d', len(comment))
        time.sleep(0.4)
        buttons = []
        for selector in ('button[aria-label="Post"]', 'button[aria-label*="Post comment" i]',
                         'button.comments-comment-box__submit-button', 'button[data-control-name*="comment_submit"]'):
            try:
                buttons.extend(card.find_elements(By.CSS_SELECTOR, selector))
            except WebDriverException:
                continue
        try:
            buttons.extend(card.find_elements(By.XPATH, './/button[normalize-space(.)="Post"]'))
        except WebDriverException:
            pass
        if not any(item.is_displayed() and item.is_enabled() for item in buttons):
            # The newer LinkedIn composer and its Post button are both portaled
            # outside the feed card. Search globally only after verified text
            # entry into the unique page-level comment editor.
            for selector in ('button[aria-label="Post"]', 'button[aria-label*="Post comment" i]',
                             'button.comments-comment-box__submit-button'):
                try:
                    buttons.extend(driver.find_elements(By.CSS_SELECTOR, selector))
                except WebDriverException:
                    continue
            try:
                buttons.extend(driver.find_elements(By.XPATH, '//button[normalize-space(.)="Post"]'))
            except WebDriverException:
                pass
            buttons = [item for item in buttons if belongs_to_clicked_post(item)]
        button = next((item for item in buttons if item.is_displayed() and item.is_enabled()), None)
        if button is None:
            log_event('Comment submission stopped: no enabled post control in scoped card (buttons_found=%d)', len(buttons))
            _save_comment_snapshot(driver)
            return {'posted': False, 'error': 'LinkedIn did not enable the Post button.'}
        log_event('Clicking enabled Post control')
        button.click()
        time.sleep(2)
        remaining = editor.text.strip()
        if remaining:
            log_event('Post click not confirmed: comment editor retained text')
            _save_comment_snapshot(driver)
            return {'posted': False, 'error': 'LinkedIn did not confirm the comment; the editor still contains text.'}
        log_event('Comment submission confirmed by cleared editor')
        return {'posted': True}
    finally:
        if acquired:
            _lock.release()


def _save_comment_snapshot(driver, card=None):
    """Save sanitized comment-card markup after a composer/submit failure."""
    try:
        html = driver.execute_script("""
          const source = arguments[0] || document.querySelector('main') || document.body;
          const clone = source.cloneNode(true);
          clone.querySelectorAll('script,style,iframe,canvas,video,audio,form,input,textarea,select').forEach(n => n.remove());
          for (const node of [clone, ...clone.querySelectorAll('*')]) {
            for (const attr of [...node.attributes]) {
              const name = attr.name.toLowerCase();
              if (name.startsWith('on') || name === 'src' || name === 'srcset' || name === 'value')
                node.removeAttribute(attr.name);
            }
          }
          return clone.outerHTML;
        """, card)
        document = ('<!doctype html><meta charset="utf-8"><title>Sanitized LinkedIn comment diagnostics</title>' + html)
        COMMENT_SNAPSHOT_PATH.write_text(document[:1_000_000], encoding='utf-8')
        try:
            os.chmod(COMMENT_SNAPSHOT_PATH, 0o600)
        except OSError:
            pass
        log_event('Saved sanitized comment DOM snapshot: bytes=%d path=%s',
                  min(len(document), 1_000_000), COMMENT_SNAPSHOT_PATH.name)
        if driver.save_screenshot(str(COMMENT_SCREENSHOT_PATH)):
            try:
                os.chmod(COMMENT_SCREENSHOT_PATH, 0o600)
            except OSError:
                pass
            log_event('Saved private comment failure screenshot: path=%s', COMMENT_SCREENSHOT_PATH.name)
    except (WebDriverException, OSError) as error:
        log_event('Comment DOM snapshot failed: %s', type(error).__name__)


def scan_feed(limit=15, scrolls=5, on_post=None):
    """Open the feed, scroll a bounded number of times, and return post text."""
    limit = max(1, min(int(limit), 30))
    scrolls = max(0, min(int(scrolls), 10))
    if not _lock.acquire(blocking=False):
        log_event('Scan rejected: another feed operation holds the browser lock')
        raise RuntimeError('A feed scan is already running.')
    global _driver
    try:
        options = Options()
        if _driver is None:
            log_event('Starting dedicated Chrome session with persistent feed profile')
            options.add_argument(f'--user-data-dir={PROFILE}')
            options.add_argument('--start-maximized')
            _driver = webdriver.Chrome(options=options, service=Service(env=_chrome_environment()))
        driver = _driver
        log_event('Navigating to feed; limit=%d scrolls=%d', limit, scrolls)
        driver.get('https://www.linkedin.com/feed/')
        if 'login' in driver.current_url.lower():
            log_event('Login page detected; attempting saved-account sign-in')
            _auto_login(driver)
        # Keep this scan request alive while the user signs in or completes a
        # checkpoint in the visible browser. LinkedIn redirects back to /feed
        # after successful authentication; then scanning continues in place.
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            current = driver.current_url.lower()
            if 'linkedin.com/feed' in current and 'login' not in current and 'checkpoint' not in current:
                log_event('Feed is available at %s', current.split('?')[0])
                break
            time.sleep(1.5)
        else:
            log_event('Authentication wait expired; final URL=%s', driver.current_url.split('?')[0])
            return {'needs_login': True, 'posts': []}
        time.sleep(3)

        posts = {}
        reviewed_urls = set()
        halted = False
        scan_started = time.monotonic()
        for iteration in range(scrolls + 1):
            cards = _find_post_cards(driver)
            log_event('Feed pass %d: located %d candidate post cards; unique_posts=%d/%d',
                      iteration + 1, len(cards), len(posts), limit)
            _save_feed_snapshot(driver, cards, iteration + 1)
            for card_index, card in enumerate(cards, start=1):
                card_started = time.monotonic()
                try:
                    try:
                        position = driver.execute_script("""const r=arguments[0].getBoundingClientRect(); return {
                          top:Math.round(r.top), bottom:Math.round(r.bottom), width:Math.round(r.width),
                          height:Math.round(r.height), windowY:Math.round(window.scrollY),
                          visible:(() => {
                            if (r.width < 100 || r.height < 80 || r.bottom <= 0 || r.top >= innerHeight) return false;
                            for (let p=arguments[0].parentElement; p; p=p.parentElement) {
                              const style=getComputedStyle(p), pr=p.getBoundingClientRect();
                              if (/(auto|scroll|hidden|clip)/.test(style.overflowY) && p.clientHeight < p.scrollHeight) {
                                if (r.bottom <= pr.top || r.top >= pr.bottom) return false;
                              }
                            }
                            return true;
                          })()
                        }""", card)
                    except WebDriverException:
                        position = {}
                    log_event('Inspecting post container %d/%d: viewport_top=%s bottom=%s size=%sx%s visible=%s window_y=%s',
                              card_index, len(cards), position.get('top'), position.get('bottom'),
                              position.get('width'), position.get('height'),
                              position.get('visible'), position.get('windowY'))
                    if not position.get('visible'):
                        log_event('Deferring offscreen post container %d/%d without clicking its controls',
                                  card_index, len(cards))
                        continue
                    _expand_post(card)
                    text = card.text.strip()
                    if len(text) < 20:
                        log_event('Skipping card %d/%d: text too short (%d chars)',
                                  card_index, len(cards), len(text))
                        continue
                    author = ''
                    try:
                        author = card.find_element(By.CSS_SELECTOR,
                            '.update-components-actor__name, .feed-shared-actor__name').text.strip()
                    except Exception:
                        try:
                            author = next((a.text.strip() for a in card.find_elements(By.CSS_SELECTOR,
                                'a[href*="/in/"]') if a.text.strip()), '')
                        except Exception:
                            pass
                    links = card.find_elements(By.CSS_SELECTOR, 'a[href*="/feed/update/"], a[href*="activity"]')
                    try:
                        attrs = driver.execute_script("""return {
                          tag: arguments[0].tagName,
                          className: String(arguments[0].className || '').slice(0, 180),
                          viewName: arguments[0].getAttribute('data-view-name') || '',
                          dataId: arguments[0].getAttribute('data-id') || '',
                          dataUrn: arguments[0].getAttribute('data-urn') || ''
                        }""", card)
                        log_event('Candidate metadata: tag=%s class=%s view=%s id_present=%s urn_present=%s text_chars=%d link_count=%d',
                            attrs.get('tag', ''), attrs.get('className', ''), attrs.get('viewName', ''),
                            bool(attrs.get('dataId')), bool(attrs.get('dataUrn')), len(text), len(links))
                    except WebDriverException:
                        pass
                    url = urljoin('https://www.linkedin.com', links[0].get_attribute('href')) if links else _post_permalink(driver, card)
                    if not url:
                        urn = card.get_attribute('data-urn') or ''
                        match = re.search(r'urn:li:activity:\d+', urn)
                        if match:
                            url = 'https://www.linkedin.com/feed/update/' + match.group(0) + '/'
                    if not url:
                        # This LinkedIn layout omits permalinks from many feed
                        # cards. Use a stable local identity while the live DOM
                        # card is available; post_comment uses card_ref and
                        # never navigates to this local key.
                        identity = hashlib.sha256(
                            (author.strip().casefold() + '\n' + re.sub(r'\s+', ' ', text).strip()).encode('utf-8')
                        ).hexdigest()[:32]
                        url = 'local-linkedin-post:' + identity
                        log_event('Post permalink unavailable; generated local identity for author=%r', author[:100])
                    if url in reviewed_urls:
                        log_event('Skipping duplicate post container for an already reviewed URL')
                        continue
                    post = {'post_url': url, 'author': author[:200], 'post_text': text[:8000]}
                    posts[url] = post
                    reviewed_urls.add(url)
                    preview = re.sub(r'\s+', ' ', text)[:140]
                    log_event('Captured post %d: author=%r text_chars=%d excerpt=%r',
                              len(posts), author[:100], min(len(text), 8000), preview)
                    if on_post is not None:
                        log_event('Reviewing this post before scrolling to another')
                        review_started = time.monotonic()
                        try:
                            outcome = on_post({**post, '_card_ref': card})
                            post['outcome'] = outcome
                            log_event('Per-post processing finished in %.3fs: status=%s card_elapsed=%.3fs',
                                      time.monotonic() - review_started,
                                      (outcome or {}).get('status', 'unknown'),
                                      time.monotonic() - card_started)
                            if isinstance(outcome, dict) and outcome.pop('_halt_scan', False):
                                halted = True
                                log_event('Stopping feed scan because the current relevant post was not completed')
                                break
                            time.sleep(0.8)
                        except Exception as error:
                            log_event('Per-post processing failed: %s', type(error).__name__)
                            post['outcome'] = {'status': 'review error', 'error': str(error)[:300]}
                            halted = True
                            break
                    if len(posts) >= limit:
                        break
                except WebDriverException:
                    continue
            if halted or len(posts) >= limit or iteration == scrolls:
                break
            moved = _scroll_feed(driver)
            time.sleep(0.8)
            if not moved:
                log_event('Stopping scan because the detected feed container did not move')
                break
        log_event('Feed scan complete in %.3fs: %d unique posts captured; halted=%s',
                  time.monotonic() - scan_started, len(posts), halted)
        return {'needs_login': False, 'halted': halted, 'posts': [
            {key: value for key, value in post.items() if key != '_card_ref'}
            for post in list(posts.values())[:limit]]}
    finally:
        _lock.release()


def close_feed_browser():
    global _driver
    with _lock:
        if _driver is not None:
            _driver.quit()
            _driver = None
