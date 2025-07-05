import firebase_admin
from firebase_admin import credentials, messaging
from firebase_admin import firestore
from datetime import datetime, timezone # Asegúrate de que timezone esté aquí también

# ==============================================================================
#                      ¡¡¡CONFIGURA ESTO CON TUS DATOS REALES!!!
# ==============================================================================
SERVICE_ACCOUNT_FILE_PATH = '/home/jarrprinmunk2002/tesis-JL/security-cam-f322b-firebase-adminsdk-fbsvc-a3bf0dd37b.json'
YOUR_PROJECT_ID = 'security-cam-f322b'

TEST_USER_EMAIL = 'cliente.prueba@example.com' # ¡Asegúrate que este usuario exista en Firestore!
# ==============================================================================

# 1. Inicializar Firebase Admin SDK (para FCM y Firestore)
try:
    if not firebase_admin._apps:
        cred = credentials.Certificate(SERVICE_ACCOUNT_FILE_PATH)
        firebase_admin.initialize_app(cred, {'projectId': YOUR_PROJECT_ID})
    
    db = firestore.client()
    
    print("[INFO] Firebase Admin SDK inicializado correctamente para FCM y Firestore.")
except Exception as e:
    print(f"[ERROR] Error al inicializar Firebase Admin SDK: {e}")
    import traceback
    traceback.print_exc()
    exit()

# ========== FUNCIÓN PARA ENVIAR NOTIFICACIÓN FCM (AHORA ENVÍA INDIVIDUALMENTE) ==========
def send_fcm_notification_from_firestore(user_email, title, body, image_url=None, custom_data=None):
    success_count = 0
    failure_count = 0
    try:
        print(f"DEBUG: Intentando obtener tokens FCM para el usuario: {user_email}")
        user_doc_ref = db.collection('usuarios').document(user_email)
        user_doc = user_doc_ref.get()

        if not user_doc.exists:
            print(f"DEBUG: Usuario {user_email} no encontrado en Firestore.")
            return False
        
        user_data = user_doc.to_dict()
        fcm_tokens = user_data.get('fcm_tokens', [])
        
        print(f"DEBUG: Tokens FCM obtenidos de Firestore para {user_email}: {fcm_tokens}")

        if not fcm_tokens:
            print(f"DEBUG: No hay tokens FCM registrados para el usuario {user_email}.")
            return False

        # --- CAMBIO CLAVE: ITERAR Y ENVIAR INDIVIDUALMENTE ---
        for token in fcm_tokens:
            try:
                message = messaging.Message(
                    token=token, # Enviar a UN solo token
                    notification=messaging.Notification(
                        title=title,
                        body=body,
                        image=image_url
                    ),
                    data=custom_data or {}
                )
                print(f"DEBUG: Enviando mensaje a token: {token[:10]}...")
                response = messaging.send(message) # <-- ¡Cambio a messaging.send()!
                print(f"DEBUG: Respuesta FCM para {token[:10]}: {response}")
                success_count += 1
            except Exception as token_e:
                failure_count += 1
                print(f"❌ Fallo al enviar notificación a token {token[:10]}: {token_e}")
                import traceback
                traceback.print_exc() # Imprimir traceback para cada fallo individual de token
        # --- FIN CAMBIO CLAVE ---

        if success_count > 0:
            print(f"✅ Notificación(es) enviada(s) con éxito a {success_count} dispositivo(s) para {user_email}.")
        if failure_count > 0:
            print(f"❌ Fallo al enviar notificación a {failure_count} dispositivo(s) para {user_email}.")
        return success_count > 0 # Devolver True si al menos una notificación fue exitosa

    except Exception as e:
        print(f"❌ Ocurrió un error general en la función de envío de notificación: {e}")
        import traceback
        traceback.print_exc()
        return False

# ========== FUNCIÓN PRINCIPAL DE PRUEBA ==========
def main():
    print(f"[INFO] Iniciando prueba de notificación para {TEST_USER_EMAIL} (leyendo token de Firestore y enviando individualmente).")
    
    test_image_url = "https://storage.googleapis.com/security-cam-f322b.appspot.com/placeholder_known.jpg" # URL de imagen pública

    title = "¡TEST FCM INDIVIDUAL!"
    body = f"Notificación de prueba individual a las {datetime.now(timezone.utc).strftime('%H:%M:%S')}."
    custom_data = {
        "event_type": "test_individual_fcm",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    send_fcm_notification_from_firestore(TEST_USER_EMAIL, title, body, image_url=test_image_url, custom_data=custom_data)

    print("[INFO] Prueba de notificación finalizada. Revisa tu emulador.")

if __name__ == "__main__":
    main()