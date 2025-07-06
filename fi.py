import os
import time
import cv2
import numpy as np
import threading # Necesario para embeddings_cache_lock
from datetime import datetime, timezone, timedelta 

# Librerías de Google Cloud y Firebase
import firebase_admin 
from firebase_admin import credentials, storage, messaging 
from firebase_admin import firestore 
import requests 

# Librerías de IA
from mtcnn import MTCNN
from keras_facenet import FaceNet
from scipy.spatial.distance import cosine
import torch 

# ========== CONFIGURACIÓN GLOBAL ==========
# -- Configuración de la Cámara (referencia para ID, fi.py no controla la cámara) --
CAMERA_ID_PC = "camera001" # <--- ¡ACTUALIZA ESTO CON EL ID DE CÁMARA REAL!
CARACAS_TIMEZONE = timezone(timedelta(hours=-4)) # Zona horaria para timestamps

# -- Rutas de Endpoints y Tópicos (si fi.py los usara, aquí como referencia) --
MAIN3_API_BASE_URL = "https://tesisdeteccion.ddns.net/api" # <--- ¡ACTUALIZA ESTO CON TU DOMINIO DDNS!

# -- Configuración de Firebase Storage (Paths dentro del bucket) --
FIREBASE_SERVICE_ACCOUNT_PATH_PC = "/home/jarrprinmunk2002/tesis-JL/security-cam-f322b-firebase-adminsdk-fbsvc-a3bf0dd37b.json" # <--- ¡ACTUALIZA ESTO con la ruta ABSOLUTA en tu VM!
FIREBASE_STORAGE_BUCKET_NAME = "security-cam-f322b.firebasestorage.app" 
FIREBASE_UPLOAD_PATH_CAPTURE_MODE = f"uploads/{CAMERA_ID_PC}/" # Carpeta donde camera_stream2.py sube las fotos a procesar

# ========== VARIABLES DE ESTADO DE PROCESAMIENTO ==========
user_embeddings_cache = {}
embeddings_cache_lock = threading.Lock() # Candado para proteger el caché

# ========== CONFIGURACIÓN DE CARPETAS LOCALES DE PROCESAMIENTO ==========
CARPETA_LOCAL_FOTOS = '/tmp/fotos/' # Fotos descargadas de Firebase para procesar
CARPETA_LOCAL_EMBEDDINGS = '/tmp/embeddings/' # Embeddings descargados de Firebase
for d in [CARPETA_LOCAL_FOTOS, CARPETA_LOCAL_EMBEDDINGS]: 
    os.makedirs(d, exist_ok=True)

# ========== INICIALIZACIÓN FIREBASE ADMIN SDK (para fi.py) ==========
firebase_app_fi = None # Variable para la instancia de la app Firebase
bucket_fi = None # Variable para el bucket de Storage
db = None # Variable para el cliente de Firestore

try:
    # Intentar obtener la app si ya existe con este nombre, si no, inicializar
    try:
        firebase_app_fi = firebase_admin.get_app(name='fi_processor_app')
        print("[INFO] Firebase Admin SDK (fi.py) ya inicializado.")
    except ValueError: # La app no existe con este nombre, inicializarla
        cred_fi = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_PATH_PC)
        # Inicializa con un nombre específico para evitar conflictos con otras apps Firebase Admin SDK
        firebase_app_fi = firebase_admin.initialize_app(cred_fi, {'projectId': 'security-cam-f322b', 'storageBucket': FIREBASE_STORAGE_BUCKET_NAME}, name='fi_processor_app')
        print("[INFO] Firebase Admin SDK (fi.py) inicializado correctamente para Storage.")
    
    # Asegurarse de que bucket_fi y db se inicializan con la app correcta
    bucket_fi = storage.bucket(app=firebase_app_fi) 
    db = firestore.client(app=firebase_app_fi) 

