// Offline browser regression: real documents, layouts and IndexedDB, stub bundles.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');

(async () => {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  const fixtures = JSON.parse(input);
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.IV_CHROMIUM_PATH ? {executablePath: process.env.IV_CHROMIUM_PATH} : {})
  });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let chartLoads = 0, plotlyLoads = 0;
    await page.route('http://iv.test/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/static/chart.umd.min.js') {
        chartLoads++;
        await route.fulfill({contentType:'application/javascript', body:`
          window.Chart = function(el) {
            const ctx = el.getContext('2d'); ctx.fillStyle = 'blue'; ctx.fillRect(10,10,80,40);
            parent.renderCount = (parent.renderCount || 0) + 1;
            return {resize(){}};
          };
          Chart.instances = {}; Chart.defaults = {plugins:{legend:{labels:{}}}};
        `});
      } else if (path === '/static/plotly.umd.min.js') {
        plotlyLoads++;
        // Chart-only consumers must render despite an unavailable Plotly bundle.
        await route.fulfill({status:404, body:'not installed'});
      } else {
        await route.fulfill({contentType:'text/html', body:'<!doctype html><html><body></body></html>'});
      }
    });
    await page.goto('http://iv.test/');
    await page.evaluate(fixtures => {
      window.renderCount = 0;
      window.peakHeavy = 0;
      new MutationObserver(() => {
        const count = document.querySelectorAll('iframe[data-iv-state="loading"],iframe[data-iv-state="live"],iframe[data-iv-state="suspending"]').length;
        window.peakHeavy = Math.max(window.peakHeavy, count);
      }).observe(document.body, {subtree:true, attributes:true, attributeFilter:['data-iv-state']});
      fixtures.forEach((srcdoc, index) => {
        const message = document.createElement('div');
        message.id = 'message-m' + index;
        message.className = 'chat-assistant';
        const text = document.createElement('div');
        text.textContent = '@@@VIZ-START\n<canvas id="chart" width="300" height="150"></canvas><script data-iv-libraries="chartjs">new Chart(document.getElementById("chart"));</script>\n@@@VIZ-END';
        message.appendChild(text);
        const mount = document.createElement('div');
        mount.id = 'm' + index + '-embeds-0';
        const frame = document.createElement('iframe');
        frame.style.cssText = 'width:600px;height:240px';
        frame.srcdoc = srcdoc;
        mount.appendChild(frame);
        message.appendChild(mount);
        document.body.appendChild(message);
      });
    }, fixtures);
    await page.waitForFunction(() => document.querySelectorAll('iframe[data-iv-state="static"]').length === 18 &&
      document.querySelectorAll('iframe[data-iv-state="live"]').length === 2, null, {timeout:15000}).catch(async error => {
        console.error(JSON.stringify({errors, chartLoads, plotlyLoads, frames:await page.evaluate(() => Array.from(document.querySelectorAll('iframe')).map(f=>({state:f.dataset.ivState,body:f.contentDocument.body.innerText.slice(0,250)})))}));
        throw error;
      });
    assert.equal(await page.evaluate(() => window.peakHeavy), 2);
    assert.ok(chartLoads <= 2, `only admitted frames load Chart.js, got ${chartLoads}`);
    assert.equal(await page.evaluate(() => window.renderCount), 2);
    assert.equal(await page.evaluate(() => window.__ivLifecycleV5.stats().sourceBytes), 0, 'parked sources live in IndexedDB, not the RAM fallback');
    assert.equal(await page.evaluate(() => document.body.innerText.includes('@@@VIZ-START')), false, 'paused sources remain hidden');
    assert.equal(await page.locator('iframe').last().contentFrame().locator('#iv-script-load-error').count(), 0);

    // Restore an older source from IndexedDB, with failed preview capture.
    await page.evaluate(() => {
      document.querySelectorAll('iframe[data-iv-state="live"]').forEach(f => {
        f.contentWindow._ivCreateSnapshot = () => Promise.resolve(null);
      });
      window.__ivLifecycleV5.activate(document.querySelector('iframe'));
    });
    await page.waitForFunction(() => document.querySelector('iframe').dataset.ivState === 'live', null, {timeout:15000});
    assert.equal(await page.evaluate(() => window.peakHeavy), 2);
    assert.equal(await page.evaluate(() => window.renderCount), 3);
    assert.equal(await page.locator('iframe').first().contentFrame().locator('#iv-script-load-error').count(), 0);

    // Repeated SPA unmounts must release records and cached sources.
    await page.evaluate(() => {
      document.querySelectorAll('.chat-assistant').forEach(message=>message.remove());
    });
    await page.waitForFunction(() => Object.keys(window.__ivLifecycleV5.stats().states).length === 0);
    assert.equal(await page.evaluate(() => window.__ivLifecycleV5.stats().previewBytes), 0);
    assert.deepEqual(errors, [], errors.join('\n'));
    console.log(JSON.stringify({chartLoads, plotlyLoads, rendered:await page.evaluate(() => window.renderCount), peakHeavy:2}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
