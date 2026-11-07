#/bin/python

'''
prérequis: pip install fastapi uvicorn httpx
usage : uvicorn script:app --host 0.0.0.0 --port 8000
'''

import time
import re
import logging
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.responses import Response
import httpx

app = FastAPI()

# -----------------------------------------------------------------------------
# 1. CONFIGURATION DU SYSTÈME DE LOGS
# -----------------------------------------------------------------------------
log_format = logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# Enregistrement rotatif dans un fichier (max 5 Mo, conserve 3 versions)
file_handler = RotatingFileHandler('iptv_server.log', maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
file_handler.setFormatter(log_format)

# Affichage en direct dans la console WSL / Terminal
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_format)

logger = logging.getLogger("IPTV_Server")
logger.setLevel(logging.INFO)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# -----------------------------------------------------------------------------
# 2. PARAMÈTRES RÉSEAU ET DONNÉES SOURCES (IPTV-ORG)
# -----------------------------------------------------------------------------
VALID_USER = ""
VALID_PASS = ""
SERVER_HOST = ""  # Votre IP WSL accessible depuis le téléphone
SERVER_PORT = 8000

# Catalogue de chaînes issues de la structure JSON d'IPTV-org
RAW_JSON_DATA = [
    # Remplacement par le flux officiel permanent de France 24 (HLS)
    {"channel": None, "feed": None, "title": "France 24 (Légal)", "url": "https://france24.com", "quality": "1080p", "labels": [], "user_agent": None, "referrer": None},
    # Remplacement par le flux officiel permanent d'Arte (HLS)
    {"channel": None, "feed": None, "title": "Arte (Légal)", "url": "https://akamaihd.net", "quality": "1080p", "labels": [], "user_agent": None, "referrer": None},
    {"channel": None, "feed": None, "title": "6ter (1080p)", "url": "http://149.62.177", "quality": "1080p", "labels": [], "user_agent": None, "referrer": None},
    {"channel": None, "feed": None, "title": "BFM TV (1080p)", "url": "https://sfr.net", "quality": "1080p", "labels": [], "user_agent": None, "referrer": None},
    {"channel": None, "feed": None, "title": "Canal+ Sport (1080p)", "url": "http://streambox.fr", "quality": "1080p", "labels": ["Geo-blocked"], "user_agent": None, "referrer": None},
    {"channel": None, "feed": None, "title": "M6 HD (1080p)", "url": "http://99.27.51", "quality": "1080p", "labels": [], "user_agent": None, "referrer": None},
    {"channel": None, "feed": None, "title": "TF1 (1080p)", "url": "http://149.62.177", "quality": "1080p", "labels": [], "user_agent": None, "referrer": None},
    {"channel": None, "feed": None, "title": "AL24 News", "url": "https://easybroadcast.io", "quality": "240p", "labels": [], "user_agent": None, "referrer": None},
    {"channel": None, "feed": None, "title": "El-Heddaf TV (1080p)", "url": "https://elheddaftv.com", "quality": "1080p", "labels": [], "user_agent": None, "referrer": None}
]

categories_db = {}  # { "Nom_Groupe": "ID_String" }
streams_db = {}     # { ID_Int: { metadata_dict } }

def init_xtream_database_from_json():
    """ Convertit et trie dynamiquement les objets structurés JSON au format Xtream Codes """
    stream_id_counter = 5000
    cat_id_counter = 1

    for item in RAW_JSON_DATA:
        title = item["title"]
        url = item["url"]

        # Classification thématique intelligente
        if "France" in title or "TF1" in title or "M6" in title or "6ter" in title:
            group = "Chaînes Généralistes FR"
        elif "BFM" in title or "AL24" in title:
            group = "Chaînes d'Actualités"
        elif "Sport" in title or "Heddaf" in title:
            group = "Chaînes de Sport"
        else:
            group = "Chaînes Internationales"

        if group not in categories_db:
            categories_db[group] = str(cat_id_counter)
            cat_id_counter += 1

        stream_id = stream_id_counter
        streams_db[stream_id] = {
            "name": title,
            "url": url,
            "category_id": categories_db[group],
            "container_extension": "m3u8" if "m3u8" in url else "ts"
        }
        stream_id_counter += 1

    logger.info(f"Moteur IPTV prêt : {len(categories_db)} catégories et {len(streams_db)} chaînes configurées.")

init_xtream_database_from_json()

def check_auth(username, password, client_ip):
    if username != VALID_USER or password != VALID_PASS:
        logger.warning(f"🔐 SÉCURITÉ - Échec d'authentification de l'IP {client_ip} (user tenté: '{username}')")
        raise HTTPException(status_code=403, detail="Invalid credentials")

# -----------------------------------------------------------------------------
# ROUTE FACETIEUSE XMLTV POUR IPTV SMARTERS PRO (ÉVITE LA 404 EPG)
# -----------------------------------------------------------------------------
@app.get("/xmltv.php")
def mock_xmltv(username: str = Query(None), password: str = Query(None)):
    # Si les identifiants ne sont pas fournis, on rejette poliment
    if username != VALID_USER or password != VALID_PASS:
        raise HTTPException(status_code=403, detail="Invalid credentials")

    # On renvoie une structure XMLTV minimale et valide
    xml_content = '<?xml version="1.0" encoding="UTF-8"?><tv></tv>'
    return Response(content=xml_content, media_type="application/xml")

