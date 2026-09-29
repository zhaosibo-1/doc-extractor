// 前端渲染验证 + 截图。用真实浏览器点一遍「选示例 → 跑抽取」，然后看图。
//
//   node scripts/shot_frontend.js http://127.0.0.1:8000 docs
//
// 依赖 Playwright 与一个 Chromium。两者都用环境变量覆盖：
//   PW_PATH  = playwright-core 的 require 路径
//   CHROME   = chrome/Chromium 可执行文件路径
// 不设就从常见的几个位置自动找 —— 找不到会给出清楚的提示，
// 而不是抛一个让人猜的 MODULE_NOT_FOUND。
//
// 这个脚本比单元测试多做的一件事：**看渲染出来的几何**。
// 置信度条的宽度、完成度圆环的弧长、来源标签的数量，都是「HTML 里有这个元素」
// 看不出来的 —— 而它们恰恰是「CSS 没生效 / 宽度没设上」这类问题的唯一症状。

const fs = require('fs');
const path = require('path');

const BASE = process.argv[2] || 'http://127.0.0.1:8000';
const OUT = process.argv[3] || 'docs';

const PW_CANDIDATES = [
  process.env.PW_PATH,
  'playwright-core',
  'playwright',
  'C:/Users/Legion/npm_global/node_modules/n8n/node_modules/playwright-core',
].filter(Boolean);

const CHROME_CANDIDATES = [
  process.env.CHROME,
  process.env.CHROME_PATH,
  'C:/Users/Legion/.agent-browser/browsers/chrome-153.0.8010.36/chrome.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
].filter(Boolean);

function loadPlaywright() {
  for (const candidate of PW_CANDIDATES) {
    try {
      return require(candidate);
    } catch (_) {
      /* 试下一个 */
    }
  }
  console.error('找不到 playwright-core。装一个，或用 PW_PATH 指过去：\n' +
    '  npm i -D playwright-core\n' +
    '  PW_PATH=/path/to/playwright-core node scripts/shot_frontend.js');
  process.exit(2);
}

function findChrome() {
  for (const candidate of CHROME_CANDIDATES) {
    if (candidate && fs.existsSync(candidate)) return candidate;
  }
  return null;   // 交给 Playwright 用它自己下载的那个
}

const { chromium } = loadPlaywright();
const CHROME = findChrome();

if (!fs.existsSync(OUT)) fs.mkdirSync(OUT, { recursive: true });

const problems = [];
const notes = [];

