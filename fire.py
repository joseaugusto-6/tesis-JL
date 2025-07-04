import os
import time
import requests # Mantener por si acaso para futuras expansiones, aunque no se usa para FCM directo
import firebase_admin
from firebase_admin import credentials, firestore, messaging # Añadir 'messaging'
from datetime import datetime, timezone

# ========== CONFIGURACIÓN ==========
SERVICE_ACCOUNT_FILE = 'security-cam-f322b-firebase-adminsdk-fbsvc-a3bf0dd37b.json'
# MAIN3_API_BASE_URL = 'https://tesisdeteccion.ddns.net/api' # Ya no es necesario para FCM directo

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

# ========== FUNCIÓN PARA ENVIAR NOTIFICACIÓN FCM DIRECTAMENTE ==========
def send_fcm_notification_direct(user_email, title, body, image_url=None, custom_data=None):
    try:
        # 1. Obtener los tokens FCM del usuario desde Firestore
        user_doc_ref = db.collection('usuarios').document(user_email)
        user_doc = user_doc_ref.get()

        if not user_doc.exists:
            print(f"DEBUG: Usuario {user_email} no encontrado para enviar notificación.")
            return False
        
        user_data = user_doc.to_dict()
        fcm_tokens = user_data.get('fcm_tokens', [])

        if not fcm_tokens:
            print(f"DEBUG: No hay tokens FCM registrados para el usuario {user_email}.")
            return False

        # 2. Construir el mensaje FCM (se usa MulticastMessage para múltiples tokens)
        message = messaging.MulticastMessage(
            tokens=fcm_tokens,
            notification=messaging.Notification(
                title=title,
                body=body,
                image=image_url # Si se proporciona una URL de imagen para la notificación
            ),
            data=custom_data or {} # Datos personalizados, que la app móvil puede leer
        )

        # 3. Enviar el mensaje
        print(f"DEBUG: Intentando enviar notificación FCM a {len(fcm_tokens)} token(s) para {user_email}...")
        response = messaging.send_multicast(message)

        if response.success_count > 0:
            print(f"✅ Notificación enviada con éxito a {response.success_count} dispositivos para {user_email}.")
        if response.failure_count > 0:
            print(f"❌ Fallo al enviar notificación a {response.failure_count} dispositivos para {user_email}.")
            for error_response in response.responses:
                if not error_response.success:
                    print(f"  FCM Error: {error_response.exception}")
        return True

    except Exception as e:
        print(f"❌ Error al enviar notificación FCM directamente: {e}")
        return False

# ========== FUNCIÓN PRINCIPAL DE PRUEBA ==========
def main():
    print(f"[INFO] Iniciando prueba de notificación para {TEST_USER_EMAIL} con dispositivo {TEST_DEVICE_ID}.")
    
    # Simular una imagen URL pública (puedes usar una real si quieres)
    # Asegúrate que esta URL sea accesible públicamente para que la notificación pueda mostrar la imagen.
    test_image_url = "https://storage.googleapis.com/security-cam-f322b.appspot.com/placeholder_known.jpg" 
    
    # Datos de la notificación
    title = "¡Prueba Directa de Notificación!"
    body = f"Esto es una prueba de FCM directa desde fire.py para el dispositivo {TEST_DEVICE_ID}."
    custom_data = {
        "event_type": "test_notification_direct",
        "device_id": TEST_DEVICE_ID,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    # Llama a la función para disparar la notificación directamente
    send_fcm_notification_direct(TEST_USER_EMAIL, title, body, image_url=test_image_url, custom_data=custom_data)

    print("[INFO] Prueba de notificación finalizada. Revisa tu emulador.")

if __name__ == "__main__":
    main()