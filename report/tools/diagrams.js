// Renders the report's diagrams (block diagrams, state machines, flowcharts,
// sequence diagrams) to PNG with mermaid + headless Chromium.
//
//   node diagrams.js <out_dir>        (needs `npm i mermaid@10.9.3` next to it)
//
// Labels are English on purpose, like the course's own example reports.
// Every diagram mirrors what the code does (see the cited files); if the
// code changes, change the source below and re-run.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const path = require('path');
const fs = require('fs');

const outDir = process.argv[2] || '.';
fs.mkdirSync(outDir, { recursive: true });

const D = {};

D.fig1 = [{ src: `flowchart TB
  subgraph EDGE["Edge layer: 4 x ESP32"]
    direction LR
    U["Board 1<br/>Ultrasonic<br/>HC-SR04 + SW-420"]
    V["Board 2<br/>Vibration<br/>SW-420"]
    DK["Board 3<br/>Door + keypad<br/>MC-38, 3x4, buzzers"]
    CAM["Board 4<br/>ESP32-CAM<br/>OV2640"]
  end
  subgraph BE["Backend: Python / FastAPI"]
    direction LR
    SM["Alarm state machine<br/>+ event log"]
    FR["Face recognition<br/>YuNet + SFace + liveness"]
  end
  subgraph CL["Clients and services"]
    direction LR
    APP["Flutter app<br/>(Android)"]
    NTFY["ntfy<br/>push notifications"]
  end
  U -->|"events"| BE
  V -->|"events"| BE
  DK <-->|"state / commands"| BE
  CAM <-->|"frames / stream"| BE
  BE <-->|"REST + WebSocket<br/>via Tailscale"| APP
  BE -->|"alarm push"| NTFY
  NTFY -.-> APP` }];

D.fig8 = [{ src: `flowchart TB
  subgraph EDGE["Edge layer: ESP32 boards (C++)"]
    direction LR
    U["ultrasonic_sensor<br/>HC-SR04 + SW-420"]
    V["vibration_sensor<br/>SW-420, interrupt"]
    DK["door_keypad_controller<br/>reed, keypad, buzzers<br/>WebServer :80"]
    CAM["esp32_cam<br/>frames + MJPEG /stream"]
  end
  subgraph BE["Backend: FastAPI, port 8000"]
    direction LR
    R["main.py<br/>routes + auth"]
    ST["state.py<br/>AppState"]
    FS["face_recognition<br/>_service.py"]
    EC["esp32_client.py"]
    NT["notifier.py"]
    R --> ST
    R --> FS
    ST --> EC
    ST --> NT
  end
  subgraph CL["Clients and services"]
    direction LR
    APP["Flutter app"]
    NTFY["ntfy server"]
  end
  U -->|"sensor-event<br/>heartbeat"| BE
  V -->|"sensor-event<br/>heartbeat"| BE
  DK <-->|"state-change, entry-delay<br/>arm, disarm, trigger-alarm"| BE
  CAM <-->|"POST /camera/recognize<br/>GET /stream"| BE
  BE <-->|"REST (Bearer) +<br/>WebSocket /ws"| APP
  BE -->|"HTTP POST"| NTFY
  NTFY -.->|"push"| APP` }];

D.fig19 = [{ src: `stateDiagram-v2
  direction TB
  [*] --> disarmed
  disarmed --> transitional: arm (app / night mode)
  transitional --> armed: 3 s elapsed or controller reports armed
  transitional --> disarmed: disarm
  armed --> entryDelay: door opened (controller)
  armed --> triggered: sensor event, not bypassed
  entryDelay --> triggered: delay expired or watchdog (+5 s)
  entryDelay --> disarmed: PIN / app / face
  armed --> disarmed: PIN / app / face / night mode
  triggered --> disarmed: PIN / app / face
  note right of armed
    Home: vibration bypassed
    Away: nothing bypassed
  end note` }];
D.fig2 = D.fig19;

D.fig17 = [{ src: `stateDiagram-v2
  direction TB
  [*] --> DISARMED
  DISARMED --> PENDING_ARM: key *
  PENDING_ARM --> EXITING: door opened
  PENDING_ARM --> DISARMED: key * cancel
  EXITING --> ARMED: door closed
  EXITING --> DISARMED: key * cancel
  DISARMED --> REMOTE_ARMING: POST /arm
  REMOTE_ARMING --> ARMED: 3 s
  REMOTE_ARMING --> DISARMED: key * cancel
  ARMED --> ENTRY_DELAY: door opened
  ENTRY_DELAY --> DISARMED: PIN + #
  ENTRY_DELAY --> ALARM: 25 s elapsed
  ARMED --> ALARM: /trigger-alarm
  ALARM --> DISARMED: PIN + # or /disarm
  note right of ALARM
    /disarm and /trigger-alarm come
    from the backend (any state)
  end note` }];

