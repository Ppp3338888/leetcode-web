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
                page.wait_for_load_state("networkidle", timeout=15000)
                page.wait_for_timeout(3000)

                for consent_text in ["Accept", "Accept All", "Got it", "I Agree"]:
                    consent_btn = page.get_by_role("button", name=consent_text)
                    if consent_btn.count() > 0:
                        try:
                            consent_btn.first.click(timeout=3000)
                            page.wait_for_timeout(1000)
                        except Exception:
                            pass

                last_error = None
                for attempt in range(3):
                    try:
                        if action == "register":
                            # Step 1: click the main Register button on the page
                            page.wait_for_selector('button:has-text("Register")', timeout=15000)
                            page.get_by_role("button", name="Register").first.click(timeout=15000)
                            page.wait_for_timeout(1500)

                            # Step 2: a "Register Contest" confirmation modal appears
                            # with its own "Register" button — target it inside the dialog
                            dialog = page.get_by_role("dialog")
                            if dialog.count() > 0:
                                dialog.get_by_role("button", name="Register").click(timeout=10000)
                            else:
                                # fallback: click the last "Register" button on the page
                                page.get_by_role("button", name="Register").last.click(timeout=10000)

                        else:
                            registered_btn = page.get_by_role("button", name="Registered").first
                            registered_btn.wait_for(state="visible", timeout=15000)
                            registered_btn.hover(timeout=10000)
                            page.wait_for_timeout(500)
                            page.get_by_role("button", name="Leave the Contest?").click(timeout=10000)
                            page.wait_for_timeout(1000)
                            page.get_by_role("button", name="Cancel Registration").click(timeout=10000)

                        page.wait_for_timeout(2000)
                        return True, ""
                    except Exception as e:
                        last_error = e
                        page.wait_for_timeout(3000)

                page.screenshot(path=f"debug_{action}.png")
                return False, str(last_error)
            finally:
                browser.close()

def browser_register(session_token, slug):
    return _run_action(session_token, slug, "register")

def browser_unregister(session_token, slug):
    return _run_action(session_token, slug, "unregister")