import os
import base64
import requests
import time
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from agora_token_builder import RtcTokenBuilder

load_dotenv()

app = FastAPI()

# Permitir conexiones desde cualquier frontend (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

APP_ID = os.getenv("AGORA_APP_ID", "").strip()
APP_CERTIFICATE = os.getenv("AGORA_APP_CERTIFICATE", "").strip()
CUSTOMER_KEY = os.getenv("AGORA_CUSTOMER_KEY", "").strip()
CUSTOMER_SECRET = os.getenv("AGORA_CUSTOMER_SECRET", "").strip()

# Diccionario en memoria para guardar el taskId de la transcripción y poder detenerla luego
active_tasks = {}

def get_basic_auth_header():
    credentials = f"{CUSTOMER_KEY}:{CUSTOMER_SECRET}"
    encoded = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/json"
    }

@app.get("/")
def read_root():
    return {"status": "Katiatupediatra Agora STT Backend is running"}

@app.get("/generate-token")
def generate_token(channelName: str, uid: int = 0):
    """
    Genera el token seguro de Agora para que Katia y el paciente entren a la videollamada.
    """
    if not APP_ID or not APP_CERTIFICATE:
        raise HTTPException(status_code=500, detail="Missing App ID or Certificate")
    
    # El token expira en 2 horas (7200 segundos)
    expiration_time = 7200
    current_time = int(time.time())
    privilege_expired_ts = current_time + expiration_time

    token = RtcTokenBuilder.buildTokenWithUid(
        APP_ID, APP_CERTIFICATE, channelName, uid, 1, privilege_expired_ts
    )
    return {"token": token, "uid": uid, "channelName": channelName}

@app.post("/start-subtitles")
async def start_subtitles(request: Request):
    """
    Inicia la tarea de subtítulos (STT) enviando una orden a los servidores de Agora.
    """
    body = await request.json()
    channel_name = body.get("channelName")
    spoken_lang = body.get("spokenLang", "es-ES")
    subtitle_lang = body.get("subtitleLang", "es-ES")
    
    if not channel_name:
        raise HTTPException(status_code=400, detail="channelName is required")

    headers = get_basic_auth_header()

    # PASO 1: Pedir permiso (Acquire Builder Token)
    acquire_url = f"https://api.agora.io/v1/projects/{APP_ID}/rtsc/speech-to-text/builderTokens"
    acquire_payload = {"instanceId": f"katia_stt_{channel_name}_{int(time.time())}"}
    
    acquire_resp = requests.post(acquire_url, json=acquire_payload, headers=headers)
    if acquire_resp.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Failed to acquire token: {acquire_resp.text}")
    
    builder_token = acquire_resp.json().get("tokenName")

    # PASO 2: Iniciar el bot de transcripción (Start Task)
    start_url = f"https://api.agora.io/v1/projects/{APP_ID}/rtsc/speech-to-text/tasks"
    
    features = ["RECOGNIZE"]
    if spoken_lang != subtitle_lang:
        features.append("TRANSLATE")

    start_payload = {
        "tokenName": builder_token,
        "languages": [spoken_lang], 
        "agoraRtcConfig": {
            "channelName": channel_name,
            "uid": "999", 
            "token": RtcTokenBuilder.buildTokenWithUid(
                APP_ID, APP_CERTIFICATE, channel_name, 999, 1, int(time.time()) + 7200
            )
        },
        "config": {
            "features": features,
            "recognizeConfig": {
                "language": spoken_lang
            }
        }
    }

    if "TRANSLATE" in features:
        start_payload["config"]["translateConfig"] = {
            "languages": [
                {"source": spoken_lang, "target": [subtitle_lang]}
            ]
        }

    start_resp = requests.post(start_url, json=start_payload, headers=headers)
    
    if start_resp.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Failed to start STT: {start_resp.text}")
    
    task_id = start_resp.json().get("taskId")
    
    # Guardamos el task_id para poder apagarlo después
    active_tasks[channel_name] = {"taskId": task_id, "builderToken": builder_token}

    return {"status": "success", "taskId": task_id, "message": "Subtitles AI joined the room"}

@app.post("/stop-subtitles")
async def stop_subtitles(request: Request):
    """
    Detiene la transcripción de Agora y saca al bot de la sala para ahorrar minutos.
    """
    body = await request.json()
    channel_name = body.get("channelName")
    
    if channel_name not in active_tasks:
        return {"status": "ignored", "message": "No active STT task found for this channel"}
    
    task_info = active_tasks[channel_name]
    task_id = task_info["taskId"]
    builder_token = task_info["builderToken"]
    
    headers = get_basic_auth_header()
    stop_url = f"https://api.agora.io/v1/projects/{APP_ID}/rtsc/speech-to-text/tasks/{task_id}?builderToken={builder_token}"
    
    stop_resp = requests.delete(stop_url, headers=headers)
    
    if stop_resp.status_code == 200:
        del active_tasks[channel_name]
        return {"status": "success", "message": "Subtitles AI stopped"}
    else:
        raise HTTPException(status_code=500, detail=f"Failed to stop STT: {stop_resp.text}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
