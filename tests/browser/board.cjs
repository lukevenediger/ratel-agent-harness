// Real Chromium regression suite. Requests can deliberately ignore AbortSignal:
// late responses still need generation checks, even if cancellation loses a race.
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const [base, raw] = process.argv.slice(2), ids = JSON.parse(raw);
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const context = await browser.newContext({viewport:{width:1280, height:900}});
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      const fetch = window.fetch;
      window.fetch = (url, options={}) => fetch(url, {...options, signal:undefined});
      window.__streams = [];
      window.EventSource = class extends EventTarget {
        constructor(url) { super(); this.url=url; window.__streams.push(this); setTimeout(() => this.onopen?.(), 0); }
        close() { this.closed=true; }
        emit(type, data) { this.dispatchEvent(new MessageEvent(type, {data:JSON.stringify(data)})); }
      };
    });
    const channel = name => page.locator(`#channels button[data-channel="${name}"]`).click();
    await page.goto(base + '/#alpha');
    await page.waitForFunction(() => document.querySelectorAll('#messages .msg').length === 100);
    assert.equal(await page.locator('#connection').innerText(), 'Live');
    await page.locator('#olderMessages').click();
    await page.waitForFunction(() => document.querySelectorAll('#messages .msg').length === 122);
    assert.equal(await page.locator('#olderMessages').isVisible(), false);
    assert.equal(await page.locator('#messages .msg').first().getAttribute('data-id'), ids.parent);
    await page.locator('#search').fill('needle');
    await page.locator('#search').press('Enter');
    await page.waitForFunction(() => document.querySelectorAll('#messages .msg:not([hidden])').length === 1);
    assert.equal(await page.locator('#messages .msg').first().getAttribute('data-id'), ids.parent);
    assert.ok((await page.locator('#channelLink').getAttribute('href')).includes('q=needle'));
    await page.locator('#operatorOnly').check();
    await page.waitForFunction(() => document.querySelector('#connection').textContent === 'Live');
    await page.locator('#filter').selectOption('stakeholder');
    await page.waitForFunction(() => document.querySelector('#connection').textContent === 'Live');
    assert.equal(await page.locator('#messages .msg').count(), 1);
    assert.ok(page.url().includes('operator=1') && page.url().includes('mention=stakeholder'));
    await page.locator('#messages .msg').first().press('Enter');
    await page.waitForSelector('#thread .replies .msg');
    assert.equal(await page.locator('#thread .replies .msg').count(), 1);
    const deepLink = await page.locator('#thread .thread-link').getAttribute('href');
    await page.reload();
    await page.waitForSelector('#thread .replies .msg');
    assert.ok(page.url().endsWith(deepLink));
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#thread').isVisible(), false);
    assert.ok(!page.url().includes('thread='));
    await page.goBack(); await page.waitForSelector('#thread .replies .msg');
    await page.goForward(); await page.waitForFunction(() => document.querySelector('#thread').hidden);
    console.log('PASS paging, whole-history search, keyboard thread and restored deep links');

    // Hold a channel request and deliver it after another selection wins.
    let release, arrived;
    const blocked = new Promise(resolve => arrived=resolve);
    await page.route('**/api/channels/alpha/history?**', async route => {
      const response = await route.fetch(); arrived();
      await new Promise(resolve => release=resolve);
      await route.fulfill({response});
    });
    await channel('beta'); await page.waitForSelector('#messages .msg');
    await channel('alpha'); await blocked;
    await channel('beta'); await page.waitForFunction(() => document.querySelector('#channame').textContent === '#beta' && document.querySelector('#messages').textContent.includes('Beta only'));
    release(); await pause(150);
    assert.equal(await page.locator('#messages .msg').count(), 1);
    assert.ok((await page.locator('#messages').innerText()).includes('Beta only'));
    await page.unroute('**/api/channels/alpha/history?**');
    // A closed stream's queued callback must never render in a newer channel.
    await page.evaluate(id => window.__streams[0].emit('message', {
      id, from:'rogue', text:'STALE STREAM', ts:new Date().toISOString(), mentions:[], attachments:[], parent:null
    }), ids.stale);
    assert.ok(!(await page.locator('#messages').innerText()).includes('STALE STREAM'));
    console.log('PASS delayed channel response and stale stream isolation');

    const releases=[];
    let metadataReady;
    const metadataBlocked = new Promise(resolve => metadataReady=resolve);
    await page.route('**/api/channels/alpha/{pins,clan}', async route => {
      const response = await route.fetch();
      await new Promise(resolve => { releases.push(resolve); if (releases.length===2) metadataReady(); });
      if (route.request().url().endsWith('/clan'))
        await route.fulfill({json:{roles:[{role:'STALE-ROLE', state:'idle', confidence:'high', context_tokens:10, checkpoint_at:100}]}});
      else await route.fulfill({response});
    });
    await channel('alpha'); await metadataBlocked;
    await channel('beta');
    await page.waitForFunction(() => document.querySelector('#connection').textContent === 'Live');
    releases.forEach(release => release()); await pause(150);
    assert.ok(!(await page.locator('#pins').innerText()).includes('Needle'));
    assert.equal(await page.locator('#clanMeter').isVisible(), false);
    await page.unroute('**/api/channels/alpha/{pins,clan}');
    console.log('PASS delayed pins and clan refresh stay in their channel');

    let oldClan, clanArrived, clanRequests=0;
    const clanBlocked = new Promise(resolve => clanArrived=resolve);
    await page.route('**/api/channels/beta/clan', async route => {
      if (++clanRequests === 1) {
        clanArrived(); await new Promise(resolve => oldClan=resolve);
        await route.fulfill({status:503, body:'older failure'});
      } else await route.fulfill({json:{roles:[{role:'fresh-role', state:'idle', confidence:'high', context_tokens:10, checkpoint_at:100}]}});
    });
    await page.evaluate(() => { refreshClan(true); }); await clanBlocked;
    await page.evaluate(() => refreshClan(true));
    assert.equal(await page.locator('#clanMeter').isVisible(), true);
    oldClan(); await pause(150);
    assert.equal(await page.locator('#clanMeter').isVisible(), true);
    await page.unroute('**/api/channels/beta/clan');
    console.log('PASS stale same-channel failure cannot hide a newer clan refresh');

    let releaseThread, threadArrived;
    const threadBlocked = new Promise(resolve => threadArrived=resolve);
    await page.route('**/thread/**', async route => {
      const response = await route.fetch(); threadArrived();
      await new Promise(resolve => releaseThread=resolve); await route.fulfill({response});
    });
    await page.locator('#messages .msg').first().press('Enter'); await threadBlocked;
    await page.getByRole('button', {name:'Close thread', exact:true}).click();
    releaseThread(); await pause(150);
    assert.equal(await page.locator('#thread').isVisible(), false);
    await page.unroute('**/thread/**');
    console.log('PASS closing an in-flight thread prevents reopening');

    await page.evaluate(() => window.__streams.at(-1).onerror());
    assert.ok((await page.locator('#connection').innerText()).includes('Reconnecting'));
    await page.locator('#connection button').click();
    await page.waitForFunction(() => document.querySelector('#connection').textContent === 'Live');
    await page.evaluate(id => {
      const stream=window.__streams.at(-1);
      const msg={id, from:'developer', text:'LIVE ONCE', ts:new Date().toISOString(), mentions:[], attachments:[], parent:null};
      stream.emit('message', msg); stream.emit('message', msg);
    }, ids.live);
    assert.equal(await page.locator(`#messages .msg[data-id="${ids.live}"]`).count(), 1);
    console.log('PASS reconnect feedback, retry and replay deduplication');

    await page.route('**/api/channels/alpha/history?**', route => route.fulfill({status:503, body:'unavailable'}));
    await channel('alpha');
    await page.waitForFunction(() => document.querySelector('#connection').textContent.includes('Could not load'));
    await page.unroute('**/api/channels/alpha/history?**');
    await page.locator('#connection button').click();
    await page.waitForFunction(() => document.querySelectorAll('#messages .msg').length === 100);
    await page.setViewportSize({width:390, height:844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.locator('#messages .msg').first().press('Enter');
    await page.waitForSelector('#thread .parent-card');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#thread').isVisible(), false);
    assert.equal(await page.evaluate(() => document.body.style.overflow), '');
    await page.locator('#search').fill('<img src=x onerror=alert(1)>');
    await page.locator('#search').press('Enter');
    await page.waitForFunction(() => document.querySelectorAll('#messages .msg').length === 1);
    assert.equal(await page.locator('#messages img').count(), 0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    console.log('PASS request failure retry, mobile layout, Escape and inert hostile text');
    // This second page uses the real EventSource implementation and server.
    const livePage = await context.newPage();
    await livePage.goto(base + '/#beta');
    await livePage.waitForFunction(() => document.querySelector('#connection').textContent === 'Live');
    const {execFileSync} = require('node:child_process');
    execFileSync(ids.python, ['-m', 'ratel.cli', 'post', 'Actual stream event', '--home', ids.home,
                             '--agent', 'developer', '--channel', 'beta']);
    await livePage.waitForFunction(() => document.querySelector('#messages').textContent.includes('Actual stream event'));
    assert.equal(await livePage.getByText('Actual stream event', {exact:true}).count(), 1);
    await livePage.close();
    console.log('PASS CLI post delivered over real browser SSE');
    const approvalPage = await context.newPage();
    approvalPage.on('pageerror', error => errors.push(error.message));
    await approvalPage.goto(base + '/?token=' + encodeURIComponent(ids.token) + '#gamma');
    await approvalPage.waitForSelector('#pins .clanadd');
    await approvalPage.waitForFunction(() => !document.querySelector('#pins .confirm').disabled);
    assert.ok(!approvalPage.url().includes('token='));
    await approvalPage.route('**/api/channels/gamma/post', route => route.abort());
    await approvalPage.locator('#pins .confirm').click();
    await approvalPage.waitForFunction(() => document.querySelector('#pins .clanhint').textContent.includes('Could not confirm'));
    await approvalPage.unroute('**/api/channels/gamma/post');
    await approvalPage.locator('#pins .confirm').click();
    await approvalPage.waitForFunction(() => document.querySelector('#pins .clanhead').textContent.includes('approved'));
    await approvalPage.close();
    console.log('PASS older pinned proposal retains editable roles and can be confirmed');
    assert.deepEqual(errors, []);
    await context.close();
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode=1; });
