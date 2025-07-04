import os
import io
import time
import cv2
import numpy as np
import random
from datetime import datetime, timezone # Importa timezone
from mtcnn import MTCNN
from keras_facenet import FaceNet
from scipy.spatial.distance import cosine
import requests # Para enviar eventos al Flask de main3.py
import torch # Asumiendo que esto es necesario para YOLOv5 y está instalado

import firebase_admin
from firebase_admin import credentials, storage, messaging # Añade 'messaging'
from firebase_admin import firestore # Para acceder a Firestore

# ========== CONFIGURACIÓN FIREBASE ==========
SERVICE_ACCOUNT_FILE = 'security-cam-f322b-firebase-adminsdk-fbsvc-a3bf0dd37b.json'
# BUCKET_NAME para inicializar firebase_admin.initialize_app (usa .appspot.com)
FIREBASE_INIT_BUCKET_NAME = 'security-cam-f322b.appspot.com' 
# BUCKET_NAME para acceso de storage (usa .firebasestorage.app)
FIREBASE_STORAGE_BUCKET_DOMAIN = 'security-cam-f322b.firebasestorage.app'

# Carpeta de imágenes a procesar y de embeddings EN FIREBASE
FIREBASE_PATH_FOTOS = 'uploads/'             # Fotos subidas por la cámara (ej. uploads/camera001/imagen.jpg)
FIREBASE_PATH_EMBEDDINGS = 'embeddings_clientes/' # Embeddings de cada cliente (ej. embeddings_clientes/email@example.com/nombre.npy)
FIREBASE_PATH_ALARMAS = 'alarmas_procesadas/' # Carpeta en firebase donde subir imágenes de alarmas/eventos procesados

# ========== CONFIGURACIÓN LOCAL ==========
CARPETA_LOCAL_FOTOS = '/tmp/fotos/'
CARPETA_LOCAL_EMBEDDINGS = '/tmp/embeddings/'
CARPETA_LOCAL_ALARMAS = '/tmp/alarmas_local/' # Donde se guardan las imágenes de alarma temporalmente antes de subir
for d in [CARPETA_LOCAL_FOTOS, CARPETA_LOCAL_EMBEDDINGS, CARPETA_LOCAL_ALARMAS]:
    os.makedirs(d, exist_ok=True)

# ========== CONFIGURACIÓN DE TU BACKEND MAIN3.PY ==========
MAIN3_API_BASE_URL = 'https://tesisdeteccion.ddns.net/api' # ¡ACTUALIZA ESTO CON TU DOMINIO DDNS!

# ========== PARÁMETROS DEL SISTEMA ==========
DISTANCE_THRESHOLD = 0.5
SIMILARITY_THRESHOLD = 0.4
DETECCIONES_REQUERIDAS = 3
cooldown_seconds = 30 # segundos (cooldown para alertas IFTTT/FCM del mismo evento)

# ========== INICIALIZACIÓN FIREBASE ==========
cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
firebase_admin.initialize_app(cred, {
    'credential': cred, # Asegurarse de que las credenciales se pasan así también
    'projectId': 'security-cam-f322b', # Tu ID de proyecto Firebase (confirmado en depuración)
    'storageBucket': FIREBASE_INIT_BUCKET_DOMAIN, # Usa el nombre .appspot.com para la inicialización
})
bucket = storage.bucket() # Este bucket es el que usa FIREBASE_INIT_BUCKET_NAME por defecto
db = firestore.client() # Inicializa el cliente de Firestore

# ========== INICIALIZAR MODELOS ==========
# Asegúrate de que estos modelos estén correctamente configurados y sus dependencias instaladas
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
        # Asegurarse de no descargar subcarpetas vacías
        if blob.name.endswith('.npy') and not blob.name.endswith('/'):
            # Construir ruta local, creando directorios intermedios si es necesario
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
        # Ignorar directorios y asegurar que sea un tipo de imagen
        if not blob.name.endswith('/') and (blob.name.lower().endswith(('.jpg', '.jpeg', '.png'))):
            local_path = os.path.join(CARPETA_LOCAL_FOTOS, os.path.basename(blob.name))
            try:
                blob.download_to_filename(local_path)
                imagenes.append({'blob': blob, 'local_path': local_path, 'nombre': os.path.basename(blob.name)})
            except Exception as e:
                print(f"Error al descargar {blob.name}: {e}")
    print(f"[INFO] Descargadas {len(imagenes)} imágenes.")
    return imagenes

