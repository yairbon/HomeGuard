// ============================================================
// HomeGuard — לוח 1 מתוך 4: חיישן אולטרה-סוני (HC-SR04)
// תפקיד: לזהות כניסה שלא מהדלת הראשית, ע"י מדידת מרחק לקיר/רצפה
// ממול ובדיקה אם המרחק "נסגר" (מישהו עומד בדרך).
//
// לוח זה הוא "טיפש" בכוונה: הוא לא יודע אם המערכת דרוכה או לא.
// הוא פשוט מדווח לבקנד כל אימות פריצה, וה-Backend (state.py,
// trigger_alert) מחליט אם זה משנה — הוא מתעלם מהדיווח אם המערכת
// לא דרוכה. ככה הלוח הזה לא צריך לדעת שום דבר על שאר המערכת.
//
// בכל אתחול (reset/power-up) הלוח מכייל את עצמו: לוקח את 10 הקריאות
// התקינות הראשונות מהחיישן וממוצע אותן כבסיס (DEFAULT_DISTANCE), במקום
// להניח 200 ס"מ קבוע. ככה מיקום התקנה סופי ששונה מעט מהתכנון המקורי
// (למשל 150 ס"מ מקיר, לא 200) לא דורש שינוי קוד — רק אתחול מחדש
// כשהחיישן כבר במקומו הסופי.
//
// לכיול הזה יש עכשיו בדיקת שפיות, כי קודם הוא קיבל כל ערך שמדד: ערך
// שלא נראה כמו התקנה אמיתית נדחה, נופלים חזרה לבסיס האחרון שנשמר
// ב-NVS (או לברירת המחדל המהודרת), ומדווחים על כך לבקנד כתקלה — במקום
// להישאר עיוורים בשקט בזמן שה-heartbeat ממשיך לדווח שהלוח בריא.
// ============================================================

#include <WiFi.h>
#include <HTTPClient.h>
#include <Preferences.h>

// כל הסודות (SSID, סיסמה, כתובת הבקנד, מפתח המכשיר) יושבים ב-secrets.h,
// שלא נכנס לגיט. העתק את esp32/secrets.h.example לתיקייה הזו בשם secrets.h
// ומלא את הערכים. קודם הם היו כתובים כאן ישירות — כלומר נשמרו בהיסטוריית
// הגיט בטקסט גלוי.
#include "secrets.h"

// --- הגדרות רשת ---
const char* ssid = WIFI_SSID;
const char* password = WIFI_PASSWORD;

// כתובת שרת הפייתון (הבקנד) — ראה secrets.h.
const char* backendHost = BACKEND_HOST;
const int backendPort = BACKEND_PORT;

// שם הלוח הזה, כפי שהבקנד מזהה אותו. הבקנד עוקב אחרי כל לוח בנפרד
// (ראה BOARD_NAMES ב-python/backend/state.py) כדי שהאפליקציה תוכל להראות
// איזה לוח בדיוק מת, במקום דגל אחד משותף לשלושתם.
const char* BOARD_NAME = "ultrasonic";

// --- הגדרות פינים לחיישן האולטרה-סוני (HC-SR04) ---
const int trigPin = 14;
const int echoPin = 12;

// --- הגדרות לוגיקת זיהוי ---
// לא const יותר: 200 הוא רק ברירת מחדל למקרה שהכיול באתחול נכשל
// (ראה calibrateBaseline). הערך האמיתי נקבע מחדש בכל reset.
float DEFAULT_DISTANCE = 200.0;       // המרחק הרגיל לקיר ממול (בסנטימטרים)
const float TOLERANCE = 10.0;         // טווח סטייה מותר כדי להתעלם מרעשים קטנים
const int REQUIRED_BREACHES = 5;      // כמה קריאות חריגות *ברצף* נדרשות כדי להזעיק
const int CALIBRATION_SAMPLES = 10;   // כמה קריאות תקינות לוקחים בכיול האתחול
int consecutiveBreaches = 0;          // המונה הנוכחי

