// Screenshots the element #box of an HTML file:  node shot.js in.html out.png
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async () => {
  const [html, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 2 });
  await page.goto('file://' + require('path').resolve(html));
  await page.locator('#box').screenshot({ path: out });
  await browser.close();
})();
