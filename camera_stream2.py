import cv2
import time
import requests
import numpy as np
import subprocess # Se mantiene, aunque ahora OpenCV es la fuente de frames
import paho.mqtt.client as mqtt # <-- ¡Añade esto para MQTT!
from datetime import datetime, timezone, timedelta # Para timestamps en Firebase
import firebase_admin # Para Firebase Admin SDK
from firebase_admin import credentials, storage # Para autenticación y Storage
import uuid 

# ========== CONFIGURACIÓN GLOBAL ==========
# -- Configuración de la Cámara --
CAMERA_INDEX = 0 + cv2.CAP_DSHOW # Índice de tu cámara + backend que funciona rápido
CAMERA_RESOLUTION = (480, 320)
CAMERA_ID_PC = "camera001"
CAMERA_FPS = 10 # FPS deseado para stream o captura

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

CAPTURE_INTERVAL_SECONDS = 4 # Capturar una imagen cada 4 segundos en CAPTURE_MODE
last_status_publish_time = time.time() # 
STATUS_PUBLISH_INTERVAL_SECONDS = 20 # Publicar estado cada 30 segundos
is_camera_on = True 

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
        client.subscribe(MQTT_COMMAND_TOPIC, MQTT_QOS) # Suscrito a comandos de modo
        client.subscribe(f"camera/power/{CAMERA_ID_PC}", MQTT_QOS) # <--- Suscrito a comandos de encendido/apagado
        print(f"MQTT: Suscrito a tópicos de comandos: {MQTT_COMMAND_TOPIC} y camera/power/{CAMERA_ID_PC}")
        # Enviar el estado inicial al conectarse
        status_payload = f"Modo: {current_mode}; Power: {'ON' if is_camera_on else 'OFF'}"
        client.publish(MQTT_STATUS_TOPIC, payload=status_payload, qos=MQTT_QOS, retain=True)
        print(f"MQTT: Estado inicial publicado: {status_payload}")
    else:
        print(f"MQTT: Falló la conexión, código de retorno {rc}\n")

# ============== FUNCIÓN PARA RECONEXIÓN AUTOMÁTICA =========== 
def on_disconnect(client, userdata, rc):
    if rc != 0:
        print("¡Conexión perdida de forma inesperada! Intentando reconectar automáticamente...")

#================ FUNCIÓN PARA ENCENDIDO/APAGADO =================
def on_message(client, userdata, msg):
    global current_mode, is_camera_on # Para modificar la variable global
    command = msg.payload.decode("utf-8")
    print(f"MQTT: Comando recibido en tópico '{msg.topic}': {command}")
    print(f"DEBUG: is_camera_on ANTES del comando: {is_camera_on}") # DEBUG

