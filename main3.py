import os
import re
import numpy as np
from flask import Flask, request, render_template, redirect, url_for, session, flash, jsonify, Response
from google.cloud import storage, firestore
import paho.mqtt.client as mqtt # <-- ¡Añade esto para MQTT!
from datetime import datetime, timedelta, timezone 
from werkzeug.security import generate_password_hash, check_password_hash
from flask_jwt_extended import create_access_token, JWTManager, jwt_required, get_jwt_identity
import requests
import threading 
import time 
import queue 
import base64 
import uuid 
import json # Para generar tokens de sesión de stream
import io 
import redis

# Inicializaciones básicas
app = Flask(__name__)
app.secret_key = 'supersecretkey'  # Cambia esto por algo más seguro en producción

# Configuración JWT
app.config["JWT_SECRET_KEY"] = "tu_clave_jwt_super_segura_aqui" # ¡CAMBIA ESTO! Debe ser la misma clave que usaste antes.
jwt = JWTManager(app)
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(days=30)

# Configuración de Google Cloud
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "security-cam-f322b-8adcddbcb279.json"
BUCKET_NAME = "security-cam-f322b.firebasestorage.app"

# URL de tu Google Cloud Function para enviar FCM
CLOUD_FUNCTION_FCM_URL = "https://sendfcmnotification-614861377558.us-central1.run.app" # <-- ¡PEGA AQUI LA URL REAL DE TU GCF!

# ========== CONFIGURACIÓN PARA STREAM DE VIDEO (Polling de Imágenes) ==========
# Diccionario global para almacenar el último frame de cada cámara
# Formato: { "camera_id": {"frame": <bytes>, "timestamp": <datetime>} }
latest_frames = {}
frames_lock = threading.Lock()

# Diccionario global para almacenar tokens de sesión de stream activos
# Formato: { "session_token": {"user_id": <id>, "camera_id": <id>, "expires": <datetime>} }
stream_sessions = {}
sessions_lock = threading.Lock()

# Diccionario global para almacenar el estado actual de cada cámara
# Formato: { "camera_id": {"mode": "STREAMING_MODE", "timestamp": <datetime>} }
camera_status = {}
camera_status_lock = threading.Lock()

# Flag para indicar si hay un stream activo (si se están recibiendo frames de la cámara fuente)
is_streaming_active = False
# Timestamp del último frame recibido (para detectar inactividad de la cámara fuente)
last_frame_received_time = time.time() # Inicializa con tiempo actual

# Imagen estática de "Stream No Disponible" (bytes JPEG codificados en base64)
# Esta es una imagen de 1x1 pixel negro.
STATIC_NO_STREAM_IMAGE_BASE64 = b'/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAYEBQYFBAYGBQYHBwYIChAKCgkJChQODwwQFxQYGBcUFhYaHSUfGhsjHBYWICwgIyYnKSopGR8tMC0oMCUoKSj/2wBDAQcHBwoIChMKChMoGhYaKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCj/wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAD/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFAEBAAAAAAAAAAAAAAAAAAAAAP/EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwAAARECEQD/AJAAAAAAAAA//9k='
STATIC_NO_STREAM_IMAGE_BYTES = base64.b64decode(STATIC_NO_STREAM_IMAGE_BASE64) # Decodifica a bytes

# ========== CONFIGURACIÓN MQTT PARA FLASK ==========
MQTT_BROKER_IP_INTERNAL = "127.0.0.1" # El broker está en la misma VM
MQTT_BROKER_PORT_INTERNAL = 1883
MQTT_CLIENT_ID_FLASK = "flask_control_client" # ID único para el cliente MQTT de Flask
MQTT_QOS_INTERNAL = 1

# Inicializa el cliente de Storage y Firestore
storage_client = storage.Client()
bucket = storage_client.get_bucket(BUCKET_NAME) 
db = firestore.Client()
# Conexión a la base de datos en memoria Redis
redis_client = redis.Redis(host='localhost', port=6379, db=0)

# Define la zona horaria de Caracas (o la que te sea relevante)
CARACAS_TIMEZONE = timezone(timedelta(hours=-4))

# Inicializar cliente MQTT para Flask
flask_mqtt_client = mqtt.Client(client_id=MQTT_CLIENT_ID_FLASK, clean_session=True)

def on_mqtt_connect_flask(client, userdata, flags, rc):
    if rc == 0:
        print(f"MQTT (Flask): Conectado al broker {MQTT_BROKER_IP_INTERNAL}:{MQTT_BROKER_PORT_INTERNAL}")
        # Suscribirse al tópico de estado de la cámara al conectar
        client.subscribe("camera/status/#", MQTT_QOS_INTERNAL) # Suscribirse a todos los tópicos de estado
        print(f"MQTT (Flask): Suscrito a tópicos de estado: camera/status/#")
    else:
        print(f"MQTT (Flask): Falló la conexión, código de retorno {rc}\n")

def on_mqtt_message_flask(client, userdata, msg):
    global camera_status # Acceder a la variable global
    topic = msg.topic
    payload = msg.payload.decode("utf-8")

    if topic.startswith("camera/status/"):
        camera_id = topic.split('/')[-1]

        # --- INICIO DE LA CORRECCIÓN CON REGEX ---
        # Usamos regex para extraer los valores de forma segura, ignorando espacios.
        # r'Modo:\s*([\w_]+)' busca "Modo:", luego cualquier espacio (\s*), y captura el nombre del modo.
        mode_match = re.search(r'Modo:\s*([\w_]+)', payload)
        # r'Power:\s*(ON|OFF)' busca "Power:", cualquier espacio, y captura ON u OFF.
        power_match = re.search(r'Power:\s*(ON|OFF)', payload, re.IGNORECASE) # IGNORECASE por si acaso

        mode_status = mode_match.group(1) if mode_match else "UNKNOWN"
        power_status_str = power_match.group(1) if power_match else "OFF"
        
        # Convertimos el string "ON" a un booleano True.
        power_status = (power_status_str.upper() == "ON")
        # --- FIN DE LA CORRECCIÓN ---

        with camera_status_lock:
            current_cam_status = camera_status.get(camera_id, {})
            current_cam_status['mode'] = mode_status
            current_cam_status['is_on'] = power_status # ¡Ahora con el valor booleano correcto!
            current_cam_status['timestamp'] = datetime.now()
            camera_status[camera_id] = current_cam_status

        # Este log ahora debería mostrar "Power: True" cuando corresponda.
        print(f"MQTT (Flask): Estado de {camera_id} actualizado a Modo: {mode_status}, Power: {power_status}")

