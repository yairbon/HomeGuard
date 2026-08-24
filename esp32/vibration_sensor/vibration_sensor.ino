// ============================================================
// HomeGuard — לוח 2 מתוך 4: חיישן רעידות (SW420-style, דיגיטלי)
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
// הערה: שם הקובץ המקורי היה "CamAndForce" אבל אין בו שום קוד מצלמה,
// וגם לא צריך שיהיה: זיהוי הפנים רץ על ה-Backend (Python + OpenCV),
// והפריימים מגיעים אליו מלוח נפרד — ה-ESP32-CAM, לוח 4.
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

// --- קליטת הרעידה: פסיקה (interrupt), לא polling ---
// SW-420 במצב משווה (comparator) לא "מחזיק" את הפין גבוה — הוא פולט
// צרורות פולסים קצרים, בסדר גודל של מיקרו-שניות עד מילי-שניות בודדות,
// ואין שום דבר בחומרה שנועל אותם עד שמישהו יקרא. הקוד הקודם עשה
// digitalRead כל 50ms, כלומר 20 דגימות בשנייה, ותפס צרור רק אם במקרה
// הפין היה HIGH בדיוק ברגע הקריאה: לצרור של 2ms זה בערך 4% סיכוי.
// כלומר מכה חזקה אחת במשקוף חלון יכלה לא לייצר שום אירוע — וזיהוי
// פריצה בכוח הוא כל הסיבה שהחיישן הזה קיים.
//
// הפסיקה תופסת כל קצה עולה, גם בן 100 מיקרו-שניות, ונועלת אותו בדגל
// שה-loop קורא בקצב שלו.
volatile bool vibrationLatched = false;

// ה-ISR עושה את המינימום המוחלט — מרים דגל וחוזר. IRAM_ATTR כי קוד של
// פסיקה חייב לשבת ב-RAM: פסיקה יכולה לקרות בזמן פעולה על ה-flash, ואז
// קוד שיושב ב-flash פשוט לא נגיש והלוח קורס. volatile כדי שהקומפיילר
// לא יטמון את הדגל ברגיסטר וה-loop יקרא ערך ישן לנצח.
void IRAM_ATTR onVibration() {
  vibrationLatched = true;
}

// --- Cooldown: אחרי דיווח, מתעלמים מרעידות נוספות לזמן מה כדי לא
// להפציץ את השרת אם הרעידה נמשכת (למשל מכות חוזרות באותה פריצה).
// זה מה ששומר על קצב הדיווחים שפוי עכשיו כשהפסיקה תופסת *כל* צרור.
unsigned long lastReportTime = 0;
const unsigned long REPORT_COOLDOWN = 5000;

unsigned long lastHeartbeat = 0;
const unsigned long HEARTBEAT_INTERVAL = 10000;

// כמה זמן מותר לקריאה יוצאת אחת לקחת. ברירת המחדל של הליבה היא 5000ms
// גם ל-connect וגם לתגובה, כלומר קריאה אחת ביש מזל חוסמת את loop() ל-10
// שניות. הלוח הזה לא עונה לבקשות נכנסות, אז אין כאן את הבעיה של בקר
// הדלת (שם תקיעה כזו משתיקה גם את /trigger-alarm), אבל 10 שניות בלי
// לפנות את דגל הפסיקה עדיין אומרות דיווח שמאחר אחרי המכה הראשונה.
// 1000ms לכל שלב חוסמים את המקרה הגרוע על ~2 שניות. הדיווחים ממילא
// fire-and-forget בלי retry, אז המחיר הוא דיווח שאבד, לא דיווח שגוי.
const uint16_t HTTP_TIMEOUT_MS = 1000;

// אתחול: הגדרת פין החיישן כקלט וחיבור הפסיקה אליו, התחברות לרשת ה-WiFi
// (חוסם עד שההתחברות מצליחה) והדפסת ה-IP שהלוח קיבל ל-Serial.
void setup() {
  Serial.begin(115200);
  Serial.println();

  pinMode(vibrationPin, INPUT);
  // RISING: מודול SW-420 מחזיק את היציאה נמוכה בשקט ומרים אותה על
  // רעידה, אז הקצה העולה הוא תחילת הצרור. הפסיקה נרשמת עוד לפני
  // ה-WiFi כדי שמכה שמגיעה בזמן ההתחברות (שיכולה לקחת שניות) תינעל
  // בדגל ותדווח מיד כשיש רשת, במקום פשוט להיעלם.
  attachInterrupt(digitalPinToInterrupt(vibrationPin), onVibration, RISING);

  // מאפשר דיווח ראשון מיד: lastReportTime=0 אומר ש-millis()-0 קטן
  // מ-REPORT_COOLDOWN בכל חמש השניות הראשונות אחרי אתחול, כלומר מכה
  // בדיוק אחרי power-up הייתה נבלעת. החיסור על unsigned עוטף כמו
  // שצריך גם כש-millis() עדיין קטן מ-REPORT_COOLDOWN.
  lastReportTime = millis() - REPORT_COOLDOWN;

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

// פותח בקשה יוצאת עם ה-timeout-ים מוגדרים. כל קריאה יוצאת בקובץ הזה
// עוברת דרך כאן ולא קוראת ל-http.begin() ישירות, כדי שפונקציית דיווח
// חדשה לא תחזיר בשקט את התקיעה של 10 השניות (ראה HTTP_TIMEOUT_MS).
// אותה פונקציה בדיוק כמו beginRequest בבקר הדלת, מאותה סיבה.
void beginRequest(HTTPClient& http, const String& url) {
  http.begin(url);
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);
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
  beginRequest(http, backendUrl("/esp32/sensor-event?sensor=vibration&board=vibration"
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
  beginRequest(http, backendUrl("/esp32/heartbeat?board=") + BOARD_NAME);
  http.addHeader("X-Device-Key", DEVICE_KEY);
  http.POST("");
  http.end();
}

// לולאת הריצה: צורכת את הדגל שה-ISR מרים (קצה עולה = תחילת צרור
// רעידות), מדווחת לבקנד בקצב שה-cooldown מתיר, ומדווחת heartbeat
// תקופתי. אין כאן יותר דגימה של הפין — הפסיקה היא זו שתופסת.
void loop() {
  // קוראים ומאפסים את הדגל מיד: אם צרור נוסף מגיע בזמן שאנחנו מדווחים,
  // ה-ISR ירים אותו שוב וניקח אותו בסבב הבא, במקום לאבד אותו או לזלוג
  // עליו. קריאה+כתיבה של bool בודד היא פעולה אטומית על הליבה הזו, אז
  // אין צורך לנטרל פסיקות סביבה.
  if (vibrationLatched) {
    vibrationLatched = false;
    Serial.println("Vibration Detected! (Force sensor triggered)");

    if (millis() - lastReportTime >= REPORT_COOLDOWN) {
      sendAlert();
      lastReportTime = millis();
    }
  }

  if (millis() - lastHeartbeat >= HEARTBEAT_INTERVAL) {
    sendHeartbeat();
    lastHeartbeat = millis();
  }

  // לא תקופת דגימה ולא debounce — הפסיקה תופסת את הרעידות בלי קשר
  // לקצב הלולאה, וה-cooldown הוא זה שמגביל דיווחים. זה רק ויתור על
  // המעבד: הליבה מפעילה yield אוטומטי רק בבנייה חד-ליבתית, אז לולאה
  // בלי delay רצה 100% CPU ומרעיבה את משימת ה-idle ואת שכבת ה-WiFi.
  delay(10);
}
