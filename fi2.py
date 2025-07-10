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


# Pega esta función al principio de tu archivo fi.py

def draw_text_with_outline(img, text, position, font_scale, color, thickness):
    """
    Dibuja un texto con un borde negro para mejorar la legibilidad.
    """
    # Dibuja el borde negro (dibujando el texto 4 veces con un ligero desfase)
    font = cv2.FONT_HERSHEY_SIMPLEX
    outline_color = (0, 0, 0) # Negro
    cv2.putText(img, text, (position[0]-1, position[1]-1), font, font_scale, outline_color, thickness+1, cv2.LINE_AA)
    cv2.putText(img, text, (position[0]+1, position[1]-1), font, font_scale, outline_color, thickness+1, cv2.LINE_AA)
    cv2.putText(img, text, (position[0]-1, position[1]+1), font, font_scale, outline_color, thickness+1, cv2.LINE_AA)
    cv2.putText(img, text, (position[0]+1, position[1]+1), font, font_scale, outline_color, thickness+1, cv2.LINE_AA)
    
    # Dibuja el texto principal encima
    cv2.putText(img, text, position, font, font_scale, color, thickness, cv2.LINE_AA)


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
#aaaa

# Reemplaza tu función main() completa por esta versión corregida
# ======================== FUNCIÓN MAIN COMPLETA Y CORREGIDA ========================
def main():
    history = [] # Para el seguimiento de desconocidos recurrentes

    while True:
        # Busca nuevos archivos en la carpeta de subidas
        blobs = [b for b in bucket.list_blobs(prefix=PREF_UPLOADS) if not b.name.endswith('/')]
        if not blobs:
            time.sleep(5)
            continue

        for blob in blobs:
            try:
                # 1. IDENTIFICAR PROPIETARIO DEL DISPOSITIVO
                nombre_archivo = os.path.basename(blob.name)
                device_id = nombre_archivo.split('_')[0] if '_' in nombre_archivo else 'unknown'
                
                owner_snap_query = db.collection('usuarios').where('devices', 'array_contains', device_id).limit(1).stream()
                owner_snap = next(owner_snap_query, None)
                
                if not owner_snap:
                    print(f"[WARN] No se encontró propietario para {device_id}. Borrando imagen.")
                    blob.delete()
                    continue
                
                owner_id = owner_snap.id
                
                # 2. CARGAR EMBEDDINGS ESPECÍFICOS PARA ESE USUARIO
                known_embs, known_labels = cargar_embeddings_por_usuario(owner_id)

                # 3. PROCESAR LA IMAGEN
                img_np = np.frombuffer(blob.download_as_bytes(), np.uint8)
                img = cv2.imdecode(img_np, cv2.IMREAD_COLOR)
                img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                
                # Inicializar variables para el evento
                evento, title, body = None, '', ''
                utc_now_obj = datetime.now(timezone.utc)

                # 4. EJECUTAR MODELOS DE IA
                # --- INICIO DE LA CORRECCIÓN ---
                # Primero, detectamos personas con YOLO y llenamos la lista 'personas'
                print(f"[INFO] Ejecutando YOLO para detectar personas...")
                personas = []
                # Asegúrate de que 'yolo' y 'NAMES' estén definidos globalmente al inicio del script
                yolo_results = yolo(img_rgb) 
                for *xywh, conf, cls in yolo_results.xywh[0]:
                    if conf > 0.5 and NAMES[int(cls)] == 'person':
                        personas.append(xywh)
                        x_yolo, y_yolo, w_yolo, h_yolo = map(int, xywh)
                        px, py = x_yolo - w_yolo//2, y_yolo - h_yolo//2
                        # El color amarillo en formato BGR (Blue, Green, Red) es (0, 255, 255)
                        cv2.rectangle(img, (px, py), (px + w_yolo, py + h_yolo), (0, 255, 255), 2)
               
                print(f"[INFO] YOLO encontró {len(personas)} persona(s).")

                # Segundo, detectamos rostros con MTCNN
                print(f"[INFO] Ejecutando MTCNN para detectar rostros...")
                faces = detector.detect_faces(img_rgb)
                print(f"[INFO] MTCNN encontró {len(faces)} rostro(s).")
                # --- FIN DE LA CORRECCIÓN ---
                
                # 5. ÁRBOL DE DECISIÓN: ¿Qué tipo de evento es esta imagen?
                
                # --- CASO A: PERSONA(S) DETECTADA(S), PERO NINGÚN ROSTRO VISIBLE ---
                # Esta condición es la clave: hay "personas" pero no "rostros".
                if len(personas) > 0 and len(faces) == 0:
                    print(f"[INFO] Detección de persona sin rostro en {device_id}.")
                    
                    now = time.time()
                    # Si es la primera vez que vemos esto en esta cámara o ha pasado mucho tiempo, (re)iniciamos el contador
                    if (device_id not in no_face_tracker or 
                        (now - no_face_tracker[device_id]['timestamp']) > NO_FACE_TIMEOUT_SECONDS):
                        no_face_tracker[device_id] = {'count': 1, 'timestamp': now}
                        print(f"[INFO] Iniciando seguimiento de rostro cubierto para {device_id}.")
                    else:
                        # Si es una detección reciente en la misma cámara, incrementamos el contador
                        no_face_tracker[device_id]['count'] += 1
                        no_face_tracker[device_id]['timestamp'] = now
                        print(f"[INFO] Detección consecutiva de rostro cubierto para {device_id}. Conteo: {no_face_tracker[device_id]['count']}.")

                    # Comprobamos si hemos alcanzado el umbral para disparar la alarma
                    if no_face_tracker[device_id]['count'] >= NO_FACE_THRESHOLD:
                        print(f"[ALARM] Umbral de rostro cubierto alcanzado para {device_id}!")
                        title = "¡ALERTA DE SEGURIDAD!"
                        body = f"Posible intruso cubriendo su rostro en la cámara {device_id}."
                        evento = {
                            'person_name': 'Rostro Cubierto',
                            'event_type': 'person_no_face_alarm'
                        }
                        # Reiniciamos el contador para esta cámara para no enviar la misma alarma repetidamente
                        no_face_tracker.pop(device_id, None)

                # --- CASO B: SÍ SE DETECTARON ROSTROS ---
                elif len(faces) > 0:
                    print(f"[INFO] Condición cumplida: Procesando {len(faces)} rostro(s) encontrado(s).")
                    unknowns, known_set = [], set()
                    detected_names = set()


                    for face in faces:
                        x, y, w, h = [abs(int(v)) for v in face['box']]
                        if w < 30 or h < 30: continue
                        
                        face_rgb = cv2.resize(img_rgb[y:y+h, x:x+w], (160, 160))
                        emb = embedder.embeddings(np.expand_dims(face_rgb, 0))[0]

                        name = "Desconocido"
                        if known_embs: # Solo comparar si el usuario tiene rostros registrados
                            best_dist = 1.0
                            for kv, kn in zip(known_embs, known_labels):
                                dist = cosine(emb, kv)
                                if dist < best_dist:
                                    best_dist = dist
                                    if dist < DIST_THRESHOLD:
                                        name = kn
                        
                        color = (0, 255, 0) if name != 'Desconocido' else (0, 0, 255)
                        cv2.rectangle(img, (x, y), (x + w, y + h), color, 2)

                        if name == 'Desconocido':
                            unknowns.append({'emb': emb})
                        else:
                            known_set.add(name)
                        
                        detected_names.add(name)

                    if detected_names:
                        display_text = ", ".join(sorted(list(detected_names)))
                        
                        # Calculamos el tamaño del texto para posicionarlo bien
                        font_scale = 0.7
                        thickness = 2
                        (text_width, text_height), _ = cv2.getTextSize(display_text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
                        
                        # Posición en la esquina superior derecha con un margen de 10px
                        image_height, image_width, _ = img.shape
                        position = (image_width - text_width - 10, text_height + 10)
                        
                        # Llamamos a nuestra nueva función para dibujar
                        draw_text_with_outline(img, display_text, position, font_scale, (255, 255, 255), thickness)

                    # Decisión basada en los rostros encontrados
                    if len(unknowns) >= 2:
                        title, body = '¡ALERTA GRUPAL!', f'{len(unknowns)} desconocidos en {device_id}.'
                        evento = {'person_name': 'Desconocidos (Grupo)', 'event_type': 'unknown_group'}
                    elif len(unknowns) == 1:
                        title, body = 'Persona desconocida detectada', f'Rostro no identificado en {device_id}.'
                        evento = {'person_name': 'Desconocido', 'event_type': 'unknown_person'}
                    elif known_set:
                        personas_txt = ', '.join(sorted(known_set))
                        title, body = 'Persona conocida detectada', f'{personas_txt} en cámara {device_id}.'
                        evento = {'person_name': personas_txt, 'event_type': 'known_person'}

                # 6. PUNTO DE ACCIÓN FINAL
                # Si se generó CUALQUIER tipo de evento en los pasos anteriores, se procesa aquí.
                if evento:
                    # Subir la imagen procesada (con los recuadros)
                    ok, buff = cv2.imencode('.jpg', img)
                    img_url = None
                    if ok:
                        pref = PREF_GROUPS if evento.get('event_type') == 'unknown_group' else PREF_PROCESSED
                        out_blob_name = pref + nombre_archivo.replace('.jpg', '_proc.jpg')
                        out_blob = bucket.blob(out_blob_name)
                        out_blob.upload_from_string(buff.tobytes(), content_type='image/jpeg')
                        out_blob.make_public()
                        img_url = out_blob.public_url

                    # Completar y registrar el evento en Firestore
                    evento.update({
                        'timestamp': utc_now_obj.isoformat(),
                        'device_id': device_id,
                        'image_url': img_url,
                        'event_details': body,
                    })
                    registrar_evento(evento) # Asegúrate de que esta función exista y esté correcta

                    # Enviar notificación push respetando las preferencias del usuario
                    user_settings = owner_snap.to_dict()
                    pref = user_settings.get('notification_preference', 'all')
                    is_critical = evento['event_type'] not in ['known_person']

                    if pref == 'all' or (pref == 'alerts_only' and is_critical):
                        print(f"[INFO] Preferencia '{pref}', enviando notificación para evento '{evento['event_type']}'.")
                        fcm_data = {'title': title, 'body': body, 'image_url': img_url, **evento}
                        send_fcm(owner_id, fcm_data)
                    else:
                        print(f"[INFO] Preferencia '{pref}', notificación suprimida para evento '{evento['event_type']}'.")

            except Exception as e:
                print(f"[CRITICAL] Error procesando el blob {nombre_archivo}: {e}")
            finally:
                # Borrar la imagen original de la carpeta 'uploads'
                blob.delete()
        
        time.sleep(3)

# =================================================================================


# =========== MAIN =========
if __name__=='__main__':
    main()
