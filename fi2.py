#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fi.py – Worker de SecurityCamApp
• Descarga cada imagen nueva de Firebase Storage (uploads/).
• Detecta personas (YOLO) y rostros (MTCNN + FaceNet).
• Genera notificaciones FCM **y** registra eventos en el backend Flask
  para que aparezcan en el Historial de la app Flutter.
    – known_person            → título “Persona conocida detectada”
    – unknown_person          → 1 rostro desconocido (nueva entrada)
    – unknown_group           → ≥2 rostros desconocidos (alerta grupal)
    – unknown_person_repeat   → rostro desconocido repetido ≥3 veces
• Sube la imagen procesada SOLO si hay evento (carpeta alarmas_procesadas/ o alertas_grupales/).
• Borra siempre la imagen original de uploads/.
"""

# ------------ IMPORTS ------------
import io, os, time
from datetime import datetime, timezone

import cv2
import numpy as np
from scipy.spatial.distance import cosine
import torch
from mtcnn import MTCNN
from keras_facenet import FaceNet

import requests  #  <-- NUEVO: registrar eventos en backend
import firebase_admin
from firebase_admin import credentials, storage, messaging, firestore
# ---------------------------------

# ---------- CONFIGURACIÓN GLOBAL ----------
SERVICE_ACCOUNT_FILE = 'security-cam-f322b-firebase-adminsdk-fbsvc-a3bf0dd37b.json'
PROJECT_ID           = 'security-cam-f322b'
BUCKET_ID            = f'{PROJECT_ID}.firebasestorage.app'

PREF_UPLOADS   = 'uploads/'
PREF_PROCESSED = 'alarmas_procesadas/'
PREF_GROUPS    = 'alertas_grupales/'
PREF_EMBEDS    = 'embeddings_clientes/'

# Endpoint del backend Flask (coincide con BASE_URL en ApiService.dart)
MAIN3_API_BASE_URL   = 'https://tesisdeteccion.ddns.net/api'

# Parámetros IA
DIST_THRESHOLD     = 0.50   # similaridad para “match”
SIM_THRESHOLD      = 0.40   # evitar duplicar rostros desconocidos
REPEAT_THRESHOLD   = 3      # veces que se repite rostro desconocido
COOLDOWN_SECONDS   = 30     # anti-spam

# Recarga embeddings cada 10 min
EMB_REFRESH_SEC    = 600
# -------------------------------------------


# ---------- FIREBASE INIT ----------
cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
firebase_admin.initialize_app(cred, {
    "projectId": PROJECT_ID,
    "storageBucket": BUCKET_ID
})
bucket = storage.bucket()
db     = firestore.client()
print("[OK] Firebase inicializado.")
# -----------------------------------


# ------------- MODELOS -------------
detector  = MTCNN()
embedder  = FaceNet()
yolo      = torch.hub.load('ultralytics/yolov5', 'yolov5n', trust_repo=True)
NAMES     = yolo.names
# -----------------------------------


# ---------- FUNCIONES AUX -----------

def cargar_embeddings() -> tuple[list[np.ndarray], list[str]]:
    """Descarga .npy del bucket y devuelve ([embedding], [label])."""
    embs, labels = [], []
    for blob in bucket.list_blobs(prefix=PREF_EMBEDS):
        if not blob.name.endswith('.npy'):
            continue
        vec = np.load(io.BytesIO(blob.download_as_bytes()), allow_pickle=True)
        vec = vec.item() if isinstance(vec, np.ndarray) else vec
        embs.extend(vec['embeddings'])
        labels.extend([vec['name']] * len(vec['embeddings']))
    print(f"[INFO] Embeddings cargados: {len(embs)}")
    return embs, labels


def send_fcm(owner: str, title: str, body: str,
             image_url: str | None, data: dict):
    """Envía push FCM a todos los tokens del usuario."""
    try:
        doc = db.collection('usuarios').document(owner).get()
        if not doc.exists:
            print(f"[WARN] Owner {owner} sin tokens.")
            return
        for token in doc.to_dict().get('fcm_tokens', []):
            msg = messaging.Message(
                token=token,
                notification=messaging.Notification(title=title,
                                                    body=body,
                                                    image=image_url),
                data=data
            )
            messaging.send(msg)
    except Exception as e:
        print(f"[WARN] Error FCM: {e}")


def registrar_evento(evento: dict):
    """Envía el evento al backend para Historial."""
    try:
        r = requests.post(f"{MAIN3_API_BASE_URL}/events/add", json=evento, timeout=5)
        if r.status_code not in (200, 201):
            print(f"[WARN] registrar_evento {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"[WARN] registrar_evento error: {e}")
# ------------------------------------


# -------- BUCLE PRINCIPAL ----------

def main():
    last_emb_load = 0
    known_embs, known_labels = [], []
    history: list[dict] = []  # historial <60 s p/rostros desconocidos

    while True:
        now = time.time()
        if now - last_emb_load > EMB_REFRESH_SEC or not known_embs:
            known_embs, known_labels = cargar_embeddings()
            last_emb_load = now

        # Listar imágenes nuevas
        blobs = [b for b in bucket.list_blobs(prefix=PREF_UPLOADS)
                 if not b.name.endswith('/')]

        if not blobs:
            time.sleep(5)
            continue

        for blob in blobs:
            nombre     = os.path.basename(blob.name)
            device_id  = nombre.split('_')[0] if '_' in nombre else 'unknown'
            owner_q    = db.collection('usuarios') \
                          .where('devices', 'array_contains', device_id) \
                          .limit(1).stream()
            owner      = next(owner_q, None)
            owner_id   = owner.id if owner else None
            if not owner_id:
                blob.delete(); continue  # sin dueño -> descartar

            # Leer imagen a memoria
            img_bytes = np.frombuffer(blob.download_as_bytes(), np.uint8)
            img       = cv2.imdecode(img_bytes, cv2.IMREAD_COLOR)
            if img is None:
                blob.delete(); continue

            utc_now = datetime.now(timezone.utc)

            # ---- Personas (YOLO) ----
            personas = []
            yolo_out = yolo(img)
            for *xywh, conf, cls in yolo_out.xywh[0]:
                if conf < 0.5 or NAMES[int(cls)] != "person":
                    continue
                x, y, w, h = map(int, xywh)
                personas.append((x - w//2, y - h//2, w, h))
                cv2.rectangle(img, (x - w//2, y - h//2),
                              (x + w//2, y + h//2), (0, 255, 255), 2)

            # ---- Rostros ----
            faces = detector.detect_faces(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            unknowns, known_names = [], set()

            for f in faces:
                x, y, w, h = [abs(int(v)) for v in f['box']]
                if w < 30 or h < 30: continue
                face_rgb = cv2.resize(
                    cv2.cvtColor(img[y:y+h, x:x+w], cv2.COLOR_BGR2RGB),
                    (160, 160)
                )
                emb = embedder.embeddings(
                    np.expand_dims(face_rgb, axis=0))[0]

                # Comparar con conocidos
                name, best = "Desconocido", float("inf")
                for kvec, kname in zip(known_embs, known_labels):
                    d = cosine(emb, kvec)
                    if d < best:
                        best = d
                        if d < DIST_THRESHOLD:
                            name = kname
                            break

                color = (0, 255, 0) if name != "Desconocido" else (0, 0, 255)
                cv2.rectangle(img, (x, y), (x+w, y+h), color, 2)
                cv2.putText(img, name, (x, y-5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

                if name == "Desconocido":
                    # validar que rostro esté dentro de bbox persona
                    if any((px < x < px+pw and py < y < py+ph)
                           for px, py, pw, ph in personas):
                        unknowns.append({'emb': emb})
                else:
                    known_names.add(name)

            # --- decidir eventos ---
            evento: dict | None = None
            is_group = len(unknowns) >= 2

            if known_names:
                persona = ', '.join(sorted(known_names))
                evento = {
                    "person_name": persona,
                    "timestamp": utc_now.isoformat(),
                    "event_type": "known_person",
                    "event_details": f"{persona} en cámara {device_id}.",
                    "device_id": device_id
                }

            if unknowns and len(unknowns) == 1:
                evento = {
                    "person_name": "Desconocido",
                    "timestamp": utc_now.isoformat(),
                    "event_type": "unknown_person",
                    "event_details": f("Rostro no identificado en {device_id}.") ,
