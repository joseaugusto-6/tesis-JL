#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fi.py – Worker de SecurityCamApp (versión limpia)
-------------------------------------------------
• Observa continuamente la carpeta **uploads/** de Firebase Storage.
• Para cada imagen:
    1. Detecta personas (YOLOv5‑Nano) y rostros (MTCNN + FaceNet).
    2. Clasifica rostro (conocido / desconocido) comparando con embeddings.
    3. Genera *eventos* y notificaciones FCM según reglas:
        – known_person            → al menos 1 rostro conocido
        – unknown_person          → 1 rostro desconocido
        – unknown_group           → ≥2 rostros desconocidos
        – unknown_person_repeat   → mismo desconocido ≥3 veces, cool‑down 30 s
    4. Sube la imagen procesada (solo si hubo evento) a
       alarmas_procesadas/ o alertas_grupales/.
    5. Llama al backend Flask (`/api/events/add`) para llenar el Historial
       de la app Flutter.
    6. Borra siempre la imagen original.
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

import requests                         # → registrar eventos backend
import firebase_admin
from firebase_admin import credentials, storage, messaging, firestore
# ==========================

# ======== CONFIG =========
SERVICE_ACCOUNT_FILE = 'security-cam-f322b-firebase-adminsdk-fbsvc-a3bf0dd37b.json'
PROJECT_ID           = 'security-cam-f322b'
BUCKET_ID            = f'{PROJECT_ID}.firebasestorage.app'

PREF_UPLOADS   = 'uploads/'
PREF_PROCESSED = 'alarmas_procesadas/'
PREF_GROUPS    = 'alertas_grupales/'
PREF_EMBEDS    = 'embeddings_clientes/'

MAIN3_API_BASE_URL   = 'https://tesisdeteccion.ddns.net/api'

DIST_THRESHOLD   = 0.50   # match conocido
SIM_THRESHOLD    = 0.40   # similitud p/duplicados
REPEAT_THRESHOLD = 3      # veces p/unknown_person_repeat
COOLDOWN_SECONDS = 30

EMB_REFRESH_SEC  = 600    # recargar embeddings
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

def cargar_embeddings() -> tuple[list[np.ndarray], list[str]]:
    """Descarga .npy de Storage y devuelve pares (embedding, label)."""
    embs, labels = [], []
    for b in bucket.list_blobs(prefix=PREF_EMBEDS):
        if not b.name.endswith('.npy'):
            continue
        vec = np.load(io.BytesIO(b.download_as_bytes()), allow_pickle=True).item()
        embs.extend(vec['embeddings'])
        labels.extend([vec['name']] * len(vec['embeddings']))
    print(f'[INFO] embeddings cargados: {len(embs)}')
    return embs, labels


def send_fcm(owner: str, title: str, body: str,
             image_url: str | None, data: dict):
    """Push a cada token FCM del usuario."""
    try:
        doc = db.collection('usuarios').document(owner).get()
        if not doc.exists:
            print(f'[WARN] usuario {owner} sin doc/tokens')
            return
        for t in doc.to_dict().get('fcm_tokens', []):
            msg = messaging.Message(
                token=t,
                notification=messaging.Notification(title=title, body=body, image=image_url),
                data=data,
            )
            messaging.send(msg)
    except Exception as e:
        print(f'[WARN] FCM error: {e}')


def registrar_evento(ev: dict):
    """POST al backend para que aparezca en Historial."""
    try:
        r = requests.post(f'{MAIN3_API_BASE_URL}/events/add', json=ev, timeout=5)
        if r.status_code not in (200, 201):
            print(f'[WARN] backend {r.status_code}: {r.text[:100]}')
    except Exception as e:
        print(f'[WARN] registrar_evento: {e}')
# =========================

# =========== LOOP =========

def main():
    last_emb, known_embs, known_labels = 0, [], []
    history: list[dict] = []  # memoria 60 s de desconocidos

    while True:
        now = time.time()
        if now - last_emb > EMB_REFRESH_SEC or not known_embs:
            known_embs, known_labels = cargar_embeddings()
            last_emb = now

        blobs = [b for b in bucket.list_blobs(prefix=PREF_UPLOADS)
                 if not b.name.endswith('/')]
        if not blobs:
            time.sleep(5)
            continue

        for blob in blobs:
            nombre = os.path.basename(blob.name)
            device_id = nombre.split('_')[0] if '_' in nombre else 'unknown'

            # Owner
            owner_snap = next(db.collection('usuarios')
                               .where('devices', 'array_contains', device_id)
                               .limit(1).stream(), None)
            owner_id = owner_snap.id if owner_snap else None
            if not owner_id:
                blob.delete(); continue

            # Leer imagen
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
                personas.append((x - w//2, y - h//2, w, h))
                cv2.rectangle(img, (x - w//2, y - h//2),
                              (x + w//2, y + h//2), (0,255,255), 2)

            # --- Rostros ---
            faces = detector.detect_faces(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            unknowns, known_set = [], set()
            for f in faces:
                x,y,w,h = [abs(int(v)) for v in f['box']]
                if w<30 or h<30: continue
                face_rgb = cv2.resize(cv2.cvtColor(img[y:y+h,x:x+w], cv2.COLOR_BGR2RGB),(160,160))
                emb = embedder.embeddings(np.expand_dims(face_rgb,0))[0]

                name, best = 'Desconocido', 1.0
                for kvec, kname in zip(known_embs, known_labels):
                    d = cosine(emb, kvec)
                    if d < best:
                        best, name = d, kname if d < DIST_THRESHOLD else 'Desconocido'
                color = (0,255,0) if name!='Desconocido' else (0,0,255)
                cv2.rectangle(img,(x,y),(x+w,y+h),color,2)
                cv2.putText(img,name,(x,y-5),cv2.FONT_HERSHEY_SIMPLEX,0.6,color,2)

                if name=='Desconocido':
                    if any(px<x<px+pw and py<y<py+ph for px,py,pw,ph in personas):
                        unknowns.append({'emb':emb})
                else:
                    known_set.add(name)

            # --- reglas de evento ---
            evento, title, body = None, '', ''
            is_group = len(unknowns) >= 2

            if known_set:
                personas = ', '.join(sorted(known_set))
                title, body = 'Persona conocida detectada', f'{personas} en cámara {device_id}.'
                evento = {'person_name': personas,'event_type':'known_person'}

            if unknowns and len(unknowns)==1:
                title, body = 'Persona desconocida detectada', f'Rostro no identificado en {device_id}.'
                evento = {'person_name':'Desconocido','event_type':'unknown_person'}

            if is_group:
                title, body = '¡ALERTA GRUPAL!', f'{len(unknowns)} desconocidos en {device_id}.'
                evento = {'person_name':'Desconocidos (Grupo)','event_type':'unknown_group'}

            # rostro repetido lógica
            if evento and evento['event_type']=='unknown_person':
                emb = unknowns[0]['emb']
                repetido=False
                for h in history:
                    if cosine(emb,h['emb'])<SIM_THRESHOLD:
                        h['count']+=1; repetido=True
                        if h['count']>=REPEAT_THRESHOLD and (utc_now-h['last']).total_seconds()>COOLDOWN_SECONDS:
                            title='Desconocido recurrente'
                            body=f'Rostro desconocido repetido en {device_id}.'
                            evento['event_type']='unknown_person_repeat'
                            h['last']=utc_now
                        break
                if not repetido:
                    history.append({'emb':emb,'count':1,'last':utc_now})
                history[:] = [h for h in history if (utc_now-h['last']).total_seconds()<60]

            # --- subir imagen procesada & notificar ---
            img_url=None
            if evento:
                ok,buff = cv2.imencode('.jpg',img)
                if ok:
                    dest = (PREF_GROUPS if is_group else PREF_PROCESSED)+nombre.replace('.jpg','_proc.jpg')
                    out = bucket.blob(dest)
                    out.upload_from_string(buff.tobytes(),content_type='image/jpeg')
                    out.make_public(); img_url=out.public_url
                # completar evento dict
                evento.update({
                    'timestamp': utc_now.isoformat(),
                    'device_id': device_id,
                    'image_url': img_url,
                    'event_details': body,
                })
                # notificación + registro
                send_fcm(owner_id,title,body,img_url,{'event_type':evento['event_type'],'device_id':device_id})
                registrar_evento(evento)

            blob.delete()

        time.sleep(3)

# =========== MAIN =========
if __name__=='__main__':
    main()