D.fig9 = [
  { title: 'Scenario 1: intrusion through a perimeter sensor (system armed)', src: `sequenceDiagram
  autonumber
  participant S as Ultrasonic board
  participant B as Backend
  participant A as App
  participant K as Door board
  S->>B: POST /esp32/sensor-event
  B->>B: trigger_alert: state = triggered
  Note over B: push to ntfy (not awaited)
  B-->>A: broadcast (triggered)
  B->>K: POST /trigger-alarm
  K->>K: ALARM, buzzers sound` },
  { title: 'Scenario 2: arming from the app', src: `sequenceDiagram
  autonumber
  participant A as App
  participant B as Backend
  participant K as Door board
  A->>B: POST /arm
  B-->>A: broadcast (transitional)
  B->>K: POST /arm
  K->>K: REMOTE_ARMING, 3 s
  B->>B: after 3 s: armed
  K->>B: POST /esp32/state-change
  B-->>A: broadcast (armed)` },
  { title: 'Scenario 3: legitimate entry through the door', src: `sequenceDiagram
  autonumber
  participant K as Door board
  participant B as Backend
  participant A as App
  K->>K: door opened, ENTRY_DELAY 25 s
  K->>B: POST /esp32/entry-delay
  B-->>A: broadcast (entryDelay)
  alt correct PIN entered
    K->>B: POST /esp32/state-change (disarmed)
    B-->>A: broadcast (disarmed)
  else time expired
    K->>B: POST /esp32/sensor-event (door)
    B-->>A: broadcast (triggered)
  else board went silent
    B->>B: watchdog after 25 s + 5 s
    B-->>A: broadcast (triggered)
  end` },
];
D.fig9.split = true;

D.fig16 = [
  { title: 'Setup', src: `flowchart TB
  A(["Boot"]) --> B["Connect WiFi"]
  B --> C["Calibrate baseline:<br/>mean of 10 valid readings"]
  C --> D{"Baseline in<br/>100-400 cm?"}
  D -- yes --> F["Store baseline in NVS"]
  D -- no --> E["Keep stored value<br/>fault: baseline_rejected"]
  F --> G(["Main loop"])
  E --> G` },
  { title: 'Main loop (every 67 ms)', src: `flowchart TB
  G["Measure distance d"] --> I{"d > 0 ?"}
  I -- no --> J["Count empty readings<br/>fault no_echo at about 30 s"]
  I -- yes --> M{"d below<br/>baseline - 10 cm?"}
  M -- no --> N["breach counter = 0"]
  M -- yes --> O["breach counter++"]
  O --> P{"5 in a row and<br/>cooldown 5 s over?"}
  P -- yes --> R["POST sensor-event<br/>sensor=ultrasonic"]
  R --> X
  P -- no --> X
  J --> X
  N --> X
  X["Force sensor flag set?<br/>POST sensor=vibration"] --> Y["Heartbeat every 10 s"]
  Y --> G` },
];
D.fig16.row = true;

D.fig18 = [
  { title: 'Setup', src: `flowchart TB
  A(["Boot"]) --> B["Brownout detector off<br/>Connect WiFi"]
  B --> C["initCamera<br/>up to 5 attempts"]
  C --> D{"Camera ready?"}
  D -- no --> E["esp_restart"]
  D -- yes --> F["startCameraServer<br/>GET /stream, own task"]
  F --> G(["loop()"])` },
  { title: 'loop(): every 500 ms + backoff', src: `flowchart TB
  H["Capture JPEG frame"] --> I["POST /camera/recognize<br/>multipart, X-Device-Key"]
  I --> J{"HTTP 2xx?"}
  J -- yes --> K["Reset backoff<br/>and failure count"]
  J -- no --> L["Backoff x2, 500 ms to 30 s<br/>failure count++"]
  L --> M{"40 failures<br/>in a row?"}
  M -- yes --> E2["esp_restart"]
  M -- no --> H
  K --> H` },
];
D.fig18.row = true;

