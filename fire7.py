import os
import io
import time
import cv2
import numpy as np
import random
from datetime import datetime
from mtcnn import MTCNN
from keras_facenet import FaceNet
from scipy.spatial.distance import cosine
import requests

import firebase_admin
from firebase_admin import credentials, storage

# ========== CONFIGURACIÓN FIREBASE ==========
SERVICE_ACCOUNT_FILE = 'security-cam-f322b-firebase-adminsdk-fbsvc-a3bf0dd37b.json'
BUCKET_NAME = 'security-cam-f322b.firebasestorage.app'

# Carpeta de imágenes a procesar y de embeddings EN FIREBASE
FIREBASE_PATH_FOTOS = 'test/'                   # Aquí van las fotos a procesar (modifica para cada cliente)
FIREBASE_PATH_EMBEDDINGS = 'clientes/embeddings/'  # Embeddings de cada cliente
FIREBASE_PATH_ALARMAS = 'clientes/alarmas/'              # Carpeta en firebase donde subir alarmas

# ========== CONFIGURACIÓN LOCAL ==========
CARPETA_LOCAL_FOTOS = '/tmp/fotos/'
CARPETA_LOCAL_EMBEDDINGS = '/tmp/embeddings/'
CARPETA_LOCAL_ALARMAS = '/tmp/alarmas/'
for d in [CARPETA_LOCAL_FOTOS, CARPETA_LOCAL_EMBEDDINGS, CARPETA_LOCAL_ALARMAS]:
    os.makedirs(d, exist_ok=True)

# ========== PARÁMETROS DEL SISTEMA ==========
DISTANCE_THRESHOLD = 0.5
SIMILARITY_THRESHOLD = 0.4
DETECCIONES_REQUERIDAS = 3
cooldown_seconds = 30  # segundos

# ========== INICIALIZACIÓN FIREBASE ==========
cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
firebase_admin.initialize_app(cred, {
    'storageBucket': BUCKET_NAME,
})
bucket = storage.bucket()

# ========== INICIALIZAR MODELOS ==========
embedder = FaceNet()
detector = MTCNN()
model = torch.hub.load('ultralytics/yolov5', 'yolov5x')
class_names = model.names

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
        except: pass

# ========== GESTIÓN DE EMBEDDINGS ==========
def descargar_embeddings_firebase():
    print("[INFO] Descargando embeddings de Firebase...")
    limpiar_carpeta(CARPETA_LOCAL_EMBEDDINGS)
    blobs = bucket.list_blobs(prefix=FIREBASE_PATH_EMBEDDINGS)
    count = 0
    for blob in blobs:
        if blob.name.endswith('.npy') and not blob.name.endswith('/'):
            local_path = os.path.join(CARPETA_LOCAL_EMBEDDINGS, os.path.basename(blob.name))
            blob.download_to_filename(local_path)
            count += 1
    print(f"[INFO] ¡Descarga de embeddings terminada! ({count} archivos)")

def cargar_embeddings():
    known_embeddings = []
    known_labels = []
    for file in os.listdir(CARPETA_LOCAL_EMBEDDINGS):
        if file.endswith('.npy'):
            vec = np.load(os.path.join(CARPETA_LOCAL_EMBEDDINGS, file), allow_pickle=True).item()
            for emb in vec['embeddings']:
                known_embeddings.append(emb)
                known_labels.append(vec['name'])
    print(f"Embeddings cargados: {len(known_embeddings)}")
    print(f"Etiquetas de conocidos: {set(known_labels)}")
    return known_embeddings, known_labels

# ========== GESTIÓN DE FOTOS ==========
def descargar_fotos_firebase():
    print("[INFO] Descargando imágenes de Firebase...")
    limpiar_carpeta(CARPETA_LOCAL_FOTOS)
    blobs = bucket.list_blobs(prefix=FIREBASE_PATH_FOTOS)
    imagenes = []
    for blob in blobs:
        if not blob.name.endswith('/') and (blob.name.lower().endswith('.jpg') or blob.name.lower().endswith('.jpeg') or blob.name.lower().endswith('.png')):
            local_path = os.path.join(CARPETA_LOCAL_FOTOS, os.path.basename(blob.name))
            blob.download_to_filename(local_path)
            imagenes.append({'blob': blob, 'local_path': local_path, 'nombre': os.path.basename(blob.name)})
    print(f"[INFO] Descargadas {len(imagenes)} imágenes.")
    return imagenes

