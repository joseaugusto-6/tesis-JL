import os
from flask import Flask, request, render_template, redirect, url_for, session, flash, jsonify # Importa jsonify
from google.cloud import storage, firestore
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_jwt_extended import create_access_token, JWTManager, jwt_required, get_jwt_identity
import requests # Para hacer peticiones HTTP (a la Cloud Function)

# Inicializaciones básicas
app = Flask(__name__)
app.secret_key = 'supersecretkey'  # Cambia esto por algo más seguro en producción

# Configuración JWT
app.config["JWT_SECRET_KEY"] = "tu_clave_jwt_super_segura_aqui" # ¡CAMBIA ESTO! Debe ser la misma clave que usaste antes.
jwt = JWTManager(app)

# Configuración de Google Cloud
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "security-cam-f322b-8adcddbcb279.json"
BUCKET_NAME = "security-cam-f322b.firebasestorage.app" # Este es el bucket principal para Firebase Storage

# URL de tu Google Cloud Function para enviar FCM
CLOUD_FUNCTION_FCM_URL = "https://sendfcmnotification-614861377558.us-central1.run.app" # <-- ¡PEGA AQUI LA URL REAL DE TU GCF!

# Inicializa el cliente de Storage y Firestore
storage_client = storage.Client()
# El bucket para interactuar con Firebase Storage usa el nombre completo
bucket = storage_client.get_bucket(BUCKET_NAME) 
db = firestore.Client()

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
            return {"error": "No se recibió archivo 'file'"}, 400
        file = request.files['file']
        device_id = request.form.get('device_id', 'unknown')
        now = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        filename = f"{device_id}_{now}.jpg"
        blob = bucket.blob(f"uploads/{device_id}/{filename}")
        blob.upload_from_file(file, content_type='image/jpeg')
        return {"message": "Imagen recibida", "filename": filename}, 200
    except Exception as e:
        return {"error": str(e)}, 500

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
        return jsonify({"msg": "Ya existe un usuario con ese correo"}), 409 # Conflict
    
    try:
        firestore_create_user(name, email, password)
        return jsonify({"msg": "Usuario registrado correctamente"}), 201 # Created
    except Exception as e:
        app.logger.error(f"Error al registrar usuario: {e}")
        return jsonify({"msg": f"Error al registrar usuario: {str(e)}"}), 500

# Ejemplo de ruta protegida (para probar JWT)
@app.route("/api/protected", methods=["GET"])
@jwt_required()
def protected():
    current_user = get_jwt_identity()
    return jsonify({"message": f"Bienvenido, {current_user}! Acceso concedido."}), 200

# ------------------------ API PARA GESTIÓN DE TOKEN FCM --------------------
@app.route("/api/fcm_token", methods=["POST"])
@jwt_required() # Asegura que solo usuarios autenticados puedan registrar tokens
def update_fcm_token():
    try:
        current_user_email = get_jwt_identity() # Obtiene el email del usuario del JWT
        data = request.json
        fcm_token = data.get("fcm_token", None)

        if not fcm_token:
            return jsonify({"msg": "Falta el token FCM."}), 400

        user_doc_ref = db.collection('usuarios').document(current_user_email)
        user_doc = user_doc_ref.get()

        if not user_doc.exists:
            return jsonify({"msg": "Usuario no encontrado."}), 404
        
        user_data = user_doc.to_dict()
        current_tokens = user_data.get('fcm_tokens', [])

        # Si el token ya existe, no lo añadimos de nuevo para evitar duplicados
        if fcm_token not in current_tokens:
            current_tokens.append(fcm_token)
            user_doc_ref.update({'fcm_tokens': current_tokens})
            return jsonify({"msg": "Token FCM registrado/actualizado correctamente."}), 200
        else:
            return jsonify({"msg": "Token FCM ya existente para este usuario."}), 200 # O 204 No Content
    except Exception as e:
        app.logger.error(f"Error al actualizar token FCM: {e}")
        return jsonify({"msg": f"Error interno del servidor: {str(e)}"}), 500
# ------------------------ FIN API PARA GESTIÓN DE TOKEN FCM ----------------

