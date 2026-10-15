import asyncio
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from pdd_listing_automation.pages.upload_platform import UploadPlatformPage

STORE_HTML = '''<div role="dialog" aria-label="选择上货店铺">
    <label><span>其他店铺</span><input id="other" type="checkbox" checked></label>
    <label><span>目标店铺</span><input id="target" type="checkbox"></label>
    <label><span>已签署商家搬家承诺书</span>
      <span><input id="commitment" type="checkbox"><span class="ant-checkbox-inner">勾选</span></span>
    </label>
    <button onclick="window.submissions=(window.submissions||0)+1">开始上传</button>
    </div>'''

AGREEMENT_HTML = '''<div id="agreement" role="dialog"
    style="position:fixed;inset:0;background:white;z-index:99">
    <h2>超级店长用户服务协议和隐私政策</h2>
    <button id="agree">同意并继续</button></div>'''

COPY_TEMPLATE = "本店承诺在进行商品复制时，所使用的上家商品数据均已获得授权。如果未经授权复制他人商品而产生的任何纠纷与后果，本店将自行承担相应责任"
COPY_HTML = f'''<div id="copy-modal" role="dialog"
    style="position:fixed;inset:0;background:white;z-index:101">
    <h2>商品复制承诺书</h2><p>{COPY_TEMPLATE}</p>
    <textarea rows="6" placeholder="请输入承诺内容"></textarea>
    <button id="sign">确认签署并继续</button></div>'''
FRAME_HTML = '<iframe style="width:1200px;height:800px"></iframe>'


def test_selection_only_changes_shop_table_and_preserves_all_consent_fields():
    async def scenario():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(FRAME_HTML)
                frame = page.frames[1]
                await frame.set_content('''<div role="dialog" aria-label="选择上货店铺">
                    <table><thead><tr><th><input type="checkbox" checked></th></tr></thead><tbody>
                      <tr><td><input id="other" type="checkbox" checked></td><td>其他店铺</td></tr>
                      <tr><td><input id="target" type="checkbox"></td><td>目标店铺</td></tr>
                    </tbody></table>
                    <label>已签署商家搬家承诺书<input id="commitment" type="checkbox" checked></label>
                    <label>已阅读商品复制承诺书<input id="copy-consent" type="checkbox" checked></label>
                    <label>其他设置<input id="other-setting" type="checkbox" checked></label>
                    </div>''')
                await frame.evaluate("""() => {
                    window.consentChanges=0;
                    for (const id of ['commitment','copy-consent','other-setting']) {
                        document.getElementById(id).onchange=()=>window.consentChanges++;
                    }
                }""")
                upload = UploadPlatformPage(page)
                await upload.select_shop('目标店铺')
                await upload.verify_shop_selection()
                assert not await frame.locator('#other').is_checked()
                assert await frame.locator('#target').is_checked()
                assert await frame.locator('#commitment').is_checked()
                assert await frame.locator('#copy-consent').is_checked()
                assert await frame.locator('#other-setting').is_checked()
                assert await frame.evaluate('window.consentChanges') == 0
            finally:
                await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('prefilled,outer', [(False, False), (True, False), (True, True)])
def test_copy_modal_is_completed_before_shop_selection(prefilled, outer):
    async def scenario():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(FRAME_HTML + (COPY_HTML if outer else ''))
                frame = page.frames[1]
                await frame.set_content(STORE_HTML + ('' if outer else COPY_HTML))
                root = page if outer else frame
                if prefilled:
                    await root.get_by_placeholder('请输入承诺内容').fill(COPY_TEMPLATE)
                await root.evaluate("""() => {
                    window.signatures=0;
                    document.querySelector('#sign').onclick=()=>{
                        window.signatures++;
                        document.querySelector('#copy-modal').style.display='none';
                    };
                }""")
                upload = UploadPlatformPage(page)
                assert await upload.accept_copy_commitment()
                assert await root.get_by_placeholder('请输入承诺内容').input_value() == COPY_TEMPLATE
                await upload.select_shop('目标店铺')
                assert await root.evaluate('window.signatures') == 1
                assert await frame.locator('#target').is_checked()
                assert await frame.evaluate('window.submissions || 0') == 0
            finally:
                await browser.close()
    asyncio.run(scenario())


