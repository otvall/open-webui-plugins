// Public-tool event fixtures; real iframes with a minimal OWUI-like embed list.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

(async () => {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  const fixtures = JSON.parse(input);
  const browser = await chromium.launch({headless:true,
    ...(process.env.IV_CHROMIUM_PATH ? {executablePath:process.env.IV_CHROMIUM_PATH} : {})});
  try {
    const page = await browser.newPage();
    let bundles = 0;
    await page.route('http://iv.test/**', async route => {
      if (new URL(route.request().url()).pathname.startsWith('/static/')) {
        bundles++;
        await route.fulfill({status:404, body:'unused for this plain JS fixture'});
      } else await route.fulfill({contentType:'text/html', body:'<!doctype html><body></body>'});
    });
    await page.goto('http://iv.test/');
    async function mount(document) {
      await page.evaluate(document => {
        if (!window.chartFrame) {
          const neighbor = window.document.createElement('iframe');
          neighbor.id = 'neighbor'; neighbor.srcdoc = '<p id="kept">Existing chart</p>';
          window.document.body.appendChild(neighbor);
          window.chartFrame = window.document.createElement('iframe');
          chartFrame.id = 'chart'; chartFrame.style.cssText = 'width:600px;height:240px';
          window.document.body.appendChild(chartFrame);
        }
        chartFrame.srcdoc = document;
      }, document);
    }
    const progress = () => page.locator('#chart').contentFrame().locator('#iv-progress');
    async function mountToolOutput() {
      await page.evaluate(result => {
        const output = document.createElement('div');
        output.id = 'tool-output';
        output.textContent = JSON.stringify(result);
        // A separate surface, as in OWUI: output embeds are not deduplicated
        // against message embeds by visualization ID.
        for (const html of result.embeds || []) {
          const frame = document.createElement('iframe');
          frame.srcdoc = html;
          output.appendChild(frame);
        }
        document.body.appendChild(output);
      }, fixtures.toolResult);
      assert.equal(await page.locator('iframe').count(), 2, 'one chart plus the untouched neighbor');
      assert.equal(await page.locator('#tool-output iframe').count(), 0, 'tool output must not mount a second chart');
    }
    await mount(fixtures.pending);
    await progress().waitFor();
    assert.match(await progress().textContent(), /Загрузка/);
    assert.equal(bundles, 0, 'loading must not download chart bundles');
    await mount(fixtures.final);
    await mountToolOutput();
    await page.waitForFunction(() => chartFrame.contentWindow.__ivRenderStatus === 'ready');
    assert.equal(await page.locator('#chart').contentFrame().locator('#value').textContent(), '42');
    assert.equal(await page.locator('#neighbor').contentFrame().locator('#kept').textContent(), 'Existing chart');
    assert.equal(await page.evaluate(() => chartFrame.contentDocument.documentElement.dataset.ivVisualizationId), fixtures.key);
    // Restore the persisted final document, not an in-memory HTML patch.
    await page.reload();
    await mount(fixtures.final);
    await mountToolOutput();
    await page.waitForFunction(() => chartFrame.contentWindow.__ivRenderStatus === 'ready');
    assert.equal(await page.locator('#chart').contentFrame().locator('#value').textContent(), '42');
    await mount(fixtures.expired);
    await page.waitForFunction(() => chartFrame.contentDocument.getElementById('iv-progress')?.getAttribute('role') === 'alert');
    assert.match(await progress().textContent(), /прервана/);
    await mount(fixtures.error);
    await page.waitForFunction(() => chartFrame.contentDocument.querySelector('h2')?.textContent.includes('<img'));
    assert.equal(await page.locator('#chart').contentFrame().locator('img').count(), 0);
    assert.match(await progress().textContent(), /Генерация прервана/);
    console.log(JSON.stringify({restored:true}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
