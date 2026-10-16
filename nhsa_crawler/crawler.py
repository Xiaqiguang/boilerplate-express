#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国家医保服务平台 - 国谈药及商保创新药配备机构查询 爬虫
====================================================
目标页面: https://fuwu.nhsa.gov.cn/nationalHallSt/#/search/allocation-mechanism-search

反爬/加密情况(动态抓包分析结论):
  1. 前端为 Vue + ElementUI SPA, 业务接口位于:
       https://fuwu.nhsa.gov.cn/ebus/fuwu/api/nthl/api/drugOptins/queryDrugOptinsInfoByName   (药品搜索)
       https://fuwu.nhsa.gov.cn/ebus/fuwu/api/nthl/api/drugOptins/queryDrugOptinsDic          (所在地/机构类别字典)
       https://fuwu.nhsa.gov.cn/ebus/fuwu/api/nthl/api/drugOptins/queryDrugOptinsInfoDetail    (配备机构明细分页)
     请求体: {"data":{"data":{"encData":"<SM4密文hex>"},"appCode":"T98H...","version":"1.0.0",
              "encType":"SM4","signType":"SM2","timestamp":秒级时间戳,"signData":"<SM2签名>"}}
     响应体: data.encData 同样是 SM4 密文。=> 直接裸调接口必须还原国密 SM2/SM4 密钥与签名逻辑,
     且 JS 经过混淆(a3_0x...), 静态逆向成本高、易随发版失效。
  2. 网关为阿里云 WAF(acw_tc Cookie), 高频访问会触发滑块验证码。
  => 本脚本采用 Playwright 驱动真实 Chromium 模拟浏览器:
     - 加解密完全交给页面自身 JS 完成 —— 站点轮换密钥/调整字段无需改代码("加密逻辑动态定位");
     - 通过 page.on("response") 拦截 queryDrugOptinsInfoDetail 响应, Playwright 拿到的是
       框架解密渲染前的最终 JSON(若仍是密文则回退解析 DOM 表格);
     - 拟人化操作(逐字输入/随机延时), 检测验证码; 无头模式自动重建会话重试,
       --headed 模式暂停等待人工过码后继续。

用法示例:
  python crawler.py                                   # 默认: 仑卡奈单抗注射液, 遍历全部省份 x 两类机构
  python crawler.py --locations ALL                   # 不限地区, 一次抓全量(最快)
  python crawler.py --drug 替尔泊肽注射液 --out out   # 指定其它药品
  python crawler.py --locations 北京市,上海市         # 只抓指定省市
  python crawler.py --headed                          # 有头模式(出现验证码时人工滑动)
