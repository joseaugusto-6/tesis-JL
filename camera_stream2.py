# ==============================================================================
# AI SECURITY CAM - SCRIPT DEL CLIENTE DE CÁMARA (IoT)
# ==============================================================================
# Este script se ejecuta en un dispositivo local (PC o Raspberry Pi) con una
# webcam. Su única responsabilidad es capturar imágenes y enviarlas al
# servidor backend, además de obedecer los comandos recibidos vía MQTT.
# ------------------------------------------------------------------------------

import cv2
import time
import requests
import paho.mqtt.client as mqtt

# ==============================================================================
# SECCIÓN DE CONFIGURACIÓN GLOBAL
# ------------------------------------------------------------------------------
# Aquí se definen todas las variables para personalizar el comportamiento de la cámara.

# --- Configuración de la Cámara ---
CAMERA_INDEX = 0 + cv2.CAP_DSHOW  # Índice de la webcam. CAP_DSHOW es para un inicio más rápido en Windows.
CAMERA_ID_PC = "camera001"         # Identificador único para esta cámara. Debe coincidir con el de la app.
CAMERA_FPS = 10                    # Fotogramas por segundo para el modo de video en vivo.

# --- Configuración del Servidor MQTT y API ---
MQTT_BROKER_IP = "34.69.206.32"    # IP pública de tu Máquina Virtual donde corre Mosquitto.
MQTT_BROKER_PORT = 1883
VM_STREAM_UPLOAD_URL = "https://tesisdeteccion.ddns.net/api/stream_upload" # URL completa del endpoint en el servidor.

# --- Tópicos MQTT ---
# Canales de comunicación para comandos y estado.
MQTT_COMMAND_TOPIC = f"camera/commands/{CAMERA_ID_PC}" # Canal para recibir órdenes.
MQTT_STATUS_TOPIC = f"camera/status/{CAMERA_ID_PC}"     # Canal para reportar su estado.
MQTT_QOS = 1 # Calidad de Servicio: 1 asegura que los mensajes lleguen al menos una vez.

# ==============================================================================
# SECCIÓN DE VARIABLES DE ESTADO
# ------------------------------------------------------------------------------
# Variables globales que controlan el comportamiento del script en tiempo real.

current_mode = "STREAMING_MODE"  # Modo inicial: 'STREAMING_MODE' o 'CAPTURE_MODE'.
is_camera_on = True              # Estado inicial de encendido/apagado.

# --- Variables para controlar el tiempo ---
last_capture_time = 0            # Registra cuándo se tomó la última foto en modo captura.
CAPTURE_INTERVAL_SECONDS = 5     # Intervalo en segundos para el modo captura.
last_status_publish_time = 0     # Registra cuándo se envió el último reporte de estado.
STATUS_PUBLISH_INTERVAL_SECONDS = 20 # Intervalo para enviar reportes de estado.

# ==============================================================================
# SECCIÓN DE FUNCIONES DE MQTT (CALLBACKS)
# ------------------------------------------------------------------------------
# Estas funciones se ejecutan automáticamente cuando ocurren eventos de MQTT.

def on_connect(client, userdata, flags, rc):
    """Se ejecuta cuando el cliente se conecta exitosamente al broker MQTT."""
    if rc == 0:
        print(f"[MQTT] Conectado exitosamente al broker en {MQTT_BROKER_IP}")
        # Se suscribe a los tópicos de comandos para poder recibir órdenes.
        client.subscribe(MQTT_COMMAND_TOPIC, qos=MQTT_QOS)
        client.subscribe(f"camera/power/{CAMERA_ID_PC}", qos=MQTT_QOS)
        print(f"[MQTT] Suscrito a los tópicos de comandos.")
        # Publica su estado inicial inmediatamente después de conectar.
        status_payload = f"Modo: {current_mode}; Power: {'ON' if is_camera_on else 'OFF'}"
        client.publish(MQTT_STATUS_TOPIC, payload=status_payload, qos=MQTT_QOS, retain=True)
    else:
        print(f"[MQTT] Falló la conexión, código de retorno: {rc}")

def on_disconnect(client, userdata, rc):
    """Se ejecuta si la conexión con el broker se pierde inesperadamente."""
    if rc != 0:
        print("[MQTT] ¡Conexión perdida! El script intentará reconectar automáticamente.")

def on_message(client, userdata, msg):
    """Se ejecuta cada vez que llega un mensaje en un tópico al que estamos suscritos."""
    global current_mode, is_camera_on
    command = msg.payload.decode("utf-8").strip().upper()
    print(f"[MQTT] Comando recibido en '{msg.topic}': '{command}'")

    if msg.topic == MQTT_COMMAND_TOPIC:
        if command in ["STREAMING_MODE", "STREAM"]:
            current_mode = "STREAMING_MODE"
            print("[INFO] Cambiando a MODO STREAMING.")
        elif command in ["CAPTURE_MODE", "CAPTURE"]:
            current_mode = "CAPTURE_MODE"
            print("[INFO] Cambiando a MODO CAPTURA.")
        else:
            print(f"[WARN] Comando de modo desconocido: {command}")
    
    elif msg.topic == f"camera/power/{CAMERA_ID_PC}":
        if command == "ON":
            is_camera_on = True
            print("[INFO] Cámara ENCENDIDA.")
        elif command == "OFF":
            is_camera_on = False
            print("[INFO] Cámara APAGADA.")
        else:
            print(f"[WARN] Comando de encendido desconocido: {command}")
    
    # Después de cualquier comando, publica inmediatamente el nuevo estado.
    status_payload = f"Modo: {current_mode}; Power: {'ON' if is_camera_on else 'OFF'}"
    client.publish(MQTT_STATUS_TOPIC, payload=status_payload, qos=MQTT_QOS, retain=True)

