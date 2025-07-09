#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fi.py – Worker de SecurityCamApp
• Mismos umbrales, rutas y lógica que acordamos.
• Marco verde = conocido, rojo = desconocido.
• Texto SIEMPRE blanco con borde negro, desplazado a la esquina sup-izq.
"""

# ======== IMPORTS ========
import io, os, time
from datetime import datetime, timezone

import cv2
import numpy as np
from scipy.spatial.distance import cosine
import torch
from mtcnn import MTCNN
from keras_facenet import FaceNet

import requests
import firebase_admin
from firebase_admin import credentials, initialize_app, storage, messaging, firestore
# =========================

# ======== CONFIG =========
SERVICE_ACCOUNT_FILE = 'security-cam-f322b-firebase-adminsdk-fbsvc-a3bf0dd37b.json'
PROJECT_ID           = 'security-cam-f322b'
BUCKET_ID            = f'{PROJECT_ID}.firebasestorage.app'

PREF_UPLOADS   = 'uploads/'
PREF_PROCESSED = 'alarmas_procesadas/'
PREF_GROUPS    = 'alertas_grupales/'
PREF_EMBEDS    = 'embeddings_clientes/'
MAIN3_API_BASE_URL   = 'https://tesisdeteccion.ddns.net/api'

NO_FACE_THRESHOLD = 3 
NO_FACE_TIMEOUT_SECONDS = 120 
DIST_THRESHOLD   = 0.50
SIM_THRESHOLD    = 0.40
REPEAT_THRESHOLD = 3
COOLDOWN_SECONDS = 30
EMB_REFRESH_SEC  = 600
CACHE_EXPIRATION_SECONDS = 600  # 10 minutos
# =========================


# ===== FIREBASE INIT =====
cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
firebase_admin.initialize_app(cred, {
    'projectId': PROJECT_ID,
    'storageBucket': BUCKET_ID,
})
bucket = storage.bucket()
db     = firestore.client()
print('[OK] Firebase inicializado')
# =========================

# --- Caché para los embeddings de los usuarios ---
# Formato: {'user_email': {'embeddings': [...], 'labels': [...], 'timestamp': ...}}
embeddings_cache = {}
# --- Memoria" para rastrear estas detecciones ---
# Formato: {'camera_id': {'count': N, 'timestamp': ...}}
no_face_tracker = {}

# ====== MODELOS ==========
detector = MTCNN()
embedder = FaceNet()
yolo     = torch.hub.load('ultralytics/yolov5', 'yolov5x', trust_repo=True)
NAMES    = yolo.names
# =========================

def cargar_embeddings_por_usuario(user_email):
    """
    Carga los embeddings para un usuario específico.
    Primero revisa la caché. Si no están o han expirado, los carga desde Firebase Storage.
    """
    now = time.time()
    user_email_safe = "".join([c for c in user_email if c.isalnum() or c in ('_', '-')])
    
    # 1. Revisar la caché
    if user_email in embeddings_cache and (now - embeddings_cache[user_email]['timestamp']) < CACHE_EXPIRATION_SECONDS:
        print(f"[CACHE] Usando embeddings en caché para el usuario {user_email}.")
        return embeddings_cache[user_email]['embeddings'], embeddings_cache[user_email]['labels']

    # 2. Si no está en caché o expiró, cargar desde Firebase Storage
    print(f"[STORAGE] Cargando embeddings desde Firebase para el usuario {user_email}...")
    embs, labels = [], []
    storage_prefix = f"{PREF_EMBEDS}{user_email_safe}/"
    
    for blob in bucket.list_blobs(prefix=storage_prefix):
        if blob.name.endswith('.npy'):
            try:
                file_bytes = blob.download_as_bytes()
                data = np.load(io.BytesIO(file_bytes), allow_pickle=True).item()
                if 'embeddings' in data and 'name' in data:
                    embs.extend(data['embeddings'])
                    labels.extend([data['name']] * len(data['embeddings']))
            except Exception as e:
                print(f"[ERROR] No se pudo leer el archivo .npy {blob.name}: {e}")

    # 3. Actualizar la caché
    embeddings_cache[user_email] = {
        'embeddings': embs,
        'labels': labels,
        'timestamp': now
    }
    print(f"[INFO] Embeddings cargados y guardados en caché para {user_email}. Total: {len(embs)}")
    return embs, labels

# ===== UTILIDADES ========
def put_text_outline(img, text, x, y,
                     text_color=(255,255,255), outline=(0,0,0),
                     font=cv2.FONT_HERSHEY_SIMPLEX, scale=0.5, thickness=1):
    """Texto blanco con borde negro, sin fondo."""
    for dx in (-1, 1):
        for dy in (-1, 1):
            cv2.putText(img, text, (x+dx, y+dy), font,
                        scale, outline, thickness+1, cv2.LINE_AA)
    cv2.putText(img, text, (x, y), font,
                scale, text_color, thickness, cv2.LINE_AA)


def cargar_embeddings_por_usuario(user_email):
    """
    Carga los embeddings para un usuario específico.
    Primero revisa la caché. Si no están o han expirado, los carga desde Firebase Storage.
    """
    now = time.time()
    user_email_safe = "".join([c for c in user_email if c.isalnum() or c in ('_', '-')])
    
    # 1. Revisar la caché
    if user_email in embeddings_cache and (now - embeddings_cache[user_email]['timestamp']) < CACHE_EXPIRATION_SECONDS:
        print(f"[CACHE] Usando embeddings en caché para el usuario {user_email}.")
        return embeddings_cache[user_email]['embeddings'], embeddings_cache[user_email]['labels']

    # 2. Si no está en caché o expiró, cargar desde Firebase Storage
    print(f"[STORAGE] Cargando embeddings desde Firebase para el usuario {user_email}...")
    embs, labels = [], []
    storage_prefix = f"{PREF_EMBEDS}{user_email_safe}/"
    
    for blob in bucket.list_blobs(prefix=storage_prefix):
        if blob.name.endswith('.npy'):
            try:
                file_bytes = blob.download_as_bytes()
                data = np.load(io.BytesIO(file_bytes), allow_pickle=True).item()
                if 'embeddings' in data and 'name' in data:
                    embs.extend(data['embeddings'])
                    labels.extend([data['name']] * len(data['embeddings']))
            except Exception as e:
                print(f"[ERROR] No se pudo leer el archivo .npy {blob.name}: {e}")

    # 3. Actualizar la caché
    embeddings_cache[user_email] = {
        'embeddings': embs,
        'labels': labels,
        'timestamp': now
    }
    print(f"[INFO] Embeddings cargados y guardados en caché para {user_email}. Total: {len(embs)}")
    return embs, labels


def send_fcm(user_email, event_data):
    user_doc_ref = db.collection('usuarios').document(user_email)
    try:
        user_doc = user_doc_ref.get()
        if not user_doc.exists:
            print(f"[ERROR] FCM: No se encontró el documento del usuario: {user_email}")
            return
    except Exception as e:
        print(f"[ERROR] FCM: No se pudo obtener el documento del usuario: {e}")
        return

    fcm_tokens = user_doc.to_dict().get('fcm_tokens', [])
    if not fcm_tokens:
        print(f"[INFO] FCM: El usuario {user_email} no tiene tokens FCM registrados.")
        return

    # Creamos una copia de la lista para iterar, por si la modificamos durante el bucle
    for token in list(fcm_tokens):
        try:
            message = messaging.Message(
                token=token,
                notification=messaging.Notification(
                    title=event_data['title'],
                    body=event_data['body'],
                ),
                android=messaging.AndroidConfig(priority='high'),
                data={'image_url': event_data.get('image_url', '')}
            )
            messaging.send(message)
            print(f"[SUCCESS] FCM: Notificación enviada exitosamente al token que termina en ...{token[-6:]}")

        except messaging.UnregisteredError:
            # ESTE ES EL CASO: El token es inválido porque la app fue desinstalada o los datos borrados.
            print(f"[CLEANUP] FCM: Token inválido detectado (...{token[-6:]}). Eliminándolo de la base de datos.")
            try:
                # Usamos ArrayRemove para eliminar el token específico de la lista en Firestore.
                user_doc_ref.update({
                    'fcm_tokens': firestore.ArrayRemove([token])
                })
                print(f"[SUCCESS] FCM: Token inválido eliminado para el usuario {user_email}.")
            except Exception as e:
                print(f"[ERROR] FCM: Fallo al intentar eliminar el token inválido: {e}")

        except Exception as e:
            # Captura cualquier otro tipo de error (ej. de red) sin eliminar el token.
            print(f"[WARN] FCM: Fallo al enviar al token ...{token[-6:]} por otra razón. Error: {e}")


def registrar_evento(ev):
    try:
        requests.post(f'{MAIN3_API_BASE_URL}/events/add',
                      json=ev, timeout=5)
    except Exception as e:
        print(f'[WARN] registrar_evento: {e}')
# =========================


# =========== LOOP =========
def main():
    history = []
    # --- Estas variables ahora se definen dentro del bucle principal ---

    while True:
        blobs = [b for b in bucket.list_blobs(prefix=PREF_UPLOADS) if not b.name.endswith('/')]
        if not blobs:
            time.sleep(5)
            continue

        for blob in blobs:
            try:
                # 1. IDENTIFICAR DISPOSITIVO Y PROPIETARIO
                nombre = os.path.basename(blob.name)
                device_id = nombre.split('_')[0] if '_' in nombre else 'unknown'
                owner_snap_query = db.collection('usuarios').where('devices', 'array_contains', device_id).limit(1).stream()
                owner_snap = next(owner_snap_query, None)
                if not owner_snap:
                    print(f"[WARN] No se encontró propietario para {device_id}. Borrando imagen.")
                    blob.delete()
                    continue
                owner_id = owner_snap.id

                # 2. CARGAR EMBEDDINGS ESPECÍFICOS DEL USUARIO
                known_embs, known_labels = cargar_embeddings_por_usuario(owner_id)

                # 3. PROCESAR IMAGEN
                img_np = np.frombuffer(blob.download_as_bytes(), np.uint8)
                img = cv2.imdecode(img_np, cv2.IMREAD_COLOR)
                img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                
                # 4. ÁRBOL DE DECISIÓN: ¿Qué tipo de evento es?
                evento, title, body = None, '', ''
                utc_now_obj = datetime.now(timezone.utc)

                personas_yolo = [] # Simulación de YOLO si no lo usas. Si lo usas, reemplaza con tu lógica.
                faces_mtcnn = detector.detect_faces(img_rgb)

                # --- CASO 1: PERSONA DETECTADA, PERO SIN ROSTRO VISIBLE ---
                if len(faces_mtcnn) == 0 and len(personas_yolo) > 0: # Ajusta 'personas_yolo' si es necesario
                    # (Lógica de conteo que ya funciona)
                    now = time.time()
                    if (device_id not in no_face_tracker or (now - no_face_tracker[device_id]['timestamp']) > NO_FACE_TIMEOUT_SECONDS):
                        no_face_tracker[device_id] = {'count': 1, 'timestamp': now}
                    else:
                        no_face_tracker[device_id]['count'] += 1
                        no_face_tracker[device_id]['timestamp'] = now
                    
                    print(f"[INFO] Detección consecutiva de rostro cubierto para {device_id}. Conteo: {no_face_tracker[device_id]['count']}.")
                    
                    if no_face_tracker[device_id]['count'] >= NO_FACE_THRESHOLD:
                        print(f"[ALARM] Umbral de rostro cubierto alcanzado para {device_id}!")
                        title = "¡ALERTA DE SEGURIDAD!"
                        body = f"Posible intruso cubriendo su rostro en la cámara {device_id}."
                        evento = {'person_name': 'Rostro Cubierto', 'event_type': 'person_no_face_alarm'}
                        no_face_tracker.pop(device_id, None)

                # --- CASO 2: SÍ SE DETECTARON ROSTROS ---
                elif len(faces_mtcnn) > 0:
                    unknowns, known_set = [], set()
                    labels_corner = []

                    for face in faces_mtcnn:
                        x, y, w, h = [abs(int(v)) for v in face['box']]
                        if w < 30 or h < 30: continue

                        face_rgb = cv2.resize(img_rgb[y:y+h, x:x+w], (160, 160))
                        emb = embedder.embeddings(np.expand_dims(face_rgb, 0))[0]

                        name, best = 'Desconocido', 1.0
                        # --- Esta es tu lógica de comparación de embeddings (Punto 2) ---
                        for kv, kn in zip(known_embs, known_labels):
                            d = cosine(emb, kv)
                            if d < best:
                                best = d
                                if d < DIST_THRESHOLD:
                                    name = kn
                        # -------------------------------------------------------------

                        if name == 'Desconocido':
                            unknowns.append({'emb': emb})
                        else:
                            known_set.add(name)

                    # Lógica de decisión después de analizar todos los rostros
                    if len(unknowns) >= 2:
                        title = '¡ALERTA GRUPAL!'
                        body = f'{len(unknowns)} desconocidos en {device_id}.'
                        evento = {'person_name': 'Desconocidos (Grupo)', 'event_type': 'unknown_group'}
                    elif len(unknowns) == 1:
                        # Aquí puedes añadir tu lógica para desconocidos recurrentes si quieres
                        title = 'Persona desconocida detectada'
                        body = f'Rostro no identificado en {device_id}.'
                        evento = {'person_name': 'Desconocido', 'event_type': 'unknown_person'}
                    elif known_set:
                        personas_txt = ', '.join(sorted(known_set))
                        title = 'Persona conocida detectada'
                        body = f'{personas_txt} en cámara {device_id}.'
                        evento = {'person_name': personas_txt, 'event_type': 'known_person'}

                # 5. PUNTO DE ACCIÓN FINAL
                # Si se generó CUALQUIER tipo de evento, se procesa aquí
                if evento:
                    ok, buff = cv2.imencode('.jpg', img)
                    img_url = None
                    if ok:
                        pref = PREF_GROUPS if evento.get('event_type') == 'unknown_group' else PREF_PROCESSED
                        out_blob = bucket.blob(pref + nombre.replace('.jpg', '_proc.jpg'))
                        out_blob.upload_from_string(buff.tobytes(), content_type='image/jpeg')
                        out_blob.make_public()
                        img_url = out_blob.public_url

                    # Completamos los datos del evento
                    evento.update({
                        'timestamp': utc_now_obj.isoformat(),
                        'device_id': device_id,
                        'image_url': img_url,
                        'event_details': body,
                    })
                    
                    # Lo registramos en Firestore
                    registrar_evento(evento) 

                    # 1. Obtenemos la preferencia del usuario del documento que ya tenemos
                    user_settings = owner_snap.to_dict()
                    notification_preference = user_settings.get('notification_preference', 'all')

                    # 2. Definimos qué es una alerta crítica
                    is_critical_alert = evento['event_type'] in [
                        'unknown_person', 
                        'unknown_person_repeated_alarm', 
                        'unknown_group',
                        'person_no_face_alarm',
                        'alarm'
                    ]

                    # 3. Decidimos si enviar la notificación
                    should_send_fcm = False
                    if notification_preference == 'all':
                        should_send_fcm = True
                    elif notification_preference == 'alerts_only' and is_critical_alert:
                        should_send_fcm = True

                    # 4. Si la decisión es enviar, preparamos los datos y llamamos a send_fcm
                    if should_send_fcm:
                        print(f"[INFO] La preferencia del usuario es '{notification_preference}'. Enviando notificación...")
                        event_data_for_fcm = {
                            'title': title,
                            'body': body,
                            'image_url': img_url,
                            'event_type': evento['event_type'],
                            'device_id': device_id
                        }
                        send_fcm(owner_id, event_data_for_fcm)
                    else:
                        print(f"[INFO] La preferencia del usuario es '{notification_preference}'. Notificación para evento '{evento['event_type']}' suprimida.")
                    # Y enviamos la notificación (respetando las preferencias del usuario)
                    # (Aquí va tu bloque de código que decide si enviar o no y llama a send_fcm)
                    # ...

            except Exception as e:
                print(f"[CRITICAL] Error procesando el blob {blob.name}: {e}")
            finally:
                blob.delete()
        
        time.sleep(3)



# =========== MAIN =========
if __name__=='__main__':
    main()
