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

**מלכודת פונטים:** הפונטים (IBM Plex Sans/Mono, Big Shoulders Display)
מצורפים כ-assets ב-`app/google_fonts/`. `google_fonts` מוריד מהרשת בזמן ריצה
כברירת מחדל — מה שלא מתאים לאפליקציית אזעקה שצריכה להיקרא בלי אינטרנט, ובפרט
כשכל תכנון הגישה מרחוק (Tailscale / `adb reverse`) מגיע לבקנד בלי route
כללי לאינטרנט. **שמות הקבצים נושאים משמעות** — ההתאמה היא
`<Family>-<Weight>.ttf` (ראה `_findFamilyWithVariantAssetPath` בחבילה), אז
שינוי שם מחזיר בשקט את אותו face להורדה מהרשת.

**מלכודת שגיאות רשת:** `HttpBackendService._post/_get` מחזירים `_Result` עם
`reachable` — קודם הם בלעו כל שגיאה והחזירו `{}`, כך ש"הבקנד לא נגיש" נראה
זהה ל"הבקנד אמר לא". במערכת אזעקה זה קונקרטי: לחיצה על DISARM בלי route
לבקנד נראתה בדיוק כמו נטרול שהצליח. כשלים מגיעים ל-`AlarmService.lastError`
ומוצגים כ-SnackBar ב-`MainShell`.

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

## אבטחה — קרא לפני שנוגעים ב-API

כל endpoint מוגן. שני סוגי אישורים, כי יש שני סוגי קוראים:

- **האפליקציה** — `/login` מחזיר session token (`secrets.token_urlsafe`, תקף
  12 שעות), שנשלח כ-`Authorization: Bearer`. הסיסמה נשמרת כ-hash (scrypt +
  salt) ומושווית ב-`compare_digest`. שינוי סיסמה מבטל את כל שאר הסשנים.
- **הלוחות** — מפתח משותף ב-`X-Device-Key` (או `?key=`), כי לוח לא יכול
  לעשות login אינטראקטיבי. נוצר אוטומטית בהרצה ראשונה ונשמר ב-`state.json`,
  או נקבע דרך `HOMEGUARD_DEVICE_KEY`.

`/health` הוא היחיד הפתוח. ה-WebSocket מקבל טוקן ב-`?token=` (ל-WebSocket אין
דרך ניידת להוסיף כותרות) ונסגר עם 1008 **לפני** `accept()`.

**אם מוסיפים route חדש — הוא חייב `Depends(require_app)` או
`Depends(require_device)`.** יש בדיקה פרמטרית ב-`tests/test_auth.py` עם טבלת
כל ה-routes; הוסיפו אליה שורה, אחרת אין מי שיתפוס route ששכחו להגן עליו.

**סודות לא בקוד:** `esp32/secrets.h` (gitignored, יש `.example`) מחזיק WiFi,
כתובת בקנד, מפתח מכשיר וקוד קודן. קודם הם היו literals בשלושת ה-`.ino`.
הערכים הישנים עדיין בהיסטוריית הגיט — לא נוקו בכוונה (החלטת המשתמש; `free-tau`
היא רשת ציבורית של האוניברסיטה, לא סוד אמיתי).

## התראות, מצלמה, תזמון — מה שנוסף אחרי סבב האבטחה

**ntfy** (`backend/notifier.py`) — התראות שמגיעות גם כשהאפליקציה סגורה, מה
ש-`flutter_local_notifications` לא יכול לעשות. נבחר על פני FCM כי FCM דורש
פרויקט Firebase ו-`google-services.json` (ובלעדיו ה-build של אנדרואיד נשבר
לגמרי). **`notify()` הוא לא coroutine בכוונה** — הוא רק מתזמן task. אם תעשה
לו `await` בתוך `trigger_alert`, תעצור את ה-broadcast לכל ה-WebSockets עד
שרת ה-ntfy יענה. יש בדיקה שנועלת את זה (`test_notify_never_blocks`).
**שם ה-topic הוא סוד** — לא נכנס ללוג האירועים, יש בדיקה גם על זה.

**`camera_worker.py`** — תהליך נפרד (לא thread בתוך הבקנד: `VideoCapture.read`
חוסם, והבקנד הוא event loop יחיד שמשרת את כל ה-WebSockets). מזדהה עם ה-device
key. בלעדיו זיהוי הפנים היה מנוע שאף אחד לא הזין.

**נטרול לפי פנים** — כבוי כברירת מחדל, opt-in ממסך המצלמה. **ל-SFace אין
liveness detection** — תמונה מודפסת עשויה לעבור. זו נוחות, לא אבטחה, וגם ה-UI
אומר את זה. כל נטרול כזה מיוחס בלוג.

