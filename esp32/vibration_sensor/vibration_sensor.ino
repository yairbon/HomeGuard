// ============================================================
// HomeGuard — לוח 2: חיישן רעידות (SW420-style, דיגיטלי)
// תפקיד: לזהות שימוש בכוח מופרז (מכה, ניסיון פריצה) דרך חיישן
// רעידות דיגיטלי (HIGH כשיש רעידה, LOW כשקט).
//
// שינוי מהגרסה המקורית (CamAndForce.ino): המקור הריץ WebServer
// שמחכה שמישהו יבקש ממנו את הסטטוס (/sensor). שיניתי לדגם "דחיפה"
// (push) — הלוח הזה יוזם ושולח דיווח לבקנד ברגע שהוא מזהה רעידה,
// באותה שיטה בדיוק כמו לוח החיישן האולטרה-סוני. כך כל לוחות החיישנים
// עובדים לפי אותו pattern, ואין צורך שהבקנד "יזכור" ויבקש (poll) מכל
// לוח בנפרד — פשוט יותר ועמיד יותר (ה-IP של כל לוח יכול להשתנות
// כשה-DHCP מחדש אותו; ב-push זה לא משנה כי הלוח הוא זה שיוזם).
//
// הערה: שם הקובץ המקורי היה "CamAndForce" אבל אין בו שום קוד מצלמה —
// זיהוי הפנים רץ על ה-Backend (Python + OpenCV) דרך מצלמה נפרדת,
// לא על ה-ESP32 הזה. אם התכוונתם למשהו אחר, תגידו לי.
//
// כמו הלוח האולטרה-סוני, גם הלוח הזה "טיפש" בכוונה — לא יודע אם
// המערכת דרוכה, פשוט מדווח וה-Backend מחליט אם זה משנה.
// ============================================================

#include <WiFi.h>
#include <HTTPClient.h>

// --- הגדרות רשת (הכניסו את שלכם) ---
const char* ssid = "Free-TAU";
const char* password = "free-tau";

// כתובת שרת הפייתון (הבקנד).
const char* backendHost = "192.168.X.X";  // TODO: לעדכן ל-IP האמיתי
const int backendPort = 8000;

// --- הגדרות פינים ---
const int vibrationPin = 13;
int lastVibrationState = LOW;

// --- Cooldown: אחרי דיווח, מתעלמים מרעידות נוספות לזמן מה כדי לא
// להפציץ את השרת אם הרעידה נמשכת (למשל מכות חוזרות באותה פריצה).
// hasReportedOnce נדרש כי millis() מתחיל מ-0: בלעדיו התנאי
// millis() - lastReportTime >= REPORT_COOLDOWN הוא false בחמש השניות
// הראשונות אחרי האתחול, כלומר רעידה בדיוק בחלון הזה נבלעה בשקט.
unsigned long lastReportTime = 0;
bool hasReportedOnce = false;
const unsigned long REPORT_COOLDOWN = 5000;

unsigned long lastHeartbeat = 0;
const unsigned long HEARTBEAT_INTERVAL = 10000;

// כמה זמן מחכים ל-WiFi באתחול לפני שממשיכים בכל זאת.
const unsigned long WIFI_CONNECT_TIMEOUT = 15000;

// התחברות ל-WiFi עם המתנה חסומה בזמן, במקום לולאה אינסופית — אותו
// שיקול בדיוק כמו בלוח האולטרה-סוני: אם הראוטר לא זמין באתחול, הלולאה
// המקורית לא הייתה מסתיימת והלוח לא היה מגיע ל-loop() בכלל, כלומר לא
// היה דוגם את חיישן הרעידות. setAutoReconnect ממשיך לנסות ברקע, וכל
// פונקציית שליחה כאן כבר בודקת WiFi.status() לפני שהיא מדווחת.
void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi");
  unsigned long startedAt = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startedAt < WIFI_CONNECT_TIMEOUT) {
    delay(500);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi connected");
    Serial.print("My IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\nWiFi unavailable — ממשיך לדגום, מנסה להתחבר ברקע.");
  }
}

// אתחול: הגדרת פין החיישן כקלט והתחברות לרשת ה-WiFi (ראה connectWiFi).
void setup() {
  Serial.begin(115200);
  Serial.println();

  pinMode(vibrationPin, INPUT);

  connectWiFi();
}

// שולח דיווח פריצה לבקנד — אותו endpoint שהחיישן האולטרה-סוני
// משתמש בו, רק עם sensor=vibration.
void sendAlert() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Cannot send alert — WiFi disconnected");
    return;
  }

  HTTPClient http;
  String url = "http://" + String(backendHost) + ":" + String(backendPort) +
               "/esp32/sensor-event?sensor=vibration&reason=Vibration%20sensor%20breached";
  http.begin(url);
  int httpResponseCode = http.POST("");

  if (httpResponseCode > 0) {
    Serial.printf("Alert sent successfully. Server responded: %d\n", httpResponseCode);
  } else {
    Serial.printf("Error sending alert: %s\n", http.errorToString(httpResponseCode).c_str());
  }
  http.end();
}

// שולח פינג "אני חי" לבקנד (ראו הסבר על ה-heartbeat למעלה) — אותה
// פונקציה בדיוק כמו בלוח האולטרה-סוני.
void sendHeartbeat() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  String url = "http://" + String(backendHost) + ":" + String(backendPort) + "/esp32/heartbeat";
  http.begin(url);
  http.POST("");
  http.end();
}

// לולאת הריצה: קוראת את מצב חיישן הרעידות, מזהה קצה עולה (LOW→HIGH)
// כדי לדווח פעם אחת בלבד לכל אירוע (לא לכל אימות שהחיישן עדיין HIGH),
// ומדווחת heartbeat תקופתי.
void loop() {
  int currentVibrationState = digitalRead(vibrationPin);

  // מדפיסים ל-Serial ומדווחים לבקנד רק ב"קצה" — כשעוברים ממצב שקט
  // (LOW) למצב רעידה (HIGH). זה מונע הצפה של דיווחים בזמן שהחיישן
  // "תקוע" על HIGH לאורך זמן.
  if (currentVibrationState == HIGH && lastVibrationState == LOW) {
    Serial.println("Vibration Detected! (Force sensor triggered)");

    if (!hasReportedOnce || millis() - lastReportTime >= REPORT_COOLDOWN) {
      sendAlert();
      lastReportTime = millis();
      hasReportedOnce = true;
    }
  }

  lastVibrationState = currentVibrationState;

  if (millis() - lastHeartbeat >= HEARTBEAT_INTERVAL) {
    sendHeartbeat();
    lastHeartbeat = millis();
  }

  delay(50); // Debounce
}
