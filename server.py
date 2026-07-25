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
    Inicia la tarea de subtítulos (STT) usando estrictamente la API v7 de Agora.
    """
    body = await request.json()
    channel_name = body.get("channelName")
    spoken_lang = body.get("spokenLang", "es-ES")
    subtitle_lang = body.get("subtitleLang", "es-ES")
    
    if not channel_name:
        raise HTTPException(status_code=400, detail="channelName is required")

    headers = get_basic_auth_header()

    # PASO ÚNICO: Start Task (API v7)
    join_url = f"https://api.agora.io/api/speech-to-text/v1/projects/{APP_ID}/join"
    
    bot_token = RtcTokenBuilder.buildTokenWithUid(
        APP_ID, APP_CERTIFICATE, channel_name, 999, 1, int(time.time()) + 7200
    )

    join_payload = {
        "name": f"katia_stt_{channel_name}_{int(time.time())}",
        "languages": [spoken_lang], 
        "maxIdleTime": 60,
        "rtcConfig": {
            "channelName": channel_name,
            "subBotUid": "999", 
            "subBotToken": bot_token,
            "pubBotUid": "999",
            "pubBotToken": bot_token
        }
    }

    # Map target languages to 2-letter codes for Azure Translation compatibility
    target_lang_map = {
        "es-ES": "es",
        "en-US": "en",
        "fr-FR": "fr",
        "de-DE": "de",
        "it-IT": "it",
        "ro-RO": "ro"
    }
    
    azure_spoken = target_lang_map.get(spoken_lang, spoken_lang)
    azure_target = target_lang_map.get(subtitle_lang, subtitle_lang)

    # Permitir que el bot escuche y reconozca ambos idiomas en la sala
    unique_languages = list(set([spoken_lang, subtitle_lang]))
    join_payload["languages"] = unique_languages

    if spoken_lang != subtitle_lang:
        join_payload["translateConfig"] = {
            "enable": True,
            "languages": [
                {"source": spoken_lang, "target": [azure_target]},
                {"source": subtitle_lang, "target": [azure_spoken]}
            ]
        }

    start_resp = requests.post(join_url, json=join_payload, headers=headers)
        
    if start_resp.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Failed to start STT: {start_resp.text}")
    
    resp_json = start_resp.json()
    task_id = resp_json.get("taskId") or resp_json.get("agent_id") or "unknown_task"
    
    active_tasks[channel_name] = {"taskId": task_id}

    return {"status": "success", "taskId": task_id, "message": "Subtitles AI joined the room"}

@app.post("/stop-subtitles")
async def stop_subtitles(request: Request):
    """
    Detiene la transcripción de Agora (API v7).
    """
    body = await request.json()
    channel_name = body.get("channelName")
    
    if channel_name not in active_tasks:
        return {"status": "ignored", "message": "No active STT task found for this channel"}
    
    task_info = active_tasks[channel_name]
    task_id = task_info["taskId"]
    
    headers = get_basic_auth_header()
    leave_url = f"https://api.agora.io/api/speech-to-text/v1/projects/{APP_ID}/leave"
    
    # Mandamos tanto agent_id como taskId por compatibilidad con distintas nomenclaturas de la API
    requests.post(leave_url, json={"agent_id": task_id, "taskId": task_id}, headers=headers)
    
    del active_tasks[channel_name]
    return {"status": "success", "message": "Subtitles AI stopped"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
