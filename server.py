import os
import base64
import requests
import time
import json
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
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

# Claves de las IAs
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "d76a065bbba6b31d34d3961140fe90fcea6d9f1b").strip()
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", "786f1cec-dc44-41d2-a693-dd0feaa2414f:fx").strip()

# Gestor de Conexiones WebSocket por sala (canal)
class ConnectionManager:
    def __init__(self):
        # channel_name -> list of WebSockets
        self.active_connections: dict[str, list[WebSocket]] = {}
        # channel_name -> { "spokenLang": "es", "subtitleLang": "en" }
        self.channel_configs = {}

    async def connect(self, websocket: WebSocket, channel_name: str):
        await websocket.accept()
        if channel_name not in self.active_connections:
            self.active_connections[channel_name] = []
        self.active_connections[channel_name].append(websocket)

    def disconnect(self, websocket: WebSocket, channel_name: str):
        if channel_name in self.active_connections:
            self.active_connections[channel_name].remove(websocket)
            if not self.active_connections[channel_name]:
                del self.active_connections[channel_name]

    async def broadcast(self, message: dict, channel_name: str):
        if channel_name in self.active_connections:
            for connection in self.active_connections[channel_name]:
                try:
                    await connection.send_json(message)
                except:
                    pass

manager = ConnectionManager()

def translate_text(text: str, source_lang: str, target_lang: str) -> str:
    """Traduce texto usando la API de DeepL"""
    if not text or not text.strip():
        return ""
    
    # DeepL usa códigos de 2 letras, ej: "es-ES" -> "ES"
    # Pero "en-US" -> "EN-US" en DeepL (o solo "EN")
    target_dl = target_lang.split("-")[0].upper()
    if target_lang == "en-US":
        target_dl = "EN-US"
    elif target_lang == "en-GB":
        target_dl = "EN-GB"

    url = "https://api-free.deepl.com/v2/translate"
    headers = {
        "Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "text": [text],
        "target_lang": target_dl
    }
    
    try:
        resp = requests.post(url, headers=headers, json=data)
        if resp.status_code == 200:
            return resp.json()["translations"][0]["text"]
        else:
            print(f"DeepL Error: {resp.text}")
            return text
    except Exception as e:
        print(f"Translation exception: {e}")
        return text

@app.get("/")
def read_root():
    return {"status": "Katiatupediatra Realtime AI Backend is running"}

@app.get("/get-ai-keys")
def get_ai_keys():
    """Devuelve las claves al frontend para que se conecte directamente a Deepgram"""
    return {
        "deepgram": DEEPGRAM_API_KEY
    }

@app.get("/generate-token")
def generate_token(channelName: str, uid: int = 0):
    if not APP_ID or not APP_CERTIFICATE:
        raise HTTPException(status_code=500, detail="Missing App ID or Certificate")
    expiration_time = 7200
    current_time = int(time.time())
    token = RtcTokenBuilder.buildTokenWithUid(
        APP_ID, APP_CERTIFICATE, channelName, uid, 1, current_time + expiration_time
    )
    return {"token": token, "uid": uid, "channelName": channelName}

@app.post("/start-subtitles")
async def start_subtitles(request: Request):
    """
    Ahora solo guarda la configuración de idiomas de la sala. 
    Ya no lanza bots de Agora.
    """
    body = await request.json()
    channel_name = body.get("channelName")
    spoken_lang = body.get("spokenLang")
    subtitle_lang = body.get("subtitleLang")
    
    if channel_name:
        manager.channel_configs[channel_name] = {
            "doctorLang": spoken_lang,
            "patientLang": subtitle_lang
        }

    return {"status": "success", "message": "Subtitles AI config saved"}

@app.post("/stop-subtitles")
async def stop_subtitles(request: Request):
    # No hay bots que detener
    return {"status": "success", "message": "Subtitles AI stopped"}

@app.get("/room-config/{channel_name}")
def get_room_config(channel_name: str):
    """El frontend del paciente llama a esto para saber qué idioma hablar"""
    config = manager.channel_configs.get(channel_name, {"doctorLang": "es-ES", "patientLang": "en-US"})
    return config

@app.websocket("/ws/subtitles/{channel_name}")
async def websocket_endpoint(websocket: WebSocket, channel_name: str):
    """
    Recibe los textos de Deepgram desde el navegador, los traduce con DeepL,
    y los retransmite a toda la sala.
    """
    await manager.connect(websocket, channel_name)
    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            
            uid = payload.get("uid")
            is_doctor = payload.get("isDoctor", False)
            original_text = payload.get("text", "")
            
            if not original_text.strip():
                continue
                
            config = manager.channel_configs.get(channel_name, {"doctorLang": "es-ES", "patientLang": "en-US"})
            doc_lang = config["doctorLang"]
            pat_lang = config["patientLang"]
            
            source_lang = doc_lang if is_doctor else pat_lang
            target_lang = pat_lang if is_doctor else doc_lang
            
            # Solo traducir si son diferentes
            translated_text = original_text
            if source_lang.split("-")[0] != target_lang.split("-")[0]:
                translated_text = translate_text(original_text, source_lang, target_lang)
                
            # Broadcast a la sala
            await manager.broadcast({
                "uid": uid,
                "isDoctor": is_doctor,
                "original": original_text,
                "translated": translated_text,
                "sourceLang": source_lang,
                "targetLang": target_lang
            }, channel_name)
            
    except WebSocketDisconnect:
        manager.disconnect(websocket, channel_name)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
