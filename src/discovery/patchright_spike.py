import asyncio
import os
import urllib.parse
from pathlib import Path
from typing import Dict, Any, List

# Import both side-by-side
from playwright.async_api import async_playwright as playwright_async
from patchright.async_api import async_playwright as patchright_async

TEST_DOMAINS = [
    "https://jobs.northropgrumman.com",
    "https://jobs.jacksonhealth.org",
    "https://servpro.hrmdirect.com",
    "https://www.theapplicantmanager.com",
    "https://boards.greenhouse.io",
    "https://jobs.lever.co"
]

async def test_domain_with_engine(playwright_engine, engine_name: str, url: str) -> Dict[str, Any]:
    """Navigates to a URL with the given browser engine and returns detection and loading stats."""
    result = {
        "success": False,
        "blocked": False,
        "webdriver_val": None,
        "title": "",
        "error": None
    }
    
    try:
        # Launch headless Chromium
        browser = await playwright_engine.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        # Settle timeout
        await page.goto(url, timeout=25000, wait_until="domcontentloaded")
        await asyncio.sleep(2)  # Let dynamic JS run
        
        title = await page.title()
        
        # Retrieve only visible body text to avoid matching CDN urls/scripts
        body_text = await page.locator("body").inner_text()
        webdriver_val = await page.evaluate("() => navigator.webdriver")
        
        result["title"] = title
        result["webdriver_val"] = webdriver_val
        
        # Target only visible block text in the page body or title
        blocked_keywords = [
            "attention required", 
            "access denied", 
            "security check", 
            "please confirm you are human", 
            "checking your browser",
            "verify you are human",
            "verify your connection"
        ]
        
        body_lower = body_text.lower()
        title_lower = title.lower()
        
        is_blocked = any(kw in body_lower or kw in title_lower for kw in blocked_keywords)
        
        result["blocked"] = is_blocked
        result["success"] = not is_blocked
        
        await browser.close()
    except Exception as e:
        result["error"] = str(e)
        
    return result

async def run_benchmark():
    print("================================================================")
    print("         PLAYWRIGHT VS PATCHRIGHT STEALTH BENCHMARK             ")
    print("================================================================")
    
    results = {}
    
    for url in TEST_DOMAINS:
        domain = urllib.parse.urlparse(url).netloc
        print(f"\nEvaluating domain: {domain}...")
        
        # 1. Test standard Playwright
        print(f" -> Testing with Standard Playwright...")
        async with playwright_async() as p_std:
            std_res = await test_domain_with_engine(p_std, "Playwright", url)
            
        # 2. Test Patchright
        print(f" -> Testing with Patchright...")
        async with patchright_async() as p_patch:
            patch_res = await test_domain_with_engine(p_patch, "Patchright", url)
            
        results[domain] = {
            "playwright": std_res,
            "patchright": patch_res
        }
        
    # Render final report table
    print("\n\n================================================================")
    print("                      BENCHMARK REPORT                          ")
    print("================================================================")
    print(f"{'Domain':<30} | {'Playwright (Std)':<18} | {'Patchright (Stealth)':<20}")
    print("-" * 75)
    
    playwright_wins = 0
    patchright_wins = 0
    ties = 0
    
    for domain, res in results.items():
        std = res["playwright"]
        pat = res["patchright"]
        
        # Format Playwright status
        if std["error"]:
            std_str = "ERR (Timeout/Network)"
        elif std["blocked"]:
            std_str = "BLOCKED (Bot Check)"
        else:
            std_str = f"OK (Title: {std['title'][:15]})"
            
        # Format Patchright status
        if pat["error"]:
            pat_str = "ERR (Timeout/Network)"
        elif pat["blocked"]:
            pat_str = "BLOCKED (Bot Check)"
        else:
            pat_str = f"OK (Title: {pat['title'][:15]})"
            
        print(f"{domain:<30} | {std_str:<18} | {pat_str:<20}")
        
        # Compare success
        std_ok = std["success"] and not std["error"]
        pat_ok = pat["success"] and not pat["error"]
        
        if pat_ok and not std_ok:
            patchright_wins += 1
        elif std_ok and not pat_ok:
            playwright_wins += 1
        else:
            ties += 1
            
    print("-" * 75)
    print(f"Patchright wins: {patchright_wins} | Playwright wins: {playwright_wins} | Ties/Parity: {ties}")
    print("================================================================")

if __name__ == "__main__":
    asyncio.run(run_benchmark())