D.fig20 = [
  { title: 'Detection and matching', src: `flowchart TB
  A["Frame from ESP32-CAM"] --> B{"Face ID enabled?"}
  B -- no --> Z["matched: false"]
  B -- yes --> C["YuNet: largest face,<br/>score 0.9+, 5 landmarks"]
  C --> D["alignCrop 112x112<br/>SFace 128-d embedding"]
  D --> F["Cosine similarity vs<br/>enrolled embeddings"]
  F --> G{"best score<br/>0.363 or more?"}
  G -- no --> Z
  G -- yes --> H(["to liveness check"])` },
  { title: 'Liveness and decision', src: `flowchart TB
  H(["from matching"]) --> H2["Landmark motion window<br/>+ MiniFASNetV2 on wide crop"]
  H2 --> I{"Live? motion AND<br/>probability 0.6 or more"}
  I -- no --> J["Log: liveness failed<br/>(no disarm)"]
  I -- yes --> K{"Face-disarm on and<br/>armed / entryDelay / triggered?"}
  K -- yes --> L["Disarm system + door board<br/>log: Disarmed by Face ID"]
  K -- no --> M["Log: Face recognized<br/>(60 s per person)"]` },
];
D.fig20.row = true;

D.fig21 = [{ src: `flowchart TD
  subgraph UI["Presentation (screens and widgets)"]
    direction LR
    L["Login"]
    H["Home<br/>status, arm / disarm,<br/>sensors, countdown"]
    E["Event log"]
    F["Faces"]
    C["Camera<br/>MJPEG"]
    S["Settings"]
  end
  subgraph ST["State (Provider, lazy: false)"]
    direction LR
    AL["AlarmService<br/>ChangeNotifier"]
    AU["AuthService<br/>ChangeNotifier"]
  end
  subgraph SV["Service layer"]
    direction LR
    BS["BackendService<br/>(abstract interface)"]
    HB["HttpBackendService<br/>REST + WebSocket,<br/>reconnect every 3 s"]
    MB["MockBackendService<br/>(widget tests only)"]
    MJ["MjpegFrameParser /<br/>MjpegStreamClient"]
    NS["NotificationService<br/>(local notifications)"]
  end
  BE["Backend (FastAPI)"]
  UI --> ST
  ST --> BS
  BS --> HB
  BS -.-> MB
  C --> MJ
  AL --> NS
  HB <-->|"REST + WS /ws"| BE
  MJ -->|"GET /camera/stream"| BE` }];

(async () => {
  const browser = await chromium.launch();
  const mermaidPath = path.join(__dirname, 'node_modules/mermaid/dist/mermaid.min.js');
  for (const [name, parts] of Object.entries(D)) {
    if (name === 'fig2') continue; // alias of fig19, copied below
    const groups = parts.split ? parts.map((p, i) => ({ id: `${name}_${i + 1}`, parts: [p] })) : [{ id: name, parts }];
    for (const g of groups) {
      const page = await browser.newPage({ viewport: { width: 1500, height: 1000 }, deviceScaleFactor: 2 });
      const body = g.parts.map(p =>
        `<div class="part">${p.title ? `<h3>${p.title}</h3>` : ''}<pre class="mermaid">${p.src.replace(/</g, '&lt;').replace(/&lt;br\/>/g, '<br/>')}</pre></div>`
      ).join(parts.row ? '<div class="arrow">&#10132;</div>' : '');
      await page.setContent(`<html><head><style>
        body{background:#fff;margin:0;padding:16px;font-family:'Liberation Sans',Arial,sans-serif;display:inline-block}
        h3{font-size:19px;margin:6px 0 4px;color:#222}
        .part{margin-bottom:${parts.row ? 0 : 14}px}
        #wrap{${parts.row ? 'display:flex;align-items:center;gap:6px' : ''}}
        .arrow{font-size:34px;color:#555;padding:0 4px}
      </style></head><body><div id="wrap">${body}</div></body></html>`);
      await page.addScriptTag({ path: mermaidPath });
      await page.evaluate(async () => {
        mermaid.initialize({ startOnLoad: false, theme: 'neutral', securityLevel: 'loose',
          fontFamily: "'Liberation Sans', Arial, sans-serif",
          themeVariables: { fontSize: '17px' },
          flowchart: { htmlLabels: true, curve: 'basis', useMaxWidth: false, nodeSpacing: 40, rankSpacing: 50 },
          state: { useMaxWidth: false },
          sequence: { useMaxWidth: false, mirrorActors: false, messageMargin: 28, actorMargin: 40, messageFontSize: 15, noteFontSize: 15, actorFontSize: 15 } });
        await mermaid.run();
      });
      await page.locator('#wrap').screenshot({ path: path.join(outDir, `${g.id}.png`) });
      await page.close();
      console.log('rendered', g.id);
    }
  }
  await browser.close();
  fs.copyFileSync(path.join(outDir, 'fig19.png'), path.join(outDir, 'fig2.png'));
})();
