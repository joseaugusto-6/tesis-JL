import cv2
import time
import requests
import numpy as np
import subprocess # Se mantiene, aunque ahora OpenCV es la fuente de frames
import paho.mqtt.client as mqtt # <-- ¡Añade esto para MQTT!
from datetime import datetime, timezone, timedelta # Para timestamps en Firebase
import firebase_admin # Para Firebase Admin SDK
from firebase_admin import credentials, storage # Para autenticación y Storage

# ========== CONFIGURACIÓN GLOBAL ==========
# -- Configuración de la Cámara --
CAMERA_INDEX = 0 + cv2.CAP_DSHOW # Índice de tu cámara + backend que funciona rápido
CAMERA_RESOLUTION = (640, 480)
CAMERA_ID_PC = "camera001"
CAMERA_FPS = 30 # FPS deseado para stream o captura

# --- NUEVO: Definir la zona horaria de Caracas ---
CARACAS_TIMEZONE = timezone(timedelta(hours=-4)) # GMT-4 (ejemplo, ajusta si es diferente)

# -- Configuración MQTT --
MQTT_BROKER_IP = "34.69.206.32" # <--- ¡ACTUALIZA ESTO con la IP pública de tu VM!
MQTT_BROKER_PORT = 1883
MQTT_CLIENT_ID = "camera_pc_client" # ID único para este cliente MQTT
MQTT_COMMAND_TOPIC = f"camera/commands/{CAMERA_ID_PC}" # <--- Tópico al que esta cámara escucha (camera_id)
MQTT_STATUS_TOPIC = f"camera/status/{CAMERA_ID_PC}" # <--- Tópico para enviar su estado
MQTT_QOS = 1 # Calidad de Servicio: 0 (At most once), 1 (At least once), 2 (Exactly once)

# -- Configuración de la VM (main3.py) --
VM_STREAM_UPLOAD_URL = "https://tesisdeteccion.ddns.net/api/stream_upload" # <--- ¡ACTUALIZA ESTO!

# -- Configuración de Firebase Storage para subida directa (Modo Captura) --
# Este archivo JSON debe estar en la PC, en el mismo directorio que camera_stream.py
FIREBASE_SERVICE_ACCOUNT_PATH_PC = "D:/TESIS/AAA THE LAST DANCE/security-cam-f322b-firebase-adminsdk-fbsvc-a3bf0dd37b.json" # <--- ¡ACTUALIZA ESTO!
FIREBASE_STORAGE_BUCKET_NAME = "security-cam-f322b.firebasestorage.app" # <--- ¡ACTUALIZA ESTO!
FIREBASE_UPLOAD_PATH_CAPTURE_MODE = f"uploads/{CAMERA_ID_PC}/" # <--- Carpeta para las fotos capturadas (camera_id)

# ========== VARIABLES DE ESTADO ==========
# Modos de operación: 'STREAMING_MODE', 'CAPTURE_MODE'
current_mode = "STREAMING_MODE" # Modo inicial al arrancar
last_capture_time = time.time() # Para controlar el tiempo entre capturas
CAPTURE_INTERVAL_SECONDS = 5 # Capturar una imagen cada 5 segundos en CAPTURE_MODE

# ========== INICIALIZACIÓN FIREBASE ADMIN SDK (PARA SUBIR A STORAGE) ==========
firebase_app_pc = None
try:
    # Solo inicializar si no se ha hecho ya (para evitar errores si se ejecuta más de una vez)
    if not firebase_admin._apps:
        cred_pc = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_PATH_PC)
        firebase_app_pc = firebase_admin.initialize_app(cred_pc, {'storageBucket': FIREBASE_STORAGE_BUCKET_NAME}, name='camera_pc_app')
    else:
        # Si ya se inicializó, obtener la instancia existente
        firebase_app_pc = firebase_admin.get_app(name='camera_pc_app')

    bucket_pc = storage.bucket(app=firebase_app_pc) # Bucket para la subida directa
    print("[INFO] Firebase Admin SDK (PC) inicializado correctamente para Storage.")
except Exception as e:
    print(f"[ERROR] Error al inicializar Firebase Admin SDK (PC): {e}")
    print("La subida de imágenes a Storage no funcionará.")
    # No salir, permitir que al menos el stream_upload a VM funcione


# ========== FUNCIÓN DE CALLBACK MQTT ==========
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"MQTT: Conectado al broker {MQTT_BROKER_IP}:{MQTT_BROKER_PORT}")
        client.subscribe(MQTT_COMMAND_TOPIC, MQTT_QOS)
        print(f"MQTT: Suscrito a tópico de comandos: {MQTT_COMMAND_TOPIC}")
        # Enviar el estado inicial al conectarse
        client.publish(MQTT_STATUS_TOPIC, payload=f"Modo: {current_mode}", qos=MQTT_QOS, retain=True)
        print(f"MQTT: Estado inicial publicado: {current_mode}")
    else:
        print(f"MQTT: Falló la conexión, código de retorno {rc}\n")

