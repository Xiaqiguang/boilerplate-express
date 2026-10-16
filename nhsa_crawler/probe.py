import asyncio, json
from playwright.async_api import async_playwright

TARGET = "https://fuwu.nhsa.gov.cn/nationalHallSt/#/search/allocation-mechanism-search"
CAPTURED = []

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path="/root/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome", args=["--no-sandbox","--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width":1440,"height":900}, locale="zh-CN")
        page = await ctx.new_page()

        async def on_response(resp):
            url = resp.url
            if any(k in url for k in ["/api","query","search","drug","org","alloc","list"]):
                try:
                    body = await resp.text()
                except Exception as e:
                    body = f"<err {e}>"
                entry = {"url":url,"status":resp.status,"method":resp.request.method,
                         "post_data":(resp.request.post_data or "")[:2000],
                         "headers":{k:v for k,v in (await resp.request.all_headers()).items() if not k.startswith(':')},
                         "body":body[:3000]}
                CAPTURED.append(entry)
                print("RESP", resp.status, resp.request.method, url[:160])

        page.on("response", on_response)
        await page.goto(TARGET, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)
        print("TITLE:", await page.title())
        html = await page.content()
        open("/workspace/nhsa_crawler/page.html","w").write(html)
        # dump visible inputs/buttons text
        texts = await page.evaluate("()=>document.body.innerText.slice(0,3000)")
        print("BODYTEXT:\n", texts)
        await browser.close()
        with open("/workspace/nhsa_crawler/capture.json","w") as f:
            json.dump(CAPTURED,f,ensure_ascii=False,indent=1)

asyncio.run(main())