// --- בדיקת שפיות לבסיס שנמדד באתחול ---
// הטווח שבו בסיס נחשב "התקנה אמיתית". בלי הבדיקה הזו הכיול קיבל כל מה
// שהוא מדד, והתרחיש הבא הופך את החיישן לחסר תועלת לגמרי: הפסקת חשמל של
// 3 שניות ב-02:00 כשמישהו (או ארגז) עומד 60 ס"מ מהחיישן מקבעת בסיס של
// 60 ס"מ. מאותו רגע המסדרון הריק ב-200 ס"מ נמדד *מעל* הבסיס,
// consecutiveBreaches מתאפס בכל דגימה, והחיישן לא יכול לירות שוב לעולם —
// בשקט מוחלט, כי sendHeartbeat ממשיך להצליח והאפליקציה מציגה לוח בריא.
//
// 100 ס"מ כרף תחתון: חיישן שמכוון לקיר/מסדרון מותקן במרחק סביר ממנו,
// ובסיס קצר מזה אומר שמשהו חסם את הקרן בזמן הכיול — לא שהקיר זז.
// 400 ס"מ כרף עליון: זה הטווח שה-HC-SR04 מתוכנן לו. ה-timeout של
// pulseIn מאפשר עד ~510 ס"מ, אבל קריאה כזו היא כבר הד מקרי, לא מדידה.
const float BASELINE_MIN_CM = 100.0;
const float BASELINE_MAX_CM = 400.0;

// קפיצה מהבסיס הקודם שמעליה מדווחים תקלה — אבל *לא* דוחים. הערך עדיין
// בטווח סביר, וההסבר הסביר ביותר הוא שהחיישן הוזז או כוון מחדש; דחייה
// הייתה הופכת התקנה מחדש לגיטימית לכזו שאי אפשר לתקן בלי למחוק NVS.
// מה שכן היה חסר זה שמישהו יידע: ארגז שהונח 150 ס"מ מהחיישן במסדרון של
// 200 ס"מ מצמצם את שדה הזיהוי בלי שום סימן חיצוני.
const float BASELINE_DRIFT_CM = 40.0;

// הבסיס האחרון שנמצא תקין נשמר ב-NVS (flash), כדי שיהיה למה ליפול חזרה
// כשהכיול נדחה. RAM לא שורדת reset — וה-reset הוא בדיוק הרגע שבו הכיול
// נכשל — אז בלי זה ברירת המחדל המהודרת (200) הייתה האפשרות היחידה, גם
// בהתקנה שכוילה ל-150 ס"מ ועבדה חודשיים. אותו namespace שבו בקר הדלת
// שומר את קוד הקודן.
Preferences prefs;
const char* PREFS_NAMESPACE = "homeguard";
const char* PREFS_BASELINE_KEY = "us_baseline";
bool haveStoredBaseline = false;

// --- זיהוי חיישן מת ---
// HC-SR04 מנותק/שרוף מחזיר 0 לנצח, ו-`if (currentDistance > 0)` בלולאה
// זורק בשקט כל דגימה כזו. כלומר החיישן יכול למות בלי שאף אחד ידע: אין
// חריגות, אין התראות, וה-heartbeat ממשיך לזרום. סופרים קריאות 0 רצופות
// ומדווחים תקלה אחרי ~30 שניות שלהן. אותו דיווח מכסה גם חיישן שמכוון
// לחלל פתוח שאין בו בכלל הד חוזר — גם זו התקנה שלא עובדת.
const unsigned long SAMPLE_INTERVAL_MS = 67;  // ~15 דגימות בשנייה
const unsigned long ZERO_FAULT_MS = 30000;
const int ZERO_READINGS_FAULT = ZERO_FAULT_MS / SAMPLE_INTERVAL_MS;
int consecutiveZeroReadings = 0;