(async () => {
  const browser = await chromium.launch(CHROME ? { executablePath: CHROME } : {});
  const page = await browser.newPage({
    viewport: { width: 1680, height: 1050 },
    deviceScaleFactor: 1.5,
  });

  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => errors.push('PAGEERROR: ' + e.message));

  await page.goto(BASE + '/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1200);

  // ---- 静态断言：前端查的元素必须都在 ----
  // tolInput 是**渲染结果后**才注入的（在结果区的按钮行里），所以不在这一批。
  // 动态 id 白名单只留它一个 —— 名单越长越容易掩盖真的缺失。
  const DYNAMIC_IDS = ['tolInput'];
  const missing = await page.evaluate(() => {
    const want = ['textInput', 'resultArea', 'btnRun', 'tplGrid', 'tplDetail',
                  'engineSel', 'exList', 'modeTabs', 'drop'];
    return want.filter((id) => !document.getElementById(id));
  });
  // 白名单里的 id 不该在静态 HTML 里出现，否则说明这个白名单在说谎
  const staticHasDynamic = await page.evaluate((dynamic) =>
    dynamic.filter((id) => document.getElementById(id)), DYNAMIC_IDS);
  if (missing.length) problems.push('缺元素: ' + missing.join(', '));
  if (staticHasDynamic.length) problems.push('动态 id 白名单过期: ' + staticHasDynamic.join(', '));
  notes.push('静态元素齐全: ' + (missing.length ? missing.join(',') : '全部 9 个 ✓'));

  // ---- 真实交互：切到「内置示例」标签页，再点一个二进制示例 ----
  // 示例列表在第三个 pane 里，默认 display:none —— 必须先点标签页。
  // 这一步同时也验证了标签页切换真的工作（只读 DOM 字符串看不出这个）。
  await page.click('#modeTabs .tab[data-mode="example"]');
  await page.waitForTimeout(400);
  const paneVisible = await page.evaluate(() => {
    const pane = document.querySelector('.pane[data-pane="example"]');
    const r = pane.getBoundingClientRect();
    return r.width > 100 && r.height > 50;
  });
  if (!paneVisible) problems.push('切到「内置示例」后 pane 仍然不可见，标签页切换坏了');
  notes.push('示例标签页切换: ' + (paneVisible ? '可见 ✓' : '不可见 ✗'));

  const ex = await page.$('#exList .ex[data-key="order_pdf"]');
  if (!ex) problems.push('示例列表里没有 order_pdf');
  else {
    const label = (await ex.innerText()).replace(/\s+/g, ' ').slice(0, 40);
    await ex.click();
    await page.waitForTimeout(1500);
    notes.push('已点击示例: ' + label);

    const srcState = await page.$eval('#srcState', (e) => e.textContent.trim());
    if (!/示例/.test(srcState)) problems.push('载入示例后来源状态没更新: ' + srcState);
    notes.push('来源状态: ' + srcState);

    const tpl = await page.$eval('#tplGrid', (e) => e.innerText.replace(/\s+/g, ' ').slice(0, 60));
    notes.push('模板区: ' + tpl);
  }

  // ---- 跑一次抽取（走 SSE）----
  await page.click('#btnRun');
  await page.waitForFunction(
    () => {
      const el = document.getElementById('resultArea');
      return el && el.querySelector('.ring, .kpi, .sect-t');
    },
    null,
    { timeout: 30000 }
  ).catch(() => problems.push('30 秒内没有渲染出结果区'));
  await page.waitForTimeout(1200);

  // ---- 断言渲染出的内容真有数据（看几何，不是看字符串）----
  const shape = await page.evaluate(() => {
    const area = document.getElementById('resultArea');
    const bars = Array.from(area.querySelectorAll('.conf .cbar i'));
    const arc = area.querySelector('.ring svg circle:last-of-type');
    return {
      fields: area.querySelectorAll('.frow').length,
      rules: area.querySelectorAll('.rule').length,
      rulesPass: area.querySelectorAll('.rule.pass').length,
      srcTags: area.querySelectorAll('.srctag').length,
      bars: bars.length,
      barsWithWidth: bars.filter((b) => b.getBoundingClientRect().width > 1).length,
      // 环形图的进度弧靠 stroke-dashoffset 表达，offset 没变就等于没画
      dashOffset: arc ? Number(arc.getAttribute('stroke-dashoffset')) : -1,
      chars: area.innerText.length,
      ringText: (area.querySelector('.ring .num') || {}).textContent || '',
    };
  });
  notes.push('结果区: ' + JSON.stringify(shape));

  if (shape.fields !== 7) problems.push(`销售订单模板应有 7 个字段行，实际渲染 ${shape.fields} 个`);
  if (shape.rules !== 3) problems.push(`销售订单模板应有 3 条规则，实际渲染 ${shape.rules} 条`);
  if (shape.rulesPass !== 3) problems.push(`应有 3 条规则通过，实际 ${shape.rulesPass} 条`);
  if (shape.srcTags < 5) problems.push(`来源定位标签只有 ${shape.srcTags} 个，来源信息可能没渲染`);
  if (shape.bars !== shape.fields) problems.push(`置信度条数量(${shape.bars})与字段行数(${shape.fields})不符`);
  if (shape.barsWithWidth !== shape.bars) {
    problems.push(`有 ${shape.bars - shape.barsWithWidth} 条置信度条宽度为 0（CSS 没生效或宽度没设）`);
  }
  if (!(shape.dashOffset >= 0 && shape.dashOffset < 10)) {
    problems.push(`完成度环形图的弧长不对：dashOffset=${shape.dashOffset}（满环应为 0）`);
  }
  if (!/100/.test(shape.ringText)) problems.push('环形图中心没有显示 100%：' + shape.ringText);
  if (shape.chars < 500) problems.push('结果区文字太少，可能没渲染: ' + shape.chars);

  // ---- 容差输入框真的在（刚加的）----
  const tol0 = await page.$('#tolInput');
  if (!tol0) problems.push('容差输入框 #tolInput 没渲染出来');
  else {
    notes.push('容差输入框存在，placeholder=' + (await tol0.getAttribute('placeholder')));
    await tol0.fill('1');
    await page.click('#btnReval');
    await page.waitForTimeout(1500);
    const toast = await page.$eval('#toast', (e) => e.textContent.trim()).catch(() => '');
    notes.push('重跑校验 toast: ' + toast);
    if (!/容差 1/.test(toast)) problems.push('重跑校验没有带上容差参数: ' + toast);

    // 重跑会整块重建结果区，旧句柄已经脱离 DOM —— 必须重新取。
    // 顺带验证「用户填的容差不会被重建冲掉」。
    const tol1 = await page.$('#tolInput');
    if (!tol1) problems.push('重跑校验后容差输入框消失了');
    else {
      const kept = await tol1.inputValue();
      notes.push('重跑后容差输入框的值: ' + JSON.stringify(kept));
      if (kept !== '1') problems.push('重跑校验把用户填的容差冲掉了: ' + JSON.stringify(kept));
    }
  }

  // 先回顶部拍视口图 —— README 的首图要是「打开就看到的样子」，
  // 不是一张 3000px 高、缩到卡片里什么都看不清的全页长图。
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(600);
  await page.screenshot({ path: OUT + '/preview.png' });
  // 全页图留作证据（能一眼看到结果区到底渲染了多长）
  await page.screenshot({ path: OUT + '/preview_full.png', fullPage: true });

  if (errors.length) problems.push('控制台错误: ' + errors.slice(0, 3).join(' | '));

  await browser.close();

  console.log('--- 观察 ---');
  notes.forEach((n) => console.log('  ' + n));
  console.log('--- 结论 ---');
  if (problems.length) {
    problems.forEach((p) => console.log('  [!] ' + p));
    process.exit(1);
  }
  console.log('  前端渲染验证通过 ✓');
})().catch((e) => { console.error('脚本失败:', e.message); process.exit(2); });