def on_message(client, userdata, msg):
    global current_mode # Para modificar la variable global
    command = msg.payload.decode("utf-8")
    print(f"MQTT: Comando recibido en tópico '{msg.topic}': {command}")

    if msg.topic == MQTT_COMMAND_TOPIC:
        if command == "MODE_STREAM":
            current_mode = "STREAMING_MODE"
            print("Cambiando a: MODO STREAMING")
        elif command == "MODE_CAPTURE":
            current_mode = "CAPTURE_MODE"
            print("Cambiando a: MODO CAPTURA DE IMÁGENES")
        else:
            print(f"Comando desconocido: {command}")

        # Publicar el nuevo estado
        client.publish(MQTT_STATUS_TOPIC, payload=f"Modo: {current_mode}", qos=MQTT_QOS, retain=True)

# ========== FUNCIÓN PARA SUBIR IMAGEN A FIREBASE STORAGE ==========
def upload_image_to_firebase_storage(image_bytes):
    try:
        filename = f"{FIREBASE_UPLOAD_PATH_CAPTURE_MODE}{CAMERA_ID_PC}_{datetime.now(CARACAS_TIMEZONE).strftime('%Y%m%d_%H%M%S')}.jpg"
        blob = bucket_pc.blob(filename)
        blob.upload_from_string(image_bytes, content_type='image/jpeg')
        blob.make_public() # Hacer la imagen pública si es necesario para fi.py o el historial
        print(f"Firebase Storage: Imagen capturada subida a {blob.public_url}")
        return blob.public_url
    except Exception as e:
        print(f"❌ Error al subir imagen a Firebase Storage: {e}")
        return None

# ========== FUNCIÓN PRINCIPAL DE OPERACIÓN DE CÁMARA ==========
def camera_operation_loop():
    global last_capture_time, current_mode # Necesario para modificar globales
    camera = cv2.VideoCapture(CAMERA_INDEX) # Usar el índice y backend configurado

    try:
        if not camera.isOpened():
            print("Error: No se pudo abrir la cámara. Asegúrate de que no esté en uso por otra aplicación.")
            # Si la cámara no abre, no hay nada que hacer, salir
            return 

        print(f"Cámara abierta correctamente. Iniciando operación en {current_mode}.")

        while True:
            success, frame = camera.read()
            if not success:
                print("Error: No se pudo leer el frame de la cámara. El bucle se detendrá.")
                break 

            # Convertir a JPEG para procesamiento y envío/subida
            ret, buffer = cv2.imencode('.jpg', frame)
            if not ret:
                print("Error: No se pudo codificar el frame como JPEG. Saltando frame.")
                continue

            jpeg_bytes = buffer.tobytes()

            # --- Lógica de Modos ---
            if current_mode == "STREAMING_MODE":
                # Enviar el frame a la VM para el stream en vivo
                try:
                    response = requests.post(
                        VM_STREAM_UPLOAD_URL, 
                        files={'frame': ('frame.jpg', jpeg_bytes, 'image/jpeg')},
                        data={'camera_id': MQTT_COMMAND_TOPIC.split('/')[-1]} # Extraer camera_id de config
                    )
                    response.raise_for_status() 
                    # print(f"Streaming: Frame enviado a VM. Respuesta: {response.status_code}")
                except requests.exceptions.RequestException as req_e:
                    print(f"❌ Streaming: Error al enviar frame a VM: {req_e}")
                time.sleep(1.0 / CAMERA_FPS) # Esperar para mantener el framerate

            elif current_mode == "CAPTURE_MODE":
                current_time = time.time()
                if (current_time - last_capture_time) >= CAPTURE_INTERVAL_SECONDS:
                    print(f"Captura: Capturando y subiendo imagen. Modo: {current_mode}")
                    image_public_url = upload_image_to_firebase_storage(jpeg_bytes)
                    # Opcional: Aquí podrías enviar un evento a main3.py/api/events/add
                    # con los datos de la imagen capturada si fi.py no lo hará por su cuenta.
                    last_capture_time = current_time # Reiniciar el contador
                time.sleep(0.1) # Pausa más corta en modo captura para no perder frames si el intervalo es largo.

            else: # Modo desconocido, por defecto hacer streaming
                print(f"Modo desconocido '{current_mode}'. Default a MODO STREAMING.")
                current_mode = "STREAMING_MODE"
                time.sleep(0.1)


    except Exception as e:
        print(f"Error general en camera_operation_loop: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if camera is not None and camera.isOpened():
            camera.release() 
            print("Cámara liberada.")
        else:
            print("La cámara no se había abierto o ya estaba liberada.")

# ========== FUNCIÓN DE INICIO ==========
def main():
    # Inicializar el cliente MQTT
    client = mqtt.Client(client_id=MQTT_CLIENT_ID, clean_session=True)
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(MQTT_BROKER_IP, MQTT_BROKER_PORT, 60)
        client.loop_start() # Iniciar el bucle de MQTT en un hilo separado
        print("MQTT: Cliente iniciado en un hilo separado.")

        # Iniciar el bucle de operación de la cámara
        camera_operation_loop()

    except Exception as e:
        print(f"Error en la ejecución principal: {e}")
    finally:
        client.loop_stop() # Detener el bucle de MQTT
        client.disconnect() # Desconectar el cliente MQTT
        print("MQTT: Cliente desconectado.")

if __name__ == '__main__':
    main()