// --- Cooldown לדיווחים: מגביל את קצב ההתראות בלי לעצור את הדגימה ---
// קודם ישב כאן delay(5000) אחרי כל דיווח, כלומר 5 שניות שבהן הלוח לא
// מודד כלום. זה לא גרם ללוח להיראות offline (במקרה הגרוע ~15.1 שניות
// מול BOARD_TIMEOUT של 30), והדיווח שקנה את החלון הזה בדרך כלל נזרק
// ממילא — הבקנד מתעלם מהתראות כשהמערכת לא דרוכה. הנזק הוא העיוורון
// עצמו: מישהו חולף ב-21:59:58 כשמנוטרל, הלוח נכנס לקירור, night mode
// דורך ב-22:00:00, ומי שנכנס ב-22:00:01 לא נמדד עד 22:00:03 — ורק אז
// מתחילות להיספר 5 הקריאות הרצופות הנדרשות. אותה צורה בדיוק כמו
// REPORT_COOLDOWN בלוח הרעידות: הדגימה ממשיכה, רק sendAlert מגודר.
unsigned long lastAlertTime = 0;
const unsigned long ALERT_COOLDOWN = 5000;

// --- דיווח תקלה ---
// קוד תקלה "נעול" שנוסע על כל heartbeat עד שהוא מתנקה (ולכן גם שורד
// כישלון רשת חד-פעמי — הוא ייצא שוב בסבב הבא).
//
// למה על ה-heartbeat ולא על /esp32/sensor-event: sensor-event נכנס
// ל-trigger_alert, שמפעיל אזעקה מלאה כשהמערכת דרוכה — כלומר תקלת כיול
// ב-02:00 הייתה מייללת סירנה — ופשוט נזרק בלי לוג כשהיא לא דרוכה. בדיוק
// הפוך ממה שצריך מדיווח תחזוקה. הבקנד מתעלם מפרמטרי query שהוא לא מכיר,
// אז התוספת הזו לא שוברת כלום היום: היא מופיעה ב-access log של השרת,
// ואפשר יהיה להציג אותה באפליקציה בלי לגעת בקושחה.
const char* FAULT_BASELINE = "baseline_rejected";
const char* FAULT_BASELINE_MOVED = "baseline_moved";
const char* FAULT_NO_ECHO = "no_echo";
const char* activeFault = nullptr;

// כמה זמן מותר לקריאה יוצאת אחת לקחת. ברירת המחדל של הליבה היא 5000ms
// גם ל-connect וגם לתגובה, כלומר קריאה אחת ביש מזל חוסמת את loop() ל-10
// שניות — 150 דגימות שלא נלקחו. הלוח הזה לא עונה לבקשות נכנסות, אז אין
// כאן את הבעיה של בקר הדלת (שם זה גם משתיק את /trigger-alarm), אבל
// עדיין: 10 שניות בלי מדידה זה חלון שמישהו יכול לעבור בו. 1000ms לכל
// שלב חוסמים את המקרה הגרוע על ~2 שניות. הדיווחים ממילא fire-and-forget
// בלי retry, אז המחיר הוא דיווח שאבד, לא דיווח שגוי.
const uint16_t HTTP_TIMEOUT_MS = 1000;

// --- Heartbeat: מדווח לבקנד כל 10 שניות שהלוח הזה חי, כדי שהאפליקציה
// תוכל להראות "ESP32 Online". לא חוסם (לא delay) כדי לא להפריע לדגימת
// החיישן שקורית כל 100ms.
unsigned long lastHeartbeat = 0;
const unsigned long HEARTBEAT_INTERVAL = 10000;