flask_mqtt_client.on_connect = on_mqtt_connect_flask
flask_mqtt_client.on_message = on_mqtt_message_flask # Añade la función on_message

try:
    flask_mqtt_client.connect(MQTT_BROKER_IP_INTERNAL, MQTT_BROKER_PORT_INTERNAL, 60)
    flask_mqtt_client.loop_start() # Iniciar el bucle de MQTT en un hilo separado
    print("MQTT (Flask): Cliente iniciado en un hilo separado para publicar/suscribir.")
except Exception as e:
    print(f"MQTT (Flask): Error al conectar el cliente MQTT: {e}")

# ---------------------- FIRESTORE USUARIOS --------------------------
def firestore_user_exists(email):
    """Verifica si ya existe un usuario con este email"""
    usuarios = db.collection('usuarios').where('email', '==', email).stream()
    return any(True for _ in usuarios)

def firestore_create_user(name, email, password):
    """Crea un nuevo usuario en Firestore con nombre, email y hash de la contraseña"""
    password_hash = generate_password_hash(password)
    db.collection('usuarios').document(email).set({
        "name": name,
        "email": email,
        "password_hash": password_hash,
        "created_at": firestore.SERVER_TIMESTAMP,
        "devices": [], # Inicializa con una lista vacía de dispositivos
        "fcm_tokens": [], # Inicializa con una lista vacía de tokens FCM
        "notification_preference": "all" 
    })

def firestore_check_user(email, password):
    """Verifica las credenciales de usuario"""
    doc_ref = db.collection('usuarios').document(email)
    doc = doc_ref.get()
    if not doc.exists:
        return False
    user = doc.to_dict()
    return check_password_hash(user["password_hash"], password)

# ------------------------ FLASK RUTAS WEB (EXISTENTES) -------------------------------

@app.route("/")
def home_page():
    return render_template("index.html")

