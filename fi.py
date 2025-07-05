import firebase_admin
from firebase_admin import credentials, messaging
from firebase_admin import firestore # <-- ¡Añadir esta importación!
from datetime import datetime

# ==============================================================================
#                      ¡¡¡CONFIGURA ESTO CON TUS DATOS REALES!!!
# ==============================================================================
SERVICE_ACCOUNT_FILE_PATH = '/home/jarrprinmunk2002/tesis-JL/security-cam-f322b-firebase-adminsdk-fbsvc-a3bf0dd37b.json'
YOUR_PROJECT_ID = 'security-cam-f322b'

# Datos del usuario de prueba (debe existir en Firestore y tener fcm_tokens)
TEST_USER_EMAIL = 'cliente.prueba@example.com' # ¡Asegúrate que este usuario exista en Firestore!
# ==============================================================================

# 1. Inicializar Firebase Admin SDK (para FCM y Firestore)
try:
    if not firebase_admin._apps:
        cred = credentials.Certificate(SERVICE_ACCOUNT_FILE_PATH)
        firebase_admin.initialize_app(cred, {'projectId': YOUR_PROJECT_ID})
    
    db = firestore.client() # <-- ¡Inicializar cliente de Firestore!
    
    print("[INFO] Firebase Admin SDK inicializado correctamente para FCM y Firestore.")
except Exception as e:
    print(f"[ERROR] Error al inicializar Firebase Admin SDK: {e}")
    import traceback
    traceback.print_exc()
    exit()

# ========== FUNCIÓN PARA ENVIAR NOTIFICACIÓN FCM (AHORA LEE DE FIRESTORE) ==========
def send_fcm_notification_from_firestore(user_email, title, body, image_url=None, custom_data=None):
    try:
        print(f"DEBUG: Intentando obtener tokens FCM para el usuario: {user_email}")
        user_doc_ref = db.collection('usuarios').document(user_email)
        user_doc = user_doc_ref.get()

        if not user_doc.exists:
            print(f"DEBUG: Usuario {user_email} no encontrado en Firestore.")
            return False
        
        user_data = user_doc.to_dict()
        fcm_tokens = user_data.get('fcm_tokens', []) # Obtener el array de tokens FCM
        
        print(f"DEBUG: Tokens FCM obtenidos de Firestore para {user_email}: {fcm_tokens}")

        if not fcm_tokens:
            print(f"DEBUG: No hay tokens FCM registrados para el usuario {user_email}.")
            return False

        # Construir el mensaje FCM (usamos MulticastMessage para múltiples tokens)
        message = messaging.MulticastMessage(
            tokens=fcm_tokens, # <-- ¡Ahora se usa el token de Firestore!
            notification=messaging.Notification(
                title=title,
                body=body,
                image=image_url
            ),
            data=custom_data or {}
        )

        # Enviar el mensaje
        print(f"DEBUG: Enviando notificación a {len(fcm_tokens)} token(s) de Firestore...")
        response = messaging.send_multicast(message)

        if response.success_count > 0:
            print(f"✅ Notificación enviada con éxito a {response.success_count} dispositivos para {user_email}.")
        if response.failure_count > 0:
            print(f"❌ Fallo al enviar notificación a {response.failure_count} dispositivos para {user_email}.")
            for error_response in response.responses:
                if not error_response.success:
                    print(f"  FCM Error Detalle: {error_response.exception}")
        return True

    except Exception as e:
        print(f"❌ Ocurrió un error al enviar la notificación (después de Firestore): {e}")
        import traceback
        traceback.print_exc()
        return False

# ========== FUNCIÓN PRINCIPAL DE PRUEBA ==========
def main():
    print(f"[INFO] Iniciando prueba de notificación para {TEST_USER_EMAIL} (leyendo token de Firestore).")
    
    test_image_url = "https://storage.googleapis.com/security-cam-f322b.appspot.com/placeholder_known.jpg" # URL de imagen pública

    title = "¡TEST FIRESTORE FCM!"
    body = f"Notificación de prueba leyendo token de Firestore a las {datetime.now().strftime('%H:%M:%S')}."
    custom_data = {
        "event_type": "test_firestore_fcm",
        "timestamp": datetime.now().isoformat()
    }

    send_fcm_notification_from_firestore(TEST_USER_EMAIL, title, body, image_url=test_image_url, custom_data=custom_data)

    print("[INFO] Prueba de notificación finalizada. Revisa tu emulador.")

if __name__ == "__main__":
    main()