# COMANDO PARA MODOS
    if msg.topic == MQTT_COMMAND_TOPIC: 
        processed_command = command.strip().upper() 
        if processed_command == "STREAMING_MODE" or processed_command == "STREAM":
            current_mode = "STREAMING_MODE"
            print("Cambiando a: MODO STREAMING")
        elif processed_command == "CAPTURE_MODE" or processed_command == "CAPTURE":
            current_mode = "CAPTURE_MODE"
            print("Cambiando a: MODO CAPTURA DE IMÁGENES")
        else:
            print(f"Comando de modo desconocido: {command}")
       
        # COMANDO PARA MODOS
    elif msg.topic == f"camera/power/{CAMERA_ID_PC}": 
        power_command = command.strip().upper()
        if power_command == "ON":
            is_camera_on = True
            print("Cámara: ENCENDIENDO...")
        elif power_command == "OFF":
            is_camera_on = False
            print("Cámara: APAGANDO...")
        else:
            print(f"Comando de encendido/apagado desconocido: {command}")
    
   # Publicar el nuevo estado COMPLETO inmediatamente después de un comando
    status_payload = f"Modo: {current_mode}; Power: {'ON' if is_camera_on else 'OFF'}"
    client.publish(MQTT_STATUS_TOPIC, payload=status_payload, qos=MQTT_QOS, retain=True)
    print(f"DEBUG_POWER: is_camera_on DESPUÉS del comando: {is_camera_on}. Estado reportado: {status_payload}") # DEBUG


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
def camera_operation_loop(mqtt_client): 
    global last_capture_time, current_mode, last_status_publish_time, is_camera_on 
    camera = None # Inicializar cámara a None

    try:
        # Bucle principal de operación
        while True:
            current_time = time.time()

            # Publicar el estado COMPLETO periódicamente (modo + encendido)
            if (current_time - last_status_publish_time) >= STATUS_PUBLISH_INTERVAL_SECONDS:
                status_payload = f"Modo: {current_mode}; Power: {'ON' if is_camera_on else 'OFF'}" 
                mqtt_client.publish(MQTT_STATUS_TOPIC, payload=status_payload, qos=MQTT_QOS, retain=True) 
                print(f"MQTT: Estado periódico publicado: {status_payload}")
                last_status_publish_time = current_time 

            # --- Gestión del estado de encendido/apagado de la cámara ---
            if is_camera_on:
                if camera is None or not camera.isOpened():
                    print("Cámara: Intentando ENCENDER y abrir la cámara...")
                    camera = cv2.VideoCapture(CAMERA_INDEX)
                    if not camera.isOpened():
                        print("Error: Falló al abrir la cámara. Reintentando...")
                        time.sleep(1) # Espera antes de reintentar abrir
                        continue # Volver a intentar abrir
                    print("Cámara: Abierta correctamente.")


                # --- Lógica de Modos (STREAMING_MODE / CAPTURE_MODE) ---
                success, frame = camera.read()
                if not success:
                    print("Error: No se pudo leer el frame de la cámara. El stream se detendrá.")
                    # Intentar cerrar y reabrir si falla la lectura
                    if camera.isOpened(): camera.release()
                    camera = None
                    time.sleep(1)
                    continue 

                ret, buffer = cv2.imencode('.jpg', frame)
                if not ret:
                    print("Error: No se pudo codificar el frame como JPEG. Saltando frame.")
                    continue

                jpeg_bytes = buffer.tobytes()

                if current_mode == "STREAMING_MODE":
                    try:
                        response = requests.post(
                            VM_STREAM_UPLOAD_URL, 
                            files={'frame': ('frame.jpg', jpeg_bytes, 'image/jpeg')},
                            data={'camera_id': CAMERA_ID_PC} 
                        )
                        response.raise_for_status() 
                        # print(f"Streaming: Frame enviado a VM. Respuesta: {response.status_code}")
                    except requests.exceptions.RequestException as req_e:
                        print(f"Streaming: Error al enviar frame a VM: {req_e}")
                    time.sleep(1.0 / CAMERA_FPS) 

                elif current_mode == "CAPTURE_MODE":
                    if (current_time - last_capture_time) >= CAPTURE_INTERVAL_SECONDS:
                        print(f"Captura: Capturando y subiendo imagen. Modo: {current_mode}.")
                        upload_image_to_firebase_storage(jpeg_bytes) # Subir la imagen a Storage
                        last_capture_time = current_time 
                    time.sleep(0.1) 

                else: # Modo desconocido
                    print(f"Modo desconocido '{current_mode}'. Default a MODO STREAMING.")
                    current_mode = "STREAMING_MODE"
                    time.sleep(0.1)
            else: # is_camera_on es False (Cámara APAGADA)
                if camera is not None and camera.isOpened():
                    print("Cámara: APAGANDO y liberando recursos...")
                    camera.release() 
                    camera = None # Marcar como cerrada
                    # Publicar estado OFF al apagar
                    status_payload = f"Modo: {current_mode}; Power: OFF"
                    mqtt_client.publish(MQTT_STATUS_TOPIC, payload=status_payload, qos=MQTT_QOS, retain=True)
                    print(f"MQTT: Estado de encendido publicado: {status_payload}")
                print("Cámara APAGADA. Esperando comando ON.")
                time.sleep(7) # Pausa para no saturar CPU mientras espera encendido


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
    # 1. Configuración inicial del cliente MQTT
    client_mqtt_instance = mqtt.Client(client_id=MQTT_CLIENT_ID, clean_session=True)
    client_mqtt_instance.on_connect = on_connect
    client_mqtt_instance.on_message = on_message
    client_mqtt_instance.on_disconnect = on_disconnect # Importante para la reconexión

    # 2. Bucle de reconexión inicial
    # Este bucle se asegura de que el script no se cierre si el servidor no está disponible.
    while True:
        try:
            print("Intentando conectar al broker MQTT...")
            
            # Configura el "Last Will" ANTES de intentar conectar.
            lwt_payload = "LWT_OFFLINE" 
            client_mqtt_instance.will_set(MQTT_STATUS_TOPIC, payload=lwt_payload, qos=1, retain=True)
            
            # Intenta la conexión con un keepalive de 10 segundos
            client_mqtt_instance.connect(MQTT_BROKER_IP, MQTT_BROKER_PORT, keepalive=10)
            
            # Si la conexión tiene éxito, imprimimos un mensaje y rompemos el bucle
            print("¡Conectado exitosamente al broker MQTT!")
            break 
            
        except Exception as e:
            # Si la conexión falla, esperamos 10 segundos y el bucle reintentará
            print(f"Fallo al conectar: {e}. Reintentando en 10 segundos...")
            time.sleep(10)

    # 3. Iniciar el bucle de red de MQTT en un hilo separado
    # Esto se ejecuta solo después de que la conexión inicial fue exitosa.
    client_mqtt_instance.loop_start()
    print("MQTT: Cliente iniciado y escuchando en segundo plano.")

    # 4. Ejecución del bucle principal de la cámara
    # Lo protegemos con try/except para asegurar un cierre limpio.
    try:
        camera_operation_loop(client_mqtt_instance)
    except KeyboardInterrupt:
        # Permite detener el programa limpiamente con Ctrl+C
        print("\nPrograma detenido por el usuario.")
    except Exception as e:
        print(f"Error crítico en el bucle principal de la cámara: {e}")
    finally:
        # Este bloque se ejecuta siempre al final, asegurando que todo se cierre.
        print("Finalizando el programa...")
        client_mqtt_instance.loop_stop()
        client_mqtt_instance.disconnect()
        print("MQTT: Cliente desconectado y programa finalizado.")

if __name__ == '__main__':
    main()