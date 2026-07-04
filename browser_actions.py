"""
Browser automation for LeetCode actions Cloudflare blocks via raw HTTP:
register / unregister from contests.
"""

import threading
from patchright.sync_api import sync_playwright

_lock = threading.Lock()

def _run_action(session_token, slug, action):
    with _lock:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                )
            )
            context.add_cookies([{
                "name": "LEETCODE_SESSION",
                "value": session_token,
                "domain": ".leetcode.com",
                "path": "/",
            }])
            page = context.new_page()
            try:
                page.goto(
                    f"https://leetcode.com/contest/{slug}/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                page.wait_for_timeout(3000)

                if action == "register":
                    page.get_by_role("button", name="Register").first.click(timeout=10000)
                    page.wait_for_timeout(2000)
                else:
                    # The button shows "Registered" until hovered, which reveals "Leave the Contest?"
                    registered_btn = page.get_by_role("button", name="Registered").first
                    registered_btn.hover(timeout=10000)
                    page.wait_for_timeout(500)
                    page.get_by_role("button", name="Leave the Contest?").click(timeout=10000)
                    page.wait_for_timeout(1000)
                    page.get_by_role("button", name="Cancel Registration").click(timeout=10000)
                    page.wait_for_timeout(2000)

                return True, ""
            except Exception as e:
                page.screenshot(path=f"debug_{action}.png")
                return False, str(e)
            finally:
                browser.close()

def browser_register(session_token, slug):
    return _run_action(session_token, slug, "register")

def browser_unregister(session_token, slug):
    return _run_action(session_token, slug, "unregister")