**`AlarmState.entryDelay`** — מצב חדש. בקר הדלת מדווח (`/esp32/entry-delay`)
כשהוא מתחיל את ספירת הנטרול, כך שהאפליקציה מציגה ספירה לאחור. קודם הלוח הריץ
את החלון הזה בשקט ודיווח רק על התוצאה, אז האפליקציה קפצה מ-ARMED ישר
ל-TRIGGERED. `trigger_alert` מסלים גם מ-`entryDelay`, לא רק מ-`armed`.

**night mode** — יש scheduler אמיתי עכשיו (`_night_mode_tick`), לא רק דגל.
**הוא לעולם לא משתיק אזעקה חיה** — דורך רק מ-`disarmed` ומנטרל רק מ-`armed`,
אף פעם לא מ-`triggered`/`entryDelay`. פועל רק על השעה העגולה, כדי שנטרול
מכוון ב-23:30 לא יבוטל דקה אחרי.

**חיוג חירום** — לא ממומש ולא ימומש בלי ספק טלפוניה. במקומו: אם האזעקה לא
נוקתה תוך `police_call_delay_seconds`, נשלחת התראת הסלמה דחופה שאומרת למי
להתקשר. הטקסט ב-UI תוקן בהתאם (קודם הבטיח "auto-called").

## מצב הבקנד (python/)

FastAPI, בנוי ונבדק. פירוט מלא ב-`python/README.md`. **שתי חבילות בדיקות
בכוונה:**

- `pytest` (`python/tests/`) — 136 בדיקות in-process מול ה-ASGI app דרך
  `httpx.ASGITransport`. בלי שרת, בלי פורטים, ובלי המתנות אמיתיות (פקיעת
  lockout ו-heartbeat מיושן נבדקים בהזזת timestamps). כל בדיקה מקבלת התקנה
  נקייה דרך reload של מודול ה-state לתיקייה זמנית. זה מה שמריצים תוך כדי
  עבודה.
- `simulate.py` — 96 בדיקות מול uvicorn אמיתי דרך סוקט אמיתי, כולל המתנה
  אמיתית של 30 שניות ל-lockout ועוד 35 ל-timeout של לוח. איטי, אבל בודק גם
  את שרת ה-ASGI ואת ה-WebSocket. מריצים לפני שמאמינים שזה עובד.

גם `ruff` (config ב-`pyproject.toml`). הכול רץ ב-CI (`.github/workflows/ci.yml`).

**persistence:** `state.json` (gitignored) שומר hash סיסמה, מפתח מכשיר, פנים,
הגדרות ולוג אירועים — כתיבה אטומית (temp + `os.replace`). **`alarm_state` לא
נשמר בכוונה** — בקנד שהופעל מחדש לא באמת יודע אם הבית דרוך; לעלות `disarmed`
ולתת לדיווח הבא של בקר הדלת לקבוע זה מתקן את עצמו, בעוד ש-`armed` ישן היה גורם
לאפליקציה להציג מצב שאף אחד לא אימת.

**liveness של לוחות:** כל לוח שולח `/esp32/heartbeat?board=<name>` כל 10 שניות;
לוח נחשב offline אחרי 30 שניות שקט, ו-`/state` מחזיר מערך `boards`. זה החליף
דגל `esp32_online` בודד ש**ננעל על true** — אחרי heartbeat אחד שום דבר לא החזיר
אותו, אז לוח יכול היה למות והאפליקציה המשיכה להראות ONLINE.

**זיהוי פנים:** YuNet (detection) + SFace (recognition), שני קבצי ONNX
ב-`python/models/`, שניהם נטענים על ידי `opencv-contrib-python` שכבר היה
בפרויקט — בלי dependency חדש ובלי build נייטיבי. זה החליף Haar cascade + LBPH.
**אל תחליף ל-`insightface` או ל-`face_recognition`** — שניהם דורשים
MSVC/CMake על Windows, בדיוק הכאב שהפרויקט הזה כבר איבד עליו ימים. אם קבצי
ה-ONNX חסרים, השירות נופל אוטומטית חזרה ל-Haar+LBPH עם אזהרה
(`FaceRecognitionService.backend_name` אומר מי פעיל).

`backend/models.py` משקף 1:1 את `app/lib/services/backend_service.dart` — אם
משנים שדה בצד אחד, לשנות גם בשני. יש בדיקה
(`test_state_shape_matches_the_flutter_client`) שנועלת את צורת ה-wire.

**פערים ידועים, לא ממומשים בכוונה:** `/arm`/`/disarm` לא אטומיים מול החומרה
(אבל התגובה מחזירה `hardware: bool` והאפליקציה מציגה את זה); דריכה מהקודן
הפיזי לא נושאת mode, אז נופלים למצב האחרון שהאפליקציה ביקשה, בברירת מחדל Away
(המחמיר); `emergency_number`/`police_call_delay_seconds` נשמרים אבל אף אחד לא
מחייג; `night_mode` נשמר אבל אין scheduler.

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
