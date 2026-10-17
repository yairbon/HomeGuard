// Renders the report's two diagrams (system block diagram, alarm states) to
// PNG with mermaid + headless Chromium.
//
//   node diagrams.js <out_dir>        (needs `npm i mermaid@10.9.3` next to it)
//
// Labels are English on purpose, like the course's own example reports.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const path = require('path');
const fs = require('fs');

const outDir = process.argv[2] || '.';
fs.mkdirSync(outDir, { recursive: true });

const D = {};

// Figure 1: the whole system
D.fig1 = `flowchart LR
  subgraph B["3 x ESP32"]
    direction TB
    S["Sensor board<br/>distance + vibration"]
    K["Door board<br/>door sensor, keypad, buzzers"]
    C["Camera board<br/>ESP32-CAM"]
  end
  BE["Backend (Python)<br/>alarm state, face recognition"]
  P["Phone app"]
  M["Email<br/>with camera picture"]
  S -->|"events"| BE
  K <-->|"events / commands"| BE
  C -->|"pictures"| BE
  BE <-->|"WiFi"| P
  BE --> M`;

// Figure 3: alarm states
D.fig3 = `stateDiagram-v2
  direction LR
  [*] --> Disarmed
  Disarmed --> Armed: keypad or app
  Armed --> EntryDelay: door opened
  Armed --> Alarm: sensor triggered
  EntryDelay --> Alarm: 30 s passed
  EntryDelay --> Disarmed: PIN
  Alarm --> Disarmed: PIN or app
  Armed --> Disarmed: app
  EntryDelay: Entry delay`;

(async () => {
  const browser = await chromium.launch();
  const mermaidPath = path.join(__dirname, 'node_modules/mermaid/dist/mermaid.min.js');
  for (const [name, src] of Object.entries(D)) {
    const page = await browser.newPage({ viewport: { width: 1500, height: 900 }, deviceScaleFactor: 2 });
    await page.setContent(`<html><head><style>
      body{background:#fff;margin:0;padding:12px;font-family:'Liberation Sans',Arial,sans-serif;display:inline-block}
    </style></head><body><div id="wrap"><pre class="mermaid">${src.replace(/</g, '&lt;').replace(/&lt;br\/>/g, '<br/>')}</pre></div></body></html>`);
    await page.addScriptTag({ path: mermaidPath });
    await page.evaluate(async () => {
      mermaid.initialize({ startOnLoad: false, theme: 'neutral', securityLevel: 'loose',
        fontFamily: "'Liberation Sans', Arial, sans-serif",
        themeVariables: { fontSize: '17px' },
        flowchart: { htmlLabels: true, curve: 'basis', useMaxWidth: false, nodeSpacing: 30, rankSpacing: 60 },
        state: { useMaxWidth: false } });
      await mermaid.run();
    });
    await page.locator('#wrap').screenshot({ path: path.join(outDir, `${name}.png`) });
    await page.close();
    console.log('rendered', name);
  }
  await browser.close();
})();