# ========== ALERTA IFTTT ==========
def enviar_alerta_ifttt(path_local_imagen, event_name, value1, value2_url, value3):
    nombre_en_firebase = FIREBASE_PATH_ALARMAS + os.path.basename(path_local_imagen)
    blob = bucket.blob(nombre_en_firebase)
    blob.upload_from_filename(path_local_imagen)
    blob.make_public() # Hacer la imagen pública para IFTTT y notificaciones
    url_publica = blob.public_url
    print(f"[ALERTA] Imagen subida a {url_publica}")

    ifttt_webhook_url = f"https://maker.ifttt.com/trigger/{event_name}/with/key/lxBuXKITSTN5Gu-bhN_m28waEiJoprM-ClSGvq69n7k"
    data = {
        'value1': value1,
        'value2': value2_url, # Usar la URL pública de la imagen
        'value3': value3
    }
    try:
        response = requests.post(ifttt_webhook_url, data=data)
        if response.status_code == 200:
            print(f"✅ Alerta '{event_name}' enviada correctamente a IFTTT.")
        else:
            print(f"❌ Error al enviar el webhook a IFTTT ({event_name}): {response.status_code}, {response.text}")
    except Exception as e:
        print(f"❌ Error al enviar a IFTTT ({event_name}): {e}")
    return url_publica # Devuelve la URL pública para usarla en FCM/Historial

