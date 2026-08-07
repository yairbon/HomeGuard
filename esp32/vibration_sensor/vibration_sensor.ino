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

// כל הסודות יושבים ב-secrets.h שלא נכנס לגיט — העתק את
// esp32/secrets.h.example לתיקייה הזו בשם secrets.h ומלא את הערכים.
#include "secrets.h"

// --- הגדרות רשת ---
const char* ssid = WIFI_SSID;
const char* password = WIFI_PASSWORD;

// כתובת שרת הפייתון (הבקנד) — ראה secrets.h.
const char* backendHost = BACKEND_HOST;
const int backendPort = BACKEND_PORT;

// שם הלוח הזה כפי שהבקנד מזהה אותו (ראה BOARD_NAMES ב-state.py).
const char* BOARD_NAME = "vibration";

// --- הגדרות פינים ---
const int vibrationPin = 13;
int lastVibrationState = LOW;

// --- Cooldown: אחרי דיווח, מתעלמים מרעידות נוספות לזמן מה כדי לא
// להפציץ את השרת אם הרעידה נמשכת (למשל מכות חוזרות באותה פריצה).
unsigned long lastReportTime = 0;
const unsigned long REPORT_COOLDOWN = 5000;

unsigned long lastHeartbeat = 0;
const unsigned long HEARTBEAT_INTERVAL = 10000;

// אתחול: הגדרת פין החיישן כקלט, התחברות לרשת ה-WiFi (חוסם עד
// שההתחברות מצליחה) והדפסת ה-IP שהלוח קיבל ל-Serial.
void setup() {
  Serial.begin(115200);
  Serial.println();

  pinMode(vibrationPin, INPUT);

  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi connected");
  Serial.print("My IP: ");
  Serial.println(WiFi.localIP());
}

// בסיס ה-URL של הבקנד, מורכב פעם אחת במקום בכל קריאה.
String backendUrl(const char* path) {
  return "http://" + String(backendHost) + ":" + String(backendPort) + String(path);
}

// שולח דיווח פריצה לבקנד — אותו endpoint שהחיישן האולטרה-סוני
// משתמש בו, רק עם sensor=vibration. כותרת X-Device-Key היא חובה,
// אחרת הבקנד מחזיר 401 (ראה require_device ב-main.py).
void sendAlert() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Cannot send alert — WiFi disconnected");
    return;
  }

  HTTPClient http;
  http.begin(backendUrl("/esp32/sensor-event?sensor=vibration&board=vibration"
                        "&reason=Vibration%20sensor%20breached"));
  http.addHeader("X-Device-Key", DEVICE_KEY);
  int httpResponseCode = http.POST("");

  if (httpResponseCode == 401) {
    Serial.println("Alert REJECTED (401) — DEVICE_KEY in secrets.h doesn't match the backend's.");
  } else if (httpResponseCode > 0) {
    Serial.printf("Alert sent successfully. Server responded: %d\n", httpResponseCode);
  } else {
    Serial.printf("Error sending alert: %s\n", http.errorToString(httpResponseCode).c_str());
  }
  http.end();
}

// שולח פינג "אני חי" לבקנד (ראו הסבר על ה-heartbeat למעלה) — אותה
// פונקציה בדיוק כמו בלוח האולטרה-סוני, רק עם board=vibration.
void sendHeartbeat() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  http.begin(backendUrl("/esp32/heartbeat?board=") + BOARD_NAME);
  http.addHeader("X-Device-Key", DEVICE_KEY);
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

    if (millis() - lastReportTime >= REPORT_COOLDOWN) {
      sendAlert();
      lastReportTime = millis();
    }
  }

  lastVibrationState = currentVibrationState;

  if (millis() - lastHeartbeat >= HEARTBEAT_INTERVAL) {
    sendHeartbeat();
    lastHeartbeat = millis();
  }

  delay(50); // Debounce
}
