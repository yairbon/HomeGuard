// ============================================================
// HomeGuard — לוח 1: חיישן אולטרה-סוני (HC-SR04)
// תפקיד: לזהות כניסה שלא מהדלת הראשית, ע"י מדידת מרחק לקיר/רצפה
// ממול ובדיקה אם המרחק "נסגר" (מישהו עומד בדרך).
//
// לוח זה הוא "טיפש" בכוונה: הוא לא יודע אם המערכת דרוכה או לא.
// הוא פשוט מדווח לבקנד כל אימות פריצה, וה-Backend (state.py,
// trigger_alert) מחליט אם זה משנה — הוא מתעלם מהדיווח אם המערכת
// לא דרוכה. ככה הלוח הזה לא צריך לדעת שום דבר על שאר המערכת.
// ============================================================

#include <WiFi.h>
#include <HTTPClient.h>

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
const float DEFAULT_DISTANCE = 200.0; // המרחק הרגיל לקיר ממול (בסנטימטרים)
const float TOLERANCE = 30.0;         // טווח סטייה מותר כדי להתעלם מרעשים קטנים
const int REQUIRED_BREACHES = 8;      // כמה קריאות חריגות *ברצף* נדרשות כדי להזעיק
int consecutiveBreaches = 0;          // המונה הנוכחי

// --- Heartbeat: מדווח לבקנד כל 10 שניות שהלוח הזה חי, כדי שהאפליקציה
// תוכל להראות "ESP32 Online". לא חוסם (לא delay) כדי לא להפריע לדגימת
// החיישן שקורית כל 100ms.
unsigned long lastHeartbeat = 0;
const unsigned long HEARTBEAT_INTERVAL = 10000;

// אתחול: הגדרת מצב הפינים, התחברות לרשת ה-WiFi (חוסם עד שההתחברות
// מצליחה) והדפסת ה-IP שהלוח קיבל ל-Serial (שימושי לאבחון/זיהוי הלוח ברשת).
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

// בסיס ה-URL של הבקנד, מורכב פעם אחת במקום בכל קריאה.
String backendUrl(const char* path) {
  return "http://" + String(backendHost) + ":" + String(backendPort) + String(path);
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
  http.begin(backendUrl("/esp32/sensor-event?sensor=ultrasonic&board=ultrasonic"
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
// הלוח (board=), כדי שהבקנד יידע מי בדיוק מדבר. בניגוד ל-sendAlert, לא
// בודקים כאן את קוד התגובה — אם זה נכשל, פשוט ננסה שוב בסבב הבא (10
// שניות), אין צורך בטיפול מיוחד בכישלון חד-פעמי.
void sendHeartbeat() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  http.begin(backendUrl("/esp32/heartbeat?board=") + BOARD_NAME);
  http.addHeader("X-Device-Key", DEVICE_KEY);
  http.POST("");
  http.end();
}

// לולאת הריצה: דוגמת את החיישן, סופרת חריגות רצופות, מדווחת פריצה
// כשמצטברות מספיק חריגות ברצף, ומדווחת heartbeat תקופתי — הכל בלי
// delay ארוך שיפגע בקצב הדגימה.
void loop() {
  float currentDistance = getDistance();

  // אנחנו בודקים רק קריאות חוקיות (מעל 0)
  if (currentDistance > 0) {

    // אם המרחק קטן משמעותית מהמרחק לקיר ממול — כנראה שמישהו/משהו חוסם
    if (currentDistance < (DEFAULT_DISTANCE - TOLERANCE)) {
      consecutiveBreaches++; // מעלים את המונה
      Serial.printf("Anomaly detected! Distance: %.1f cm (Count: %d)\n", currentDistance, consecutiveBreaches);

      // אם הגענו לרצף הנדרש - זו חדירה אמיתית ולא רעש אקראי
      if (consecutiveBreaches >= REQUIRED_BREACHES) {
        Serial.println("ALARM! Confirmed intrusion.");
        sendAlert();

        consecutiveBreaches = 0; // איפוס המונה
        delay(5000); // Cooldown - השהייה כדי לא להפציץ את השרת בהתראות חוזרות
      }
    } else {
      // הטווח חזר להיות תקין - מאפסים את מונה רצף החריגות
      consecutiveBreaches = 0;
    }
  }

  // דיווח heartbeat תקופתי, בלי לחסום את דגימת החיישן
  if (millis() - lastHeartbeat >= HEARTBEAT_INTERVAL) {
    sendHeartbeat();
    lastHeartbeat = millis();
  }

  delay(100); // דוגמים את החיישן עשר פעמים בשנייה
}