except Exception as e:
    print(f"[ERROR] Error al inicializar Firebase Admin SDK (fi.py): {e}")
    print("El procesamiento de IA y subida de imágenes a Storage no funcionará.")
    import traceback
    traceback.print_exc()
    # Si la inicialización falla, salir del script
    exit() 


# ========== INICIALIZAR MODELOS DE IA ==========
embedder = FaceNet()
detector = MTCNN()
try:
    model = torch.hub.load('ultralytics/yolov5', 'yolov5x') # O 'yolov5n' si es más ligero
    class_names = model.names
    print("[INFO] Modelos de IA (FaceNet, MTCNN, YOLOv5) inicializados correctamente.")
except Exception as e:
    print(f"[ERROR] Error al cargar modelos de IA: {e}. Asegúrate de tener PyTorch y YOLOv5 configurados.")
    model = None
    class_names = []
    # Si los modelos de IA no cargan, el script no puede hacer su trabajo
    exit()


# ========== FUNCIONES AUXILIARES ==========
def agregar_borde_texto(imagen, texto, pos, fuente, tam, color_texto, color_borde, grosor):
    x, y = pos
    for dx in [-1, 1]:
        for dy in [-1, 1]:
            cv2.putText(imagen, texto, (x + dx, y + dy), fuente, tam, color_borde, grosor + 2, cv2.LINE_AA)
    cv2.putText(imagen, texto, (x, y), fuente, tam, color_texto, grosor, cv2.LINE_AA)

def rect_overlap(x1, y1, w1, h1, x2, y2, w2, h2):
    return (x1 < x2 + w2 and x1 + w1 > x2 and y1 < y2 + h2 and y1 + h1 > y2)

def limpiar_carpeta(path):
    for f in os.listdir(path):
        try:
            os.remove(os.path.join(path, f))
        except Exception as e:
            print(f"Error al limpiar {os.path.join(path, f)}: {e}")

# ========== GESTIÓN DE EMBEDDINGS (Ahora por usuario específico) ==========
def descargar_embeddings_firebase_for_user(user_email_safe): 
    print(f"[INFO] Descargando embeddings de Firebase para {user_email_safe}...")
    limpiar_carpeta(CARPETA_LOCAL_EMBEDDINGS) 

    blobs = storage.bucket(name=FIREBASE_STORAGE_BUCKET_NAME, app=firebase_app_fi).list_blobs(prefix=f"{FIREBASE_PATH_EMBEDDINGS}{user_email_safe}/") 
    count = 0
    for blob in blobs:
        if blob.name.endswith('.npy') and not blob.name.endswith('/'):
            local_path = os.path.join(CARPETA_LOCAL_EMBEDDINGS, os.path.basename(blob.name))
            try:
                blob.download_to_filename(local_path)
                count += 1
            except Exception as e:
                print(f"Error al descargar {blob.name}: {e}")
    print(f"[INFO] ¡Descarga de embeddings terminada! ({count} archivos para {user_email_safe})")

def cargar_embeddings_for_user(user_email_safe):
    known_embeddings = []
    known_labels = []
    for file in os.listdir(CARPETA_LOCAL_EMBEDDINGS):
        if file.endswith('.npy'):
            try:
                vec = np.load(os.path.join(CARPETA_LOCAL_EMBEDDINGS, file), allow_pickle=True).item()
                for emb in vec['embeddings']:
                    known_embeddings.append(emb)
                    known_labels.append(vec['name'])
            except Exception as e:
                print(f"Error al cargar embedding {file}: {e}")
    print(f"Embeddings cargados para {user_email_safe}: {len(known_embeddings)}")
    print(f"Etiquetas de conocidos para {user_email_safe}: {set(known_labels)}")
    return known_embeddings, known_labels

