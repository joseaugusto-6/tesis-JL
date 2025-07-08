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

DIST_THRESHOLD   = 0.50
SIM_THRESHOLD    = 0.40
REPEAT_THRESHOLD = 3
COOLDOWN_SECONDS = 30
EMB_REFRESH_SEC  = 600
# =========================


# ===== FIREBASE INIT =====
cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
firebase_admin.initialize_app(cred, {
    'projectId': PROJECT_ID,
    'storageBucket': BUCKET_ID,
})
bucket = storage.bucket()
db     = firestore.client()
# =========================

# ====== MODELOS ==========
detector = MTCNN()
embedder = FaceNet()
yolo     = torch.hub.load('ultralytics/yolov5', 'yolov5x', trust_repo=True)
NAMES    = yolo.names
# =========================
print('[OK] Firebase y Modelos de IA inicializados.')

# --- NUEVO: Caché para los embeddings de los usuarios ---
# Formato: {'user_email': {'embeddings': [...], 'labels': [...], 'timestamp': ...}}
embeddings_cache = {}

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
                
                # ... El resto de tu lógica para detectar rostros, comparar, definir evento, etc. ...
                # ...
                # if evento:
                #     ...
                #     # Lógica de filtrado de notificaciones y llamada a send_fcm
                #     ...

            except Exception as e:
                print(f"[CRITICAL] Error procesando el blob {blob.name}: {e}")
            finally:
                # Asegurarse de borrar el blob procesado (o fallido)
                blob.delete()

        time.sleep(3)

# =========== MAIN =========
if __name__=='__main__':
    main()