输出: <out>/<药品名>.md (Markdown 表格) 和同名 .json (原始数据)
"""
import argparse
import asyncio
import json
import os
import random
import re
import sys
from datetime import datetime

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

BASE_URL = "https://fuwu.nhsa.gov.cn/nationalHallSt/#/search/allocation-mechanism-search"
DETAIL_API_KEY = "queryDrugOptinsInfoDetail"   # 明细接口 URL 关键字(动态匹配, 站点改名时在此调整)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def find_chromium():
    """自动定位本机 Chromium/Chrome 可执行文件(Playwright 缓存或系统安装)。"""
    cands = []
    cache = os.path.expanduser("~/.cache/ms-playwright")
    if os.path.isdir(cache):
        for d in sorted(os.listdir(cache), reverse=True):
            for sub in ("chrome-linux64/chrome", "chrome-linux/chrome",
                        "chrome-headless-shell-linux64/chrome-headless-shell"):
                p = os.path.join(cache, d, sub)
                if os.path.exists(p):
                    cands.append(p)
    for p in ("/opt/google/chrome/chrome", "/usr/bin/google-chrome",
              "/usr/bin/chromium", "/usr/bin/chromium-browser"):
        if os.path.exists(p):
            cands.append(p)
    return cands[0] if cands else None


class CaptchaError(RuntimeError):
    pass


# ---------------------------------------------------------------- 工具函数
def is_hex_cipher(s):
    return isinstance(s, str) and len(s) >= 32 and re.fullmatch(r"[0-9A-Fa-f]+", s) is not None


def pick(rec, keys):
    for k in keys:
        v = rec.get(k)
        if v not in (None, "", "-"):
            return str(v).strip()
    return ""


def extract_fields(rec):
    """从接口 JSON 记录提取机构名称/地址; 字段名做了多候选容错(动态适配)。"""
    org = pick(rec, ["orgName", "fixMedinsName", "medinsName", "hospName",
                     "phacName", "name", "机构名称"])
    addr = pick(rec, ["adrDesc", "detAddrName", "addr", "address",
                      "bizEntsAddr", "detailAddress", "详情地址"])
    return {"机构名称": org, "详情地址": addr, "_raw": rec}


def parse_total(pager_texts):
    for t in pager_texts:
        m = re.search(r"总共\s*(\d+)\s*条", t)
        if m:
            return int(m.group(1))
    return None


# ---------------------------------------------------------------- 抓取核心
class NhsaCrawler:
    def __init__(self, headed=False, fast=False, manual_captcha=True):
        self.headed = headed
        self.fast = fast
        self.manual_captcha = manual_captcha
        self.detail_records = []     # 当前捕获的明文记录(来自接口拦截)
        self._wait = 1200 if fast else 2500

    # ---------- 基础设施 ----------
    async def start(self):
        self.p = await async_playwright().start()
        exe = find_chromium()
        args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        try:
            self.browser = await self.p.chromium.launch(headless=not self.headed,
                                                        executable_path=exe, args=args)
        except Exception:
            self.browser = await self.p.chromium.launch(headless=not self.headed, args=args)
        self.ctx = await self.browser.new_context(user_agent=UA,
                                                  viewport={"width": 1440, "height": 900},
                                                  locale="zh-CN")
        await self.ctx.add_init_script("""
            Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
            window.chrome={runtime:{}};
        """)
        self.page = await self.ctx.new_page()
        self.page.on("response", self._on_response)

    async def close(self):
        try:
            await self.browser.close()
        finally:
            await self.p.stop()

    async def _on_response(self, resp):
        """拦截 drugOptins 明细接口响应; 兼容密文/明文两种返回。"""
        if DETAIL_API_KEY not in resp.url:
            return
        try:
            js = await resp.json()
        except Exception:
            return
        node = js.get("data") if isinstance(js, dict) else None
        # 若响应本身是密文包裹 {data:{signData,encData,...}} -> 无法离线解密, 走 DOM 兜底
        if isinstance(node, dict) and "encData" in node and "list" not in node:
            self.detail_records = []
            return
        recs = self._find_list(js)
        if recs is not None:
            self.detail_records = recs

    @staticmethod
    def _find_list(obj, depth=0):
        """在任意嵌套 JSON 中寻找第一条元素含'机构/名称'类字段且非密文的 list。"""
        if depth > 5 or obj is None:
            return None
        if isinstance(obj, list):
            if obj and isinstance(obj[0], dict):
                return obj
            return None
        if isinstance(obj, dict):
            for key in ("list", "records", "rows", "result", "data"):
                if key in obj:
                    r = NhsaCrawler._find_list(obj[key], depth + 1)
                    if r is not None:
                        return r
        return None

    async def _pause(self, extra=0.0):
        await self.page.wait_for_timeout(int(self._wait + random.uniform(300, 900) + extra))

    async def _check_captcha(self):
        hit = await self.page.evaluate("""()=>{
            const vis=el=>!!el && el.offsetParent!==null;
            const cap=vis(document.querySelector('.nc-container,.slide-verify,[class*=captcha],[id*=captcha]'));
            const t=document.body.innerText||'';
            const dlg=vis(document.querySelector('.el-dialog__wrapper'));
            return cap || (/请完成验证|拖动|滑块|安全校验/.test(t)&&dlg);}""")
        if not hit:
            return
        print("[警告] 疑似出现验证码。", flush=True)
        if self.headed and self.manual_captcha:
            print(">>> 请在浏览器窗口中手动完成滑块验证，完成后按回车继续...", flush=True)
            await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
            await self._pause(1000)
        else:
            raise CaptchaError("检测到验证码(无头模式)，请用 --headed 运行人工过码或稍后重试。")

    # ---------- DOM 兜底解析(接口为密文时读表格) ----------
    async def read_table_dom(self):
        return await self.page.evaluate("""()=>{
          const tables=[...document.querySelectorAll('table')];
          // 取列数为4(序号/药品名称/机构名称/详情地址)的表
          for(const t of tables){
            const trs=[...t.querySelectorAll('tbody tr')];
            for(const tr of trs){
              const tds=[...tr.querySelectorAll('td')].map(x=>x.innerText.trim());
              if(tds.length>=4 && tds[2]){
                 return [...trs].map(r=>{const c=[...r.querySelectorAll('td')].map(x=>x.innerText.trim());
                    return {机构名称:c[2]||'', 详情地址:(c[3]||'').trim(), 药品名称:c[1]||''};})
                    .filter(x=>x.机构名称);
              }
            }
          }
          return null;}""")

    async def get_current_rows(self):
        """优先用接口拦截到的 JSON, 否则读 DOM。"""
        if self.detail_records:
            return [extract_fields(r) for r in self.detail_records if isinstance(r, dict)]
        rows = await self.read_table_dom()
        if rows:
            return [{"机构名称": r["机构名称"], "详情地址": r["详情地址"], "_raw": r} for r in rows]
        return []

    async def _wait_table_settle(self, tries=10):
        """等待机构表内容稳定(连续两次读数一致), 避免加载中误判空表。"""
        prev = None
        for _ in range(tries):
            cur = await self.page.evaluate("""()=>{
              const w=[...document.querySelectorAll('.el-table')].find(t=>{
                 const hs=[...t.querySelectorAll('th')].map(h=>h.innerText.trim());
                 return hs.includes('机构名称');});
              if(!w)return null;
              const rows=[...w.querySelectorAll('tbody tr')].length;
              const empty=!!w.querySelector('.el-table__empty-text')&&w.querySelector('.el-table__empty-text').offsetParent!==null;
              return {rows, empty};}""")
            if cur and prev and cur == prev:
                return cur
            prev = cur
            await self.page.wait_for_timeout(700)
        return prev

    async def read_form_state(self):
        return await self.page.evaluate("""()=>{
          const vis=e=>e&&e.offsetParent!==null;
          const pagerTxt=[...document.querySelectorAll('.el-pagination')].filter(vis).map(e=>e.innerText.replace(/\\n/g,'|'));
          const locInput=document.querySelector('.city-select-component input');
          const catInput=[...document.querySelectorAll('input')].find(i=>i.placeholder==='请选项'||i.placeholder==='请选择'&&i.closest('.el-form-item')&&/机构类别/.test(i.closest('.el-form-item').innerText));
          return {pagerTxt, loc:locInput?locInput.value:null, cat:catInput?catInput.value:null};}""")

    async def _dropdown_open(self):
        """是否有下拉/级联面板处于打开状态(ElementUI popper 用 visibility 隐藏)。"""
        return await self.page.evaluate("""()=>{
            const vis=el=>!!el && el.offsetParent!==null && getComputedStyle(el).visibility!=='hidden';
            return [...document.querySelectorAll('.el-select-dropdown,.el-cascader-menu,.el-cascader__dropdown')]
                   .some(vis);}""")

    async def open_dropdown(self, trigger):
        """健壮地打开下拉/级联面板: click -> mouse.click -> JS click -> focus+Enter。"""
        for meth in ("click", "press"):
            try:
                if meth == "click":
                    await trigger.click(timeout=4000)
                else:
                    box = await trigger.bounding_box()
                    if not box:
                        continue
                    await self.page.mouse.click(box["x"] + box["width"] / 2,
                                                box["y"] + box["height"] / 2)
            except Exception:
                continue
            await self.page.wait_for_timeout(900)
            if await self._dropdown_open():
                return True
        # 最后手段: JS 原生点击 + Enter
        try:
            await trigger.evaluate("el=>el.dispatchEvent(new MouseEvent('click',{bubbles:true}))")
            await self.page.wait_for_timeout(900)
            await trigger.focus()
            await self.page.keyboard.press("Enter")
            await self.page.wait_for_timeout(900)
        except Exception:
            pass
        return await self._dropdown_open()

    # ---------- 页面流程 ----------
    async def open_search_page(self, drug_name):
        await self.page.goto(BASE_URL, wait_until="domcontentloaded", timeout=90_000)
        await self._pause(3000)
        await self.page.evaluate("""()=>{document.querySelectorAll('.el-dialog__headerbtn')
            .forEach(b=>{if(b.offsetParent!==null)b.click();});}""")
        await self._pause()
        inp = self.page.locator("input[placeholder*='药品名称']").first
        await inp.click()
        for ch in drug_name:                       # 模拟真人逐字输入
            await inp.type(ch, delay=random.randint(40, 120))
        await self._pause()
        await self.page.locator("button:has-text('查询')").first.click()
        await self._pause(2500)
        await self._check_captcha()
        try:
            await self.page.wait_for_selector(f"td:has-text('{drug_name}')", timeout=20_000)
        except PWTimeout:
            if await self.page.locator("tbody tr").count() == 0:
                raise RuntimeError(f"未搜索到药品: {drug_name}")

    async def get_matched_drugs(self):
        """ElementUI 固定表头: header table 与 body table 分属不同 wrapper,
        需按共同的 .el-table 容器聚合 th + tbody 行。"""
        return await self.page.evaluate("""()=>{
          const out=[];
          document.querySelectorAll('.el-table').forEach(w=>{
            const hs=[...w.querySelectorAll('th')].map(h=>h.innerText.trim());
            if(!hs.includes('操作'))return;
            w.querySelectorAll('tbody tr').forEach(tr=>{
              const tds=[...tr.querySelectorAll('td')].map(td=>td.innerText.trim());
              const link=[...tr.querySelectorAll('button,a')].some(x=>x.innerText.trim()==='查看');
              if(tds.length>=2 && link) out.push({name:tds[1]});
            });
          });
          return out;}""")

    async def click_view(self, index=0):
        btns = self.page.locator("tbody tr button:has-text('查看'), tbody a:has-text('查看')")
        n = await btns.count()
        if n == 0:
            raise RuntimeError("找不到『查看』按钮")
        await btns.nth(min(index, n - 1)).click()
        await self._pause(3000)
        await self._check_captcha()

    async def select_location(self, name):
        """操作『所在地』级联选择器(ElementUI 2.x: li.el-cascader-node);
        name=None 表示清空(全国)。"""
        cas = self.page.locator(".city-select-component .el-input__inner").first
        cur = await cas.input_value()
        if name is None:
            if cur:
                cleared = await self.page.evaluate("""()=>{
                  const c=document.querySelector('.city-select-component'); if(!c)return false;
                  let el=c; while(el){ if(el.__vue__){ try{
                      const v=el.__vue__;
                      if(v.clearSelection){v.clearSelection();return true;}
                      if(v.$emit){v.$emit('input',''); v.value=''; return true;}
                  }catch(e){} break;} el=el.parentElement; }
                  return false;}""")
                if not cleared:
                    ok = await self.open_dropdown(cas)
                    if ok:
                        await self.page.keyboard.press("Escape")
                await self._pause()
            return
        if cur == name:
            return
        if not await self.open_dropdown(cas):
            raise LookupError(f"无法打开『所在地』级联面板 (目标: {name})")
        node = self.page.locator("li.el-cascader-node").filter(has_text=name).first
        try:
            await node.click(timeout=8_000)
        except PWTimeout:
            await self.page.keyboard.press("Escape")
            raise LookupError(f"所在地选项不存在: {name}")
        await self._pause(600)
        # 校验选中值; 若未生效则点击节点内的 radio 重试
        val = await cas.input_value()
        if not val:
            try:
                await node.locator(".el-radio, input.el-radio__original").first.click(timeout=5_000)
            except Exception:
                pass
            val = await cas.input_value()
        if not val:
            await self.page.keyboard.press("Escape")
            raise LookupError(f"选择『{name}』后输入框仍为空")
        # 面板若还开着(出现第二级地市菜单), 尝试选"全部", 否则 Esc 保持省级
        still = await self.page.evaluate("""()=>{const m=[...document.querySelectorAll('.el-cascader-menu')];
            return m.length>1 && m[1].offsetParent!==null;}""")
        if still:
            second = self.page.locator(".el-cascader-menu").nth(1)
            allnode = second.locator("li.el-cascader-node").filter(has_text="全部")
            if await allnode.count() > 0:
                await allnode.first.click()
            else:
                await self.page.keyboard.press("Escape")
        await self._pause()

    async def select_category(self, name):
        cat_input = self.page.locator("input[placeholder='请选项']").first
        cur = await cat_input.input_value()
        if cur == name:
            return
        if not await self.open_dropdown(cat_input):
            raise LookupError(f"无法打开『机构类别』下拉 (目标: {name})")
        opt = self.page.locator("li.el-select-dropdown__item").filter(has_text=name).first
        try:
            await opt.click(timeout=6_000)
        except PWTimeout:
            opts = await self.page.evaluate("""()=>[...document.querySelectorAll('.el-select-dropdown')]
                 .filter(d=>d.offsetParent!==null).map(d=>d.innerText.split('\\n')).flat().filter(Boolean).slice(0,20)""")
            await self.page.keyboard.press("Escape")
            raise LookupError(f"机构类别选项不存在: {name} (可见: {opts})")
        await self._pause()
        val = await cat_input.input_value()
        if val != name:
            raise LookupError(f"选择『机构类别={name}』失败, 当前值: {val!r}")

    async def click_detail_query(self):
        btns = self.page.locator("button:has-text('查询')")
        n = await btns.count()
        await btns.nth(n - 1).click()
        self.detail_records = []
        await self._pause(2500)
        await self._check_captcha()

    async def scrape_current_combo(self):
        """抓取当前 所在地+机构类别 组合下的全部分页记录(逐页翻页累积)。"""
        await self.click_detail_query()
        await self._wait_table_settle()          # 等表格稳定再读分页信息
        st = await self.read_form_state()
        total = parse_total(st["pagerTxt"])
        collected, seen = [], set()

        def add(rows):
            cnt = 0
            for r in rows:
                key = (r["机构名称"], r["详情地址"])
                if key not in seen:
                    seen.add(key)
                    collected.append(r); cnt += 1
            return cnt

        add(await self.get_current_rows())
        page_no = 1
        while page_no < 300:
            need_more = (total is not None and len(collected) < total) or \
                        (total is None and len(collected) % 10 == 0 and page_no == 1)
            if not need_more and page_no > 1:
                break
            nxt = self.page.locator(".el-pagination .btn-next:not([disabled])").last
            if await nxt.count() == 0:
                break
            try:
                await nxt.click(timeout=8_000)
            except PWTimeout:
                break
            await self._pause(1200)
            await self._check_captcha()
            await self._wait_table_settle(tries=6)
            before = len(collected)
            added = add(await self.get_current_rows())
            page_no += 1
            st = await self.read_form_state()
            t2 = parse_total(st["pagerTxt"])
            if t2:
                total = t2
            if added == 0 and before == len(collected):
                break  # 下一页无新数据, 结束
        return collected, total

    async def back_to_result_list(self):
        ok = await self.page.evaluate("""()=>{
          for(const e of document.querySelectorAll('button,a,span')){
            const t=(e.innerText||'').trim();
            if((t==='返回'||t.startsWith('< 返回')||t==='<返回')&&e.offsetParent!==null){e.click();return true;}}
          const ic=document.querySelector('.el-icon-back');
          if(ic&&ic.offsetParent!==null){(ic.closest('button,a,span')||ic).click();return true;}
          return false;}""")
        if not ok:
            await self.page.go_back()
        await self._pause(2500)

    # ---------- 主流程 ----------
    async def run(self, drugs, locations, categories, out_dir="output"):
        os.makedirs(out_dir, exist_ok=True)
        await self.start()
        summary = []
        try:
            for drug in drugs:
                print(f"\n===== 药品: {drug} =====", flush=True)
                await self.open_search_page(drug)
                matched = await self.get_matched_drugs()
                print(f"搜索结果 {len(matched)} 条: {[m['name'][:40] for m in matched]}", flush=True)
                if not matched:
                    continue
                for mi, m in enumerate(matched):
                    label = m["name"]
                    await self.click_view(mi)
                    print(f"-- 进入详情: {label}", flush=True)
                    combos_data = []
                    for cat in categories:
                        for loc in locations:
                            key = f"{loc or '全国'} / {cat}"
                            try:
                                await self.select_location(loc)
                                await self.select_category(cat)
                                recs, total = await self.scrape_current_combo()
                            except LookupError as e:
                                print(f"   [跳过] {key}: {e}", flush=True)
                                continue
                            except CaptchaError:
                                raise
                            except Exception as e:
                                print(f"   [错误] {key}: {e}", flush=True)
                                continue
                            print(f"   {key}: 抓到 {len(recs)} 条 (接口/分页 total={total})", flush=True)
                            combos_data.append({"location": loc, "category": cat,
                                                "total": total, "records": recs})
                    safe = re.sub(r"[\s/\\:*?\"<>|]+", "_", drug)
                    raw_path = os.path.join(out_dir, f"{safe}.json")
                    with open(raw_path, "w", encoding="utf-8") as f:
                        json.dump({"drug": drug, "label": label, "combos": combos_data,
                                   "captured_at": datetime.now().isoformat()},
                                  f, ensure_ascii=False, indent=1)
                    md_path = os.path.join(out_dir, f"{safe}.md")
                    write_markdown(md_path, drug, label, combos_data)
                    summary.append((drug, sum(len(c["records"]) for c in combos_data), md_path))
                    # 每个药品条目处理完后重新搜索, 保证状态干净
                    await self.open_search_page(drug)
        finally:
            await self.close()
        print("\n===== 汇总 =====")
        for s in summary:
            print(f"{s[0]} | 机构行数 {s[1]} | {s[2]}")
        return summary


# ---------------------------------------------------------------- Markdown 输出
def write_markdown(path, drug, label, combos_data):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# {drug} 配备机构名单", ""]
    lines.append("- **数据来源**: 国家医保服务平台 · 信息查询 → 公共查询 → 国谈药及商保创新药配备机构查询")
    lines.append(f"- **页面**: {BASE_URL}")
    lines.append(f"- **抓取时间**: {now}")
    lines.append(f"- **药品条目显示名**: {label}")
    lines.append("")
    seen_global = set()
    total_uniq = 0
    for c in combos_data:
        loc = c["location"] or "全国"
        cat = c["category"] or "全部类别"
        uniq = []
        for r in c["records"]:
            key = (r["机构名称"], r["详情地址"])
            if key in seen_global:
                continue
            seen_global.add(key)
            uniq.append(r)
        total_uniq += len(uniq)
        lines.append(f"## 所在地：{loc} ｜ 机构类别：{cat}（共 {c['total'] if c['total'] is not None else len(c['records'])} 条）")
        lines.append("")
        if not uniq:
            lines.append("_无记录_")
            lines.append("")
            continue
        lines.append("| 序号 | 机构名称 | 详情地址 |")
        lines.append("| --- | --- | --- |")
        for i, e in enumerate(uniq, 1):
            nm = str(e["机构名称"]).replace("|", "\\|").replace("\n", " ")
            ad = str(e["详情地址"]).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {i} | {nm} | {ad} |")
        lines.append("")
    lines.append("---")
    lines.append(f"合计去重机构数：**{total_uniq}**")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[已写出] {path} ({total_uniq} 家机构)", flush=True)


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="国家医保服务平台 国谈药配备机构爬虫")
    ap.add_argument("--drug", action="append", default=None, help="药品名称, 可多次传入")
    ap.add_argument("--locations", default="",
                    help="逗号分隔省市列表; 留空=自动遍历全部省份; ALL=不限地区一次抓全量")
    ap.add_argument("--categories", default="定点医疗机构,定点零售药店")
    ap.add_argument("--out", default="output")
    ap.add_argument("--headed", action="store_true", help="有头模式, 验证码可人工处理")
    ap.add_argument("--fast", action="store_true", help="减少等待时间")
    ap.add_argument("--auto-captcha-retry", type=int, default=2, help="遇验证码重建会话重试次数")
    args = ap.parse_args()

    drugs = args.drug or ["仑卡奈单抗注射液"]
    cats = [c.strip() for c in args.categories.split(",") if c.strip()]

    async def amain():
        nonlocal_loc = args.locations.strip()
        locs = None
        if nonlocal_loc.upper() == "ALL":
            locs = [None]
        elif nonlocal_loc:
            locs = [l.strip() for l in nonlocal_loc.split(",") if l.strip()]

        if locs is None:  # 动态获取省份列表(站点调整选项时无需改代码)
            probe = NhsaCrawler(headed=args.headed, fast=args.fast)
            await probe.start()
            try:
                await probe.open_search_page(drugs[0])
                await probe.click_view(0)
                await probe.page.locator(".city-select-component .el-input__inner").first.click()
                await probe.page.wait_for_timeout(2000)
                provs = await probe.page.evaluate("""()=>{
                   const menus=[...document.querySelectorAll('.el-cascader-menu')].filter(m=>m.offsetParent!==null);
                   if(!menus.length)return [];
                   return [...menus[0].querySelectorAll('.el-cascader-menu__node')].map(n=>n.innerText.trim());}""")
                await probe.page.keyboard.press("Escape")
                locs = provs or [None]
                print("自动获取到所在地列表:", locs, flush=True)
            finally:
                await probe.close()

        attempt = 0
        while True:
            try:
                c = NhsaCrawler(headed=args.headed, fast=args.fast)
                await c.run(drugs, locs, cats, args.out)
                return
            except CaptchaError as e:
                attempt += 1
                print(f"[验证码中断] {e}", flush=True)
                if not args.headed and attempt <= args.auto_captcha_retry:
                    print("重建浏览器会话并重试...", flush=True)
                    await asyncio.sleep(random.uniform(5, 10))
                    continue
                raise

    asyncio.run(amain())


if __name__ == "__main__":
    main()