# ========== GESTIÓN DE FOTOS A PROCESAR ==========
def descargar_fotos_firebase():
    print("[INFO] Descargando imágenes de Firebase...")
    limpiar_carpeta(CARPETA_LOCAL_FOTOS)
    blobs = storage.bucket(name=FIREBASE_STORAGE_BUCKET_NAME, app=firebase_app_fi).list_blobs(prefix=FIREBASE_PATH_FOTOS) 
    imagenes = []
    for blob in blobs:
        if not blob.name.endswith('/') and (blob.name.lower().endswith(('.jpg', '.jpeg', '.png'))):
            local_path = os.path.join(CARPETA_LOCAL_FOTOS, os.path.basename(blob.name))
            try:
                blob.download_to_filename(local_path)
                imagenes.append({'blob': blob, 'local_path': local_path, 'nombre': os.path.basename(blob.name)})
            except Exception as e:
                print(f"Error al descargar {blob.name}: {e}")
    print(f"[INFO] Descargadas {len(imagenes)} imágenes.")
    return imagenes

# ========== ENVÍO DE NOTIFICACIONES FCM DIRECTAMENTE (Individual) ==========
def send_fcm_notification_direct(user_email, title, body, image_url=None, custom_data=None):
    success_count = 0
    failure_count = 0
    try:
        print(f"DEBUG_FCM: Intentando obtener tokens FCM para el usuario: {user_email}")
        user_doc_ref = db.collection('usuarios').document(user_email)
        user_doc = user_doc_ref.get()

        if not user_doc.exists:
            print(f"DEBUG_FCM: Usuario {user_email} no encontrado en Firestore.")
            return False
        
        user_data = user_doc.to_dict()
        fcm_tokens = user_data.get('fcm_tokens', [])
        
        print(f"DEBUG_FCM: Tokens FCM obtenidos de Firestore para {user_email}: {fcm_tokens}")

        if not fcm_tokens:
            print(f"DEBUG_FCM: No hay tokens FCM registrados para el usuario {user_email}.")
            return False

        for token in fcm_tokens:
            try:
                message = messaging.Message(
                    token=token, 
                    notification=messaging.Notification(
                        title=title,
                        body=body,
                        image=image_url 
                    ),
                    data=custom_data or {}
                )
                print(f"DEBUG_FCM: Enviando mensaje a token: {token[:10]}...")
                response = messaging.send(message) 
                print(f"DEBUG_FCM: Respuesta FCM para {token[:10]}: {response}")
                success_count += 1
            except Exception as token_e:
                failure_count += 1
                print(f"❌ Fallo al enviar notificación a token {token[:10]}: {token_e}")
                import traceback
                traceback.print_exc() 
        return success_count > 0 

    except Exception as e:
        print(f"❌ Ocurrió un error general en la función de envío de notificación: {e}")
        import traceback
        traceback.print_exc()
        return False

# ========== GESTIÓN DE EVENTOS PARA EL BACKEND DE LA APP ==========
def enviar_evento_a_main3(event_data):
    try:
        response = requests.post(f"{MAIN3_API_BASE_URL}/events/add", json=event_data)
        if response.status_code == 201:
            print(f"✅ Evento enviado correctamente a main3.py API: {event_data.get('event_type')}")
        else:
            print(f"❌ Error al enviar evento a main3.py API: {response.status_code}, {response.text}")
    except Exception as e:
        print(f"❌ Error de conexión al enviar evento a main3.py API: {e}")

# ========== FUNCIONES DE BÚSQUEDA DE USUARIO POR DISPOSITIVO ==========
def get_user_email_by_device_id(device_id):
    """Busca el email del usuario que posee el device_id."""
    try:
        users_with_device = db.collection('usuarios').where('devices', 'array_contains', device_id).limit(1).stream()
        for user_doc in users_with_device:
            return user_doc.id 
        return None
    except Exception as e:
        print(f"Error al buscar usuario por device_id {device_id}: {e}")
        return None