# ==============================================================================
# SECCIÓN DEL BUCLE PRINCIPAL DE OPERACIÓN
# ------------------------------------------------------------------------------

def camera_operation_loop(mqtt_client):
    """
    Bucle principal que se ejecuta constantemente para manejar la cámara y enviar imágenes.
    """
    global last_capture_time, current_mode, is_camera_on, last_status_publish_time
    camera = None

    while True:
        try:
            current_time = time.time()

            # --- Reporte de Estado Periódico ---
            if (current_time - last_status_publish_time) >= STATUS_PUBLISH_INTERVAL_SECONDS:
                status_payload = f"Modo: {current_mode}; Power: {'ON' if is_camera_on else 'OFF'}"
                mqtt_client.publish(MQTT_STATUS_TOPIC, payload=status_payload, qos=MQTT_QOS, retain=True)
                print(f"[MQTT] Reporte de estado periódico enviado: {status_payload}")
                last_status_publish_time = current_time

            # --- Lógica de Encendido/Apagado de la Cámara ---
            if not is_camera_on:
                if camera is not None and camera.isOpened():
                    camera.release()
                    camera = None
                    print("[INFO] Cámara liberada y en modo de espera.")
                time.sleep(2) # Pausa para no consumir CPU mientras está apagada.
                continue

            if camera is None or not camera.isOpened():
                print("[INFO] Intentando iniciar hardware de la cámara...")
                camera = cv2.VideoCapture(CAMERA_INDEX)
                if not camera.isOpened():
                    print("[ERROR] Falló al abrir la cámara. Reintentando...")
                    time.sleep(2)
                    continue
                print("[INFO] Hardware de la cámara iniciado correctamente.")
            
            # --- Captura y Envío de Imágenes ---
            success, frame = camera.read()
            if not success:
                print("[WARN] No se pudo leer un frame de la cámara.")
                continue

            # Decidimos si debemos enviar este frame al servidor.
            should_send = False
            if current_mode == "STREAMING_MODE":
                should_send = True
            elif current_mode == "CAPTURE_MODE":
                if (current_time - last_capture_time) >= CAPTURE_INTERVAL_SECONDS:
                    should_send = True
                    last_capture_time = current_time
            
            if should_send:
                ret, buffer = cv2.imencode('.jpg', frame)
                if ret:
                    jpeg_bytes = buffer.tobytes()
                    try:
                        # Lógica de envío unificada: siempre se envía al servidor.
                        files = {'frame': ('frame.jpg', jpeg_bytes, 'image/jpeg')}
                        data = {'camera_id': CAMERA_ID_PC, 'mode': current_mode}
                        
                        response = requests.post(VM_STREAM_UPLOAD_URL, files=files, data=data)
                        response.raise_for_status() # Lanza un error si la respuesta no es 200 OK.
                        
                        print(f"[OK] Frame enviado al servidor en modo: {current_mode}")

                    except requests.exceptions.RequestException as e:
                        print(f"[ERROR] No se pudo enviar el frame al servidor: {e}")
                else:
                    print("[WARN] No se pudo codificar el frame a JPEG.")

            # Pausa para controlar la tasa de envío.
            if current_mode == "STREAMING_MODE":
                time.sleep(1.0 / CAMERA_FPS)
            else:
                time.sleep(1) # Pausa más larga en modo captura para no sobrecargar.

        except Exception as e:
            print(f"[CRITICAL] Error inesperado en el bucle principal: {e}")
            time.sleep(5) # Esperar antes de reintentar en caso de un error grave.

# ==============================================================================
# SECCIÓN DE INICIO DEL SCRIPT
# ------------------------------------------------------------------------------

def main():
    """Función principal que configura y arranca el cliente MQTT y el bucle de la cámara."""
    client_mqtt = mqtt.Client(client_id=CAMERA_ID_PC, clean_session=True)
    client_mqtt.on_connect = on_connect
    client_mqtt.on_message = on_message
    client_mqtt.on_disconnect = on_disconnect

    # Bucle de reconexión inicial para asegurar que el script no se cierre si el servidor no está listo.
    while True:
        try:
            print("[INIT] Intentando conectar al broker MQTT...")
            # Configura el "Last Will and Testament" para notificar desconexiones abruptas.
            lwt_payload = "LWT_OFFLINE" 
            client_mqtt.will_set(MQTT_STATUS_TOPIC, payload=lwt_payload, qos=MQTT_QOS, retain=True)
            
            client_mqtt.connect(MQTT_BROKER_IP, MQTT_BROKER_PORT, keepalive=10)
            print("[INIT] ¡Conectado exitosamente al broker!")
            break 
        except Exception as e:
            print(f"[INIT] Fallo al conectar: {e}. Reintentando en 10 segundos...")
            time.sleep(10)

    # Inicia el hilo de red de MQTT para que se ejecute en segundo plano.
    client_mqtt.loop_start()
    print("[INIT] Cliente MQTT iniciado. Entrando en el bucle principal de la cámara.")

    try:
        camera_operation_loop(client_mqtt)
    except KeyboardInterrupt:
        print("\n[INFO] Programa detenido por el usuario (Ctrl+C).")
    except Exception as e:
        print(f"[FATAL] Error crítico final: {e}")
    finally:
        print("[INFO] Finalizando el programa...")
        client_mqtt.loop_stop()
        client_mqtt.disconnect()
        print("[INFO] Cliente MQTT desconectado. Programa finalizado.")

if __name__ == '__main__':
    main()
