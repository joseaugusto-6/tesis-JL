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
print('[OK] Firebase inicializado')
# =========================

# ====== MODELOS ==========
detector = MTCNN()
embedder = FaceNet()
yolo     = torch.hub.load('ultralytics/yolov5', 'yolov5n', trust_repo=True)
NAMES    = yolo.names
# =========================

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


def cargar_embeddings():
    embs, labels = [], []
    for b in bucket.list_blobs(prefix=PREF_EMBEDS):
        if b.name.endswith('.npy'):
            vec = np.load(io.BytesIO(b.download_as_bytes()),
                          allow_pickle=True).item()
            embs.extend(vec['embeddings'])
            labels.extend([vec['name']] * len(vec['embeddings']))
    print(f'[INFO] embeddings cargados: {len(embs)}')
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
    last_emb, known_embs, known_labels = 0, [], []
    history = []  # [{emb,count,last}]

    while True:
        now = time.time()
        if now - last_emb > EMB_REFRESH_SEC or not known_embs:
            known_embs, known_labels = cargar_embeddings()
            last_emb = now

        blobs = [b for b in bucket.list_blobs(prefix=PREF_UPLOADS)
                 if not b.name.endswith('/')]
        if not blobs:
            time.sleep(5); continue

        for blob in blobs:
            nombre    = os.path.basename(blob.name)
            device_id = nombre.split('_')[0] if '_' in nombre else 'unknown'

            owner_snap = next(db.collection('usuarios')
                               .where('devices','array_contains',device_id)
                               .limit(1).stream(), None)
            owner_id = owner_snap.id if owner_snap else None
            if not owner_id:
                blob.delete(); continue

            img_np = np.frombuffer(blob.download_as_bytes(), np.uint8)
            img    = cv2.imdecode(img_np, cv2.IMREAD_COLOR)
            if img is None:
                blob.delete(); continue

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
         # 1. Creamos un diccionario con todos los datos para la notificación
          event_data_for_fcm = {
              'title': title,
              'body': body,
              'image_url': img_url,
              'event_type': evento['event_type'],
              'device_id': device_id
          }

          # 2. Hacemos la llamada correcta con solo 2 argumentos: el email y el diccionario
          send_fcm(owner_id, event_data_for_fcm)
                registrar_evento(evento)

            blob.delete()

        time.sleep(3)

# =========== MAIN =========
if __name__=='__main__':
    main()
