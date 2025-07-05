import os
import time
import requests # Mantener por si acaso para futuras expansiones (ej. API add_event)
import firebase_admin
from firebase_admin import credentials, firestore, messaging # ¡Asegúrate de que 'messaging' esté importado!
from datetime import datetime, timezone

# ========== CONFIGURACIÓN ==========
SERVICE_ACCOUNT_FILE = 'security-cam-f322b-firebase-adminsdk-fbsvc-a3bf0dd37b.json'
# Ya no necesitamos MAIN3_API_BASE_URL para FCM, pero la dejaremos si la usaremos para eventos:
# MAIN3_API_BASE_URL = 'https://tesisdeteccion.ddns.net/api' # Mantener para 'enviar_evento_a_main3' si se añade

# Datos de usuario y dispositivo de prueba (usados aquí para testear)
TEST_USER_EMAIL = 'cliente.prueba@example.com' 
TEST_DEVICE_ID = 'camera001' 

# ========== INICIALIZACIÓN FIREBASE ==========
try:
    cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
    if not firebase_admin._apps: # Inicializar solo si no se ha hecho
        firebase_admin.initialize_app(cred) # Solo credenciales, project_id no es necesario si está en JSON
    db = firestore.client()
    print("[INFO] Firebase Admin SDK inicializado correctamente para Firestore.")
except Exception as e:
    print(f"[ERROR] Error al inicializar Firebase Admin SDK para Firestore: {e}")
    exit()

# ========== FUNCIÓN PARA ENVIAR NOTIFICACIÓN FCM DIRECTAMENTE ==========
def send_fcm_notification_direct(user_email, title, body, image_url=None, custom_data=None):
    try:
        # 1. Obtener los tokens FCM del usuario desde Firestore
        user_doc_ref = db.collection('usuarios').document(user_email)
        user_doc = user_doc_ref.get()

        if not user_doc.exists:
            print(f"DEBUG_FCM: Usuario {user_email} no encontrado para enviar notificación.")
            return False
        
        user_data = user_doc.to_dict()
        fcm_tokens = user_data.get('fcm_tokens', [])

        if not fcm_tokens:
            print(f"DEBUG_FCM: No hay tokens FCM registrados para el usuario {user_email}.")
            return False

        # 2. Construir el mensaje FCM (usamos MulticastMessage para múltiples tokens)
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
        print(f"DEBUG_FCM: Intentando enviar notificación FCM a {len(fcm_tokens)} token(s) para {user_email}...")
        response = messaging.send_multicast(message)

        if response.success_count > 0:
            print(f"✅ Notificación FCM enviada con éxito a {response.success_count} dispositivos para {user_email}.")
        if response.failure_count > 0:
            print(f"❌ Fallo al enviar notificación FCM a {response.failure_count} dispositivos para {user_email}.")
            for error_response in response.responses:
                if not error_response.success:
                    print(f"  FCM Error Detalle: {error_response.exception}")
                    # Considerar limpiar tokens inválidos aquí si error_response.exception es 'messaging/invalid-argument'
        return True

    except Exception as e:
        print(f"❌ Error al enviar notificación FCM directamente: {e}")
        import traceback
        traceback.print_exc()
        return False

# ========== FUNCIÓN PRINCIPAL DE PRUEBA ==========
def main():
    print(f"[INFO] Iniciando prueba de notificación para {TEST_USER_EMAIL} con dispositivo {TEST_DEVICE_ID}.")
    
    # Simular una imagen URL pública (usa una real que tengas pública si quieres)
    test_image_url = "https://storage.googleapis.com/security-cam-f322b.appspot.com/placeholder_known.jpg" # URL de imagen pública

    title = "¡TEST FCM Directo!"
    body = f"Notificación de prueba directa para {TEST_DEVICE_ID} a las {datetime.now(timezone.utc).strftime('%H:%M:%S')}."
    custom_data = {
        "event_type": "test_direct_notification",
        "device_id": TEST_DEVICE_ID,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    send_fcm_notification_direct(TEST_USER_EMAIL, title, body, image_url=test_image_url, custom_data=custom_data)

    print("[INFO] Prueba de notificación finalizada. Revisa tu emulador.")

if __name__ == "__main__":
    main()