# ========== ALERTA IFTTT ==========
def enviar_alerta_ifttt(path_local_imagen):
    nombre_en_firebase = FIREBASE_PATH_ALARMAS + os.path.basename(path_local_imagen)
    blob = bucket.blob(nombre_en_firebase)
    blob.upload_from_filename(path_local_imagen)
    blob.make_public()
    url_publica = blob.public_url
    print(f"[ALERTA] Imagen subida a {url_publica}")

    ifttt_webhook_url = "https://maker.ifttt.com/trigger/send_alarm/with/key/lxBuXKITSTN5Gu-bhN_m28waEiJoprM-ClSGvq69n7k"
    data = {
        'value1': "Rostro desconocido detectado con personas",
        'value2': url_publica,
        'value3': "Aquí está la imagen de la inferencia combinada"
    }
    try:
        response = requests.post(ifttt_webhook_url, data=data)
        if response.status_code == 200:
            print("✅ Alarma enviada correctamente a IFTTT.")
        else:
            print(f"❌ Error al enviar el webhook a IFTTT: {response.status_code}, {response.text}")
    except Exception as e:
        print(f"❌ Error al enviar a IFTTT: {e}")

def enviar_alerta_persona_sin_rostro(path_local_imagen):
    # Sube imagen procesada a firebase en carpeta alarmas/
    nombre_en_firebase = FIREBASE_PATH_ALARMAS + os.path.basename(path_local_imagen)
    blob = bucket.blob(nombre_en_firebase)
    blob.upload_from_filename(path_local_imagen)
    blob.make_public()
    url_publica = blob.public_url
    print(f"[ALERTA] Imagen subida a {url_publica} (sin rostro)")

    webhook_url = "https://maker.ifttt.com/trigger/persona_sin_rostro/with/key/lxBuXKITSTN5Gu-bhN_m28waEiJoprM-ClSGvq69n7k"
    data = {
        'value1': "Persona detectada 3 veces sin rostro",
        'value2': url_publica,
        'value3': "Alerta por detección de persona sin rostro"
    }
    try:
        response = requests.post(webhook_url, data=data)
        if response.status_code == 200:
            print("✅ Alerta de persona sin rostro enviada correctamente.")
        else:
            print(f"❌ Error al enviar alerta de persona sin rostro: {response.status_code}, {response.text}")
    except Exception as e:
        print(f"❌ Error al enviar a IFTTT (persona sin rostro): {e}")

