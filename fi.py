import os
import io # Mantener por si acaso, aunque no se usa directamente en este flujo
import time
import cv2
import numpy as np
import random # Mantenido por si acaso, aunque obtener_color_aleatorio podría eliminarse si no se usa
from datetime import datetime, timezone 
from mtcnn import MTCNN
from keras_facenet import FaceNet
from scipy.spatial.distance import cosine
import requests 
import torch 

import firebase_admin
from firebase_admin import credentials, storage, messaging 
from firebase_admin import firestore 

# ========== CONFIGURACIÓN FIREBASE ==========
SERVICE_ACCOUNT_FILE = 'security-cam-f322b-firebase-adminsdk-fbsvc-a3bf0dd37b.json'
FIREBASE_INIT_BUCKET_NAME = 'security-cam-f322b.firebasestorage.app' 
FIREBASE_STORAGE_BUCKET_DOMAIN = 'security-cam-f322b.firebasestorage.app' # No se usa directamente, podría eliminarse

# Carpeta de imágenes a procesar y de embeddings EN FIREBASE
FIREBASE_PATH_FOTOS = 'uploads/'             
FIREBASE_PATH_EMBEDDINGS = 'embeddings_clientes/' 
FIREBASE_PATH_ALARMAS = 'alarmas_procesadas/' 

# ========== CONFIGURACIÓN LOCAL ==========
CARPETA_LOCAL_FOTOS = '/tmp/fotos/'
CARPETA_LOCAL_EMBEDDINGS = '/tmp/embeddings/'
CARPETA_LOCAL_ALARMAS = '/tmp/alarmas_local/' # Se usa para guardar la imagen procesada antes de subirla
for d in [CARPETA_LOCAL_FOTOS, CARPETA_LOCAL_EMBEDDINGS, CARPETA_LOCAL_ALARMAS]:
    os.makedirs(d, exist_ok=True)

# ========== CONFIGURACIÓN DE TU BACKEND MAIN3.PY ==========
MAIN3_API_BASE_URL = 'https://tesisdeteccion.ddns.net/api' # ¡ACTUALIZA ESTO CON TU DOMINIO DDNS!

# ========== PARÁMETROS DEL SISTEMA ==========
DISTANCE_THRESHOLD = 0.5
SIMILARITY_THRESHOLD = 0.4
DETECCIONES_REQUERIDAS = 3
cooldown_seconds = 30 

# ========== INICIALIZACIÓN FIREBASE ==========
try:
    cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
    if not firebase_admin._apps: 
        firebase_admin.initialize_app(cred, {'projectId': 'security-cam-f322b'}) 
    bucket = storage.bucket(name=FIREBASE_INIT_BUCKET_NAME) 
    db = firestore.client() 
    print("[INFO] Firebase Admin SDK inicializado correctamente para FCM, Firestore y Storage.")
except Exception as e:
    print(f"[ERROR] Error al inicializar Firebase Admin SDK: {e}")
    import traceback
    traceback.print_exc()
    exit()

# ========== INICIALIZAR MODELOS ==========
embedder = FaceNet()
detector = MTCNN()
try:
    model = torch.hub.load('ultralytics/yolov5', 'yolov5x')
    class_names = model.names
except Exception as e:
    print(f"Error al cargar modelo YOLOv5: {e}. Asegúrate de tener PyTorch y YOLOv5 configurados.")
    model = None
    class_names = []


# ========== FUNCIONES AUXILIARES ==========
def obtener_color_aleatorio():
    # No se usa actualmente para dibujar, pero puede mantenerse si hay planes futuros
    return (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))

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

# ========== GESTIÓN DE EMBEDDINGS ==========
def descargar_embeddings_firebase():
    print("[INFO] Descargando embeddings de Firebase...")
    limpiar_carpeta(CARPETA_LOCAL_EMBEDDINGS)
    blobs = bucket.list_blobs(prefix=FIREBASE_PATH_EMBEDDINGS)
    count = 0
    for blob in blobs:
        if blob.name.endswith('.npy') and not blob.name.endswith('/'):
            relative_path = os.path.relpath(blob.name, FIREBASE_PATH_EMBEDDINGS)
            local_dir = os.path.join(CARPETA_LOCAL_EMBEDDINGS, os.path.dirname(relative_path))
            os.makedirs(local_dir, exist_ok=True)
            local_path = os.path.join(local_dir, os.path.basename(blob.name))
            
            blob.download_to_filename(local_path)
            count += 1
    print(f"[INFO] ¡Descarga de embeddings terminada! ({count} archivos)")