// אתחול: הגדרת מצב הפינים, התחברות לרשת ה-WiFi (חוסם עד שההתחברות
// מצליחה), הדפסת ה-IP שהלוח קיבל ל-Serial (שימושי לאבחון/זיהוי הלוח
// ברשת) ורק אז כיול הבסיס.
//
// סדר הפעולות הזה מכוון: הכיול רץ *אחרי* שה-WiFi למעלה, כי כיול שנדחה
// הוא בדיוק המקרה שצריך להגיע לבקנד, ובלי רשת setFault היה יכול רק
// להדפיס ל-Serial — שאף אחד לא קורא ב-02:00. ההמתנה ל-WiFi גם משמשת
// כזמן ההתייצבות של החיישן אחרי power-up, הרבה מעבר ל-200ms שהיו כאן.
void setup() {
  Serial.begin(115200);
  pinMode(trigPin, OUTPUT);
  pinMode(echoPin, INPUT);

  // התחברות ל-WiFi
  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi connected!");
  Serial.print("My IP: ");
  Serial.println(WiFi.localIP());

  // מאפשר דיווח ראשון מיד: lastAlertTime=0 היה אומר ש-millis()-0 קטן
  // מ-ALERT_COOLDOWN בכל חמש השניות הראשונות אחרי אתחול, כלומר חלון
  // עיוור קטן בדיוק אחרי power-up. החיסור על unsigned עוטף כמו שצריך
  // גם כש-millis() עדיין קטן מ-ALERT_COOLDOWN.
  lastAlertTime = millis() - ALERT_COOLDOWN;

  delay(200); // רגע להתייצבות אחרי power-up, לפני הדגימה הראשונה
  loadStoredBaseline();
  calibrateBaseline();
}

// פונקציה שמבצעת את המדידה ומחזירה מרחק בס"מ.
// עקרון הפעולה: שולחים פולס קצר (10 מיקרו-שניות) על trigPin, שגורם
// לחיישן לפלוט "צפצוף" אולטרה-סוני. echoPin עולה ל-HIGH כשהצפצוף
// יוצא, ויורד ל-LOW כשההד חוזר — pulseIn מודד כמה זמן זה ארך.
float getDistance() {
  digitalWrite(trigPin, LOW);
  delayMicroseconds(2);
  digitalWrite(trigPin, HIGH);
  delayMicroseconds(10);
  digitalWrite(trigPin, LOW);

  // ה-timeout (30000 מיקרו-שניות = 30ms) מונע מהקוד להיתקע אם אין הד
  // חוזר (למשל אם אין שום דבר בטווח האפקטיבי של החיישן).
  long duration = pulseIn(echoPin, HIGH, 30000);

  if (duration == 0) return 0; // חריגה מטווח או שגיאת קריאה

  // מהירות הקול היא בערך 0.034 ס"מ למיקרו-שנייה. מחלקים ב-2 כי
  // המדידה כוללת את הזמן שלוקח לקול ללכת ולחזור (מסלול כפול).
  return duration * 0.034 / 2;
}

