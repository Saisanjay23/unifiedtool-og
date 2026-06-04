import asyncio
import os
import re
import sys
from playwright.async_api import async_playwright

# Add project root to sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.core.runtime import configure_runtime
configure_runtime()

from backend.platforms.facebook.analysis import BULK_EXTRACTION_JS, _clean_location_candidate
from backend.platforms.utils import parse_followers, parse_date_robust

FIXTURES_DIR = os.path.join(PROJECT_ROOT, "backend", "tests", "fixtures")

async def test_facebook_profile_fixture():
    fixture_path = os.path.join(FIXTURES_DIR, "facebook_profile.html")
    fixture_url = f"file:///{fixture_path.replace(os.sep, '/')}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(fixture_url)

        # 1. Run the JS bulk extraction
        bulk_data = await page.evaluate(BULK_EXTRACTION_JS)
        
        # 2. Extract name
        h1_text = bulk_data.get("h1_text", "")
        assert h1_text == "Jaydeep Adani"

        # 3. Extract location
        dom_location_link = bulk_data.get("dom_location_link", "")
        location = _clean_location_candidate(dom_location_link)
        assert location == "Ahemdabad, 380001"

        # 4. Extract followers and fallback to friends count for personal profiles
        body_text_head = bulk_data.get("body_text_head", "")
        
        followers_from_text = 0
        if body_text_head:
            f_m = re.search(r"([\d,.]+K?M?)\s+followers", body_text_head, re.IGNORECASE)
            if f_m:
                followers_from_text = parse_followers(f_m.group(1))

        friends_from_text = 0
        if body_text_head:
            fr_m = re.search(r"([\d,.]+K?M?)\s+friends", body_text_head, re.IGNORECASE)
            if fr_m:
                friends_from_text = parse_followers(fr_m.group(1))

        dom_followers = bulk_data.get("dom_followers", 0)
        dom_friends = bulk_data.get("dom_friends", 0)

        assert dom_friends == 500
        assert dom_followers == 0
        
        resolved_followers = followers_from_text or dom_followers
        resolved_friends = dom_friends or friends_from_text

        followers = resolved_followers or resolved_friends
        assert followers == 500

        # 5. Extract creation dates
        text_dates = bulk_data.get("text_dates", [])
        assert "May 2026" in text_dates

        await browser.close()


async def test_facebook_page_fixture():
    fixture_path = os.path.join(FIXTURES_DIR, "facebook_page.html")
    fixture_url = f"file:///{fixture_path.replace(os.sep, '/')}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(fixture_url)

        # 1. Run the JS bulk extraction
        bulk_data = await page.evaluate(BULK_EXTRACTION_JS)
        
        # 2. Extract name
        h1_text = bulk_data.get("h1_text", "")
        assert h1_text == "Cyfirma"

        # 3. Extract JSON-LD location
        json_ld_location = bulk_data.get("json_ld_location", "")
        assert json_ld_location == "Bangalore, India"

        # 4. Extract JSON-LD followers & likes
        json_ld_followers = bulk_data.get("json_ld_followers", 0)
        json_ld_likes = bulk_data.get("json_ld_likes", 0)
        assert json_ld_followers == 15000
        assert json_ld_likes == 12000

        # 5. Extract followers from text
        body_text_head = bulk_data.get("body_text_head", "")
        
        followers_from_text = 0
        likes_from_text = 0
        friends_from_text = 0

        if body_text_head:
            f_m = re.search(r"([\d,.]+K?M?)\s+followers", body_text_head, re.IGNORECASE)
            if f_m:
                followers_from_text = parse_followers(f_m.group(1))
            l_m = re.search(r"([\d,.]+K?M?)\s+likes", body_text_head, re.IGNORECASE)
            if l_m:
                likes_from_text = parse_followers(l_m.group(1))
            fr_m = re.search(r"([\d,.]+K?M?)\s+friends", body_text_head, re.IGNORECASE)
            if fr_m:
                friends_from_text = parse_followers(fr_m.group(1))

        assert followers_from_text == 15000
        assert likes_from_text == 12000
        assert friends_from_text == 0

        # Under the new logic:
        dom_followers = bulk_data.get("dom_followers", 0)
        dom_likes = bulk_data.get("dom_likes", 0)
        dom_friends = bulk_data.get("dom_friends", 0)

        assert dom_followers == 15000
        assert dom_likes == 12000
        assert dom_friends == 0

        is_personal_profile = (friends_from_text > 0 or dom_friends > 0)
        assert is_personal_profile is False

        resolved_followers = json_ld_followers or followers_from_text or dom_followers
        followers = resolved_followers
        assert followers == 15000

        await browser.close()

async def test_facebook_degraded_health_check():
    from backend.core.health import HealthManager, HealthDegradedError
    from backend.platforms.facebook.analysis import FacebookAnalyzer
    from backend.core.config import settings

    health = HealthManager()
    health_state = health._get_platform("facebook")
    health_state.is_suspended = True
    health_state.suspension_reason = "Mock critical suspension for test"
    
    assert health.get_health_status("facebook") == "suspended"

    analyzer = FacebookAnalyzer(config=settings, health=health)
    
    try:
        await analyzer._do_analysis(None, "https://www.facebook.com/facebook", "Test")
        assert False, "Should have raised HealthDegradedError"
    except HealthDegradedError:
        pass  # Expected
        
    health.clear_suspension("facebook")


async def test_selector_hit_consecutive_failures():
    from backend.core.health import HealthManager
    health = HealthManager()
    
    health_state = health._get_platform("facebook")
    health_state.selector_stats = {}
    health_state.selector_hits = 0
    health_state.selector_misses = 0

    for _ in range(3):
        await health.record_selector_hit("facebook", "display_name", success=False)
        
    stats = health_state.selector_stats.get("display_name", {})
    assert stats.get("misses") == 3
    assert stats.get("consecutive_misses") == 3

    await health.record_selector_hit("facebook", "display_name", success=True)
    assert stats.get("consecutive_misses") == 0
    assert stats.get("hits") == 1


if __name__ == "__main__":
    print("\n==================================================")
    print("      RUNNING FACEBOOK PARSER FIXTURE TESTS")
    print("==================================================")
    try:
        print("Running test_facebook_profile_fixture...")
        asyncio.run(test_facebook_profile_fixture())
        print("-> test_facebook_profile_fixture: PASS\n")

        print("Running test_facebook_page_fixture...")
        asyncio.run(test_facebook_page_fixture())
        print("-> test_facebook_page_fixture: PASS\n")

        print("Running test_facebook_degraded_health_check...")
        asyncio.run(test_facebook_degraded_health_check())
        print("-> test_facebook_degraded_health_check: PASS\n")

        print("Running test_selector_hit_consecutive_failures...")
        asyncio.run(test_selector_hit_consecutive_failures())
        print("-> test_selector_hit_consecutive_failures: PASS\n")

        print("SUCCESS: All parser fixture & health tests passed!")
        print("==================================================")
    except AssertionError as e:
        print(f"\nFAIL: Assertion failed: {e}")
        print("==================================================")
        sys.exit(1)
    except Exception as e:
        print(f"\nCRASH: Test execution crashed: {e}")
        print("==================================================")
        sys.exit(1)
