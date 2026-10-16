import asyncio, json
from playwright.async_api import async_playwright

TARGET = "https://fuwu.nhsa.gov.cn/nationalHallSt/#/search/allocation-mechanism-search"
EXE = "/root/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=EXE,
            args=["--no-sandbox","--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width":1440,"height":900}, locale="zh-CN")
        page = await ctx.new_page()
        reqs=[]
        page.on("request", lambda r: reqs.append((r.method, r.url, (r.post_data or '')[:200])) if '/ebus/' in r.url else None)
        await page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(6000)
        await page.evaluate("""()=>{document.querySelectorAll('.el-dialog__headerbtn').forEach(b=>{if(b.offsetParent!==null)b.click();});}""")
        inp = page.locator("input[placeholder*='药品名称']").first
        await inp.fill("仑卡奈单抗注射液")
        await page.locator("button:has-text('查询')").first.click()
        await page.wait_for_timeout(5000)
        await page.locator("button:has-text('查看'), a:has-text('查看')").first.click()
        await page.wait_for_timeout(6000)

        # inspect the detail form controls
        ctl = await page.evaluate(r"""()=>{
          const res={inputs:[],selects:[],cascader:[],labels:[]};
          document.querySelectorAll('input').forEach(i=>{if(i.offsetParent!==null)res.inputs.push({ph:i.placeholder,val:i.value,cls:i.className.slice(0,50),ro:i.readOnly})});
          document.querySelectorAll('.el-select').forEach(s=>{if(s.offsetParent!==null)res.selects.push({txt:s.innerText.replace(/\n/g,'|').slice(0,80)})});
          document.querySelectorAll('.el-cascader,.el-cascader-panel').forEach(s=>{res.cascader.push(s.className.slice(0,80))});
          document.querySelectorAll('form .el-form-item__label,label').forEach(l=>{if(l.offsetParent!==null)res.labels.push(l.innerText.trim())});
          return res;}""")
        print("CONTROLS:", json.dumps(ctl, ensure_ascii=False, indent=1))

        # click the 所在地 select to open dropdown and dump options
        sel = page.locator(".el-select:has(input[placeholder*='所在地'])").first
        if await sel.count()==0:
            sel = page.locator(".el-select").nth(1)
        await sel.click()
        await page.wait_for_timeout(2500)
        opts = await page.evaluate("""()=>{const v=document.querySelector('.el-select-dropdown:not([style*="display: none"]), .el-cascader-menu');
           return v? v.innerText.replace(/\\n+/g,'|').slice(0,2000):'NO DROPDOWN';}""")
        print("LOCATION OPTIONS:", opts)
        # screenshot of dropdown
        await page.screenshot(path="/workspace/nhsa_crawler/dropdown.png")
        # press escape, then check 机构类别 select
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(800)
        sels = page.locator(".el-select")
        n = await sels.count()
        for i in range(n):
            t = await sels.nth(i).inner_text()
            print(f"SEL{i}: {t!r}")
        # open the one containing 定点 or empty near label 机构类别
        cat = page.locator(".el-select:has(input[placeholder*='机构类别'])").first
        if await cat.count()>0:
            await cat.click(); await page.wait_for_timeout(2000)
            o2 = await page.evaluate("""()=>{const dd=[...document.querySelectorAll('.el-select-dropdown')].filter(d=>d.offsetParent!==null);return dd.map(d=>d.innerText.replace(/\\n+/g,'|').slice(0,500)).join('###')||'none'}""")
            print("CATEGORY OPTIONS:", o2)
        await browser.close()

asyncio.run(main())
