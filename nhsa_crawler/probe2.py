import asyncio, json
from playwright.async_api import async_playwright

TARGET = "https://fuwu.nhsa.gov.cn/nationalHallSt/#/search/allocation-mechanism-search"
EXE = "/root/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome"
CAP = []

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=EXE,
            args=["--no-sandbox","--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width":1440,"height":900}, locale="zh-CN")
        page = await ctx.new_page()

        async def on_resp(resp):
            u = resp.url
            if "/ebus/" in u or "captcha" in u.lower() or "verify" in u.lower():
                try: body = await resp.text()
                except Exception as e: body = f"<err {e}>"
                req = resp.request
                hdrs = {k:v for k,v in (await req.all_headers()).items() if not k.startswith(":")}
                CAP.append({"url":u,"status":resp.status,"method":req.method,
                            "post":req.post_data,"headers":hdrs,"body":body[:4000]})
                print(">>", resp.status, req.method, u)
                print("   POST:", (req.post_data or "")[:600])
                print("   BODY:", body[:600].replace("\n"," "))
        page.on("response", on_resp)

        # dismiss dialog if any
        await page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)
        # close el-dialog
        try:
            closed = await page.evaluate("""()=>{
                const btns=[...document.querySelectorAll('.el-dialog__wrapper .el-dialog__headerbtn, .el-dialog__close, button')];
                let n=0; for(const b of btns){ if(b.offsetParent!==null && (b.className.includes('el-dialog__headerbtn')||b.getAttribute('aria-label')==='Close')){b.click();n++;} }
                return n;}""")
            print("closed dialogs:", closed)
        except Exception as e: print("dialog err", e)
        await page.wait_for_timeout(1000)

        # find drug name input
        inputs = await page.evaluate("""()=>[...document.querySelectorAll('input')].map(i=>({ph:i.placeholder,type:i.type,cls:i.className.slice(0,80),vis:i.offsetParent!==null}))""")
        print("INPUTS:", json.dumps(inputs, ensure_ascii=False))

        inp = page.locator("input[placeholder*='药品名称']").first
        if await inp.count()==0:
            inp = page.locator("input").filter(has_not_text="").first
        await inp.click()
        await inp.fill("仑卡奈单抗注射液")
        await page.wait_for_timeout(500)
        # click 查询 button
        btn = page.get_by_role("button", name="查询").first
        if await btn.count()==0:
            btn = page.locator("button:has-text('查 询'), button:has-text('查询')").first
        await btn.click()
        print("clicked query, waiting...")
        await page.wait_for_timeout(6000)
        txt = await page.evaluate("()=>document.body.innerText")
        print("AFTER SEARCH:\n", txt[:2500])
        await page.screenshot(path="/workspace/nhsa_crawler/after_search.png")

        # click first 查看
        view = page.locator("button:has-text('查看'), a:has-text('查看')").first
        if await view.count()>0:
            await view.click()
            await page.wait_for_timeout(6000)
            txt2 = await page.evaluate("()=>document.body.innerText")
            print("AFTER VIEW:\n", txt2[:3000])
            await page.screenshot(path="/workspace/nhsa_crawler/after_view.png", full_page=True)
        else:
            print("no 查看 found")
        await browser.close()
        with open("/workspace/nhsa_crawler/capture2.json","w") as f:
            json.dump(CAP,f,ensure_ascii=False,indent=1)

asyncio.run(main())
