# Archivo: registration_worker.py

import io
import os
import time
import json
import cv2
import numpy as np
import firebase_admin
from firebase_admin import credentials, storage
from mtcnn import MTCNN
from keras_facenet import FaceNet

# ======== CONFIGURACIÓN (ajusta si es necesario) ========
# Asegúrate de que este archivo de credenciales esté en la misma carpeta o proporciona la ruta completa
SERVICE_ACCOUNT_FILE = 'security-cam-f322b-firebase-adminsdk-fbsvc-a3bf0dd37b.json' 
BUCKET_ID = 'security-cam-f322b.firebasestorage.app'

# Prefijos de las carpetas en Firebase Storage
PENDING_JOBS_PREFIX = 'face_registration_pending/'
COMPLETED_JOBS_PREFIX = 'embeddings_clientes/'

# ======== INICIALIZACIÓN DE FIREBASE Y MODELOS DE IA ========
try:
    cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
    firebase_admin.initialize_app(cred, {'storageBucket': BUCKET_ID})
    bucket = storage.bucket()
    print('[INFO] Firebase Admin SDK inicializado correctamente.')
except Exception as e:
    print(f"[ERROR] No se pudo inicializar Firebase: {e}")
    exit()

try:
    print('[INFO] Cargando modelos de IA (MTCNN y FaceNet)...')
    detector = MTCNN()
    embedder = FaceNet()
    print('[INFO] Modelos de IA cargados.')
except Exception as e:
    print(f"[ERROR] No se pudieron cargar los modelos de IA: {e}")
    exit()


def find_pending_batches():
    """Encuentra lotes de trabajo pendientes agrupando los archivos por su carpeta única (batch_id)."""
    all_blobs = bucket.list_blobs(prefix=PENDING_JOBS_PREFIX)
    batches = {}
    for blob in all_blobs:
        if blob.name.endswith('/'): # Ignorar las 'carpetas' vacías
            continue
        # La ruta es como: pending_prefix/user_email_safe/batch_id/filename.jpg
        # Nos interesa agrupar por 'pending_prefix/user_email_safe/batch_id/'
        batch_path = os.path.dirname(blob.name) + '/'
        if batch_path not in batches:
            batches[batch_path] = []
        batches[batch_path].append(blob)
    return batches

def process_batch(batch_path, blob_list):
    """Procesa un lote de imágenes para generar y guardar un archivo .npy."""
    print(f"\n[INFO] Nuevo lote de trabajo encontrado en: {batch_path}")
    
    # 1. Encontrar y leer el archivo de metadatos
    metadata_blob = next((b for b in blob_list if b.name.endswith('metadata.json')), None)
    if not metadata_blob:
        print(f"[ERROR] No se encontró metadata.json en el lote {batch_path}. Saltando lote.")
        # Aquí podrías añadir lógica para borrar este lote corrupto si lo deseas
        return

    try:
        metadata_str = metadata_blob.download_as_string()
        metadata = json.loads(metadata_str)
        person_name = metadata.get('person_name', 'nombre_desconocido')
        user_email = metadata.get('user_email', 'email_desconocido')
        print(f"[INFO] Procesando registro para '{person_name}' del usuario '{user_email}'.")
    except Exception as e:
        print(f"[ERROR] No se pudo leer o parsear metadata.json en {batch_path}: {e}")
        return

    # 2. Procesar cada imagen para obtener los embeddings
    embeddings = []
    image_blobs = [b for b in blob_list if not b.name.endswith('metadata.json')]
    
    for image_blob in image_blobs:
        try:
            print(f"  -> Procesando imagen: {os.path.basename(image_blob.name)}...")
            img_bytes = image_blob.download_as_bytes()
            img_np = np.frombuffer(img_bytes, np.uint8)
            img_rgb = cv2.imdecode(img_np, cv2.IMREAD_COLOR)
            
            # Detección de rostro
            faces = detector.detect_faces(img_rgb)
            if not faces:
                print(f"  [WARN] No se detectó rostro en {image_blob.name}. Saltando imagen.")
                continue
            
            x, y, w, h = faces[0]['box']
            x, y = abs(x), abs(y)
            face = img_rgb[y:y+h, x:x+w]
            face_resized = cv2.resize(face, (160, 160))
            
            # Generación de embedding
            embedding_vector = embedder.embeddings([face_resized])[0]
            embeddings.append(embedding_vector)
        except Exception as e:
            print(f"  [ERROR] Falló el procesamiento de la imagen {image_blob.name}: {e}")

    # 3. Guardar el archivo .npy si se generaron embeddings
    if not embeddings:
        print(f"[ERROR] No se pudo generar ningún embedding para el lote {batch_path}. No se creará archivo .npy.")
    else:
        user_email_safe = os.path.basename(os.path.dirname(os.path.dirname(batch_path)))
        safe_person_name = person_name.replace(" ", "_").lower()
        npy_filename = f"{safe_person_name}.npy"
        npy_path = f"{COMPLETED_JOBS_PREFIX}{user_email_safe}/{npy_filename}"
        
        print(f"[INFO] Se generaron {len(embeddings)} embeddings. Creando archivo en: {npy_path}")
        
        npy_data = {'name': person_name, 'embeddings': embeddings}
        
        # Convertir a bytes para subir a storage
        with io.BytesIO() as npy_buffer:
            np.save(npy_buffer, npy_data, allow_pickle=True)
            npy_buffer.seek(0)
            bucket.blob(npy_path).upload_from_file(npy_buffer, content_type='application/octet-stream')
        
        print(f"[SUCCESS] Archivo .npy para '{person_name}' subido correctamente.")

    # 4. Limpiar el lote procesado de la carpeta "pending"
    print(f"[INFO] Limpiando lote de trabajo: {batch_path}")
    for blob in blob_list:
        blob.delete()
    print("[INFO] Lote limpiado.")


def main():
    """Bucle principal del worker."""
    print("--- Worker de Registro Facial Iniciado ---")
    while True:
        try:
            pending_batches = find_pending_batches()
            if not pending_batches:
                print("No hay nuevos trabajos de registro. Esperando 15 segundos...", end='\r')
            else:
                for batch_path, blob_list in pending_batches.items():
                    process_batch(batch_path, blob_list)
            
            time.sleep(15)
        except Exception as e:
            print(f"\n[CRITICAL] Error en el bucle principal del worker: {e}")
            time.sleep(30) # Esperar un poco más si hay un error crítico


if __name__ == '__main__':
    main()