# HomeGuard — הנחיות לעבודה בפרויקט

מערכת אבטחה היברידית: ESP32 (C++) + בקנד Python (FastAPI, זיהוי פנים) +
אפליקציית Flutter (Android). פירוט מלא ב-[README.md](README.md).

## סביבה — קריטי, תמיד תזכור את זה

הפרויקט חי ב-`C:\dev\HomeGuard` בכוונה (לא תחת `C:\Users\<username>`), כי
נתיב עם תווים לא-ASCII שבר כלי Android/Gradle. **אל תעביר קבצים בחזרה
לתיקיית המשתמש.**

**בניית Android/Gradle קורסת נייטיבית על המכונה הזו** — תקלה ברמת המערכת
(Winsock catalog: AF_UNIX רשום ל-SOCK_DGRAM בלבד, אין רישום SOCK_STREAM),
שגורמת ל-`java.io.IOException: Unable to establish loopback connection`
בכל JDK 17 (נבדק: ojdkbuild, Temurin, אפילו ה-GA המקורי). לא נפתר על ידי
`netsh winsock reset`, `sfc /scannow`, או `DISM /RestoreHealth` + restart —
כל אלה נוסו. **הפתרון שעובד בפועל: לבנות דרך WSL2** (Ubuntu, שם AF_UNIX
תקין). זה כבר מוגדר ועובד — `flutter-dev` ו-`/flutter-build` יודעים את זה.

בנייה (מה-Bash/PowerShell tool):
```bash
wsl -d Ubuntu -- bash -c '
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
export ANDROID_HOME=/opt/android-sdk
export PATH="/opt/flutter/bin:/opt/android-sdk/platform-tools:/opt/android-sdk/cmdline-tools/latest/bin:$PATH"
cd /mnt/c/dev/HomeGuard/app
flutter build apk --debug
'
```
עדיף פשוט `/flutter-build` (custom command, `.claude/commands/`) שכבר עוטף
את זה. APK בנוי נמצא ב-`app\build\app\outputs\flutter-apk\app-debug.apk`.
התקנה על מכשיר מחובר: adb של Windows (`C:\dev\android-sdk\platform-tools\adb.exe`),
לא ה-adb בתוך WSL2 (המכשיר לא נראה משם).

**מלכודת:** אחרי ערבוב build נייטיבי-Windows ו-WSL2 על אותה תיקיית פרויקט,
עלולות להופיע שגיאות Dart מוזרות ("method X isn't defined" בשרשרת) שהן
בעצם build cache מקולקל, לא קוד שבור — `flutter clean && flutter pub get`
פותר את זה.