def cargar_embeddings():
    known_embeddings = []
    known_labels = []
    for root, dirs, files in os.walk(CARPETA_LOCAL_EMBEDDINGS):
        for file in files:
            if file.endswith('.npy'):
                try:
                    vec = np.load(os.path.join(root, file), allow_pickle=True).item()
                    for emb in vec['embeddings']:
                        known_embeddings.append(emb)
                        known_labels.append(vec['name'])
                except Exception as e:
                    print(f"Error al cargar embedding {file}: {e}")
    print(f"Embeddings cargados: {len(known_embeddings)}")
    print(f"Etiquetas de conocidos: {set(known_labels)}")
    return known_embeddings, known_labels

# ========== GESTIÓN DE FOTOS A PROCESAR ==========
def descargar_fotos_firebase():
    print("[INFO] Descargando imágenes de Firebase...")
    limpiar_carpeta(CARPETA_LOCAL_FOTOS)
    blobs = bucket.list_blobs(prefix=FIREBASE_PATH_FOTOS)
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
                response = messaging.send(message) # Envío individual
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

# ========== PROCESAMIENTO PRINCIPAL ==========
def procesar_imagenes():
    historial_desconocidos = []
    persona_sin_rostro_contador = 0
    last_group_alert_time = None
    last_embeddings_download = 0
    embeddings_update_interval = 600 
    known_embeddings, known_labels = [], []

    while True:
        now_ts = time.time()
        if now_ts - last_embeddings_download > embeddings_update_interval or not known_embeddings:
            descargar_embeddings_firebase()
            known_embeddings, known_labels = cargar_embeddings()
            last_embeddings_download = now_ts

        imagenes = descargar_fotos_firebase()
        if not imagenes:
            print("No hay imágenes nuevas para procesar.")
            time.sleep(10)
            continue

        current_utc_time = datetime.now(timezone.utc) 
        historial_desconocidos[:] = [item for item in historial_desconocidos if (current_utc_time - item.get('ultima_vista', current_utc_time)).total_seconds() <= 60]

        for img_dict in imagenes:
            local_path = img_dict['local_path']
            blob = img_dict['blob']
            nombre_archivo = img_dict['nombre'] 

            device_id = nombre_archivo.split('_')[0] if '_' in nombre_archivo else 'unknown'
            print(f"[DEBUG] Device ID extraído: {device_id}") 

            owner_email = get_user_email_by_device_id(device_id)
            if not owner_email:
                print(f"[INFO] Dispositivo {device_id} no asociado a ningún usuario. No se enviarán notificaciones ni se guardará historial.")
                try:
                    blob.delete() 
                except Exception as e:
                    print(f"Error al eliminar blob {blob.name} sin usuario asociado: {e}")
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
                output_filename = f"{nombre_archivo.split('.')[0]}_processed.jpg"
                output_local_path = os.path.join(CARPETA_LOCAL_ALARMAS, output_filename)
                cv2.imwrite(output_local_path, img_result) 

                blob_processed = bucket.blob(FIREBASE_PATH_ALARMAS + output_filename)
                blob_processed.upload_from_filename(output_local_path)
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
                    enviar_evento_a_main3(event_data) # Enviar a la API de historial
                    send_fcm_notification_direct( # <-- ¡Aquí se llama la función que funciona!
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
                                   (current_utc_time - item.get('ultima_alarma', datetime.min.replace(tzinfo=timezone.utc))).total_seconds() > cooldown_seconds:
                                    
                                    # Removed IFTTT alert call
                                    send_fcm_notification_direct( # <-- ¡Aquí se llama la función que funciona!
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
                         # Removed IFTTT alert call
                         send_fcm_notification_direct( # <-- ¡Aquí se llama la función que funciona!
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
                    # Removed IFTTT alert call
                    send_fcm_notification_direct( # <-- ¡Aquí se llama la función que funciona!
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