# -----------------------------------------------------------------------------
# 3. ENDPOINT XTREAM CODES API AVEC TOUTES LES CLÉS DE COMPATIBILITÉ MOBILES
# -----------------------------------------------------------------------------
@app.get("/player_api.php")
def xtream_api(
    request: Request,
    username: str = Query(None),
    password: str = Query(None),
    action: str = Query(None)
):
    client_ip = request.client.host
    if not username or not password:
        logger.warning(f"⚠️ REQUÊTE REJETÉE - IP {client_ip} : Paramètres manquants.")
        return JSONResponse(status_code=400, content={"error": "Missing credentials"})

    check_auth(username, password, client_ip)
    current_timestamp = int(time.time())

    # --- ÉTAPE 1 : Authentification Initiale ---
    if action is None:
        time_now_string = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(current_timestamp))
        logger.info(f"✅ CLIENT CONNECTÉ - IP {client_ip} : L'utilisateur '{username}' a validé la mire d'accueil.")

        data_response = {
            "user_info": {
                "username": username,
                "password": password,
                "message": "Authentification Reussie",
                "auth": 1,
                "status": "Active",
                "exp_date": "1900000000",
                "is_trial": "0",
                "active_cons": "0",
                "created_at": "1700000000",
                "max_connections": "5",
                "allowed_output_formats": ["m3u8", "ts", "rtmp"]
            },
            "server_info": {
                "url": SERVER_HOST,
                "port": str(SERVER_PORT),
                "https_port": "443",
                "server_protocol": "http",
                "rtmp_port": "1935",
                "timezone": "Europe/Paris",
                "timestamp_now": current_timestamp,
                "time_now": time_now_string
            }
        }
        return JSONResponse(content=data_response)

    # --- ÉTAPE 2 : Chargement des Dossiers (Catégories) ---
    elif action == "get_live_categories":
        logger.info(f"📁 SYNCHRO - IP {client_ip} : Envoi des catégories IPTV.")
        categories_list = [
            {"category_id": cat_id, "category_name": cat_name, "parent_id": 0}
            for cat_name, cat_id in categories_db.items()
        ]
        return JSONResponse(content=categories_list)

    # --- ÉTAPE 3 : Chargement de la Liste de Chaînes ---
    elif action == "get_live_streams":
        logger.info(f"📺 SYNCHRO - IP {client_ip} : Envoi de la structure des chaînes.")
        output = []
        for idx, (stream_id, data) in enumerate(streams_db.items(), 1):
            output.append({
                "num": idx,
                "name": data["name"],
                "stream_id": int(stream_id),
                "stream_type": "live",
                "stream_icon": "https://wikimedia.org",
                "epg_channel_id": "",
                "added": "1700000000",
                "category_id": str(data["category_id"]),
                "container_extension": data["container_extension"],
                "custom_sid": "",
                "tv_archive": 0,
                "direct_source": "",
                "tv_archive_duration": 0
            })
        return JSONResponse(content=output)

    return JSONResponse(content=[])

# -----------------------------------------------------------------------------
# 4. TUNNEL REVERSE PROXY ASYNCHRONE ET EXTRA-TOLÉRANT AUX LOGS
# -----------------------------------------------------------------------------
@app.get("/live/{username}/{password}/{stream_id}.{extension}")
async def stream_router(username: str, password: str, stream_id: int, extension: str, request: Request):
    client_ip = request.client.host
    check_auth(username, password, client_ip)

    if stream_id in streams_db:
        channel_name = streams_db[stream_id]["name"]
        real_url = streams_db[stream_id]["url"]

        logger.info(f"🍿 LECTURE EN COURS - IP {client_ip} : '{username}' regarde '{channel_name}'")

        # Le script télécharge le flux à la volée avec gestion stricte des exceptions network
        async def stream_generator():
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            try:
                # verify=False désactive les blocages SSL (liens IPTV distants périmés) + Timeout à 12s
                async with httpx.AsyncClient(follow_redirects=True, verify=False, timeout=12.0) as client:
                    async with client.stream("GET", real_url, headers=headers) as response:
                        if response.status_code >= 400:
                            logger.error(f"❌ FLUX DISTANT EN ERREUR - Le serveur d'origine a renvoyé un code {response.status_code}")
                            return

                        # Buffer augmenté à 8 Ko pour maximiser la fluidité réseau sous WSL
                        async for chunk in response.aiter_bytes(chunk_size=8192):
                            yield chunk

            except httpx.RequestError as net_err:
                logger.error(f"❌ ERREUR PAQUETS RÉSEAU - Connexion rompue sur '{channel_name}': {net_err}")
            except Exception as global_err:
                logger.error(f"❌ ERREUR CRITIQUE TUNNEL - Échec du Reverse Proxy : {global_err}")

        media_type = "application/x-mpegURL" if extension == "m3u8" else "video/mp2t"
        return StreamingResponse(stream_generator(), media_type=media_type)

    logger.error(f"❌ FLUX INTROUVABLE - IP {client_ip} : Demande de flux inexistant ID {stream_id}")
    raise HTTPException(status_code=404, detail="Stream non trouvé")
