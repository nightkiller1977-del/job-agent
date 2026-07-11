import asyncio
import os
from pathlib import Path
from patchright.async_api import async_playwright

async def run_spike():
    print("Starting Patchright spike...")
    
    # Ensure state dir exists for screenshot
    state_dir = Path("state")
    state_dir.mkdir(exist_ok=True)
    screenshot_path = state_dir / "patchright_bot_check.png"
    
    async with async_playwright() as p:
        print("Launching Chromium browser...")
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        # 1. Simple HTTP Headers Diagnostic
        test_url = "https://httpbin.org/headers"
        print(f"Navigating to {test_url}...")
        try:
            await page.goto(test_url, timeout=30000)
            headers = await page.inner_text("pre")
            print("Headers response from httpbin:")
            print(headers)
        except Exception as e:
            print(f"Navigation to httpbin failed: {e}")
            
        # 2. Bot Evasion Heuristics Check (bot.sannysoft.com)
        test_bot_url = "https://bot.sannysoft.com/"
        print(f"Navigating to {test_bot_url} to test bot properties...")
        try:
            await page.goto(test_bot_url, timeout=30000)
            await asyncio.sleep(2)
            
            # Check if navigator.webdriver is bypassed (stealth)
            webdriver_val = await page.evaluate("() => navigator.webdriver")
            print(f"navigator.webdriver value: {webdriver_val} (expected: False or undefined for stealth)")
            
            await page.screenshot(path=str(screenshot_path))
            print(f"Bot check screenshot saved successfully to: {screenshot_path}")
        except Exception as e:
            print(f"Navigation to bot.sannysoft.com failed: {e}")
            
        await browser.close()
        print("Spike execution finished successfully.")

if __name__ == "__main__":
    asyncio.run(run_spike())