// כיול אוטומטי של מרחק הבסיס בכל אתחול: לוקחים CALIBRATION_SAMPLES
// קריאות *תקינות* (מדלגים על 0/timeout, כמו בלולאה הרגילה) וממוצעים
// אותן, במקום להניח 200 ס"מ קבוע מראש. ככה אם המיקום הסופי של החיישן
// שונה מעט מהתכנון (למשל 150 ס"מ מקיר, לא 200), הסף מתאים את עצמו
// אוטומטית — פשוט נותנים ל-reset אחד לקרות אחרי שהחיישן כבר במקומו.
void calibrateBaseline() {
  Serial.println("Calibrating baseline distance...");
  float sum = 0;
  int collected = 0;
  int attempts = 0;
  const int MAX_ATTEMPTS = 50; // תקרת ביטחון - לא נתקעים לנצח אם אין הד בכלל

  while (collected < CALIBRATION_SAMPLES && attempts < MAX_ATTEMPTS) {
    float d = getDistance();
    attempts++;
    if (d > 0) {
      collected++;
      sum += d;
      Serial.printf("  sample %d/%d: %.1f cm\n", collected, CALIBRATION_SAMPLES, d);
    }
    delay(60); // מרווח קצר בין דגימות, שהד קודם ידעך לפני הבאה
  }

  if (collected == 0) {
    // אף קריאה תקינה לא התקבלה (חיישן מנותק/שרוף, או שאין שום דבר בטווח
    // האפקטיבי שלו) - נשארים עם הבסיס הקודם/ברירת המחדל במקום 0, שהיה
    // גורם לכל קריאה עתידית להיחשב חריגה (0 < DEFAULT_DISTANCE - TOLERANCE
    // תמיד אמת). זו תקלה אמיתית, לא הערת Serial: בלי דיווח הלוח נראה בריא.
    Serial.printf("WARNING: calibration got no valid reading — keeping %.1f cm.\n", DEFAULT_DISTANCE);
    setFault(FAULT_BASELINE);
    return;
  }

  float measured = sum / collected;

  if (!isPlausibleBaseline(measured)) {
    // הערך נמדד, הוא פשוט לא יכול להיות התקנה אמיתית (ראה BASELINE_MIN_CM).
    // נופלים חזרה למה שכבר ידוע — הבסיס השמור, או ברירת המחדל — ומדווחים.
    Serial.printf("WARNING: measured baseline %.1f cm is outside %.0f-%.0f cm — rejected, keeping %.1f cm.\n",
                  measured, BASELINE_MIN_CM, BASELINE_MAX_CM, DEFAULT_DISTANCE);
    setFault(FAULT_BASELINE);
    return;
  }

  // הערך התקבל. אם הוא רחוק מהבסיס השמור, מדווחים על כך (בלי לדחות —
  // ראה BASELINE_DRIFT_CM) ומאמצים אותו, כדי שהאתחול הבא כבר ישווה
  // למציאות הנוכחית ולא ידווח על אותה קפיצה שוב ושוב.
  if (haveStoredBaseline && fabs(measured - DEFAULT_DISTANCE) > BASELINE_DRIFT_CM) {
    Serial.printf("WARNING: baseline moved %.1f cm -> %.1f cm since the last boot.\n",
                  DEFAULT_DISTANCE, measured);
    setFault(FAULT_BASELINE_MOVED);
  }

  DEFAULT_DISTANCE = measured;
  storeBaseline(measured);
  Serial.printf("Baseline calibrated: %.1f cm (from %d readings)\n", DEFAULT_DISTANCE, collected);
}

// האם הערך נראה כמו בסיס של התקנה אמיתית (ראה BASELINE_MIN_CM/MAX_CM).
bool isPlausibleBaseline(float cm) {
  return cm >= BASELINE_MIN_CM && cm <= BASELINE_MAX_CM;
}

// טוען את הבסיס האחרון שנשמר, אם יש כזה והוא עדיין בטווח סביר. הערך
// הזה הוא גם נקודת הנפילה-לאחור של כיול שנדחה וגם ההשוואה שמזהה שהחיישן
// זז. ערך שמור שאיבד שפיות (למשל אחרי שינוי התקנה + כיול שגוי בגרסה
// ישנה של הקושחה) נזרק כאן, כדי שלא ניפול חזרה למשהו גרוע יותר מ-200.
void loadStoredBaseline() {
  prefs.begin(PREFS_NAMESPACE, /*readOnly=*/true);
  float stored = prefs.getFloat(PREFS_BASELINE_KEY, 0.0);
  prefs.end();

  if (isPlausibleBaseline(stored)) {
    DEFAULT_DISTANCE = stored;
    haveStoredBaseline = true;
    Serial.printf("Previous baseline from NVS: %.1f cm\n", stored);
  } else {
    Serial.printf("No usable baseline in NVS — falling back on %.1f cm.\n", DEFAULT_DISTANCE);
  }
}