# ========== PROCESAMIENTO PRINCIPAL (BUCLE) ==========
def procesar_imagenes():
    global user_embeddings_cache, embeddings_cache_lock 
    historial_desconocidos = [] # Historial de desconocidos para detección recurrente
    persona_sin_rostro_contador = 0 # Contador para alarma de persona sin rostro
    last_group_alert_time = None # Tiempo de última alerta grupal
    embeddings_update_interval = 600 
    
    while True: # Bucle infinito para procesar imágenes continuamente
        # 1. Descargar nuevas imágenes de la carpeta 'uploads/'
        imagenes = descargar_fotos_firebase() 
        if not imagenes:
            print("No hay imágenes nuevas para procesar.")
            time.sleep(10) # Esperar antes de volver a verificar
            continue

        current_utc_time = datetime.now(timezone.utc) 
        historial_desconocidos[:] = [item for item in historial_desconocidos if (current_utc_time - item.get('ultima_vista', current_utc_time)).total_seconds() <= 60]

        for img_dict in imagenes: # Procesar cada imagen descargada
            local_path = img_dict['local_path']
            blob = img_dict['blob']
            nombre_archivo = img_dict['nombre'] 

            device_id = nombre_archivo.split('_')[0] if '_' in nombre_archivo else 'unknown'
            print(f"[DEBUG] Device ID extraído: {device_id}") 

            owner_email = get_user_email_by_device_id(device_id)
            if not owner_email:
                print(f"[INFO] Dispositivo {device_id} no asociado a ningún usuario. Se omite procesamiento facial/notificaciones.")
                try:
                    blob.delete() 
                except Exception as e:
                    print(f"Error al eliminar blob {blob.name} sin usuario asociado: {e}")
                continue
            
            # --- Carga y Cache de Embeddings por Usuario ---
            user_email_safe = "".join([c for c in owner_email if c.isalnum() or c in ('_', '-')]) 
            
            known_embeddings = [] 
            known_labels = [] 

            with embeddings_cache_lock:
                if user_email_safe in user_embeddings_cache and \
                   (time.time() - user_embeddings_cache[user_email_safe]['timestamp']) < 3600: 
                    
                    known_embeddings = user_embeddings_cache[user_email_safe]['embeddings']
                    known_labels = user_embeddings_cache[user_email_safe]['labels']
                    print(f"[INFO] Embeddings cargados desde caché para {owner_email}.")
                else:
                    print(f"[INFO] Embeddings no encontrados en caché o expirados para {owner_email}. Descargando y cargando.")
                    descargar_embeddings_firebase_for_user(user_email_safe) 
                    known_embeddings, known_labels = cargar_embeddings_for_user(user_email_safe) 
                    
                    user_embeddings_cache[user_email_safe] = {
                        "embeddings": known_embeddings,
                        "labels": known_labels,
                        "timestamp": time.time()
                    }
                    print(f"[INFO] Embeddings cargados y cacheados para {owner_email}.")
            # --- FIN Carga y Cache de Embeddings por Usuario ---

            if not known_embeddings:
                print(f"[INFO] No hay embeddings disponibles para el usuario {owner_email}. Omitiendo reconocimiento facial.")
                try:
                    blob.delete() 
                except Exception as e:
                    print(f"Error al eliminar blob {blob.name} sin embeddings: {e}")
                continue


            img = cv2.imread(local_path)
            if img is None:
                print(f"[ERROR] No se pudo cargar la imagen {local_path}.")
                try:
                    blob.delete() 
                except Exception as e:
                    print(f"Error al eliminar blob {blob.name} al no cargar imagen: {e}")
                continue

            img_result = img.copy() 

            # --- Detección de Personas (YOLOv5) ---
            personas_detectadas_bboxes = []
            if model is not None:
                results = model(img)
                detections = results.xywh[0]
                for det in detections:
                    x, y, w, h, conf, cls = det
                    if conf < 0.5: continue
                    if class_names[int(cls)] == "person":
                        personas_detectadas_bboxes.append((int(x - w/2), int(y - h/2), int(w), int(h)))
                        cv2.rectangle(img_result, (int(x - w/2), int(y - h/2)),
                                      (int(x + w/2), int(y + h/2)), (0, 255, 255), 2)
            print(f"[INFO] {len(personas_detectadas_bboxes)} persona(s) detectada(s) en {nombre_archivo} por YOLO.")


            # --- Detección y Reconocimiento Facial (MTCNN + FaceNet) ---
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            faces = detector.detect_faces(rgb)
            rostros_desconocidos_validados = []
            conocidos_en_imagen = set()

            for d in faces:
                x, y, w, h = d['box']
                x, y = abs(x), abs(y)
                if w < 30 or h < 30: continue 

                rostro = rgb[y:y+h, x:x+w]
                if rostro.size == 0: continue

                rostro_resized = cv2.resize(rostro, (160, 160))
                rostro_array = np.expand_dims(rostro_resized, axis=0)
                embedding = embedder.embeddings(rostro_array)[0]

                nombre_reconocido = "Desconocido"
                dist_min = float('inf')

                for i, known_vec in enumerate(known_embeddings):
                    dist = cosine(embedding, known_vec)
                    if dist < dist_min:
                        dist_min = dist
                        if dist < DISTANCE_THRESHOLD:
                            nombre_reconocido = known_labels[i]
                
                color_rec = (0, 0, 255) 
                if nombre_reconocido != "Desconocido":
                    conocidos_en_imagen.add(nombre_reconocido)
                    color_rec = (0, 255, 0) 

                cv2.rectangle(img_result, (x, y), (x + w, y + h), color_rec, 2)
                cv2.rectangle(img_result, (x, y - 25), (x + w, y), color_rec, -1)
                agregar_borde_texto(img_result, nombre_reconocido, (x, y - 10),
                                     cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                                     (255, 255, 255), (0, 0, 0), 2)

                if nombre_reconocido == "Desconocido":
                    for (px, py, pw, ph) in personas_detectadas_bboxes:
                        if rect_overlap(px, py, pw, ph, x, y, w, h):
                            rostros_desconocidos_validados.append({
                                'embedding': embedding,
                                'bbox': (x, y, w, h)
                            })
                            break
            
            print(f"[INFO] Personas conocidas detectadas por FaceNet: {list(conocidos_en_imagen)}")
            print(f"[INFO] {len(rostros_desconocidos_validados)} rostro(s) desconocido(s) validado(s).")

            # --- Lógica de Eventos y Notificaciones ---
            
            if len(conocidos_en_imagen) > 0 or len(rostros_desconocidos_validados) > 0 or len(personas_detectadas_bboxes) > 0:
                # Convertir la imagen procesada a bytes para subirla directamente
                _, img_encoded = cv2.imencode('.jpg', img_result)
                img_bytes = img_encoded.tobytes()

                blob_processed = bucket_fi.blob(FIREBASE_PATH_ALARMAS + nombre_archivo.replace('.jpg', '_processed.jpg')) 
                blob_processed.upload_from_string(img_bytes, content_type='image/jpeg') 
                blob_processed.make_public() 
                image_public_url = blob_processed.public_url
                print(f"[INFO] Imagen procesada subida a: {image_public_url}")

                # --- Notificación y Registro de Eventos (Persona Conocida) ---
                for nombre_conocido in conocidos_en_imagen:
                    event_data = {
                        "person_name": nombre_conocido,
                        "timestamp": current_utc_time.isoformat(),
                        "event_type": "known_person",
                        "image_url": image_public_url,
                        "event_details": f"Detección de {nombre_conocido}",
                        "device_id": device_id
                    }
                    enviar_evento_a_main3(event_data) 
                    send_fcm_notification_direct( 
                        owner_email,
                        "Persona Conocida Detectada",
                        f"{nombre_conocido} fue detectado/a por la cámara {device_id}.",
                        image_url=image_public_url,
                        custom_data={"event_type": "known_person", "person_name": nombre_conocido, "device_id": device_id}
                    )
                
                # --- Notificación y Registro de Eventos (Persona Desconocida - Alarma) ---
                if len(rostros_desconocidos_validados) > 0:
                    is_new_unknown_alarm = True 
                    for rostro_data in rostros_desconocidos_validados:
                        emb = rostro_data['embedding']
                        match_found_in_history = False
                        for item in historial_desconocidos:
                            dist = cosine(emb, item['embedding'])
                            if dist < SIMILARITY_THRESHOLD:
                                item['contador'] += 1
                                item['ultima_vista'] = current_utc_time
                                match_found_in_history = True
                                is_new_unknown_alarm = False 
                                if item['contador'] >= DETECCIONES_REQUERIDAS and \
                                   (current_utc_time - item.get('ultima_alarma', datetime.min.replace(tzinfo=timezone.utc))).total_seconds() > COOLDOWN_SECONDS:
                                    
                                    send_fcm_notification_direct( 
                                        owner_email,
                                        "¡ALERTA DE INTRUSO!",
                                        f"Rostro desconocido detectado en la cámara {device_id}. Detecciones: {item['contador']}.",
                                        image_url=image_public_url,
                                        custom_data={"event_type": "unknown_person_repeated_alarm", "device_id": device_id}
                                    )
                                    enviar_evento_a_main3({ 
                                        "person_name": "Desconocido (Recurrente)",
                                        "timestamp": current_utc_time.isoformat(),
                                        "event_type": "unknown_person", 
                                        "image_url": image_public_url,
                                        "event_details": f"Rostro desconocido recurrente en {device_id}. Detecciones: {item['contador']}.",
                                        "device_id": device_id
                                    })
                                    item['ultima_alarma'] = current_utc_time 
                                break
                        if not match_found_in_history:
                            historial_desconocidos.append({
                                'embedding': emb,
                                'contador': 1,
                                'ultima_alarma': datetime.min.replace(tzinfo=timezone.utc),
                                'ultima_vista': current_utc_time
                            })
                    
                    if is_new_unknown_alarm: 
                         send_fcm_notification_direct( 
                            owner_email,
                            "Persona Desconocida Detectada",
                            f"Se detectó un rostro no identificado en la cámara {device_id}.",
                            image_url=image_public_url,
                            custom_data={"event_type": "unknown_person", "device_id": device_id}
                        )
                         enviar_evento_a_main3({ 
                            "person_name": "Desconocido",
                            "timestamp": current_utc_time.isoformat(),
                            "event_type": "unknown_person",
                            "image_url": image_public_url,
                            "event_details": f"Rostro desconocido detectado en {device_id}.",
                            "device_id": device_id
                         })


            # --- Detección de Personas sin Rostro (Alarma) ---
            if len(faces) == 0 and len(personas_detectadas_bboxes) > 0: 
                persona_sin_rostro_contador += 1
                print(f"[INFO] Persona(s) sin rostro detectada ({persona_sin_rostro_contador}/{DETECCIONES_REQUERIDAS})")
                if persona_sin_rostro_contador >= DETECCIONES_REQUERIDAS:
                    persona_sin_rostro_contador = 0
                    send_fcm_notification_direct( 
                        owner_email,
                        "Alerta: Persona sin Rostro",
                        f"Persona detectada sin rostro en cámara {device_id}.",
                        image_url=image_public_url,
                        custom_data={"event_type": "person_no_face_alarm", "device_id": device_id}
                    )
                    enviar_evento_a_main3({ 
                        "person_name": "Persona sin Rostro",
                        "timestamp": current_utc_time.isoformat(),
                        "event_type": "unknown_person", 
                        "image_url": image_public_url,
                        "event_details": f"Persona detectada sin rostro en {device_id}.",
                        "device_id": device_id
                    })
            else:
                persona_sin_rostro_contador = 0 

            # --- Limpieza del blob original ---
            try:
                blob.delete() 
            except Exception as e:
                print(f"Error al eliminar blob original {blob.name}: {e}")
            
        print("⏳ Esperando nuevas imágenes...")
        time.sleep(10)

if __name__ == "__main__":
    procesar_imagenes()