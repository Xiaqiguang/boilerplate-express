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
        await page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(6000)
        # close dialog
        await page.evaluate("""()=>{document.querySelectorAll('.el-dialog__headerbtn').forEach(b=>{if(b.offsetParent!==null)b.click();});}""")
        inp = page.locator("input[placeholder*='药品名称']").first
        await inp.fill("仑卡奈单抗注射液")
        await page.locator("button:has-text('查询')").first.click()
        await page.wait_for_timeout(5000)
        await page.locator("button:has-text('查看'), a:has-text('查看')").first.click()
        await page.wait_for_timeout(6000)

        # dump DOM structure of the detail view
        info = await page.evaluate(r"""()=>{
          const out={};
          // selects
          out.selects=[...document.querySelectorAll('select, .el-select')].map(s=>({cls:s.className.slice(0,60),
              text:(s.innerText||'').slice(0,200),
              options:[...s.querySelectorAll('option')].map(o=>o.textContent.trim()).slice(0,40)}));
          // tables
          out.tables=[...document.querySelectorAll('table')].map(t=>({
              headers:[...t.querySelectorAll('th')].map(h=>h.innerText.trim()),
              rows:t.querySelectorAll('tbody tr').length,
              firstRow:[...(t.querySelectorAll('tbody tr')[0]?.querySelectorAll('td')||[])].map(td=>td.innerText.trim().slice(0,60))}));
          // pagination widgets
          out.pagers=[...document.querySelectorAll('.el-pagination, .pagination-container')].map(pg=>pg.innerText.replace(/\n/g,'|').slice(0,200));
          // tabs / radio groups for 机构类别
          out.tabs=[...document.querySelectorAll('.el-tabs__item, .el-radio-button, .el-radio')].map(t=>t.innerText.trim());
          return out;
        }""")
        print(json.dumps(info, ensure_ascii=False, indent=1))
        await browser.close()

asyncio.run(main())