# ------------------------ API PARA REGISTRAR EVENTOS ------------------------
@app.route("/api/events/add", methods=["POST"])
# Puedes añadir @jwt_required() si quieres que las cámaras/RPi se autentiquen
# con un token JWT válido para registrar eventos. Por ahora, lo dejaremos público
# para simplificar la integración con la RPi/fire7.py, pero es una vulnerabilidad si no está protegida.
def add_event():
    try:
        data = request.json
        
        # Validación básica de los datos del evento
        required_fields = ["person_name", "timestamp", "event_type", "image_url", "device_id"]
        if not all(field in data for field in required_fields):
            app.logger.warning(f"add_event: Missing required fields: {data}")
            return jsonify({"msg": "Faltan campos obligatorios para el evento."}), 400

        # Convertir timestamp a objeto datetime si viene como string
        try:
            event_timestamp = datetime.fromisoformat(data["timestamp"].replace('Z', '+00:00'))
        except ValueError:
            app.logger.warning(f"add_event: Invalid timestamp format: {data.get('timestamp')}")
            return jsonify({"msg": "Formato de timestamp inválido. Usa ISO 8601."}), 400

        event_data = {
            "person_name": data["person_name"],
            "timestamp": event_timestamp, # Guardar como datetime
            "event_type": data["event_type"], # "known_person", "unknown_person", "alarm", etc.
            "image_url": data["image_url"], # URL pública de la imagen del rostro
            "event_details": data.get("event_details", ""), # Opcional
            "device_id": data.get("device_id", "unknown"), # ID de la cámara que generó el evento
            "recorded_at": firestore.SERVER_TIMESTAMP # Para saber cuándo se recibió en el servidor
        }
        
        # Guardar en la colección 'events' en Firestore
        db.collection('events').add(event_data)
        
        return jsonify({"msg": "Evento registrado correctamente."}), 201
    except Exception as e:
        app.logger.error(f"Error al añadir evento: {e}")
        return jsonify({"msg": f"Error interno del servidor: {str(e)}"}), 500

# ------------------------ FIN API PARA REGISTRAR EVENTOS --------------------

@app.route("/api/events/history", methods=["GET"])
@jwt_required() # Protege esta ruta con JWT
def get_event_history():
    try:
        current_user_email = get_jwt_identity() # Obtiene el email del usuario del JWT

        # 1. Obtener la lista de dispositivos asociados a este usuario
        user_doc_ref = db.collection('usuarios').document(current_user_email)
        user_doc = user_doc_ref.get()

        if not user_doc.exists:
            app.logger.warning(f"get_event_history: User {current_user_email} not found.")
            return jsonify({"msg": "Usuario no encontrado en la base de datos."}), 404
        
        user_data = user_doc.to_dict()
        user_devices = user_data.get('devices', []) # Obtiene la lista de dispositivos del usuario

        if not user_devices:
            return jsonify({"events": []}), 200 # Si el usuario no tiene dispositivos, devuelve lista vacía

        # 2. Consultar eventos filtrando por los device_ids del usuario
        # Firestore tiene límites en las consultas 'in' (máximo 10 valores).
        # Si un usuario tiene muchos dispositivos, esto requerirá una lógica más avanzada
        # (ej. múltiples consultas o un esquema de datos diferente).
        # Para empezar, asumiremos que no hay tantos dispositivos.

        events_ref = db.collection('events') \
                      .where('device_id', 'in', user_devices) \
                      .order_by('timestamp', direction=firestore.Query.DESCENDING) \
                      .limit(100) # Últimos 100 eventos de los dispositivos del usuario

        events = []
        for doc in events_ref.stream():
            event_data = doc.to_dict()
            # Convertir timestamp a string ISO para enviar a la app móvil
            # Firestore Timestamp objects need .isoformat() or similar for JSON serialization
            if isinstance(event_data.get('timestamp'), datetime):
                event_data['timestamp'] = event_data['timestamp'].isoformat()
            # If it's a Firestore Timestamp object, convert it
            elif hasattr(event_data.get('timestamp'), 'isoformat'): # Fallback check
                event_data['timestamp'] = event_data['timestamp'].isoformat()
            elif hasattr(event_data.get('timestamp'), 'to_dict') and 'nanoseconds' in event_data['timestamp'].to_dict(): # For older Firestore Timestamp objects
                event_data['timestamp'] = event_data['timestamp'].to_datetime().isoformat()
            
            events.append(event_data)
        
        return jsonify({"events": events}), 200
    except Exception as e:
        app.logger.error(f"Error al obtener historial de eventos: {e}")
        return jsonify({"msg": f"Error interno del servidor: {str(e)}"}), 500

# ------------------------ API PARA DATOS DE DASHBOARD --------------------
@app.route('/api/dashboard_data', methods=['GET'])
@jwt_required()
def get_dashboard_data():
    current_user_email = get_jwt_identity()

    # Obtener eventos del usuario logueado, ordenados por timestamp descendente
    # y limitados a, por ejemplo, los últimos 5 para el resumen.
    events_ref = db.collection('eventos').where('user_email', '==', current_user_email).order_by('timestamp', direction=firestore.Query.DESCENDING).limit(5)
    latest_events = events_ref.stream()

    events_list = []
    for event in latest_events:
        event_data = event.to_dict()
        events_list.append({
            'id': event.id,
            'type': event_data.get('type'),
            'timestamp': event_data.get('timestamp').isoformat(), # Convertir a string ISO 8601
            'image_url': event_data.get('image_url'),
            'details': event_data.get('details', {})
        })

    # Opcional: Calcular estadísticas (ej. eventos hoy, alarmas hoy)
    # Para esto necesitaríamos una consulta más compleja, por ahora solo los últimos eventos.
    # Si quieres añadir estadísticas, podemos hacerlo en un paso posterior.

    return jsonify({
        'latest_events': events_list,
        'total_entries_today': 0, # Placeholder, se implementará después si es necesario
        'alarms_today': 0 # Placeholder, se implementará después si es necesario
    }), 200


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)