# ========== ENVÍO DE NOTIFICACIONES FCM ==========
def enviar_notificacion_fcm(user_email, title, body, image_url=None, data=None):
    try:
        # Obtener los tokens FCM del usuario desde Firestore
        user_doc_ref = db.collection('usuarios').document(user_email)
        user_doc = user_doc_ref.get()

        if not user_doc.exists:
            print(f"[FCM] Usuario {user_email} no encontrado para enviar notificación.")
            return

        user_data = user_doc.to_dict()
        fcm_tokens = user_data.get('fcm_tokens', [])

        if not fcm_tokens:
            print(f"[FCM] No hay tokens FCM registrados para el usuario {user_email}.")
            return

        # Construir el mensaje FCM
        message = messaging.MulticastMessage(
            tokens=fcm_tokens,
            notification=messaging.Notification(
                title=title,
                body=body,
                image=image_url # Opcional: URL de la imagen en la notificación (solo para algunos clientes)
            ),
            data=data or {} # Datos personalizados, que la app puede leer
        )

        # Enviar el mensaje
        response = messaging.send_multicast(message)

        if response.success_count > 0:
            print(f"[FCM] Notificación enviada con éxito a {response.success_count} dispositivos para {user_email}.")
        if response.failure_count > 0:
            print(f"[FCM] Fallo al enviar notificación a {response.failure_count} dispositivos para {user_email}.")
            for error_response in response.responses:
                if not error_response.success:
                    print(f"  [FCM Error] {error_response.exception}")
                    # Opcional: Si el token es inválido, podrías considerarlo para limpieza
                    # if isinstance(error_response.exception, messaging.FirebaseError) and \
                    #    error_response.exception.code == 'messaging/invalid-argument':
                    #    # Lógica para remover el token inválido del array 'fcm_tokens' del usuario en Firestore
        
    except Exception as e:
        print(f"❌ Error al enviar notificación FCM: {e}")

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
        # Busca en la colección 'usuarios' donde el array 'devices' contenga el device_id
        users_with_device = db.collection('usuarios').where('devices', 'array_contains', device_id).limit(1).stream()
        for user_doc in users_with_device:
            return user_doc.id # El ID del documento del usuario es su email
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
    embeddings_update_interval = 600 # 10 minutos
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

        current_utc_time = datetime.now(timezone.utc) # Usar timezone.utc para consistencia
        historial_desconocidos[:] = [item for item in historial_desconocidos if (current_utc_time - item.get('ultima_vista', current_utc_time)).total_seconds() <= 60]

        for img_dict in imagenes:
            local_path = img_dict['local_path']
            blob = img_dict['blob']
            nombre_archivo = img_dict['nombre'] # Ej: camera001_20250704_130000.jpg

            # Extraer device_id del nombre del archivo (ej. "camera001")
            device_id = nombre_archivo.split('_')[0] if '_' in nombre_archivo else 'unknown'
            print(f"[DEBUG] Device ID extraído: {device_id}") # DEBUG para verificar

            # Obtener el email del usuario propietario del dispositivo
            owner_email = get_user_email_by_device_id(device_id)
            if not owner_email:
                print(f"[INFO] Dispositivo {device_id} no asociado a ningún usuario. No se enviarán notificaciones ni se guardará historial.")
                try:
                    blob.delete() # Eliminar el blob de Firebase Storage si no se puede asociar
                except Exception as e:
                    print(f"Error al eliminar blob {blob.name} sin usuario asociado: {e}")
                continue

            img = cv2.imread(local_path)
            if img is None:
                print(f"[ERROR] No se pudo cargar la imagen {local_path}.")
                try:
                    blob.delete() # Eliminar el blob de Firebase Storage si no se puede cargar
                except Exception as e:
                    print(f"Error al eliminar blob {blob.name} al no cargar imagen: {e}")
                continue

            img_result = img.copy() # Imagen para dibujar resultados

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
                if w < 30 or h < 30: continue # Ignorar rostros muy pequeños

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
                
                color_rec = (0, 0, 255) # Rojo para desconocido
                if nombre_reconocido != "Desconocido":
                    conocidos_en_imagen.add(nombre_reconocido)
                    color_rec = (0, 255, 0) # Verde para conocido

                cv2.rectangle(img_result, (x, y), (x + w, y + h), color_rec, 2)
                cv2.rectangle(img_result, (x, y - 25), (x + w, y), color_rec, -1)
                agregar_borde_texto(img_result, nombre_reconocido, (x, y - 10),
                                     cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                                     (255, 255, 255), (0, 0, 0), 2)

                if nombre_reconocido == "Desconocido":
                    # Validar si el rostro desconocido está dentro de una persona detectada por YOLO
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
            
            # Guardar imagen procesada en Storage para historial/notificaciones
            # Si se detectó algo, guardar la imagen procesada
            if len(conocidos_en_imagen) > 0 or len(rostros_desconocidos_validados) > 0 or len(personas_detectadas_bboxes) > 0:
                output_filename = f"{nombre_archivo.split('.')[0]}_processed.jpg"
                output_local_path = os.path.join(CARPETA_LOCAL_ALARMAS, output_filename)
                cv2.imwrite(output_local_path, img_result) # Guarda la imagen procesada localmente

                # Sube la imagen procesada a Firebase Storage para el historial y notificaciones
                blob_processed = bucket.blob(FIREBASE_PATH_ALARMAS + output_filename)
                blob_processed.upload_from_filename(output_local_path)
                blob_processed.make_public() # Hacerla pública
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
                    enviar_notificacion_fcm(
                        owner_email,
                        "Persona Conocida Detectada",
                        f"{nombre_conocido} fue detectado/a por la cámara {device_id}.",
                        image_url=image_public_url,
                        data={"event_type": "known_person", "person_name": nombre_conocido, "device_id": device_id}
                    )
                
                # --- Notificación y Registro de Eventos (Persona Desconocida - Alarma) ---
                if len(rostros_desconocidos_validados) > 0:
                    # Lógica de detección de alarmas por personas desconocidas (si se requiere DETECCIONES_REQUERIDAS)
                    is_new_unknown_alarm = True # Para enviar notif y evento al menos una vez por rostro desconocido
                    for rostro_data in rostros_desconocidos_validados:
                        emb = rostro_data['embedding']
                        match_found_in_history = False
                        for item in historial_desconocidos:
                            dist = cosine(emb, item['embedding'])
                            if dist < SIMILARITY_THRESHOLD:
                                item['contador'] += 1
                                item['ultima_vista'] = current_utc_time
                                match_found_in_history = True
                                is_new_unknown_alarm = False # Si ya está en historial, no es nueva alarma única.
                                if item['contador'] >= DETECCIONES_REQUERIDAS and \
                                   (current_utc_time - item.get('ultima_alarma', datetime.min.replace(tzinfo=timezone.utc))).total_seconds() > cooldown_seconds:
                                    
                                    # ENVIAR ALERTA DE ROSTRO DESCONOCIDO REPETIDO
                                    alerta_url = enviar_alerta_ifttt(output_local_path, "send_alarm", # Revisa el nombre del evento IFTTT
                                                                     "Rostro desconocido detectado MÚLTIPLES veces",
                                                                     image_public_url,
                                                                     "Alerta por persona desconocida recurrente.")
                                    enviar_notificacion_fcm(
                                        owner_email,
                                        "¡ALERTA DE INTRUSO!",
                                        f"Rostro desconocido detectado en la cámara {device_id}. Detecciones: {item['contador']}.",
                                        image_url=image_public_url,
                                        data={"event_type": "unknown_person_repeated_alarm", "device_id": device_id}
                                    )
                                    enviar_evento_a_main3({ # Registrar evento en historial de app
                                        "person_name": "Desconocido (Recurrente)",
                                        "timestamp": current_utc_time.isoformat(),
                                        "event_type": "unknown_person", # O un tipo más específico si lo agregamos
                                        "image_url": image_public_url,
                                        "event_details": f"Rostro desconocido recurrente en {device_id}. Detecciones: {item['contador']}.",
                                        "device_id": device_id
                                    })
                                    item['ultima_alarma'] = current_utc_time # Actualiza el tiempo de la última alarma
                                break
                        if not match_found_in_history:
                            historial_desconocidos.append({
                                'embedding': emb,
                                'contador': 1,
                                'ultima_alarma': datetime.min.replace(tzinfo=timezone.utc),
                                'ultima_vista': current_utc_time
                            })
                    
                    # Alerta de rostro desconocido (primera detección de un nuevo desconocido)
                    if is_new_unknown_alarm: # Si no fue una recurrente, es una primera detección
                         alerta_url = enviar_alerta_ifttt(output_local_path, "send_alarm",
                                                          "Rostro desconocido detectado",
                                                          image_public_url,
                                                          "Alerta de primera detección de rostro desconocido.")
                         enviar_notificacion_fcm(
                            owner_email,
                            "Persona Desconocida Detectada",
                            f"Se detectó un rostro no identificado en la cámara {device_id}.",
                            image_url=image_public_url,
                            data={"event_type": "unknown_person", "device_id": device_id}
                        )
                         enviar_evento_a_main3({ # Registrar evento en historial de app
                            "person_name": "Desconocido",
                            "timestamp": current_utc_time.isoformat(),
                            "event_type": "unknown_person",
                            "image_url": image_public_url,
                            "event_details": f"Rostro desconocido detectado en {device_id}.",
                            "device_id": device_id
                         })


            # --- Detección de Personas sin Rostro (Alarma) ---
            if len(faces) == 0 and len(personas_detectadas_bboxes) > 0: # Si hay personas pero no se detectaron rostros
                persona_sin_rostro_contador += 1
                print(f"[INFO] Persona(s) sin rostro detectada ({persona_sin_rostro_contador}/{DETECCIONES_REQUERIDAS})")
                if persona_sin_rostro_contador >= DETECCIONES_REQUERIDAS:
                    persona_sin_rostro_contador = 0
                    # La imagen procesada ya está en output_local_path y su URL es image_public_url
                    alerta_url = enviar_alerta_ifttt(output_local_path, "persona_sin_rostro",
                                                     "Persona detectada sin rostro 3 veces",
                                                     image_public_url,
                                                     "Alerta por detección de persona sin rostro")
                    enviar_notificacion_fcm(
                        owner_email,
                        "Alerta: Persona sin Rostro",
                        f"Persona detectada sin rostro en cámara {device_id}.",
                        image_url=image_public_url,
                        data={"event_type": "person_no_face_alarm", "device_id": device_id}
                    )
                    enviar_evento_a_main3({ # Registrar evento en historial de app
                        "person_name": "Persona sin Rostro",
                        "timestamp": current_utc_time.isoformat(),
                        "event_type": "unknown_person", # O un tipo más específico si lo agregamos
                        "image_url": image_public_url,
                        "event_details": f"Persona detectada sin rostro en {device_id}.",
                        "device_id": device_id
                    })
            else:
                persona_sin_rostro_contador = 0 # Resetear contador si hay rostros o no hay personas

            # --- Limpieza del blob original ---
            try:
                blob.delete() # Elimina la imagen original del bucket 'uploads/'
            except Exception as e:
                print(f"Error al eliminar blob original {blob.name}: {e}")
            
        print("⏳ Esperando nuevas imágenes...")
        time.sleep(10)

if __name__ == "__main__":
    procesar_imagenes()