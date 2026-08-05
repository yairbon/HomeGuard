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

// --- הגדרות רשת ---
const char* ssid = "Free-TAU";
const char* password = "free-tau";

// כתובת שרת הפייתון (הבקנד). עדכן ל-IP האמיתי של המחשב שמריץ אותו
// (uvicorn, פורט 8000 כברירת מחדל — ראה python/README.md).
const char* backendHost = "192.168.X.X";  // TODO: לעדכן ל-IP האמיתי
const int backendPort = 8000;

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

// --- Cooldown אחרי דיווח: מונע הצפה של השרת אם החדירה נמשכת. מנוהל
// עם millis() ולא עם delay() — ראה את ההסבר ב-loop().
unsigned long lastReportTime = 0;
bool hasReportedOnce = false;
const unsigned long REPORT_COOLDOWN = 5000;

// כמה זמן מחכים ל-WiFi באתחול לפני שממשיכים בכל זאת.
const unsigned long WIFI_CONNECT_TIMEOUT = 15000;

// התחברות ל-WiFi עם המתנה חסומה בזמן, במקום לולאה אינסופית. אם הראוטר
// לא זמין ברגע האתחול (למשל הפסקת חשמל שבה הלוח עולה לפני הראוטר),
// הלולאה המקורית פשוט לא הייתה מסתיימת — הלוח לעולם לא היה מגיע ל-loop()
// ולכן גם לא היה דוגם את החיישן בכלל. עדיף שימשיך לדגום ויתחבר ברקע:
// setAutoReconnect גורם לליבת ה-ESP32 להמשיך לנסות לבד, וכל פונקציית
// שליחה כאן כבר בודקת WiFi.status() לפני שהיא מנסה לדווח.
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
    Serial.println("\nWiFi connected!");
    Serial.print("My IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\nWiFi unavailable — ממשיך לדגום, מנסה להתחבר ברקע.");
  }
}

// אתחול: הגדרת מצב הפינים והתחברות לרשת ה-WiFi (ראה connectWiFi).
void setup() {
  Serial.begin(115200);
  pinMode(trigPin, OUTPUT);
  pinMode(echoPin, INPUT);

  connectWiFi();
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

// שולח דיווח פריצה לבקנד. ה-endpoint האמיתי מצפה ל-POST עם
// sensor ו-reason כ-query parameters (לא body) — ראה
// python/backend/main.py, esp32_sensor_event().
void sendAlert() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Cannot send alert — WiFi disconnected");
    return;
  }

  HTTPClient http;
  String url = "http://" + String(backendHost) + ":" + String(backendPort) +
               "/esp32/sensor-event?sensor=ultrasonic&reason=Ultrasonic%20sensor%20breached";
  http.begin(url);
  int httpResponseCode = http.POST("");  // גוף ריק — כל המידע ב-query string

  if (httpResponseCode > 0) {
    Serial.printf("Alert sent successfully. Server responded: %d\n", httpResponseCode);
  } else {
    Serial.printf("Error sending alert: %s\n", http.errorToString(httpResponseCode).c_str());
  }
  http.end();
}

// שולח פינג "אני חי" לבקנד (ראו הסבר על ה-heartbeat למעלה). בניגוד
// ל-sendAlert, לא בודקים כאן את קוד התגובה — אם זה נכשל, פשוט ננסה שוב
// בסבב הבא (10 שניות), אין צורך בטיפול מיוחד בכישלון חד-פעמי.
void sendHeartbeat() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  String url = "http://" + String(backendHost) + ":" + String(backendPort) + "/esp32/heartbeat";
  http.begin(url);
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
        // ה-cooldown מנוהל ב-millis() ולא ב-delay(5000) כמו קודם. עם
        // delay חוסם, הלוח היה עיוור לחלוטין למשך 5 שניות אחרי כל דיווח —
        // גם לדגימת החיישן וגם ל-heartbeat — כלומר בדיוק בחלון שבו פורץ
        // ממשיך להיכנס. זה גם היה חריג לעומת לוח הרעידות, שכבר עבד ככה.
        if (!hasReportedOnce || millis() - lastReportTime >= REPORT_COOLDOWN) {
          Serial.println("ALARM! Confirmed intrusion.");
          sendAlert();
          lastReportTime = millis();
          hasReportedOnce = true;
        }
        consecutiveBreaches = 0; // איפוס המונה
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
