// Browser persistence regression with real iframe lifecycles and local stub bundles.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');

(async () => {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  const fixtures = JSON.parse(input);
  const browser = await chromium.launch({headless:true,
    ...(process.env.IV_CHROMIUM_PATH ? {executablePath:process.env.IV_CHROMIUM_PATH} : {})});
  const failures = [];
  let chatRequests = 0;
  async function session({blockStorage = false, failChart = false} = {}) {
    const context = await browser.newContext();
    if (blockStorage) await context.addInitScript(() => {
      Object.defineProperty(window, 'indexedDB', {get() { throw new Error('storage unavailable'); }});
    });
    await context.route('http://iv.test/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path.startsWith('/api/')) { chatRequests++; await route.fulfill({status:500, body:'No chat lookup allowed'}); }
      else if (path === '/static/chart.umd.min.js') {
        await route.fulfill(failChart ? {status:404, body:'missing'} : {contentType:'application/javascript', body:`
          window.Chart = function(canvas, config) {
            this.canvas = canvas.canvas || canvas;
            this.options = config.options || {}; this.update = function() {};
            const ctx = this.canvas.getContext('2d');
            ctx.fillStyle = 'blue'; ctx.fillRect(1, 1, config.data.datasets[0].data[0], 20);
            this.resize = function() {}; this.destroy = function() {delete Chart.instances[this.id];};
            this.id = Object.keys(Chart.instances).length; Chart.instances[this.id] = this;
          };
          Chart.instances = {}; Chart.defaults = {plugins:{legend:{labels:{}}}};
        `});
      } else if (path === '/static/plotly.umd.min.js') {
        // The unused dependency must not hold a Chart.js-only visualization hostage.
        await route.fulfill({status:404, body:'missing unused Plotly'});
      } else await route.fulfill({contentType:'text/html', body:'<!doctype html><body></body>'});
    });
    const page = await context.newPage();
    page.on('pageerror', error => failures.push(error.message));
    await page.goto('http://iv.test/');
    return {page, context};
  }
  async function mount(page, docs, prefix = 'unrelated') {
    await page.evaluate(({docs, prefix}) => {
      docs.forEach((srcdoc, i) => {
        const holder = document.createElement('section');
        // Deliberately no OWUI message IDs, embed indices, or VIZ markers.
        holder.id = prefix + '-' + i;
        const frame = document.createElement('iframe');
        frame.style.cssText = 'width:600px;height:260px';
        frame.srcdoc = srcdoc;
        holder.appendChild(frame); document.body.appendChild(holder);
      });
    }, {docs, prefix});
  }
  async function ready(page, index, expected) {
    await page.waitForFunction(({index, expected}) => {
      const f = document.querySelectorAll('iframe')[index];
      return f && f.contentWindow.__ivRenderStatus === 'ready' &&
        f.contentDocument.getElementById('value')?.textContent === String(expected);
    }, {index, expected}, {timeout:15000}).catch(async error => {
      console.error(await page.evaluate(() => Array.from(document.querySelectorAll('iframe')).map(f => ({
        state:f.dataset.ivState, status:f.contentWindow.__ivRenderStatus, text:f.contentDocument.body.innerText.slice(0,500)
      }))));
      throw error;
    });
  }
  try {
    const {page, context} = await session();
    await mount(page, fixtures.valid);
    await ready(page, 3, 40);
    await page.waitForFunction(() => document.querySelectorAll('iframe[data-iv-state="static"]').length === 2);
    // Restore the oldest chart using cached source; identical data and ID survive eviction.
    await page.evaluate(() => window.__ivLifecycleV5.activate(document.querySelector('iframe')));
    await ready(page, 0, 10);
    assert.equal(await page.locator('iframe').first().contentFrame().locator('#identity').textContent(), fixtures.ids[0]);
    await page.evaluate(() => {
      const runtime = document.querySelector('iframe').contentWindow;
      runtime.saveState('choice', 'kept');
      runtime.getToolData().value = 999; // runtime mutation must not rewrite the saved snapshot
    });
    // Unmount/remount in a different order and under unrelated wrappers.
    await page.evaluate(() => document.body.replaceChildren());
    await page.waitForFunction(() => Object.keys(window.__ivLifecycleV5.stats().states).length === 0);
    await mount(page, fixtures.valid.slice().reverse(), 'new-dom');
    await ready(page, 3, 10);
    assert.equal(await page.evaluate(() => document.querySelectorAll('iframe')[3].contentWindow.loadState('choice', null)), 'kept');
    await ready(page, 2, 20);
    assert.equal(await page.evaluate(() => document.querySelectorAll('iframe')[2].contentWindow.loadState('choice', null)), null);
    // Full page reload: no old manager records and no message source to inspect.
    await page.reload();
    await mount(page, [fixtures.valid[0]], 'after-reload');
    await ready(page, 0, 10);
    await context.close();

    // A clean browser context has neither localStorage nor the former IDB cache.
    const clean = await session({blockStorage:true});
    await mount(clean.page, fixtures.valid);
    await ready(clean.page, 3, 40);
    await clean.page.evaluate(() => window.__ivLifecycleV5.activate(document.querySelector('iframe')));
    await ready(clean.page, 0, 10);
    assert.equal(await clean.page.evaluate(() => document.querySelector('iframe').contentWindow.loadState('choice', null)), null);
    await clean.context.close();

    for (const [key, expected] of [
      ['unsupported', 'Unsupported saved artifact version'],
      ['malformed', 'Saved artifact contains invalid JSON'],
      ['missingData', 'Saved HTML or data is missing'],
      ['badScript', 'broken generated script'],
      ['plotly', 'Required library unavailable: plotly'],
    ]) {
      const check = await session();
      await mount(check.page, [fixtures[key]]);
      const alert = check.page.locator('iframe').contentFrame().locator('#iv-artifact-error');
      await alert.waitFor({timeout:15000});
      assert.ok((await alert.textContent()).includes(expected));
      assert.equal(await check.page.locator('iframe').contentFrame().locator('#iv-loader').count(), 0);
      await check.context.close();
    }
    const missingLibrary = await session({failChart:true});
    await mount(missingLibrary.page, [fixtures.valid[0]]);
    const alert = missingLibrary.page.locator('iframe').contentFrame().locator('#iv-artifact-error');
    await alert.waitFor({timeout:15000});
    assert.ok((await alert.textContent()).includes('Required library unavailable: chartjs'));
    await missingLibrary.context.close();
    assert.equal(chatRequests, 0, 'saved renderer must never fetch chat messages');
    assert.ok(failures.every(message => message.includes('broken generated script')), failures.join('\n'));
    console.log(JSON.stringify({restored:true, coldContext:true, storageBlocked:true, chatRequests, errorsVisible:true}));
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
