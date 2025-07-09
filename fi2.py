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
    while True:
        blobs = [b for b in bucket.list_blobs(prefix=PREF_UPLOADS) if not b.name.endswith('/')]
        if not blobs:
            time.sleep(5)
            continue

        for blob in blobs:
            try:
                # 1. Identificar dispositivo y propietario (sin cambios)
                nombre = os.path.basename(blob.name)
                device_id = nombre.split('_')[0] if '_' in nombre else 'unknown'
                owner_snap_query = db.collection('usuarios').where('devices', 'array_contains', device_id).limit(1).stream()
                owner_snap = next(owner_snap_query, None)
                if not owner_snap:
                    print(f"[WARN] No se encontró propietario para el dispositivo {device_id}. Borrando imagen.")
                    blob.delete()
                    continue
                
                owner_id = owner_snap.id
                
                # 2. **CAMBIO CLAVE**: Cargar embeddings SOLO para este usuario
                known_embs, known_labels = cargar_embeddings_por_usuario(owner_id)
                if not known_embs:
                    print(f"[INFO] El usuario {owner_id} no tiene rostros registrados. Se tratarán todos como desconocidos.")

                # 3. Procesamiento de la imagen y lógica de detección (el resto del código es casi igual)
                img_np = np.frombuffer(blob.download_as_bytes(), np.uint8)
                img = cv2.imdecode(img_np, cv2.IMREAD_COLOR)

                utc_now = datetime.now(timezone.utc)

                # --- YOLO personas ---
                personas = []
                for *xywh, conf, cls in yolo(img).xywh[0]:
                    if conf < 0.5 or NAMES[int(cls)] != 'person':
                        continue
                    x, y, w, h = map(int, xywh)
                    px, py = x - w//2, y - h//2
                    personas.append((px, py, w, h))
                    cv2.rectangle(img, (px, py), (px+w, py+h), (0,255,255), 2)

                # --- Rostros ---
                faces = detector.detect_faces(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
                unknowns, known_set = [], set()
                labels_corner = []         # ← nombres a mostrar en la esquina

# En la función main(), dentro del bucle 'for blob in blobs:'
# ... después de las líneas que obtienen 'personas' y 'faces'...

                # --- INICIO DE LA NUEVA LÓGICA PARA ROSTRO CUBIERTO ---
                evento, title, body = None, '', ''
                utc_now_obj = datetime.now(timezone.utc)

                # Condición: Se detectó al menos una persona, pero CERO rostros.
                if len(personas) > 0 and len(faces) == 0:
                    print(f"[INFO] Detección de persona sin rostro en {device_id}.")
                    
                    # Revisamos nuestra memoria de seguimiento
                    now = time.time()
                    if (device_id not in no_face_tracker or 
                        (now - no_face_tracker[device_id]['timestamp']) > NO_FACE_TIMEOUT_SECONDS):
                        # Si es la primera vez o ha pasado mucho tiempo, reiniciamos el contador
                        no_face_tracker[device_id] = {'count': 1, 'timestamp': now}
                        print(f"[INFO] Iniciando seguimiento de rostro cubierto para {device_id}.")
                    else:
                        # Si es una detección reciente, incrementamos el contador
                        no_face_tracker[device_id]['count'] += 1
                        no_face_tracker[device_id]['timestamp'] = now
                        print(f"[INFO] Detección consecutiva de rostro cubierto para {device_id}. Conteo: {no_face_tracker[device_id]['count']}.")

                    # Comprobamos si hemos alcanzado el umbral para la alarma
                    if no_face_tracker[device_id]['count'] >= NO_FACE_THRESHOLD:
                        print(f"[ALARM] Umbral de rostro cubierto alcanzado para {device_id}!")
                        title = "¡ALERTA DE SEGURIDAD!"
                        body = f"Posible intruso cubriendo su rostro en la cámara {device_id}."
                        evento = {
                            'person_name': 'Rostro Cubierto',
                            'event_type': 'person_no_face_alarm' # Nuevo tipo de evento
                        }
                        # Reiniciamos el contador para no enviar la misma alarma repetidamente
                        no_face_tracker.pop(device_id, None)

                # --- FIN DE LA NUEVA LÓGICA ---

            # El resto de tu lógica para procesar los rostros que SÍ se encontraron
            # (El bucle for f in faces: y la lógica de conocidos/desconocidos)
            # ...

                for f in faces:
                    x,y,w,h = [abs(int(v)) for v in f['box']]
                    if w<30 or h<30: continue
                    face_rgb = cv2.resize(
                        cv2.cvtColor(img[y:y+h,x:x+w], cv2.COLOR_BGR2RGB), (160,160))
                    emb = embedder.embeddings(np.expand_dims(face_rgb,0))[0]

                    name, best = 'Desconocido', 1.0
                    for kv, kn in zip(known_embs, known_labels):
                        d = cosine(emb, kv)
                        if d < best:
                            best = d
                            if d < DIST_THRESHOLD:
                                name = kn
                                break

                    color = (0,255,0) if name!='Desconocido' else (0,0,255)
                    cv2.rectangle(img,(x,y),(x+w,y+h),color,2)

                    # Guardar etiqueta para la esquina
                    labels_corner.append(name)

                    if name=='Desconocido':
                        if any(px<x<px+pw and py<y<py+ph for px,py,pw,ph in personas):
                            unknowns.append({'emb': emb})
                    else:
                        known_set.add(name)

                # --- Dibujar etiquetas en esquina sup-izq ---
                y_offset = 20
                for lbl in labels_corner:
                    put_text_outline(img, lbl, 10, y_offset)
                    y_offset += 18

                # --- Reglas de evento (sin cambios) ---
                evento, title, body = None, '', ''
                is_group = len(unknowns) >= 2

                if known_set:
                    personas_txt = ', '.join(sorted(known_set))
                    title = 'Persona conocida detectada'
                    body  = f'{personas_txt} en cámara {device_id}.'
                    evento = {'person_name': personas_txt, 'event_type': 'known_person'}

                if unknowns and len(unknowns)==1:
                    title = 'Persona desconocida detectada'
                    body  = f'Rostro no identificado en {device_id}.'
                    evento = {'person_name': 'Desconocido',
                              'event_type': 'unknown_person'}

                if is_group:
                    title = '¡ALERTA GRUPAL!'
                    body  = f'{len(unknowns)} desconocidos en {device_id}.'
                    evento = {'person_name': 'Desconocidos (Grupo)',
                              'event_type': 'unknown_group'}

                if evento and evento['event_type']=='unknown_person':
                    emb = unknowns[0]['emb']
                    rep=False
                    for hst in history:
                        if cosine(emb,hst['emb']) < SIM_THRESHOLD:
                            rep=True
                            hst['count']+=1
                            if (hst['count']>=REPEAT_THRESHOLD and
                                (utc_now-hst['last']).total_seconds()>COOLDOWN_SECONDS):
                                title='Desconocido recurrente'
                                body=f'Rostro desconocido repetido en {device_id}.'
                                evento['event_type']='unknown_person_repeat'
                                hst['last']=utc_now
                            break
                    if not rep:
                        history.append({'emb':emb,'count':1,'last':utc_now})
                    history[:] = [h for h in history
                                  if (utc_now-h['last']).total_seconds()<60]

                # --- Subir img & notificar (sin cambios) ---
                img_url=None
                if evento:
                    ok,buff = cv2.imencode('.jpg', img)
                    if ok:
                        pref = PREF_GROUPS if is_group else PREF_PROCESSED
                        out_blob = bucket.blob(pref + nombre.replace('.jpg','_proc.jpg'))
                        out_blob.upload_from_string(buff.tobytes(),
                                                    content_type='image/jpeg')
                        out_blob.make_public()
                        img_url = out_blob.public_url

                    evento.update({
                        'timestamp'    : utc_now.isoformat(),
                        'device_id'    : device_id,
                        'image_url'    : img_url,
                        'event_details': body,
                    })
                     # 1. Obtenemos la preferencia del usuario del documento que ya tenemos
                    user_settings = owner_snap.to_dict()
                    notification_preference = user_settings.get('notification_preference', 'all')
                    
                    # 2. Definimos qué es una alerta
                    is_critical_alert = evento['event_type'] in [
                        'unknown_person', 
                        'unknown_person_repeated_alarm', 
                        'unknown_group',
                        'person_no_face_alarm',
                        'alarm'
                    ]
                    # 3. Decidimos si enviar la notificación basándonos en la preferencia
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
                        # Si no, simplemente lo registramos en el log y no hacemos nada más
                        print(f"[INFO] La preferencia del usuario es '{notification_preference}'. Notificación para evento '{evento['event_type']}' suprimida.")
                    
                    registrar_evento(evento)

            except Exception as e:
                print(f"[CRITICAL] Error procesando el blob {blob.name}: {e}")
            finally:
                # Asegurarse de borrar el blob procesado (o fallido)
                blob.delete()
        
        time.sleep(3)



# =========== MAIN =========
if __name__=='__main__':
    main()