# ========== PROCESAMIENTO PRINCIPAL ==========
def procesar_imagenes():
    historial_desconocidos = []
    persona_sin_rostro_contador = 0
    last_group_alert_time = None
    last_embeddings_download = 0
    embeddings_update_interval = 600  # 10 minutos
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

        now = datetime.now()
        historial_desconocidos[:] = [item for item in historial_desconocidos if (now - item.get('ultima_vista', now)).total_seconds() <= 60]

        for img_dict in imagenes:
            local_path = img_dict['local_path']
            blob = img_dict['blob']
            nombre_archivo = img_dict['nombre']

            img = cv2.imread(local_path)
            if img is None:
                print(f"[ERROR] No se pudo cargar la imagen {local_path}.")
                continue

            results = model(img)
            detections = results.xywh[0]
            img_result = img.copy()

            personas_detectadas = []
            for det in detections:
                x, y, w, h, conf, cls = det
                if conf < 0.5:
                    continue
                if class_names[int(cls)] == "person":
                    personas_detectadas.append((int(x - w/2), int(y - h/2), int(w), int(h)))
                    cv2.rectangle(img_result, (int(x - w/2), int(y - h/2)),
                                  (int(x + w/2), int(y + h/2)), (0, 255, 255), 2)
            print(f"[INFO] {len(personas_detectadas)} persona(s) detectada(s) en {nombre_archivo}")

            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            faces = detector.detect_faces(rgb)
            rostros_validados = []
            grupo_disparado = False

            conocidos_detectados = set()

            for d in faces:
                x, y, w, h = d['box']
                x, y = abs(x), abs(y)
                if w < 30 or h < 30:
                    continue

                rostro = rgb[y:y+h, x:x+w]
                if rostro.size == 0:
                    continue

                rostro_resized = cv2.resize(rostro, (160, 160))
                rostro_array = np.expand_dims(rostro_resized, axis=0)
                embedding = embedder.embeddings(rostro_array)[0]

                nombre = "Desconocido"
                dist_min = float('inf')

                for i, known_vec in enumerate(known_embeddings):
                    dist = cosine(embedding, known_vec)
                    if dist < dist_min:
                        dist_min = dist
                        if dist < DISTANCE_THRESHOLD:
                            nombre = known_labels[i]
                if nombre != "Desconocido":
                    conocidos_detectados.add(nombre)

                color = (0, 255, 0) if nombre != "Desconocido" else (0, 0, 255)
                cv2.rectangle(img_result, (x, y), (x + w, y + h), color, 2)
                cv2.rectangle(img_result, (x, y - 25), (x + w, y), color, -1)
                agregar_borde_texto(img_result, nombre, (x, y - 10),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                                    (255, 255, 255), (0, 0, 0), 2)

                if nombre == "Desconocido":
                    for (px, py, pw, ph) in personas_detectadas:
                        if rect_overlap(px, py, pw, ph, x, y, w, h):
                            rostros_validados.append({
                                'embedding': embedding,
                                'bbox': (x, y, w, h)
                            })
                            break
            print(f"[INFO] Personas conocidas detectadas en la imagen: {list(conocidos_detectados)}")
            print(f"[INFO] {len(rostros_validados)} rostro(s) desconocido(s) detectado(s) en {nombre_archivo}")

            if len(faces) == 0 and len(personas_detectadas) > 0:
                persona_sin_rostro_contador += 1
                print(f"[INFO] Persona(s) sin rostro detectada ({persona_sin_rostro_contador}/3)")
                if persona_sin_rostro_contador >= 3:
                    persona_sin_rostro_contador = 0
                    timestamp = now.strftime('%Y%m%d_%H%M%S')
                    output_path = os.path.join(CARPETA_LOCAL_ALARMAS, f"persona_sin_rostro_{timestamp}.jpg")
                    cv2.imwrite(output_path, img_result)
                    enviar_alerta_persona_sin_rostro(output_path)
            else:
                persona_sin_rostro_contador = 0

            if len(rostros_validados) >= 2:
                if last_group_alert_time is None or (now - last_group_alert_time).total_seconds() > cooldown_seconds:
                    last_group_alert_time = now
                    timestamp = now.strftime('%Y%m%d_%H%M%S')
                    output_path = os.path.join(CARPETA_LOCAL_ALARMAS, f"alerta_grupal_{timestamp}.jpg")
                    cv2.imwrite(output_path, img_result)
                    enviar_alerta_ifttt(output_path)

            for rostro_data in rostros_validados:
                emb = rostro_data['embedding']
                match_found = False

                for item in historial_desconocidos:
                    dist = cosine(emb, item['embedding'])
                    if dist < SIMILARITY_THRESHOLD:
                        item['contador'] += 1
                        item['ultima_vista'] = now
                        match_found = True
                        if item['contador'] >= DETECCIONES_REQUERIDAS and not grupo_disparado:
                            tiempo_ultima = item.get('ultima_alarma', datetime.min)
                            if (now - tiempo_ultima).total_seconds() > cooldown_seconds:
                                timestamp = now.strftime('%Y%m%d_%H%M%S')
                                output_path = os.path.join(CARPETA_LOCAL_ALARMAS, f"alerta_{timestamp}.jpg")
                                cv2.imwrite(output_path, img_result)
                                enviar_alerta_ifttt(output_path)
                                item['ultima_alarma'] = now
                        break

                if not match_found:
                    historial_desconocidos.append({
                        'embedding': emb,
                        'contador': 1,
                        'ultima_alarma': datetime.min,
                        'ultima_vista': now
                    })

            # ========== LIMPIEZA ==========
            try:
                os.remove(local_path)
            except:
                pass
            # blob.delete()  # <<--- DESCOMENTA SOLO CUANDO QUIERAS BORRAR DEL BUCKET

        print("⏳ Esperando nuevas imágenes...")
        time.sleep(10)

if __name__ == "__main__":
    procesar_imagenes()