// שומר בסיס שעבר את בדיקות השפיות. נכתב בכל אתחול מוצלח ולא רק בשינוי,
// כי מדובר בכתיבה אחת ל-boot — NVS עושה wear leveling, וזה רחוק מלהיות
// קצב שמבלה flash.
void storeBaseline(float cm) {
  prefs.begin(PREFS_NAMESPACE, /*readOnly=*/false);
  prefs.putFloat(PREFS_BASELINE_KEY, cm);
  prefs.end();
  haveStoredBaseline = true;
}

// בסיס ה-URL של הבקנד, מורכב פעם אחת במקום בכל קריאה.
String backendUrl(const char* path) {
  return "http://" + String(backendHost) + ":" + String(backendPort) + String(path);
}

// פותח בקשה יוצאת עם ה-timeout-ים מוגדרים. כל קריאה יוצאת בקובץ הזה
// עוברת דרך כאן ולא קוראת ל-http.begin() ישירות, כדי שפונקציית דיווח
// חדשה לא תחזיר בשקט את התקיעה של 10 השניות (ראה HTTP_TIMEOUT_MS).
// אותה פונקציה בדיוק כמו beginRequest בבקר הדלת, מאותה סיבה.
void beginRequest(HTTPClient& http, const String& url) {
  http.begin(url);
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);
}

// שולח דיווח פריצה לבקנד. ה-endpoint האמיתי מצפה ל-POST עם
// sensor ו-reason כ-query parameters (לא body) — ראה
// python/backend/main.py, esp32_sensor_event().
//
// כותרת X-Device-Key היא החובה החדשה: בלעדיה הבקנד מחזיר 401. זה מה
// שמונע ממישהו אחר ברשת לזייף דיווח פריצה (או, גרוע מכך, לזייף דיווח
// "המערכת נוטרלה").
void sendAlert() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Cannot send alert — WiFi disconnected");
    return;
  }

  HTTPClient http;
  beginRequest(http, backendUrl("/esp32/sensor-event?sensor=ultrasonic&board=ultrasonic"
                                "&reason=Ultrasonic%20sensor%20breached"));
  http.addHeader("X-Device-Key", DEVICE_KEY);
  int httpResponseCode = http.POST("");  // גוף ריק — כל המידע ב-query string

  if (httpResponseCode == 401) {
    Serial.println("Alert REJECTED (401) — DEVICE_KEY in secrets.h doesn't match the backend's.");
  } else if (httpResponseCode > 0) {
    Serial.printf("Alert sent successfully. Server responded: %d\n", httpResponseCode);
  } else {
    Serial.printf("Error sending alert: %s\n", http.errorToString(httpResponseCode).c_str());
  }
  http.end();
}

// שולח פינג "אני חי" לבקנד (ראו הסבר על ה-heartbeat למעלה). מדווח את שם
// הלוח (board=), כדי שהבקנד יידע מי בדיוק מדבר, ואת קוד התקלה הנעול אם
// יש כזה (ראה activeFault). בניגוד ל-sendAlert, לא בודקים כאן את קוד
// התגובה — אם זה נכשל, פשוט ננסה שוב בסבב הבא (10 שניות), אין צורך
// בטיפול מיוחד בכישלון חד-פעמי.
void sendHeartbeat() {
  if (WiFi.status() != WL_CONNECTED) return;
  String url = backendUrl("/esp32/heartbeat?board=") + BOARD_NAME;
  if (activeFault != nullptr) {
    url += "&fault=";
    url += activeFault;
  }
  HTTPClient http;
  beginRequest(http, url);
  http.addHeader("X-Device-Key", DEVICE_KEY);
  http.POST("");
  http.end();
}