# ----------- API PARA LAS CÁMARAS (NO TOCAR) -------------
@app.route('/upload', methods=['POST'])
def upload_image():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No se recibió archivo 'file'"}), 400
        file = request.files['file']
        device_id = request.form.get('device_id', 'unknown')
        now = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        filename = f"{device_id}_{now}.jpg"
        blob = bucket.blob(f"uploads/{device_id}/{filename}")
        blob.upload_from_file(file, content_type='image/jpeg')
        return jsonify({"message": "Imagen recibida", "filename": filename}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ------------------------ RUTAS API PARA APP MÓVIL -------------------------------

@app.route("/api/login", methods=["POST"])
def api_login():
    email = request.json.get("email", None)
    password = request.json.get("password", None)

    if not email or not password:
        return jsonify({"msg": "Faltan email o contraseña"}), 400

    if firestore_check_user(email, password):
        access_token = create_access_token(identity=email)
        return jsonify({"access_token": access_token}), 200
    else:
        return jsonify({"msg": "Credenciales inválidas"}), 401

@app.route("/api/register", methods=["POST"])
def api_register():
    name = request.json.get("name", None)
    email = request.json.get("email", None)
    password = request.json.get("password", None)

    if not name or not email or not password:
        return jsonify({"msg": "Completa todos los campos"}), 400

    if firestore_user_exists(email):
        return jsonify({"msg": "Ya existe un usuario con ese correo"}), 409
    
    try:
        firestore_create_user(name, email, password)
        return jsonify({"msg": "Usuario registrado correctamente"}), 201
    except Exception as e:
        app.logger.error(f"Error al registrar usuario: {e}")
        return jsonify({"msg": f"Error al registrar usuario: {str(e)}"}), 500

@app.route("/api/protected", methods=["GET"])
@jwt_required()
def protected():
    current_user = get_jwt_identity()
    return jsonify({"message": f"Bienvenido, {current_user}! Acceso concedido."}), 200

@app.route("/api/fcm_token", methods=["POST"])
@jwt_required()
def update_fcm_token():
    try:
        current_user_email = get_jwt_identity()
        data = request.json
        fcm_token = data.get("fcm_token", None)

        if not fcm_token:
            return jsonify({"msg": "Falta el token FCM."}), 400

        user_doc_ref = db.collection('usuarios').document(current_user_email)
        user_doc = user_doc_ref.get()

        if not user_doc.exists:
            app.logger.warning(f"get_user_devices: User {current_user_email} not found.")
            return jsonify({"msg": "Usuario no encontrado."}), 404
        
        user_data = user_doc.to_dict()
        current_tokens = user_data.get('fcm_tokens', [])

        if fcm_token not in current_tokens:
            current_tokens.append(fcm_token)
            user_doc_ref.update({'fcm_tokens': current_tokens})
            return jsonify({"msg": "Token FCM registrado/actualizado correctamente."}), 200
        else:
            return jsonify({"msg": "Token FCM ya existente para este usuario."}), 200
    except Exception as e:
        app.logger.error(f"Error al actualizar token FCM: {e}")
        return jsonify({"msg": f"Error interno del servidor: {str(e)}"}), 500

@app.route("/api/events/add", methods=["POST"])
def add_event():
    try:
        data = request.json
        
        required_fields = ["person_name", "timestamp", "event_type", "image_url", "device_id"]
        if not all(field in data for field in required_fields):
            app.logger.warning(f"add_event: Missing required fields: {data}")
            return jsonify({"msg": "Faltan campos obligatorios para el evento."}), 400

        try:
            event_timestamp = datetime.fromisoformat(data["timestamp"].replace('Z', '+00:00'))
        except ValueError:
            app.logger.warning(f"add_event: Invalid timestamp format: {data.get('timestamp')}")
            return jsonify({"msg": "Formato de timestamp inválido. Usa ISO 8601."}), 400

        event_data = {
            "person_name": data["person_name"],
            "timestamp": event_timestamp,
            "event_type": data["event_type"],
            "image_url": data["image_url"],
            "event_details": data.get("event_details", ""),
            "device_id": data.get("device_id", "unknown"),
            "recorded_at": firestore.SERVER_TIMESTAMP
        }
        
        db.collection('events').add(event_data)
        
        return jsonify({"msg": "Evento registrado correctamente."}), 201
    except Exception as e:
        app.logger.error(f"Error al añadir evento: {e}")
        return jsonify({"msg": f"Error interno del servidor: {str(e)}"}), 500

@app.route("/api/events/history", methods=["GET"])
@jwt_required()
def get_event_history():
    try:
        current_user_email = get_jwt_identity()

        user_doc_ref = db.collection('usuarios').document(current_user_email)
        user_doc = user_doc_ref.get()

        if not user_doc.exists:
            app.logger.warning(f"get_event_history: User {current_user_email} not found.")
            return jsonify({"msg": "Usuario no encontrado en la base de datos."}), 404
        
        user_data = user_doc.to_dict()
        user_devices = user_data.get('devices', [])

        if not user_devices:
            return jsonify({"events": []}), 200

        events_ref = db.collection('events') \
                      .where('device_id', 'in', user_devices) \
                      .order_by('timestamp', direction=firestore.Query.DESCENDING) \
                      .limit(100)

        events = []
        for doc in events_ref.stream():
            event_data = doc.to_dict()
            event_data['id'] = doc.id
            if isinstance(event_data.get('timestamp'), datetime):
                event_data['timestamp'] = event_data['timestamp'].isoformat()
            elif hasattr(event_data.get('timestamp'), 'isoformat'):
                event_data['timestamp'] = event_data['timestamp'].isoformat()
            elif hasattr(event_data.get('timestamp'), 'to_dict') and 'nanoseconds' in event_data['timestamp'].to_dict():
                event_data['timestamp'] = event_data['timestamp'].to_datetime().isoformat()
            
            events.append(event_data)
        
        return jsonify({"events": events}), 200
    except Exception as e:
        app.logger.error(f"Error al obtener historial de eventos: {e}")
        return jsonify({"msg": f"Error interno del servidor: {str(e)}"}), 500

#---------- API para obtener los datos para el dashboard ---------------
@app.route('/api/dashboard_data', methods=['GET'])
@jwt_required()
def get_dashboard_data():
    current_user_email = get_jwt_identity()
    
    user_doc_ref = db.collection('usuarios').document(current_user_email)
    user_doc = user_doc_ref.get()

    if not user_doc.exists:
        app.logger.warning(f"get_dashboard_data: User {current_user_email} not found.")
        return jsonify({"msg": "Usuario no encontrado en la base de datos."}), 404
    
    user_data = user_doc.to_dict()
    user_devices = user_data.get('devices', [])

    if not user_devices:
        return jsonify({
            'latest_events': [],
            'total_entries_today': 0,
            'alarms_today': 0
        }), 200

    # --- Lógica para obtener los últimos eventos ---
    # Filtrar por los dispositivos del usuario y ordenar por timestamp descendente
    latest_events_query = db.collection('events') \
                          .where('device_id', 'in', user_devices) \
                          .order_by('timestamp', direction=firestore.Query.DESCENDING) \
                          .limit(5) # Últimos 5 eventos para el resumen del dashboard
    
    events_list = []
    for event in latest_events_query.stream():
        event_data = event.to_dict()
        events_list.append({
            'id': event.id, # Incluir el ID del documento si es útil
            'person_name': event_data.get('person_name', 'Desconocido'),
            'event_type': event_data.get('event_type', 'unknown'),
            'timestamp': event_data.get('timestamp').isoformat() if isinstance(event_data.get('timestamp'), datetime) else event_data.get('timestamp'), # Asegurar formato ISO
            'image_url': event_data.get('image_url', ''),
            'event_details': event_data.get('event_details', ''),
            'device_id': event_data.get('device_id', 'unknown')
        })

    # --- Lógica para calcular estadísticas diarias  ---
    # Obtener la fecha de hoy en la zona horaria de Caracas
    now_caracas = datetime.now(CARACAS_TIMEZONE)
    today_start = now_caracas.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = now_caracas.replace(hour=23, minute=59, second=59, microsecond=999999)

    # Consulta para todos los eventos de hoy
    today_events_query = db.collection('events') \
                          .where('device_id', 'in', user_devices) \
                          .where('timestamp', '>=', today_start) \
                          .where('timestamp', '<=', today_end)
    
    total_entries_today = 0
    alarms_today = 0

    for event in today_events_query.stream():
        event_data = event.to_dict()
        total_entries_today += 1
        if event_data.get('event_type') in ['alarm', 'unknown_person', 'unknown_person_repeated_alarm', 'person_no_face_alarm']:
            alarms_today += 1
    
    app.logger.info(f"DEBUG_DASHBOARD: latest_events_query found {len(events_list)} events.")
    app.logger.info(f"DEBUG_DASHBOARD: latest_events_query data: {events_list}") # DEBUG para ver la data exacta
    app.logger.info(f"DEBUG_DASHBOARD: total_entries_today: {total_entries_today}, alarms_today: {alarms_today}") # DEBUG para ver los contadores
    
    return jsonify({
        'latest_events': events_list,
        'total_entries_today': total_entries_today,
        'alarms_today': alarms_today
    }), 200

# ------------------API para obtener la lista de dispositivos del usuario -------------------
@app.route('/api/user_devices', methods=['GET'])
@jwt_required()
def get_user_devices():
    current_user_email = get_jwt_identity()

    user_doc_ref = db.collection('usuarios').document(current_user_email)
    user_doc = user_doc_ref.get()

    if not user_doc.exists:
        app.logger.warning(f"get_user_devices: User {current_user_email} not found.")
        return jsonify({"msg": "Usuario no encontrado en la base de datos."}), 404

    user_data = user_doc.to_dict()
    # Obtiene la lista de dispositivos del usuario desde Firestore
    devices_from_firestore = user_data.get('devices', []) 

    # Lista para almacenar los dispositivos con su estado
    devices_with_status = []
    with camera_status_lock: # Acceder al diccionario global de estados de cámara
        for device_id in devices_from_firestore:
            status_info = camera_status.get(device_id) # Obtener el estado de la cámara
            mode = status_info['mode'] if status_info else 'UNKNOWN'
            # Puedes añadir más detalles aquí si los necesitas, ej. timestamp del estado
            devices_with_status.append({
                'id': device_id,
                'mode': mode,
                'is_active': status_info is not None and (datetime.now() - status_info['timestamp']).total_seconds() < 20, # Considerar activa si el último reporte es de hace menos de 60s
                'is_on': status_info['is_on'] if status_info and 'is_on' in status_info else False
            })

    return jsonify({"devices": devices_with_status}), 200 # Devuelve una lista de diccionarios), 200

# ------------------------ API AÑADIR NUEVO DISPOSITIVO -----------------------
@app.route('/api/add_device', methods=['POST'])
@jwt_required()
def add_device_to_user():
    try:
        current_user_email = get_jwt_identity()
        data = request.json
        device_id_to_add = data.get('device_id', None)

        if not device_id_to_add:
            return jsonify({"msg": "Falta el ID del dispositivo"}), 400

        user_doc_ref = db.collection('usuarios').document(current_user_email)
        user_doc = user_doc_ref.get()

        if not user_doc.exists:
            app.logger.warning(f"add_device_to_user: User {current_user_email} not found.")
            return jsonify({"msg": "Usuario no encontrado."}), 404
        
        user_data = user_doc.to_dict()
        current_devices = user_data.get('devices', [])

        if device_id_to_add in current_devices:
            return jsonify({"msg": "El dispositivo ya está asociado a este usuario."}), 409 # Conflict
        
        current_devices.append(device_id_to_add)
        user_doc_ref.update({'devices': current_devices})

        return jsonify({"msg": f"Dispositivo {device_id_to_add} añadido correctamente."}), 200

    except Exception as e:
        app.logger.error(f"Error al añadir dispositivo: {e}")
        return jsonify({"msg": f"Error interno del servidor: {str(e)}"}), 500

# ------------------------ API ELIMINAR DISPOSITIVO --------------------------
@app.route('/api/remove_device', methods=['POST']) # O DELETE, pero POST es más fácil con body
@jwt_required()
def remove_device_from_user():
    try:
        current_user_email = get_jwt_identity()
        data = request.json
        device_id_to_remove = data.get('device_id', None)

        if not device_id_to_remove:
            return jsonify({"msg": "Falta el ID del dispositivo"}), 400

        user_doc_ref = db.collection('usuarios').document(current_user_email)
        user_doc = user_doc_ref.get()

        if not user_doc.exists:
            app.logger.warning(f"remove_device_from_user: User {current_user_email} not found.")
            return jsonify({"msg": "Usuario no encontrado."}), 404
        
        user_data = user_doc.to_dict()
        current_devices = user_data.get('devices', [])

        if device_id_to_remove not in current_devices:
            return jsonify({"msg": "El dispositivo no está asociado a este usuario."}), 404 # Not Found
        
        current_devices.remove(device_id_to_remove) # Elimina el dispositivo de la lista
        user_doc_ref.update({'devices': current_devices})

        return jsonify({"msg": f"Dispositivo {device_id_to_remove} eliminado correctamente."}), 200

    except Exception as e:
        app.logger.error(f"Error al eliminar dispositivo: {e}")
        return jsonify({"msg": f"Error interno del servidor: {str(e)}"}), 500

# ------------------------ API PARA RECIBIR STREAM DE PC -------------------------------
@app.route('/api/stream_upload', methods=['POST'])
def stream_upload():
    try:
        if 'frame' not in request.files:
            return jsonify({"error": "No se recibió el archivo 'frame'."}), 400
        
        frame_file = request.files['frame']
        camera_id = request.form.get('camera_id')

        if not camera_id:
            return jsonify({"error": "Falta el camera_id."}), 400
            
        frame_data = frame_file.read()
        if not frame_data:
            return jsonify({"error": "El frame de video está vacío."}), 400

        # --- LÓGICA CON REDIS ---
        # 1. Guardamos el frame en Redis. La clave será, por ejemplo, "frame:camera001"
        redis_key = f"frame:{camera_id}"
        redis_client.set(redis_key, frame_data)
        # Le damos un tiempo de expiración para que no se quede para siempre si la cámara se apaga
        redis_client.expire(redis_key, 15) 
        
        # 2. Guardamos una copia en Firebase Storage para la IA (esto no cambia)
        now_str = datetime.now(CARACAS_TIMEZONE).strftime('%Y%m%d_%H%M%S')
        filename = f"{camera_id}_{now_str}.jpg"
        blob_path = f"uploads/{camera_id}/{filename}"
        bucket.blob(blob_path).upload_from_string(frame_data, content_type='image/jpeg')
        
        app.logger.info(f"Frame de {camera_id} recibido y guardado en Redis y Storage.")
        return jsonify({"message": "Frame recibido."}), 200

    except Exception as e:
        app.logger.error(f"Error en stream_upload: {e}")
        return jsonify({"error": "Error interno del servidor."}), 500

# ------------------------ FIN API PARA RECIBIR STREAM DE PC ---------------------------

# ------------------------ API PARA LLAMAR GCF Y ENVIAR FCM (SIMULADA AHORA) --------------------
@app.route("/api/send_notification_via_gcf", methods=["POST"])
def send_notification_via_gcf():
    try:
        data = request.json
        if not all(k in data for k in ['user_email', 'title', 'body']):
            app.logger.warning(f"send_notification_via_gcf: Missing required fields in request: {data}")
            return jsonify({"error": "Missing user_email, title, or body in request"}), 400

        # Si decides volver a usar GCF para FCM, descomenta esta parte
        # gcf_response = requests.post(CLOUD_FUNCTION_FCM_URL, json=data)
        # gcf_response.raise_for_status()
        # return jsonify(gcf_response.json()), gcf_response.status_code
        
        # Por ahora, simplemente devuelve una confirmación simulada si no usas GCF
        return jsonify({"message": "GCF call simulated (FCM handled directly by fi.py now)"}), 200
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Error al llamar a la GCF: {e}")
        return jsonify({"error": f"Failed to call Cloud Function: {str(e)}"}), 500
    except Exception as e:
        app.logger.error(f"Error en send_notification_via_gcf: {e}")
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500

# ------------------------ API PARA OBTENER UN TOKEN DE SESIÓN PARA EL STREAM ------------------------
@app.route('/api/get_stream_session_token', methods=['POST'])
@jwt_required()
def get_stream_session_token():
    user_id = get_jwt_identity() # Obtiene el ID del usuario del token JWT
    camera_id = request.json.get('camera_id')

    if not camera_id:
        return jsonify({"msg": "Missing camera_id"}), 400

    # TODO: En una aplicación real, aquí deberías verificar si 'user_id' está autorizado para 'camera_id'.
    # Por ahora, asumimos que si el usuario está autenticado, puede solicitar un token para cualquier cámara.

    session_token = str(uuid.uuid4()) # Genera un UUID único como token
    expires_at = datetime.now() + timedelta(minutes=5) # Token válido por 5 minutos

    with sessions_lock:
        stream_sessions[session_token] = {
            "user_id": user_id,
            "camera_id": camera_id,
            "expires": expires_at
        }
    
    return jsonify({"session_token": session_token, "expires_at": expires_at.isoformat()}), 200

# ------------------------ FIN API PARA OBTENER UN TOKEN DE SESIÓN --------------------

# ------------------------ RUTA WEB PARA EL STREAM -------------------------------
@app.route('/live_stream', methods=['GET'])
def live_stream_web_page():
    camera_id = request.args.get('camera_id')
    session_token = request.args.get('session_token')

    if not camera_id or not session_token:
        return "Error: Faltan parámetros de cámara o token de sesión en la URL.", 400
    
    # Pasa los parámetros a la plantilla HTML
    return render_template('live_stream_page.html', camera_id=camera_id, session_token=session_token)
# ------------------------ FIN RUTA WEB PARA EL STREAM ---------------------------

# ------------------------ API PARA SERVIR EL ÚLTIMO FRAME (para polling) ------------------------
@app.route('/api/latest_frame', methods=['GET'])
def latest_frame():
    camera_id = request.args.get('camera_id')
    if not camera_id:
        return Response(b'{"error": "Missing camera_id parameter."}', mimetype='application/json', status=400)

    # --- LÓGICA CON REDIS ---
    # Buscamos el frame en nuestro "pizarrón" centralizado de Redis
    redis_key = f"frame:{camera_id}"
    frame_data = redis_client.get(redis_key)
    
    response = None
    if frame_data:
        response = Response(frame_data, mimetype='image/jpeg')
    else:
        response = Response(STATIC_NO_STREAM_IMAGE_BYTES, mimetype='image/jpeg')

    # Añadimos las cabeceras para prevenir el caché del navegador (esto no cambia)
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    
    return response
# ------------------------ FIN API PARA SERVIR EL ÚLTIMO FRAME --------------------

# ------------------------ API PARA RE-TRANSMITIR STREAM A LA APP (via Polling) ------------------------
# Este endpoint es el que la página web llamará para obtener la última imagen.
# Ya no es /api/live_feed, es /api/latest_frame
# Mantenemos /api/live_feed para el mensaje de debug si se accede incorrectamente.
@app.route('/api/live_feed', methods=['GET'])
@jwt_required() # Protegido, pero ahora solo para verificar si la cámara está "viva" si se accede directamente.
def live_feed():
    return jsonify({"message": "Use /api/latest_frame with camera_id and session_token for polling stream."}), 200 # <-- Mensaje actualizado

# ------------------------ FIN API PARA RE-TRANSMITIR STREAM A LA APP (via Polling) --------------------

# ------------------------ API PARA CONTROLAR LA CÁMARA REMOTAMENTE --------------------------
@app.route('/api/camera_control', methods=['POST'])
@jwt_required()
def camera_control():
    try:
        current_user_email = get_jwt_identity()
        data = request.json
        camera_id = data.get('camera_id', None)
        mode = data.get('mode', None) # 'MODE_STREAM' o 'MODE_CAPTURE'

        if not camera_id or not mode:
            return jsonify({"msg": "Faltan camera_id o modo."}), 400

        # TODO: Autenticación adicional - Verificar si current_user_email está autorizado para esta camera_id
        # Puedes consultar la colección 'usuarios' para ver si user_email.devices contiene camera_id
        user_doc_ref = db.collection('usuarios').document(current_user_email)
        user_doc = user_doc_ref.get()
        if not user_doc.exists or camera_id not in user_doc.to_dict().get('devices', []):
            return jsonify({"msg": "Usuario no autorizado para esta cámara o cámara no encontrada."}), 403 # Forbidden

        # Publicar el comando MQTT
        mqtt_topic = f"camera/commands/{camera_id}"
        mqtt_payload = mode # El comando será MODE_STREAM o MODE_CAPTURE

        flask_mqtt_client.publish(mqtt_topic, payload=mqtt_payload, qos=MQTT_QOS_INTERNAL, retain=False)
        print(f"MQTT (Flask): Comando '{mqtt_payload}' publicado a '{mqtt_topic}' por {current_user_email}.")

        return jsonify({"msg": f"Comando '{mode}' enviado a la cámara {camera_id}."}), 200

    except Exception as e:
        app.logger.error(f"Error en camera_control: {e}")
        return jsonify({"msg": f"Error interno del servidor: {str(e)}"}), 500

# ------------------------ FIN API PARA CONTROLAR LA CÁMARA --------------------------

# ------------------------ API PARA CONTROLAR EL ENCENDIDO/APAGADO DE LA CÁMARA --------------------------
@app.route('/api/camera_power', methods=['POST'])
@jwt_required()
def camera_power():
    try:
        current_user_email = get_jwt_identity()
        data = request.json
        camera_id = data.get('camera_id', None)
        power_state = data.get('power_state', None) # 'ON' o 'OFF'

        if not camera_id or power_state not in ['ON', 'OFF']:
            return jsonify({"msg": "Faltan camera_id o estado de encendido/apagado válido ('ON'/'OFF')."}), 400

        # Autenticación adicional: Verificar si current_user_email está autorizado para esta camera_id
        user_doc_ref = db.collection('usuarios').document(current_user_email)
        user_doc = user_doc_ref.get()
        if not user_doc.exists or camera_id not in user_doc.to_dict().get('devices', []):
            return jsonify({"msg": "Usuario no autorizado para esta cámara o cámara no encontrada."}), 403 # Forbidden

        # Publicar el comando MQTT
        mqtt_topic = f"camera/power/{camera_id}" # Nuevo tópico para control de encendido
        mqtt_payload = power_state 

        flask_mqtt_client.publish(mqtt_topic, payload=mqtt_payload, qos=MQTT_QOS_INTERNAL, retain=False)
        print(f"MQTT (Flask): Comando de encendido '{power_state}' publicado a '{mqtt_topic}' para {camera_id} por {current_user_email}.")

        return jsonify({"msg": f"Comando '{power_state}' enviado a la cámara {camera_id}."}), 200

    except Exception as e:
        app.logger.error(f"Error en camera_power: {e}")
        return jsonify({"msg": f"Error interno del servidor: {str(e)}"}), 500

# ------------------------ FIN API PARA CONTROLAR EL ENCENDIDO/APAGADO --------------------------

# ------------------------ API PARA OBTENER EL ESTADO DE LA CÁMARA --------------------------
@app.route('/api/camera_status/<string:camera_id>', methods=['GET'])
@jwt_required()
def get_camera_status(camera_id):
    try:
        current_user_email = get_jwt_identity()

        # Autenticación: Verificar si el usuario está autorizado para esta camera_id
        user_doc_ref = db.collection('usuarios').document(current_user_email)
        user_doc = user_doc_ref.get()
        if not user_doc.exists or camera_id not in user_doc.to_dict().get('devices', []):
            return jsonify({"msg": "Usuario no autorizado para esta cámara o cámara no encontrada."}), 403

        with camera_status_lock:
            status_info = camera_status.get(camera_id)

        if status_info:
            # Incluir un timestamp para que la app sepa cuán reciente es el estado
            return jsonify({
                "camera_id": camera_id,
                "mode": status_info["mode"],
                "timestamp": status_info["timestamp"].isoformat()
            }), 200
        else:
            return jsonify({"msg": "Estado de cámara no disponible o no reportado.", "mode": "UNKNOWN"}), 404

    except Exception as e:
        app.logger.error(f"Error al obtener estado de cámara: {e}")
        return jsonify({"msg": f"Error interno del servidor: {str(e)}"}), 500

# ------------------------ FIN API PARA OBTENER EL ESTADO DE LA CÁMARA --------------------

# ------------------------ API PARA RECIBIR FOTOS DE REGISTRO FACIAL --------------------
@app.route('/api/upload_registration_images', methods=['POST'])
@jwt_required()
def upload_registration_images():
    """
    Recibe un lote de imágenes desde la app móvil para el registro facial de un usuario.
    Guarda las imágenes en una carpeta temporal en Firebase Storage para ser procesadas por un worker.
    """
    try:
        # 1. Identificar al usuario a través del token JWT
        current_user_email = get_jwt_identity()
        app.logger.info(f"Solicitud de registro de rostro recibida para: {current_user_email}")

        # 2. Verificar que los archivos de imagen fueron enviados
        if 'images' not in request.files:
            return jsonify({"msg": "Petición inválida: no se encontraron archivos con la clave 'images'."}), 400

        images = request.files.getlist('images')

        if not images or all(img.filename == '' for img in images):
            return jsonify({"msg": "No se enviaron archivos de imagen."}), 400

        # 3. Crear una ruta de almacenamiento única y temporal para este lote de imágenes
        # Sanitizamos el email para usarlo como nombre de carpeta
        user_email_safe = "".join([c for c in current_user_email if c.isalnum() or c in ('_', '-')])
        
        # Creamos un ID único para este lote para evitar sobreescribir
        batch_id = str(uuid.uuid4())
        
        # La ruta base donde el worker buscará nuevos trabajos
        base_storage_path = f"face_registration_pending/{user_email_safe}/{batch_id}/"

        # --- NUEVO: RECIBIR EL NOMBRE DE LA PERSONA ---
        person_name = request.form.get('person_name', 'desconocido')
        if not person_name:
            return jsonify({"msg": "Falta el nombre de la persona a registrar."}), 400

        # ... (código existente para crear la ruta base_storage_path) ...

        # --- NUEVO: GUARDAR EL NOMBRE EN UN ARCHIVO DE METADATOS ---
        metadata = {'person_name': person_name, 'user_email': current_user_email}
        metadata_blob_path = f"{base_storage_path}metadata.json"
        bucket.blob(metadata_blob_path).upload_from_string(
            json.dumps(metadata), # import json si no está
            content_type='application/json'
        )

        # 4. Subir cada imagen a la carpeta temporal en Firebase Storage
        for image in images:
            # NOTA: Usamos el nombre de archivo original que envía la app
            blob_path = f"{base_storage_path}{image.filename}"
            blob = bucket.blob(blob_path)
            
            # Subimos el flujo de bytes directamente sin guardarlo en el disco de la VM
            blob.upload_from_file(image.stream, content_type=image.content_type)

        app.logger.info(f"Se guardaron {len(images)} imágenes para {current_user_email} en el lote {batch_id}. Esperando procesamiento del worker.")

        # 5. Devolver una respuesta exitosa a la app
        return jsonify({"msg": f"Se recibieron {len(images)} imágenes correctamente. Serán procesadas en breve."}), 200

    except Exception as e:
        app.logger.error(f"Error crítico al subir imágenes de registro para {current_user_email}: {e}")
        import traceback
        traceback.print_exc() # Imprime el error completo en el log para debugging
        return jsonify({"msg": "Error interno en el servidor al guardar las imágenes."}), 500
# --------------------------------------------------------------------------------------------

# ======================== API PARA LIMPIAR HISTORIAL DE EVENTOS ========================
@app.route('/api/events/clear_history', methods=['DELETE'])
@jwt_required()
def clear_event_history():
    """
    Elimina TODOS los eventos asociados a los dispositivos de un usuario.
    Usa un proceso por lotes para ser más eficiente con Firestore.
    """
    try:
        current_user_email = get_jwt_identity()
        app.logger.info(f"Solicitud para limpiar historial recibida de {current_user_email}")

        # 1. Obtenemos los dispositivos del usuario para saber qué eventos borrar
        user_doc_ref = db.collection('usuarios').document(current_user_email)
        user_doc = user_doc_ref.get()
        if not user_doc.exists:
            return jsonify({"msg": "Usuario no encontrado."}), 404
        
        user_devices = user_doc.to_dict().get('devices', [])
        
        # Si el usuario no tiene dispositivos, no hay nada que borrar
        if not user_devices:
            return jsonify({"msg": "El usuario no tiene dispositivos, no hay nada que borrar."}), 200

        # 2. Preparamos la consulta para encontrar todos los eventos relevantes
        events_query = db.collection('events').where('device_id', 'in', user_devices)
        docs_to_delete = events_query.stream()
        
        # 3. Usamos un lote (batch) para eliminar eficientemente
        batch = db.batch()
        deleted_count = 0
        for doc in docs_to_delete:
            batch.delete(doc.reference)
            deleted_count += 1
            # Firestore recomienda lotes de máximo 500 operaciones. Hacemos commit y empezamos uno nuevo.
            if deleted_count % 499 == 0:
                batch.commit()
                batch = db.batch()
        
        # Hacemos commit del último lote (que puede tener menos de 500 documentos)
        batch.commit()
        
        app.logger.info(f"Se eliminaron {deleted_count} eventos para el usuario {current_user_email}.")
        return jsonify({"msg": f"Historial eliminado correctamente. Se borraron {deleted_count} eventos."}), 200

    except Exception as e:
        app.logger.error(f"Error al limpiar historial para {current_user_email}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"msg": "Error interno del servidor al limpiar el historial."}), 500
# =======================================================================================

# ======================== API PARA OBTENER LA ÚLTIMA ALERTA CRÍTICA ========================
@app.route('/api/latest_alert', methods=['GET'])
@jwt_required()
def get_latest_alert():
    """
    Busca y devuelve el evento de alerta más reciente (persona desconocida, alarma, etc.)
    asociado a los dispositivos de un usuario.
    """
    try:
        current_user_email = get_jwt_identity()

        # 1. Obtenemos los dispositivos del usuario
        user_doc_ref = db.collection('usuarios').document(current_user_email)
        user_doc = user_doc_ref.get()
        if not user_doc.exists:
            return jsonify({"msg": "Usuario no encontrado."}), 404
        
        user_devices = user_doc.to_dict().get('devices', [])
        if not user_devices:
            return jsonify({"latest_alert": None}), 200 # No hay dispositivos, por tanto no hay alertas

        # 2. Definimos qué tipos de eventos consideramos una "alerta crítica"
        critical_event_types = [
            'unknown_person', 
            'unknown_person_repeated_alarm', 
            'unknown_group',
            'person_no_face_alarm',
            'alarm' # Un tipo de alarma genérico si lo tuvieras
        ]

        # 3. Hacemos la consulta a Firestore
        alert_query = db.collection('events').where('device_id', 'in', user_devices) \
                                              .where('event_type', 'in', critical_event_types) \
                                              .order_by('timestamp', direction=firestore.Query.DESCENDING) \
                                              .limit(1) # ¡Solo queremos la más reciente!
        
        results = alert_query.stream()
        
        # 4. Procesamos el resultado
        latest_alert_doc = next(results, None)
        
        if latest_alert_doc:
            alert_data = latest_alert_doc.to_dict()
            alert_data['id'] = latest_alert_doc.id
            # Aseguramos que el timestamp sea un string en formato ISO para JSON
            if 'timestamp' in alert_data and isinstance(alert_data['timestamp'], datetime):
                alert_data['timestamp'] = alert_data['timestamp'].isoformat()
            
            return jsonify({"latest_alert": alert_data}), 200
        else:
            # Si no se encontraron alertas, devolvemos null
            return jsonify({"latest_alert": None}), 200

    except Exception as e:
        app.logger.error(f"Error al obtener última alerta para {current_user_email}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"msg": "Error interno del servidor al obtener la última alerta."}), 500
# =======================================================================================

# ======================== API PARA AJUSTES DE USUARIO ========================

@app.route('/api/user/settings', methods=['GET'])
@jwt_required()
def get_user_settings():
    """Devuelve las configuraciones de un usuario, como sus preferencias de notificación."""
    try:
        current_user_email = get_jwt_identity()
        user_doc_ref = db.collection('usuarios').document(current_user_email)
        user_doc = user_doc_ref.get()

        if not user_doc.exists:
            return jsonify({"msg": "Usuario no encontrado."}), 404
        
        user_data = user_doc.to_dict()
        
        # Devolvemos la preferencia, o 'all' si no existe en la base de datos
        settings = {
            'notification_preference': user_data.get('notification_preference', 'all')
        }
        
        return jsonify(settings), 200
    except Exception as e:
        app.logger.error(f"Error al obtener ajustes para {current_user_email}: {e}")
        return jsonify({"msg": "Error interno del servidor."}), 500


@app.route('/api/user/settings', methods=['POST'])
@jwt_required()
def update_user_settings():
    """Actualiza las configuraciones de un usuario."""
    try:
        current_user_email = get_jwt_identity()
        data = request.json
        new_preference = data.get('notification_preference')

        # Validamos que la opción enviada sea una de las permitidas
        if new_preference not in ['all', 'alerts_only']:
            return jsonify({"msg": "Preferencia de notificación inválida."}), 400

        user_doc_ref = db.collection('usuarios').document(current_user_email)
        user_doc_ref.update({
            'notification_preference': new_preference
        })

        return jsonify({"msg": "Ajustes guardados correctamente."}), 200
    except Exception as e:
        app.logger.error(f"Error al actualizar ajustes para {current_user_email}: {e}")
        return jsonify({"msg": "Error interno del servidor."}), 500

# =======================================================================================

# Reemplaza la función get_profile_summary completa por esta

# ======================== API PARA RESUMEN DEL PERFIL DE USUARIO ========================
@app.route('/api/user/profile_summary', methods=['GET'])
@jwt_required()

def get_profile_summary():
    """Recoge y devuelve un resumen de la cuenta, incluyendo los nombres de los rostros registrados."""
    try:
        current_user_email = get_jwt_identity()
        user_doc_ref = db.collection('usuarios').document(current_user_email)
        user_doc = user_doc_ref.get()

        if not user_doc.exists:
            return jsonify({"msg": "Usuario no encontrado."}), 404

        user_data = user_doc.to_dict()

        # --- LÓGICA MODIFICADA PARA LEER LOS NOMBRES DE LOS EMBEDDINGS ---
        user_email_safe = "".join([c for c in current_user_email if c.isalnum() or c in ('_', '-')])
        storage_prefix = f"embeddings_clientes/{user_email_safe}/"
        app.logger.info(f"DEBUG: Buscando embeddings en la carpeta: {storage_prefix}")
        blobs = bucket.list_blobs(prefix=storage_prefix)

        registered_names = []
        for blob in blobs:
            if blob.name.endswith('.npy'):
                try:
                    # Descargamos el archivo en memoria y lo leemos con numpy
                    file_bytes = blob.download_as_bytes()
                    data = np.load(io.BytesIO(file_bytes), allow_pickle=True).item()
                    if 'name' in data:
                        registered_names.append(data['name'])
                except Exception as e:
                    app.logger.error(f"Error al leer el archivo .npy {blob.name}: {e}")

        # --- FIN DE LA LÓGICA MODIFICADA ---

        # Construir el resumen
        summary = {
            "name": user_data.get('name', ''),
            "email": user_data.get('email', ''),
            "device_count": len(user_data.get('devices', [])),
            "face_registered": len(registered_names) > 0,
            "registered_names": registered_names, # <-- NUEVA LISTA DE NOMBRES
            "notification_preference": user_data.get('notification_preference', 'all')
        }

        return jsonify(summary), 200

    except Exception as e:
        app.logger.error(f"Error al generar resumen para {current_user_email}: {e}")
        return jsonify({"msg": "Error interno del servidor."}), 500
# =======================================================================================

# ======================== API PARA ELIMINAR EMBEDDINGS DE ROSTROS ========================
@app.route('/api/embeddings/delete', methods=['DELETE'])
@jwt_required()
def delete_embedding():
    """Elimina un archivo .npy de un rostro registrado para un usuario."""
    try:
        current_user_email = get_jwt_identity()
        data = request.json
        person_name = data.get('person_name')

        if not person_name:
            return jsonify({"msg": "Falta el nombre de la persona a eliminar."}), 400

        # 1. Sanitizar los nombres para construir la ruta del archivo, igual que al crearlo
        user_email_safe = "".join([c for c in current_user_email if c.isalnum() or c in ('_', '-')])
        safe_person_name = person_name.replace(" ", "_").lower()
        
        # 2. Construir la ruta exacta del archivo en Firebase Storage
        blob_path = f"embeddings_clientes/{user_email_safe}/{safe_person_name}.npy"
        app.logger.info(f"Intento de eliminación para: {blob_path}")

        # 3. Obtener el blob y eliminarlo si existe
        blob = bucket.blob(blob_path)
        if blob.exists():
            blob.delete()
            app.logger.info(f"Archivo {blob_path} eliminado exitosamente.")
            return jsonify({"msg": f"El rostro de '{person_name}' ha sido eliminado."}), 200
        else:
            app.logger.warning(f"Se intentó eliminar un archivo no existente: {blob_path}")
            return jsonify({"msg": "No se encontró el rostro especificado."}), 404

    except Exception as e:
        app.logger.error(f"Error al eliminar embedding para {current_user_email}: {e}")
        return jsonify({"msg": "Error interno del servidor."}), 500
# =======================================================================================

#if __name__ == "__main__":
 #   app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)