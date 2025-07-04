import os
import time
import requests
import firebase_admin
from firebase_admin import credentials, firestore

# ========== CONFIGURACIÓN ==========
SERVICE_ACCOUNT_FILE = 'security-cam-f322b-firebase-adminsdk-fbsvc-a3bf0dd37b.json'
MAIN3_API_BASE_URL = 'https://tesisdeteccion.ddns.net/api' # ¡ACTUALIZA ESTO CON TU DOMINIO DDNS!

# Datos de usuario y dispositivo de prueba
TEST_USER_EMAIL = 'cliente.prueba@example.com' # ¡Asegúrate que este usuario exista en Firestore!
TEST_DEVICE_ID = 'camera001' # ¡Asegúrate que este dispositivo esté en la lista 'devices' del usuario!

# ========== INICIALIZACIÓN FIREBASE ==========
try:
    cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("[INFO] Firebase Admin SDK inicializado correctamente.")
except Exception as e:
    print(f"[ERROR] Error al inicializar Firebase Admin SDK: {e}")
    exit()

# ========== LLAMAR A LA API DE NOTIFICACIONES EN MAIN3.PY (PARA DISPARAR GCF) ==========
def trigger_fcm_via_main3(user_email, title, body, image_url=None, custom_data=None):
    try:
        payload = {
            "user_email": user_email,
            "title": title,
            "body": body,
            "image_url": image_url if image_url else "",
            "data": custom_data if custom_data else {}
        }
        response = requests.post(f"{MAIN3_API_BASE_URL}/send_notification_via_gcf", json=payload)
        response.raise_for_status() # Lanza un error para códigos de estado 4xx/5xx
        
        print(f"✅ Petición de notificación enviada a main3.py para {user_email}. Status: {response.status_code}, Respuesta: {response.json()}")
    except requests.exceptions.RequestException as e:
        print(f"❌ Error al enviar petición de notificación a main3.py: {e}")
    except Exception as e:
        print(f"❌ Error inesperado en trigger_fcm_via_main3: {e}")

# ========== FUNCIÓN PRINCIPAL DE PRUEBA ==========
def main():
    print(f"[INFO] Iniciando prueba de notificación para {TEST_USER_EMAIL} con dispositivo {TEST_DEVICE_ID}.")
    
    # Simular una imagen URL pública (puedes usar una real si quieres)
    test_image_url = "https://storage.googleapis.com/security-cam-f322b.appspot.com/placeholder_known.jpg" # Usa una URL de imagen pública real si quieres
    
    # Datos de la notificación
    title = "¡Prueba de Notificación!"
    body = f"Esto es una prueba de FCM desde fire.py para el dispositivo {TEST_DEVICE_ID}."
    custom_data = {
        "event_type": "test_notification",
        "device_id": TEST_DEVICE_ID,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    # Llama a la función para disparar la notificación
    trigger_fcm_via_main3(TEST_USER_EMAIL, title, body, image_url=test_image_url, custom_data=custom_data)

    print("[INFO] Prueba de notificación finalizada. Revisa los logs de main3.py y la GCF, y tu emulador.")

if __name__ == "__main__":
    main()