**מלכודת desugaring:** `flutter_local_notifications` דורש core library
desugaring מופעל ב-Gradle (`android/app/build.gradle.kts`), אחרת ה-build
נכשל ב-`:app:checkDebugAarMetadata` עם השגיאה "requires core library
desugaring to be enabled". זה כבר מתוקן — `compileOptions {
isCoreLibraryDesugaringEnabled = true }` + `dependencies {
coreLibraryDesugaring("com.android.tools:desugar_jdk_libs:2.1.4") }`. זה
כנראה היה ה-build הראשון שבאמת הגיע עד Gradle עד עכשיו (כל הניסיונות
הקודמים נכשלו קודם, ב-AF_UNIX או ב-package_config מעורבב), אז זו הפעם
הראשונה שהתקלה הזו בכלל נחשפה.

**הרצה על מכשיר פיזי אמיתי (לא רק build — קישוריות בזמן ריצה):**
כתובת ה-backend כבר לא הארד-קודד — `BackendConfig` (`app/lib/services/
backend_config.dart`) שומר אותה ב-SharedPreferences, נטען לפני `runApp`
ב-`main.dart`, ונערך במסך Settings ("Backend connection"). ברירת המחדל
עדיין `http://localhost:8000`, וכל שינוי דורש **restart של האפליקציה**
(ה-`baseUrl` על `HttpBackendService` הוא `final` — אין live-reconnect
בכוונה, ראה את הקומנט ב-`_saveBackendUrl`). `localhost` עובד
לאמולטור/desktop/web (רצים על אותה מכונה כמו ה-backend), אבל **לא**
לטלפון פיזי, כי `localhost` שם מתייחס לטלפון עצמו. שלוש גישות אפשריות:
1. **IP אמיתי ברשת ה-WiFi המקומית** (במקום `localhost` ב-`main.dart`,
   נמצא עם `Get-NetIPAddress`/`ipconfig`) — דורש גם firewall rule
   (`New-NetFirewallRule -DisplayName "HomeGuard backend (dev)" -Direction
   Inbound -Protocol TCP -LocalPort 8000 -Action Allow`, **המשתמש חייב
   להריץ בעצמו ב-PowerShell מורם — לא פעולה שסוכן יכול/צריך לבצע**), וגם
   שהטלפון וה-PC יהיו על אותה רשת פיזית ללא client isolation. **נכשל בפועל
   על הרשת של המשתמש** — נבדק עם `curl`/`nc` ישירות מהטלפון דרך `adb
   shell` (הדרך הנכונה לאבחן, לא ping — Windows חוסם ICMP כברירת מחדל
   בפרופיל Public גם כשיש allow rule ל-TCP, אז ping נכשל גם כשה-TCP היה
   עובד) — ה-TCP connect עצמו קרס בטיים-אאוט מוחלט (לא "connection
   refused" מהיר), מה שמצביע על client isolation ברמת הראוטר או שכבת
   firewall נוספת שלא Windows Defender.
2. **`adb reverse tcp:8000 tcp:8000`** (מנהרת USB, `localhost:8000`
   בטלפון מתועל בפועל ל-`localhost:8000` על ה-PC) — זו הגישה שעבדה בפועל.
   **קריטי:** להריץ את `adb reverse` **לפני** ההתקנה/הרצה של האפליקציה,
   לא אחרי — אם האפליקציה כבר רצה כשה-tunnel מוקם, ניסיון ה-WebSocket
   הראשון שלה (ב-`HttpBackendService` constructor) נכשל לפני שה-tunnel
   קיים, ולמרות שיש reconnect loop (כל 3 שניות), חוסר-הסנכרון הזה גרם
   לבלבול אמיתי (פקודות arm/disarm הצליחו ב-REST, אבל המסך לא השתקף בזמן
   אמת). גם: חיבור ה-USB לפעמים נופל (transport_id משתנה) — `adb reverse
   --list` בודק אם המנהרה עדיין קיימת, ואפשר להריץ שוב בבטחה (אידמפוטנטי).
3. **Tailscale** (VPN רשתי פרטי, WireGuard) — נבחר כדי לפתור את הבעיה
   ששתי הגישות למעלה לא פותרות: "עובד מכל רשת, לא רק USB/LAN מקומי".
   המחשב והטלפון מתחברים שניהם **החוצה** לשרתי Tailscale (בלי לפתוח אף
   port לאינטרנט הפתוח — חשוב במיוחד כאן כי זו מערכת אזעקה עם auth חלש,
   סיסמה משותפת אחת + lockout של 30 שניות בלבד; לא ראוי לחשוף את
   ה-backend לאינטרנט הרחב). זה אותו עיקרון שחברות כמו Ring/Nest
   משתמשות בו (המכשיר הביתי יוזם חיבור יוצא, לא מקשיב לנכנס), רק בקנה
   מידה אישי דרך שירות מוכן במקום ענן עצמאי. **מותקן על המחשב הזה**
   (`C:\Program Files\Tailscale`, שירות רץ), אבל **לא מחובר לחשבון** —
   `tailscale status` יחזיר "Logged out" עד שמישהו יפתח את ה-URL
   שההתקנה נתנה ויתחבר. זו התחברות לחשבון (Google/Microsoft/GitHub SSO
   או מייל) — **לא פעולה שסוכן יכול/צריך לבצע**, בדיוק כמו ה-firewall
   rule למעלה. אחרי חיבור: להתקין Tailscale גם בטלפון, להריץ `tailscale
   ip -4` במחשב כדי לקבל את ה-IP הפרטי שלו (בטווח `100.x.x.x`), ולהזין
   אותו (עם `http://` ו-`:8000`) בשדה "Backend URL" במסך Settings של
   האפליקציה.

שחזור סביבה במחשב חדש: אם Windows בריא (לא עם התקלה הזו) — נסה קודם
`.\scripts\setup-windows.ps1`. אם נתקלים באותה תקלה — `scripts\setup-wsl.sh`
(רץ בתוך WSL2, `wsl -d Ubuntu -- bash /mnt/c/dev/HomeGuard/scripts/setup-wsl.sh`).

## כלי Claude Code — plugins וסקילים

`.claude/settings.json` (מקומם ב-git) מצהיר על שלושה marketplaces ושלושה
plugins ב-**project scope**, כלומר כל סשן שנפתח על HomeGuard מתקין ומפעיל
אותם אוטומטית — כולל קונטיינרים ארעיים בענן, שמתחילים כל פעם מ-`$HOME` נקי:

| Plugin | מקור | מה זה נותן |
| --- | --- | --- |
| `claude-mem` | `thedotmack/claude-mem` | זיכרון מתמשך בין סשנים; 19 סקילים + hooks (SessionStart/PostToolUse/Stop) + MCP server |
| `headroom` | `headroomlabs-ai/headroom` | hooks בלבד (SessionStart, PreToolUse), 0 עלות context |
| `claude-code-setup` | `anthropics/claude-plugins-official` | סקיל שממליץ על אוטומציות (hooks/סקילים/MCP) לפי הקודבייס |

עלות ה-context הקבועה של שלושתם יחד היא ~1.9k טוקנים לסשן. שים לב ש-
`claude-mem` ו-`headroom` מריצים hooks — כלומר קוד צד-שלישי רץ בכל תחילת
סשן ובכל קריאת כלי. זו החלטה מודעת, לא תופעת לוואי.

ה-marketplace הרשמי נוסף עם `--sparse` (רק `.claude-plugin` ו-
`plugins/claude-code-setup`) כדי לא לשכפל מונורפו של 278 plugins.

**סקיל רביעי, `task-observer`, לא מותקן כ-plugin** אלא vendored ב-
`.claude/skills/task-observer/` — הריפו שלו
(`rebelytics/one-skill-to-rule-them-all`) מפרסם `SKILL.md` חשוף בלי
`marketplace.json`, אז אין מה להתקין. פרטים ואיך לעדכן:
`.claude/skills/task-observer/PROVENANCE.md`.

להתקנת אותו סט ב-**כל ריפו אחר** (user scope, לא רק HomeGuard):
`scripts/setup-claude-plugins.sh` (WSL2/Linux/macOS) או
`scripts/setup-claude-plugins.ps1` (Windows). פעם אחת לכל מכונה.

**OmniRoute לא נכלל** — למרות שהוא מופיע ברשימה, `diegosouzapw/OmniRoute`
הוא אפליקציית LLM-router ב-TypeScript, לא plugin של Claude Code: אין בו
`.claude-plugin/` בשום מקום בעץ. ה-`skills/` שלו הם 42 מסמכי SKILL.md
שמתעדים את ה-REST API של שרת OmniRoute רץ — לא רלוונטיים ל-HomeGuard,
ולא ניתנים להתקנה דרך `claude plugin install`.

## מבנה וקונבנציות

- `esp32/` — **שלושה בקרים נפרדים**, לא אחד מרכזי (עדכון מהתכנון המקורי —
  כל חיישן על ESP32 משלו). בנוי, מתועד, נבדק שמתקמפל. ראה `esp32/README.md`.
- `python/` — בקנד FastAPI (בנוי ונבדק, ראה למטה) שמאחד את זיהוי הפנים +
  תקשורת ה-ESP32 + משרת את האפליקציה.
- `app/` — Flutter, Android בלבד (native, לא web/PWA). שם החבילה
  `com.homeguard.homeguard_app`. Push notifications להתראות אבטחה,
  סיסמה משותפת אחת להתחברות (לא multi-user).

## עיצוב UI (Flutter)

- לא רוצים מראה "AI-generated גנרי" — Material 3, אבל עם ניגודיות
  ופלטת צבעים מכוונת (לא ברירת המחדל הסגולה של Flutter). מצב אזעקה
  צריך שפה ויזואלית ברורה (אדום/דרוך מול ירוק/מנוטרל).
- עדיפות לרספונסיביות בסיסית (טלפון בלבד בשלב זה, לא טאבלט/דסקטופ).

## Must-have (מהמסמך המקורי)

זיהוי חדירה (לייזר/דלת) או פריצה בכוח; נטרול דרך קודן פיזי; אזעקה קולית.
Nice-to-have: אימות ביומטרי, דאשבורד לוג בזמן אמת, שינוי סיסמה מהאפליקציה.

## מצב האפליקציה (app/)

בנוי: מסך login (עם ניעילה חכמה אחרי 3 ניסיונות שגויים — כמו הקודן הפיזי),
דאשבורד (AlarmStatusIndicator + מצבי דריכה Home/Away + פאנל חיישנים לפי
חיישן ספציפי + סטטוס חיבור ESP32), מסך event log עם פילטר לפי חומרה, מסך
settings עם שינוי סיסמה + night mode + איש קשר לחירום + כתובת backend
(ראה הסעיף על Tailscale למעלה), מסך faces, מסך
camera. **מדבר בפועל עם ה-backend האמיתי** — `HttpBackendService`
(`services/http_backend_service.dart`) הוא ה-`BackendService` שנטען
כברירת מחדל ב-`main.dart` (REST + WebSocket ל-`/ws`), נבדק end-to-end
(ראה `python/simulate.py`, 51/51 עברו כולל אחרי restart של הבקנד).
`MockBackendService` נשאר קיים רק בשביל widget tests (אין שרת אמיתי
בזמן `flutter test`) ועבודת UI אופליין — הוא לא נטען יותר ב-`main.dart`.

**מלכודת lockout:** הבקנד הוא הסמכות היחידה ל-lockout (client-side lockout
ניתן לעקיפה בקלות — רק להפעיל מחדש את האפליקציה). `AuthService` הוא רק
retransmitter דק שמשקף את מה ש-`login()` מחזיר, לא סופר ניסיונות בעצמו
יותר.

**מלכודת Provider:** `AlarmService`/`AuthService` חייבים `lazy: false`
ב-`main.dart`. הם נרשמים ל-broadcast streams של ה-backend בקונסטרקטור, וללא
`lazy: false` הם לא נבנים עד שמשהו קורא להם לראשונה — כלומר אירועים
שנפלטים לפני זה (למשל ה-login הראשון) פשוט אובדים כי אין עדיין מאזין
(broadcast streams לא משדרים מחדש לאירועים שכבר קרו). זה גרם לבאג ש"דברים
מופיעים רק אחרי דריכה ראשונה" — אם משהו דומה קורה שוב, תבדוק קודם `lazy`.

**התראות:** יש התראות מקומיות (`flutter_local_notifications`) שיורות
כשהאזעקה מופעלת. **זו לא push אמיתי** — עובד רק כשהאפליקציה חיה
(foreground/background), לא כשהיא סגורה לגמרי. Push אמיתי (Firebase Cloud
Messaging) דורש שהמשתמש ייצור פרויקט Firebase בעצמו (לא יכול ליצור חשבון
בשבילו) ויוריד `google-services.json` — בלי הקובץ הזה, הוספת plugin ה-
Gradle של Firebase תשבור את ה-build לגמרי. אל תוסיף firebase_core/
firebase_messaging בלי לוודא שהקובץ קיים.

## מצב הבקנד (python/)

FastAPI, בנוי ונבדק end-to-end (ראה `python/README.md` לפירוט מלא, ו-
`python/simulate.py` — סקריפט סימולציה חי, לא unit tests עם mocking, שרץ
נגד שרת אמיתי ובודק כל endpoint כולל lockout מלא עם המתנה אמיתית של 30
שניות. 51/51 עברו, גם אחרי restart של הבקנד לוודא שזה לא בודק bytecode
ישן): login+lockout, arm/disarm עם עיכוב יציאה, alerts לפי חיישן ספציפי,
faces CRUD, WebSocket `/ws` ששולח את כל ה-state בזמן אמת, וזיהוי פנים
עובד (OpenCV — Haar cascade + LBPH, **לא** dlib/`face_recognition`
בכוונה, כדי להימנע מ-build נייטיבי עם CMake/Visual Studio על המכונה הזו
שכבר סבלה מספיק מבעיות toolchain). `backend/models.py` משקף 1:1 את
`app/lib/services/backend_service.dart` — אם משנים שדה בצד אחד, לשנות
גם בשני.

**פערים ידועים, לא תוקנו בכוונה** (נמצאו תוך כדי pass תיעוד מקיף, ראה
`python/README.md` לפירוט): `/arm`/`/disarm` מעדכנים state מקומי ומפקדים
על החומרה לא-אטומית; `report_state_from_esp32` לא יכול להחיל מחדש את ה-
bypass של Home mode כי הוא לא מקבל את ה-mode; הודעת הלוג של
`set_night_mode` מתארת auto-arm שלא ממומש בפועל.

הרצה: `python\venv\Scripts\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8000`
מתוך `python/`.

## מצב הקושחה (esp32/)

**שלושה בקרי ESP32 נפרדים, לא אחד** (גיא ויאיר סיפקו 3 קבצי .ino נפרדים —
לוח לכל חיישן, לא בקר מרכזי אחד כמו שתוכנן במקור). בנוי, מתועד בהרחבה,
**נבדק שמתקמפל** (arduino-cli + esp32:esp32 core, ראה `esp32/README.md`):

1. `ultrasonic_sensor/` — HC-SR04. חיישן "טיפש": מדווח פריצה, לא יודע אם
   דרוך. תוקן: URL/port/method שגויים בטיוטה המקורית (הצביע על endpoint
   שלא קיים).
2. `vibration_sensor/` — חיישן דיגיטלי. הומר מ-WebServer (polling) ל-WiFi
   client (push), כדי להתאים לדפוס של שאר הלוחות. הוסרה הערה לא ראויה
   מהטיוטה המקורית.
3. `door_keypad_controller/` — הסמכות היחידה לדריכה/נטרול (יש לו את
   הקודן). הוא היחיד שגם שולח וגם מקבל — מריץ WebServer קטן עם `/arm`
   ו-`/disarm` כדי שהאפליקציה תוכל לפקוד עליו מרחוק. הקודן עדיין מדומה
   דרך Serial Monitor (בכוונה, לבדיקת ה-state machine לפני חיווט הקודן
   הפיזי 4x4).

**חיישן שם:** ה"לייזר" מהמסמך המקורי הוא בפועל אולטרה-סוני (HC-SR04) —
שונה השם בכל המערכת (`SensorType.laser` → `ultrasonic`, Python+Flutter+
esp32) כדי לשקף את החומרה האמיתית.

**באג אמיתי שנתפס תוך כדי:** `trigger_alert`/`triggerAlert` (גם ב-Python
וגם ב-Dart) לא בדקו את ה-`bypassed` flag לפני שהסלימו לאזעקה מלאה — כלומר
מצב Home (שאמור להתעלם מחיישן הרעידות) לא באמת עבד. תוקן בשני הצדדים,
ויש regression test על זה ב-`python/simulate.py` (section "Home mode
sensor bypass") שממשיך לעבור.

**עדיין צריך:** WiFi credentials ו-`backendHost`/`HOMEGUARD_DOOR_CONTROLLER_URL`
הם placeholders (`192.168.X.X`) — למלא עם הרשת/IP האמיתיים לפני הבהוב.
צריך גם לחבר בפועל את חומרה — לא נבדק על לוחות אמיתיים, רק קומפילציה.
