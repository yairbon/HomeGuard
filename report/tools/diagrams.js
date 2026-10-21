// Renders the report's diagrams (system block diagram, distance-sensor flowchart,
// alarm states) to PNG with mermaid + headless Chromium.
//
//   node diagrams.js <out_dir>        (needs `npm i mermaid@10.9.3` next to it)
//
// Labels are Hebrew, like the report. A number followed by its unit carries an
// LRM + no-break space so it keeps reading "10 cm" inside a right-to-left label.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const path = require('path');
const fs = require('fs');

const outDir = process.argv[2] || '.';
fs.mkdirSync(outDir, { recursive: true });

const U = '‎ '; // between a number and its unit
const D = {};

// Figure 1: the whole system
D.fig1 = `flowchart LR
  subgraph B["שלושה בקרי ESP32"]
    direction TB
    S["בקר חיישנים<br/>חיישן מרחק + חיישן רעידות"]
    K["בקר דלת<br/>חיישן דלת, קודן, זמזמים"]
    C["בקר מצלמה<br/>ESP32-CAM"]
  end
  BE["בקנד (Python)<br/>מצב אזעקה, זיהוי פנים"]
  P["אפליקציה בטלפון"]
  M["אימייל<br/>עם תמונה מהמצלמה"]
  S -->|"אירועים"| BE
  K <-->|"אירועים ופקודות"| BE
  C -->|"תמונות ווידאו"| BE
  BE <-->|"WiFi: פקודות, עדכונים, וידאו"| P
  BE --> M`;

// Figure 2: how the distance sensor decides to report an intrusion
// (at power-up the sensor first measures the "normal" distance; see the caption)
D.fig2 = `flowchart LR
  B["מדידה<br/>(כ-15${U}Hz)"] --> C{"קצר מהרגיל<br/>ביותר מ-10${U}cm"}
  C -->|"לא"| D["המונה<br/>מתאפס"]
  C -->|"כן"| F{"5 קריאות<br/>כאלה ברצף"}
  F -->|"כן"| G{"עברו 5${U}s<br/>מהדיווח<br/>האחרון"}
  G -->|"כן"| H["דיווח<br/>חדירה<br/>לבקנד"]
  D --> X(("חזרה<br/>למדידה"))
  F -->|"לא"| X
  G -->|"לא"| X
  H --> X`;

// Figure 4: alarm states (the same five the app shows)
D.fig4 = `stateDiagram-v2
  direction LR
  [*] --> Disarmed
  Disarmed: מנוטרלת
  Arming: בדריכה
  Armed: דרוכה
  EntryDelay: ספירת כניסה
  Alarm: אזעקה
  Disarmed --> Arming: קודן או אפליקציה
  Arming --> Armed: דלת נסגרה<br/>או 3${U}s באפליקציה
  Arming --> Disarmed: ביטול
  Armed --> EntryDelay: דלת נפתחה
  Armed --> Alarm: חיישן הופעל
  Armed --> Disarmed: אפליקציה<br/>או פנים
  EntryDelay --> Alarm: 30${U}s
  EntryDelay --> Disarmed: קוד, אפליקציה<br/>או פנים
  Alarm --> Disarmed: קוד, אפליקציה<br/>או פנים`;

(async () => {
  const browser = await chromium.launch();
  const mermaidPath = path.join(__dirname, 'node_modules/mermaid/dist/mermaid.min.js');
  for (const [name, src] of Object.entries(D)) {
    const page = await browser.newPage({ viewport: { width: 1500, height: 900 }, deviceScaleFactor: 2 });
    await page.setContent(`<html><head><style>
      body{background:#fff;margin:0;padding:12px;font-family:'Liberation Sans',Arial,sans-serif;display:inline-block}
      .nodeLabel,.edgeLabel,.label,.edgeLabel p,.nodeLabel p{unicode-bidi:plaintext;text-align:center}
    </style></head><body><div id="wrap"><pre class="mermaid">${src.replace(/</g, '&lt;').replace(/&lt;br\/>/g, '<br/>')}</pre></div></body></html>`);
    await page.addScriptTag({ path: mermaidPath });
    await page.evaluate(async () => {
      mermaid.initialize({ startOnLoad: false, theme: 'neutral', securityLevel: 'loose',
        fontFamily: "'Liberation Sans', Arial, sans-serif",
        themeVariables: { fontSize: '17px' },
        flowchart: { htmlLabels: true, curve: 'basis', useMaxWidth: false, nodeSpacing: 30, rankSpacing: 45 },
        state: { useMaxWidth: false } });
      await mermaid.run();
    });
    await page.locator('#wrap').screenshot({ path: path.join(outDir, `${name}.png`) });
    await page.close();
    console.log('rendered', name);
  }
  await browser.close();
})();
