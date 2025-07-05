import os
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
import uuid # Para generar tokens de sesión de stream

# Inicializaciones básicas
app = Flask(__name__)
app.secret_key = 'supersecretkey'  # Cambia esto por algo más seguro en producción

# Configuración JWT
app.config["JWT_SECRET_KEY"] = "tu_clave_jwt_super_segura_aqui" # ¡CAMBIA ESTO! Debe ser la misma clave que usaste antes.
jwt = JWTManager(app)

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
    print(f"MQTT (Flask): Mensaje recibido en '{topic}': {payload}")

    if topic.startswith("camera/status/"):
        camera_id = topic.split('/')[-1]
        mode_status = payload.replace("Modo: ", "") # Extraer el modo
        with camera_status_lock:
            camera_status[camera_id] = {"mode": mode_status, "timestamp": datetime.now()}
        print(f"MQTT (Flask): Estado de {camera_id} actualizado a {mode_status}")

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
        "fcm_tokens": [] # Inicializa con una lista vacía de tokens FCM
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
def index():
    if 'user_email' in session:
        return redirect(url_for('upload_npy'))
    return render_template("index.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not name or not email or not password:
            flash("Completa todos los campos.", "danger")
            return render_template("register.html")
        if firestore_user_exists(email):
            flash("Ya existe un usuario registrado con ese correo.", "danger")
            return render_template("register.html")
        firestore_create_user(name, email, password)
        flash("Usuario registrado correctamente. Ahora inicia sesión.", "success")
        return redirect(url_for('login'))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if firestore_check_user(email, password):
            session['user_email'] = email
            flash(f"¡Bienvenido {email}!", "success")
            return redirect(url_for('upload_npy'))
        else:
            flash("Correo o contraseña incorrectos.", "danger")
            return render_template("login.html")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop('user_email', None)
    flash("Sesión cerrada.", "success")
    return redirect(url_for('index'))

@app.route('/upload_npy', methods=['GET', 'POST'])
def upload_npy():
    if 'user_email' not in session:
        flash("Debes iniciar sesión.", "danger")
        return redirect(url_for('login'))
    mensaje = ""
    if request.method == "POST":
        user_email = session['user_email']
        if 'npyfile' not in request.files:
            mensaje = "Por favor selecciona un archivo .npy"
        else:
            file = request.files['npyfile']
            if not file.filename.endswith('.npy'):
                mensaje = "Solo se permiten archivos .npy"
            else:
                original_filename = file.filename
                user_email_safe = "".join([c for c in user_email if c.isalnum() or c in ('_', '-')])
                blob = bucket.blob(f"embeddings_clientes/{user_email_safe}/{original_filename}")
                blob.upload_from_file(file, content_type='application/octet-stream')
                mensaje = f"Archivo {original_filename} subido correctamente a la carpeta {user_email_safe}"
    return render_template("upload_npy.html", mensaje=mensaje, username=session.get('user_email', ''))


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

# Nuevo endpoint para obtener datos del dashboard
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

    latest_events_query = db.collection('events') \
                          .where('device_id', 'in', user_devices) \
                          .order_by('timestamp', direction=firestore.Query.DESCENDING) \
                          .limit(5)
    
    events_list = []
    for event in latest_events_query.stream():
        event_data = event.to_dict()
        events_list.append({
            'id': event.id,
            'person_name': event_data.get('person_name', 'Desconocido'),
            'event_type': event_data.get('event_type', 'unknown'),
            'timestamp': event_data.get('timestamp').isoformat() if isinstance(event_data.get('timestamp'), datetime) else event_data.get('timestamp'),
            'image_url': event_data.get('image_url', ''),
            'event_details': event_data.get('event_details', ''),
            'device_id': event_data.get('device_id', 'unknown')
        })

    now_caracas = datetime.now(CARACAS_TIMEZONE)
    today_start = now_caracas.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = now_caracas.replace(hour=23, minute=59, second=59, microsecond=999999)

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
    
    return jsonify({
        'latest_events': events_list,
        'total_entries_today': total_entries_today,
        'alarms_today': alarms_today
    }), 200

# Nuevo endpoint para obtener la lista de dispositivos del usuario
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
    devices = user_data.get('devices', []) # Obtiene la lista de dispositivos del usuario

    return jsonify({"devices": devices}), 200

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
    global is_streaming_active, last_frame_received_time # Acceder a la variable global
    try:
        # Esperamos que el frame se envíe como un archivo binario o base64 en el cuerpo
        # Si se envía como 'file' en form-data:
        if 'frame' not in request.files:
            # O si se envía como raw binary data:
            if request.data:
                frame_data = request.data
                # print("Recibido frame como raw data") # DEBUG
            else:
                return jsonify({"error": "No se recibió frame de video."}), 400
        else:
            frame_data = request.files['frame'].read()
            print("Recibido frame como file") # DEBUG

        if not frame_data:
            return jsonify({"error": "Frame de video vacío."}), 400
        
        camera_id = request.form.get('camera_id', 'default_camera') # Obtener camera_id del form-data
        if not camera_id:
            return jsonify({"error": "Missing camera_id in form data."}), 400

        # Guardar el último frame para la camera_id específica en el diccionario
        with frames_lock:
            latest_frames[camera_id] = {"frame": frame_data, "timestamp": datetime.now()}
            # Actualizar el estado general del stream si es el primer frame o si vuelve a estar activo
            if not is_streaming_active:
                is_streaming_active = True
            last_frame_received_time = time.time() # Actualizar tiempo de último frame general
        
        return jsonify({"message": "Frame recibido."}), 200
    except Exception as e:
        app.logger.error(f"Error al recibir frame de stream: {e}")
        # Considerar si todos los streams están inactivos, no solo uno
        # is_streaming_active = False # Esta variable ahora es más para el estado general
        return jsonify({"error": str(e)}), 500

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
#@jwt_required() # Proteger el acceso a la última imagen
def latest_frame():
    # Obtener el camera_id del request arguments
    camera_id = request.args.get('camera_id')
    if not camera_id:
        return Response(b'{"error": "Missing camera_id parameter."}', mimetype='application/json', status=400) # Devolver JSON de error

    with frames_lock:
        frame_data_for_camera = latest_frames.get(camera_id) # Obtener frame específico para esta camera_id

    # Verificar si el frame es reciente (no más de 15 segundos, por ejemplo)
    if frame_data_for_camera and (time.time() - frame_data_for_camera['timestamp'].timestamp()) < 15: # Usar .timestamp() para comparar con time.time()
        return Response(frame_data_for_camera['frame'], mimetype='image/jpeg')
    else:
        # Si no hay frames recientes para esa cámara, o la cámara no existe, devolver la imagen estática
        return Response(STATIC_NO_STREAM_IMAGE_BYTES, mimetype='image/jpeg')

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

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)