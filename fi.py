import firebase_admin
from firebase_admin import credentials, messaging
from datetime import datetime

# ==============================================================================
#                      ¡¡¡CONFIGURA ESTO CON TUS DATOS REALES!!!
# ==============================================================================
# RUTA EXACTA de tu archivo de credenciales de Firebase Admin SDK en la VM
SERVICE_ACCOUNT_FILE_PATH = '/home/jarrprinmunk2002/tesis-JL/security-cam-f322b-firebase-adminsdk-fbsvc-a3bf0dd37b.json'

# TU ID de Proyecto de Firebase (security-cam-f322b)
YOUR_PROJECT_ID = 'security-cam-f322b'

# EL TOKEN FCM REAL DE TU EMULADOR DE ANDROID (copiado de Firestore)
# Ejemplo: 'evW-1A89D...TU_TOKEN_FCM_AQUI...zL-oP9Q'
YOUR_FCM_TOKEN = 'ecv-sFxRQkObc1DEEFpwlp:APA91bF_s_uLgiqM57T7JHl0scxrEg0Pip-aOhZnEkfBY5L3XOMIhknCn28F-nPLdoPearQgF7YSUbIfNxULi_dehVWiyIhjlmoAFl3_lh1Kg-vW0uiHEAs' # ¡¡¡PEGA AQUÍ EL TOKEN REAL!!!
# ==============================================================================

try:
    # 1. Inicializar Firebase Admin SDK
    # Solo inicializar si no se ha hecho ya en esta ejecución del script
    if not firebase_admin._apps:
        cred = credentials.Certificate(SERVICE_ACCOUNT_FILE_PATH)
        firebase_admin.initialize_app(cred, {'projectId': YOUR_PROJECT_ID})
    
    print("[INFO] Firebase Admin SDK inicializado correctamente.")

    # 2. Construir el mensaje FCM
    message = messaging.Message(
        token=YOUR_FCM_TOKEN,
        notification=messaging.Notification(
            title="¡TEST MÍNIMO!",
            body=f"Notificación desde fire_minimal.py a las {datetime.now().strftime('%H:%M:%S')}",
        ),
        data={
            "test_type": "minimal_direct",
            "timestamp": datetime.now().isoformat()
        }
    )

    # 3. Enviar el mensaje
    print(f"DEBUG: Intentando enviar notificación a token: {YOUR_FCM_TOKEN[:10]}...") # Imprimir solo los primeros 10 chars del token
    response = messaging.send(message)

    # 4. Imprimir la respuesta
    print(f"✅ Mensaje enviado con éxito: {response}") # El 'response' es el Message ID
    print("¡Revisa tu emulador/dispositivo Android!")

except Exception as e:
    print(f"❌ Ocurrió un error al enviar la notificación: {e}")
    import traceback
    traceback.print_exc() # Esto imprimirá el traceback completo