// נועל קוד תקלה ושולח heartbeat מיידי, כדי שהתקלה לא תמתין עד עשר שניות
// לסבב הבא. קוד אחד בכל רגע — התקלה האחרונה גוברת; שתי תקלות שונות בו
// זמנית (כיול שנדחה *וגם* חיישן מת) הן בפועל אותה בעיית התקנה אחת.
void setFault(const char* code) {
  if (activeFault == code) return; // כבר נעול על אותה תקלה
  activeFault = code;
  Serial.printf("FAULT reported to backend: %s\n", code);
  sendHeartbeat();
}

// מנקה את התקלה ומדווח מיד, כדי שהבקנד לא ימשיך לראות תקלה שנפתרה.
void clearFault() {
  if (activeFault == nullptr) return;
  Serial.printf("FAULT cleared: %s\n", activeFault);
  activeFault = nullptr;
  sendHeartbeat();
}

// לולאת הריצה: דוגמת את החיישן, סופרת חריגות רצופות, מדווחת פריצה
// כשמצטברות מספיק חריגות ברצף, ומדווחת heartbeat תקופתי — הכל בלי
// delay ארוך שיפגע בקצב הדגימה.
void loop() {
  float currentDistance = getDistance();

  // אנחנו בודקים רק קריאות חוקיות (מעל 0)
  if (currentDistance > 0) {
    // קריאה תקינה: אם היינו ברצף של אפסים, החיישן חי אחרי הכל.
    consecutiveZeroReadings = 0;
    if (activeFault == FAULT_NO_ECHO) clearFault();

    // אם המרחק קטן משמעותית מהמרחק לקיר ממול — כנראה שמישהו/משהו חוסם
    if (currentDistance < (DEFAULT_DISTANCE - TOLERANCE)) {
      consecutiveBreaches++; // מעלים את המונה
      Serial.printf("Anomaly detected! Distance: %.1f cm (Count: %d)\n", currentDistance, consecutiveBreaches);

      // אם הגענו לרצף הנדרש - זו חדירה אמיתית ולא רעש אקראי
      if (consecutiveBreaches >= REQUIRED_BREACHES) {
        consecutiveBreaches = 0; // איפוס המונה

        // ה-cooldown מגדר רק את הדיווח. הדגימה נמשכת, ולכן נוכחות
        // שממשיכה בזמן הקירור נספרת מחדש ומדווחת מיד כשהחלון נסגר,
        // במקום להתחיל להימדד רק אחרי 5 שניות של עיוורון.
        if (millis() - lastAlertTime >= ALERT_COOLDOWN) {
          Serial.println("ALARM! Confirmed intrusion.");
          sendAlert();
          lastAlertTime = millis();
        }
      }
    } else {
      // הטווח חזר להיות תקין - מאפסים את מונה רצף החריגות
      consecutiveBreaches = 0;
    }
  } else {
    // 0 = לא חזר הד. דגימה בודדת כזו היא נורמלית לגמרי, אבל רצף ארוך
    // שלה אומר חיישן מת/מנותק — והלולאה מתעלמת מכל אחת מהן בשקט.
    // מדווחים פעם אחת כשהרצף חוצה את הסף (==, לא >=), כדי לא לירות
    // דיווח בכל דגימה כל עוד המצב נמשך.
    consecutiveZeroReadings++;
    if (consecutiveZeroReadings == ZERO_READINGS_FAULT) {
      Serial.printf("WARNING: %d consecutive empty readings (~%lus) — sensor looks dead.\n",
                    consecutiveZeroReadings, ZERO_FAULT_MS / 1000);
      setFault(FAULT_NO_ECHO);
    }
  }

  // דיווח heartbeat תקופתי, בלי לחסום את דגימת החיישן
  if (millis() - lastHeartbeat >= HEARTBEAT_INTERVAL) {
    sendHeartbeat();
    lastHeartbeat = millis();
  }

  delay(SAMPLE_INTERVAL_MS); // ~15 דגימות בשנייה (פי 1.5 מהר יותר מ-100ms)
}
