import os
from flask import Flask, request, render_template, redirect, url_for, session, flash, jsonify
from google.cloud import storage, firestore
from datetime import datetime, timedelta, timezone # Importa timedelta y timezone
from werkzeug.security import generate_password_hash, check_password_hash
from flask_jwt_extended import create_access_token, JWTManager, jwt_required, get_jwt_identity
import requests

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
CLOUD_FUNCTION_FCM_URL = "https://sendfcmnotification-614861377558.us-central1.run.app" # <-- ¡PEGA AQUI LA URL REAL DE TU GCF! (Aunque ya no la usaremos para FCM)

# Inicializa el cliente de Storage y Firestore
storage_client = storage.Client()
bucket = storage_client.get_bucket(BUCKET_NAME) 
db = firestore.Client()

# Define la zona horaria de Caracas (o la que te sea relevante)
CARACAS_TIMEZONE = timezone(timedelta(hours=-4)) # GMT-4 (ejemplo, ajusta si es diferente)

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
# ------------------------ API PARA LLAMAR GCF Y ENVIAR FCM --------------------
# Nota: Este endpoint y la GCF ya no son necesarios para FCM, ya que fire.py envía directo.
# Se mantiene aquí por si lo necesitabas para otros fines o para documentar el camino.
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
# ------------------------ FIN API PARA LLAMAR GCF Y ENVIAR FCM ---------------
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)