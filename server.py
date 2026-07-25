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
    body = await request.json()
    channel_name = body.get("channelName")
    spoken_lang = body.get("spokenLang")
    subtitle_lang = body.get("subtitleLang")
    doctor_uid = body.get("doctorUid")
    patient_uid = body.get("patientUid")
    
    if not channel_name or not spoken_lang or not subtitle_lang:
        raise HTTPException(status_code=400, detail="Missing required fields")

    # Si ya hay tareas activas, detenerlas primero
    if channel_name in active_tasks:
        try:
            await stop_subtitles(request)
        except:
            pass

    headers = get_basic_auth_header()
    join_url = f"https://api.agora.io/api/speech-to-text/v1/projects/{APP_ID}/join"

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

    # 1. BOT PARA EL DOCTOR (Entiende spoken_lang, traduce a subtitle_lang)
    bot_token_doc = RtcTokenBuilder.buildTokenWithUid(
        APP_ID, APP_CERTIFICATE, channel_name, 998, 1, int(time.time()) + 7200
    )
    payload_doc = {
        "name": f"stt_doc_{channel_name}_{int(time.time())}",
        "languages": [spoken_lang], 
        "maxIdleTime": 60,
        "rtcConfig": {
            "channelName": channel_name,
            "subBotUid": "998", 
            "subBotToken": bot_token_doc,
            "pubBotUid": "998",
            "pubBotToken": bot_token_doc
        }
    }
    
    # El bot del doctor SOLO escucha al doctor para máxima precisión
    if doctor_uid:
        payload_doc["rtcConfig"]["subscribeAudioUid"] = [str(doctor_uid)]

    if spoken_lang != subtitle_lang:
        payload_doc["translateConfig"] = {
            "enable": True,
            "languages": [{"source": spoken_lang, "target": [azure_target]}]
        }

    start_resp_doc = requests.post(join_url, json=payload_doc, headers=headers)
    if start_resp_doc.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Failed to start Doc STT: {start_resp_doc.text}")
    
    task_id_doc = start_resp_doc.json().get("taskId") or start_resp_doc.json().get("agent_id")

    # 2. BOT PARA EL PACIENTE (Entiende subtitle_lang, traduce a spoken_lang)
    # SOLO se lanza si el paciente ya está en la sala y los idiomas son distintos
    task_id_pat = None
    if patient_uid and spoken_lang != subtitle_lang:
        bot_token_pat = RtcTokenBuilder.buildTokenWithUid(
            APP_ID, APP_CERTIFICATE, channel_name, 999, 1, int(time.time()) + 7200
        )
        payload_pat = {
            "name": f"stt_pat_{channel_name}_{int(time.time())}",
            "languages": [subtitle_lang], 
            "maxIdleTime": 60,
            "rtcConfig": {
                "channelName": channel_name,
                "subBotUid": "999", 
                "subBotToken": bot_token_pat,
                "pubBotUid": "999",
                "pubBotToken": bot_token_pat,
                "subscribeAudioUid": [str(patient_uid)]
            },
            "translateConfig": {
                "enable": True,
                "languages": [{"source": subtitle_lang, "target": [azure_spoken]}]
            }
        }
        
        start_resp_pat = requests.post(join_url, json=payload_pat, headers=headers)
        if start_resp_pat.status_code == 200:
            task_id_pat = start_resp_pat.json().get("taskId") or start_resp_pat.json().get("agent_id")

    active_tasks[channel_name] = {
        "taskId": task_id_doc,
        "taskIdPat": task_id_pat
    }

    return {"status": "success", "taskId": task_id_doc, "message": "Bidirectional STT joined"}

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
    task_id_doc = task_info.get("taskId")
    task_id_pat = task_info.get("taskIdPat")
    
    headers = get_basic_auth_header()
    leave_url = f"https://api.agora.io/api/speech-to-text/v1/projects/{APP_ID}/leave"
    
    if task_id_doc:
        requests.post(leave_url, json={"agent_id": task_id_doc, "taskId": task_id_doc}, headers=headers)
    if task_id_pat:
        requests.post(leave_url, json={"agent_id": task_id_pat, "taskId": task_id_pat}, headers=headers)
    
    del active_tasks[channel_name]
    return {"status": "success", "message": "Subtitles AI stopped"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