def test_copy_modal_opened_by_commitment_checkbox_finishes_signing():
    async def scenario():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(FRAME_HTML)
                frame = page.frames[1]
                await frame.set_content(STORE_HTML + COPY_HTML)
                await frame.evaluate("""() => {
                    const modal=document.querySelector('#copy-modal');
                    modal.style.display='none';
                    document.querySelector('#commitment').onclick=event=>{
                        event.preventDefault();
                        setTimeout(()=>modal.style.display='block',80);
                    };
                    document.querySelector('#sign').onclick=()=>{
                        modal.style.display='none';
                        setTimeout(()=>document.querySelector('#commitment').checked=true,80);
                    };
                }""")
                upload = UploadPlatformPage(page)
                await upload.select_shop('目标店铺')
                await upload.accept_commitment()
                assert await frame.locator('#commitment').is_checked()
                assert not await frame.locator('#copy-modal').is_visible()
                page.wait_for_timeout=AsyncMock()
                await upload.start_upload()
                assert await frame.evaluate('window.submissions') == 1
            finally:
                await browser.close()
    asyncio.run(scenario())


def test_unknown_copy_template_stops_before_signing():
    async def scenario():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(FRAME_HTML)
                frame = page.frames[1]
                await frame.set_content(COPY_HTML.replace(COPY_TEMPLATE, '未识别的新模板'))
                upload = UploadPlatformPage(page)
                with pytest.raises(RuntimeError, match='未识别到页面展示'):
                    await upload.accept_copy_commitment()
                assert await frame.get_by_placeholder('请输入承诺内容').input_value() == ''
                assert await frame.locator('#copy-modal').is_visible()
            finally:
                await browser.close()
    asyncio.run(scenario())


def test_shop_selection_preserves_signed_commitment_and_survives_rerender():
    async def scenario():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content("<iframe></iframe>")
                frame = page.frames[1]
                await frame.set_content(STORE_HTML)
                await frame.evaluate("""() => {
                    const field = document.querySelector('#commitment');
                    field.checked = true;
                    field.onclick = () => { throw new Error('must not touch existing consent'); };
                }""")
                upload = UploadPlatformPage(page)
                await upload.select_shop("目标店铺")
                assert await frame.locator('#commitment').is_checked()
                assert not await frame.locator('#other').is_checked()
                # React 替换节点后不依赖人工附加属性判断选店。
                await frame.evaluate("""() => {
                    for (const el of document.querySelectorAll('input')) {
                        const replacement = el.cloneNode();
                        replacement.checked = el.checked;
                        el.replaceWith(replacement);
                    }
                }""")
                await upload.verify_shop_selection()
                await upload.accept_commitment()
            finally:
                await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("outer,delayed", [(False, False), (True, False), (False, True)])
def test_service_modal_and_async_commitment_are_handled_before_upload(outer, delayed):
    async def scenario():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('<iframe></iframe>' + (AGREEMENT_HTML if outer else ''))
                frame = page.frames[1]
                await frame.set_content(STORE_HTML + ('' if outer else AGREEMENT_HTML))
                modal_root = page if outer else frame
                await modal_root.evaluate("""delayed => {
                    if (delayed) document.querySelector('#agreement').style.display='none';
                    window.agreements = 0;
                    document.querySelector('#agree').onclick = () => {
                        window.agreements++;
                        document.querySelector('#agreement').style.display='none';
                        const doc = document.querySelector('iframe')?.contentDocument || document;
                        setTimeout(() => doc.querySelector('#commitment').checked=true, 120);
                    };
                }""", delayed)
                if delayed:
                    await frame.evaluate("""() => {
                        document.querySelector('#commitment').onclick = event => {
                            event.preventDefault();
                            setTimeout(() => document.querySelector('#agreement').style.display='block', 100);
                        };
                    }""")
                upload = UploadPlatformPage(page)
                await upload.accept_service_agreement()
                await upload.select_shop("目标店铺")
                await upload.accept_commitment()
                assert await frame.locator('#commitment').is_checked()
                assert await modal_root.evaluate('window.agreements') == 1
                page.wait_for_timeout = AsyncMock()
                await upload.start_upload()
                assert await frame.evaluate('window.submissions') == 1
            finally:
                await browser.close()
    asyncio.run(scenario())


def test_async_checkbox_state_does_not_fail_immediately():
    async def scenario():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('<iframe></iframe>')
                frame = page.frames[1]
                await frame.set_content(STORE_HTML)
                await frame.evaluate("""() => {
                    document.querySelector('#commitment').onclick = event => {
                        event.preventDefault();
                        setTimeout(() => document.querySelector('#commitment').checked=true, 200);
                    };
                }""")
                upload = UploadPlatformPage(page)
                await upload.select_shop("目标店铺")
                await upload.accept_commitment()
                assert await frame.locator('#commitment').is_checked()
                await frame.locator('#commitment').evaluate('el => el.checked=false')
                with pytest.raises(RuntimeError, match="尚未确认"):
                    await upload.start_upload()
                assert await frame.evaluate('window.submissions || 0') == 0
            finally:
                await browser.close()
    asyncio.run(scenario())
